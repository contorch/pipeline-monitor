"""How meetings are transcribed: asked from meeting-capture, never re-derived.

meeting-capture owns every rule here: which engine runs (on this Mac with
Apple's on-device speech, Gemini, or nothing yet), the language (it follows the
Mac), whether live mode streams calls to Gemini, which Gemini key its launchd
recorder can see, and how to fix what is missing. It answers with

    meeting-capture stt --json

one JSON object on stdout, schema 1. The fields are listed under "Contract" in
meeting-capture's README and in this repo's; change both sides together. This
module only reads that answer, for the menu bar and `contorch
setup|status|doctor`. It used to re-implement the rules and drifted: it kept
assuming en-US after meeting-capture's language started following the Mac, and
promised "audio never leaves this Mac" on a Dutch Mac where meeting-capture
picks Gemini.

  * find_meeting_capture(): the Homebrew opt/ path (Apple silicon, then
    Intel), then PATH, then the per-user venv of a source install.
  * read(): runs it once, with a timeout. The result is one of
      ok       the JSON
      old      meeting-capture < 0.7: no `stt --json`, so a usage error
               (exit 2). It transcribes with Gemini only, so the result is
               never "on this Mac".
      error    it failed, timed out, printed something unparsable, or used
               a schema this contorch doesn't know. The result is unknown:
               never "on this Mac", and no privacy claim.
      missing  meeting-capture isn't installed.
  * current(wait): read() cached per (plist mtime, the meeting-capture
    executable's resolved path + mtime + size, the key file's mtime or
    absence). A `meeting-capture stt|language|mode` change rewrites the plist
    and a `brew upgrade` replaces the executable, so both show up on the next
    refresh. Otherwise an answer lasts TTL_S (10 min; the Mac's language can
    change too) and an error RETRY_S (60 s). With wait=False (the menu bar's
    5-second timer) a stale answer is refreshed on a background thread.
    "checking" is returned until the first answer arrives, so the menu never
    waits on a subprocess.
  * privacy(): the only place that says where audio goes. It uses nothing
    but the JSON's "uploads" and "live.active".

The reads go through the brew wrapper, which builds meeting-capture's venv the
first time it runs after an install or a `brew upgrade` (a pip install, which
takes time). For that reason the timeouts are generous.

Stdlib only: the menu bar and the `contorch` CLI share it.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

HOME = Path.home()
PLIST = HOME / "Library" / "LaunchAgents" / "com.contorch.meeting-capture.plist"
KEY_FILE = HOME / ".config" / "google" / "key"

# Where `brew install contorch/tap/contorch` puts the CLI: the stable opt/
# path. launchd gives the menu bar no shell PATH.
MC_CANDIDATES = (
    "/opt/homebrew/opt/meeting-capture/bin/meeting-capture",
    "/usr/local/opt/meeting-capture/bin/meeting-capture",
)
MC_VENV = HOME / ".meeting-capture" / "venv" / "bin" / "meeting-capture"

SCHEMA = 1                    # the `meeting-capture stt --json` schema this module reads
# Normally ~0.2 s, and meeting-capture bounds its own helper probe (20 s). The
# long tail is the brew wrapper building meeting-capture's venv on its first
# run after an install or upgrade: a timeout must not kill that pip install.
READ_TIMEOUT_S = 300.0        # contorch status / doctor / setup
BACKGROUND_TIMEOUT_S = 600.0  # the menu bar's read, on its own thread
TTL_S = 600.0
RETRY_S = 60.0

OLD_LABEL = "Gemini (meeting-capture < 0.7 — upgrade for on-device)"
LIVE_LABEL = "live: calls stream to Gemini"
ENGINES = ("apple", "gemini", "none")


# ------------------------------------------------------------------ locate + read

def _which(name: str) -> str | None:
    return shutil.which(name)


def find_meeting_capture() -> str | None:
    """The meeting-capture CLI: brew's opt/ path, then PATH, then the venv."""
    for c in (*MC_CANDIDATES, _which("meeting-capture"), str(MC_VENV)):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _tail(text: str, n: int = 200) -> str:
    lines = (text or "").strip().splitlines()
    return lines[-1][:n] if lines else ""


