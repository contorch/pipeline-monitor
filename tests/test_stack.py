"""contorch stop/resume: the recorder through meeting-capture's own verbs
(`stop --json --reason`, `start --json`), launchctl only for retired agents
and meeting-capture 0.7; stopped.json remembers why."""
from __future__ import annotations

import json

from test_lifecycle import Recorder, rec  # noqa: F401
from pipeline_monitor import contorch as ct


def test_stop_goes_through_meeting_capture_and_records_the_reason(rec, capfd):
    assert ct.main(["stop", "--json", "--reason", "quit"]) == 0
    doc = json.loads(capfd.readouterr().out)
    assert doc["schema"] == "contorch.stack/1" and doc["ok"] and doc["reason"] == "quit" and doc["stopped"]
    assert rec.verbs() == [["stop", "--reason", "quit", "--json"]]
    assert json.loads(ct.STOPPED_MARKER.read_text())["reason"] == "quit"


def test_resume_after_the_users_stop_is_not_automatic_quit_and_update_are(rec):
    from pipeline_monitor import lifecycle
    ct.stop(log=lambda _: None)
    assert lifecycle.on_launch()["resumed"] is None
    assert ct.main(["resume", "--json"]) == 0                    # the user resumes it
    assert rec.verbs()[-1] == ["start", "--json"] and not ct.is_stopped()
    for reason in ("quit", "update"):
        ct.stop(log=lambda _: None, reason=reason)
        assert lifecycle.on_launch()["resumed"] is True


def test_meeting_capture_0_7_falls_back_to_launchctl(rec, monkeypatch):
    import subprocess
    rec.mc.set(rc={"stop --reason user --json": 2, "start --json": 2})
    ct.LAUNCH_AGENTS.mkdir(parents=True)
    (ct.LAUNCH_AGENTS / "com.contorch.meeting-capture.plist").write_text("")
    calls = []
    monkeypatch.setattr(ct, "_launchctl", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(ct.time, "sleep", lambda s: None)
    ct.stop(log=lambda _: None)
    assert ("disable", f"gui/{ct._uid()}/com.contorch.meeting-capture") in calls
    assert ("bootout", f"gui/{ct._uid()}/com.contorch.meeting-capture") in calls


def test_a_retired_chroma_agent_is_still_stopped_with_launchctl(rec, monkeypatch):
    import subprocess
    ct.LAUNCH_AGENTS.mkdir(parents=True)
    (ct.LAUNCH_AGENTS / "com.contorch.context-orchestrator-chroma.plist").write_text("")
    calls = []
    monkeypatch.setattr(ct, "_launchctl", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(ct.time, "sleep", lambda s: None)
    ct.stop(log=lambda _: None)
    assert rec.verbs() == [["stop", "--reason", "user", "--json"]]
    assert ("bootout", f"gui/{ct._uid()}/com.contorch.context-orchestrator-chroma") in calls
    assert not any("meeting-capture" in " ".join(c) for c in calls)
