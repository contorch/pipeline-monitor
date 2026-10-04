"""Read-only status collectors for the pipeline subsystems.

Every collector returns a plain dict and FAILS SILENTLY (returns
{"ok": False, "error": str(e)}) when its data source is missing or
unreachable. A half-installed pipeline must still produce a useful
dashboard, not a wall of red.

Each collector is independent. The app composes them.

Decisions come from the owners' JSON (pipeline_monitor.ownerstate):
"recording?" from `meeting-capture status --json`, the permission rows from
`meeting-capture check --json`, the index from `contorch-memory status
--json`, meeting-capture's settings from `config --json` (mcconfig), the
modules from pipeline_monitor.modules. The daemon-log parser below only
DISPLAYS recent activity (the meeting, a silent chunk) and stands in for
meeting-capture 0.7, which has no status/check --json.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import mcconfig, modules, owners, ownerstate
from . import transcription as stt

HOME = Path.home()
CO_DIR = HOME / ".context-orchestrator"
CO_DB = CO_DIR / "context.db"
HOOK_HEARTBEAT = CO_DIR / "auto-context-heartbeat.json"
TRANSCRIPTS_DIR = HOME / "transcripts"
MEETING_CAPTURE_DIR = HOME / ".meeting-capture"
MEETING_CAPTURE_LOG = MEETING_CAPTURE_DIR / "daemon.log"


# ----------------------------------------------------------- context-orch SQLite

def db_status() -> dict[str, Any]:
    """Counts + recency from context.db."""
    if not CO_DB.exists():
        return {"ok": False, "error": f"missing {CO_DB}"}
    try:
        conn = sqlite3.connect(f"file:{CO_DB}?mode=ro", uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        rk = conn.execute("SELECT COUNT(*) FROM repo_knowledge").fetchone()[0]
        srcs = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        last_rk = conn.execute(
            "SELECT created_at FROM repo_knowledge ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_src = conn.execute(
            "SELECT added_at FROM sources ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return {
            "ok": True,
            "tasks": tasks,
            "sources": srcs,
            "repo_knowledge": rk,
            "last_repo_knowledge": last_rk[0] if last_rk else None,
            "last_source": last_src[0] if last_src else None,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ----------------------------------------------------------- memory (context-orchestrator)

def memory_status(wait: bool = False) -> dict[str, Any]:
    """The index as context-orchestrator reports it (`contorch-memory status
    --json`: embeddings, vector_index in_process|server|none, docs,
    transcripts, index_compatible). pm no longer mirrors the embedding rules
    (INV-D2). {"ok", "status", "data", "error", "label"}."""
    r = ownerstate.memory(wait)
    d = r.get("data") or {}
    out: dict[str, Any] = {"status": r["status"], "data": d or None, "error": r.get("error")}
    if r["status"] == "ok":
        out["ok"] = bool(d.get("ok"))
        if not d.get("ok") and d.get("error"):
            out["error"] = f"{d['error'].get('code')}: {d['error'].get('message')}"
        out["keyword_only"] = d.get("vector_index") == "none"
    elif r["status"] == "old":
        out.update(ok=False, error="context-orchestrator < 0.5 — upgrade it (brew upgrade context-orchestrator)")
    elif r["status"] == "checking":
        out["ok"] = True
    else:
        out["ok"] = False
    return out


# ----------------------------------------------------------- MCP server log

def _mcp_log_dir() -> Optional[Path]:
    base = HOME / "Library" / "Caches" / "claude-cli-nodejs"
    if not base.is_dir():
        return None
    # Pick the most recently active project log dir
    candidates = []
    for proj in base.iterdir():
        d = proj / "mcp-logs-context-orchestrator"
        if d.is_dir():
            files = list(d.glob("*.jsonl"))
            if files:
                latest = max(files, key=lambda f: f.stat().st_mtime)
                candidates.append((latest.stat().st_mtime, d, latest))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def mcp_status(tail_calls: int = 20) -> dict[str, Any]:
    """Recent tool calls + connection state from the MCP server log."""
    out: dict[str, Any] = {"ok": False}
    log_dir = _mcp_log_dir()
    if not log_dir:
        out["error"] = "no MCP log dir found"
        return out
    files = sorted(log_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        out["error"] = "no .jsonl log files"
        return out
    latest = files[0]
    out["log_file"] = str(latest)
    out["log_age_s"] = int(time.time() - latest.stat().st_mtime)

    calls: list[dict[str, Any]] = []
    last_connect: Optional[str] = None
    last_error: Optional[str] = None
    last_call: Optional[dict[str, Any]] = None
    try:
        with latest.open() as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                ts = e.get("timestamp", "")
                msg = e.get("debug") or e.get("error") or ""
                if "Successfully connected" in msg:
                    last_connect = ts
                if "Calling MCP tool" in msg:
                    name = msg.split(":", 1)[1].strip() if ":" in msg else "?"
                    calls.append({"ts": ts, "tool": name, "result": "pending"})
                elif "completed successfully" in msg:
                    if calls and calls[-1]["result"] == "pending":
                        m = re.search(r"in (\d+)ms", msg)
                        calls[-1]["result"] = "ok"
                        calls[-1]["latency_ms"] = int(m.group(1)) if m else None
                elif "failed after" in msg or "Error executing tool" in msg:
                    if calls and calls[-1]["result"] == "pending":
                        calls[-1]["result"] = "fail"
                    last_error = msg[:300]
        out["ok"] = True
        out["recent_calls"] = calls[-tail_calls:]
        out["last_connect"] = last_connect
        out["last_error"] = last_error
        out["last_call"] = calls[-1] if calls else None
        # Heuristic: alive if log was touched recently
        out["alive"] = out["log_age_s"] < 600
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ----------------------------------------------------------- launchd

# Daemons are being rebranded com.stirredo.* → com.contorch.* — watch both
# prefixes during the transition and report whichever variant is loaded.
# transcript-watcher is gone from the default install (context-orchestrator
# 0.3 indexes on demand from the MCP server), so it is not a required daemon.
_DAEMON_SUFFIXES = [
    "context-orchestrator-chroma",
    "meeting-capture",
]
LAUNCHD_TARGETS = [
    f"com.{org}.{suffix}"
    for suffix in _DAEMON_SUFFIXES
    for org in ("contorch", "stirredo")
]

CAPTURE_MODES = ("batch", "live")


def capture_mode_status(wait: bool = True) -> dict[str, Any]:
    """Which capture mode the recorder is configured for, as meeting-capture
    reports it (`meeting-capture config --json`; meeting-capture 0.7: its
    plist). "batch" when unset. wait=False (the menu timer): the cached
    answer, refreshed in the background."""
    out: dict[str, Any] = {"ok": False, "mode": "batch", "installed": mcconfig.installed(wait=wait)}
    if not out["installed"]:
        out["error"] = "meeting-capture's recorder agent isn't installed"
        return out
    mode = str(mcconfig.setting("mode", "batch", wait=wait) or "batch").strip().lower()
    out["mode"] = mode if mode in CAPTURE_MODES else "batch"
    out["ok"] = True
    return out


# ----------------------------------------------------------- transcription engine

def transcription_status(wait: bool = False) -> dict[str, Any]:
    """How meeting-capture transcribes: on this Mac (with its language),
    Gemini, or unavailable (and why), plus whether live mode streams calls and
    where audio goes. Asked from meeting-capture (`meeting-capture stt
    --json`; pipeline_monitor.transcription), never re-derived here. The
    answer is cached and refreshed on a background thread unless wait=True,
    so the 5-second timer never waits on it. {"ok": False} without the
    recorder's launchd agent (the menu leaves the line out)."""
    try:
        if not mcconfig.installed(wait=wait):
            return {"ok": False, "installed": False,
                    "error": "meeting-capture's recorder agent isn't installed"}
        return stt.current(wait=wait)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def launchd_status() -> dict[str, Any]:
    """`launchctl list` parsed for our daemons."""
    out: dict[str, Any] = {"ok": False, "daemons": {}}
    try:
        r = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=2,
        )
        if r.returncode != 0:
            out["error"] = r.stderr.strip()
            return out
        lines = r.stdout.strip().splitlines()
        found: dict[str, Any] = {}
        # Format: PID  Status  Label
        for line in lines[1:]:  # skip header
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            pid_s, status_s, label = parts
            if label in LAUNCHD_TARGETS:
                found[label] = {
                    "pid": None if pid_s == "-" else int(pid_s),
                    "status": int(status_s),  # last exit code
                    "running": pid_s != "-",
                    "installed": True,
                }
        # One entry per daemon: prefer the loaded variant (contorch first)
        # so a machine mid-rebrand doesn't show duplicate/ghost rows.
        for suffix in _DAEMON_SUFFIXES:
            for org in ("contorch", "stirredo"):
                label = f"com.{org}.{suffix}"
                if label in found:
                    out["daemons"][label] = found[label]
                    break
            else:
                out["daemons"][f"com.contorch.{suffix}"] = {
                    "pid": None, "status": None, "running": False, "installed": False,
                }
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ----------------------------------------------------------- recordings dir

