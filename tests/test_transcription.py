"""The transcription reader: pipeline-monitor asks meeting-capture
(`meeting-capture stt --json`) and never re-derives its rules. Against a fake
meeting-capture that prints canned answers; nothing real is run."""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

from conftest import DUTCH, LIVE_ON, NEEDS_MODEL, UNAVAILABLE, mc_json, write_plist
from pipeline_monitor import transcription as stt


# ------------------------------------------------------------ finding meeting-capture

def _exe(path, executable=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755 if executable else 0o644)
    return str(path)


def test_find_meeting_capture_order(tmp_path, monkeypatch):
    arm, intel = _exe(tmp_path / "arm" / "meeting-capture"), _exe(tmp_path / "intel" / "meeting-capture")
    on_path, venv = _exe(tmp_path / "path" / "meeting-capture"), _exe(tmp_path / "venv" / "meeting-capture")
    assert stt.find_meeting_capture() is None
    monkeypatch.setattr(stt, "MC_VENV", venv)
    assert stt.find_meeting_capture() == venv
    monkeypatch.setattr(stt, "_which", lambda name: on_path if name == "meeting-capture" else None)
    assert stt.find_meeting_capture() == on_path
    monkeypatch.setattr(stt, "MC_CANDIDATES", (str(tmp_path / "gone"), intel))
    assert stt.find_meeting_capture() == intel
    monkeypatch.setattr(stt, "MC_CANDIDATES", (arm, intel))
    assert stt.find_meeting_capture() == arm
    monkeypatch.setattr(stt, "MC_CANDIDATES", (_exe(tmp_path / "noexec" / "meeting-capture", False),))
    assert stt.find_meeting_capture() == on_path                 # not executable: skipped


# ------------------------------------------------------------ reading the answer

def test_read_runs_stt_json_and_keeps_the_answer(fake_mc):
    r = stt.read()
    assert r["status"] == "ok" and r["data"] == mc_json() and r["mc"] == str(fake_mc.path)
    assert fake_mc.calls() == [["stt", "--json"]]


def test_on_this_mac(fake_mc):
    v = stt.current()
    assert (v["status"], v["engine"], v["label"]) == ("ok", "apple", "on this Mac (en-US)")
    assert v["leaves_mac"] is False and v["privacy"] == "meeting audio never leaves this Mac"
    assert v["attention"] is False and v["ok"] is True


def test_the_dutch_mac_with_a_key_is_never_called_on_device(fake_mc):
    """The drift this replaces: pipeline-monitor assumed en-US and said "audio
    never leaves this Mac" while meeting-capture picked Gemini."""
    fake_mc.set(json=mc_json(**DUTCH))
    v = stt.current()
    assert (v["engine"], v["label"]) == ("gemini", "Gemini")
    assert v["leaves_mac"] is True and v["privacy"] == "meeting audio is uploaded to Google Gemini for transcription"
    assert "never leaves" not in v["privacy"] and "on this Mac" not in v["label"]


def test_live_mode_streams_even_when_batch_runs_here(fake_mc):
    fake_mc.set(json=mc_json(**LIVE_ON))
    v = stt.current()
    assert v["label"] == "on this Mac (en-US) · live: calls stream to Gemini"
    assert v["leaves_mac"] is True and "streams to Google Gemini" in v["privacy"]


def test_live_requested_but_blocked_keeps_the_plain_label(fake_mc):
    fake_mc.set(json=mc_json(live={"requested": True, "active": False,
                                   "blocker": "live mode streams to Gemini and no Google API key is set"}))
    v = stt.current()
    assert v["label"] == "on this Mac (en-US)" and v["leaves_mac"] is False


@pytest.mark.parametrize("over", [NEEDS_MODEL, UNAVAILABLE,
                                  dict(choice="gemini", engine="gemini", ready=False, uploads=True,
                                       reason="chosen with `meeting-capture stt gemini`, but no Google API key")])
def test_nothing_can_transcribe_needs_attention(fake_mc, over):
    fake_mc.set(json=mc_json(**over))
    v = stt.current()
    assert v["attention"] is True and v["label"] == f"unavailable — {over['reason']}"


