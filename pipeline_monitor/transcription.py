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
    Intel), then PATH, then the per-user venv of a source install. That is
    what setup runs commands with (`meeting-capture stt apple`, …).
  * find_reader(): what a read runs. Homebrew's meeting-capture is a bash
    wrapper that, after an install or a `brew upgrade`, deletes and rebuilds
    ~/.meeting-capture/venv with pip — the venv the launchd recorder runs
    from, so a rebuild under a running recorder breaks its lazy imports
    mid-meeting, and two rebuilds at once break each other. A read must never
    start one. So when the CLI is that wrapper, reads run the venv's own
    `meeting-capture` (the code the recorder actually runs), with the
    wrapper's MEETING_CAPTURE_SYSAUDIO. Venv not built: no read, "run
    `meeting-capture install`". Its version stamp differs from the wrapper's
    version: read the venv (still what the recorder runs) and add a note.
    Only setup, in the foreground, reads through the wrapper (fresh()).
  * read(): runs it once, with a timeout that kills its whole process
    group. The result is one of
      ok       the JSON
      old      meeting-capture < 0.7: no `stt —json`, so a usage error
               (exit 2). It transcribes with Gemini only, so the result is
               never "on this Mac".
      error    it failed, timed out, printed something unparsable, used a
               schema this contorch doesn't know, or isn't set up yet. The
               result is unknown: never "on this Mac", and no privacy claim.
      missing  meeting-capture isn't installed.
  * current(wait): read() cached per (plist mtime, the CLI and the reader:
    resolved path + mtime + size, the venv's version stamp, the key file's
    mtime or absence). A `meeting-capture stt|language|mode` change rewrites
    the plist, and a `brew upgrade` or venv rebuild changes the CLI or the
    stamp, so both show up on the next refresh. Otherwise an answer lasts
    TTL_S (10 min; the Mac's language can change too) and an error RETRY_S
    (60 s). With wait=False (the menu bar's 5-second timer) a stale answer is
    refreshed on a background thread. "checking" is returned until the first
    answer arrives, so the menu never waits on a subprocess.
  * privacy(): the only place that says where audio goes. It uses nothing
    but the JSON's "live.active", "uploads" and "may_upload": "never leaves
    this Mac" only when may_upload is false. (may_upload is also true for
    auto with a key: meeting-capture hands a chunk to Gemini by itself when
    on-device transcription fails.)

Stdlib only: the menu bar and the `contorch` CLI share it.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
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
# meeting-capture's per-user venv (the brew wrapper's MEETING_CAPTURE_VENV
# default): what the recorder runs, and what reads run.
VENV = Path(os.environ.get("MEETING_CAPTURE_VENV") or HOME / ".meeting-capture" / "venv")
MC_VENV = VENV / "bin" / "meeting-capture"
MC_STAMP = VENV / ".formula-version"

SCHEMA = 1                    # the `meeting-capture stt --json` schema this module reads
# A read runs the venv's meeting-capture, never a venv build: ~0.2 s, and
# meeting-capture bounds each helper probe (20 s, at most two).
READ_TIMEOUT_S = 60.0         # contorch status / doctor, and the menu bar's thread
# Setup reads through the brew wrapper, in the foreground: its first run
# after an install or upgrade builds the venv with pip (minutes).
SETUP_READ_TIMEOUT_S = 900.0
TTL_S = 600.0
RETRY_S = 60.0

OLD_LABEL = "Gemini (meeting-capture < 0.7 — upgrade for on-device)"
LIVE_LABEL = "live: calls stream to Gemini"
ENGINES = ("apple", "gemini", "none")
NOT_BUILT = ("meeting-capture isn't set up yet (no ~/.meeting-capture/venv) — run `meeting-capture install` "
             "or `contorch setup`")


# ------------------------------------------------------------------ locate + read

def _which(name: str) -> str | None:
    return shutil.which(name)


def find_meeting_capture() -> str | None:
    """The meeting-capture CLI: brew's opt/ path, then PATH, then the venv."""
    for c in (*MC_CANDIDATES, _which("meeting-capture"), str(MC_VENV)):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


_STAMP_CHECK = re.compile(rb'\$\(cat "\$STAMP"[^)]*\)" != "([^"]+)"')
_SYSAUDIO_DEFAULT = re.compile(rb'MEETING_CAPTURE_SYSAUDIO:-([^}"]+)\}')


def _wrapper(path: str) -> bytes | None:
    """The text of a Homebrew meeting-capture wrapper, or None when `path`
    isn't one (a source install's console script, a test's fake)."""
    try:
        with open(path, "rb") as f:
            head = f.read(16384)
    except OSError:
        return None
    return head if head.startswith(b"#!") and b".formula-version" in head else None


def wrapper_version(path: str) -> str | None:
    """The formula version a Homebrew wrapper builds its venv for ("" if it
    can't be read from it), or None when `path` isn't that wrapper."""
    text = _wrapper(path)
    if text is None:
        return None
    m = _STAMP_CHECK.search(text)
    return m.group(1).decode("utf-8", "replace") if m else ""


def _stamp() -> str | None:
    try:
        return MC_STAMP.read_text().strip() or None
    except OSError:
        return None


def find_reader(mc: str | None = None) -> dict[str, Any] | None:
    """What a read runs — never the brew wrapper (module doc): {"argv0",
    "env", "mc", "note", "error"}, or None when meeting-capture isn't
    installed. "error" set: nothing to run."""
    mc = mc or find_meeting_capture()
    if not mc:
        return None
    text = _wrapper(mc)
    if text is None:
        return {"argv0": mc, "env": None, "mc": mc, "note": None, "error": None}
    version = wrapper_version(mc)
    if not (os.path.isfile(MC_VENV) and os.access(MC_VENV, os.X_OK)):
        return {"argv0": None, "env": None, "mc": mc, "note": None, "error": NOT_BUILT}
    env = None
    m = _SYSAUDIO_DEFAULT.search(text)          # the sysaudio the wrapper would export
    sysaudio = m.group(1).decode("utf-8", "replace") if m else ""
    if not os.environ.get("MEETING_CAPTURE_SYSAUDIO") and sysaudio and os.path.isfile(sysaudio):
        env = {**os.environ, "MEETING_CAPTURE_SYSAUDIO": sysaudio}
    stamp = _stamp()
    note = None
    if version and stamp != version:
        note = (f"meeting-capture {version} is installed, but the recorder still runs "
                f"{stamp or 'an older version'} — run `meeting-capture install` between meetings")
    return {"argv0": str(MC_VENV), "env": env, "mc": mc, "note": note, "error": None}


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
            or not isinstance(live.get("active"), bool) or not isinstance(live.get("requested"), bool)
            or not isinstance(data.get("may_upload"), bool)):
        return "`meeting-capture stt --json` lacks engine / ready / uploads / may_upload / live"
    return None


