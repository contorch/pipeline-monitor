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

LAUNCHD_TARGETS = [
    "com.stirredo.context-orchestrator-chroma",
    "com.stirredo.transcript-watcher",
    "com.stirredo.meeting-capture",
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
        # Format: PID  Status  Label
        for line in lines[1:]:  # skip header
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            pid_s, status_s, label = parts
            if label in LAUNCHD_TARGETS:
                out["daemons"][label] = {
                    "pid": None if pid_s == "-" else int(pid_s),
                    "status": int(status_s),  # last exit code
                    "running": pid_s != "-",
                }
        # Mark unknown daemons as not-installed
        for t in LAUNCHD_TARGETS:
            out["daemons"].setdefault(t, {"pid": None, "status": None, "running": False, "installed": False})
            if "installed" not in out["daemons"][t]:
                out["daemons"][t]["installed"] = True
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
    """Is meeting-capture currently recording? Heuristic: read its log tail."""
    out: dict[str, Any] = {"ok": False, "recording": False}
    if not MEETING_CAPTURE_LOG.exists():
        out["error"] = f"missing {MEETING_CAPTURE_LOG}"
        return out
    try:
        st = MEETING_CAPTURE_LOG.stat()
        out["log_age_s"] = int(time.time() - st.st_mtime)
        # Read last ~16KB to find the most recent state event
        with MEETING_CAPTURE_LOG.open("rb") as f:
            f.seek(max(0, st.st_size - 16384))
            tail = f.read().decode("utf-8", errors="ignore")
        lines = tail.strip().splitlines()
        # Look backwards for the most recent recording state hint
        recording = False
        current_file = None
        for line in reversed(lines):
            low = line.lower()
            if "recording stopped" in low or "session ended" in low:
                recording = False
                break
            if "recording started" in low or "session started" in low:
                recording = True
                # Try to extract file path
                m = re.search(r"(/[^\s]+\.(?:wav|m4a|md))", line)
                if m:
                    current_file = m.group(1)
                break
        out["recording"] = recording
        out["current_file"] = current_file
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
        """State for the menu bar icon: rec / err / ok.

        Only flag 'err' for things that actually mean something is broken:
          - chroma daemon unreachable
          - any installed launchd daemon stopped
          - MCP server has a recent tool-call failure (not just idle)
        We deliberately do NOT flag MCP as 'err' just because it hasn't
        seen a tool call recently — Claude Code might just not be open.
        """
        if self.recording.get("recording"):
            return "rec"
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