def recordings_status(limit: int = 15) -> dict[str, Any]:
    """Recent transcripts. meeting-capture >= 0.5 / context-orchestrator >= 0.4
    keep them in context.db (table `transcripts`, full text in `body`); older
    installs wrote ~/transcripts/*.md. Both are listed, newest first."""
    out: dict[str, Any] = {"ok": False, "dir": str(TRANSCRIPTS_DIR), "db": str(CO_DB)}
    sessions: list[dict[str, Any]] = []
    total = 0
    now = time.time()
    try:
        if CO_DB.exists():
            conn = sqlite3.connect(f"file:{CO_DB}?mode=ro", uri=True, timeout=2)
            try:
                has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                   "AND name='transcripts'").fetchone()
                if has:
                    total += conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
                    for mid, title, size, updated in conn.execute(
                            "SELECT meeting_id, title, length(body), updated_at FROM transcripts "
                            "ORDER BY updated_at DESC LIMIT ?", (limit,)):
                        sessions.append({
                            "name": mid, "title": title or mid, "meeting_id": mid, "path": None,
                            "size": size or 0,
                            "mtime": datetime.fromtimestamp(updated).isoformat(timespec="seconds"),
                            "age_s": int(now - updated),
                        })
            finally:
                conn.close()
        if TRANSCRIPTS_DIR.is_dir():
            files = list(TRANSCRIPTS_DIR.glob("*.md"))
            total += len(files)
            for f in sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)[:limit]:
                st = f.stat()
                sessions.append({
                    "name": f.name, "title": f.stem, "meeting_id": None, "path": str(f),
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                    "age_s": int(now - st.st_mtime),
                })
        if not CO_DB.exists() and not TRANSCRIPTS_DIR.is_dir():
            out["error"] = "no transcripts yet"
            return out
        sessions.sort(key=lambda x: x["age_s"])
        out["sessions"] = sessions[:limit]
        out["total_count"] = total
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def transcript_text(meeting_id: str) -> str | None:
    """Full text of a stored transcript (read-only)."""
    try:
        conn = sqlite3.connect(f"file:{CO_DB}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute("SELECT body FROM transcripts WHERE meeting_id = ?",
                               (meeting_id,)).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:
        return None


