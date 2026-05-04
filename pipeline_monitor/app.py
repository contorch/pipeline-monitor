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

from . import status as st
from .smoketest import run_smoke_test

REFRESH_INTERVAL_S = 5

ICON_RECORDING = "● REC"
ICON_OK = "○"
ICON_ERR = "⚠"


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

def _build_now_section(snap: st.Snapshot) -> list[rumps.MenuItem]:
    items = []
    rec = snap.recording
    if not rec.get("ok"):
        items.append(rumps.MenuItem(f"Recording: ? ({_truncate(rec.get('error', 'unknown'), 40)})"))
    elif rec.get("recording"):
        items.append(rumps.MenuItem("● RECORDING"))
        if rec.get("current_file"):
            items.append(rumps.MenuItem(f"  → {Path(rec['current_file']).name}"))
    else:
        items.append(rumps.MenuItem("○ idle"))
    return items


def _build_recent_section(snap: st.Snapshot) -> list[rumps.MenuItem]:
    items = []
    r = snap.recordings
    if not r.get("ok"):
        items.append(rumps.MenuItem(f"  (no transcripts dir)"))
        return items
    sessions = r.get("sessions", [])
    if not sessions:
        items.append(rumps.MenuItem("  (no recordings yet)"))
        return items
    items.append(rumps.MenuItem(f"  {r['total_count']} total · {len(sessions)} shown"))
    for sess in sessions[:10]:
        size_kb = sess["size"] / 1024
        title = f"  {sess['name']} ({size_kb:.0f}KB · {_ago(sess['age_s'])})"
        mi = rumps.MenuItem(_truncate(title, 70), callback=_open_path_callback(sess["path"]))
        items.append(mi)
    return items


def _build_index_section(snap: st.Snapshot) -> list[rumps.MenuItem]:
    items = []
    c = snap.chroma
    d = snap.db
    if c.get("ok"):
        items.append(rumps.MenuItem(
            f"  Chroma: {c.get('doc_count', '?')} docs @ {c.get('dim', '?')}d"
        ))
    else:
        items.append(rumps.MenuItem(f"  ⚠ Chroma: {_truncate(c.get('error', 'unreachable'), 40)}"))
    if d.get("ok"):
        items.append(rumps.MenuItem(
            f"  SQLite: {d.get('tasks', 0)} tasks · {d.get('sources', 0)} sources · {d.get('repo_knowledge', 0)} insights"
        ))
        if d.get("last_repo_knowledge"):
            items.append(rumps.MenuItem(f"  Last insight: {_ago(d['last_repo_knowledge'])}"))
    else:
        items.append(rumps.MenuItem(f"  ⚠ SQLite: {_truncate(d.get('error', '?'), 40)}"))
    return items


def _build_mcp_section(snap: st.Snapshot) -> list[rumps.MenuItem]:
    items = []
    m = snap.mcp
    if not m.get("ok"):
        items.append(rumps.MenuItem(f"  ⚠ MCP log: {_truncate(m.get('error', '?'), 40)}"))
        return items
    # "Active" if log was touched in last 10 min; else "idle" (not "stale" —
    # MCP servers don't write to log unless tools are being called, so silence
    # just means Claude Code isn't actively asking it for anything).
    log_age = m.get("log_age_s", 9999)
    activity = "active" if log_age < 600 else "idle"
    items.append(rumps.MenuItem(f"  MCP server: {activity} · last log {_ago(log_age)}"))
    last = m.get("last_call")
    if last:
        result_emoji = {"ok": "✓", "fail": "✗", "pending": "…"}.get(last.get("result"), "?")
        lat = f" {last['latency_ms']}ms" if last.get("latency_ms") else ""
        items.append(rumps.MenuItem(
            f"  Last call: {result_emoji} {last['tool']}{lat} · {_ago(last.get('ts'))}"
        ))
    else:
        items.append(rumps.MenuItem("  Last call: (none yet)"))
    # Only surface the most-recent error if it was actually the last call
    # (i.e. the failure is current). If a later call succeeded, the error
    # is historical and we don't want to alarm anyone.
    if m.get("last_error") and last and last.get("result") == "fail":
        items.append(rumps.MenuItem(f"  ⚠ Last error: {_truncate(m['last_error'], 60)}"))

    # Auto-context hook
    h = snap.hook
    if h.get("ok"):
        lat = f" {h['latency_ms']}ms" if h.get("latency_ms") else ""
        chars = f" · {h['injected_chars']}c" if h.get("injected_chars") else ""
        items.append(rumps.MenuItem(
            f"  Auto-context hook: {_ago(h.get('age_s'))}{lat}{chars}"
        ))
    else:
        items.append(rumps.MenuItem(f"  Auto-context hook: {_truncate(h.get('error', '?'), 50)}"))

    # Tool-call timeline submenu
    timeline = rumps.MenuItem("  → Tool-call timeline (last 20)")
    for c in reversed(m.get("recent_calls", [])):
        emoji = {"ok": "✓", "fail": "✗", "pending": "…"}.get(c.get("result"), "?")
        lat = f" {c['latency_ms']}ms" if c.get("latency_ms") else ""
        timeline.add(rumps.MenuItem(f"  {emoji} {c['tool']}{lat} · {_ago(c.get('ts'))}"))
    items.append(timeline)

    return items


