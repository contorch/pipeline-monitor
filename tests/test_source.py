"""The menu's "Source:" line, its ⚠ and the one notification per outage —
for meeting-capture 0.7.x, from its settings (plist) and its daemon log
(0.7.0: no fallback; 0.7.1: the line-in fallback's lines). Never
"recording" unless the recording state says so."""
from __future__ import annotations

import io
import time
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import pytest

from conftest import write_plist
from pipeline_monitor import source as src
from pipeline_monitor import status as st

UMC = {"device": "UMC404HD 192k", "me_channel": 0, "them_channel": 1}
MISSING = "no input device matching 'UMC404HD 192k'. Available: External Microphone, MacBook Pro Microphone"
LINEIN = {"MEETING_CAPTURE_SOURCE": "linein", "MEETING_CAPTURE_INPUT_DEVICE": "UMC404HD 192k",
          "MEETING_CAPTURE_ME_CHANNEL": "0", "MEETING_CAPTURE_THEM_CHANNEL": "1"}
PID = 4242


def _ts(age_s: float) -> str:
    return datetime.fromtimestamp(time.time() - age_s).strftime("%Y-%m-%d %H:%M:%S")


def _start(age, pid=PID, v071=True, fallback="on"):
    out = [f"{_ts(age)},000 INFO meeting-capture daemon starting (pid={pid}, mic=MacBook Pro Microphone)",
           f"{_ts(age)},001 INFO SOURCE: line-in — reading the USB audio interface continuously "
           "(not a call participant; chunks only on speech)"]
    if v071:
        out.append(f"{_ts(age)},002 INFO line-in fallback: " + (
            "on — if the interface is missing during a call on this Mac, this Mac's call audio is recorded instead"
            if fallback == "on" else "off (MEETING_CAPTURE_LINEIN_FALLBACK) — nothing is recorded while the "
                                     "interface is missing"))
    return out


def _err(age):
    return f"{_ts(age)},500 ERROR line-in capture unavailable: {MISSING} — retrying in 30s"


def _outage_070(minutes: int, last_age: float = 10) -> list[str]:
    """meeting-capture 0.7.0's lines, as on 2026-10-06."""
    out = [f"{_ts(minutes * 60 + 200)} INFO chunk 9.1s [them] -> meeting-2026-10-06T09-00-00 (226 chars)"]
    for k in range(minutes * 2, -1, -1):
        age = last_age + k * 30
        out += [f"{_ts(age + 0.2)} INFO mic inactive — session ended",
                f"{_ts(age + 0.1)} INFO line-in: listening on the interface",
                f"{_ts(age)} ERROR line-in capture unavailable: {MISSING} — retrying in 30s"]
    return out


@pytest.fixture
def rig(tmp_path):
    class R:
        pass
    r = R()
    r.log = tmp_path / "daemon.log"
    r.plist = write_plist(tmp_path / "agent.plist", dict(LINEIN))
    r.reader = src.LogReader()

    def write(lines, append=False):
        with r.log.open("a" if append else "w") as f:
            f.write("\n".join(lines) + "\n")

    def status(running=True, pid=PID, now=None):
        return src.status(daemon_pid=pid, daemon_running=running, log_path=r.log, plist=r.plist,
                          now=now, reader=r.reader)
    r.write, r.status = write, status
    return r


def _snap(source, recording=False):
    return st.Snapshot(source=source, recording={"ok": True, "recording": recording}, chroma={"ok": True})


# ---- meeting-capture 0.7.0 ---------------------------------------------------------------------

def test_070_outage_from_its_log(rig):
    import pipeline_monitor.app as app
    rig.write(_start(3000, v071=False) + _outage_070(41))
    s = rig.status()
    assert s["via"] == "settings" and s["configured"] == "linein" and s["device"] == "UMC404HD 192k"
    p = s["problem"]
    assert p["code"] == "linein_device_missing" and p["fallback"] is None and p["message"] == MISSING
    assert abs((time.time() - p["since"]) - (41 * 60 + 10)) < 5
    title = app._build_source_line(_snap(s)).title
    assert title.startswith("⚠ Source: UMC404HD 192k not connected — not recording (since ")
    assert _snap(s).overall() == "err"
    assert any("fallback: none in meeting-capture 0.7.0" in d for d in app._source_details(_snap(s)))