@pytest.mark.parametrize("uploads,active,may,leaves,words", [
    (False, False, False, False, "never leaves this Mac"),
    (False, False, True, False, "Gemini takes over (uploaded)"),
    (True, False, True, True, "uploaded to Google Gemini"),
    (False, True, True, True, "streams to Google Gemini"),
    (True, True, True, True, "streams to Google Gemini"),
])
def test_privacy_comes_only_from_uploads_live_active_and_may_upload(fake_mc, uploads, active, may, leaves,
                                                                      words):
    # An engine or fallback flag that contradicts these fields doesn't change the wording.
    fake_mc.set(json=mc_json(engine="gemini" if not uploads else "apple", uploads=uploads, may_upload=may,
                             gemini_fallback=not may, live={"requested": active, "active": active}))
    v = stt.current()
    assert v["leaves_mac"] is leaves and v["may_leave_mac"] is may and words in v["privacy"]
    assert ("never leaves" in v["privacy"]) is (not may)


def test_auto_with_a_key_may_upload_and_is_never_called_never_leaves(fake_mc):
    """The review's blocker: stt unset/auto, a key the recorder sees, on-device
    ready, batch. Nothing uploads now, but meeting-capture hands a chunk to
    Gemini by itself when on-device fails (gemini_fallback, may_upload)."""
    fake_mc.set(json=mc_json(gemini_key=True, gemini_fallback=True))
    v = stt.current()
    assert v["label"] == "on this Mac (en-US)" and v["leaves_mac"] is False and v["may_leave_mac"] is True
    assert v["privacy"] == stt.FALLBACK_PRIVACY and "never leaves" not in v["privacy"]
    assert v["privacy_fix"] == "`meeting-capture stt apple` never uploads"
    fake_mc.set(json=mc_json(choice="apple", gemini_key=True, on_device_only_hint=None))
    stt.clear_cache()
    v = stt.current()
    assert v["privacy"] == stt.NEVER_PRIVACY and v["may_leave_mac"] is False and v["privacy_fix"] is None


def test_an_old_meeting_capture_is_gemini_and_never_on_device(fake_mc):
    fake_mc.set(mode="old")
    v = stt.current()
    assert v["status"] == "old" and v["engine"] == "gemini"
    assert v["label"] == "Gemini (meeting-capture < 0.7 — upgrade for on-device)"
    assert v["leaves_mac"] is True and "uploaded" in v["privacy"] and v["attention"] is False


@pytest.mark.parametrize("mode,why", [("garbage", "isn't JSON"), ("fail", "exit 1")])
def test_unreadable_answers_are_unknown_and_claim_nothing(fake_mc, mode, why):
    fake_mc.set(mode=mode)
    v = stt.current()
    assert v["status"] == "error" and why in v["error"] and v["label"].startswith("unknown — ")
    assert v["leaves_mac"] is None and v["privacy"] is None and v["engine"] == "error"


@pytest.mark.parametrize("answer,why", [
    (mc_json(schema=2), "schema 2"),
    ({k: v for k, v in mc_json().items() if k != "uploads"}, "lacks"),
    ({k: v for k, v in mc_json().items() if k != "may_upload"}, "lacks"),
    (mc_json(engine="whisper"), "lacks"),
    (mc_json(live={"active": "yes"}), "lacks"),
    ([1, 2], "JSON object"),
])
def test_answers_this_contorch_cannot_rely_on_are_unknown(fake_mc, answer, why):
    fake_mc.set(json=answer)
    v = stt.current()
    assert v["status"] == "error" and why in v["error"] and v["privacy"] is None


def test_a_timeout_is_unknown(fake_mc):
    fake_mc.set(sleep=3)
    r = stt.read(timeout=0.5)
    assert r["status"] == "error" and "timed out" in r["error"]


