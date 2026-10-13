"""Where the recorder's audio comes from, and whether that works — the
menu's "Source:" line, its ⚠ and the one notification per outage
(backport of pipeline-monitor #18 for meeting-capture 0.7.x).

meeting-capture 0.7 has no `status --json`: the source comes from its
settings (the launchd plist's environment, the same keys `meeting-capture
source` edits) and an outage from its daemon log, read since the running
daemon's own "daemon starting (pid=N" line:

  * 0.7.0 retries a missing interface every 30 s and logs `line-in:
    listening on the interface` followed at once by `ERROR line-in capture
    unavailable: … — retrying in 30s`. It has no fallback: while that lasts
    nothing is recorded.
  * 0.7.1 logs `line-in fallback: on|off` at start, the same ERROR line
    every 30 s while the interface is missing, `… recording this Mac's call
    audio instead` when a call is recorded from this Mac meanwhile, `… is
    connected again — staying on this Mac's call audio until the call ends`,
    `line-in fallback: the call ended`, and `line-in: '<dev>' is back`.

    status(daemon_pid) -> {"ok", "via": "settings", "configured": "sck"|"linein",
                           "device", "me_channel", "them_channel",
                           "effective": "linein"|"sck"|None,
                           "problem": None | {code, device, message, since, fallback, returned?}}

`problem.fallback`: "active" (recording this Mac's call audio instead),
"armed" (a call would be), "off" (the setting is off), None (meeting-capture
0.7.0: no fallback). `problem.returned`: the interface came back during a
fallback call (meeting-capture switches back when the call ends).
"""
from __future__ import annotations

import plistlib
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# The daemon logs an "unavailable" line every 30 s while an outage lasts: it
# is still on if its newest line is this recent (nothing is logged while paused).
LOG_FRESH_S = 95
BLOCK = 256 * 1024
MAX_SCAN = 64 * 1024 * 1024
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_UNAVAILABLE = re.compile(r"line-in capture unavailable: (.*?)(?: — retrying in \d+s)?\s*$")


def _log_path() -> Path:
    return Path.home() / ".meeting-capture" / "daemon.log"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.contorch.meeting-capture.plist"