def _run(argv: list[str], timeout: float, env: dict | None) -> tuple[int, str, str]:
    """Run argv in its own process group; on timeout kill the whole group
    (whatever it started too) and raise subprocess.TimeoutExpired."""
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace", env=env, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        raise
    return p.returncode, out, err


def read(mc: str | None = None, timeout: float = READ_TIMEOUT_S, *,
         via_wrapper: bool = False) -> dict[str, Any]:
    """Ask meeting-capture once: {"status": ok|old|error|missing, "mc", "data",
    "error", "note"}. Runs find_reader(mc) — never the brew wrapper — unless
    via_wrapper (setup, in the foreground, where building the venv is fine)."""
    reader = find_reader(mc)
    if reader is None:
        return {"status": "missing", "mc": None, "data": None, "error": "meeting-capture is not installed",
                "note": None}
    mc, note = reader["mc"], reader["note"]
    argv0, env = (mc, None) if via_wrapper else (reader["argv0"], reader["env"])
    if via_wrapper:
        note = None                         # the wrapper brings the venv up to date first
    if not argv0:
        return {**_failed(mc, reader["error"]), "note": None}

    def failed(why: str) -> dict[str, Any]:
        return {**_failed(mc, why), "note": note}
    try:
        rc, out, err = _run([argv0, "stt", "--json"], timeout, env)
    except subprocess.TimeoutExpired:
        return failed(f"`meeting-capture stt --json` timed out after {timeout:.0f}s")
    except OSError as e:
        return failed(f"can't run {argv0}: {e.strerror or e}")
    if rc == 2:
        # argparse's usage error: no `stt` subcommand, or no --json. That is
        # meeting-capture < 0.7, which only transcribes with Gemini.
        return {"status": "old", "mc": mc, "data": None, "error": None, "note": note}
    if rc != 0:
        return failed(f"`meeting-capture stt --json` failed (exit {rc}): {_tail(err) or 'no detail'}")
    try:
        data = json.loads(out)
    except ValueError:
        return failed("`meeting-capture stt --json` printed something that isn't JSON")
    why = _problem(data)
    return failed(why) if why else {"status": "ok", "mc": mc, "data": data, "error": None, "note": note}


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
    run = (find_reader(mc) or {}).get("argv0") or mc
    return (_sig(PLIST), os.path.realpath(mc), _sig(mc), os.path.realpath(run), _sig(run),
            _sig(MC_STAMP), _sig(KEY_FILE))


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
                res = read(mc)
            except Exception as e:  # noqa: BLE001 — never kill the thread silently
                res = _failed(mc, f"{type(e).__name__}: {e}")
            _store(key, res)
        threading.Thread(target=_bg, name="stt-json", daemon=True).start()
    if hit:
        return hit[1]                       # stale, while the refresh runs
    return {"status": "checking", "mc": mc, "data": None, "error": None, "note": None}


