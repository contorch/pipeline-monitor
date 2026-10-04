"""Menu bar app — composes status collectors into a six-section menu.

rumps gives us NSStatusItem + NSMenu with minimal Python. The icon
title is one of three glyphs:
  ●  red dot — actively recording
  ○  gray   — idle and healthy
  !  yellow — at least one subsystem is unhealthy

Click → menu with sections separated by separators, plus an actions
submenu at the bottom. Auto-refreshes every REFRESH_INTERVAL_S seconds.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Optional

import rumps
from AppKit import NSObject
from PyObjCTools import AppHelper  # noqa: F401  (ensures AppKit init order)

from . import channel as chan
from . import contorch as ct
from . import mcconfig, modules, owners
from . import status as st
from . import transcription as stt

REFRESH_INTERVAL_S = 5
PULSE_INTERVAL_S = 0.5


def _prune_menu_refs(menu) -> None:
    """Drop discarded NSMenuItems from rumps' global callback registry.

    rumps registers every MenuItem in NSApp._ns_to_py_and_callback (a PLAIN
    dict, not a WeakKeyDictionary), and Menu.clear() never prunes it. A daemon
    that rebuilds its menu on a timer therefore pins every item it ever created
    — observed leaking 6+ GB over ~13 days. Call this just before menu.clear()
    so the about-to-be-discarded items can actually be deallocated. Defensive:
    silently no-ops if rumps' internals move.
    """
    try:
        registry = rumps.rumps.NSApp._ns_to_py_and_callback
    except AttributeError:
        return

    def _walk(m):
        try:
            items = list(m.values())
        except Exception:
            return
        for item in items:
            ns = getattr(item, "_menuitem", None)
            if ns is not None:
                registry.pop(ns, None)
            _walk(item)  # recurse into submenu children

    _walk(menu)


def _log(msg: str) -> None:
    """One line to stderr (Contorch.app: ~/Library/Logs/Contorch/menubar.log)."""
    import sys
    print(msg, file=sys.stderr, flush=True)


def _notify(app: str, title: str, body: str) -> None:
    """Best-effort notification: UNUserNotificationCenter inside Contorch.app,
    osascript elsewhere (rumps.notification silently no-ops without a signed
    bundle). See pipeline_monitor.notify."""
    from .notify import post
    post(app, title, body)


# Icon assets are package data (pipeline_monitor/assets/*.png, listed in
# pyproject's package-data), so a wheel — brew's venv, the app bundle — has
# them. They used to sit in the repo root, which no install contains, so the
# menu bar fell back to a text "○". Regenerate with assets/build-menubar-icons.py.
ASSETS_DIR = Path(str(resources.files(__package__) / "assets"))
GLYPH_TEMPLATE = ASSETS_DIR / "glyph-template.png"
GLYPH_TEMPLATE_PULSE = ASSETS_DIR / "glyph-template-pulse.png"
GLYPH_REC = ASSETS_DIR / "glyph-rec.png"

# Fallback title text used only if the asset PNGs are missing (e.g. a
# user installed from an older release before icons existed).
ICON_FALLBACK_RECORDING = "● REC"
ICON_FALLBACK_OK = "○"
ICON_FALLBACK_ERR = "⚠"
ICON_FALLBACK_PERM = "⚠ PERM"


def _ago(iso_or_seconds) -> str:
    """Human-friendly 'X ago' for an ISO timestamp or age in seconds.

    SQLite's `datetime('now')` returns UTC strings with no timezone info.
    Treat any naive timestamp as UTC (not local) so we don't end up with
    negative ages on machines whose local clock is behind UTC.
    """
    from datetime import timezone
    if iso_or_seconds is None:
        return "never"
    if isinstance(iso_or_seconds, (int, float)):
        s = int(iso_or_seconds)
    else:
        try:
            s_str = str(iso_or_seconds).replace("Z", "+00:00").replace(" ", "T")
            dt = datetime.fromisoformat(s_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            s = int(time.time() - dt.timestamp())
        except Exception:
            return str(iso_or_seconds)[:19]
    if s < 0:
        return "just now"  # clock skew safety
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _truncate(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ============================================================ menu builders
#
# Design principles for the redesigned menu:
#   - One line per concept, not three
#   - No section headers — separators are enough
#   - Hide deep detail (timelines, daemon PIDs, paths) behind submenus
#   - Status glyphs only when something's wrong (●/✓/✗/⚠), not as bullets
#   - All previous functionality still reachable, just one click deeper

def _open_url_callback(url: str | None):
    """Open a Privacy & Security pane (meeting-capture's settings_url)."""
    def _cb(_=None):
        subprocess.Popen(["open", url or st.SCREEN_PANE])
    return _cb


def _mode_suffix(snap: st.Snapshot) -> str:
    """' · live' / ' · batch' for the status line; empty if the agent is missing."""
    cm = snap.capture_mode or {}
    return f" · {cm['mode']}" if cm.get("ok") else ""


UNKNOWN_REASONS = {"no_state": "the recorder hasn't reported yet", "stale_heartbeat": "the recorder stopped reporting",
                   "unknown_state": "the recorder's state is unreadable", "error": "meeting-capture didn't answer"}


def _build_status_line(snap: st.Snapshot) -> rumps.MenuItem:
    """Top of menu — the headline. ● Recording only when meeting-capture says
    so (`meeting-capture status --json`); "can't tell" is said as such."""
    if ct.is_stopped():
        return rumps.MenuItem("⏸ contorch is stopped — nothing is recording")
    head = snap.headline()
    if head == "needs_setup":
        return rumps.MenuItem("Contorch isn't set up on this Mac yet")
    if head == "memory_only":
        return rumps.MenuItem("Memory only — this Mac doesn't record")
    rec = snap.recording
    mode = _mode_suffix(snap)
    if not rec.get("ok"):
        return rumps.MenuItem(f"⚠ {_truncate(rec.get('error', 'unknown'), 50)}")
    denied = (snap.permissions or {}).get("denied") or []
    if denied:
        p = denied[0]
        return rumps.MenuItem(f"⚠ Not recording: {p['title']} is off", callback=_open_url_callback(p["settings_url"]))
    if head == "recording_unknown":
        why = UNKNOWN_REASONS.get(rec.get("reason"), rec.get("reason") or "unknown")
        return rumps.MenuItem(f"? Can't tell whether a meeting is being recorded — {why}")
    if rec.get("recording"):
        f = rec.get("current_file")
        if rec.get("stale"):
            age_s = rec.get("last_chunk_age_s") or 0
            mins = max(1, age_s // 60)
            note = f"silent for {mins}m — another app likely capturing audio (Cluely / Loom / OBS)"
            if f:
                return rumps.MenuItem(f"⚠ Recording — {Path(f).name} · {note}")
            return rumps.MenuItem(f"⚠ Recording — {note}")
        if f:
            return rumps.MenuItem(f"● Recording{mode} — {Path(f).name}")
        return rumps.MenuItem(f"● Recording{mode}")
    return rumps.MenuItem(f"○ Idle{mode}")


def _permission_lines(snap: st.Snapshot) -> list[rumps.MenuItem]:
    """One row per permission the recorder needs and doesn't have, worded by
    meeting-capture (`check --json`: its per-channel hint); a click opens
    the right Privacy pane."""
    out = []
    for p in (snap.permissions or {}).get("problems") or []:
        state = {"not_determined": "not asked yet", "denied": "off", "not_granted": "off"}.get(p["status"],
                                                                                               p["status"])
        text = f"⚠ {p['title']}: {state}" + (f" — {p['hint']}" if p.get("hint") else "")
        out.append(rumps.MenuItem(_truncate(text, 110), callback=_open_url_callback(p.get("settings_url"))))
    return out


def _module_rows(snap: st.Snapshot, on_setup=None) -> list[rumps.MenuItem]:
    """Modules that aren't on: a greyed title, then the one way to add it
    (brew: the install line, copied; app: turn it on). From
    modules.menu_lines — the same rows the SwiftUI shell will draw.
    "Set up Contorch…" opens setup in Terminal inside the app (on_setup),
    and copies `contorch setup` elsewhere."""
    out = []
    for line in modules.menu_lines(snap.modules) if snap.modules.get("modules") else []:
        if line["id"] == "setup":
            out.append(rumps.MenuItem("Set up Contorch…",
                                      callback=on_setup or _copy_command_callback("contorch setup")))
            continue
        out.append(rumps.MenuItem(line["text"], callback=(
            _copy_command_callback(line["action"]["command"]) if line.get("enabled") and line.get("action")
            else None)))
        add = line.get("add")
        if add:
            out.append(rumps.MenuItem(f"    {add['text'] if add['kind'] == 'action' else 'Copy: ' + add['command']}",
                                      callback=_copy_command_callback(add["command"])))
    return out


def _copy_command_callback(command: str):
    """Put a command on the clipboard and say where to run it (Phase 1: the
    terminal; the app's own setup window comes later)."""
    def _cb(_=None):
        if copy_to_clipboard(command):
            _notify("contorch", "Copied — paste it in Terminal", command)
    return _cb


def _attention_lines(snap: st.Snapshot) -> list[rumps.MenuItem]:
    """The channel's attention codes (pipeline_monitor.channel.attention)."""
    try:
        claude = (snap.modules or {}).get("claude") or {}
        items = chan.attention(recorder_backend=mcconfig.agent().get("backend") if snap.recorder_on() else None,
                               mcp_present=(claude.get("mcp") or {}).get("present"),
                               mcp_matches=(claude.get("mcp") or {}).get("matches"))
    except Exception:
        return []
    words = {"interrupted": "A Contorch adopt/uninstall was interrupted — run it again to finish",
             "owner_gone": "The install that managed Contorch here is gone — contorch adopt or uninstall",
             "mixed_channels": "Parts of Contorch come from another install — contorch channel",
             "brew_relinked": "Homebrew's Contorch commands are back on PATH — contorch channel",
             "marker_unreadable": "~/.contorch/channel.json is unreadable — contorch channel"}
    return [rumps.MenuItem(f"⚠ {words.get(a['code'], a['code'])}") for a in items]


def _build_transcription_line(snap: st.Snapshot) -> rumps.MenuItem | None:
    """'Transcription: on this Mac (en-US)' / 'Gemini' / '⚠ … unavailable — why',
    with ' · live: calls stream to Gemini' while live mode uploads every call,
    as meeting-capture reports it (`meeting-capture stt --json`). None when
    the recorder isn't installed (the status line says so)."""
    t = snap.transcription or {}
    if not t.get("ok"):
        return None
    text = _truncate(f"Transcription: {t.get('label') or '?'}", 90)   # an error label carries stderr
    return rumps.MenuItem(f"⚠ {text}" if t.get("attention") else text)


def _transcription_details(snap: st.Snapshot) -> list[str]:
    """Details-submenu lines: the setting, on-device state, key, live mode and
    where audio goes, all from meeting-capture's own answer."""
    t = snap.transcription or {}
    if not t.get("ok"):
        return [f"Transcription: {_truncate(t['error'], 60)}"] if t.get("error") else []
    lines = [f"Transcription: {_truncate(t.get('label') or '?', 70)}"]
    d = t.get("data")
    if d:
        lines.append(f"  setting: {d.get('choice')} · locale {d.get('locale')}")
        a = d.get("apple") or {}
        lines.append(f"  on-device: {'ready' if a.get('usable') else _truncate(a.get('reason') or '?', 60)}")
        if d.get("needs_model") and d.get("install_hint"):
            lines.append(f"  set it up: {d['install_hint']}")
        lines.append(f"  Gemini key: {'yes' if d.get('gemini_key') else 'none'}")
        live = d.get("live") or {}
        if live.get("active"):
            lines.append("  live mode: on — every call streams to Gemini (uploaded)")
        elif live.get("requested"):
            lines.append(f"  live mode: runs batch — {_truncate(live.get('blocker') or '?', 60)}")
    if t.get("privacy"):
        lines.append(f"  audio: {_truncate(t['privacy'], 100)}")
    if t.get("privacy_fix"):
        lines.append(f"  {_truncate(t['privacy_fix'], 70)}")
    if t.get("note"):
        lines.append(f"  ! {_truncate(t['note'], 100)}")
    return lines


def _meeting_capture_bin() -> str | None:
    """The meeting-capture CLI (owners.locate, pm's one binary locator:
    the app's bundle, else brew's opt/ path first — launchd gives this app no
    shell PATH). The same lookup the transcription line uses."""
    return owners.locate("meeting-capture")


def _build_index_line(snap: st.Snapshot) -> rumps.MenuItem:
    """The memory, as context-orchestrator reports it (`contorch-memory
    status --json`): documents, embeddings, keyword-only."""
    m = snap.memory or {}
    d = snap.db
    if m.get("status") == "checking":
        return rumps.MenuItem("Index: checking…")
    data = m.get("data") or {}
    if not m.get("ok"):
        return rumps.MenuItem(f"⚠ Index: {_truncate(m.get('error') or 'unavailable', 70)}")
    parts = []
    if data.get("vector_index") == "none":
        parts.append("keyword search only")
    else:
        parts.append(f"{data.get('docs') if data.get('docs') is not None else '?'} docs")
        emb = data.get("embeddings") or "auto"
        parts.append({"auto": "auto", "local": "local model"}.get(emb, "Gemini" if emb.startswith("gemini")
                                                                   else emb))
    if data.get("transcripts") is not None:
        parts.append(f"{data['transcripts']} transcripts")
    if d.get("ok"):
        parts.append(f"{d.get('repo_knowledge', 0)} insights")
    return rumps.MenuItem("Index: " + " · ".join(parts))


def _build_mcp_lines(snap: st.Snapshot) -> list[rumps.MenuItem]:
    """Two lines: MCP server activity, auto-context hook activity.
    Last-tool-call detail goes into the timeline submenu."""
    items = []
    m = snap.mcp
    h = snap.hook

    # MCP line
    if not m.get("ok"):
        items.append(rumps.MenuItem(f"⚠ MCP: {_truncate(m.get('error', '?'), 50)}"))
    else:
        last = m.get("last_call")
        if last:
            result_emoji = {"ok": "✓", "fail": "✗", "pending": "…"}.get(last.get("result"), "?")
            items.append(rumps.MenuItem(
                f"MCP: {result_emoji} {last['tool']} · {_ago(last.get('ts'))}"
            ))
        else:
            items.append(rumps.MenuItem("MCP: idle (no calls yet)"))
        # Surface a current failure prominently
        if m.get("last_error") and last and last.get("result") == "fail":
            items.append(rumps.MenuItem(f"⚠ {_truncate(m['last_error'], 60)}"))

    # Hook line
    if h.get("ok"):
        items.append(rumps.MenuItem(f"Auto-context hook: {_ago(h.get('age_s'))}"))
    else:
        items.append(rumps.MenuItem(f"Auto-context hook: {_truncate(h.get('error', 'no fires yet'), 50)}"))

    return items


def _build_system_line(snap: st.Snapshot) -> rumps.MenuItem:
    """What runs in the background. Memory is daemon-free (in-process
    index); only the recorder is an agent."""
    if not snap.recorder_on():
        return rumps.MenuItem("Background: nothing runs")
    rec = snap.recording or {}
    if rec.get("source") == "owner":
        if rec.get("pid") and rec.get("reason") != "daemon_not_running":
            return rumps.MenuItem(f"Background: recorder running (pid {rec['pid']})")
        if rec.get("reason") == "daemon_not_running":
            return rumps.MenuItem("⚠ Background: the recorder isn't running")
    l = snap.launchd
    if not l.get("ok"):
        return rumps.MenuItem(f"⚠ launchctl: {_truncate(l.get('error', '?'), 50)}")
    info = next((i for lbl, i in l.get("daemons", {}).items() if lbl.endswith("meeting-capture")), {})
    if info.get("running"):
        return rumps.MenuItem(f"Background: recorder running (pid {info['pid']})")
    return rumps.MenuItem("⚠ Background: the recorder isn't running")


def _build_recent_submenu(snap: st.Snapshot) -> rumps.MenuItem:
    """Submenu listing recent sessions. Each one has its own submenu:
    Copy transcript (the whole text to the clipboard) and Open in TextEdit.
    macOS menus have no per-item right-click, so this is the equivalent."""
    r = snap.recordings
    sessions = r.get("sessions", []) if r.get("ok") else []
    label = f"Recent sessions ({len(sessions)})" if sessions else "Recent sessions (none yet)"
    submenu = rumps.MenuItem(label)
    if not r.get("ok"):
        submenu.add(rumps.MenuItem(f"⚠ {_truncate(r.get('error', '?'), 50)}"))
        return submenu
    if not sessions:
        return submenu
    for sess in sessions[:10]:
        size_kb = sess["size"] / 1024
        title = f"{sess.get('title') or sess['name']} · {size_kb:.0f}KB · {_ago(sess['age_s'])}"
        item = rumps.MenuItem(_truncate(title, 70))
        item.add(rumps.MenuItem("Copy transcript", callback=_copy_transcript_callback(sess)))
        item.add(rumps.MenuItem(
            "Open in TextEdit",
            callback=(_open_path_callback(sess["path"]) if sess.get("path")
                      else _open_transcript_callback(sess["meeting_id"]))))
        submenu.add(item)
    return submenu


def _build_tool_calls_submenu(snap: st.Snapshot) -> rumps.MenuItem:
    """Submenu for the MCP tool-call timeline."""
    m = snap.mcp
    calls = m.get("recent_calls", []) if m.get("ok") else []
    submenu = rumps.MenuItem(f"Tool calls ({len(calls)})" if calls else "Tool calls (none)")
    for c in reversed(calls):
        emoji = {"ok": "✓", "fail": "✗", "pending": "…"}.get(c.get("result"), "?")
        lat = f" {c['latency_ms']}ms" if c.get("latency_ms") else ""
        submenu.add(rumps.MenuItem(f"{emoji} {c['tool']}{lat} · {_ago(c.get('ts'))}"))
    return submenu


def _build_details_submenu(snap: st.Snapshot) -> rumps.MenuItem:
    """Submenu containing per-daemon PIDs, disk usage, and other detail
    that's useful but doesn't deserve top-level pixels."""
    submenu = rumps.MenuItem("Details")

    # Per-daemon status
    l = snap.launchd
    if l.get("ok"):
        for label, info in l.get("daemons", {}).items():
            short = label.split(".")[-1]
            if not info.get("installed"):
                submenu.add(rumps.MenuItem(f"– {short}: not installed"))
            elif info.get("running"):
                submenu.add(rumps.MenuItem(f"✓ {short} · pid {info['pid']}"))
            else:
                submenu.add(rumps.MenuItem(f"✗ {short} · stopped (exit {info.get('status')})"))
        submenu.add(rumps.separator)

    # The memory, as context-orchestrator reports it
    m = (snap.memory or {}).get("data") or {}
    if m:
        submenu.add(rumps.MenuItem(
            f"Index: {m.get('vector_index')} · {m.get('docs')} docs · embeddings {m.get('embeddings')}"))
        submenu.add(rumps.MenuItem(
            f"chromadb {m.get('chromadb_version')} (index written by {m.get('index_written_by') or 'unknown'}"
            f"{'' if m.get('index_compatible', True) else ' — NOT compatible'})"))
    for row in (snap.modules or {}).get("modules") or []:
        submenu.add(rumps.MenuItem(f"Module {row['title']}: {row['state']}"))
    for line in _transcription_details(snap):
        submenu.add(rumps.MenuItem(line))

    # SQLite breakdown
    d = snap.db
    if d.get("ok"):
        submenu.add(rumps.MenuItem(
            f"SQLite: {d.get('tasks', 0)} tasks · {d.get('sources', 0)} sources · {d.get('repo_knowledge', 0)} insights"
        ))

    # Hook detail
    h = snap.hook
    if h.get("ok"):
        lat = f" · {h['latency_ms']}ms" if h.get("latency_ms") else ""
        chars = f" · {h['injected_chars']}c" if h.get("injected_chars") else ""
        submenu.add(rumps.MenuItem(f"Hook: {_ago(h.get('age_s'))}{lat}{chars}"))

    submenu.add(rumps.separator)

    # Disk usage
    disk = snap.disk
    if disk.get("ok"):
        for k, v in disk.get("dirs", {}).items():
            submenu.add(rumps.MenuItem(f"{k}: {v}"))

    return submenu


def _open_path_callback(path: str):
    def _cb(_):
        subprocess.Popen(["open", path])
    return _cb


def _open_transcript_callback(meeting_id: str):
    """Transcripts live in the database, not in files: pipe the text to the
    default text editor (`open -f`) instead of opening a path."""
    def _cb(_):
        text = st.transcript_text(meeting_id)
        if text is None:
            rumps.notification("contorch", "Transcript not found", meeting_id)
            return
        subprocess.run(["open", "-f"], input=text.encode("utf-8"), check=False)
    return _cb


def _session_text(sess: dict) -> str | None:
    """Full transcript text for a Recent-sessions entry (database row, or a
    legacy ~/transcripts file)."""
    if sess.get("meeting_id"):
        return st.transcript_text(sess["meeting_id"])
    try:
        return Path(sess["path"]).read_text(encoding="utf-8", errors="replace")
    except (OSError, KeyError, TypeError):
        return None


def copy_to_clipboard(text: str) -> bool:
    res = subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
    return res.returncode == 0


def _copy_transcript_callback(sess: dict):
    def _cb(_):
        text = _session_text(sess)
        name = sess.get("title") or sess.get("name") or "transcript"
        if not text:
            rumps.notification("contorch", "Transcript not found", name)
            return
        if copy_to_clipboard(text):
            rumps.notification("contorch", "Transcript copied",
                               f"{name} — {len(text.split()):,} words on the clipboard")
        else:
            rumps.notification("contorch", "Couldn't copy the transcript", name)
    return _cb


def _open_dir_callback(path: Path):
    def _cb(_):
        if path.is_dir():
            subprocess.Popen(["open", str(path)])
        else:
            rumps.notification("pipeline-monitor", "Not found", str(path))
    return _cb


# ============================================================ Contorch.app glue
#
# Presentation only: what each item does is decided by its Python owner —
# lifecycle (location, move, quit), updates (Sparkle), loginitem
# (SMAppService.mainApp), setup_launcher (setup in Terminal), and the
# `contorch uninstall|rollback` verbs.

ISSUES_URL = "https://github.com/contorch/contorch-macos/issues/new"
LOGS_DIR = Path.home() / "Library" / "Logs" / "Contorch"


def in_app() -> bool:
    return owners.channel() == "app" and owners.bundle_root() is not None


def app_version() -> str | None:
    """'0.4.0 (12)' from the running app's Info.plist (the menu bar process
    sees the app as its main bundle), else None."""
    try:
        from Foundation import NSBundle
        b = NSBundle.mainBundle()
        short = b.objectForInfoDictionaryKey_("CFBundleShortVersionString")
        build = b.objectForInfoDictionaryKey_("CFBundleVersion")
    except Exception:
        return None
    if not short:
        return None
    return f"{short} ({build})" if build else str(short)


def report_url() -> str:
    """A new contorch-macos issue with the versions filled in — no logs (Copy
    diagnostics is the redacted way to add them)."""
    import platform
    from urllib.parse import urlencode
    from . import __version__
    lines = [
        "**What happened?**", "", "", "**What did you expect?**", "", "",
        "---",
        f"Contorch.app: {app_version() or 'not the app'}",
        f"pipeline-monitor: {__version__} · channel: {owners.channel()}",
        f"macOS {platform.mac_ver()[0]} ({platform.machine()})",
        "Diagnostics: menu › Diagnostics › Copy diagnostics, then paste here (redacted: no keys, no transcript text)",
    ]
    return ISSUES_URL + "?" + urlencode({"body": "\n".join(lines)})


def has_verb(module: str) -> bool:
    """Whether this pipeline-monitor ships `contorch <module>` (adopt/rollback,
    uninstall): menu items for verbs it doesn't have aren't shown."""
    import importlib.util
    return importlib.util.find_spec(f"pipeline_monitor.{module}") is not None


def _alert(title: str, message: str, buttons: list[str], checkbox: str | None = None) -> tuple[int, bool]:
    """A modal NSAlert in front of everything (a menu bar app has no window):
    -> (index of the button pressed, whether the checkbox is ticked)."""
    from AppKit import NSAlert, NSApplication, NSButton, NSMakeRect
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(message)
    for b in buttons:
        a.addButtonWithTitle_(b)
    box = None
    if checkbox:
        box = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 22))
        box.setButtonType_(3)              # NSButtonTypeSwitch
        box.setTitle_(checkbox)
        box.setState_(0)                   # unticked by default
        a.setAccessoryView_(box)
    rc = int(a.runModal())
    return rc - 1000, bool(box is not None and box.state())   # NSAlertFirstButtonReturn = 1000


