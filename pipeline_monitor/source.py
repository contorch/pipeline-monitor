"""Where the recorder's audio comes from, and whether that works — the
menu's "Source:" line, its ⚠ and the one notification per outage.

meeting-capture says it itself (`meeting-capture status --json`, ≥ 0.8:
`source`, `input`, `effective_source`, `linein_fallback`, `problem`; its
README "Contract"). pipeline-monitor only words it.

meeting-capture 0.7 (the release on Homebrew today) has no `status --json`:
the source comes from its settings (plist / env, through mcconfig) and an
outage from its daemon log — 0.7 retries a missing interface every 30 s and
logs `line-in: listening on the interface` followed at once by
`ERROR line-in capture unavailable: … — retrying in 30s`. 0.7 has no
fallback: while that lasts nothing is recorded. The same log reading stands
in when a newer meeting-capture's daemon hasn't written the fields (an older
daemon still running).

    status(wait) -> {"ok", "via": "owner"|"settings", "configured": "sck"|"linein",
                     "device", "me_channel", "them_channel",
                     "effective": "linein"|"sck"|None,
                     "problem": None | {code, device, message, since, fallback}}

`problem.fallback`: "active" (recording this Mac's call audio instead),
"armed" (a call would be), "off" (the setting is off), None (meeting-capture
0.7: no fallback). `problem.returned`: the interface came back during a
fallback call (meeting-capture switches back when the call ends).
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import mcconfig, ownerstate

# 0.7 retries every 30 s: an outage is still on if its newest line is this recent.
LOG_FRESH_S = 95
TAIL_BYTES = 65536
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_UNAVAILABLE = re.compile(r"line-in capture unavailable: (.*?)(?: — retrying in \d+s)?\s*$")


def _log_path() -> Path:
    return Path.home() / ".meeting-capture" / "daemon.log"


def _int(v, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def configured(wait: bool = False) -> dict[str, Any]:
    """The source settings (meeting-capture's `config --json`; 0.7: its plist)."""
    src = str(mcconfig.setting("source", "sck", wait=wait) or "sck").strip().lower()
    return {"configured": "linein" if src == "linein" else "sck",
            "device": (mcconfig.setting("input_device", None, wait=wait) or "").strip() or None,
            "me_channel": _int(mcconfig.setting("me_channel", "0", wait=wait), 0),
            "them_channel": _int(mcconfig.setting("them_channel", "1", wait=wait), 1)}


def _age(line: str, now: float) -> float | None:
    m = _TS.match(line)
    if not m:
        return None
    try:
        return now - datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def log_problem(path: Path | None = None, now: float | None = None) -> dict | None:
    """A line-in outage the daemon log shows (meeting-capture 0.7, or a daemon
    that predates the state fields): {code, message, since, last_age_s}, or
    None. `since` is the first failure of the current run of them in the
    log's tail (so at most ~45 min back for 0.7)."""
    now = time.time() if now is None else now
    p = path or _log_path()
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            lines = f.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    since_age = last_age = message = None
    for i, line in enumerate(lines):
        low = line.lower()
        if "line-in capture unavailable" in low:
            age = _age(line, now)
            if age is None:
                continue
            m = _UNAVAILABLE.search(line)
            message = m.group(1).strip() if m else "unavailable"
            if since_age is None:
                since_age = age
            last_age = age
        elif "line-in: listening on the interface" in low:
            nxt = next((l for l in lines[i + 1:i + 3] if _TS.match(l)), "")
            if "line-in capture unavailable" not in nxt.lower():
                since_age = last_age = message = None          # it opened
        elif ("line-in:" in low and " is back " in low) or " info chunk " in low:
            since_age = last_age = message = None
    if last_age is None or last_age > LOG_FRESH_S:
        return None
    code = "linein_device_missing" if message.startswith("no input device matching") else "linein_unavailable"
    return {"code": code, "message": message[:300], "since": now - since_age, "last_age_s": int(last_age)}


def status(wait: bool = False, log_path: Path | None = None) -> dict[str, Any]:
    """meeting-capture's answer when its daemon gives one, else the settings
    (+ the log for an outage). Never raises."""
    try:
        r = ownerstate.recorder(wait)
        d = r.get("data") or {}
        if r["status"] == "ok" and "effective_source" in d and d.get("reason") != "daemon_not_running":
            inp = d.get("input") or {}
            return {"ok": True, "via": "owner",
                    "configured": "linein" if d.get("source") == "linein" else "sck",
                    "device": inp.get("device"), "me_channel": _int(inp.get("me_channel"), 0),
                    "them_channel": _int(inp.get("them_channel"), 1),
                    "effective": d.get("effective_source"), "fallback_setting": d.get("linein_fallback"),
                    "problem": d.get("problem") or None}
        out: dict[str, Any] = {"ok": True, "via": "settings", **configured(wait)}
        out["effective"] = out["configured"]
        out["problem"] = None
        # A daemon that can't say (0.7, an older daemon): its log. Not while
        # meeting-capture is still being asked (a newer one's answer would
        # word it differently), nor for a daemon that isn't running.
        if out["configured"] == "linein" and r["status"] != "checking" \
                and d.get("reason") != "daemon_not_running":
            lp = log_problem(log_path)
            if lp:
                out["problem"] = {**lp, "device": out["device"], "fallback": None}
                out["effective"] = None
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
                                     "off": "off (meeting-capture config set linein_fallback 1)"}.get(
            fb, "none in this meeting-capture version — nothing is recorded"))
    if src.get("via") == "settings":
        out.append("  (from the settings and the daemon log — this meeting-capture can't report it)")
    return out


class OutageNotifier:
    """One notification per outage: when a problem first shows up, and again
    only after it cleared (or meeting-capture reports a new outage)."""

    def __init__(self) -> None:
        self._key = None

    def check(self, src: dict, recording=None) -> tuple[str, str] | None:
        if not src.get("ok"):
            return None
        p = src.get("problem")
        if not p:
            self._key = None
            return None
        # the log's "since" moves as its tail scrolls: only the owner's is an id
        key = (src.get("device"), p.get("code"), p.get("since") if src.get("via") == "owner" else None)
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
