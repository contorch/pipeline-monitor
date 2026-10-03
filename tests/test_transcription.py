"""Transcription engine: probe contract, setting semantics, caching — against a
fake helper (the real sysaudio is never run)."""
from __future__ import annotations

import os
import time

import pytest

from conftest import calls, fake_helper, write_plist
from pipeline_monitor import transcription as stt


# ------------------------------------------------------------ probe contract

@pytest.mark.parametrize("rc,stdout,stderr,status,usable", [
    (0, '{"available":true,"installed":true,"locale":"en-US","reason":""}', "", "ready", True),
    (69, '{"available":false,"reason":"needs macOS 26 or later","locale":"en-US"}', "", "unavailable", False),
    (69, '{"available":false,"reason":"needs Apple silicon","arch":"x86_64"}', "", "unavailable", False),
    (75, '{"available":true,"installed":false,"reason":"model for hi-IN not installed"}', "", "needs_model", False),
    (1, "", "unknown arg: transcribe\n", "old_helper", False),        # sysaudio <= meeting-capture 0.6
    (0, '{"available":false,"reason":"locale not supported"}', "", "unavailable", False),
    (0, "", "", "error", False),                                       # exit 0 but no JSON
    (1, "garbage", "boom\n", "error", False),
    (70, '{"available":true}', "", "error", False),
])
def test_parse_probe(rc, stdout, stderr, status, usable):
    p = stt.parse_probe(rc, stdout, stderr, "en-US")
    assert p["status"] == status and p["usable"] is usable
    assert p["reason"]


def test_parse_probe_keeps_the_helpers_reason_and_lists():
    p = stt.parse_probe(75, 'diag line\n{"available":true,"installed":false,"locale":"hi-IN",'
                            '"supported":["en-US","hi-IN"],"installed_locales":["en-US"],'
                            '"reason":"speech model for hi-IN not installed"}', "", "hi-IN")
    assert p["reason"] == "speech model for hi-IN not installed"
    assert p["supported"] == ["en-US", "hi-IN"] and p["installed_locales"] == ["en-US"]
    assert p["locale"] == "hi-IN" and p["installed"] is False


def test_probe_runs_the_contract_argv(tmp_path):
    binary, log = fake_helper(tmp_path, probe_rc=0)
    p = stt.probe(str(binary), "en-GB")
    assert p["status"] == "ready" and p["usable"]
    assert calls(log) == ["transcribe --probe --locale en-GB"]


def test_probe_old_sysaudio_is_unavailable_not_an_error(tmp_path):
    old = tmp_path / "old-sysaudio"
    old.write_text('#!/bin/sh\necho "unknown arg: $1" >&2\nexit 1\n')
    old.chmod(0o755)
    p = stt.probe(str(old), "en-US")
    assert p["status"] == "old_helper" and "upgrade meeting-capture" in p["reason"]


def test_probe_timeout_and_missing_binary(tmp_path):
    binary, _ = fake_helper(tmp_path, sleep=3)
    p = stt.probe(str(binary), "en-US", timeout=0.3)
    assert p["status"] == "error" and "timed out" in p["reason"]
    assert stt.probe(None, "en-US")["status"] == "no_helper"
    assert stt.probe(str(tmp_path / "nope"), "en-US")["status"] == "error"


def test_install_model(tmp_path):
    binary, log = fake_helper(tmp_path, probe_rc=75, install_rc=0,
                              install={"installed": True, "locale": "hi-IN", "seconds": 14.6})
    r = stt.install_model(str(binary), "hi-IN")
    assert r["ok"] and r["seconds"] == 14.6
    assert calls(log) == ["transcribe --install --locale hi-IN"]
    bad, _ = fake_helper(tmp_path, install_rc=69, install={"installed": False, "locale": "xx-XX"},
                         name="bad")
    r = stt.install_model(str(bad), "xx-XX")
    assert not r["ok"] and "not supported" in r["error"]


# ------------------------------------------------------------ settings