def _on_main(fn, *args) -> None:
    AppHelper.callAfter(fn, *args)


# ============================================================ menu delegate

class _MenuOpenDelegate(NSObject):
    """NSMenu delegate — fires a sync refresh right before the menu
    appears, so the items the user sees are always current.

    Without this, rumps' periodic refresh rebuilds the underlying
    NSMenu items every 5s, but macOS only renders the dropdown at
    open time. If you opened the menu, then started a recording, the
    items would stay frozen on whatever was true at open time.
    """

    def initWithApp_(self, app):
        self = self.init()
        if self is None:
            return None
        self._app = app
        return self

    def menuWillOpen_(self, menu):
        try:
            self._app._refresh_callback(None)
        except Exception:
            pass


# ============================================================ app

class PipelineMonitor(rumps.App):
    def __init__(self):
        self._updates = None                      # Contorch.app: pipeline_monitor.updates.Updates
        self._busy: str | None = None             # "Uninstalling…" etc. while a verb runs
        self._mode_switching: str | None = None  # target mode while a switch is in flight
        self._stack_busy: str | None = None      # 'Stopping'/'Resuming' while a stop/resume runs
        # Use the template glyph if available; otherwise fall back to text.
        # template=True tells macOS to auto-tint for light/dark menu bars.
        if GLYPH_TEMPLATE.exists():
            super().__init__(
                "pipeline-monitor",
                icon=str(GLYPH_TEMPLATE),
                template=True,
                quit_button=None,
            )
            self._has_icons = True
        else:
            super().__init__(
                "pipeline-monitor",
                title=ICON_FALLBACK_OK,
                quit_button=None,
            )
            self._has_icons = False

        self._snap: Optional[st.Snapshot] = None
        self._pulse_phase = 0  # 0 = base glyph, 1 = bolder pulse glyph
        self._is_pulsing = False

        self.refresh_timer = rumps.Timer(self._refresh_callback, REFRESH_INTERVAL_S)
        self.refresh_timer.start()
        # Pulse timer is only started when we enter recording state.
        self.pulse_timer = rumps.Timer(self._pulse_tick, PULSE_INTERVAL_S)

        self._refresh_callback(None)  # initial sync paint

        # Wire menuWillOpen so the dropdown shows fresh data even if the
        # 5s timer hasn't fired since the user clicked. Has to be set
        # AFTER the first repaint (which is the call above) because
        # rumps doesn't materialize the underlying NSMenu until the
        # first add().
        self._menu_delegate = _MenuOpenDelegate.alloc().initWithApp_(self)
        try:
            self._menu._menu.setDelegate_(self._menu_delegate)
        except Exception:
            pass

        # Contorch.app: Sparkle (only from /Applications), the setup
        # launcher's .command (rewritten so it points at this copy).
        if in_app():
            AppHelper.callAfter(self._start_updates)
            try:
                from . import setup_launcher
                setup_launcher.write()
            except Exception as e:  # noqa: BLE001
                _log(f"[setup] couldn't write the setup launcher: {e!r}")

        # Launch policy is Python's (lifecycle.on_launch): resume a stack a
        # quit or an update stopped, in every channel. Off the main thread:
        # starting the recorder can take a few seconds.
        def _launch():
            from . import lifecycle
            res: dict = {}
            try:
                res = lifecycle.on_launch()
                _log(f"[lifecycle] on_launch: {res}")
            except Exception as e:  # noqa: BLE001 — never keep the menu from starting
                _log(f"[lifecycle] on_launch failed: {e!r}")
            AppHelper.callAfter(self._refresh_callback, None)
            if res.get("needs_setup") and res.get("location") == "ok":
                AppHelper.callAfter(self._offer_setup)
        threading.Thread(target=_launch, name="on-launch", daemon=True).start()

    # ----- callbacks -----

    def _refresh_callback(self, _):
        from . import lifecycle
        key = lifecycle.watch_key()          # setup (in Terminal) wrote channel/modules/preferences
        if key != getattr(self, "_watch", None):
            self._watch = key
            stt.clear_cache()
        self._snap = st.collect()
        self._repaint()

    def _on_refresh_now(self, _):
        from . import ownerstate
        ownerstate.clear()
        self._refresh_callback(None)
        docs = ((self._snap.memory or {}).get("data") or {}).get("docs", "?")
        _notify("pipeline-monitor", "Refreshed", f"{docs} docs in the index")

    def _on_smoke_test(self, _):
        # rumps.notification silently no-ops when Python isn't running as a
        # signed .app bundle (which is our case under launchd). Use a modal
        # alert + osascript notification so the result is always visible,
        # and mirror to stderr so it lands in the launchd log.
        import sys
        from .diagnostics import smoke
        _notify("pipeline-monitor", "Running smoke test", "End-to-end memory check…")
        print("[smoke] starting…", file=sys.stderr, flush=True)
        result = smoke()
        print(f"[smoke] result: {result}", file=sys.stderr, flush=True)
        title = "Smoke test passed" if result["ok"] else "Smoke test FAILED"
        body = result["summary"]
        _notify("pipeline-monitor", title, body)
        # Modal so the user always sees the result even if Notification Center
        # is muted / Focus is on / app lacks notification permission.
        rumps.alert(title=title, message=body, ok="OK")
        self._refresh_callback(None)

    def _on_open_latest_transcript(self, _):
        sessions = (self._snap.recordings.get("sessions") if self._snap else None) or []
        if not sessions:
            rumps.notification("contorch", "No transcripts yet", "")
            return
        s = sessions[0]
        (_open_path_callback(s["path"]) if s.get("path")
         else _open_transcript_callback(s["meeting_id"]))(_)

    def _on_new_meeting(self, _):
        """`meeting-capture new`: speech from now on goes into a new
        transcript (back-to-back meetings otherwise share one until there
        are 15 minutes without speech)."""
        mc = _meeting_capture_bin()
        if not mc:
            rumps.notification("contorch", "meeting-capture not found",
                               "Install it: brew install contorch/tap/contorch")
            return
        res = subprocess.run([mc, "new"], capture_output=True, text=True, timeout=30)
        if res.returncode == 0:
            rumps.notification("contorch", "New meeting started",
                               "Speech from now on goes into a new transcript.")
        else:
            rumps.notification("contorch", "Couldn't start a new meeting",
                               (res.stderr or res.stdout).strip()[-200:])

    def _on_recording_settings(self, _):
        """`meeting-capture ui`: source (this Mac / USB interface), device,
        host/guest inputs with live level meters, pause. It reopens an
        already-running page, so repeated clicks are safe."""
        mc = _meeting_capture_bin()
        if not mc:
            rumps.notification("contorch", "meeting-capture not found",
                               "Install it: brew install contorch/tap/contorch")
            return
        subprocess.Popen([mc, "ui"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)

    def _on_open_co_dir(self, _):
        _open_dir_callback(st.CO_DIR)(_)

    def _on_open_mcp_log(self, _):
        log = self._snap and self._snap.mcp.get("log_file")
        if log:
            subprocess.Popen(["open", log])
        else:
            rumps.notification("pipeline-monitor", "No MCP log", "Server may not have run yet")

    def _on_import_transcripts(self, _):
        """Memory-only Macs: import a transcript bundle made on the recording
        Mac (`contorch-transcripts export`/`embed`). The file chooser is
        osascript's `choose file` (no Automation permission needed); the
        import is context-orchestrator's `contorch-transcripts import --json`."""
        from . import owners as ow

        def _run():
            pick = subprocess.run(["osascript", "-e", 'POSIX path of (choose file with prompt '
                                   '"Import transcripts (a bundle or a folder of .md files)")'],
                                  capture_output=True, text=True, timeout=600)
            path = pick.stdout.strip()
            if pick.returncode != 0 or not path:
                return
            res = ow.call("contorch-transcripts", "import", path, "--json", timeout=1800)
            d = res.get("data") or {}
            if res["status"] == "ok" and d.get("ok", True) and d.get("event", "result") == "result":
                how = " (keyword search: this Mac has no matching embedding key)" \
                    if d.get("embeddings") == "keyword_only" else ""
                _notify("contorch", "Transcripts imported",
                        f"{d.get('imported', 0)} imported, {d.get('skipped', 0)} already here{how}")
            else:
                msg = (d.get("error") or {}).get("message") if isinstance(d.get("error"), dict) else res.get("error")
                _notify("contorch", "Import failed", _truncate(str(msg or "unknown error"), 200))
            AppHelper.callAfter(self._refresh_callback, None)
        threading.Thread(target=_run, name="import-transcripts", daemon=True).start()

    def _on_quit(self, _):
        rumps.quit_application()

    # ----- Contorch.app: updates, login item, setup, move, uninstall, diagnostics -----

    def _start_updates(self):
        from . import updates
        self._updates = updates.Updates(on_change=lambda: self._refresh_callback(None), log=_log)
        res = self._updates.start()
        _log(f"[updates] {res}")

    def _offer_setup(self):
        """§5.1 step 5: offered once per launch; the menu row stays until setup
        has written the channel marker."""
        if getattr(self, "_setup_offered", False):
            return
        self._setup_offered = True
        i, _ = _alert("Set up Contorch?",
                      "Setup runs in Terminal: it asks what this Mac should do (record meetings, or memory only), "
                      "then connects Claude Code. You can run it again any time from the menu.",
                      ["Set Up…", "Later"])
        if i == 0:
            self._on_setup(None)

    def _on_setup(self, _):
        if not in_app():
            _copy_command_callback("contorch setup")(None)
            return
        from . import setup_launcher
        res = setup_launcher.launch()
        if not res["ok"]:
            _notify("Contorch", "Couldn't open setup in Terminal",
                    f"{res.get('error')} — or run: \"{setup_launcher.contorch_bin()}\" setup")

    def _on_move(self, _):
        from . import lifecycle
        res = lifecycle.move_to_applications()
        if not res["ok"]:
            _alert("Couldn't move Contorch to Applications", res["error"]["message"], ["OK"])
            return
        subprocess.Popen(["open", "-n", res["dest"]])
        self._quit_handover()

    def _quit_handover(self):
        _QUIT_REASON["value"] = "handover"
        rumps.quit_application()

    def _on_login_item(self, _):
        from . import loginitem
        st = loginitem.status(force=True)
        if st == "requires_approval":
            loginitem.open_settings()      # the user turned it off there; only they can turn it back on
            return
        res = loginitem.set_on(st != "enabled")
        if not res["ok"]:
            _notify("Contorch", "Open at Login didn't change", (res.get("error") or {}).get("message") or res["status"])
        self._refresh_callback(None)

    def _on_check_updates(self, _):
        if self._updates is not None:
            self._updates.check()

    def _on_install_now(self, _):
        i, _x = _alert("Install the update now?",
                       "Contorch can't tell whether a meeting is being recorded. Installing now stops the recorder; "
                       "it starts again when the new version opens.", ["Install Now", "Wait"])
        if i == 0 and self._updates is not None:
            self._updates.install_now()

    def _on_copy_diagnostics(self, _):
        """`contorch doctor --json --bundle`: versions, the owners' JSON and
        log tails, redacted (no keys, no transcript text)."""
        def _run():
            import json as _json
            from . import diagnostics
            try:
                doc = diagnostics.bundle()
                ok = copy_to_clipboard(_json.dumps(doc, indent=2, default=str))
                _notify("Contorch", "Diagnostics copied" if ok else "Couldn't copy diagnostics",
                        "Redacted: no keys, no transcript text. Paste it into your report.")
            except Exception as e:  # noqa: BLE001
                _notify("Contorch", "Couldn't collect diagnostics", str(e)[:200])
        threading.Thread(target=_run, name="diagnostics", daemon=True).start()

    def _on_open_logs(self, _):
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(LOGS_DIR)])

    def _on_report(self, _):
        webbrowser.open(report_url())

    def _run_verb(self, busy: str, argv: list[str], schema: str, done) -> None:
        """Run a `contorch` verb (JSON Lines → result) off the main thread."""
        self._busy = busy
        self._refresh_callback(None)

        def _run():
            res = owners.call("contorch", *argv, schema=schema, timeout=1800)
            AppHelper.callAfter(self._verb_done, res, done)
        threading.Thread(target=_run, name=f"contorch-{argv[0]}", daemon=True).start()

    def _verb_done(self, res: dict, done) -> None:
        self._busy = None
        d = res.get("data") or {}
        if res["status"] == "ok" and d.get("ok"):
            done(d)
            return
        err = d.get("error") if isinstance(d.get("error"), dict) else {"message": res.get("error")}
        _alert("Contorch couldn't finish", f"{err.get('message') or err.get('code') or 'unknown error'}\n\n"
               "Nothing more was changed. Choose it again to resume.", ["OK"])
        self._refresh_callback(None)

    def _on_uninstall(self, _):
        i, remove = _alert(
            "Uninstall Contorch?",
            "This stops the recorder, removes Contorch from Claude Code (MCP server, hook, skills) and from "
            "Login Items, and resets its privacy permissions. Your meetings and memory are kept unless you tick "
            "the box. Afterwards, drag Contorch to the Trash.",
            ["Uninstall", "Cancel"], checkbox="Also delete my meetings and memory")
        if i != 0:
            return
        argv = ["uninstall", "--yes", "--json"] + (["--remove-data"] if remove else [])

        def _done(d):
            from . import loginitem
            loginitem.unregister()
            root = owners.bundle_root()
            if root is not None:
                subprocess.Popen(["open", "-R", str(root)])       # show it, ready for the Trash
            todo = [t.get("message") or t.get("code") for t in d.get("todo") or []]
            if todo:
                _alert("Contorch is uninstalled", "Still to do:\n• " + "\n• ".join(todo), ["OK"])
            self._quit_handover()
        self._run_verb("Uninstalling Contorch…", argv, "contorch.uninstall", _done)

    def _on_rollback(self, _):
        i, _x = _alert("Go back to Homebrew?",
                       "Contorch goes back to the Homebrew install this app took over: the recorder, Claude Code's "
                       "entries and the menu bar are handed back to it. Your meetings and memory stay.",
                       ["Go Back to Homebrew", "Cancel"])
        if i != 0:
            return

        def _done(d):
            from . import loginitem
            loginitem.unregister()
            self._quit_handover()
        self._run_verb("Going back to Homebrew…", ["rollback", "--yes", "--json"], "contorch.rollback", _done)

    def on_reopen(self) -> None:
        """Opened again from Finder or Spotlight while running (§5.1 Reopen):
        the way back when the menu bar icon is hidden (notch, menu bar
        settings)."""
        snap = self._snap
        head = _build_status_line(snap).title if snap else "Contorch is running"
        buttons, actions = ["OK"], [None]
        if in_app():
            buttons.append("Set Up…")
            actions.append(self._on_setup)
            if has_verb("uninstall"):
                buttons.append("Uninstall…")
                actions.append(self._on_uninstall)
        buttons.append("Quit")
        actions.append(self._on_quit)
        i, _x = _alert("Contorch is running", f"{head}\n\nIts menu is the Contorch icon in the menu bar.", buttons)
        if 0 <= i < len(actions) and actions[i] is not None:
            actions[i](None)

    # ----- the Contorch.app part of the menu -----

    def _app_top_rows(self) -> list[rumps.MenuItem]:
        """Rows under the headline: where the app runs from, update state."""
        from . import lifecycle
        out = []
        if not in_app():
            return out
        if self._busy:
            out.append(rumps.MenuItem(self._busy))
        loc = lifecycle.location()
        if loc not in ("ok", "not_in_app"):
            why = {"translocated": "Contorch is running from the download",
                   "read_only": "Contorch is running from the disk image",
                   "outside_applications": "Contorch isn't in Applications"}.get(loc, loc)
            out.append(rumps.MenuItem(f"⚠ {why} — it can't start at login or update"))
            out.append(rumps.MenuItem("Move Contorch to Applications…", callback=self._on_move))
        u = self._updates.menu_state() if self._updates is not None else {}
        if u.get("waiting"):
            v = u.get("installing_version") or ""
            if u.get("reason") == "recording":
                out.append(rumps.MenuItem(f"Update {v} waits until the meeting is over"))
            elif u.get("reason") == "recording_unknown":
                out.append(rumps.MenuItem("Update waiting: can't tell whether a meeting is being recorded"))
                if u.get("override_offered"):
                    out.append(rumps.MenuItem("Install now (stops recording)…", callback=self._on_install_now))
            else:
                out.append(rumps.MenuItem(f"Installing update {v}…"))
        elif u.get("pending_version"):
            out.append(rumps.MenuItem(f"Update Available ({u['pending_version']})…", callback=self._on_check_updates))
        return out

    def _app_bottom_rows(self) -> list[rumps.MenuItem]:
        """Open at Login, Check for Updates…, Diagnostics, Uninstall."""
        out = []
        if in_app():
            from . import loginitem
            st = loginitem.status()
            if st != "unavailable":
                title = "Open at Login" + (" (off in System Settings — click to open it)"
                                           if st == "requires_approval" else "")
                item = rumps.MenuItem(title, callback=None if self._busy else self._on_login_item)
                item.state = 1 if st == "enabled" else 0
                out.append(item)
            if self._updates is not None and self._updates.started:
                out.append(rumps.MenuItem("Check for Updates…",
                                          callback=self._on_check_updates if self._updates.can_check() else None))
        diag = rumps.MenuItem("Diagnostics")
        diag.add(rumps.MenuItem("Copy diagnostics", callback=self._on_copy_diagnostics))
        diag.add(rumps.MenuItem("Open logs folder", callback=self._on_open_logs))
        diag.add(rumps.MenuItem("Report a problem…", callback=self._on_report))
        if in_app() and has_verb("adopt") and (chan.read() or {}).get("adopted_from"):
            diag.add(rumps.separator)
            diag.add(rumps.MenuItem("Go back to Homebrew…", callback=None if self._busy else self._on_rollback))
        out.append(diag)
        if in_app() and has_verb("uninstall"):
            out.append(rumps.MenuItem("Uninstall Contorch…", callback=None if self._busy else self._on_uninstall))
        return out

    # ----- pulse animation (recording state only) -----

    def _start_pulse(self):
        if self._is_pulsing or not self._has_icons:
            return
        self._is_pulsing = True
        self._pulse_phase = 0
        self.pulse_timer.start()

    def _stop_pulse(self):
        if not self._is_pulsing:
            return
        self._is_pulsing = False
        self.pulse_timer.stop()
        # Reset to the base template glyph.
        if self._has_icons:
            self.icon = str(GLYPH_TEMPLATE)

    def _pulse_tick(self, _):
        if not self._has_icons:
            return
        self._pulse_phase = 1 - self._pulse_phase
        self.icon = str(GLYPH_TEMPLATE_PULSE if self._pulse_phase else GLYPH_TEMPLATE)

    # ----- repaint -----

    def _repaint(self):
        snap = self._snap
        if not snap:
            return

        overall = snap.overall()
        if overall == "rec":
            # Start the pulse animation; title carries the REC text so
            # users can confirm at a glance even if the pulse is subtle.
            self._start_pulse()
            self.title = " REC" if self._has_icons else ICON_FALLBACK_RECORDING
        elif overall == "rec_stale":
            # Daemon claims REC but no chunks landing — show ⚠ REC, no pulse,
            # so the user notices something's wrong mid-meeting before they
            # lose 50 minutes of audio (the Cluely/SCK-conflict failure mode).
            self._stop_pulse()
            self.title = " ⚠ REC" if self._has_icons else "⚠ REC"
        elif overall == "perm":
            # sysaudio refused Screen Recording — nothing is being captured
            # even though the daemon is up. Name the problem in the bar.
            self._stop_pulse()
            self.title = " ⚠ PERM" if self._has_icons else ICON_FALLBACK_PERM
        elif overall == "err":
            self._stop_pulse()
            self.title = " ⚠" if self._has_icons else ICON_FALLBACK_ERR
        else:
            self._stop_pulse()
            self.title = "" if self._has_icons else ICON_FALLBACK_OK

        # Tear down + rebuild menu — simpler than diffing.
        # Layout: status line, separator, index summary, separator, MCP +
        # hook lines, separator, daemon summary + 3 submenus, separator,
        # actions. Headers are intentionally absent — separators do the
        # grouping work without consuming a row each.
        _prune_menu_refs(self.menu)  # release prior items from rumps' global registry
        self.menu.clear()

        self.menu.add(_build_status_line(snap))
        recorder = snap.recorder_on()
        if recorder:
            for line in _permission_lines(snap):
                self.menu.add(line)
            tline = _build_transcription_line(snap)
            if tline is not None:
                self.menu.add(tline)
        for line in (_attention_lines(snap) + _module_rows(snap, self._on_setup if in_app() else None)
                     + self._app_top_rows()):
            self.menu.add(line)
        self.menu.add(rumps.separator)

        self.menu.add(_build_index_line(snap))
        self.menu.add(rumps.separator)

        for line in _build_mcp_lines(snap):
            self.menu.add(line)
        self.menu.add(_build_tool_calls_submenu(snap))
        self.menu.add(rumps.separator)

        self.menu.add(_build_system_line(snap))
        self.menu.add(_build_recent_submenu(snap))
        self.menu.add(_build_details_submenu(snap))
        self.menu.add(rumps.separator)

        # Actions — primary actions visible, secondary ones grouped into
        # an "Open" submenu so the bottom of the menu doesn't sprawl.
        self.menu.add(rumps.MenuItem("Refresh", callback=self._on_refresh_now))
        self.menu.add(rumps.MenuItem("Run smoke test", callback=self._on_smoke_test))
        if recorder:     # the recorder's own actions only when this Mac records
            self.menu.add(rumps.MenuItem("Start new meeting", callback=self._on_new_meeting))
            self.menu.add(rumps.MenuItem("Recording settings…", callback=self._on_recording_settings))
            self.menu.add(self._build_mode_toggle(snap))
        else:
            self.menu.add(rumps.MenuItem("Import transcripts…", callback=self._on_import_transcripts))
        open_submenu = rumps.MenuItem("Open")
        open_submenu.add(rumps.MenuItem("Latest transcript", callback=self._on_open_latest_transcript))
        open_submenu.add(rumps.MenuItem("~/.context-orchestrator", callback=self._on_open_co_dir))
        open_submenu.add(rumps.MenuItem("MCP log", callback=self._on_open_mcp_log))
        self.menu.add(open_submenu)
        self.menu.add(self._build_stack_toggle())
        keep = self._build_keep_recording(snap)
        if keep is not None:
            self.menu.add(keep)
        self.menu.add(rumps.separator)
        for row in self._app_bottom_rows():
            self.menu.add(row)
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Quit", callback=self._on_quit))

    # ----- capture mode toggle -----
    #
    # One item that flips between "Switch to live mode" and "Switch to batch
    # mode". It is a thin wrapper over `meeting-capture mode <target>`, which
    # owns the real work (edit MEETING_CAPTURE_MODE in the launchd plist and
    # relaunch the agent) so the CLI and the menu can never disagree. The
    # relaunch cuts any session in progress, so the item is inert while a
    # recording is live — switch before the call, not during it.

    def _build_mode_toggle(self, snap: st.Snapshot) -> rumps.MenuItem:
        cm = snap.capture_mode or {}
        if not cm.get("ok"):
            return rumps.MenuItem("Capture mode: agent not installed")
        target = "batch" if cm["mode"] == "live" else "live"
        if self._mode_switching:
            return rumps.MenuItem(f"Switching to {self._mode_switching} mode…")
        if snap.recording.get("recording"):
            return rumps.MenuItem(f"Switch to {target} mode (stop recording first)")
        item = rumps.MenuItem(f"Switch to {target} mode")
        item.set_callback(lambda _, t=target: self._on_switch_mode(t))
        return item

    def _on_switch_mode(self, target: str):
        binary = _meeting_capture_bin()
        if binary is None:
            _notify("pipeline-monitor", "meeting-capture not found", "Install the CLI (brew or pip) first.")
            return
        self._mode_switching = target
        self._refresh_callback(None)

        def _run():
            try:
                res = subprocess.run([binary, "mode", target], capture_output=True, text=True, timeout=45)
                if res.returncode == 0:
                    _notify("pipeline-monitor", f"Capture mode: {target}",
                            "Daemon relaunched. Next meeting streams live." if target == "live"
                            else "Daemon relaunched. Back to chunked transcription.")
                else:
                    _notify("pipeline-monitor", "Mode switch failed",
                            (res.stderr or res.stdout or "unknown error").strip()[-200:])
            except Exception as e:  # noqa: BLE001 — surface anything to the user
                _notify("pipeline-monitor", "Mode switch failed", str(e))
            finally:
                self._mode_switching = None
                # Back on the main thread for the AppKit menu rebuild.
                AppHelper.callAfter(self._refresh_callback, None)

        threading.Thread(target=_run, name="capture-mode-switch", daemon=True).start()


    # ----- stop / resume everything -----
    #
    # Same code path as `contorch stop` / `contorch resume` (contorch.py), so
    # the menu and the CLI cannot disagree. The menu-bar app itself keeps
    # running — it is where you resume from.

    def _build_stack_toggle(self) -> rumps.MenuItem:
        if self._stack_busy:
            return rumps.MenuItem(f"{self._stack_busy} contorch…")
        if ct.is_stopped():
            return rumps.MenuItem("Resume everything", callback=lambda _: self._on_stack("resume"))
        return rumps.MenuItem("Stop everything", callback=lambda _: self._on_stack("stop"))

    def _build_keep_recording(self, snap: st.Snapshot) -> rumps.MenuItem | None:
        """"Keep recording after Quit" (lifecycle preference); hidden on a
        Mac that doesn't record."""
        from . import lifecycle
        if not (snap.capture_mode or {}).get("installed"):
            return None
        item = rumps.MenuItem("Keep recording after Quit", callback=self._on_keep_recording)
        item.state = 1 if lifecycle.keep_recording_after_quit() else 0
        return item

    def _on_keep_recording(self, sender):
        from . import lifecycle
        lifecycle.set_keep_recording_after_quit(not lifecycle.keep_recording_after_quit())
        self._refresh_callback(None)

    def _on_stack(self, action: str):
        self._stack_busy = "Stopping" if action == "stop" else "Resuming"
        self._refresh_callback(None)

        def _run():
            lines: list[str] = []
            try:
                ok = (ct.stop if action == "stop" else ct.resume)(log=lines.append)
                if action == "stop":
                    _notify("contorch", "Stopped" if ok else "Stopped with errors",
                            "Nothing records or indexes until you choose Resume everything."
                            if ok else "\n".join(lines)[-200:])
                else:
                    _notify("contorch", "Running" if ok else "Resumed with errors",
                            "Capture, indexing and search are back." if ok else "\n".join(lines)[-200:])
            except Exception as e:  # noqa: BLE001 — surface anything to the user
                _notify("contorch", f"{action.capitalize()} failed", str(e))
            finally:
                self._stack_busy = None
                AppHelper.callAfter(self._refresh_callback, None)

        threading.Thread(target=_run, name=f"contorch-{action}", daemon=True).start()


