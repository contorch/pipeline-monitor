"""Adopt and rollback: one install channel takes Contorch over from another.

    contorch adopt    [--plan] [--yes] [--json] [--from brew|dev|app] [--not-recording]
    contorch rollback [--plan] [--yes] [--json]          (app → the channel it adopted from)

Both are ORDERINGS OF OWNER VERBS (SPEC-v2 §4.3). pipeline-monitor decides
the order, the refusals and the marker; every change to data, the recorder or
Claude Code is the owner's own command:

    adopt (run by the target channel's contorch; backups first)
     1  refuse while a meeting is being recorded, or when that can't be told
        (`meeting-capture status --json`: recording true / null)
     2  mark ~/.contorch/channel.json `adopting` (writers = [target, old] +
        an op token that only this operation's children carry)
     3  stop the old writers: the old channel's `meeting-capture stop
        --reason update --json`; `brew services stop contorch`
     4  the target's `contorch-memory status --json`: refuse when the index
        isn't compatible (chroma_downgrade)
     5  the target's `contorch-memory index migrate --in-process --backup-dir D`
        (backs up context.db + the chroma folder, retires the chroma server);
        with no server, `contorch-memory backup --to D` instead — either way a
        verified backup exists before anything moves
     6  the target's `meeting-capture install --adopt [--no-load] --backup-dir D`
        (settings migrate, the legacy plist moves aside; no sysaudio env from
        pm: meeting-capture resolves its own helper). --no-load into the app,
        whose permissions setup asks first; brew/dev start it again
     7  the target's `contorch-memory claude install --channel <target>` and
        `meeting-capture skill install`
     8  CLI links (`contorch cli install`) when the cli module is wanted (app)
     9  `brew unlink` + `brew pin` the formulas (target ≠ brew); `brew
        services start contorch` (target = brew)
    10  write the marker: owner = target

    rollback (app → brew, run by the app's contorch)
     1  refuse unless `recording: false`; 2 mark
     3  `brew unpin` + `brew upgrade` the three formulas to (at least) this
        suite, then `brew link`; a tap without this suite → schema_newer
     4  the app's `meeting-capture uninstall --adopt`, `contorch-memory claude
        uninstall`, `meeting-capture skill uninstall`; the CLI links
     5  brew's `contorch-memory status --json`; only when it can't open the
        index, brew's `contorch-memory restore --from <adopt backup>`
     6  hand off to brew's own `contorch adopt --yes --json` (same op token)

Re-running an interrupted adopt resumes it (the marker keeps the op and the
backup dir; every owner verb is idempotent). pm's own venv needs no chromadb.

Every system command other than an owner's goes through run_system() (brew,
ps), so tests replace it; launchctl is never called from here.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import channel, modules, owners

FORMULAS = ("contorch", "meeting-capture", "context-orchestrator")


def run_system(argv: list[str], timeout: float = 900) -> subprocess.CompletedProcess:
    """brew / ps. Tests replace this."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1"})


class OpError(RuntimeError):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail

    def as_json(self) -> dict:
        return {"code": self.code, "message": self.message, **({"detail": self.detail} if self.detail else {})}


def _step(kind: str, why: str, **args) -> dict:
    return {"kind": kind, "why": why, "args": args}


def _owner(name: str, argv: list[str], ch: str, why: str, *, schema: str, on_old: str = "fail",
           on_missing: str = "fail", **extra) -> dict:
    return _step("owner", why, name=name, argv=argv, channel=ch, schema=schema, on_old=on_old,
                 on_missing=on_missing, **extra)


def suite_version() -> str:
    from . import __version__
    return __version__


# ------------------------------------------------------------------ facts

def brew_bin(prefix: str | None = None) -> str | None:
    for p in ([prefix] if prefix else list(owners.BREW_PREFIXES)):
        b = Path(p) / "bin" / "brew"
        if b.exists():
            return str(b)
    return None


def brew_formulas(prefix: str | None = None) -> list[str]:
    """The Contorch formulas Homebrew has installed (a Cellar entry)."""
    out = []
    for p in ([prefix] if prefix else list(owners.BREW_PREFIXES)):
        out += [f for f in FORMULAS if (Path(p) / "Cellar" / f).is_dir() and f not in out]
    return out


