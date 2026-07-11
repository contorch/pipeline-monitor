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
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

import rumps
from AppKit import NSObject
from PyObjCTools import AppHelper  # noqa: F401  (ensures AppKit init order)

from . import status as st
from .smoketest import run_smoke_test

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


def _notify(app: str, title: str, body: str) -> None:
    """Best-effort macOS notification via osascript. Works without a signed
    .app bundle (unlike rumps.notification, which silently no-ops in that
    case). Failures are swallowed — notifications are optional UX."""
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'display notification "{_esc(body)}" '
        f'with title "{_esc(app)}" subtitle "{_esc(title)}"'
    )
    try:
        subprocess.run(["osascript", "-e", script], timeout=3, check=False)
    except Exception:
        pass

# Icon assets live alongside the package, two levels up from app.py
# (repo_root/assets/) so the build-menubar-icons script can regenerate
# them without touching the package itself.
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
GLYPH_TEMPLATE = ASSETS_DIR / "glyph-template.png"
GLYPH_TEMPLATE_PULSE = ASSETS_DIR / "glyph-template-pulse.png"
GLYPH_REC = ASSETS_DIR / "glyph-rec.png"

# Fallback title text used only if the asset PNGs are missing (e.g. a
# user installed from an older release before icons existed).
ICON_FALLBACK_RECORDING = "● REC"
ICON_FALLBACK_OK = "○"
ICON_FALLBACK_ERR = "⚠"


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