def test_070_with_the_interface_working(rig):
    rig.write(_start(3000, v071=False) + _outage_070(3, last_age=100) +
              [f"{_ts(60)} INFO line-in: listening on the interface",
               "line-in: device=3 channels=2 me=ch0 them=ch1 @ 16000Hz",
               f"{_ts(20)} INFO chunk 4.0s [me] -> meeting-x (40 chars)"])
    s = rig.status()
    assert s["problem"] is None
    assert src.text(s) == "Source: line-in — UMC404HD 192k (Me in 1 · Them in 2)"
    assert _snap(s).overall() == "ok"


def test_an_old_outage_is_over_and_a_stopped_daemon_has_none(rig):
    rig.write(_start(3000, v071=False) + _outage_070(3, last_age=600))   # stopped retrying 10 min ago (paused)
    assert rig.status()["problem"] is None
    rig2 = src.LogReader()
    rig.write(_start(3000, v071=False) + _outage_070(3))
    assert src.status(daemon_pid=PID, daemon_running=False, log_path=rig.log, plist=rig.plist,
                      reader=rig2)["problem"] is None


def test_this_macs_call_audio(rig):
    write_plist(rig.plist, {})
    rig.write(_start(3000, v071=False) + _outage_070(3))                 # lines from a line-in setup
    s = rig.status()
    assert s["problem"] is None and src.text(s) == "Source: this Mac's call audio"


def test_only_the_running_daemons_lines_count(rig):
    rig.write(_start(4000, pid=1111, v071=False) + _outage_070(3) + _start(30, pid=PID))
    assert rig.status()["problem"] is None


def test_the_users_real_log_replayed(tmp_path):
    """The user's ~/.meeting-capture/daemon.log up to the missed call of
    2026-10-06 (read-only; skipped where it doesn't exist)."""
    real = Path.home() / ".meeting-capture" / "daemon.log"
    if not real.exists() or b"2026-10-06 11:17:57,843 ERROR line-in capture unavailable" not in real.read_bytes():
        pytest.skip("not this Mac")
    data = real.read_bytes()
    cut = data.index(b"\n", data.index(b"2026-10-06 11:17:57,843 ERROR")) + 1
    log = tmp_path / "replay.log"
    log.write_bytes(data[:cut])
    plist = write_plist(tmp_path / "agent.plist", dict(LINEIN))
    now = datetime(2026, 10, 6, 11, 18, 10).timestamp()
    s = src.status(daemon_pid=34565, log_path=log, plist=plist, now=now, reader=src.LogReader())
    import pipeline_monitor.app as app
    title = app._build_source_line(_snap(s)).title
    assert title == "⚠ Source: UMC404HD 192k not connected — not recording (since 10:37)"


# ---- meeting-capture 0.7.1: the fallback ------------------------------------------------------

def test_071_armed_active_returned_back(rig):
    import pipeline_monitor.app as app
    rig.write(_start(700) + [_err(600 - 30 * k) for k in range(20)])      # every 30 s, newest 30 s ago
    s = rig.status()
    assert s["problem"]["fallback"] == "armed" and s["effective"] == "sck"
    since = s["problem"]["since"]
    assert app._build_source_line(_snap(s)).title.startswith(
        "⚠ Source: UMC404HD 192k not connected — will record this Mac's calls instead (since ")
    assert _snap(s).overall() == "err"

    rig.write([f"{_ts(20)},000 WARNING line-in: 'UMC404HD 192k' not connected — this Mac is in a call: "
               "recording this Mac's call audio instead (line-in fallback; `meeting-capture linein-fallback "
               "off` turns it off)",
               f"{_ts(20)},001 INFO mic active — starting recording session"], append=True)
    s = rig.status(now=time.time() + 3600)          # a long call: no "unavailable" lines meanwhile
    assert s["problem"]["fallback"] == "active" and s["problem"]["since"] == since
    assert app._build_source_line(_snap(s, True)).title.startswith(
        "⚠ Source: UMC404HD 192k not connected — recording this Mac instead (since ")
    assert _snap(s, True).overall() == "rec"

    rig.write([f"{_ts(10)},000 INFO line-in: 'UMC404HD 192k' is connected again — staying on this Mac's "
               "call audio until the call ends"], append=True)
    s = rig.status()
    assert s["problem"]["returned"]
    assert app._build_source_line(_snap(s, True)).title == \
        "Source: UMC404HD 192k is back — recording this Mac until the call ends"

    rig.write([f"{_ts(5)},000 INFO chunk 30.0s [them] -> meeting-x (10 chars) via apple",
               f"{_ts(5)},001 INFO mic inactive — session ended",
               f"{_ts(5)},002 INFO line-in fallback: the call ended — looking for the interface again",
               f"{_ts(5)},003 INFO line-in: listening on the interface"], append=True)
    rig.write([f"{_ts(5)},004 INFO line-in: 'UMC404HD 192k' is back (unavailable for 11m) — recording from "
               "the interface again"], append=True)
    s = rig.status()
    assert s["problem"] is None and src.text(s) == "Source: line-in — UMC404HD 192k (Me in 1 · Them in 2)"


