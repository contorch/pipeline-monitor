"""Which speech-to-text engine meeting-capture transcribes with, read cheaply.

meeting-capture (>= 0.7) can transcribe on this Mac with Apple's on-device
speech model (SpeechTranscriber: macOS 26+, Apple silicon) through its signed
`sysaudio` helper, or with Gemini. Its settings live in the launchd plist env
of com.contorch.meeting-capture, written only by meeting-capture itself
(`meeting-capture stt auto|apple|gemini`, `meeting-capture language LOCALE`):

    MEETING_CAPTURE_STT     auto (default) | apple | gemini
    MEETING_CAPTURE_LOCALE  default en-US
    MEETING_CAPTURE_TRANSCRIBER=gemini|whisper (legacy) counts as auto

    auto   = on this Mac when the helper's probe says it is usable now (model
             installed), else Gemini when a key resolves, else unavailable.
    apple  = on this Mac only; never uploads (audio is kept and retried).
    gemini = Gemini (needs a key).

Whether the Mac can do it comes from the helper's own probe (stable JSON
contract, shared with meeting-capture):

    sysaudio transcribe --probe --locale L
      -> one JSON line {"available", "reason", "os", "arch", "locale",
         "installed", "supported", "installed_locales"}
      exit 0 usable now · 69 unusable (macOS < 26, Intel, locale unsupported)
      · 75 supported but the model is not installed.
    Older sysaudio builds print "unknown arg: transcribe" and exit non-zero:
    that means unavailable.

How this is read (pipeline-monitor never edits the plist):
  * setting, locale: the plist env (a file read; same as the capture mode).
  * on-device availability: one probe of the helper, cached per
    (helper path, its mtime + size, locale) for PROBE_TTL_S. Upgrading
    meeting-capture (new helper binary) or `meeting-capture language` (new
    locale) changes the cache key, so those show up on the next refresh; the
    menu bar never runs the helper on its 5-second timer otherwise, and runs
    it off the main thread (`wait=False`) so a slow probe cannot freeze the
    menu. The daemon's log is NOT parsed for this.
  * Gemini key: what the daemon would see — GOOGLE_API_KEY/GEMINI_API_KEY in
    its plist env, else ~/.config/google/key.

Stdlib only: `contorch` (setup/doctor/status) and the menu bar share it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

HOME = Path.home()
PLIST = HOME / "Library" / "LaunchAgents" / "com.contorch.meeting-capture.plist"
KEY_FILE = HOME / ".config" / "google" / "key"

STT_ENV = "MEETING_CAPTURE_STT"
LOCALE_ENV = "MEETING_CAPTURE_LOCALE"
LEGACY_ENV = "MEETING_CAPTURE_TRANSCRIBER"
BIN_ENV = "MEETING_CAPTURE_TRANSCRIBE_BIN"   # dev/testing override (contract)
SYSAUDIO_ENV = "MEETING_CAPTURE_SYSAUDIO"

SETTINGS = ("auto", "apple", "gemini")
DEFAULT_SETTING = "auto"
DEFAULT_LOCALE = "en-US"

# sysexits(3), as the helper uses them.
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_TEMPFAIL = 75

# Where Homebrew puts the helper; the stable opt/ path is the one the
# Screen Recording grant and the plist use.
BREW_HELPERS = (
    "/opt/homebrew/opt/meeting-capture/bin/sysaudio",
    "/usr/local/opt/meeting-capture/bin/sysaudio",
)

PROBE_TIMEOUT_S = 20.0
PROBE_TTL_S = 15 * 60       # a definitive answer (ready / unavailable / needs model)
PROBE_RETRY_S = 60          # a probe that failed or timed out: try again soon
INSTALL_TIMEOUT_S = 900     # a new language family can be a ~250 MB download

OLD_HELPER_REASON = "this sysaudio predates on-device transcription — upgrade meeting-capture"
NO_HELPER_REASON = "sysaudio not found — install meeting-capture"


# ------------------------------------------------------------------ config

def plist_env(plist: Path | None = None) -> dict:
    """EnvironmentVariables of the meeting-capture launchd agent ({} if none)."""
    import plistlib
    try:
        payload = plistlib.loads(Path(plist or PLIST).read_bytes())
        env = payload.get("EnvironmentVariables") or {}
        return {str(k): str(v) for k, v in env.items()}
    except Exception:
        return {}


def setting_from_env(env: dict) -> str:
    """auto | apple | gemini. Unset, unknown, or only the legacy
    MEETING_CAPTURE_TRANSCRIBER (gemini|whisper) → auto."""
    raw = str(env.get(STT_ENV, "")).strip().lower()
    return raw if raw in SETTINGS else DEFAULT_SETTING


def locale_from_env(env: dict) -> str:
    return str(env.get(LOCALE_ENV, "")).strip() or DEFAULT_LOCALE


def has_gemini_key(env: dict, key_file: Path | None = None) -> bool:
    """Would the daemon find a Gemini key? Its own environment (the plist
    env), then the key file — the same order meeting-capture resolves it."""
    if str(env.get("GOOGLE_API_KEY", "")).strip() or str(env.get("GEMINI_API_KEY", "")).strip():
        return True
    try:
        f = Path(key_file or KEY_FILE)
        return f.is_file() and f.read_text(encoding="utf-8").strip() != ""
    except OSError:
        return False


def _which(name: str) -> str | None:
    return shutil.which(name)


def find_helper(env: dict | None = None) -> str | None:
    """The `sysaudio` that does `transcribe`, in the order meeting-capture
    resolves it: MEETING_CAPTURE_TRANSCRIBE_BIN (this process, then the
    daemon's plist env), then the sysaudio the daemon is pinned to
    (MEETING_CAPTURE_SYSAUDIO in the plist), then this environment's
    MEETING_CAPTURE_SYSAUDIO, the Homebrew opt/ path, and PATH."""
    env = plist_env() if env is None else env
    candidates = [
        os.environ.get(BIN_ENV),
        env.get(BIN_ENV),
        env.get(SYSAUDIO_ENV),
        os.environ.get(SYSAUDIO_ENV),
        *BREW_HELPERS,
        _which("sysaudio"),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return str(c)
    return None


# ------------------------------------------------------------------ probe

def _last_json(stdout: str) -> dict | None:
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None
    return None


def _result(status: str, reason: str, locale: str, info: dict | None = None,
            exit_code: int | None = None, helper: str | None = None) -> dict[str, Any]:
    info = info or {}
    return {
        # ready | needs_model | unavailable | old_helper | no_helper | error
        # (+ checking / skipped, which never come from the helper itself)
        "status": status,
        "usable": status == "ready",
        "reason": reason,
        "locale": str(info.get("locale") or locale),
        "installed": bool(info.get("installed", status == "ready")),
        "supported": list(info.get("supported") or []),
        "installed_locales": list(info.get("installed_locales") or []),
        "os": str(info.get("os") or ""),
        "arch": str(info.get("arch") or ""),
        "exit": exit_code,
        "helper": helper,
        "checked_at": time.time(),
    }


def parse_probe(returncode: int, stdout: str, stderr: str, locale: str,
                helper: str | None = None) -> dict[str, Any]:
    """Map one `sysaudio transcribe --probe` run onto a status."""
    err = (stderr or "").strip()
    if returncode != 0 and "unknown arg" in err.lower():
        return _result("old_helper", OLD_HELPER_REASON, locale, exit_code=returncode, helper=helper)
    info = _last_json(stdout)
    reason = str((info or {}).get("reason") or "").strip()
    if returncode == 0:
        if info is None:
            return _result("error", "the helper's probe printed no status", locale,
                           exit_code=returncode, helper=helper)
        if info.get("available") is False:
            return _result("unavailable", reason or "on-device transcription is not available on this Mac",
                           locale, info, returncode, helper)
        return _result("ready", reason or "ready", locale, info, returncode, helper)
    if returncode == EX_UNAVAILABLE:
        return _result("unavailable", reason or "on-device transcription is not available on this Mac",
                       locale, info, returncode, helper)
    if returncode == EX_TEMPFAIL:
        return _result("needs_model", reason or f"the speech model for {locale} is not installed",
                       locale, info, returncode, helper)
    tail = err.splitlines()[-1][:160] if err else ""
    return _result("error", reason or f"probe failed (exit {returncode}){': ' + tail if tail else ''}",
                   locale, info, returncode, helper)


def probe(helper: str | None, locale: str, timeout: float = PROBE_TIMEOUT_S) -> dict[str, Any]:
    """Run the helper's probe once (no cache)."""
    if not helper:
        return _result("no_helper", NO_HELPER_REASON, locale)
    try:
        r = subprocess.run([helper, "transcribe", "--probe", "--locale", locale],
                           capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return _result("error", f"the helper's probe timed out after {timeout:.0f}s", locale, helper=helper)
    except OSError as e:
        return _result("error", f"could not run {helper}: {e.strerror or e}", locale, helper=helper)
    return parse_probe(r.returncode, r.stdout, r.stderr, locale, helper)


def install_model(helper: str, locale: str, timeout: float = INSTALL_TIMEOUT_S) -> dict[str, Any]:
    """`sysaudio transcribe --install --locale L` — download/reserve the model."""
    try:
        r = subprocess.run([helper, "transcribe", "--install", "--locale", locale],
                           capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout:.0f}s", "exit": None}
    except OSError as e:
        return {"ok": False, "error": str(e), "exit": None}
    info = _last_json(r.stdout) or {}
    ok = r.returncode == 0 and info.get("installed", True) is not False
    err = (r.stderr or "").strip()
    if r.returncode != 0 and "unknown arg" in err.lower():
        err = OLD_HELPER_REASON
    elif r.returncode == EX_UNAVAILABLE and not err:
        err = f"{locale} is not supported on this Mac"
    return {"ok": ok, "exit": r.returncode, "seconds": info.get("seconds"),
            "error": "" if ok else (err.splitlines()[-1][:200] if err else f"exit {r.returncode}")}


# ------------------------------------------------------------------ cache

_cache: dict[tuple, tuple[float, dict]] = {}
_inflight: set[tuple] = set()
_lock = threading.Lock()


def _cache_key(helper: str, locale: str) -> tuple:
    try:
        s = os.stat(helper)
        sig: tuple | None = (s.st_mtime_ns, s.st_size)
    except OSError:
        sig = None
    return (helper, sig, locale)


def _fresh(entry: tuple[float, dict] | None, ttl: float) -> bool:
    if not entry:
        return False
    at, res = entry
    limit = PROBE_RETRY_S if res.get("status") == "error" else ttl
    return time.monotonic() - at < limit


def _store(key: tuple, res: dict) -> None:
    with _lock:
        _cache[key] = (time.monotonic(), res)
        _inflight.discard(key)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def cached_probe(helper: str | None, locale: str, ttl: float = PROBE_TTL_S,
                 wait: bool = True) -> dict[str, Any]:
    """The probe result, re-run at most every `ttl` seconds per (helper
    binary, locale). With wait=False a missing/stale answer is refreshed on
    a background thread; until the first one lands this returns status
    "checking"."""
    if not helper:
        return _result("no_helper", NO_HELPER_REASON, locale)
    key = _cache_key(helper, locale)
    with _lock:
        hit = _cache.get(key)
    if _fresh(hit, ttl):
        return hit[1]
    if wait:
        res = probe(helper, locale)
        _store(key, res)
        return res
    with _lock:
        start = key not in _inflight
        if start:
            _inflight.add(key)
    if start:
        def _bg() -> None:
            try:
                res = probe(helper, locale)
            except Exception as e:  # noqa: BLE001 — never kill the thread silently
                res = _result("error", f"probe failed: {e}", locale, helper=helper)
            _store(key, res)
        threading.Thread(target=_bg, name="stt-probe", daemon=True).start()
    if hit:
        return hit[1]   # stale but better than nothing while the refresh runs
    return _result("checking", "checking…", locale, helper=helper)


# ------------------------------------------------------------------ decision

def resolve(setting: str, locale: str, probe_result: dict, has_key: bool) -> dict[str, Any]:
    """The engine meeting-capture uses for this setting: apple | gemini |
    none (nothing can transcribe; audio is kept and retried) | checking."""
    p = probe_result or {}
    why = p.get("reason") or "on-device transcription is unavailable"
    if p.get("status") == "needs_model":
        why += f" (install it: meeting-capture language {locale})"
    if setting == "gemini":
        if has_key:
            return {"engine": "gemini", "reason": "set to Gemini"}
        return {"engine": "none", "reason": "set to Gemini, but there is no Gemini API key"}
    if p.get("status") == "checking":
        return {"engine": "checking", "reason": "checking…"}
    if p.get("usable"):
        return {"engine": "apple",
                "reason": "on-device only" if setting == "apple" else "available on this Mac"}
    if setting == "apple":
        return {"engine": "none", "reason": f"{why} (set to on-device only — audio is kept until it works)"}
    if has_key:
        return {"engine": "gemini", "reason": f"on-device unavailable: {why}"}
    return {"engine": "none", "reason": f"{why}, and there is no Gemini API key"}


def label(state: dict) -> str:
    """'on this Mac (en-US)' / 'Gemini' / 'unavailable — <reason>' / 'checking…'."""
    engine = state.get("engine")
    if engine == "apple":
        loc = (state.get("probe") or {}).get("locale") or state.get("locale") or DEFAULT_LOCALE
        return f"on this Mac ({loc})"
    if engine == "gemini":
        return "Gemini"
    if engine == "checking":
        return "checking…"
    return f"unavailable — {state.get('reason') or 'unknown'}"


def current(wait: bool = True, ttl: float = PROBE_TTL_S, env: dict | None = None,
            plist: Path | None = None) -> dict[str, Any]:
    """Everything the menu bar / doctor show about transcription.
    The helper is probed only when the setting can use it (auto / apple)."""
    plist = Path(plist or PLIST)
    env = plist_env(plist) if env is None else env
    raw = str(env.get(STT_ENV, "")).strip()
    setting = setting_from_env(env)
    loc = locale_from_env(env)
    key = has_gemini_key(env)
    helper = find_helper(env)
    if setting == "gemini":
        p = _result("skipped", "not checked (set to Gemini)", loc, helper=helper)
    else:
        p = cached_probe(helper, loc, ttl=ttl, wait=wait)
    state = resolve(setting, loc, p, key)
    out = {
        "ok": True,
        "installed": plist.is_file(),
        "setting": setting,
        "configured": raw or (f"auto (legacy {LEGACY_ENV}={env[LEGACY_ENV]})" if env.get(LEGACY_ENV) else "auto"),
        "locale": loc,
        "has_key": key,
        "helper": helper,
        "probe": p,
        **state,
    }
    out["label"] = label(out)
    return out