# ----------------------------------------------------------- meeting-capture

# `chunk 9.1s [them] -> meeting-2026-05-03T21-37-42.md (226 chars)` (<= 0.4)
# `chunk 9.1s [them] -> meeting-2026-05-03T21-37-42 (226 chars)`    (>= 0.5)
_CHUNK_RE = re.compile(r"\bINFO chunk \d+(?:\.\d+)?s (?:\[\w+\] )?-> (\S+?) \(\d+ chars\)")
_SESSION_RE = re.compile(r"new session:\s*(meeting-\S+)", re.IGNORECASE)

def log_recording_status() -> dict[str, Any]:
    """What the daemon log's tail shows: DISPLAY only (the current meeting,
    a recording that has gone silent), and the stand-in for meeting-capture
    0.7, which has no `status --json`. Decisions use recording_status().

    meeting-capture daemon's actual log vocabulary (verified May 2026):
      • `mic active — starting recording session`           → start
      • `sysaudio: stream started, piping PCM to stdout`    → start (sub-event)
      • `new session: meeting-2026-05-03T21-37-42[.md]`     → start (gives the meeting)
      • `INFO chunk 9.1s [them] -> meeting-...[.md] (226 chars)` → live (chunk landed; role tag [them]/[me] since v0.2.0)
    meeting-capture >= 0.5.0 stores transcripts in the database and logs the
    meeting id without `.md`; older versions log the file name. Both match.
      • `mic inactive — session ended`                      → stop
      • `sysaudio error: ... "The user declined TCCs ..."`  → permission denied
    Walks the tail backwards and returns the most recent state event.
    A `declined TCCs` line newer than the last `stream started` line means
    sysaudio's Screen Recording grant is gone (macOS update / rebuild) —
    surfaced as `permission_denied` so the menu bar shows ⚠ PERM instead
    of a misleading ○ Idle while every session dies at spawn.
    Chunk lines are treated as live-recording evidence so long meetings
    don't flip the indicator to Idle once the START sentinel scrolls past
    the tail buffer.
    """
    out: dict[str, Any] = {"ok": False, "recording": False}
    if not MEETING_CAPTURE_LOG.exists():
        out["error"] = f"missing {MEETING_CAPTURE_LOG}"
        return out
    try:
        st = MEETING_CAPTURE_LOG.stat()
        out["log_age_s"] = int(time.time() - st.st_mtime)
        # 64 KB tail covers ~25 min of busy logging — enough margin for
        # a long-running meeting before the most recent sentinel scrolls
        # off the buffer. Cheap, all in OS page cache.
        with MEETING_CAPTURE_LOG.open("rb") as f:
            f.seek(max(0, st.st_size - 65536))
            tail = f.read().decode("utf-8", errors="ignore")
        lines = tail.strip().splitlines()

        # Freshness guard: if the most-recent START sentinel is older than
        # this, treat the daemon as not recording — covers the case where
        # meeting-capture hangs (e.g. blocked SSL_read on a Gemini call) and
        # never emits its STOP sentinel, leaving stale chunk lines as the
        # newest evidence in the tail.
        #
        # 11 min: chunks legitimately gap up to MAX_CHUNK_SECONDS (600s) in
        # meeting-capture's chunker — long monologues with intermittent
        # silence accumulate into a single chunk that only emits at the max
        # boundary. Earlier 90s was too aggressive and caused false-idle
        # readings during normal long-form chunks. The daemon's own bails
        # (recorder.py NO_EMIT_BAIL_S = 300s, SILENT_AUDIO_BAIL_S = 300s)
        # mean any real wedge resolves itself within ~5 min anyway, so a
        # generous monitor threshold doesn't lose us much actual visibility.
        STALE_AFTER_S = 660
        now = time.time()
        ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

        def _age_seconds(line: str) -> float | None:
            m = ts_re.match(line)
            if not m:
                return None
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                return None
            return now - ts

        # If we're in a session but no chunk has landed in this many seconds,
        # flag the recording as STALE — daemon thinks it's recording but
        # nothing is being captured. Most often: another app stole system
        # audio capture (Cluely, Loom, OBS — anything using SCK / recall.ai).
        #
        # 600s = MAX_CHUNK_SECONDS in meeting-capture's chunker. A single
        # legitimate chunk can accumulate up to that long during a quiet
        # monologue before the chunker force-emits at the max boundary.
        # Below this we'd false-warn during normal long chunks. Above it,
        # the daemon's own no-emit / silent-pcm bails (both 300s) will have
        # already fired and respawned sysaudio — so a real wedge produces a
        # diagnostic in the daemon log within 5 min, well before this gate.
        STALE_CHUNK_AFTER_S = 600

        # Permission check first: the decline line is followed by a
        # "session ended" STOP sentinel, so the state walk below would
        # otherwise just report Idle. sysaudio's own lines carry no
        # timestamp, so age comes from the nearest earlier daemon line.
        PERM_DENIED_WINDOW_S = 15 * 60  # daemon backoff caps at 300s
        denied_idx = started_idx = None
        for i in range(len(lines) - 1, -1, -1):
            low = lines[i].lower()
            if denied_idx is None and "declined tccs" in low:
                denied_idx = i
            elif started_idx is None and "sysaudio: stream started" in low:
                started_idx = i
            if denied_idx is not None and started_idx is not None:
                break
        if denied_idx is not None and (started_idx is None or denied_idx > started_idx):
            denied_age = None
            for j in range(denied_idx, -1, -1):
                denied_age = _age_seconds(lines[j])
                if denied_age is not None:
                    break
            if denied_age is not None and denied_age <= PERM_DENIED_WINDOW_S:
                out["permission_denied"] = True
                out["permission_denied_age_s"] = int(denied_age)

        recording = False
        current_file = None
        last_chunk_age_s: float | None = None
        for line in reversed(lines):
            low = line.lower()
            # STOP sentinels — definitive end-of-recording markers
            if "session ended" in low or "recording stopped" in low or "shutting down" in low:
                recording = False
                break
            # START sentinels — any of these means we're mid-recording.
            # Match against the original case-preserved line so the
            # filename keeps its `T` separator (`meeting-...T13-01-53.md`).
            chunk_match = _CHUNK_RE.search(line)
            if (
                "mic active" in low
                or "starting recording session" in low
                or "sysaudio: stream started" in low
                or "session started" in low
                or low.startswith("new session:")
                or " new session:" in low
                or chunk_match is not None
            ):
                age = _age_seconds(line)
                if age is not None and age > STALE_AFTER_S:
                    # Newest start-evidence is too old — daemon is stuck or
                    # session ended without a stop-sentinel. Don't claim REC.
                    recording = False
                    break
                recording = True
                # Prefer the filename from the chunk line we just matched
                # (it's the most recent log entry). Fall back to the LAST
                # "new session:" in the tail, which marks the current
                # session if no chunks have landed yet.
                if not current_file:
                    if chunk_match is not None:
                        current_file = chunk_match.group(1)
                    else:
                        sess_matches = _SESSION_RE.findall(tail)
                        if sess_matches:
                            current_file = sess_matches[-1]
                # Find the AGE of the newest chunk specifically (not just any
                # start sentinel), so we can flag a stale recording when the
                # daemon is alive but no PCM is reaching the chunker (e.g.
                # another SCK consumer stole system audio capture).
                for inner in reversed(lines):
                    if _CHUNK_RE.search(inner):
                        a = _age_seconds(inner)
                        if a is not None:
                            last_chunk_age_s = a
                        break
                break
        if out.get("permission_denied"):
            # Whatever the walk concluded, a denied spawn is not a recording.
            recording = False
        out["recording"] = recording
        out["current_file"] = current_file
        if recording and last_chunk_age_s is not None:
            out["last_chunk_age_s"] = int(last_chunk_age_s)
            out["stale"] = last_chunk_age_s > STALE_CHUNK_AFTER_S
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def recording_status(wait: bool = False) -> dict[str, Any]:
    """Is a meeting being recorded right now? meeting-capture's answer
    (`meeting-capture status --json`): recording true | false | None (can't
    tell — never shown as ● REC). The log only adds display detail (the
    meeting's name, a recording gone silent). meeting-capture 0.7 has no
    status --json: the log parser decides, as before (source "log")."""
    log = log_recording_status()
    r = ownerstate.recorder(wait)
    if r["status"] in ("old", "missing", "not_built"):
        return {**log, "source": "log"}
    out: dict[str, Any] = {"ok": True, "source": "owner", "recording": None}
    if r["status"] == "ok":
        d = r["data"]
        out.update(recording=d.get("recording"), state=d.get("state"), since=d.get("since"),
                   pid=d.get("pid"), reason=d.get("reason"), meeting_id=d.get("meeting_id"))
    elif r["status"] == "checking":
        out["reason"] = "checking"
    else:
        out["reason"] = "error"
        out["error"] = r.get("error")
    if out["recording"]:
        out["current_file"] = out.get("meeting_id") or log.get("current_file")
        if log.get("recording") and log.get("stale"):
            out["stale"] = True
            out["last_chunk_age_s"] = log.get("last_chunk_age_s")
    return out