# ============================================================ quit (lifecycle.on_quit)
#
# Every way out goes through one Python decision: the Quit item and a quit
# Apple Event reach rumps' before_quit; SIGTERM (brew services stop/restart,
# brew upgrade, launchctl bootout) doesn't, so its handler routes it to the
# same quit. A quit Apple Event carrying kAEQuitReason ('why?') is a logout,
# restart or shutdown: launchd stops the agents then, nothing to do.

_QUIT_REASON = {"value": "user"}
_KEEP: dict = {}
K_AE_QUIT_REASON = 0x7768793F          # 'why?'


def quit_reason() -> str:
    reason = _QUIT_REASON["value"]
    try:
        from Foundation import NSAppleEventManager
        ev = NSAppleEventManager.sharedAppleEventManager().currentAppleEvent()
        if ev is not None and ev.attributeDescriptorForKeyword_(K_AE_QUIT_REASON) is not None:
            return "logout"
    except Exception:
        pass
    return reason


def _before_quit() -> None:
    from . import lifecycle
    try:
        res = lifecycle.on_quit(quit_reason())
        print(f"[lifecycle] on_quit: {res}", file=__import__("sys").stderr, flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[lifecycle] on_quit failed: {e!r}", file=__import__("sys").stderr, flush=True)


def install_sigterm() -> None:
    """SIGTERM → the normal quit path. Python runs a signal handler on the
    main thread the next time it executes bytecode; a 0.25 s NSTimer keeps
    that prompt while the run loop idles (PROVEN within 0.25 s)."""
    import signal
    from Foundation import NSTimer

    def _handler(signum, frame):
        _QUIT_REASON["value"] = "signal"
        rumps.quit_application()
    signal.signal(signal.SIGTERM, _handler)

    class _Tick(NSObject):
        def tick_(self, t):
            pass
    _KEEP["tick"] = _Tick.alloc().init()
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.25, _KEEP["tick"], b"tick:", None,
                                                                             True)


_REOPEN: dict = {}


def install_reopen(app: "PipelineMonitor") -> None:
    """applicationShouldHandleReopen:hasVisibleWindows: on rumps' application
    delegate: opening Contorch again from Finder or Spotlight shows
    app.on_reopen(). Added to rumps' delegate class before run() sets the
    delegate (rumps has no hook for it)."""
    _REOPEN["app"] = app

    def _reopen(self, nsapp, has_visible_windows):
        target = _REOPEN.get("app")
        if target is not None:
            AppHelper.callAfter(target.on_reopen)
        return False

    import objc
    sel = objc.selector(_reopen, selector=b"applicationShouldHandleReopen:hasVisibleWindows:", signature=b"Z@:@Z")
    if not rumps.rumps.NSApp.instancesRespondToSelector_(b"applicationShouldHandleReopen:hasVisibleWindows:"):
        objc.classAddMethods(rumps.rumps.NSApp, [sel])


def main():
    rumps.events.before_quit.register(_before_quit)
    install_sigterm()
    app = PipelineMonitor()
    install_reopen(app)
    app.run()


if __name__ == "__main__":
    main()