def _failed(mc: str | None, why: str) -> dict[str, Any]:
    return {"status": "error", "mc": mc, "data": None, "error": why}


def _problem(data: Any) -> str | None:
    """Why `data` isn't a schema-1 answer this module can rely on (None if it is)."""
    if not isinstance(data, dict):
        return "`meeting-capture stt --json` didn't print a JSON object"
    if data.get("schema") != SCHEMA:
        return (f"`meeting-capture stt --json` answers in schema {data.get('schema')!r}, "
                f"this contorch reads schema {SCHEMA} — upgrade contorch")
    live = data.get("live")
    if (data.get("engine") not in ENGINES or not isinstance(data.get("ready"), bool)
            or not isinstance(data.get("uploads"), bool) or not isinstance(live, dict)
            or not isinstance(live.get("active"), bool) or not isinstance(live.get("requested"), bool)):
        return "`meeting-capture stt --json` lacks engine / ready / uploads / live"
    return None


def read(mc: str | None = None, timeout: float = READ_TIMEOUT_S) -> dict[str, Any]:
    """Ask meeting-capture once: {"status": ok|old|error|missing, "mc", "data", "error"}."""
    mc = mc or find_meeting_capture()
    if not mc:
        return {"status": "missing", "mc": None, "data": None, "error": "meeting-capture is not installed"}
    try:
        r = subprocess.run([mc, "stt", "--json"], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return _failed(mc, f"`meeting-capture stt --json` timed out after {timeout:.0f}s")
    except OSError as e:
        return _failed(mc, f"can't run {mc}: {e.strerror or e}")
    if r.returncode == 2:
        # argparse's usage error: no `stt` subcommand, or no --json. That is
        # meeting-capture < 0.7, which only transcribes with Gemini.
        return {"status": "old", "mc": mc, "data": None, "error": None}
    if r.returncode != 0:
        return _failed(mc, f"`meeting-capture stt --json` failed (exit {r.returncode}): "
                           f"{_tail(r.stderr) or 'no detail'}")
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return _failed(mc, "`meeting-capture stt --json` printed something that isn't JSON")
    why = _problem(data)
    return _failed(mc, why) if why else {"status": "ok", "mc": mc, "data": data, "error": None}


def command(hint: str | None, mc: str) -> list[str] | None:
    """A meeting-capture hint from the JSON ("meeting-capture language en-US")
    as argv for `mc`. Only `stt` and `language` commands are run; anything
    else gives None."""
    try:
        argv = shlex.split(hint or "")
    except ValueError:
        return None
    if len(argv) < 3 or argv[0] != "meeting-capture" or argv[1] not in ("stt", "language"):
        return None
    return [mc, *argv[1:]]


# ------------------------------------------------------------------ cache

_cache: dict[tuple, tuple[float, dict]] = {}
_inflight: set[tuple] = set()
_lock = threading.Lock()


def _sig(path) -> tuple | None:
    try:
        s = os.stat(path)
    except OSError:
        return None
    return (s.st_mtime_ns, s.st_size)


def cache_key(mc: str) -> tuple:
    return (_sig(PLIST), os.path.realpath(mc), _sig(mc), _sig(KEY_FILE))


def _fresh(entry: tuple[float, dict] | None, ttl: float) -> bool:
    if not entry:
        return False
    at, res = entry
    return time.monotonic() - at < (RETRY_S if res["status"] == "error" else ttl)


def _store(key: tuple, res: dict) -> None:
    with _lock:
        _cache.clear()                      # one meeting-capture, one configuration at a time
        _cache[key] = (time.monotonic(), res)
        _inflight.discard(key)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def cached_read(wait: bool = True, ttl: float = TTL_S) -> dict[str, Any]:
    """read(), re-run at most every `ttl` (RETRY_S after an error) or when the
    cache key changes. wait=False never blocks: a missing or stale answer is
    refreshed on a background thread, and until the first one arrives this
    returns status "checking"."""
    mc = find_meeting_capture()
    if not mc:
        return read(None)
    key = cache_key(mc)
    with _lock:
        hit = _cache.get(key)
    if _fresh(hit, ttl):
        return hit[1]
    if wait:
        res = read(mc)
        _store(key, res)
        return res
    with _lock:
        start = key not in _inflight
        if start:
            _inflight.add(key)
    if start:
        def _bg() -> None:
            try:
                res = read(mc, timeout=BACKGROUND_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001 — never kill the thread silently
                res = _failed(mc, f"{type(e).__name__}: {e}")
            _store(key, res)
        threading.Thread(target=_bg, name="stt-json", daemon=True).start()
    if hit:
        return hit[1]                       # stale, while the refresh runs
    return {"status": "checking", "mc": mc, "data": None, "error": None}


# ------------------------------------------------------------------ what to show

def privacy(data: dict | None) -> str | None:
    """Where meeting audio goes. Based only on the JSON's "uploads" and
    "live.active"; None when that isn't known."""
    if not data:
        return None
    if data["live"]["active"]:
        return "every call streams to Google Gemini as it happens (live mode)"
    if data["uploads"]:
        return "meeting audio is uploaded to Google Gemini for transcription"
    if data["engine"] == "none":
        return "nothing transcribes yet: recordings wait on this Mac"
    return "meeting audio never leaves this Mac"


def label(view: dict) -> str:
    """'on this Mac (en-US)' / 'Gemini' / 'unavailable — <why>', plus
    ' · live: calls stream to Gemini' while live mode streams; the fallbacks'
    own labels otherwise."""
    status = view["status"]
    if status == "checking":
        return "checking…"
    if status == "old":
        return OLD_LABEL
    if status != "ok":
        return f"unknown — {view.get('error') or status}"
    d = view["data"]
    if not d["ready"] or d["engine"] == "none":
        text = f"unavailable — {d.get('reason') or 'no engine can run'}"
    elif d["engine"] == "apple":
        text = f"on this Mac ({d.get('locale')})"
    else:
        text = "Gemini"
    if d["live"]["active"]:
        text += f" · {LIVE_LABEL}"
    return text


def view(reading: dict) -> dict[str, Any]:
    """A read() result as the menu bar, status, doctor and setup show it."""
    status, d = reading["status"], reading.get("data")
    out: dict[str, Any] = {
        "status": status,
        "ok": status != "missing",          # something to show (missing: the menu greys it out)
        "mc": reading.get("mc"),
        "error": reading.get("error"),
        "data": d,
        # apple | gemini | none from meeting-capture; an old meeting-capture
        # only has Gemini; error/checking: not known
        "engine": d["engine"] if d else ("gemini" if status == "old" else status),
        # nothing can transcribe: the menu's ⚠
        "attention": bool(d) and not d["ready"],
        # audio leaves this Mac: True / False / None (unknown)
        "leaves_mac": (d["uploads"] or d["live"]["active"]) if d else (True if status == "old" else None),
        "privacy": privacy(d) if d else (
            "meeting audio is uploaded to Google Gemini for transcription" if status == "old" else None),
    }
    out["label"] = label(out)
    return out


def current(wait: bool = True, ttl: float = TTL_S) -> dict[str, Any]:
    """view(cached_read()). Never raises."""
    try:
        return view(cached_read(wait=wait, ttl=ttl))
    except Exception as e:  # noqa: BLE001 — status/doctor/menu must still render
        return view(_failed(None, f"{type(e).__name__}: {e}"))


def fresh(mc: str | None = None, timeout: float = READ_TIMEOUT_S) -> dict[str, Any]:
    """view(read()) right now, bypassing the cache (setup, after a change)."""
    try:
        res = read(mc, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        res = _failed(mc, f"{type(e).__name__}: {e}")
    if res.get("mc"):
        _store(cache_key(res["mc"]), res)
    return view(res)