def test_a_timeout_kills_what_meeting_capture_started(fake_mc):
    fake_mc.set(sleep=30, spawn=True)
    t0 = time.monotonic()
    r = stt.read(timeout=1.0)
    assert r["status"] == "error" and "timed out" in r["error"] and time.monotonic() - t0 < 10
    pid = int((fake_mc.root / "grandchild.pid").read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        pytest.fail("the grandchild outlived the timeout")


def test_without_meeting_capture():
    v = stt.current()
    assert v["status"] == "missing" and v["ok"] is False and v["privacy"] is None


def test_command_only_runs_stt_and_language_hints():
    assert stt.command("meeting-capture language en-US", "/x/mc") == ["/x/mc", "language", "en-US"]
    assert stt.command("meeting-capture stt auto --language en-US", "/x/mc") == \
        ["/x/mc", "stt", "auto", "--language", "en-US"]
    for bad in (None, "", "meeting-capture", "meeting-capture uninstall now", "rm -rf ~", "meeting-capture 'x"):
        assert stt.command(bad, "/x/mc") is None


# ------------------------------------------------------------ cache

def test_cached_until_the_plist_executable_or_key_changes(fake_mc):
    write_plist(stt.PLIST, {})
    for _ in range(3):
        stt.current()
    assert fake_mc.reads() == 1
    st = stt.PLIST.stat()                                    # `meeting-capture stt|language|mode`
    os.utime(stt.PLIST, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    stt.current()
    assert fake_mc.reads() == 2
    stt.KEY_FILE.write_text("AIza-new")                      # a key appears
    stt.current()
    assert fake_mc.reads() == 3
    st = fake_mc.path.stat()                                 # brew upgrade → new executable
    os.utime(fake_mc.path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    stt.current()
    stt.current()
    assert fake_mc.reads() == 4


def test_ttl_and_a_failed_read_is_retried_sooner(fake_mc, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(stt.time, "monotonic", lambda: clock[0])
    stt.current()
    clock[0] += stt.TTL_S - 1
    stt.current()
    assert fake_mc.reads() == 1
    clock[0] += 2
    stt.current()
    assert fake_mc.reads() == 2
    fake_mc.set(mode="garbage")
    clock[0] += stt.TTL_S + 1
    assert stt.current()["status"] == "error"
    clock[0] += stt.RETRY_S + 1                              # errors: a minute, not ten
    stt.current()
    assert fake_mc.reads() == 4


def test_without_waiting_it_says_checking_then_answers_once(fake_mc):
    fake_mc.set(sleep=0.4)
    first = stt.current(wait=False)
    assert first["status"] == "checking" and first["label"] == "checking…" and first["leaves_mac"] is None
    assert stt.current(wait=False)["status"] == "checking"   # no second run while one is in flight
    deadline = time.time() + 15
    while time.time() < deadline:
        v = stt.current(wait=False)
        if v["status"] != "checking":
            break
        time.sleep(0.05)
    assert v["label"] == "on this Mac (en-US)"
    assert fake_mc.reads() == 1


def test_fresh_bypasses_the_cache(fake_mc):
    stt.current()
    fake_mc.set(json=mc_json(**DUTCH))
    assert stt.current()["engine"] == "apple"                # cached
    assert stt.fresh()["engine"] == "gemini"
    assert stt.current()["engine"] == "gemini"               # and refreshed it


# ------------------------------------------------------------ the Homebrew wrapper is never run by a read

WRAPPER = """#!/bin/bash
set -e
VENV="${{MEETING_CAPTURE_VENV:-$HOME/.meeting-capture/venv}}"
STAMP="$VENV/.formula-version"
echo ran >> "{marker}"
if ! "$VENV/bin/python" -c "" 2>/dev/null || [ "$(cat "$STAMP" 2>/dev/null)" != "{version}" ]; then
  echo "meeting-capture: setting up environment (first run / upgrade)..." >&2
  exit 1
fi
export MEETING_CAPTURE_SYSAUDIO="${{MEETING_CAPTURE_SYSAUDIO:-{sysaudio}}}"
exec "{venv_mc}" "$@"
"""


@pytest.fixture
def brew(tmp_path, monkeypatch, fake_mc):
    """Homebrew's layout: opt/bin/meeting-capture is the formula's bash wrapper
    (a run after an upgrade would rm -rf and rebuild the venv; this one only
    leaves a marker), and the venv's own meeting-capture is the fake."""
    opt = tmp_path / "opt" / "bin"
    opt.mkdir(parents=True)
    (opt / "sysaudio").write_text("")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(fake_mc.path, venv / "bin" / "meeting-capture")
    os.symlink(sys.executable, venv / "bin" / "python")
    monkeypatch.setenv("MEETING_CAPTURE_VENV", str(venv))
    stamp = venv / ".formula-version"
    stamp.write_text("0.7.0\n")
    marker = tmp_path / "wrapper-ran"
    wrapper = opt / "meeting-capture"
    wrapper.write_text(WRAPPER.format(marker=marker, version="0.7.0", sysaudio=opt / "sysaudio",
                                      venv_mc=venv / "bin" / "meeting-capture"))
    wrapper.chmod(0o755)
    monkeypatch.setattr(stt, "MC_CANDIDATES", (str(wrapper),))
    monkeypatch.setattr(stt, "MC_VENV", venv / "bin" / "meeting-capture")
    monkeypatch.setattr(stt, "MC_STAMP", stamp)
    monkeypatch.delenv("MEETING_CAPTURE_SYSAUDIO", raising=False)

    class B:
        pass
    b = B()
    b.wrapper, b.venv, b.stamp, b.marker, b.sysaudio = wrapper, venv, stamp, marker, opt / "sysaudio"
    b.ran = lambda: marker.exists()
    return b


def _wait_answer():
    deadline = time.time() + 15
    while time.time() < deadline:
        v = stt.current(wait=False)
        if v["status"] != "checking":
            return v
        time.sleep(0.05)
    pytest.fail("no answer")


def test_reads_run_the_venv_never_the_wrapper(brew, fake_mc):
    assert stt.wrapper_version(str(brew.wrapper)) == "0.7.0"
    assert stt.wrapper_version(str(fake_mc.path)) is None
    v = _wait_answer()                                       # the menu bar's background read
    assert v["label"] == "on this Mac (en-US)" and v["note"] is None
    stt.clear_cache()
    assert stt.current(wait=True)["status"] == "ok"          # contorch status / doctor
    assert not brew.ran() and fake_mc.reads() == 2
    # with the sysaudio the wrapper would have exported
    env = json.loads((fake_mc.root / "mc-env.json").read_text())
    assert env["MEETING_CAPTURE_SYSAUDIO"] == str(brew.sysaudio)


def test_after_brew_upgrade_reads_the_venv_and_says_so(brew, fake_mc):
    """The review's major: the wrapper's version no longer matches the venv's
    stamp. A read must not start the rebuild (rm -rf of the venv the
    recorder runs from); it reads what the recorder still runs and notes it."""
    brew.wrapper.write_text(brew.wrapper.read_text().replace('"0.7.0"', '"0.8.0"'))
    for wait in (False, True):
        stt.clear_cache()
        v = _wait_answer() if not wait else stt.current(wait=True)
        assert v["status"] == "ok" and not brew.ran()
        assert v["note"] == ("meeting-capture 0.8.0 is installed, but the recorder still runs 0.7.0 — "
                             "run `meeting-capture install` between meetings")
    brew.stamp.write_text("0.8.0\n")                         # the user rebuilt it: re-read at once
    assert stt.current(wait=True)["note"] is None and fake_mc.reads() == 3


def test_an_unbuilt_venv_is_not_built_by_a_read(brew, fake_mc):
    (brew.venv / "bin" / "meeting-capture").unlink()
    for v in (stt.current(wait=True), _wait_answer()):
        assert v["status"] == "error" and v["error"] == stt.NOT_BUILT and v["privacy"] is None
    assert not brew.ran() and fake_mc.reads() == 0


def test_setup_reads_through_the_wrapper(brew, fake_mc):
    v = stt.fresh(str(brew.wrapper))
    assert v["status"] == "ok" and brew.ran()


def test_a_read_after_a_rebuild_is_fresh(brew, fake_mc):
    stt.current()
    st = brew.stamp.stat()
    os.utime(brew.stamp, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    stt.current()
    assert fake_mc.reads() == 2