def _build_status_line(snap: st.Snapshot) -> rumps.MenuItem:
    """Top of menu — recording state. Visible at a glance."""
    rec = snap.recording
    if not rec.get("ok"):
        return rumps.MenuItem(f"⚠ {_truncate(rec.get('error', 'unknown'), 50)}")
    if rec.get("recording"):
        f = rec.get("current_file")
        if rec.get("stale"):
            age_s = rec.get("last_chunk_age_s", 0)
            mins = max(1, age_s // 60)
            note = f"silent for {mins}m — another app likely capturing audio (Cluely / Loom / OBS)"
            if f:
                return rumps.MenuItem(f"⚠ Recording — {Path(f).name} · {note}")
            return rumps.MenuItem(f"⚠ Recording — {note}")
        if f:
            return rumps.MenuItem(f"● Recording — {Path(f).name}")
        return rumps.MenuItem("● Recording")
    return rumps.MenuItem("○ Idle")


def _build_index_line(snap: st.Snapshot) -> rumps.MenuItem:
    """One line summarising the chroma + sqlite state."""
    c = snap.chroma
    d = snap.db
    if not c.get("ok"):
        return rumps.MenuItem(f"⚠ Index: {_truncate(c.get('error', 'unreachable'), 50)}")
    parts = [f"{c.get('doc_count', '?')} docs"]
    if d.get("ok"):
        parts.append(f"{d.get('repo_knowledge', 0)} insights")
        if d.get("last_repo_knowledge"):
            parts.append(f"last {_ago(d['last_repo_knowledge'])}")
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
    """One line: are the daemons up?"""
    l = snap.launchd
    if not l.get("ok"):
        return rumps.MenuItem(f"⚠ launchctl: {_truncate(l.get('error', '?'), 50)}")
    daemons = l.get("daemons", {})
    installed = [info for info in daemons.values() if info.get("installed")]
    running = [info for info in installed if info.get("running")]
    if len(running) == len(installed) and installed:
        return rumps.MenuItem(f"Daemons: {len(running)}/{len(installed)} up")
    down = len(installed) - len(running)
    return rumps.MenuItem(f"⚠ Daemons: {down} down ({len(running)}/{len(installed)} up)")


def _build_recent_submenu(snap: st.Snapshot) -> rumps.MenuItem:
    """Submenu listing recent sessions. Click any to open."""
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
        title = f"{sess['name']} · {size_kb:.0f}KB · {_ago(sess['age_s'])}"
        submenu.add(rumps.MenuItem(_truncate(title, 70),
                                   callback=_open_path_callback(sess["path"])))
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

    # Chroma dim
    c = snap.chroma
    if c.get("ok"):
        submenu.add(rumps.MenuItem(f"Chroma: {c.get('doc_count')} docs @ {c.get('dim')}d"))

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


def _open_dir_callback(path: Path):
    def _cb(_):
        if path.is_dir():
            subprocess.Popen(["open", str(path)])
        else:
            rumps.notification("pipeline-monitor", "Not found", str(path))
    return _cb


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

    # ----- callbacks -----

    def _refresh_callback(self, _):
        self._snap = st.collect()
        self._repaint()

    def _on_refresh_now(self, _):
        self._refresh_callback(None)
        rumps.notification("pipeline-monitor", "Refreshed", f"chroma={self._snap.chroma.get('doc_count','?')} docs")

    def _on_smoke_test(self, _):
        # rumps.notification silently no-ops when Python isn't running as a
        # signed .app bundle (which is our case under launchd). Use a modal
        # alert + osascript notification so the result is always visible,
        # and mirror to stderr so it lands in the launchd log.
        import sys
        _notify("pipeline-monitor", "Running smoke test", "End-to-end pipeline check…")
        print("[smoke] starting…", file=sys.stderr, flush=True)
        result = run_smoke_test()
        print(f"[smoke] result: {result}", file=sys.stderr, flush=True)
        if result["ok"]:
            title = "Smoke test passed"
            body = f"{result['duration_ms']}ms · {result['summary']}"
        else:
            title = "Smoke test FAILED"
            body = f"stage={result.get('stage','?')} · {result.get('error','unknown')[:200]}"
        _notify("pipeline-monitor", title, body)
        # Modal so the user always sees the result even if Notification Center
        # is muted / Focus is on / app lacks notification permission.
        rumps.alert(title=title, message=body, ok="OK")
        self._refresh_callback(None)

    def _on_open_transcripts(self, _):
        _open_dir_callback(st.TRANSCRIPTS_DIR)(_)

    def _on_open_co_dir(self, _):
        _open_dir_callback(st.CO_DIR)(_)

    def _on_open_mcp_log(self, _):
        log = self._snap and self._snap.mcp.get("log_file")
        if log:
            subprocess.Popen(["open", log])
        else:
            rumps.notification("pipeline-monitor", "No MCP log", "Server may not have run yet")

    def _on_restart_chroma(self, _):
        # com.stirredo.* → com.contorch.* rebrand: use whichever plist exists.
        for org in ("contorch", "stirredo"):
            plist = Path.home() / f"Library/LaunchAgents/com.{org}.context-orchestrator-chroma.plist"
            if plist.exists():
                break
        else:
            rumps.notification("pipeline-monitor", "Plist missing", str(plist))
            return
        try:
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, timeout=3)
            subprocess.run(["launchctl", "load", str(plist)], capture_output=True, timeout=3)
            rumps.notification("pipeline-monitor", "Chroma daemon restarted", "Reload triggered")
        except Exception as e:
            rumps.notification("pipeline-monitor", "Restart failed", str(e))
        self._refresh_callback(None)

    def _on_quit(self, _):
        rumps.quit_application()

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
        open_submenu = rumps.MenuItem("Open")
        open_submenu.add(rumps.MenuItem("Transcripts folder", callback=self._on_open_transcripts))
        open_submenu.add(rumps.MenuItem("~/.context-orchestrator", callback=self._on_open_co_dir))
        open_submenu.add(rumps.MenuItem("MCP log", callback=self._on_open_mcp_log))
        self.menu.add(open_submenu)
        self.menu.add(rumps.MenuItem("Restart chroma", callback=self._on_restart_chroma))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Quit", callback=self._on_quit))


def main():
    PipelineMonitor().run()


if __name__ == "__main__":
    main()