def permissions_status(wait: bool = False) -> dict[str, Any]:
    """The recorder's permissions as meeting-capture reports them
    (`meeting-capture check --json`): rows not granted, each with
    meeting-capture's own hint and Privacy pane URL. meeting-capture 0.7 has
    no check --json: a recent "declined TCCs" in its log is all there is."""
    r = ownerstate.permissions(wait)
    if r["status"] == "ok":
        probs = ownerstate.permission_problems(r["data"])
        return {"ok": True, "source": "owner", "problems": probs,
                "denied": [p for p in probs if p["status"] in ("denied", "not_granted")],
                "identity": (r["data"] or {}).get("identity")}
    if r["status"] == "old":
        log = log_recording_status()
        if log.get("permission_denied"):
            prob = {"id": "screen_audio", "title": "Screen & System Audio Recording", "status": "denied",
                    "hint": LEGACY_PERMISSION_HINT, "settings_url": SCREEN_PANE}
            return {"ok": True, "source": "log", "problems": [prob], "denied": [prob]}
        return {"ok": True, "source": "log", "problems": [], "denied": []}
    return {"ok": False, "source": "owner", "problems": [], "denied": [], "status": r["status"],
            "error": r.get("error")}


# meeting-capture 0.7 only (its check has no --json and no per-channel hint).
LEGACY_PERMISSION_HINT = ("sysaudio was refused Screen & System Audio Recording — `meeting-capture doctor` "
                          "shows which sysaudio to allow")
