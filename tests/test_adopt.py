"""Adopt and rollback as orderings of OWNER verbs, against fake owners in a
scratch HOME with a scratch Homebrew prefix and a scratch Contorch.app. No
launchctl, brew, claude or tccutil runs for real; pm never touches
context.db, the chroma folder, settings.json or CLAUDE.md itself."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FakeOwner, cm_claude, mc_agent
from pipeline_monitor import adopt, channel, contorch, modules, owners

PYTHON = sys.executable          # before any test points sys.executable into a scratch app


class World:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp, self.mp = tmp_path, monkeypatch
        self.log = tmp_path / "owner-calls.jsonl"
        monkeypatch.setenv("FAKE_OWNER_LOG", str(self.log))
        self.prefix = tmp_path / "brew"
        self.app = tmp_path / "Applications" / "Contorch.app"
        self.system: list[list[str]] = []
        self.brew_version = "0.4.1"
        self.ps = ""
        monkeypatch.setattr(adopt, "run_system", self._system)
        # Homebrew: three formulas, their opt/ wrappers (fakes), a brew binary
        for f in adopt.FORMULAS:
            (self.prefix / "Cellar" / f / "0.4.0").mkdir(parents=True)
        (self.prefix / "bin").mkdir(parents=True)
        (self.prefix / "bin" / "brew").write_text("")
        self.brew = {
            "meeting-capture": FakeOwner(self.prefix / "opt" / "meeting-capture" / "bin", "meeting-capture"),
            "contorch-memory": FakeOwner(self.prefix / "opt" / "context-orchestrator" / "bin", "contorch-memory"),
            "contorch": FakeOwner(self.prefix / "opt" / "contorch" / "bin", "contorch"),
        }
        bindir = self.app / "Contents" / "Resources" / "bin"
        self.appo = {n: FakeOwner(bindir, n) for n in ("meeting-capture", "contorch-memory", "contorch")}
        for o in (*self.brew.values(), *self.appo.values()):
            self.defaults(o)

    def defaults(self, o: FakeOwner) -> None:
        guarded = ["install", "uninstall", "start", "stop", "skill"] if o.name == "meeting-capture" else ["claude"]
        o.set(guarded=guarded)
        if o.name == "meeting-capture":
            o.answer("config", {"schema": "meeting-capture.config/1", "ok": True, "watch_paths": [],
                                "agent": {"backend": "launchctl", "installed": True}, "settings": {}})
            o.answer("status", {"schema": "meeting-capture.status/1", "ok": True, "recording": False,
                                "state": "idle"})
            for verb in ("stop", "install", "uninstall"):
                o.answer(verb, mc_agent(verb))
            o.answer("skill", {"schema": "meeting-capture.skill/1", "ok": True, "action": "linked"})
        elif o.name == "contorch-memory":
            o.answer("status", {"schema": "contorch-memory.status/1", "ok": True, "vector_index": "server",
                                "index_compatible": True, "chromadb_version": "1.5.9"})
            o.answer("index migrate", {"schema": "contorch-memory.backup/1", "ok": True, "action": "index_migrate",
                                       "performed": True, "todo": ["Run `contorch-memory claude install` …"]})
            o.answer("backup", {"schema": "contorch-memory.backup/1", "ok": True, "action": "backup"})
            o.answer("restore", {"schema": "contorch-memory.backup/1", "ok": True, "action": "restore"})
            o.answer("claude install", cm_claude("install"))
            o.answer("claude uninstall", cm_claude("uninstall"))
            o.answer("claude status", cm_claude("status"))

    def _system(self, argv, timeout=900):
        self.system.append([Path(argv[0]).name, *argv[1:]])
        if argv[1:3] == ["list", "--versions"]:
            return subprocess.CompletedProcess(argv, 0, f"contorch {self.brew_version}\n", "")
        if argv[0] == "ps":
            return subprocess.CompletedProcess(argv, 0, self.ps, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def as_app(self):
        self.mp.setenv("CONTORCH_CHANNEL", "app")
        self.mp.setattr(owners.sys, "executable", str(self.app / "Contents" / "MacOS" / "contorch-python"))

    def as_brew(self):
        self.mp.setenv("CONTORCH_CHANNEL", "brew")
        self.mp.setattr(owners.sys, "executable", str(self.tmp / "home" / ".contorch" / "venv" / "bin" / "python"))

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        out = []
        for c in (json.loads(l) for l in self.log.read_text().splitlines()):
            where = "app" if "/Contorch.app/" in c["exe"] else "brew"
            out.append({**c, "where": where, "cmd": " ".join(a for a in c["args"] if a != "--json")})
        return out

    def changes(self) -> list[str]:
        """Calls that change something, as 'where:name cmd' (reads left out)."""
        reads = ("config", "status", "claude status")
        return [f"{c['where']}:{c['name']} {c['cmd']}" for c in self.calls()
                if not any(c["cmd"] == r or c["cmd"].startswith(r + " --channel") for r in reads)]


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def brew_owned(world):
    world.as_brew()
    channel.claim()
    assert channel.read()["owner"] == "brew"


# ------------------------------------------------------------ adopt brew → app

def test_adopt_brew_to_app_orders_owner_verbs_and_backs_up_first(world):
    brew_owned(world)
    world.as_app()
    plan = adopt.plan_adopt()
    assert plan["ok"] and plan["from"] == "brew" and plan["to"] == "app"
    res = adopt.execute(plan)
    assert res["ok"], res
    ch = world.changes()
    bdir = plan["backup_dir"]
    assert ch == [
        "brew:meeting-capture stop --reason update",
        f"app:contorch-memory index migrate --in-process --backup-dir {bdir}/memory",
        f"app:meeting-capture install --adopt --no-load --backup-dir {bdir}",
        f"app:contorch-memory claude install --channel app --backup-dir {bdir}",
        "app:meeting-capture skill install",
    ]
    assert ["brew", "services", "stop", "contorch/tap/contorch"] in world.system
    assert ["brew", "unlink", *adopt.FORMULAS] in world.system and ["brew", "pin", *adopt.FORMULAS] in world.system
    assert world.system.index(["brew", "services", "stop", "contorch/tap/contorch"]) < \
        world.system.index(["brew", "unlink", *adopt.FORMULAS])
    m = channel.read()
    assert m["owner"] == "app" and m["writers"] == ["app"] and m["state"] == "ok" and "op" not in m
    assert m["adopted_from"]["channel"] == "brew" and m["adopted_from"]["pinned"] == list(adopt.FORMULAS)
    assert {"grant_permissions", "brew_cleanup_later"} <= {t["code"] for t in res["todo"]}


def test_every_child_carries_the_op_token_and_its_own_channel(world):
    brew_owned(world)
    world.as_app()
    plan = adopt.plan_adopt()
    adopt.execute(plan)
    for c in world.calls():
        if c["cmd"].startswith(("stop", "install", "index", "claude install", "skill")):
            assert c["op"] == plan["op"], c
            assert c["channel"] == c["where"], c          # brew's binary runs as brew, the app's as app


def test_no_sysaudio_or_settings_env_is_passed_to_the_recorder_install(world):
    brew_owned(world)
    world.as_app()
    adopt.execute(adopt.plan_adopt())
    inst = [c for c in world.calls() if c["cmd"].startswith("install")]
    assert inst and all("--sysaudio" not in c["args"] for c in inst)
    src = Path(adopt.__file__).read_text()
    assert "MEETING_CAPTURE_SYSAUDIO" not in src


def test_without_a_server_the_memory_is_backed_up_with_backup(world):
    brew_owned(world)
    world.appo["contorch-memory"].answer("index migrate", {"schema": "contorch-memory.backup/1", "ok": True,
                                                            "action": "index_migrate", "performed": False})
    world.as_app()
    plan = adopt.plan_adopt()
    assert adopt.execute(plan)["ok"]
    assert f"app:contorch-memory backup --to {plan['backup_dir']}/memory" in world.changes()


@pytest.mark.parametrize("recording,code", [(True, "recording_in_progress"), (None, "recording_unknown")])
def test_refuses_while_recording_or_when_it_cannot_tell(world, recording, code):
    brew_owned(world)
    world.brew["meeting-capture"].answer("status", {"schema": "meeting-capture.status/1", "ok": True,
                                                     "recording": recording, "reason": "stale_heartbeat"})
    world.as_app()
    plan = adopt.plan_adopt()
    assert plan["ok"] is False and plan["error"]["code"] == code
    assert world.changes() == [] and channel.read()["owner"] == "brew"        # nothing changed
    if recording is None:
        assert adopt.plan_adopt(not_recording=True)["ok"]


def test_meeting_capture_0_7_cannot_tell_and_says_how_to_proceed(world):
    brew_owned(world)
    world.brew["meeting-capture"].set(old=["status", "config"])
    world.as_app()
    plan = adopt.plan_adopt()
    assert plan["error"]["code"] == "recording_unknown" and "--not-recording" in plan["error"]["message"]


def test_a_memory_only_mac_is_never_recording(world):
    brew_owned(world)
    world.brew["meeting-capture"].answer("config", {"schema": "meeting-capture.config/1", "ok": True,
                                                     "agent": {"installed": False}, "settings": {}})
    world.brew["meeting-capture"].answer("status", {"schema": "meeting-capture.status/1", "ok": True,
                                                     "recording": None, "reason": "no_state"})
    world.as_app()
    assert adopt.plan_adopt()["ok"]


def test_chroma_downgrade_is_refused_before_anything_moves(world):
    brew_owned(world)
    world.appo["contorch-memory"].answer("status", {"schema": "contorch-memory.status/1", "ok": True,
                                                     "index_compatible": False, "index_written_by": "1.6.0",
                                                     "chromadb_version": "1.5.9"})
    world.as_app()
    res = adopt.execute(adopt.plan_adopt())
    assert res["ok"] is False and res["error"]["code"] == "chroma_downgrade"
    assert not any("migrate" in c or "install" in c for c in world.changes())
    assert channel.read()["state"] == "adopting"          # interrupted: re-run resumes


@pytest.mark.parametrize("code", ["backup_unverified", "migrate_unverified", "server_running"])
def test_owner_error_codes_are_mapped_and_stop_the_run(world, code):
    brew_owned(world)
    world.appo["contorch-memory"].answer("index migrate", {"schema": "contorch-memory.backup/1", "ok": False,
                                                            "error": {"code": code, "message": "nope"}}, rc=1)
    world.as_app()
    res = adopt.execute(adopt.plan_adopt())
    assert res["ok"] is False and res["error"]["code"] == adopt.ERROR_MAP[code]
    assert res["failed_step"]["kind"] == "memory_backup"
    assert not any(c.startswith("app:meeting-capture install") for c in world.changes())


def test_an_interrupted_adopt_resumes_with_the_same_op_and_backup_dir(world):
    brew_owned(world)
    world.appo["contorch-memory"].answer("claude install", cm_claude("install", ok=False,
                                                                     error={"code": "claude_failed",
                                                                            "message": "x"}), rc=1)
    world.as_app()
    first = adopt.plan_adopt()
    res = adopt.execute(first)
    assert res["ok"] is False and res["error"]["code"] == "claude_failed"
    m = channel.read()
    assert m["state"] == "adopting" and m["op"]["id"] == first["op"] and m["backup_dir"] == first["backup_dir"]
    # brew's own installers are now locked out (only op children may write)
    assert channel.check("brew")["ok"] is False
    world.defaults(world.appo["contorch-memory"])
    again = adopt.plan_adopt()
    assert again["resume"] and again["op"] == first["op"] and again["backup_dir"] == first["backup_dir"]
    # the backup already made counts (contorch-memory refuses a non-empty dir)
    world.appo["contorch-memory"].answer("index migrate", {"schema": "contorch-memory.backup/1", "ok": False,
                                                            "error": {"code": "dir_not_empty", "message": "x"}},
                                         rc=1)
    res = adopt.execute(again)
    assert res["ok"], res
    assert channel.read()["owner"] == "app" and channel.read()["state"] == "ok"


def test_adopt_by_the_owner_itself_is_a_no_op(world):
    brew_owned(world)
    plan = adopt.plan_adopt()
    assert plan["noop"] and adopt.execute(plan)["ok"] and world.changes() == []


def test_without_a_marker_an_existing_brew_install_is_adopted(world):
    world.as_app()
    plan = adopt.plan_adopt()
    assert plan["from"] == "brew"
    assert adopt.execute(plan)["ok"] and channel.read()["owner"] == "app"


def test_dont_record_on_this_mac_adopts_without_the_recorder(world):
    world.as_app()
    modules._write({"memory": True, "recorder": False, "linein": False, "cli": False})
    world.brew["meeting-capture"].answer("config", {"schema": "meeting-capture.config/1", "ok": True,
                                                     "agent": {"installed": False}, "settings": {}})
    res = adopt.execute(adopt.plan_adopt())
    assert res["ok"]
    assert not any("install --adopt" in c or "skill install" in c for c in world.changes())
    assert "grant_permissions" not in {t["code"] for t in res["todo"]}


def test_restart_claude_code_lists_the_running_mcp_servers(world):
    brew_owned(world)
    world.ps = "  411 /bin/bash /opt/homebrew/bin/contorch-mcp\n  412 /usr/bin/vim notes\n"
    world.as_app()
    res = adopt.execute(adopt.plan_adopt())
    assert {"code": "restart_claude_code", "pids": [411],
            "message": "1 Claude Code session(s) still run the old MCP server; quit and reopen Claude Code."} \
        in res["todo"]


def test_pm_needs_no_chromadb():
    """Scenario D's unit half: adopt/rollback/uninstall import no chromadb
    (pm's venv has none in any channel; CO's own interpreter does the data)."""
    code = ("import sys; import pipeline_monitor.adopt, pipeline_monitor.uninstall, pipeline_monitor.contorch;"
            "print('chromadb' in sys.modules)")
    r = subprocess.run([PYTHON, "-c", code], capture_output=True, text=True)
    assert r.stdout.strip() == "False", r.stderr


# ------------------------------------------------------------ rollback app → brew

def app_adopted(world):
    brew_owned(world)
    world.as_app()
    assert adopt.execute(adopt.plan_adopt())["ok"]
    world.log.unlink()
    world.system.clear()


def test_rollback_upgrades_brew_first_then_hands_off(world):
    app_adopted(world)
    m = channel.read()
    plan = adopt.plan_rollback()
    assert plan["ok"], plan
    world.brew["contorch"].answer("adopt", {"schema": "contorch.adopt/1", "event": "result", "ok": True,
                                            "todo": []})
    res = adopt.execute(plan)
    assert res["ok"], res
    brew_calls = [s for s in world.system if s[0] == "brew"]
    assert brew_calls[:3] == [["brew", "unpin", *adopt.FORMULAS], ["brew", "upgrade", *adopt.FORMULAS],
                              ["brew", "list", "--versions", "contorch"]]
    assert ["brew", "link", "--overwrite", *adopt.FORMULAS] in brew_calls
    assert world.changes() == [
        "app:meeting-capture uninstall --adopt",
        "app:contorch-memory claude uninstall --channel app",
        "app:meeting-capture skill uninstall",
        "brew:contorch adopt --yes",
    ]
    handoff = [c for c in world.calls() if c["cmd"] == "adopt --yes"][0]
    assert handoff["channel"] == "brew" and handoff["op"] == plan["op"]
    assert not any(c["cmd"].startswith("restore") for c in world.calls())     # index compatible: no restore
    # the marker says adopting → brew until brew's adopt writes it (that fake doesn't)
    assert channel.read()["adopting_to"] == "brew" and channel.read()["writers"] == ["brew", "app"]
    assert m["adopted_from"]["backup_dir"] != plan["backup_dir"]


def test_rollback_restores_the_index_only_when_brew_cannot_open_it(world):
    app_adopted(world)
    adopt_backup = channel.read()["adopted_from"]["backup_dir"]
    world.brew["contorch-memory"].answer("status", {"schema": "contorch-memory.status/1", "ok": True,
                                                     "index_compatible": False})
    world.brew["contorch"].answer("adopt", {"ok": True, "event": "result"})
    assert adopt.execute(adopt.plan_rollback())["ok"]
    assert f"brew:contorch-memory restore --from {adopt_backup}/memory --stop-server" in world.changes()


def test_rollback_refuses_a_tap_older_than_this_suite(world):
    app_adopted(world)
    world.brew_version = "0.3.0"
    res = adopt.execute(adopt.plan_rollback())
    assert res["ok"] is False and res["error"]["code"] == "schema_newer"
    assert not any(c.startswith("app:meeting-capture uninstall") for c in world.changes())


def test_rollback_needs_an_adopted_app(world):
    brew_owned(world)
    assert adopt.plan_rollback()["error"]["code"] == "nothing_to_roll_back"
    world.as_app()
    assert adopt.plan_rollback()["error"]["code"] == "nothing_to_roll_back"


def test_brew_adopting_back_resumes_the_rollback_op(world):
    """The handoff: brew's `contorch adopt --yes` sees `adopting → brew` and
    resumes with the app's op token, stopping the app's (already gone)
    recorder and starting brew's again."""
    app_adopted(world)
    plan = adopt.plan_rollback()
    for st in plan["steps"]:
        if st["kind"] != "handoff":
            adopt._do(st, adopt.Run(plan))
    world.log.unlink()
    world.as_brew()
    world.mp.setenv("CONTORCH_OP", plan["op"])
    back = adopt.plan_adopt()
    assert back["resume"] and back["from"] == "app" and back["op"] == plan["op"]
    assert adopt.execute(back)["ok"]
    assert "brew:meeting-capture install --adopt --backup-dir " + back["backup_dir"] in world.changes()
    assert ["brew", "services", "start", "contorch/tap/contorch"] in world.system
    m = channel.read()
    assert m["owner"] == "brew" and m["writers"] == ["brew"] and m["adopted_from"]["channel"] == "app"


