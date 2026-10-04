"""meeting-capture's settings, as meeting-capture reports them.

meeting-capture owns its configuration (~/.meeting-capture/env from 0.8; the
launchd plist's EnvironmentVariables before that). pipeline-monitor never
parses it: it runs `meeting-capture config --json` (meeting-capture.config/1)
and caches the answer until a file named in its `watch_paths` changes (a stat,
never a read).

Compatibility: meeting-capture 0.7 (the release on Homebrew today) has no
`config --json` and keeps its settings in the plist, so legacy_plist_env()
reads them there. Delete the fallback once the tap no longer ships 0.7.
"""
from __future__ import annotations

import os
import plistlib
import threading
import time
from pathlib import Path
from typing import Any

from . import owners

LEGACY_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.contorch.meeting-capture.plist"
SCHEMA = "meeting-capture.config/"
TIMEOUT_S = 15
NEGATIVE_TTL_S = 60.0          # an old or missing meeting-capture is asked again after a minute

_lock = threading.Lock()
_cache: dict[str, Any] = {"key": None, "doc": None, "none_until": 0.0}


def _stamp(paths: list[str]) -> tuple:
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((p, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((p, None, None))
    return tuple(out)


def clear_cache() -> None:
    with _lock:
        _cache.update(key=None, doc=None, none_until=0.0)


def doc(force: bool = False, wait: bool = True) -> dict | None:
    """`meeting-capture config --json`, or None when meeting-capture is
    missing or too old to have it (then use the legacy fallbacks).
    wait=False (the menu bar's timer) never runs it in the foreground: a
    stale answer is refreshed on a background thread and the last one (or
    None) is returned meanwhile."""
    with _lock:
        cached = _cache["doc"]
        if cached and not force and _cache["key"] == _stamp(cached.get("watch_paths") or []):
            return cached
        if not cached and not force and time.monotonic() < _cache["none_until"]:
            return None
        if not wait:
            start = not _cache.get("inflight")
            _cache["inflight"] = True
    if not wait:
        if start:
            def _bg() -> None:
                try:
                    _refresh()
                finally:
                    with _lock:
                        _cache["inflight"] = False
            threading.Thread(target=_bg, name="mc-config", daemon=True).start()
        return cached
    return _refresh()


def _refresh() -> dict | None:
    res = owners.call("meeting-capture", "config", "--json", schema=SCHEMA, timeout=TIMEOUT_S,
                      foreground=False)
    fresh = res["data"] if res["status"] == "ok" else None
    with _lock:
        if fresh:
            _cache.update(key=_stamp(fresh.get("watch_paths") or []), doc=fresh)
        else:
            _cache.update(key=None, doc=None, none_until=time.monotonic() + NEGATIVE_TTL_S)
    return fresh


def known() -> bool:
    """Has meeting-capture answered yet (a document, or "too old")? Until
    then a non-waiting caller should say "checking", not "not installed"."""
    with _lock:
        return _cache["doc"] is not None or time.monotonic() < _cache["none_until"]


def legacy_plist_env(plist: Path | None = None) -> dict:
    try:
        env = plistlib.loads(Path(plist or LEGACY_PLIST).read_bytes()).get("EnvironmentVariables") or {}
        return {str(k): str(v) for k, v in env.items()}
    except Exception:
        return {}


def setting(name: str, default: str | None = None, wait: bool = True) -> str | None:
    """One setting by its short name (`mode`, `source`, `stt`, …)."""
    d = doc(wait=wait)
    if d is None:
        return legacy_plist_env().get(f"MEETING_CAPTURE_{name.upper()}", default)
    row = (d.get("settings") or {}).get(name.lower()) or {}
    v = row.get("value")
    return default if v is None else str(v)


def installed(wait: bool = True) -> bool:
    """Is the recorder agent installed (either backend)?"""
    d = doc(wait=wait)
    if d is None:
        return LEGACY_PLIST.is_file()
    return bool((d.get("agent") or {}).get("installed"))


def agent() -> dict:
    """{backend, installed, sysaudio, plist} as meeting-capture reports it
    (legacy: the plist's own pin)."""
    d = doc()
    if d is None:
        return {"backend": "launchctl" if LEGACY_PLIST.is_file() else "none",
                "installed": LEGACY_PLIST.is_file(),
                "sysaudio": legacy_plist_env().get("MEETING_CAPTURE_SYSAUDIO") or None,
                "plist": str(LEGACY_PLIST) if LEGACY_PLIST.is_file() else None, "legacy": True}
    return {**(d.get("agent") or {}), "legacy": False}