SCREEN_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"


def modules_status(wait: bool = False) -> dict[str, Any]:
    """The module rows (modules.status) from the owners' answers: memory =
    this install's MCP entry exists (Claude Code status), recorder / line-in
    from meeting-capture's settings. wait=False (the menu): cached answers
    only, never a subprocess in the foreground."""
    try:
        ch = owners.channel()
        claude = ownerstate.claude(wait)
        mem = None
        if claude["status"] == "ok":
            mem = bool(((claude["data"] or {}).get("mcp") or {}).get("present"))
        elif claude["status"] in ("missing",):
            mem = False
        if owners.locate("meeting-capture"):
            rec = mcconfig.installed(wait=wait)
            lin = bool(rec) and mcconfig.setting("source", "sck", wait=wait) == "linein"
            if not mcconfig.known():          # still asking: unknown, never "not set up"
                rec = lin = None
        else:
            rec = lin = False
        actual = {"memory": mem, "recorder": rec, "linein": lin,
                  "cli": True if ch != "app" else bool(modules.cli_links()["linked"])}
        doc = modules.status(ch, actual=actual)
        doc["claude"] = claude.get("data")
        return doc
    except Exception as e:  # noqa: BLE001 — the menu must render
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "modules": [], "set_up": False}


# ----------------------------------------------------------- auto-context hook heartbeat