def default_from(me: str, marker: dict | None) -> str | None:
    """Which channel to adopt from: the marker's owner; with no marker (an
    install made before markers existed) Homebrew when its formulas are
    there, else a source install."""
    if marker and marker.get("owner") in owners.CHANNELS:
        return marker["owner"]
    if me != "brew" and (Path(owners.brew_prefix()) / "opt" / "contorch").exists():
        return "brew"
    return "dev" if me != "dev" else None


def recording(ch: str, layout: dict | None, not_recording: bool) -> None:
    """Refuse while a meeting is (or might be) being recorded. A Mac with no
    recorder agent can't be recording."""
    exe = owners.locate_in(ch, "meeting-capture", layout) or owners.locate("meeting-capture")
    if exe is None:
        return
    cfg = owners.call("meeting-capture", "config", "--json", schema="meeting-capture.config/", exe=exe,
                      timeout=30)
    if cfg["status"] == "ok" and not (cfg["data"].get("agent") or {}).get("installed"):
        return
    res = owners.call("meeting-capture", "status", "--json", schema="meeting-capture.status/", exe=exe,
                      timeout=30)
    rec = res["data"].get("recording") if res["status"] == "ok" else None
    if rec is True:
        raise OpError("recording_in_progress", "A meeting is being recorded. Run this again after it ends.")
    if rec is None and not not_recording:
        why = (res["data"] or {}).get("reason") if res["status"] == "ok" else res.get("error")
        raise OpError("recording_unknown",
                      f"Can't tell whether a meeting is being recorded ({why}). Upgrade meeting-capture "
                      "(≥ 0.8 answers `meeting-capture status --json`), or, if you are sure nothing is "
                      "being recorded, run this again with --not-recording.")


def mcp_processes(exclude: str | None = None) -> list[int]:
    """PIDs of running contorch-mcp servers not started from `exclude` (for
    the restart_claude_code todo; display only)."""
    try:
        res = run_system(["ps", "-axo", "pid=,command="], timeout=10)
    except Exception:
        return []
    out = []
    for line in res.stdout.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit() and ("contorch-mcp" in cmd or "context_orchestrator.server" in cmd) \
                and not (exclude and exclude in cmd):
            out.append(int(pid))
    return out


# ------------------------------------------------------------------ plans

