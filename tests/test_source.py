"""The menu's "Source:" line, its ⚠ and the one notification per outage —
from meeting-capture's `status --json` (≥ 0.8), or for meeting-capture 0.7
from its settings (plist) and daemon log. Never "recording" unless
meeting-capture says so."""
from __future__ import annotations

import io
import json
import time
from contextlib import redirect_stdout
from datetime import datetime

import pytest

from conftest import write_plist
from pipeline_monitor import mcconfig
from pipeline_monitor import source as src
from pipeline_monitor import status as st

UMC_INPUT = {"device": "UMC404HD 192k", "me_channel": 0, "them_channel": 1}
MISSING = "no input device matching 'UMC404HD 192k'. Available: External Microphone, MacBook Pro Microphone"


def _ts(age_s: float) -> str:
    return datetime.fromtimestamp(time.time() - age_s).strftime("%Y-%m-%d %H:%M:%S")


def _status(recording=False, state="idle", **extra):
    return {"schema": "meeting-capture.status/1", "ok": True, "recording": recording, "state": state,
            "pid": 4242, "since": 1.0, "meeting_id": None, "stale": False, **extra}


def _problem(fallback, since=None):
    return {"code": "linein_device_missing", "device": "UMC404HD 192k", "message": MISSING,
            "since": since or time.time() - 600, "fallback": fallback}


@pytest.fixture
def mc(fake_mc, tmp_path, monkeypatch):
    log = tmp_path / "daemon.log"
    log.write_text("")
    monkeypatch.setattr(st, "MEETING_CAPTURE_LOG", log)
    fake_mc.log = log

    def answer(key, doc=None, rc=0):
        cur = json.loads(fake_mc.state_path.read_text())
        lines = dict(cur.get("lines", {}))
        if doc is not None:
            lines[key] = [json.dumps(doc)]
        fake_mc.set(lines=lines, rc={**cur.get("rc", {}), key: rc})
    fake_mc.answer = answer
    return fake_mc


def _snap(source, recording=False):
    return st.Snapshot(source=source, recording={"ok": True, "recording": recording, "source": "owner"},
                       capture_mode={"installed": True, "ok": True, "mode": "batch"},
                       modules={"set_up": True, "modules": [{"id": "recorder", "state": "on"}]})


# ---- meeting-capture says it (status --json) ---------------------------------------------------

OWNER_CASES = [
    # (status --json fields, recording, menu line)
    (dict(source="sck", input=None, effective_source="sck", linein_fallback=True, problem=None), False,
     "Source: this Mac's call audio"),
    (dict(source="linein", input=UMC_INPUT, effective_source="linein", linein_fallback=True, problem=None), False,
     "Source: line-in — UMC404HD 192k (Me in 1 · Them in 2)"),
    (dict(source="linein", input=UMC_INPUT, effective_source="sck", linein_fallback=True,
          problem=_problem("active")), True,
     "⚠ Source: UMC404HD 192k not connected — recording this Mac instead"),
    (dict(source="linein", input=UMC_INPUT, effective_source="sck", linein_fallback=True,
          problem=dict(_problem("active"), returned=time.time() - 30)), True,
     "Source: UMC404HD 192k is back — recording this Mac until the call ends"),
    (dict(source="linein", input=UMC_INPUT, effective_source="sck", linein_fallback=True,
          problem=_problem("armed")), False,
     "⚠ Source: UMC404HD 192k not connected — will record this Mac's calls instead"),
    (dict(source="linein", input=UMC_INPUT, effective_source=None, linein_fallback=False,
          problem=_problem("off")), False,
     "⚠ Source: UMC404HD 192k not connected — not recording"),
]


@pytest.mark.parametrize("fields,recording,line", OWNER_CASES)
def test_the_menu_line_from_meeting_capture(mc, fields, recording, line):
    import pipeline_monitor.app as app
    mc.answer("status --json", _status(recording, "recording" if recording else "idle", **fields))
    s = src.status(wait=True, log_path=mc.log)
    assert s["via"] == "owner"
    title = app._build_source_line(_snap(s, recording)).title
    assert title.split(" (since ")[0] == line
    assert ("(since " in title) == bool(fields["problem"] and not fields["problem"].get("returned"))