def hook_status() -> dict[str, Any]:
    """When did the auto-context hook last fire? Hook writes a heartbeat file."""
    out: dict[str, Any] = {"ok": False}
    if not HOOK_HEARTBEAT.exists():
        out["error"] = "no heartbeat — hook may not be installed or hasn't fired yet"
        return out
    try:
        data = json.loads(HOOK_HEARTBEAT.read_text())
        st = HOOK_HEARTBEAT.stat()
        out.update(data)
        out["age_s"] = int(time.time() - st.st_mtime)
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ----------------------------------------------------------- disk

def disk_status() -> dict[str, Any]:
    """Disk usage of pipeline data dirs."""
    out: dict[str, Any] = {"ok": False, "dirs": {}}
    try:
        for label, path in [
            ("context-orchestrator", CO_DIR),
            ("meeting-capture", MEETING_CAPTURE_DIR),
        ]:
            if path.is_dir():
                r = subprocess.run(
                    ["du", "-sh", str(path)],
                    capture_output=True, text=True, timeout=3,
                )
                if r.returncode == 0:
                    out["dirs"][label] = r.stdout.split()[0]
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ----------------------------------------------------------- composite

@dataclass
class Snapshot:
    """All collectors' output bundled into one frame."""
    ts: float = field(default_factory=time.time)
    memory: dict[str, Any] = field(default_factory=dict)
    db: dict[str, Any] = field(default_factory=dict)
    mcp: dict[str, Any] = field(default_factory=dict)
    launchd: dict[str, Any] = field(default_factory=dict)
    recordings: dict[str, Any] = field(default_factory=dict)
    recording: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    capture_mode: dict[str, Any] = field(default_factory=dict)
    hook: dict[str, Any] = field(default_factory=dict)
    disk: dict[str, Any] = field(default_factory=dict)
    transcription: dict[str, Any] = field(default_factory=dict)
    modules: dict[str, Any] = field(default_factory=dict)

    def recorder_on(self) -> bool:
        """Does this Mac record (the recorder module is on, or — before any
        choice — its agent is installed)?"""
        rows = {r["id"]: r for r in self.modules.get("modules") or []}
        rec = rows.get("recorder")
        if rec is None:
            return bool(self.capture_mode.get("installed"))
        return rec["state"] in ("on", "attention")

    def memory_only(self) -> bool:
        return bool(self.modules.get("set_up")) and not self.recorder_on()

    def headline(self) -> str:
        """recording | recording_unknown | memory_only | idle | needs_setup."""
        if not self.modules.get("set_up", True):
            return "needs_setup"
        if self.memory_only():
            return "memory_only"
        rec = self.recording.get("recording")
        if rec:
            return "recording"
        if rec is None and self.recording.get("source") == "owner" \
                and self.recording.get("reason") not in ("checking", "daemon_not_running"):
            return "recording_unknown"
        return "idle"

    def overall(self) -> str:
        """State for the menu bar icon: rec / rec_stale / perm / err / idle.

        ● REC only when meeting-capture says recording: true (the log only
        for meeting-capture 0.7). 'err' only for things that are broken:
          - the memory (context-orchestrator's status says not ok)
          - the recorder's agent isn't running while it should
          - nothing can transcribe (meeting-capture's `stt --json`)
          - MCP's most recent tool call failed in the last hour
        A memory-only Mac has no recorder to complain about.
        """
        recorder = self.recorder_on()
        if recorder and self.recording.get("recording"):
            return "rec_stale" if self.recording.get("stale") else "rec"
        if recorder and self.permissions.get("denied"):
            return "perm"
        problems = []
        if self.memory and not self.memory.get("ok", True):
            problems.append("memory")
        if recorder:
            for label, info in self.launchd.get("daemons", {}).items():
                if label.endswith("meeting-capture") and info.get("installed") and not info.get("running"):
                    problems.append("recorder")
            if self.transcription.get("ok") and self.transcription.get("attention"):
                problems.append("transcription")
        last_call = self.mcp.get("last_call")
        if last_call and last_call.get("result") == "fail":
            try:
                ts_str = str(last_call.get("ts", "")).replace("Z", "+00:00")
                if time.time() - datetime.fromisoformat(ts_str).timestamp() < 3600:
                    problems.append("mcp")
            except Exception:
                pass
        return "err" if problems else "idle"