@pytest.mark.parametrize("env,setting", [
    ({}, "auto"),
    ({"MEETING_CAPTURE_STT": "apple"}, "apple"),
    ({"MEETING_CAPTURE_STT": " GEMINI "}, "gemini"),
    ({"MEETING_CAPTURE_STT": "whisper"}, "auto"),                     # unknown → default
    ({"MEETING_CAPTURE_TRANSCRIBER": "gemini"}, "auto"),              # legacy → auto
    ({"MEETING_CAPTURE_TRANSCRIBER": "whisper"}, "auto"),
    ({"MEETING_CAPTURE_TRANSCRIBER": "gemini", "MEETING_CAPTURE_STT": "apple"}, "apple"),
])
def test_setting_from_env(env, setting):
    assert stt.setting_from_env(env) == setting


def test_locale_default_and_override():
    assert stt.locale_from_env({}) == "en-US"
    assert stt.locale_from_env({"MEETING_CAPTURE_LOCALE": "hi-IN"}) == "hi-IN"


def test_gemini_key_is_what_the_daemon_would_see(tmp_path, monkeypatch):
    key = tmp_path / "key"
    assert not stt.has_gemini_key({}, key)
    monkeypatch.setenv("GOOGLE_API_KEY", "shell-only")       # the daemon does not get the shell env
    assert not stt.has_gemini_key({}, key)
    assert stt.has_gemini_key({"GEMINI_API_KEY": "x"}, key)
    key.write_text("  \n")
    assert not stt.has_gemini_key({}, key)
    key.write_text("AIza-test")
    assert stt.has_gemini_key({}, key)


READY = {"status": "ready", "usable": True, "reason": "ready"}
GONE = {"status": "unavailable", "usable": False, "reason": "needs macOS 26 or later"}
OLD = {"status": "old_helper", "usable": False, "reason": stt.OLD_HELPER_REASON}


@pytest.mark.parametrize("setting,probe,key,engine", [
    ("auto", READY, False, "apple"),
    ("auto", READY, True, "apple"),          # on-device wins; the key is optional
    ("auto", GONE, True, "gemini"),
    ("auto", OLD, True, "gemini"),
    ("auto", GONE, False, "none"),
    ("apple", READY, False, "apple"),
    ("apple", GONE, True, "none"),           # on-device only never uploads
    ("gemini", READY, True, "gemini"),
    ("gemini", READY, False, "none"),
    ("auto", {"status": "checking", "usable": False}, True, "checking"),
])
def test_resolve(setting, probe, key, engine):
    assert stt.resolve(setting, "en-US", probe, key)["engine"] == engine


def test_labels():
    assert stt.label({"engine": "apple", "locale": "en-US"}) == "on this Mac (en-US)"
    assert stt.label({"engine": "apple", "locale": "hi", "probe": {"locale": "hi-IN"}}) == "on this Mac (hi-IN)"
    assert stt.label({"engine": "gemini"}) == "Gemini"
    assert stt.label({"engine": "none", "reason": "no key"}) == "unavailable — no key"
    assert stt.label({"engine": "checking"}) == "checking…"


def test_find_helper_order(tmp_path, monkeypatch):
    a, _ = fake_helper(tmp_path, name="a")
    b, _ = fake_helper(tmp_path, name="b")
    c, _ = fake_helper(tmp_path, name="c")
    assert stt.find_helper({}) is None
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(c),))
    assert stt.find_helper({}) == str(c)
    assert stt.find_helper({"MEETING_CAPTURE_SYSAUDIO": str(b)}) == str(b)          # the daemon's pinned sysaudio
    assert stt.find_helper({"MEETING_CAPTURE_SYSAUDIO": str(tmp_path / "gone")}) == str(c)
    monkeypatch.setenv("MEETING_CAPTURE_TRANSCRIBE_BIN", str(a))                     # dev override wins
    assert stt.find_helper({"MEETING_CAPTURE_SYSAUDIO": str(b)}) == str(a)


# ------------------------------------------------------------ cache