def test_active_fallback_is_never_called_recording_unless_meeting_capture_says_recording(mc):
    mc.answer("status --json", _status(None, "recording", reason="stale_heartbeat", source="linein",
                                       input=UMC_INPUT, effective_source="sck", linein_fallback=True,
                                       problem=_problem("active")))
    s = src.status(wait=True)
    assert src.text(s, None) == "Source: UMC404HD 192k not connected — will record this Mac's calls instead"
    assert "recording this Mac" not in src.text(s, False)


def test_the_icon_warns_while_the_interface_is_missing_and_rec_wins_while_recording_instead():
    armed = {"ok": True, "via": "owner", "configured": "linein", **UMC_INPUT, "problem": _problem("armed")}
    assert _snap(armed).overall() == "err"
    active = dict(armed, problem=_problem("active"))
    assert _snap(active, recording=True).overall() == "rec"
    fine = dict(armed, problem=None)
    assert _snap(fine).overall() == "idle"
    memory_only = _snap(armed)
    memory_only.modules = {"set_up": True, "modules": [{"id": "recorder", "state": "off"}]}
    assert memory_only.overall() == "idle"


def test_a_stopped_daemon_has_no_outage(mc):
    write_plist(mcconfig.LEGACY_PLIST, {"MEETING_CAPTURE_SOURCE": "linein",
                                         "MEETING_CAPTURE_INPUT_DEVICE": "UMC404HD 192k"})
    mc.answer("config --json", rc=2)
    mc.log.write_text(f"{_ts(5)} ERROR line-in capture unavailable: {MISSING} — retrying in 30s\n")
    mc.answer("status --json", _status(None, reason="daemon_not_running", source="linein", input=UMC_INPUT,
                                       effective_source="sck", problem=_problem("armed")))
    s = src.status(wait=True, log_path=mc.log)
    assert s["via"] == "settings" and s["problem"] is None


# ---- meeting-capture 0.7: settings + daemon log -------------------------------------------------

def _legacy(mc, env):
    write_plist(mcconfig.LEGACY_PLIST, env)
    mc.answer("config --json", rc=2)
    mc.answer("status --json", rc=2)


def _outage_log(minutes: int, last_age: float = 10) -> str:
    """meeting-capture 0.7's lines, as on 2026-10-06."""
    out = [f"{_ts(minutes * 60 + 200)} INFO chunk 9.1s [them] -> meeting-2026-10-06T09-00-00 (226 chars)"]
    for k in range(minutes * 2, -1, -1):
        age = last_age + k * 30
        out += [f"{_ts(age + 0.2)} INFO mic inactive — session ended",
                f"{_ts(age + 0.1)} INFO line-in: listening on the interface",
                f"{_ts(age)} ERROR line-in capture unavailable: {MISSING} — retrying in 30s"]
    return "\n".join(out) + "\n"


def test_meeting_capture_0_7_outage_from_its_log(mc):
    import pipeline_monitor.app as app
    _legacy(mc, {"MEETING_CAPTURE_SOURCE": "linein", "MEETING_CAPTURE_INPUT_DEVICE": "UMC404HD 192k",
                 "MEETING_CAPTURE_ME_CHANNEL": "0", "MEETING_CAPTURE_THEM_CHANNEL": "1"})
    mc.log.write_text(_outage_log(41))
    s = src.status(wait=True, log_path=mc.log)
    assert s["via"] == "settings" and s["configured"] == "linein" and s["device"] == "UMC404HD 192k"
    p = s["problem"]
    assert p["code"] == "linein_device_missing" and p["fallback"] is None and p["message"] == MISSING
    assert abs((time.time() - p["since"]) - (41 * 60 + 10)) < 5
    title = app._build_source_line(_snap(s)).title
    assert title.startswith("⚠ Source: UMC404HD 192k not connected — not recording (since ")
    assert _snap(s).overall() == "err"
    details = app._source_details(_snap(s))
    assert any("fallback: none in this meeting-capture version" in d for d in details)


def test_meeting_capture_0_7_with_the_interface_working(mc):
    _legacy(mc, {"MEETING_CAPTURE_SOURCE": "linein", "MEETING_CAPTURE_INPUT_DEVICE": "UMC404HD 192k"})
    mc.log.write_text(_outage_log(3, last_age=100) +
                      f"{_ts(60)} INFO line-in: listening on the interface\n"
                      "line-in: device=3 channels=2 me=ch0 them=ch1 @ 16000Hz\n"
                      f"{_ts(20)} INFO chunk 4.0s [me] -> meeting-x (40 chars)\n")
    s = src.status(wait=True, log_path=mc.log)
    assert s["problem"] is None
    assert src.text(s) == "Source: line-in — UMC404HD 192k (Me in 1 · Them in 2)"