# ------------------------------------------------------------ the CLI

def _cli(*args):
    r = subprocess.run([PYTHON, "-c", "import sys; from pipeline_monitor import contorch; "
                                              "sys.exit(contorch.main(sys.argv[1:]))", *args],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    return r


def test_cli_without_a_terminal_or_yes_changes_nothing(world, monkeypatch):
    brew_owned(world)
    world.as_app()
    r = _cli("adopt", "--json")
    doc = json.loads(r.stdout.strip().splitlines()[-1])
    assert r.returncode == 1 and doc["error"]["code"] == "needs_yes"
    assert channel.read()["owner"] == "brew" and world.changes() == []


def test_cli_plan_json(world, capfd):
    brew_owned(world)
    world.as_app()
    assert contorch.main(["adopt", "--plan", "--json"]) == 0
    doc = json.loads(capfd.readouterr().out)
    assert doc["schema"] == "contorch.adopt.plan/1" and doc["ok"] and doc["steps"][0]["kind"] == "mark"
    assert world.changes() == []


def test_cli_yes_json_streams_progress_then_a_result(world, capfd):
    brew_owned(world)
    world.as_app()
    assert contorch.main(["adopt", "--yes", "--json"]) == 0
    lines = [json.loads(l) for l in capfd.readouterr().out.splitlines()]
    assert all(l["schema"] == "contorch.adopt/1" for l in lines)
    assert lines[0]["event"] == "progress" and lines[0]["step"] == 1
    assert lines[-1]["event"] == "result" and lines[-1]["ok"] is True