def test_cached_probe_runs_once_and_reprobes_on_new_binary_or_locale(tmp_path):
    binary, log = fake_helper(tmp_path)
    for _ in range(3):
        assert stt.cached_probe(str(binary), "en-US")["usable"]
    assert len(calls(log)) == 1
    stt.cached_probe(str(binary), "hi-IN")                  # `meeting-capture language hi-IN`
    assert len(calls(log)) == 2
    st = binary.stat()                                      # brew upgrade → new helper
    os.utime(binary, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    stt.cached_probe(str(binary), "en-US")
    assert len(calls(log)) == 3


def test_cached_probe_ttl_and_failed_probe_retry(tmp_path, monkeypatch):
    binary, log = fake_helper(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(stt.time, "monotonic", lambda: clock[0])
    stt.cached_probe(str(binary), "en-US", ttl=900)
    clock[0] += 899
    stt.cached_probe(str(binary), "en-US", ttl=900)
    assert len(calls(log)) == 1
    clock[0] += 2
    stt.cached_probe(str(binary), "en-US", ttl=900)
    assert len(calls(log)) == 2
    broken, blog = fake_helper(tmp_path, probe_rc=1, probe={}, name="broken")
    stt.cached_probe(str(broken), "en-US", ttl=900)
    clock[0] += stt.PROBE_RETRY_S + 1                       # errors are retried soon, not in 15 min
    stt.cached_probe(str(broken), "en-US", ttl=900)
    assert len(calls(blog)) == 2


def test_cached_probe_without_waiting_returns_checking_then_the_answer(tmp_path):
    binary, log = fake_helper(tmp_path, sleep=0.3)
    first = stt.cached_probe(str(binary), "en-US", wait=False)
    assert first["status"] == "checking" and not first["usable"]
    again = stt.cached_probe(str(binary), "en-US", wait=False)   # no second probe while one runs
    assert again["status"] == "checking"
    deadline = time.time() + 10
    while time.time() < deadline:
        res = stt.cached_probe(str(binary), "en-US", wait=False)
        if res["status"] != "checking":
            break
        time.sleep(0.05)
    assert res["status"] == "ready"
    assert len(calls(log)) == 1


# ------------------------------------------------------------ current()

def test_current_reads_the_plist_and_probes(tmp_path, monkeypatch):
    binary, log = fake_helper(tmp_path, probe={"available": True, "installed": True, "locale": "hi-IN",
                                               "installed_locales": ["en-US", "hi-IN"]})
    plist = write_plist(stt.PLIST, {"MEETING_CAPTURE_STT": "apple", "MEETING_CAPTURE_LOCALE": "hi-IN",
                                    "MEETING_CAPTURE_SYSAUDIO": str(binary)})
    t = stt.current()
    assert t["installed"] and t["setting"] == "apple" and t["locale"] == "hi-IN"
    assert t["engine"] == "apple" and t["label"] == "on this Mac (hi-IN)"
    assert calls(log) == ["transcribe --probe --locale hi-IN"]
    write_plist(plist, {"MEETING_CAPTURE_STT": "gemini", "MEETING_CAPTURE_SYSAUDIO": str(binary)})
    stt.KEY_FILE.write_text("AIza-test")
    t = stt.current()
    assert t["engine"] == "gemini" and t["label"] == "Gemini"
    assert len(calls(log)) == 1                       # set to Gemini: the helper is not run


def test_current_auto_with_old_sysaudio_and_no_key_is_unavailable(tmp_path):
    old = tmp_path / "sysaudio"
    old.write_text('#!/bin/sh\necho "unknown arg: $1" >&2\nexit 1\n')
    old.chmod(0o755)
    write_plist(stt.PLIST, {"MEETING_CAPTURE_TRANSCRIBER": "gemini", "MEETING_CAPTURE_SYSAUDIO": str(old)})
    t = stt.current()
    assert t["setting"] == "auto" and "legacy" in t["configured"]
    assert t["engine"] == "none"
    assert t["label"].startswith("unavailable — this sysaudio predates on-device transcription")


def test_missing_model_says_how_to_install_it():
    p = {"status": "needs_model", "usable": False, "reason": "speech model for hi-IN not installed"}
    r = stt.resolve("apple", "hi-IN", p, False)
    assert r["engine"] == "none" and "meeting-capture language hi-IN" in r["reason"]
    assert stt.resolve("auto", "hi-IN", p, True)["engine"] == "gemini"


# ------------------------------------------------------------ live mode
# meeting-capture's cli.live_mode_blocker(): live mode streams every call to
# Gemini unless the source is line-in, stt is apple, or there is no key.

LIVE = {"MEETING_CAPTURE_MODE": "live"}


@pytest.mark.parametrize("env,key,blocker", [
    ({}, True, None),                                                    # stt auto streams too
    ({"MEETING_CAPTURE_STT": "gemini"}, True, None),
    ({"MEETING_CAPTURE_STT": "apple"}, True, stt.LIVE_NEVER_UPLOADS),
    ({}, False, stt.LIVE_NEEDS_KEY),
    ({"MEETING_CAPTURE_SOURCE": "linein"}, True, stt.LIVE_LINEIN),
    ({"MEETING_CAPTURE_SOURCE": "linein", "MEETING_CAPTURE_STT": "apple"}, False, stt.LIVE_LINEIN),
])
def test_live_blocker_mirrors_meeting_capture(env, key, blocker):
    assert stt.live_blocker({**LIVE, **env}, key) == blocker
    state = stt.live_state({**LIVE, **env}, key)
    assert state == {"requested": True, "streaming": blocker is None, "blocker": blocker}


@pytest.mark.parametrize("mode", [None, "batch", "", "LIVEISH"])
def test_live_not_requested(mode):
    env = {} if mode is None else {"MEETING_CAPTURE_MODE": mode}
    assert stt.live_state(env, True) == {"requested": False, "streaming": False, "blocker": None}
    assert stt.live_requested({"MEETING_CAPTURE_MODE": " Live "})


def test_current_live_mode_with_a_key_file_says_calls_stream_to_gemini(tmp_path):
    """The review's repro: live mode, stt auto, a key file. Before the fix the
    menu, status and doctor said "on this Mac (en-US)" while every call was
    streamed to Gemini."""
    binary, _ = fake_helper(tmp_path)
    write_plist(stt.PLIST, {**LIVE, "MEETING_CAPTURE_SYSAUDIO": str(binary)})
    stt.KEY_FILE.write_text("AIza-test")
    stt.KEY_FILE.chmod(0o600)
    t = stt.current()
    assert t["engine"] == "apple"                                       # batch engine is unchanged
    assert t["live"] == {"requested": True, "streaming": True, "blocker": None}
    assert t["label"] == "on this Mac (en-US) · live: calls stream to Gemini"


def test_current_live_mode_key_in_the_plist_env_counts_for_the_running_recorder(tmp_path):
    binary, _ = fake_helper(tmp_path)
    write_plist(stt.PLIST, {**LIVE, "MEETING_CAPTURE_SYSAUDIO": str(binary), "GEMINI_API_KEY": "AIza-x"})
    assert stt.current()["live"]["streaming"]


@pytest.mark.parametrize("extra,blocker", [
    ({}, stt.LIVE_NEEDS_KEY),                                            # no key anywhere
    ({"MEETING_CAPTURE_STT": "apple"}, stt.LIVE_NEVER_UPLOADS),
    ({"MEETING_CAPTURE_SOURCE": "linein"}, stt.LIVE_LINEIN),
])
def test_current_live_mode_that_runs_batch_keeps_the_plain_label(tmp_path, monkeypatch, extra, blocker):
    binary, _ = fake_helper(tmp_path)
    write_plist(stt.PLIST, {**LIVE, "MEETING_CAPTURE_SYSAUDIO": str(binary), **extra})
    if extra:
        stt.KEY_FILE.write_text("AIza-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "shell-only")                  # the recorder never sees it
    t = stt.current()
    assert t["live"] == {"requested": True, "streaming": False, "blocker": blocker}
    assert t["label"] == "on this Mac (en-US)"


def test_current_live_mode_with_gemini_engine(tmp_path):
    write_plist(stt.PLIST, {**LIVE, "MEETING_CAPTURE_STT": "gemini"})
    stt.KEY_FILE.write_text("AIza-test")
    assert stt.current()["label"] == "Gemini · live: calls stream to Gemini"
