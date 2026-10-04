"""`contorch setup` as an ordering of owners' verbs, with the modules
question first: memory-only, macOS 15 with no key, an existing brew install
(no question), the channel guard, resume after an interruption. Fake owners
only; no launchctl, brew or claude runs."""
from __future__ import annotations

import json
import subprocess

import pytest

from conftest import FakeOwner, cm_claude, mc_agent, mc_json, on_brew, write_plist, UNAVAILABLE
from pipeline_monitor import channel, contorch as ct, mcconfig, modules
from pipeline_monitor import transcription as stt

PERMS_OK = {"schema": "meeting-capture.permissions/1", "ok": True, "channel": "brew",
            "permissions": [{"id": "screen_audio", "status": "granted", "required": True},
                            {"id": "microphone", "status": "granted", "required": True}]}


class Stack:
    def __init__(self, tmp_path, monkeypatch, fake_mc, ran):
        self.tmp, self.mc, self.ran = tmp_path, fake_mc, ran
        self.cm = FakeOwner(tmp_path / "co", "contorch-memory")
        self.cm.answer("index migrate", {"schema": "contorch-memory.backup/1", "ok": True, "action": "index_migrate",
                                         "performed": False})
        self.cm.answer("claude install", cm_claude("install"))
        self.cm.answer("claude status", cm_claude("status", mcp={"present": False, "matches": False}))
        self.cm.answer("selftest", {"schema": "contorch-memory.selftest/1", "ok": True, "stage": "done", "ms": 9})
        on_brew(tmp_path, "contorch-memory", self.cm.path)
        on_brew(tmp_path, "contorch-mcp", FakeOwner(tmp_path / "co", "contorch-mcp").path)
        self.lines = {
            "install --json": [json.dumps(mc_agent("install"))],
            "uninstall --json": [json.dumps(mc_agent("uninstall"))],
            "restart --json": [json.dumps(mc_agent("restart"))],
            "check --json": [json.dumps(PERMS_OK)],
            "skill install --json": [json.dumps({"schema": "meeting-capture.skill/1", "ok": True,
                                                 "action": "linked"})],
        }
        fake_mc.set(lines=self.lines)
        monkeypatch.setattr(ct.shutil, "which", lambda n: "/x/claude" if n == "claude" else None)
        self.launchctl: list[tuple] = []
        monkeypatch.setattr(ct, "_launchctl", lambda *a: self.launchctl.append(a) or
                            subprocess.CompletedProcess(a, 0, "", ""))
        monkeypatch.setattr(ct, "STATE_DIR", tmp_path / "home" / ".contorch")
        monkeypatch.setattr(ct, "STOPPED_MARKER", tmp_path / "home" / ".contorch" / "stopped.json")
        monkeypatch.setattr(ct, "LAUNCH_AGENTS", tmp_path / "LaunchAgents")

    def mc_after(self, key, doc):
        self.mc.set(after={key: doc})

    def cm_changes(self):
        return [" ".join(a for a in c["args"] if a != "--json") for c in self.cm.calls()
                if c["args"][:2] not in (["claude", "status"], ["status", "--json"])]

    def __contains__(self, argv):       # setup's own commands (ct._run): open, brew, embeddings
        return argv in self.ran


@pytest.fixture
def stack(tmp_path, monkeypatch, fake_mc):
    ran: list[list[str]] = []

    def fake_run(cmd, timeout=300):
        ran.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(ct, "_run", fake_run)
    monkeypatch.setattr(ct, "KEY_FILE", tmp_path / "google" / "key")
    monkeypatch.setattr(ct, "_interactive", lambda: True)
    return Stack(tmp_path, monkeypatch, fake_mc, ran)


class Term:
    def __init__(self, monkeypatch, inputs=(), keys=()):
        self.inputs, self.keys, self.prompts = list(inputs), list(keys), []
        monkeypatch.setattr("builtins.input", self._input)
        monkeypatch.setattr(ct.getpass, "getpass", lambda p="": self.keys.pop(0) if self.keys else "")

    def _input(self, prompt=""):
        self.prompts.append(prompt)
        return self.inputs.pop(0) if self.inputs else ""


def _setup(**kw):
    out: list[str] = []
    ok = ct.setup(log=out.append, **kw)
    return ok, "\n".join(out)


# ------------------------------------------------------------ the owners' verbs, in order

