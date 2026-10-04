"""Contorch modules, declared once (both channels).

    memory    context.db (SQLite + FTS5) + the in-process vector index, the
              MCP server, the transcripts skill, the CLAUDE.md block, the
              auto-context hook. No background process. Base of everything.
    recorder  records this Mac's calls (one background agent) and
              transcribes them into memory. Needs a transcription engine:
              on-device (macOS 26+) or an accepted Gemini key.
    linein    a USB audio interface as the recorder's source.
    cli       the commands on your PATH (always on with Homebrew).

Per module and Mac, three facts:

    present   its code can run here. App: always (the bundle has
              everything). Homebrew/dev: its owner's executable is found
              (owners.locate). `brew install contorch --without-meeting-capture`
              is a memory-only Mac: no recorder code on disk.
    wanted    the user's choice in ~/.contorch/modules.json
              (`contorch.modules/1`), written only by set_wanted(). No file
              = never chosen: infer_wanted() derives it from what is set up,
              so existing installs migrate with no question asked.
    actual    is it really set up / running — the owners' answers, passed in
              (observe() asks them).

Turning a module on or off runs its OWNER's command (MODULES below); this
file holds no recorder or memory logic. Used by `contorch setup`, `contorch
modules`, the menu bar's greyed rows and the tap's CI (`modules brew-spec`).
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import owners

SCHEMA = "contorch.modules/1"
TAP = "contorch/tap"
ORDER = ("memory", "recorder", "linein", "cli")
EMBEDDINGS_SOURCES = ("local", "gemini", "imported")


@dataclass(frozen=True)
class Module:
    id: str
    title: str
    summary: str
    requires: tuple[str, ...] = ()
    default: dict[str, bool] = field(default_factory=dict)       # per channel, for setup
    entry: str | None = None                                      # executable whose presence = code present
    owner: str = ""
    background: str | None = None                                 # launchd label while on
    permissions: tuple[str, ...] = ()
    enable: tuple[tuple[str, ...], ...] = ()                       # owner argv run to turn it on
    disable: tuple[tuple[str, ...], ...] = ()
    brew_add: str | None = None                                    # how Homebrew adds the code
    brew_always_on: bool = False                                   # Homebrew provides it by construction


MODULES: dict[str, Module] = {m.id: m for m in (
    Module("memory", "Memory",
           "Your meetings and notes, searchable from Claude Code. Nothing runs in the background.",
           default={"app": True, "brew": True, "dev": True},
           entry="contorch-mcp", owner="context-orchestrator",
           enable=(("contorch-memory", "claude", "install", "--json"),),
           disable=(("contorch-memory", "claude", "uninstall", "--json"),),
           brew_add=f"brew install {TAP}/contorch"),
    Module("recorder", "Meeting recorder",
           "Records your calls on this Mac and turns them into transcripts.",
           requires=("memory",), default={"app": True, "brew": True, "dev": True},
           entry="meeting-capture", owner="meeting-capture",
           background="com.contorch.meeting-capture",
           permissions=("screen_audio", "microphone"),
           enable=(("meeting-capture", "install", "--json"),),
           disable=(("meeting-capture", "uninstall", "--json"),),
           brew_add=f"brew install {TAP}/meeting-capture"),
    Module("linein", "Audio interface (line-in)",
           "Record from a USB audio interface instead of this Mac's call audio.",
           requires=("recorder",), default={"app": False, "brew": False, "dev": False},
           entry="meeting-capture", owner="meeting-capture",
           permissions=("microphone",),
           # Turning it on only offers the source; the device is chosen on the
           # Recording settings page (meeting-capture ui). Off = back to this Mac.
           enable=(),
           disable=(("meeting-capture", "source", "sck"),),
           brew_add=f"brew install {TAP}/meeting-capture"),
    Module("cli", "Terminal commands",
           "contorch, contorch-transcripts and meeting-capture on your PATH.",
           requires=("memory",), default={"app": False, "brew": True, "dev": True},
           entry="contorch", owner="pipeline-monitor",
           enable=(("contorch", "cli", "install", "--json"),),
           disable=(("contorch", "cli", "uninstall", "--json"),),
           brew_always_on=True),
)}


def channel() -> str:
    """app | brew | dev — $CONTORCH_CHANNEL only (owners.channel)."""
    return owners.channel()


def locate(name: str) -> str | None:
    return owners.locate(name)


def present(ch: str | None = None) -> set[str]:
    ch = ch or channel()
    if ch == "app":
        return set(ORDER)
    return {m for m in ORDER if (MODULES[m].brew_always_on and ch == "brew")
            or (MODULES[m].entry and locate(MODULES[m].entry))}


# ------------------------------------------------------------------ the user's choice

def state_file() -> Path:
    return Path.home() / ".contorch" / "modules.json"


def _read() -> dict | None:
    try:
        d = json.loads(state_file().read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("wanted"), dict) else None


def wanted() -> dict[str, bool] | None:
    d = _read()
    if d is None:
        return None
    return {m: bool(d["wanted"].get(m, False)) for m in ORDER}


def embeddings_source() -> str | None:
    v = (_read() or {}).get("embeddings_source")
    return v if v in EMBEDDINGS_SOURCES else None


def infer_wanted(actual: dict[str, bool | None]) -> dict[str, bool] | None:
    """Migration for installs made before modules existed: what is set up is
    what was wanted. None when nothing is set up (a fresh Mac)."""
    if not any(actual.get(m) for m in ORDER):
        return None
    w = {m: bool(actual.get(m)) for m in ORDER}
    for m in ORDER:                      # keep the requirement graph consistent
        if w[m]:
            for r in closure([m]):
                w[r] = True
    return w


def _write(choice: dict[str, bool], embeddings: str | None = None) -> None:
    f = state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {"schema": SCHEMA, "wanted": {m: bool(choice.get(m)) for m in ORDER},
                           "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    src = embeddings if embeddings is not None else embeddings_source()
    if src in EMBEDDINGS_SOURCES:
        doc["embeddings_source"] = src
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    os.replace(tmp, f)


def set_embeddings_source(src: str) -> None:
    if src not in EMBEDDINGS_SOURCES:
        raise ValueError(f"embeddings source must be one of {', '.join(EMBEDDINGS_SOURCES)}")
    _write(wanted() or {m: False for m in ORDER}, embeddings=src)


def closure(ids) -> list[str]:
    need: set[str] = set()

    def add(m: str) -> None:
        if m not in MODULES:
            raise ValueError(f"unknown module {m!r}; choose from {', '.join(ORDER)}")
        if m not in need:
            need.add(m)
            for r in MODULES[m].requires:
                add(r)
    for i in ids:
        add(i)
    return [m for m in ORDER if m in need]


def dependents(mid: str) -> list[str]:
    return [m for m in ORDER if m != mid and mid in closure([m])]


NO_ENGINE_TEXT = "add a Gemini key to record (on-device transcription needs macOS 26)"


def plan(enable=(), disable=(), ch: str | None = None, actual: dict | None = None,
         can_transcribe: bool | None = None) -> dict[str, Any]:
    """What a change means, without doing it: the resulting choice, the
    owner commands to run in order, and code that must be added first.

    can_transcribe=False (meeting-capture's `stt --json`: no engine, i.e.
    macOS 15 with no accepted Gemini key): the recorder stays OFF —
    nothing records audio that nothing will transcribe."""
    ch = ch or channel()
    cur = wanted() or infer_wanted(actual or {}) or {m: False for m in ORDER}
    new = dict(cur)
    for m in closure(list(enable)):
        new[m] = True
    for m in disable:
        for d in [m, *dependents(m)]:
            new[d] = False
    held = []
    if can_transcribe is False and new["recorder"] and not cur.get("recorder"):
        for d in ["recorder", *dependents("recorder")]:
            if new[d] and not cur.get(d):
                new[d] = False
                held.append(d)
    have = present(ch)
    missing = [m for m in ORDER if new[m] and m not in have]
    steps = []
    for m in reversed(ORDER):                         # turn off leaves first
        if cur.get(m) and not new[m]:
            steps += [{"module": m, "argv": list(a)} for a in MODULES[m].disable]
    for m in ORDER:                                   # turn on the base first
        if new[m] and not cur.get(m) and not (ch == "brew" and MODULES[m].brew_always_on):
            steps += [{"module": m, "argv": list(a)} for a in MODULES[m].enable]
    out = {"from": cur, "to": new, "missing_code": missing,
           "add_code": list(dict.fromkeys(MODULES[m].brew_add for m in missing if MODULES[m].brew_add)),
           "steps": steps}
    if held:
        out["held"] = {"modules": held, "code": "no_engine", "text": NO_ENGINE_TEXT}
    return out


def set_wanted(enable=(), disable=(), ch: str | None = None, actual: dict | None = None,
               can_transcribe: bool | None = None) -> dict[str, bool]:
    ch = ch or channel()
    p = plan(enable, disable, ch, actual, can_transcribe)
    if p["missing_code"]:
        raise RuntimeError("not installed on this Mac: " + ", ".join(p["missing_code"])
                           + (" — " + "; ".join(p["add_code"]) if p["add_code"] else ""))
    _write(p["to"])
    return p["to"]


# ------------------------------------------------------------------ what is set up (owners' answers)

def observe(ch: str | None = None) -> dict[str, bool | None]:
    """`actual` from the owners: memory = this install's MCP entry exists
    (`contorch-memory claude status --json`); recorder = its agent is
    installed and linein = its source is line-in (`meeting-capture config
    --json`, or the 0.7 plist); cli = the links exist. None = can't tell."""
    from . import mcconfig
    ch = ch or channel()
    out: dict[str, bool | None] = {m: None for m in ORDER}
    if locate("contorch-memory"):
        res = owners.call("contorch-memory", "claude", "status", "--channel", ch, "--json",
                          schema="contorch-memory.claude/", foreground=False, timeout=30)
        if res["status"] == "ok":
            out["memory"] = bool((res["data"].get("mcp") or {}).get("present"))
    else:
        out["memory"] = False
    if locate("meeting-capture"):
        out["recorder"] = mcconfig.installed()
        out["linein"] = bool(out["recorder"]) and (mcconfig.setting("source", "sck") == "linein")
    else:
        out["recorder"] = out["linein"] = False
    if ch == "brew":
        out["cli"] = True
    elif ch == "app":
        out["cli"] = bool(cli_links()["linked"])
    else:
        out["cli"] = True if locate("contorch") else None
    return out