def test_071_a_fallback_calls_late_chunk_does_not_end_the_outage(rig):
    rig.write(_start(700) + [_err(100),
              f"{_ts(90)},000 WARNING line-in: 'UMC404HD 192k' not connected — this Mac is in a call: "
              "recording this Mac's call audio instead (line-in fallback; …)",
              f"{_ts(90)},001 INFO mic active — starting recording session",
              f"{_ts(20)},000 INFO line-in fallback: the call ended — looking for the interface again",
              _err(19),
              f"{_ts(15)},000 INFO chunk 30.0s [them] -> meeting-x (10 chars) via apple",
              f"{_ts(15)},001 INFO mic inactive — session ended"])
    s = rig.status()
    assert s["problem"]["fallback"] == "armed"


def test_071_fallback_off(rig):
    rig.write(_start(100, fallback="off") + [_err(40), _err(10),
              f"{_ts(9)},000 WARNING line-in: 'UMC404HD 192k' not connected and this Mac is in a call — NOT "
              "recording (the line-in fallback is off; `meeting-capture linein-fallback on` records this Mac's "
              "call audio meanwhile)"])
    s = rig.status()
    assert s["problem"]["fallback"] == "off" and s["effective"] is None
    assert src.text(s) == "Source: UMC404HD 192k not connected — not recording"
    assert "  fallback: off (meeting-capture linein-fallback on)" in src.details(s)


def test_the_log_is_read_incrementally(rig, monkeypatch):
    rig.write(_start(700) + ["x" * 100] * 5000)
    assert rig.status()["problem"] is None
    reads = []
    real = src._read_since_start
    monkeypatch.setattr(src, "_read_since_start", lambda *a: reads.append(a) or real(*a))
    rig.write([_err(5)], append=True)
    assert rig.status()["problem"]["fallback"] == "armed"
    assert reads == []                              # only the appended line was read
    rig.write(_start(1, pid=999) + [_err(0)])       # a new daemon (and a new file)
    assert rig.status(pid=999)["problem"] is not None and len(reads) == 1


# ---- one notification per outage ---------------------------------------------------------------

def _problem(fallback, since=1000.0):
    return {"code": "linein_device_missing", "device": "UMC404HD 192k", "message": MISSING,
            "since": since, "fallback": fallback}


def test_one_notification_per_outage():
    n = src.OutageNotifier()
    base = {"ok": True, "via": "settings", "configured": "linein", **UMC}
    first = n.check(dict(base, problem=_problem("armed")))
    assert first == ("UMC404HD 192k isn't connected",
                     "Nothing comes in from it. A call on this Mac is recorded from this Mac's audio instead.")
    assert n.check(dict(base, problem=_problem("active")), True) is None   # same outage
    assert n.check({"ok": False}) is None                  # can't tell: not "over"
    assert n.check(dict(base, problem=_problem("armed"))) is None
    assert n.check(dict(base, problem=None)) is None       # over
    again = n.check(dict(base, problem=_problem(None, since=2000.0)))
    assert again[1].startswith("Nothing is being recorded.")
    assert n.check(dict(base, problem=_problem("active", since=3000.0)), True)[1] == \
        "Recording this Mac's call audio instead."


# ---- contorch status / doctor -------------------------------------------------------------------

def test_contorch_status_and_doctor_lines():
    from pipeline_monitor import contorch as ct
    s = {"ok": True, "via": "settings", "configured": "linein", **UMC, "problem": _problem(None)}
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert ct._print_source(_snap(s)) is True
    assert buf.getvalue().startswith("  source                   ⚠ UMC404HD 192k not connected — not recording "
                                     "(since ")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert ct._print_source(_snap(s), doctor=True) is True
    out = buf.getvalue()
    assert "  ✗ source: UMC404HD 192k not connected — not recording (since " in out
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert ct._print_source(_snap({"ok": True, "configured": "sck", "problem": None})) is False
    assert buf.getvalue() == "  source                   this Mac's call audio\n"


def test_no_agent_no_source_line(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "MEETING_CAPTURE_PLIST", tmp_path / "none.plist")
    assert st.source_status({"ok": True, "daemons": {}}) == {}