def test_setup_orders_owner_verbs_and_claims_the_marker(stack, monkeypatch, fake_mc):
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    Term(monkeypatch)
    ok, out = _setup()
    assert ok, out
    cm = stack.cm_changes()
    assert cm[0].startswith(f"index migrate --in-process --backup-dir {stack.tmp}/home/.contorch/backups/setup-")
    assert cm[1:] == ["selftest", "claude install --channel brew"]
    assert fake_mc.changes() == [["skill", "install", "--json"], ["install", "--json"], ["restart", "--json"]]
    assert ["check", "--json"] in fake_mc.calls()
    m = channel.read()
    assert m["owner"] == "brew" and m["writers"] == ["brew"]
    assert modules.wanted() == {"memory": True, "recorder": True, "linein": False, "cli": True}
    assert not any(a[0] == "kickstart" for a in stack.launchctl)          # restart is meeting-capture's


def test_the_chroma_server_is_retired_through_context_orchestrator(stack, monkeypatch):
    stack.cm.answer("index migrate", {"schema": "contorch-memory.backup/1", "ok": True, "action": "index_migrate",
                                      "performed": True, "todo": ["Run `contorch-memory claude install`"]})
    Term(monkeypatch)
    ok, out = _setup()
    assert ok and "backed up your memory to" in out and "retired the chroma server" in out
    migrate = stack.cm_changes()[0]
    assert migrate.startswith("index migrate --in-process --backup-dir ") and "/.contorch/backups/setup-" in migrate


def test_a_refused_migration_changes_nothing_else(stack, monkeypatch, fake_mc):
    stack.cm.answer("index migrate", {"schema": "contorch-memory.backup/1", "ok": False,
                                      "error": {"code": "backup_unverified", "message": "copy differs"}}, rc=1)
    Term(monkeypatch)
    ok, out = _setup()
    assert ok is False and "backup_unverified" in out
    assert ["install", "--json"] not in fake_mc.calls() and channel.read() is None


def test_rerun_after_an_interrupted_run_resumes(stack, monkeypatch, fake_mc):
    stack.cm.answer("index migrate", {"schema": "contorch-memory.backup/1", "ok": False,
                                      "error": {"code": "server_running", "message": "busy"}}, rc=1)
    Term(monkeypatch)
    assert _setup()[0] is False
    stack.cm.answer("index migrate", {"schema": "contorch-memory.backup/1", "ok": True, "performed": False})
    Term(monkeypatch)
    ok, out = _setup()
    assert ok, out
    assert channel.read()["owner"] == "dev"


def test_refused_on_channel_conflict(stack, monkeypatch, tmp_path, fake_mc):
    app = tmp_path / "Contorch.app"
    app.mkdir()
    m = channel.build("app")
    m["layout"] = {"bundle_root": str(app)}
    channel.write(m)
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    ok, out = _setup()
    assert ok is False and "managed by Contorch.app" in out
    assert stack.cm.calls() == [] and fake_mc.calls() == []


# ------------------------------------------------------------ memory only

def test_memory_only_mac_runs_no_recorder_step(stack, monkeypatch, fake_mc):
    term = Term(monkeypatch, inputs=["n", "1"])          # record? no · embeddings: local
    ok, out = _setup()
    assert ok, out
    assert "Record meetings on this Mac?" in term.prompts[0]
    assert "this Mac doesn't record (memory only)" in out
    assert not any(c[:1] in (["install"], ["skill"], ["check"], ["restart"]) for c in fake_mc.calls())
    assert ["/x/contorch-memory", "embeddings", "local"] not in stack.ran      # (the real path, below)
    assert any(c[-2:] == ["embeddings", "local"] for c in stack.ran)
    w = json.loads(modules.state_file().read_text())
    assert w["wanted"]["recorder"] is False and w["embeddings_source"] == "local"
    assert "Import transcripts" in out


def test_without_meeting_capture_installed_it_is_memory_only_without_asking(stack, monkeypatch, fake_mc, tmp_path):
    fake_mc.path.unlink()                                 # brew install contorch --without-meeting-capture
    term = Term(monkeypatch, inputs=["3"])                # embeddings: imported
    ok, out = _setup()
    assert ok, out
    assert not any("Record meetings" in p for p in term.prompts)
    assert modules.embeddings_source() == "imported"
    assert any(c[-2:] == ["embeddings", "gemini"] for c in stack.ran)
    assert "keyword" in out                               # no key here: keyword search