# ------------------------------------------------------------------ the one status call

def add_hint(mid: str, ch: str, have: set[str]) -> dict[str, Any]:
    m = MODULES[mid]
    if ch != "app" and mid not in have and m.brew_add:
        return {"kind": "command", "command": m.brew_add,
                "text": f"{m.title} isn't installed. In Terminal: {m.brew_add}"}
    return {"kind": "action", "action": "enable", "module": mid,
            "command": f"contorch modules enable {mid}", "text": f"Turn on {m.title.lower()}…"}


def status(ch: str | None = None, actual: dict[str, bool | None] | None = None,
           can_transcribe: bool | None = None) -> dict[str, Any]:
    """Per module: present/wanted/actual and ONE state for every UI:
    on | attention | available | unavailable | missing | needs_setup."""
    ch = ch or channel()
    actual = actual or {}
    have = present(ch)
    want = wanted()
    inferred = False
    if want is None:
        want = infer_wanted(actual)
        inferred = want is not None
    rows = []
    for mid in ORDER:
        m = MODULES[mid]
        w = None if want is None else want[mid]
        a = actual.get(mid)
        if ch == "brew" and m.brew_always_on:
            w, a = True, True
        blocked_by = [r for r in m.requires if want is not None and not want.get(r)]
        if want is None:
            state = "needs_setup"
        elif w and a is False:
            state = "attention"
        elif w:
            state = "on"
        elif blocked_by:
            state = "unavailable"
        elif mid not in have:
            state = "missing"
        else:
            state = "available"
        row = {"id": mid, "title": m.title, "summary": m.summary, "present": mid in have,
               "wanted": w, "actual": a, "state": state, "requires": list(m.requires),
               "blocked_by": blocked_by, "background": m.background,
               "permissions": list(m.permissions), "default": m.default.get(ch, False)}
        if mid == "recorder" and state == "available" and can_transcribe is False:
            row["no_engine"] = NO_ENGINE_TEXT
        if state in ("missing", "available"):
            row["add"] = add_hint(mid, ch, have)
        rows.append(row)
    return {"schema": SCHEMA, "ok": True, "channel": ch, "set_up": want is not None,
            "inferred": inferred, "record_on_this_mac": bool(want and want["recorder"]),
            "embeddings_source": embeddings_source(), "modules": rows}