def _int(v, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def configured(plist: Path | None = None) -> dict[str, Any]:
    """The source settings, as meeting-capture 0.7 keeps them (its plist)."""
    try:
        env = plistlib.loads((plist or _plist_path()).read_bytes()).get("EnvironmentVariables") or {}
    except Exception:  # noqa: BLE001 — no agent / unreadable: the defaults
        env = {}
    src = str(env.get("MEETING_CAPTURE_SOURCE", "sck") or "sck").strip().lower()
    return {"configured": "linein" if src == "linein" else "sck",
            "device": (str(env.get("MEETING_CAPTURE_INPUT_DEVICE") or "")).strip() or None,
            "me_channel": _int(env.get("MEETING_CAPTURE_ME_CHANNEL", "0"), 0),
            "them_channel": _int(env.get("MEETING_CAPTURE_THEM_CHANNEL", "1"), 1)}


def _ts(line: str) -> float | None:
    m = _TS.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def _read_since_start(path: Path, pid: int | None) -> tuple[list[str], int]:
    """(lines from daemon `pid`'s start line to EOF, byte offset of EOF).
    Without the start line (another pid, scrolled past MAX_SCAN): the last BLOCK."""
    size = path.stat().st_size
    with path.open("rb") as f:
        needle = f"daemon starting (pid={pid},".encode() if pid else None
        pos, buf = size, b""
        while needle and pos > 0 and size - pos < MAX_SCAN:
            step = min(BLOCK, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            i = buf.rfind(needle)
            if i >= 0:
                start = buf.rfind(b"\n", 0, i) + 1
                return buf[start:].decode("utf-8", errors="ignore").splitlines(), size
        f.seek(max(0, size - BLOCK))
        return f.read().decode("utf-8", errors="ignore").splitlines(), size


class LogReader:
    """The daemon log's line-in story, read incrementally: the first call
    finds the running daemon's start line, later calls read only what was
    appended (the menu refreshes every few seconds; the log is MBs)."""

    def __init__(self) -> None:
        self._key = None
        self._offset = 0
        self._pending: list[str] = []
        self.reset()

    def reset(self) -> None:
        self.started = False
        self.source: str | None = None
        self.fb_setting: bool | None = None
        self.problem: dict | None = None
        self.last_err: float | None = None
        self.active = False

    def feed(self, lines: list[str]) -> None:
        # a "listening" line needs the line after it: hold it back until that arrives
        lines = self._pending + lines
        self._pending = []
        for i, line in enumerate(lines):
            low = line.lower()
            if "line-in: listening on the interface" in low:
                nxt = next((l for l in lines[i + 1:i + 3] if _TS.match(l)), None)
                if nxt is None:
                    self._pending = lines[i:]
                    return
                if "line-in capture unavailable" not in nxt.lower():
                    self.problem, self.active = None, False      # it opened
                continue
            self._line(line, low)

    def _line(self, line: str, low: str) -> None:
        if "daemon starting (pid=" in low:
            self.reset()
            self.started = True
        elif "source: line-in" in low:
            self.source = "linein"
        elif " mode: " in low and self.source is None:
            self.source = "sck"
        elif "line-in fallback: on" in low:
            self.fb_setting = True
        elif "line-in fallback: off" in low:
            self.fb_setting = False
        elif "line-in capture unavailable" in low:
            ts = _ts(line)
            if ts is None:
                return
            m = _UNAVAILABLE.search(line)
            msg = m.group(1).strip() if m else "unavailable"
            self.problem = {"code": "linein_device_missing" if msg.startswith("no input device matching")
                            else "linein_unavailable", "message": msg[:300],
                            "since": self.problem["since"] if self.problem else ts}
            self.last_err, self.active = ts, False
        elif self.problem is not None and "recording this mac's call audio instead" in low:
            self.active = True
        elif self.problem is not None and "is connected again — staying" in low:
            self.problem["returned"] = _ts(line) or time.time()
        elif "line-in fallback: the call ended" in low:
            self.active = False
        elif "line-in:" in low and " is back " in low:
            self.problem, self.active = None, False
        elif " info chunk " in low and self.fb_setting is None and self.source != "sck":
            # 0.7.0: line-in delivered audio. (0.7.1 says "is back"; its
            # fallback call's chunks can land after the call ended.)
            self.problem = None

    def read(self, path: Path, pid: int | None) -> None:
        st = path.stat()
        key = (pid, st.st_ino)
        if key != self._key or st.st_size < self._offset:
            self._key, self._pending = key, []
            self.reset()
            lines, self._offset = _read_since_start(path, pid)
            self.feed(lines)
            return
        if st.st_size == self._offset:
            return
        with path.open("rb") as f:
            f.seek(self._offset)
            data = f.read(st.st_size - self._offset)
        cut = data.rfind(b"\n") + 1                # whole lines only
        self._offset += cut
        self.feed(data[:cut].decode("utf-8", errors="ignore").splitlines())

    def outage(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        p = self.problem
        if p is None:
            return None
        if not self.active and (self.last_err is None or now - self.last_err > LOG_FRESH_S):
            return None                            # not checked lately: paused, or the daemon is gone
        fb = None if self.fb_setting is None else ("active" if self.active else
                                                   "armed" if self.fb_setting else "off")
        return {**p, "fallback": fb}


_reader = LogReader()


def log_problem(path: Path | None = None, pid: int | None = None, now: float | None = None,
                reader: LogReader | None = None) -> dict | None:
    """The line-in outage the daemon log shows: {code, message, since,
    fallback, returned?}, or None."""
    r = reader or _reader
    try:
        r.read(path or _log_path(), pid)
    except OSError:
        return None
    return r.outage(now)


def status(daemon_pid: int | None = None, daemon_running: bool = True, log_path: Path | None = None,
           plist: Path | None = None, now: float | None = None, reader: LogReader | None = None) -> dict[str, Any]:
    """The settings (+ the log for an outage, while the daemon runs). Never raises."""
    try:
        out: dict[str, Any] = {"ok": True, "via": "settings", **configured(plist)}
        out["effective"] = out["configured"]
        out["problem"] = None
        if out["configured"] == "linein" and daemon_running:
            lp = log_problem(log_path, daemon_pid, now, reader)
            if lp:
                out["problem"] = {**lp, "device": out["device"]}
                out["effective"] = "sck" if lp.get("fallback") in ("active", "armed") else None
        return out
    except Exception as e:  # noqa: BLE001 — the menu must render
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "problem": None}


# ------------------------------------------------------------------ words

def _dev(src: dict) -> str:
    return src.get("device") or "the default input"


def what_is_wrong(src: dict) -> str:
    p = src.get("problem") or {}
    if p.get("code") == "linein_device_missing":
        return f"{_dev(src)} not connected"
    return f"line-in ({_dev(src)}) unavailable"


def recording_instead(src: dict, recording) -> bool:
    """Is this Mac's call audio being recorded in place of the missing
    interface right now? Only when meeting-capture says both."""
    return (src.get("problem") or {}).get("fallback") == "active" and recording is True


def warn(src: dict, recording=None) -> bool:
    """⚠ on the Source line: the configured interface can't be used — except
    once it is back and only the call in progress stays on this Mac's audio."""
    p = src.get("problem")
    return bool(p) and not (p.get("returned") and recording_instead(src, recording))


def text(src: dict, recording=None) -> str | None:
    """"Source: …" for the menu, `contorch status` and Details; None when
    unknown. Never says recording unless meeting-capture says recording."""
    if not src.get("ok"):
        return None
    if src.get("configured") != "linein":
        return "Source: this Mac's call audio"
    p = src.get("problem")
    if not p:
        return (f"Source: line-in — {_dev(src)} "
                f"(Me in {src.get('me_channel', 0) + 1} · Them in {src.get('them_channel', 1) + 1})")
    fb = p.get("fallback")
    if recording_instead(src, recording) and p.get("returned"):
        return f"Source: {_dev(src)} is back — recording this Mac until the call ends"
    if recording_instead(src, recording):
        tail = "recording this Mac instead"
    elif fb in ("active", "armed"):
        tail = "will record this Mac's calls instead"
    else:
        tail = "not recording"
    return f"Source: {what_is_wrong(src)} — {tail}"


def problem_since(src: dict) -> str | None:
    p = src.get("problem") or {}
    try:
        return datetime.fromtimestamp(float(p["since"])).strftime("%H:%M")
    except (KeyError, TypeError, ValueError, OSError):
        return None


def details(src: dict) -> list[str]:
    """Details-submenu / doctor lines under the Source line."""
    if not src.get("ok"):
        return [f"Source: unknown — {src.get('error')}"] if src.get("error") else []
    out = []
    if src.get("configured") == "linein":
        out.append(f"  configured: line-in — {_dev(src)}, Me = input {src.get('me_channel', 0) + 1}, "
                   f"Them = input {src.get('them_channel', 1) + 1}")
    else:
        out.append("  configured: this Mac's call audio")
    p = src.get("problem")
    if p:
        since = problem_since(src)
        out.append(f"  problem{' since ' + since if since else ''}: {str(p.get('message') or p.get('code'))[:90]}")
        fb = p.get("fallback")
        out.append("  fallback: " + {"active": "recording this Mac's call audio",
                                     "armed": "on — a call on this Mac is recorded from this Mac's audio",
                                     "off": "off (meeting-capture linein-fallback on)"}.get(
            fb, "none in meeting-capture 0.7.0 — nothing is recorded (brew upgrade meeting-capture)"))
    if src.get("via") == "settings":
        out.append("  (from the recorder's settings and its daemon log)")
    return out


class OutageNotifier:
    """One notification per outage: when a problem first shows up, and again
    only after it cleared."""

    def __init__(self) -> None:
        self._key = None

    def check(self, src: dict, recording=None) -> tuple[str, str] | None:
        if not src.get("ok"):
            return None
        p = src.get("problem")
        if not p:
            self._key = None
            return None
        key = (src.get("device"), p.get("code"), p.get("since"))
        if key == self._key:
            return None
        self._key = key
        title = f"{_dev(src)} isn't connected" if p.get("code") == "linein_device_missing" \
            else f"Line-in ({_dev(src)}) isn't working"
        if recording_instead(src, recording):
            body = "Recording this Mac's call audio instead."
        elif p.get("fallback") in ("active", "armed"):
            body = "Nothing comes in from it. A call on this Mac is recorded from this Mac's audio instead."
        else:
            body = ("Nothing is being recorded. Plug it in, or choose this Mac's call audio in "
                    "Recording settings…")
        return title, body