# ------------------------------------------------------------------ what to show

FALLBACK_PRIVACY = "on this Mac — but if on-device transcription stops working, Gemini takes over (uploaded)"
NEVER_PRIVACY = "meeting audio never leaves this Mac"


def privacy(data: dict | None) -> str | None:
    """Where meeting audio goes. Based only on the JSON's "live.active",
    "uploads" and "may_upload" (README "Contract"); None when that isn't
    known. "never leaves this Mac" only when may_upload is false."""
    if not data:
        return None
    if data["live"]["active"]:
        return "every call streams to Google Gemini as it happens (live mode)"
    if data["uploads"]:
        return "meeting audio is uploaded to Google Gemini for transcription"
    if data["engine"] == "none":
        return "nothing transcribes yet: recordings wait on this Mac"
    if data["may_upload"]:
        return FALLBACK_PRIVACY
    return NEVER_PRIVACY


def privacy_fix(data: dict | None) -> str | None:
    """How to make privacy() say "never leaves this Mac" when audio may leave
    it only through auto's Gemini fallback (the JSON's on_device_only_hint);
    None otherwise."""
    if not data or not data["may_upload"] or data["uploads"] or data["live"]["active"]:
        return None
    hint = data.get("on_device_only_hint")
    return f"`{hint}` never uploads" if hint else None


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
        # audio leaves this Mac now: True / False / None (unknown)
        "leaves_mac": (d["uploads"] or d["live"]["active"]) if d else (True if status == "old" else None),
        # ... or can, with nobody changing a setting (the JSON's may_upload)
        "may_leave_mac": d["may_upload"] if d else (True if status == "old" else None),
        "privacy": privacy(d) if d else (
            "meeting audio is uploaded to Google Gemini for transcription" if status == "old" else None),
        "privacy_fix": privacy_fix(d),
        # the recorder runs an older meeting-capture than Homebrew installed
        "note": reading.get("note"),
    }
    out["label"] = label(out)
    return out


def current(wait: bool = True, ttl: float = TTL_S) -> dict[str, Any]:
    """view(cached_read()). Never raises."""
    try:
        return view(cached_read(wait=wait, ttl=ttl))
    except Exception as e:  # noqa: BLE001 — status/doctor/menu must still render
        return view(_failed(None, f"{type(e).__name__}: {e}"))


def fresh(mc: str | None = None, timeout: float = SETUP_READ_TIMEOUT_S) -> dict[str, Any]:
    """view(read()) right now through `mc` itself — the brew wrapper too,
    which may build the venv first — bypassing the cache. Setup only, in the
    foreground."""
    try:
        res = read(mc, timeout=timeout, via_wrapper=True)
    except Exception as e:  # noqa: BLE001
        res = _failed(mc, f"{type(e).__name__}: {e}")
    if res.get("mc"):
        _store(cache_key(res["mc"]), res)
    return view(res)
