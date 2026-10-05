"""The owners' answers the menu bar, `contorch status` and `doctor` show,
read in the background and cached (the 5-second menu timer never waits on a
subprocess):

    recorder()     `meeting-capture status --json`   is a meeting being recorded?
                   (meeting-capture.status/1: recording true | false | null)
    permissions()  `meeting-capture check --json`    screen & system audio, microphone
                   (meeting-capture.permissions/1: rows with hint + settings_url)
    memory()       `contorch-memory status --json`   the index: embeddings,
                   in-process / server / none, docs, transcripts, compatibility
    claude()       `contorch-memory claude status --json`   MCP, hook, CLAUDE.md, skill

Each returns {"status": ok|old|error|missing|not_built|checking, "data",
"error"}. "old" = the owner predates the verb (meeting-capture 0.7 has
neither status nor check --json; context-orchestrator 0.4 no status --json):
callers fall back and never claim more than they know.

Reads run the owners' own venv executables, never a Homebrew wrapper
(owners.background). A read is repeated after its TTL, or sooner when a file
it depends on changes (`key`): the recorder's state.json, the daemon log's
"declined TCCs" line count for permissions.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import owners

def _mc_dir() -> Path:
    return Path.home() / ".meeting-capture"


class Cached:
    def __init__(self, name: str, args: tuple[str, ...], schema: str, ttl: float, retry: float,
                 key: Callable[[], Any] = lambda: None, timeout: float = 60):
        self.name, self.args, self.schema = name, args, schema
        self.ttl, self.retry, self.key, self.timeout = ttl, retry, key, timeout
        self._lock = threading.Lock()
        self._entry: tuple[float, Any, dict] | None = None      # (at, key, result)
        self._inflight = False

    def clear(self) -> None:
        with self._lock:
            self._entry = None

    def _read(self) -> dict:
        try:
            res = owners.call(self.name, *self.args, schema=self.schema, foreground=False, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001 — a status line must still render
            res = {"status": "error", "data": None, "error": f"{type(e).__name__}: {e}"}
        return {"status": res["status"], "data": res.get("data"), "error": res.get("error")}

    def _fresh(self, entry, key) -> bool:
        if not entry:
            return False
        at, k, res = entry
        ttl = self.ttl if res["status"] in ("ok", "old", "missing") else self.retry
        return k == key and time.monotonic() - at < ttl

    def get(self, wait: bool = False) -> dict:
        key = self.key()
        with self._lock:
            entry = self._entry
        if self._fresh(entry, key):
            return entry[2]
        if wait:
            res = self._read()
            with self._lock:
                self._entry = (time.monotonic(), key, res)
            return res
        with self._lock:
            start = not self._inflight
            self._inflight = True
        if start:
            def _bg() -> None:
                res = self._read()
                with self._lock:
                    self._entry = (time.monotonic(), self.key(), res)
                    self._inflight = False
            threading.Thread(target=_bg, name=f"{self.name}-{self.args[0]}", daemon=True).start()
        if entry:
            return entry[2]                  # stale while the refresh runs
        return {"status": "checking", "data": None, "error": None}


def _stat(p: Path) -> tuple | None:
    try:
        s = os.stat(p)
    except OSError:
        return None
    return (s.st_mtime_ns, s.st_size)


def _declined_count() -> int:
    """How many "declined TCCs" lines the daemon log's tail has: a new one
    only triggers a re-read of `check --json` (it decides nothing)."""
    log = _mc_dir() / "daemon.log"
    try:
        size = log.stat().st_size
        with log.open("rb") as f:
            f.seek(max(0, size - 65536))
            return f.read().lower().count(b"declined tccs")
    except OSError:
        return 0


RECORDER = Cached("meeting-capture", ("status", "--json"), "meeting-capture.status/", ttl=20, retry=20,
                  key=lambda: _stat(_mc_dir() / "state.json"), timeout=20)
PERMISSIONS = Cached("meeting-capture", ("check", "--json"), "meeting-capture.permissions/", ttl=600, retry=60,
                     key=_declined_count, timeout=40)
MEMORY = Cached("contorch-memory", ("status", "--json"), "contorch-memory.status/", ttl=60, retry=30,
                timeout=30)
# Claude Code integration of this install ($CONTORCH_CHANNEL reaches it).
CLAUDE = Cached("contorch-memory", ("claude", "status", "--json"), "contorch-memory.claude/", ttl=120,
                retry=60, timeout=30)


def recorder(wait: bool = False) -> dict:
    return RECORDER.get(wait)


def permissions(wait: bool = False) -> dict:
    return PERMISSIONS.get(wait)


def memory(wait: bool = False) -> dict:
    return MEMORY.get(wait)


def claude(wait: bool = False) -> dict:
    return CLAUDE.get(wait)


def clear() -> None:
    for c in (RECORDER, PERMISSIONS, MEMORY, CLAUDE):
        c.clear()


# ------------------------------------------------------------------ what to show

PERMISSION_TITLES = {"screen_audio": "Screen & System Audio Recording",
                     "system_audio": "System Audio Recording Only",   # meeting-capture's taps backend
                     "microphone": "Microphone"}


def permission_problems(doc: dict | None) -> list[dict]:
    """Rows the recorder needs that aren't granted, as meeting-capture words
    them ({id, title, status, hint, settings_url})."""
    if not doc or not doc.get("ok"):
        return []
    out = []
    for row in doc.get("permissions") or []:
        if row.get("required") and row.get("status") != "granted":
            out.append({"id": row.get("id"), "title": PERMISSION_TITLES.get(row.get("id"), row.get("id")),
                        "status": row.get("status"), "hint": row.get("hint"),
                        "settings_url": row.get("settings_url")})
    return out