def menu_lines(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows the menu shows for modules that are NOT on: a greyed title plus
    the one way to add it (per channel). rumps renders these as disabled
    items with a clickable child; the SwiftUI shell renders the same data."""
    out = []
    for r in doc["modules"]:
        if r["state"] == "on":
            continue
        if r["state"] == "needs_setup":
            return [{"id": "setup", "enabled": True, "text": "Set up Contorch…",
                     "action": {"command": "contorch setup"}}]
        if r["state"] == "unavailable":
            needs = ", ".join(MODULES[b].title.lower() for b in r["blocked_by"])
            out.append({"id": r["id"], "enabled": False, "text": f"{r['title']} — needs {needs}"})
        elif r.get("no_engine"):
            out.append({"id": r["id"], "enabled": False, "text": f"{r['title']}: {r['no_engine']}"})
        elif r["state"] in ("missing", "available"):
            out.append({"id": r["id"], "enabled": False, "text": r["title"], "add": r["add"]})
        elif r["state"] == "attention":
            out.append({"id": r["id"], "enabled": True, "severity": "warn",
                        "text": f"{r['title']} is on but not set up — run Set up Contorch… again",
                        "action": {"command": "contorch setup"}})
    return out


def brew_formula_spec() -> list[dict[str, Any]]:
    """The tap, derived from MODULES (tap CI diffs this against Formula/)."""
    return [
        {"formula": "context-orchestrator", "provides": ["memory"], "depends_on": []},
        {"formula": "meeting-capture", "provides": ["recorder", "linein"], "depends_on": []},
        {"formula": "contorch", "provides": ["cli", "menu bar"],
         "depends_on": ["context-orchestrator", "meeting-capture => :recommended"]},
    ]


# ------------------------------------------------------------------ the cli module (app channel)

CLI_COMMANDS = ("contorch", "meeting-capture", "contorch-transcripts", "contorch-memory")


def cli_dir() -> Path:
    return Path.home() / ".local" / "bin"


def cli_links() -> dict[str, Any]:
    """~/.local/bin links that point into a Contorch.app bundle's
    Contents/Resources/bin (ours); anything else there is the user's."""
    linked, foreign = [], []
    for name in CLI_COMMANDS:
        p = cli_dir() / name
        if p.is_symlink():
            target = os.readlink(p)
            (linked if "/Contents/Resources/bin/" in target else foreign).append(str(p))
        elif p.exists():
            foreign.append(str(p))
    return {"dir": str(cli_dir()), "linked": linked, "foreign": foreign}


def cli_install() -> dict[str, Any]:
    ch = channel()
    if ch != "app":
        return {"schema": "contorch.cli/1", "ok": True, "action": "none", "channel": ch,
                "why": "Homebrew and source installs put the commands on PATH themselves"}
    root = owners.bundle_root()
    if root is None:
        return {"schema": "contorch.cli/1", "ok": False, "action": "none",
                "error": {"code": "not_in_app", "message": "run this from Contorch.app's contorch"}}
    bindir = root / "Contents" / "Resources" / "bin"
    d = cli_dir()
    d.mkdir(parents=True, exist_ok=True)
    made, kept = [], []
    for name in CLI_COMMANDS:
        p, target = d / name, bindir / name
        if p.is_symlink() and "/Contents/Resources/bin/" in os.readlink(p):
            if os.readlink(p) == str(target):
                continue
            p.unlink()                                   # ours, from a moved app
        elif p.exists() or p.is_symlink():
            kept.append(str(p))
            continue
        p.symlink_to(target)
        made.append(str(p))
    on_path = str(d) in os.environ.get("PATH", "").split(os.pathsep)
    return {"schema": "contorch.cli/1", "ok": True, "action": "linked" if made else "none",
            "linked": made, "kept_user_files": kept, "dir": str(d), "on_path": on_path}


def cli_uninstall() -> dict[str, Any]:
    removed = []
    for p in cli_links()["linked"]:
        Path(p).unlink(missing_ok=True)
        removed.append(p)
    return {"schema": "contorch.cli/1", "ok": True, "action": "removed" if removed else "none",
            "removed": removed}


# ------------------------------------------------------------------ CLI

def add_cli(sub) -> None:
    p = sub.add_parser("modules", help="which parts of Contorch this Mac has and wants (memory, recorder, "
                                       "line-in, terminal commands)")
    p.add_argument("action", nargs="?", choices=("list", "enable", "disable", "plan", "brew-spec"),
                   default="list")
    p.add_argument("ids", nargs="*")
    p.add_argument("--json", action="store_true", help="one JSON document (contorch.modules/1)")
    p.set_defaults(func=_cmd_modules)
    c = sub.add_parser("cli", help="put Contorch.app's commands on your PATH (~/.local/bin) or take them off")
    c.add_argument("action", choices=("install", "uninstall"))
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=_cmd_cli)