def plan_adopt(me: str | None = None, frm: str | None = None, not_recording: bool = False) -> dict:
    """Steps that move Contorch to this install's channel (me)."""
    me = me or owners.channel()
    m = channel.read()
    if m and m.get("unreadable"):
        return _refuse("marker_unreadable", f"{channel.marker_path()} can't be read; fix or delete it.")
    resuming = bool(m and m.get("state") == "adopting" and m.get("adopting_to") == me)
    if m and m.get("state") not in (None, "ok") and not resuming:
        return _refuse("interrupted", m.get("blocked_message") or "an operation is in progress")
    old = (m or {}).get("owner") if resuming else (frm or default_from(me, m))
    if old == me and not resuming:
        return {"ok": True, "noop": True, "from": me, "to": me, "steps": [], "todo": [],
                "message": f"this {me} install already owns Contorch here"}
    if old not in owners.CHANNELS:
        return _refuse("nothing_to_adopt", "No other Contorch install was found to adopt from.")
    old_layout = (m or {}).get("layout") or {"brew_prefix": owners.brew_prefix()}
    try:
        recording(old, old_layout, not_recording)
    except OpError as e:
        return {"ok": False, "error": e.as_json()}
    op_id = (m or {}).get("op", {}).get("id") if resuming else channel.new_op_id()
    bdir = (m or {}).get("backup_dir") if resuming else str(
        Path.home() / ".contorch" / "backups" / time.strftime("adopt-%Y%m%d-%H%M%S"))
    want = modules.wanted() or modules.infer_wanted(modules.observe(me)) or {}
    recorder = want.get("recorder", True) and owners.locate("meeting-capture") is not None
    steps: list[dict] = [
        _step("mark", "an interrupted adopt is detected and resumed, never half-applied",
              owner=old, adopting_to=me, op_id=op_id, backup_dir=bdir, layout=old_layout),
    ]
    if owners.locate_in(old, "meeting-capture", old_layout):
        steps.append(_owner("meeting-capture", ["stop", "--reason", "update", "--json"], old,
                            f"stop the {old} recorder before anything moves", schema="meeting-capture.agent/",
                            on_old="skip", on_missing="skip", layout=old_layout))
    if old == "brew" and me != "brew" and "contorch" in brew_formulas(old_layout.get("brew_prefix")):
        steps.append(_step("brew", "the Homebrew menu bar would run beside this one",
                           argv=["services", "stop", "contorch/tap/contorch"], prefix=old_layout.get("brew_prefix")))
    steps += [
        _step("index_compatible", "never open an index written by a newer chromadb"),
        _step("memory_backup", "verified backup of context.db and the vector index; the chroma server is "
                               "retired (in-process from now on)", dir=str(Path(bdir) / "memory")),
    ]
    if recorder:
        # Into the app: registered but not started (its permissions are asked
        # first, by setup). Back to brew/dev: their sysaudio keeps its grants.
        load = [] if me != "app" else ["--no-load"]
        steps.append(_owner("meeting-capture", ["install", "--adopt", *load, "--backup-dir", bdir, "--json"],
                            me, "the recorder agent becomes this install's (settings kept"
                                + ("; not started yet)" if load else ")"),
                            schema="meeting-capture.agent/"))
    steps.append(_owner("contorch-memory", ["claude", "install", "--channel", me, "--backup-dir", bdir, "--json"],
                        me, "Claude Code's MCP server, hook, CLAUDE.md block and transcripts skill",
                        schema="contorch-memory.claude/"))
    if recorder:
        steps.append(_owner("meeting-capture", ["skill", "install", "--json"], me, "the /meeting skill",
                            schema="meeting-capture.skill/", on_old="skip"))
    if me == "app" and want.get("cli"):
        steps.append(_step("cli_links", "the commands on your PATH point into the app", action="install"))
    pinned: list[str] = []
    if old == "brew" and me != "brew":
        pinned = brew_formulas(old_layout.get("brew_prefix"))
        if pinned:
            steps.append(_step("brew", "off PATH, so `contorch` & co. resolve to this install (opt/ paths "
                                       "keep working for a rollback)", argv=["unlink", *pinned],
                               prefix=old_layout.get("brew_prefix")))
            steps.append(_step("brew", "`brew upgrade` would relink an unpinned formula",
                               argv=["pin", *pinned], prefix=old_layout.get("brew_prefix")))
    if me == "brew" and "contorch" in brew_formulas():
        steps.append(_step("brew", "the menu bar, at login", argv=["services", "start", "contorch/tap/contorch"]))
    steps.append(_step("write_marker", "the new owner", owner=me,
                       adopted_from={"channel": old, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                     "backup_dir": bdir, "pinned": pinned, "layout": old_layout}))
    todo = []
    if recorder and me == "app":
        todo.append({"code": "grant_permissions",
                     "message": "Start the recorder and allow its permissions: contorch setup "
                                "(or `meeting-capture start`)"})
    if pinned:
        todo.append({"code": "brew_cleanup_later",
                     "message": "When you're happy: brew unpin " + " ".join(pinned)
                                + " && brew uninstall " + " ".join(pinned)})
    return {"ok": True, "from": old, "to": me, "resume": resuming, "op": op_id, "backup_dir": bdir,
            "steps": steps, "todo": todo}