def collect(wait: bool = False) -> Snapshot:
    """Run every collector and return a snapshot. Sub-second: the owners are
    asked on background threads (wait=True asks them now: `contorch status`)."""
    mods = modules_status(wait=wait)
    recorder_present = owners.locate("meeting-capture") is not None
    return Snapshot(
        memory=memory_status(wait=wait),
        db=db_status(),
        mcp=mcp_status(),
        launchd=launchd_status(),
        recordings=recordings_status(),
        recording=recording_status(wait=wait) if recorder_present else {"ok": False, "recording": False,
                                                                        "error": "no recorder on this Mac"},
        permissions=permissions_status(wait=wait) if recorder_present else {"ok": True, "problems": [],
                                                                            "denied": []},
        capture_mode=capture_mode_status(wait=wait),
        hook=hook_status(),
        disk=disk_status(),
        transcription=transcription_status(wait=wait),
        modules=mods,
    )


if __name__ == "__main__":
    import json as _j
    snap = collect(wait=True)
    print(_j.dumps({
        "overall": snap.overall(),
        "headline": snap.headline(),
        "memory": snap.memory,
        "db": snap.db,
        "mcp": {k: v for k, v in snap.mcp.items() if k != "recent_calls"},
        "mcp_recent_calls": snap.mcp.get("recent_calls", [])[-5:],
        "launchd": snap.launchd,
        "recordings_count": snap.recordings.get("total_count"),
        "recording": snap.recording,
        "hook": snap.hook,
        "disk": snap.disk,
        "transcription": snap.transcription,
    }, indent=2, default=str))