def test_a_blocked_model_download_is_a_todo_not_a_failure(stack, monkeypatch):
    stack.cm.answer("selftest", {"schema": "contorch-memory.selftest/1", "ok": False, "stage": "embed",
                                 "error": {"code": "proxy", "message": "407"}}, rc=1)
    Term(monkeypatch, inputs=["n", "1"])
    ok, out = _setup()
    assert ok is False                                    # a to-do is left …
    assert "Search will use keywords until the model downloads" in out
    assert channel.read()["owner"] == "dev"               # … but setup finished


def test_saying_no_on_a_recording_mac_turns_the_recorder_off(stack, monkeypatch, fake_mc):
    write_plist(mcconfig.LEGACY_PLIST, {})                # meeting-capture 0.7-style install
    fake_mc.set(rc={"config --json": 2})
    Term(monkeypatch, inputs=["n", ""])
    ok, out = _setup()
    assert ["uninstall", "--json"] in fake_mc.calls() and "the recorder is off on this Mac" in out


# ------------------------------------------------------------ no engine (macOS 15, no key)

def test_macos_15_without_a_key_keeps_the_recorder_off(stack, monkeypatch, fake_mc):
    fake_mc.set(json=mc_json(**UNAVAILABLE))
    Term(monkeypatch, keys=[""])
    ok, out = _setup(record="yes")
    assert ok is False
    assert "the recorder stays off" in out and modules.NO_ENGINE_TEXT in out
    assert ["install", "--json"] not in fake_mc.calls()
    assert modules.wanted()["recorder"] is False


def test_macos_15_with_a_rejected_key_keeps_the_recorder_off(stack, monkeypatch, fake_mc):
    bad = dict(mc_json(**UNAVAILABLE), gemini_key=True)
    fake_mc.set(json=bad, lines={**stack.lines, "stt --json --check-key": [
        json.dumps(dict(bad, key_check={"key": "rejected", "message": "API key not valid"}))]})
    Term(monkeypatch, keys=["AIza-bad"])
    ok, out = _setup(record="yes")
    assert ok is False and "Google rejected the Gemini key" in out
    assert ["install", "--json"] not in fake_mc.calls()


def test_an_existing_recorder_keeps_recording_when_its_key_goes_bad(stack, monkeypatch, fake_mc):
    write_plist(mcconfig.LEGACY_PLIST, {})
    fake_mc.set(json=dict(mc_json(**UNAVAILABLE), gemini_key=True), rc={"config --json": 2})
    monkeypatch.setattr(ct, "_interactive", lambda: False)
    ok, out = _setup()
    assert ["install", "--json"] in fake_mc.calls()       # today's parking behaviour, not switched off


# ------------------------------------------------------------ an existing brew install: no new question

def test_existing_brew_install_is_kept_without_a_question(stack, monkeypatch, fake_mc):
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    write_plist(mcconfig.LEGACY_PLIST, {})
    fake_mc.set(rc={"config --json": 2})
    stack.cm.answer("claude status", cm_claude("status"))
    monkeypatch.setattr(ct, "_interactive", lambda: False)
    ok, out = _setup()
    assert ok, out
    assert modules.wanted() == {"memory": True, "recorder": True, "linein": False, "cli": True}
    assert ["install", "--json"] in fake_mc.calls()


def test_meeting_capture_0_7_falls_back_to_its_plain_commands(stack, monkeypatch, fake_mc):
    fake_mc.set(rc={"install --json": 2, "restart --json": 2, "check --json": 2, "skill install --json": 2,
                    "config --json": 2})
    Term(monkeypatch)
    ok, out = _setup()
    assert [str(fake_mc.path), "install"] in stack.ran                     # 0.7's own plain verb
    assert "System Settings → Privacy & Security → Screen & System Audio Recording" in out
    assert ("kickstart", "-k", f"gui/{ct._uid()}/{ct.MC_LABEL}") in stack.launchctl


def test_old_context_orchestrator_is_a_todo(stack, monkeypatch):
    stack.cm.set(old=["index migrate", "claude install", "selftest"])
    Term(monkeypatch)
    ok, out = _setup()
    assert ok is False
    assert "Upgrade context-orchestrator (≥ 0.5)" in out


def test_claude_code_is_context_orchestrators_to_connect(stack, monkeypatch):
    stack.cm.answer("claude install", cm_claude("install", todo=["Restart Claude Code"],
                                               blocked_by_managed_settings=True,
                                               managed_reasons=["allowManagedHooksOnly"]))
    Term(monkeypatch)
    ok, out = _setup()
    assert "managed settings block part of this: allowManagedHooksOnly" in out
    assert "✗ Restart Claude Code" in out
