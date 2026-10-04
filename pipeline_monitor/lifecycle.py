"""What happens when the menu bar app starts, quits or updates — Python
decides, the shells (rumps today, SwiftUI later) only report the event.

    on_launch()        every channel: resume the stack if a QUIT or an UPDATE
                       stopped it (never after the user's own "Stop
                       everything"). This is what brings the recorder back
                       after `brew services restart`, `brew upgrade` or a
                       SIGTERM when "Keep recording after Quit" is off.
                       Inside Contorch.app only: where it runs from
                       (location()), the channel marker (claim + attention),
                       heal (`meeting-capture heal --json`, and Claude Code's
                       entries through `contorch-memory claude install` when
                       its status isn't ok) — only while meeting-capture says
                       nothing is being recorded — and whether to offer setup.
    on_quit(reason)    reason user | signal | logout | update | handover.
                       logout: launchd takes the agents down, nothing to do.
                       handover: another install takes over (Move to
                       Applications… started the copy; Go back to Homebrew
                       and Uninstall already stopped what they own), nothing
                       to do. update (or an
                       update staged to install on quit): always stop
                       (reason=update). user / signal: stop (reason=quit)
                       unless "Keep recording after Quit" is on.
    install_allowed()  may an update replace the app now? Only when
                       `meeting-capture status --json` says recording: false;
                       recording, and can't-tell, hold it.
    prepare_update()   stop the stack (reason=update) before the swap.
    move_to_applications()  copy the running app into /Applications (or
                       ~/Applications), the way "Move to Applications…" does
                       it; the menu then opens the copy and quits.

Preferences (~/.contorch/preferences.json, `contorch preferences …`):
    keep_recording_after_quit   default: app off (Quit stops recording);
                                Homebrew and source on (today's behaviour:
                                the menu bar is a KeepAlive brew service)
    update_feed                 stable | beta
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from . import channel as chan
from . import modules, owners

QUIT_REASONS = ("user", "signal", "logout", "update", "handover")
FEEDS = ("stable", "beta")
update_staged = False            # set by the Sparkle delegate (willInstallUpdateOnQuit; M4)


def _ct():
    from . import contorch
    return contorch


# ------------------------------------------------------------------ preferences

def preferences_path() -> Path:
    return Path.home() / ".contorch" / "preferences.json"


def _prefs() -> dict:
    try:
        d = json.loads(preferences_path().read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_prefs(d: dict) -> None:
    p = preferences_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n")
    os.replace(tmp, p)


def keep_recording_after_quit() -> bool:
    v = _prefs().get("keep_recording_after_quit")
    if isinstance(v, bool):
        return v
    return owners.channel() != "app"


def set_keep_recording_after_quit(on: bool) -> None:
    _write_prefs({**_prefs(), "keep_recording_after_quit": bool(on)})


def update_feed() -> str:
    v = _prefs().get("update_feed")
    return v if v in FEEDS else "stable"


def set_update_feed(feed: str) -> None:
    if feed not in FEEDS:
        raise ValueError(f"update feed must be one of {', '.join(FEEDS)}")
    _write_prefs({**_prefs(), "update_feed": feed})


def preferences() -> dict:
    return {"schema": "contorch.preferences/1", "ok": True,
            "keep_recording_after_quit": keep_recording_after_quit(), "update_feed": update_feed(),
            "defaults": {"keep_recording_after_quit": owners.channel() != "app"}}


# ------------------------------------------------------------------ facts

def recording() -> bool | None:
    """meeting-capture's answer: True / False / None (can't tell, or a
    meeting-capture without `status --json`)."""
    if owners.locate("meeting-capture") is None:
        return False                     # no recorder on this Mac
    res = owners.call("meeting-capture", "status", "--json", schema="meeting-capture.status/", timeout=30)
    if res["status"] != "ok":
        return None
    r = res["data"].get("recording")
    return r if isinstance(r, bool) else None


def recorder_installed() -> bool:
    from . import mcconfig
    return owners.locate("meeting-capture") is not None and mcconfig.installed()


def location() -> str:
    """Where the app runs from: ok | translocated | read_only |
    outside_applications (not_in_app outside a bundle). Only `ok` registers
    agents or a login item; the others get "Move to Applications…"."""
    root = owners.bundle_root()
    if root is None:
        return "not_in_app"
    path = str(root)
    if "/AppTranslocation/" in path:
        return "translocated"
    # Read-only = the volume (the DMG). NOT os.access(path, W_OK): macOS's App Management
    # protection (com.apple.macl on a launched, notarized bundle) makes access() say "not
    # writable" for every installed copy, which would turn off Sparkle and the login item in
    # /Applications (measured on macOS 27 with a notarized labtest build).
    try:
        if os.statvfs(path).f_flag & os.ST_RDONLY:
            return "read_only"
    except OSError:
        return "read_only"
    allowed = ("/Applications/", str(Path.home() / "Applications") + "/")
    if not any((path + "/").startswith(a) for a in allowed):
        return "outside_applications"
    return "ok"


# ------------------------------------------------------------------ the events

def on_launch() -> dict[str, Any]:
    ct = _ct()
    out: dict[str, Any] = {"resumed": None, "healed": None, "needs_setup": False, "location": location(),
                           "attention": []}
    reason = ct.stopped_reason()
    if reason in ("quit", "update"):
        lines: list[str] = []
        out["resumed"] = ct.resume(log=lines.append)
        out["resume_lines"] = lines
    if owners.channel() != "app" or out["location"] == "not_in_app":
        return out
    if out["location"] != "ok":
        return out                       # nothing registers from a translocated / read-only copy
    marker = chan.read()
    if marker is None or modules.wanted() is None:
        out["needs_setup"] = True
    claimed = chan.claim()
    out["attention"] = chan.attention()
    if not claimed["ok"]:
        out["attention"].append({"code": claimed.get("code"), "message": claimed.get("message")})
        return out
    if recording() is False:
        healed = owners.call("meeting-capture", "heal", "--json", schema="meeting-capture.agent/", timeout=120)
        out["healed"] = healed["status"] == "ok" and bool((healed["data"] or {}).get("performed"))
        st = owners.call("contorch-memory", "claude", "status", "--channel", "app", "--json",
                         schema="contorch-memory.claude/", timeout=60)
        if st["status"] == "ok" and not st["data"].get("ok") and (st["data"].get("mcp") or {}).get("present"):
            owners.call("contorch-memory", "claude", "install", "--channel", "app", "--json",
                        schema="contorch-memory.claude/", timeout=300)
            out["healed"] = True
    return out


def on_quit(reason: str) -> dict[str, Any]:
    ct = _ct()
    reason = reason if reason in QUIT_REASONS else "user"
    keep = keep_recording_after_quit()
    if ct.is_stopped():
        action = "none (already stopped)"
    elif reason == "logout":
        action = "none (logout: launchd stops the agents; nothing is persisted)"
    elif reason == "handover":
        action = "none (handed over: another install, or nothing, takes over)"
    elif not recorder_installed():
        action = "none (this Mac doesn't record)"
    elif reason == "update" or update_staged:
        ct.stop(log=lambda _m: None, reason="update")
        action = "stopped (update)"
    elif keep:
        action = "none (Keep recording after Quit is on)"
    else:
        ct.stop(log=lambda _m: None, reason="quit")
        action = "stopped (quit)"
    return {"reason": reason, "keep_recording_after_quit": keep, "update_staged": update_staged,
            "action": action, "at": time.time()}


def install_allowed() -> tuple[bool, str]:
    rec = recording()
    if rec is False:
        return True, "idle"
    if rec is True:
        return False, "recording"
    return False, "recording_unknown"


def prepare_update() -> bool:
    ct = _ct()
    if ct.is_stopped() and ct.stopped_reason() == "user":
        return True                      # stays the user's stop
    return ct.stop(log=lambda _m: None, reason="update")


def applications_dir() -> Path:
    """/Applications when this user may write there, else ~/Applications."""
    sys_apps = Path("/Applications")
    return sys_apps if os.access(sys_apps, os.W_OK) else Path.home() / "Applications"


def _run(argv: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _trash(path: Path) -> bool:
    """Move an older copy to the Trash (recoverable), the way Finder would."""
    try:
        from Foundation import NSFileManager, NSURL
        ok, _url, _err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            NSURL.fileURLWithPath_(str(path)), None, None)
        return bool(ok)
    except Exception:
        return False


def move_to_applications(dest_dir: Path | None = None) -> dict[str, Any]:
    """Copy the running Contorch.app into Applications.

    -> {ok, dest, replaced?} | {ok: False, error: {code, message}}
    An older copy already there goes to the Trash first (an update by hand).
    The copy loses com.apple.quarantine: Gatekeeper already approved this app
    when it first opened, and a copy that keeps the attribute would be
    translocated again. Nothing is registered from here: the copy's own
    on_launch does that."""
    root = owners.bundle_root()
    if root is None:
        return {"ok": False, "error": {"code": "not_in_app", "message": "not running from Contorch.app"}}
    dest_dir = dest_dir or applications_dir()
    dest = dest_dir / root.name
    try:
        if dest.resolve() == root.resolve():
            return {"ok": True, "dest": str(dest), "noop": True}
    except OSError:
        pass
    dest_dir.mkdir(parents=True, exist_ok=True)
    replaced = False
    if dest.exists():
        if not _trash(dest):
            return {"ok": False, "error": {"code": "exists",
                                           "message": f"{dest} already exists and couldn't be moved to the "
                                                      "Trash — quit it and delete it, then try again"}}
        replaced = True
    res = _run(["ditto", str(root), str(dest)])
    if res.returncode != 0:
        return {"ok": False, "error": {"code": "copy_failed", "message": (res.stderr or res.stdout).strip()[-300:]}}
    _run(["xattr", "-dr", "com.apple.quarantine", str(dest)], timeout=120)
    return {"ok": True, "dest": str(dest), "replaced": replaced}


def watch_key() -> tuple:
    """mtimes of the files the menu re-evaluates on (channel.json,
    modules.json, preferences.json): setup runs in Terminal and writes them."""
    out = []
    for p in (chan.marker_path(), modules.state_file(), preferences_path()):
        try:
            out.append(p.stat().st_mtime_ns)
        except OSError:
            out.append(None)
    return tuple(out)


# ------------------------------------------------------------------ CLI

def add_cli(sub) -> None:
    p = sub.add_parser("preferences", help="Keep recording after Quit, update feed")
    p.add_argument("action", nargs="?", choices=("show", "set"), default="show")
    p.add_argument("key", nargs="?", choices=("keep-recording-after-quit", "update-feed"))
    p.add_argument("value", nargs="?")
    p.add_argument("--json", action="store_true", help="one JSON document (contorch.preferences/1)")
    p.set_defaults(func=_cmd_preferences)
    q = sub.add_parser("lifecycle", help="what the app does at launch and quit (for the app's shell)")
    q.add_argument("event", choices=("launch", "quit", "install-allowed"))
    q.add_argument("--reason", choices=QUIT_REASONS, default="user")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_cmd_lifecycle)


def _emit(doc: dict) -> None:
    from . import jsonout
    with jsonout.reserved_stdout() as out:
        jsonout.emit(doc, out)


def _cmd_preferences(args) -> int:
    if args.action == "set":
        try:
            if args.key == "keep-recording-after-quit" and args.value in ("on", "off"):
                set_keep_recording_after_quit(args.value == "on")
            elif args.key == "update-feed":
                set_update_feed(args.value or "")
            else:
                raise ValueError("usage: contorch preferences set keep-recording-after-quit on|off "
                                 "| update-feed stable|beta")
        except ValueError as e:
            if args.json:
                _emit({"schema": "contorch.preferences/1", "ok": False,
                       "error": {"code": "usage", "message": str(e)}})
            else:
                print(f"✗ {e}")
            return 2
    doc = preferences()
    if args.json:
        _emit(doc)
    else:
        print(f"keep recording after Quit: {'on' if doc['keep_recording_after_quit'] else 'off'}")
        print(f"update feed:               {doc['update_feed']}")
    return 0


def _cmd_lifecycle(args) -> int:
    if args.event == "launch":
        doc = on_launch()
    elif args.event == "quit":
        doc = on_quit(args.reason)
    else:
        ok, why = install_allowed()
        doc = {"allowed": ok, "reason": why}
    doc = {"schema": "contorch.lifecycle/1", "ok": True, "event": args.event, **doc}
    if args.json:
        _emit(doc)
    else:
        print(json.dumps(doc, indent=2, default=str))
    return 0