def plan_rollback(me: str | None = None, not_recording: bool = False) -> dict:
    """App → the channel it adopted from (brew)."""
    me = me or owners.channel()
    m = channel.read() or {}
    src = (m.get("adopted_from") or {}).get("channel")
    if me != "app" or m.get("owner") != "app" or src != "brew":
        return _refuse("nothing_to_roll_back",
                       "Only Contorch.app can go back to the Homebrew install it adopted "
                       f"(owner here: {m.get('owner') or 'nobody'}, adopted from: {src or 'nothing'}).")
    try:
        recording("app", m.get("layout"), not_recording)
    except OpError as e:
        return {"ok": False, "error": e.as_json()}
    af = m["adopted_from"]
    prefix = (af.get("layout") or {}).get("brew_prefix") or owners.brew_prefix()
    op_id = channel.new_op_id()
    pinned = af.get("pinned") or list(FORMULAS)
    bdir = str(Path.home() / ".contorch" / "backups" / time.strftime("rollback-%Y%m%d-%H%M%S"))
    steps = [
        _step("mark", "an interrupted rollback is detected", owner="app", adopting_to="brew", op_id=op_id,
              backup_dir=bdir, layout=m.get("layout")),
        _step("brew", "back on PATH and upgradable", argv=["unpin", *pinned], prefix=prefix),
        _step("brew", "Homebrew's code at (at least) this suite's version, so it reads the same data",
              argv=["upgrade", *FORMULAS], prefix=prefix, ok_rc=(0, 1)),
        _step("brew_suite", "a tap without this suite would run older code on newer data", prefix=prefix),
        _step("brew", "", argv=["link", "--overwrite", *pinned], prefix=prefix),
        _owner("meeting-capture", ["uninstall", "--adopt", "--json"], "app",
               "the app's recorder agent is unregistered (only the app can)", schema="meeting-capture.agent/",
               on_missing="skip"),
        _owner("contorch-memory", ["claude", "uninstall", "--channel", "app", "--json"], "app",
               "the app's Claude Code entries", schema="contorch-memory.claude/"),
        _owner("meeting-capture", ["skill", "uninstall", "--json"], "app", "the app's /meeting skill link",
               schema="meeting-capture.skill/", on_missing="skip"),
        _step("cli_links", "the app's command links", action="uninstall"),
        _step("restore_if_needed", "brew's chromadb can open the index, or the adopt backup is put back",
              backup=str(Path(af.get("backup_dir") or "") / "memory"), prefix=prefix),
        _step("handoff", "brew's own adopt takes everything back (same code, other direction)", prefix=prefix),
    ]
    return {"ok": True, "from": "app", "to": "brew", "op": op_id, "backup_dir": bdir,
            "steps": steps,
            "todo": [{"code": "login_item", "message": "Turn off Contorch in System Settings › General › "
                                                       "Login Items, then move Contorch.app to the Trash."}]}