def _build_system_section(snap: st.Snapshot) -> list[rumps.MenuItem]:
    items = []
    l = snap.launchd
    if l.get("ok"):
        for label, info in l.get("daemons", {}).items():
            short = label.split(".")[-1]
            if not info.get("installed"):
                items.append(rumps.MenuItem(f"  - {short}: not installed"))
            elif info.get("running"):
                items.append(rumps.MenuItem(f"  ✓ {short}: pid {info['pid']}"))
            else:
                items.append(rumps.MenuItem(f"  ✗ {short}: stopped (exit {info.get('status')})"))
    else:
        items.append(rumps.MenuItem(f"  ⚠ launchctl: {_truncate(l.get('error', '?'), 40)}"))
    d = snap.disk
    if d.get("ok"):
        bits = " · ".join(f"{k}={v}" for k, v in d.get("dirs", {}).items())
        items.append(rumps.MenuItem(f"  Disk: {bits}"))
    return items


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


# ============================================================ app

class PipelineMonitor(rumps.App):
    def __init__(self):
        super().__init__("pipeline-monitor", title=ICON_OK, quit_button=None)
        self._snap: Optional[st.Snapshot] = None
        self.refresh_timer = rumps.Timer(self._refresh_callback, REFRESH_INTERVAL_S)
        self.refresh_timer.start()
        self._refresh_callback(None)  # initial sync paint

    # ----- callbacks -----

    def _refresh_callback(self, _):
        self._snap = st.collect()
        self._repaint()

    def _on_refresh_now(self, _):
        self._refresh_callback(None)
        rumps.notification("pipeline-monitor", "Refreshed", f"chroma={self._snap.chroma.get('doc_count','?')} docs")

    def _on_smoke_test(self, _):
        rumps.notification("pipeline-monitor", "Running smoke test", "End-to-end pipeline check…")
        result = run_smoke_test()
        if result["ok"]:
            rumps.notification("pipeline-monitor", "✓ Smoke test passed",
                               f"{result['duration_ms']}ms · {result['summary']}")
        else:
            rumps.notification("pipeline-monitor", "✗ Smoke test failed",
                               result.get("error", "unknown error"))
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
        plist = Path.home() / "Library/LaunchAgents/com.stirredo.context-orchestrator-chroma.plist"
        if not plist.exists():
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

    # ----- repaint -----

    def _repaint(self):
        snap = self._snap
        if not snap:
            return

        overall = snap.overall()
        if overall == "rec":
            self.title = ICON_RECORDING
        elif overall == "err":
            self.title = ICON_ERR
        else:
            self.title = ICON_OK

        # Tear down + rebuild menu — simpler than diffing
        self.menu.clear()
        self.menu.add(rumps.MenuItem(f"Pipeline Monitor · {datetime.now().strftime('%H:%M:%S')}"))
        self.menu.add(rumps.separator)

        self.menu.add(rumps.MenuItem("NOW"))
        for it in _build_now_section(snap):
            self.menu.add(it)
        self.menu.add(rumps.separator)

        self.menu.add(rumps.MenuItem("RECENT SESSIONS"))
        for it in _build_recent_section(snap):
            self.menu.add(it)
        self.menu.add(rumps.separator)

        self.menu.add(rumps.MenuItem("INDEX HEALTH"))
        for it in _build_index_section(snap):
            self.menu.add(it)
        self.menu.add(rumps.separator)

        self.menu.add(rumps.MenuItem("MCP / CONNECTIONS"))
        for it in _build_mcp_section(snap):
            self.menu.add(it)
        self.menu.add(rumps.separator)

        self.menu.add(rumps.MenuItem("SYSTEM HEALTH"))
        for it in _build_system_section(snap):
            self.menu.add(it)
        self.menu.add(rumps.separator)

        self.menu.add(rumps.MenuItem("Refresh now", callback=self._on_refresh_now))
        self.menu.add(rumps.MenuItem("Run end-to-end smoke test", callback=self._on_smoke_test))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Open transcripts folder", callback=self._on_open_transcripts))
        self.menu.add(rumps.MenuItem("Open ~/.context-orchestrator", callback=self._on_open_co_dir))
        self.menu.add(rumps.MenuItem("Open MCP log", callback=self._on_open_mcp_log))
        self.menu.add(rumps.MenuItem("Restart chroma daemon", callback=self._on_restart_chroma))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Quit", callback=self._on_quit))


def main():
    PipelineMonitor().run()


if __name__ == "__main__":
    main()