def test_meeting_capture_0_7_an_old_outage_is_over(mc):
    _legacy(mc, {"MEETING_CAPTURE_SOURCE": "linein", "MEETING_CAPTURE_INPUT_DEVICE": "UMC404HD 192k"})
    mc.log.write_text(_outage_log(3, last_age=600))       # the daemon stopped retrying 10 min ago
    assert src.status(wait=True, log_path=mc.log)["problem"] is None


def test_meeting_capture_0_7_this_macs_call_audio(mc):
    _legacy(mc, {})
    mc.log.write_text(_outage_log(3))                      # stale lines from an earlier line-in setup
    s = src.status(wait=True, log_path=mc.log)
    assert s["problem"] is None and src.text(s) == "Source: this Mac's call audio"


def test_the_users_log_from_2026_10_06(mc):
    """The exact lines meeting-capture 0.7 wrote during the missed call."""
    _legacy(mc, {"MEETING_CAPTURE_SOURCE": "linein", "MEETING_CAPTURE_INPUT_DEVICE": "UMC404HD 192k"})
    avail = ("no input device matching 'UMC404HD 192k'. Available: External Microphone, MacBook Pro Microphone, "
             "Microsoft Teams Audio, Steam Streaming Microphone, Steam Streaming Speakers")
    mc.log.write_text(f"{_ts(31)} INFO mic inactive — session ended\n"
                      f"{_ts(31)} INFO line-in: listening on the interface\n"
                      f"{_ts(31)} ERROR line-in capture unavailable: {avail} — retrying in 30s\n"
                      f"{_ts(1)} INFO mic inactive — session ended\n"
                      f"{_ts(1)} INFO line-in: listening on the interface\n"
                      f"{_ts(1)} ERROR line-in capture unavailable: {avail} — retrying in 30s\n")
    s = src.status(wait=True, log_path=mc.log)
    assert s["problem"]["message"] == avail
    assert src.text(s) == "Source: UMC404HD 192k not connected — not recording"


# ---- one notification per outage ---------------------------------------------------------------

def test_one_notification_per_outage():
    n = src.OutageNotifier()
    base = {"ok": True, "via": "owner", "configured": "linein", **UMC_INPUT}
    first = n.check(dict(base, problem=_problem("armed", since=1000.0)))
    assert first == ("UMC404HD 192k isn't connected",
                     "Nothing comes in from it. A call on this Mac is recorded from this Mac's audio instead.")
    assert n.check(dict(base, problem=_problem("active", since=1000.0)), True) is None   # same outage
    assert n.check({"ok": False}) is None                  # can't tell: not "over"
    assert n.check(dict(base, problem=_problem("armed", since=1000.0))) is None
    assert n.check(dict(base, problem=None)) is None       # over
    again = n.check(dict(base, problem=_problem("off", since=2000.0)))
    assert again[1].startswith("Nothing is being recorded.")
    legacy = src.OutageNotifier()
    lp = dict(base, via="settings", problem=dict(_problem(None, since=3000.0)))
    assert legacy.check(lp) is not None
    lp2 = dict(lp, problem=dict(lp["problem"], since=3100.0))           # the log's tail scrolled
    assert legacy.check(lp2) is None


def test_recording_instead_is_said_in_the_notification():
    n = src.OutageNotifier()
    s = {"ok": True, "via": "owner", "configured": "linein", **UMC_INPUT, "problem": _problem("active")}
    assert n.check(s, True)[1] == "Recording this Mac's call audio instead."


# ---- contorch status / doctor -------------------------------------------------------------------

def test_contorch_status_and_doctor_lines():
    from pipeline_monitor import contorch as ct
    s = {"ok": True, "via": "owner", "configured": "linein", **UMC_INPUT, "problem": _problem("off")}
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert ct._print_source(_snap(s)) is True
    line = buf.getvalue()
    assert line.startswith("  source                   ⚠ UMC404HD 192k not connected — not recording (since ")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert ct._print_source(_snap(s), doctor=True) is True
    out = buf.getvalue()
    assert "  ✗ source: UMC404HD 192k not connected — not recording (since " in out
    assert "fallback: off (meeting-capture config set linein_fallback 1)" in out
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert ct._print_source(_snap({"ok": True, "configured": "sck", "problem": None})) is False
    assert buf.getvalue() == "  source                   this Mac's call audio\n"
