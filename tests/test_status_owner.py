"""● REC, the update/adopt gates and the permission rows come from
meeting-capture's own JSON (`status --json`, `check --json`); the daemon log
only adds display detail and stands in for meeting-capture 0.7."""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from conftest import write_plist
from pipeline_monitor import status as st


def _ts(age_s: float = 5) -> str:
    return datetime.fromtimestamp(time.time() - age_s).strftime("%Y-%m-%d %H:%M:%S")


def _status(recording, **extra):
    return {"schema": "meeting-capture.status/1", "ok": True, "recording": recording, "state": "idle",
            "pid": 4242, **extra}


@pytest.fixture
def mc(fake_mc, tmp_path, monkeypatch):
    log = tmp_path / "daemon.log"
    log.write_text("")
    monkeypatch.setattr(st, "MEETING_CAPTURE_LOG", log)
    fake_mc.log = log
    return fake_mc


def _answer(mc, key, doc, rc=0):
    import json
    mc.set(lines={**mc_state(mc).get("lines", {}), key: [json.dumps(doc)]},
           rc={**mc_state(mc).get("rc", {}), key: rc})


def mc_state(mc):
    import json
    return json.loads(mc.state_path.read_text())


def test_recording_comes_from_meeting_capture(mc):
    _answer(mc, "status --json", _status(True, state="recording", meeting_id="meeting-x"))
    r = st.recording_status(wait=True)
    assert r["source"] == "owner" and r["recording"] is True and r["current_file"] == "meeting-x"


def test_cant_tell_is_never_rec(mc):
    """recording: null (no state, a dead daemon, a stale heartbeat) is not ● REC."""
    mc.log.write_text(f"{_ts(10)} INFO mic active — starting recording session\n"
                      f"{_ts(5)} INFO chunk 9.1s [them] -> meeting-y (100 chars)\n")
    _answer(mc, "status --json", _status(None, reason="stale_heartbeat"))
    r = st.recording_status(wait=True)
    assert r["recording"] is None                     # the log says recording; the owner can't tell
    snap = st.Snapshot(recording=r, capture_mode={"installed": True, "ok": True, "mode": "batch"},
                       modules={"set_up": True, "modules": [{"id": "recorder", "state": "on"}]})
    assert snap.overall() != "rec" and snap.headline() == "recording_unknown"
    import pipeline_monitor.app as app
    assert app._build_status_line(snap).title.startswith("? Can't tell whether a meeting is being recorded")


def test_idle_is_idle_even_if_the_log_looks_busy(mc):
    mc.log.write_text(f"{_ts(5)} INFO chunk 9.1s [them] -> meeting-y (100 chars)\n")
    _answer(mc, "status --json", _status(False))
    assert st.recording_status(wait=True)["recording"] is False


def test_a_silent_recording_is_flagged_from_the_log(mc):
    mc.log.write_text(f"{_ts(1300)} INFO mic active — starting recording session\n"
                      f"{_ts(700)} INFO chunk 9.1s [them] -> meeting-z (100 chars)\n"
                      f"{_ts(20)} INFO sysaudio: stream started, piping PCM to stdout\n")
    _answer(mc, "status --json", _status(True, state="recording", meeting_id="meeting-z"))
    r = st.recording_status(wait=True)
    assert r["recording"] and r["stale"] is True


def test_meeting_capture_0_7_falls_back_to_the_log(mc):
    mc.set(rc={"status --json": 2})
    mc.log.write_text(f"{_ts(10)} INFO mic active — starting recording session\n"
                      f"{_ts(5)} INFO chunk 9.1s [them] -> meeting-old (100 chars)\n")
    r = st.recording_status(wait=True)
    assert r["source"] == "log" and r["recording"] is True and r["current_file"] == "meeting-old"


PERMS = {"schema": "meeting-capture.permissions/1", "ok": True, "channel": "brew",
         "identity": {"helper": "/opt/homebrew/opt/meeting-capture/bin/sysaudio",
                      "subject": "/opt/homebrew/Cellar/meeting-capture/0.8.0/bin/sysaudio"},
         "permissions": [
             {"id": "screen_audio", "status": "denied", "required": True, "can_request": False,
              "hint": "Turn on sysaudio (/opt/homebrew/opt/meeting-capture/bin/sysaudio) in Screen & System "
                      "Audio Recording", "settings_url": "x-apple.systempreferences:…?Privacy_ScreenCapture"},
             {"id": "microphone", "status": "not_determined", "required": True, "can_request": True,
              "hint": "macOS asks on your first call", "settings_url": "x-apple.systempreferences:…?Privacy_Microphone"}]}


def test_permission_rows_come_from_check_json_both_rows(mc):
    _answer(mc, "check --json", PERMS)
    p = st.permissions_status(wait=True)
    assert [r["id"] for r in p["problems"]] == ["screen_audio", "microphone"]
    assert [r["id"] for r in p["denied"]] == ["screen_audio"]           # not asked yet isn't ⚠ PERM
    snap = st.Snapshot(permissions=p, capture_mode={"installed": True, "ok": True, "mode": "batch"},
                       recording={"ok": True, "recording": False, "source": "owner"},
                       modules={"set_up": True, "modules": [{"id": "recorder", "state": "on"}]})
    assert snap.overall() == "perm"
    import pipeline_monitor.app as app
    lines = [i.title for i in app._permission_lines(snap)]
    assert lines[0].startswith("⚠ Screen & System Audio Recording: off — Turn on sysaudio (/opt/homebrew")
    assert lines[1] == "⚠ Microphone: not asked yet — macOS asks on your first call"


def test_the_app_channels_hint_is_shown_as_meeting_capture_words_it(mc):
    app_perms = {**PERMS, "channel": "app", "permissions": [
        {**PERMS["permissions"][0], "hint": "Turn on Contorch in Screen & System Audio Recording"}]}
    _answer(mc, "check --json", app_perms)
    import pipeline_monitor.app as app
    snap = st.Snapshot(permissions=st.permissions_status(wait=True))
    assert app._permission_lines(snap)[0].title == ("⚠ Screen & System Audio Recording: off — Turn on Contorch "
                                                    "in Screen & System Audio Recording")


def test_meeting_capture_0_7_permission_from_the_log(mc):
    mc.set(rc={"check --json": 2})
    mc.log.write_text(f"{_ts(30)} INFO mic active — starting recording session\n"
                      "sysaudio error: The user declined TCCs for application, window, display capture\n")
    p = st.permissions_status(wait=True)
    assert p["source"] == "log" and p["denied"][0]["id"] == "screen_audio"
    assert "meeting-capture doctor" in p["denied"][0]["hint"]


def test_perm_hint_is_gone():
    import pipeline_monitor.app as app
    assert not hasattr(app, "PERM_HINT")
    assert "re-add bin/sysaudio" not in (st.Path(app.__file__)).read_text()


def test_capture_mode_from_meeting_captures_config(mc, tmp_path):
    import json
    env = tmp_path / "mcenv"
    env.write_text("x")
    _answer(mc, "config --json", {"schema": "meeting-capture.config/1", "ok": True, "watch_paths": [str(env)],
                                  "agent": {"backend": "launchctl", "installed": True},
                                  "settings": {"mode": {"value": "live", "source": "file"}}})
    assert st.capture_mode_status() == {"ok": True, "mode": "live", "installed": True}