def _refuse(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


# ------------------------------------------------------------------ execute

ERROR_MAP = {"server_running": "server_running", "backup_unverified": "backup_unverified",
             "migrate_unverified": "backup_unverified", "chroma_downgrade": "chroma_downgrade",
             "settings_invalid": "settings_invalid", "channel_conflict": "channel_conflict",
             "agent_elsewhere": "agent_elsewhere"}


class Run:
    """One execution: the op token, the marker as it stands, collected todo."""

    def __init__(self, plan: dict):
        self.plan = plan
        self.op_id = plan.get("op")
        self.todo: list[dict] = list(plan.get("todo") or [])
        self.layout = None

    def env(self, ch: str) -> dict:
        e = {"CONTORCH_CHANNEL": ch}
        if self.op_id:
            e["CONTORCH_OP"] = self.op_id
        return e


def execute(plan: dict, log: Callable[[dict], None] = lambda e: None) -> dict:
    """Run a plan's steps in order; stop at the first failure and say which.
    Re-running the same command resumes (owner verbs are idempotent)."""
    if not plan.get("ok"):
        return plan
    if plan.get("noop"):
        return {"ok": True, "done": [], "todo": [], "message": plan.get("message")}
    run = Run(plan)
    steps = plan["steps"]
    done = []
    for i, st in enumerate(steps):
        log({"event": "progress", "step": i + 1, "of": len(steps), "kind": st["kind"], "why": st["why"]})
        try:
            res = _do(st, run)
        except OpError as e:
            return {"ok": False, "error": e.as_json(), "failed_step": st, "done": done, "todo": run.todo}
        except Exception as e:   # noqa: BLE001 — report, never leave the caller guessing
            return {"ok": False, "error": {"code": "internal", "message": f"{type(e).__name__}: {e}"},
                    "failed_step": st, "done": done, "todo": run.todo}
        done.append({"kind": st["kind"], **({"result": res} if res is not None else {})})
    if plan.get("to"):
        # Every MCP server running now was started from the old registration.
        pids = mcp_processes()
        if pids:
            run.todo.append({"code": "restart_claude_code", "pids": pids,
                             "message": f"{len(pids)} Claude Code session(s) still run the old MCP server; "
                                        "quit and reopen Claude Code."})
    return {"ok": True, "done": done, "todo": run.todo}


def _do(st: dict, run: Run) -> Any:
    k, a = st["kind"], st["args"]
    if k == "mark":
        cur = channel.read() or {}
        if cur.get("state") == "adopting" and cur.get("op", {}).get("id") == a["op_id"]:
            return "resumed"
        channel.write(channel.build(a["owner"], "adopting", adopting_to=a["adopting_to"], op_id=a["op_id"],
                                    extra={"backup_dir": a.get("backup_dir"), "layout": a.get("layout")}))
        return None
    if k == "write_marker":
        cur = channel.read() or {}
        extra = {"adopted_from": a["adopted_from"]} if a.get("adopted_from") else {}
        if not extra and cur.get("adopted_from"):
            extra["adopted_from"] = cur["adopted_from"]
        channel.write(channel.build(a["owner"], "ok", extra=extra))
        return None
    if k == "owner":
        return _owner_step(a, run)
    if k == "brew":
        brew = brew_bin(a.get("prefix"))
        if brew is None:
            raise OpError("brew_failed", "Homebrew's brew wasn't found")
        r = run_system([brew, *a["argv"]])
        if r.returncode not in a.get("ok_rc", (0,)):
            raise OpError("brew_failed", f"brew {' '.join(a['argv'])}: {(r.stderr or r.stdout).strip()[-300:]}",
                          command=f"brew {' '.join(a['argv'])}")
        return None
    if k == "index_compatible":
        res = owners.call("contorch-memory", "status", "--json", schema="contorch-memory.status/", timeout=60)
        if res["status"] == "old":
            raise OpError("owner_too_old", "this context-orchestrator has no `contorch-memory status --json` "
                                           "(needs 0.5)")
        if res["status"] != "ok":
            raise OpError("owner_failed", res["error"])
        if res["data"].get("index_compatible") is False:
            d = res["data"]
            raise OpError("chroma_downgrade", f"the search index was written by chromadb "
                                              f"{d.get('index_written_by')}; this install has {d.get('chromadb_version')}")
        return {"vector_index": res["data"].get("vector_index")}
    if k == "memory_backup":
        return _memory_backup(a, run)
    if k == "cli_links":
        doc = modules.cli_install() if a["action"] == "install" else modules.cli_uninstall()
        if not doc["ok"]:
            raise OpError(doc["error"]["code"], doc["error"]["message"])
        return doc.get("action")
    if k == "brew_suite":
        brew = brew_bin(a.get("prefix"))
        r = run_system([brew or "brew", "list", "--versions", "contorch"])
        have = (r.stdout.split() or ["", ""])[-1] if r.returncode == 0 else ""
        if _vkey(have) < _vkey(suite_version()):
            raise OpError("schema_newer", f"Homebrew's contorch is {have or 'missing'}, older than this app "
                                          f"({suite_version()}); `brew update` first, or wait for the tap")
        return have
    if k == "restore_if_needed":
        exe = owners.locate_in("brew", "contorch-memory", {"brew_prefix": a.get("prefix")})
        res = owners.call("contorch-memory", "status", "--json", schema="contorch-memory.status/", exe=exe,
                          env=run.env("brew"))
        if res["status"] == "ok" and res["data"].get("index_compatible") is not False:
            return "compatible"
        r = owners.call("contorch-memory", "restore", "--from", a["backup"], "--stop-server", "--json",
                        schema="contorch-memory.backup/", exe=exe, env=run.env("brew"), timeout=900)
        if r["status"] != "ok" or not r["data"].get("ok"):
            raise OpError(owners.error_of(r)["code"], owners.error_of(r)["message"])
        return "restored"
    if k == "handoff":
        exe = owners.locate_in("brew", "contorch", {"brew_prefix": a.get("prefix")})
        if exe is None:
            raise OpError("brew_failed", "Homebrew's contorch isn't installed")
        res = owners.call("contorch", "adopt", "--yes", "--json", exe=exe, env=run.env("brew"), timeout=1800)
        if res["status"] != "ok" or not res["data"].get("ok"):
            raise OpError((res["data"] or {}).get("error", {}).get("code") or "handoff_failed",
                          (res["data"] or {}).get("error", {}).get("message") or res.get("error") or "")
        run.todo += res["data"].get("todo") or []
        return "brew adopted"
    raise OpError("unknown_step", k)


def _owner_step(a: dict, run: Run) -> Any:
    """One owner verb. An owner too old for it (exit 2) gets the step's
    `fallbacks` in order (e.g. the same verb without --remove-data, then
    meeting-capture 0.7's plain `uninstall`, run as text); then `on_old`
    decides: skip (with a to-do) or fail."""
    exe = owners.locate_in(a["channel"], a["name"], a.get("layout"))
    if exe is None:
        if a["on_missing"] == "skip":
            return "skipped (not installed)"
        raise OpError("owner_missing", f"{a['name']} ({a['channel']}) isn't installed")
    for argv in [a["argv"], *(a.get("fallbacks") or [])]:
        if "--json" not in argv:                         # an older owner's text verb
            r = run_system([exe, *argv], timeout=300)
            if r.returncode == 0:
                return f"done ({a['name']} {' '.join(argv)}; older {a['name']})"
            raise OpError("owner_failed", f"{a['name']} {' '.join(argv)}: {(r.stderr or r.stdout).strip()[-300:]}")
        res = owners.call(a["name"], *argv, schema=a["schema"], exe=exe, env=run.env(a["channel"]), timeout=900)
        if res["status"] != "old":
            break
        if "--remove-data" in argv:
            run.todo.append({"code": "data_kept",
                             "message": f"{a['name']} can't remove its data yet (`{' '.join(argv[:2])} "
                                        "--remove-data`); it was kept"})
    if res["status"] == "old":
        if a["on_old"] == "skip":
            run.todo.append({"code": "owner_too_old", "message": f"{a['name']} {' '.join(a['argv'][:2])}: "
                                                                 f"{res['error']}"})
            return "skipped (older owner)"
        raise OpError("owner_too_old", f"{a['name']} is too old for `{' '.join(a['argv'][:3])}` — upgrade it")
    if res["status"] != "ok":
        raise OpError("owner_failed", res["error"])
    d = res["data"]
    for t in d.get("todo") or []:
        run.todo.append({"code": "owner_todo", "message": t if isinstance(t, str) else str(t)})
    if d.get("ok") is False:
        err = owners.error_of(res)
        raise OpError(ERROR_MAP.get(err["code"], err["code"]), err["message"])
    return {k: d[k] for k in ("action", "performed", "changed") if k in d} or None


def _memory_backup(a: dict, run: Run) -> Any:
    """`index migrate --in-process --backup-dir D` (backs up while it retires
    a chroma server); with no server, `backup --to D`. Resuming: a backup
    already in D counts."""
    d = a["dir"]
    res = owners.call("contorch-memory", "index", "migrate", "--in-process", "--backup-dir", d, "--json",
                      schema="contorch-memory.backup/", env=run.env(owners.channel()), timeout=1800)
    if res["status"] == "old":
        raise OpError("owner_too_old", "this context-orchestrator can't back up its data (needs 0.5)")
    if res["status"] != "ok":
        raise OpError("owner_failed", res["error"])
    if not res["data"].get("ok"):
        err = owners.error_of(res)
        if err["code"] != "dir_not_empty":
            raise OpError(ERROR_MAP.get(err["code"], err["code"]), err["message"])
        return "kept the backup already in " + d
    for t in res["data"].get("todo") or []:
        if "claude install" not in str(t):        # the next step is exactly that
            run.todo.append({"code": "owner_todo", "message": str(t)})
    if res["data"].get("performed"):
        return {"migrated": True, "backup": d}
    b = owners.call("contorch-memory", "backup", "--to", d, "--json", schema="contorch-memory.backup/",
                    env=run.env(owners.channel()), timeout=1800)
    if b["status"] != "ok":
        raise OpError("owner_failed", b["error"])
    if not b["data"].get("ok"):
        err = owners.error_of(b)
        if err["code"] == "dir_not_empty" and run.plan.get("resume"):
            return "kept the backup already in " + d
        raise OpError(ERROR_MAP.get(err["code"], err["code"]), err["message"])
    return {"migrated": False, "backup": d}


def _vkey(v: str | None) -> tuple:
    import re
    parts = re.split(r"[.+-]", v or "0")[:3]
    return tuple(int(p) if p.isdigit() else 0 for p in parts) + (0,) * (3 - len(parts))


# ------------------------------------------------------------------ CLI (shared with uninstall)

def add_cli(sub) -> None:
    for name, help_ in (("adopt", "make this install own Contorch on this Mac (backs up your data first)"),
                        ("rollback", "Contorch.app: go back to the Homebrew install it adopted")):
        p = sub.add_parser(name, help=help_)
        _common_flags(p)
        if name == "adopt":
            p.add_argument("--from", dest="frm", choices=owners.CHANNELS,
                           help="the install to take over (default: the marker's owner, else Homebrew's)")
        p.set_defaults(func=_cmd)


def _common_flags(p) -> None:
    p.add_argument("--plan", action="store_true", help="show the steps; change nothing")
    p.add_argument("--yes", action="store_true", help="don't ask (required without a terminal)")
    p.add_argument("--json", action="store_true", help="JSON Lines progress, then {\"event\": \"result\"}")
    p.add_argument("--not-recording", action="store_true",
                   help="you know no meeting is being recorded (when meeting-capture can't tell)")


def _cmd(args) -> int:
    if args.cmd == "adopt":
        plan = plan_adopt(frm=args.frm, not_recording=args.not_recording)
    else:
        plan = plan_rollback(not_recording=args.not_recording)
    return run_cli(args.cmd, plan, args)


def run_cli(cmd: str, plan: dict, args, execute: Callable | None = None) -> int:
    """--plan / confirmation / --yes, with JSON or text output (adopt,
    rollback, uninstall). No terminal and no --yes: needs_yes, nothing done."""
    from . import jsonout
    execute = execute or globals()["execute"]
    schema = f"contorch.{cmd}"
    if args.plan or not plan.get("ok"):
        doc = {"schema": f"{schema}.plan/1", **plan}
        if args.json:
            with jsonout.reserved_stdout() as out:
                jsonout.emit(doc, out)
        else:
            _print_plan(cmd, plan)
        return 0 if plan.get("ok") else 1
    if not args.yes:
        if not sys.stdin.isatty():
            doc = {"schema": f"{schema}/1", "event": "result", "ok": False,
                   "error": {"code": "needs_yes", "message": "no terminal to confirm on: pass --yes"}}
            if args.json:
                with jsonout.reserved_stdout() as out:
                    jsonout.emit(doc, out)
            else:
                print("✗ no terminal to confirm on: pass --yes", file=sys.stderr)
            return 1
        _print_plan(cmd, plan)
        if input(f"\nRun these {len(plan['steps'])} steps? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    if args.json:
        with jsonout.reserved_stdout() as out:
            res = execute(plan, log=lambda e: jsonout.emit({"schema": f"{schema}/1", **e}, out))
            jsonout.emit({"schema": f"{schema}/1", "event": "result", **res}, out)
    else:
        res = execute(plan, log=lambda e: print(f"  [{e['step']}/{e['of']}] {e['why'] or e['kind']}"))
        if res["ok"]:
            print(f"\n✓ {cmd} done" + (f" — {res['message']}" if res.get("message") else ""))
        else:
            print(f"\n✗ {cmd} stopped: {res['error']['message']}\n  Run it again to resume.", file=sys.stderr)
        for t in res.get("todo") or []:
            print(f"  · {t.get('message') or t.get('code')}")
    return 0 if res["ok"] else 1


def _print_plan(cmd: str, plan: dict) -> None:
    if not plan.get("ok"):
        print(f"✗ {plan['error']['message']}", file=sys.stderr)
        return
    if plan.get("noop"):
        print(plan.get("message") or "nothing to do")
        return
    head = f"contorch {cmd}"
    if plan.get("from") or plan.get("to"):
        head += f": {plan.get('from')} → {plan.get('to')}"
    print(head + (" (resuming)" if plan.get("resume") else ""))
    if plan.get("backup_dir"):
        print(f"  backups: {plan['backup_dir']}")
    for i, st in enumerate(plan["steps"], 1):
        what = st["args"].get("name") or ""
        argv = " ".join(st["args"].get("argv") or [])
        print(f"  {i:>2}. {st['why'] or st['kind']}" + (f"  ({what} {argv})".rstrip() if argv else ""))
    for t in plan.get("todo") or []:
        print(f"  then: {t.get('message') or t.get('code')}")
