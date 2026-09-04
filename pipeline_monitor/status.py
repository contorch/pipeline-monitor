"""Read-only status collectors for the pipeline subsystems.

Every collector returns a plain dict and FAILS SILENTLY (returns
{"ok": False, "error": str(e)}) when its data source is missing or
unreachable. A half-installed pipeline must still produce a useful
dashboard, not a wall of red.

Each collector is independent. The app composes them.
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

import httpx

HOME = Path.home()
CO_DIR = HOME / ".context-orchestrator"
CO_DB = CO_DIR / "context.db"
CO_CHROMA_DIR = CO_DIR / "chroma"
HOOK_HEARTBEAT = CO_DIR / "auto-context-heartbeat.json"
TRANSCRIPTS_DIR = HOME / "transcripts"
MEETING_CAPTURE_DIR = HOME / ".meeting-capture"
MEETING_CAPTURE_LOG = MEETING_CAPTURE_DIR / "daemon.log"

CHROMA_HOST = "127.0.0.1"
CHROMA_PORT = 8765
CHROMA_TIMEOUT = 1.0  # seconds


# ----------------------------------------------------------- chroma

def chroma_status() -> dict[str, Any]:
    """Heartbeat + collection stats from the chroma daemon."""
    base = f"http://{CHROMA_HOST}:{CHROMA_PORT}"
    out: dict[str, Any] = {"ok": False, "host": f"{CHROMA_HOST}:{CHROMA_PORT}"}
    try:
        with httpx.Client(timeout=CHROMA_TIMEOUT) as cx:
            hb = cx.get(f"{base}/api/v2/heartbeat")
            hb.raise_for_status()
            out["heartbeat_ns"] = hb.json().get("nanosecond heartbeat")

            # Get default collection
            tdb = "/api/v2/tenants/default_tenant/databases/default_database"
            colls = cx.get(f"{base}{tdb}/collections")
            colls.raise_for_status()
            ctx = next((c for c in colls.json() if c["name"] == "context"), None)
            if ctx:
                out["collection_id"] = ctx["id"]
                cnt = cx.get(f"{base}{tdb}/collections/{ctx['id']}/count")
                cnt.raise_for_status()
                out["doc_count"] = int(cnt.text)

                # Sample one embedding to expose dim
                sample = cx.post(
                    f"{base}{tdb}/collections/{ctx['id']}/get",
                    json={"limit": 1, "include": ["embeddings"]},
                )
                if sample.status_code == 200:
                    embs = sample.json().get("embeddings") or []
                    if embs and embs[0]:
                        out["dim"] = len(embs[0])
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


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
_DAEMON_SUFFIXES = [
    "context-orchestrator-chroma",
    "transcript-watcher",
    "meeting-capture",
]
LAUNCHD_TARGETS = [
    f"com.{org}.{suffix}"
    for suffix in _DAEMON_SUFFIXES
    for org in ("contorch", "stirredo")
]


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
    """Recent transcript files + sizes."""
    out: dict[str, Any] = {"ok": False, "dir": str(TRANSCRIPTS_DIR)}
    if not TRANSCRIPTS_DIR.is_dir():
        out["error"] = "no transcripts dir"
        return out
    try:
        files = sorted(
            TRANSCRIPTS_DIR.glob("*.md"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )[:limit]
        sessions = []
        for f in files:
            st = f.stat()
            sessions.append({
                "name": f.name,
                "path": str(f),
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "age_s": int(time.time() - st.st_mtime),
            })
        out["sessions"] = sessions
        out["total_count"] = sum(1 for _ in TRANSCRIPTS_DIR.glob("*.md"))
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ----------------------------------------------------------- meeting-capture

def recording_status() -> dict[str, Any]:
    """Is meeting-capture currently recording? Heuristic: read its log tail.

    meeting-capture daemon's actual log vocabulary (verified May 2026):
      • `mic active — starting recording session`           → start
      • `sysaudio: stream started, piping PCM to stdout`    → start (sub-event)
      • `new session: meeting-2026-05-03T21-37-42.md`       → start (gives filename)
      • `INFO chunk 9.1s [them] -> meeting-...md (226 chars)` → live (chunk landed; role tag [them]/[me] since v0.2.0)
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
            chunk_match = re.search(
                r"\bINFO chunk \d+(?:\.\d+)?s (?:\[\w+\] )?-> (\S+\.md)", line
            )
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
                        sess_matches = re.findall(
                            r"new session:\s*(\S+\.md)", tail, re.IGNORECASE
                        )
                        if sess_matches:
                            current_file = sess_matches[-1]
                # Find the AGE of the newest chunk specifically (not just any
                # start sentinel), so we can flag a stale recording when the
                # daemon is alive but no PCM is reaching the chunker (e.g.
                # another SCK consumer stole system audio capture).
                for inner in reversed(lines):
                    if re.search(r"\bINFO chunk \d+(?:\.\d+)?s (?:\[\w+\] )?-> (\S+\.md)", inner):
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
            ("chroma", CO_CHROMA_DIR),
            ("transcripts", TRANSCRIPTS_DIR),
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
    chroma: dict[str, Any] = field(default_factory=dict)
    db: dict[str, Any] = field(default_factory=dict)
    mcp: dict[str, Any] = field(default_factory=dict)
    launchd: dict[str, Any] = field(default_factory=dict)
    recordings: dict[str, Any] = field(default_factory=dict)
    recording: dict[str, Any] = field(default_factory=dict)
    hook: dict[str, Any] = field(default_factory=dict)
    disk: dict[str, Any] = field(default_factory=dict)

    def overall(self) -> str:
        """State for the menu bar icon: rec / rec_stale / perm / err / ok.

        Only flag 'err' for things that actually mean something is broken:
          - chroma daemon unreachable
          - any installed launchd daemon stopped
          - MCP server has a recent tool-call failure (not just idle)
        We deliberately do NOT flag MCP as 'err' just because it hasn't
        seen a tool call recently — Claude Code might just not be open.
        """
        if self.recording.get("recording"):
            # Distinguish a healthy live recording from one where the daemon
            # claims REC but no chunks are landing — the latter is a loud
            # ⚠ in the menu bar so the user notices mid-meeting.
            if self.recording.get("stale"):
                return "rec_stale"
            return "rec"
        if self.recording.get("permission_denied"):
            # sysaudio is being refused Screen Recording — every session dies
            # at spawn, so the meeting is NOT being captured. Loudest state
            # short of a live recording.
            return "perm"
        problems = []
        if not self.chroma.get("ok"):
            problems.append("chroma")
        for label, info in self.launchd.get("daemons", {}).items():
            if info.get("installed") and not info.get("running"):
                problems.append(label.split(".")[-1])
        # Only flag MCP if its MOST RECENT call failed (active problem),
        # not if it's been idle.
        last_call = self.mcp.get("last_call")
        if last_call and last_call.get("result") == "fail":
            # And only if that failure was within the last hour
            try:
                from datetime import datetime
                ts_str = str(last_call.get("ts", "")).replace("Z", "+00:00")
                age_s = time.time() - datetime.fromisoformat(ts_str).timestamp()
                if age_s < 3600:
                    problems.append("mcp")
            except Exception:
                pass
        return "err" if problems else "ok"


def collect() -> Snapshot:
    """Run every collector and return a snapshot. Each call is sub-second."""
    return Snapshot(
        chroma=chroma_status(),
        db=db_status(),
        mcp=mcp_status(),
        launchd=launchd_status(),
        recordings=recordings_status(),
        recording=recording_status(),
        hook=hook_status(),
        disk=disk_status(),
    )


if __name__ == "__main__":
    import json as _j
    snap = collect()
    print(_j.dumps({
        "overall": snap.overall(),
        "chroma": snap.chroma,
        "db": snap.db,
        "mcp": {k: v for k, v in snap.mcp.items() if k != "recent_calls"},
        "mcp_recent_calls": snap.mcp.get("recent_calls", [])[-5:],
        "launchd": snap.launchd,
        "recordings_count": snap.recordings.get("total_count"),
        "recording": snap.recording,
        "hook": snap.hook,
        "disk": snap.disk,
    }, indent=2, default=str))