def _emit(doc: dict, as_json: bool) -> None:
    from . import jsonout
    if as_json:
        with jsonout.reserved_stdout() as out:
            jsonout.emit(doc, out)


def _cmd_cli(args) -> int:
    doc = cli_install() if args.action == "install" else cli_uninstall()
    if args.json:
        _emit(doc, True)
    elif doc.get("error"):
        print(f"✗ {doc['error']['message']}", file=sys.stderr)
    else:
        for p in doc.get("linked") or doc.get("removed") or []:
            print(f"  {'linked' if args.action == 'install' else 'removed'} {p}")
        for p in doc.get("kept_user_files") or []:
            print(f"  kept {p} (not ours)")
        if doc.get("why"):
            print(f"  {doc['why']}")
        if args.action == "install" and doc.get("on_path") is False:
            print(f"  ! {doc['dir']} isn't on your PATH yet")
    return 0 if doc["ok"] else 1


def _cmd_modules(args) -> int:
    if args.action == "brew-spec":
        print(json.dumps(brew_formula_spec(), indent=2))
        return 0
    actual = observe()
    if args.action == "plan":
        doc = {"schema": "contorch.modules.plan/1", "ok": True, **plan(args.ids, actual=actual)}
        if args.json:
            _emit(doc, True)
        else:
            print(json.dumps(doc, indent=2))
        return 0
    if args.action in ("enable", "disable"):
        try:
            set_wanted(enable=args.ids if args.action == "enable" else (),
                       disable=args.ids if args.action == "disable" else (), actual=actual)
        except (RuntimeError, ValueError) as e:
            if args.json:
                _emit({"schema": SCHEMA, "ok": False,
                       "error": {"code": "module_missing" if isinstance(e, RuntimeError) else "unknown_module",
                                 "message": str(e)}}, True)
            else:
                print(f"✗ {e}", file=sys.stderr)
            return 1
    doc = status(actual=actual)
    if args.json:
        _emit(doc, True)
        return 0
    for r in doc["modules"]:
        line = f"  {r['title']:<28} {r['state']}"
        if r.get("add"):
            line += f"   ({r['add']['command']})"
        print(line)
    if args.action in ("enable", "disable"):
        print("\nRun `contorch setup` to apply it.")
    return 0
