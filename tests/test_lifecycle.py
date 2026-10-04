"""Quit, launch and update policy (pipeline_monitor.lifecycle), against a
fake meeting-capture and a fake launchctl. The rumps study's table: Quit
stops recording unless "Keep recording after Quit"; logout does nothing; an
update always stops; the next launch resumes a quit/update stop in EVERY
channel (brew's SIGTERM included); a user's "Stop everything" is respected."""
from __future__ import annotations

import json
import signal
import subprocess

import pytest

from conftest import write_plist
from pipeline_monitor import contorch as ct
from pipeline_monitor import lifecycle, mcconfig, owners


class Recorder:
    """The fake meeting-capture's agent: stop/start/status/heal answers."""

    def __init__(self, fake_mc, tmp_path, monkeypatch):
        self.mc = fake_mc
        self.recording = False
        monkeypatch.setattr(ct, "LAUNCH_AGENTS", tmp_path / "LaunchAgents")
        monkeypatch.setattr(ct, "STATE_DIR", tmp_path / "home" / ".contorch")
        monkeypatch.setattr(ct, "STOPPED_MARKER", tmp_path / "home" / ".contorch" / "stopped.json")
        monkeypatch.setattr(ct, "_launchctl", lambda *a: subprocess.CompletedProcess(a, 113, "", ""))
        # found in every channel (in the app it would be the bundle's copy)
        monkeypatch.setattr(owners, "locate", lambda name: str(fake_mc.path) if name == "meeting-capture" else None)
        self.sync()

    def sync(self):
        agent = lambda action, **kw: json.dumps({"schema": "meeting-capture.agent/1", "ok": True,  # noqa: E731
                                                 "action": action, "performed": True, **kw})
        self.mc.set(lines={
            "config --json": [json.dumps({"schema": "meeting-capture.config/1", "ok": True, "watch_paths": [],
                                          "agent": {"backend": "launchctl", "installed": True}, "settings": {}})],
            "status --json": [json.dumps({"schema": "meeting-capture.status/1", "ok": True,
                                          "recording": self.recording})],
            **{f"stop --reason {r} --json": [agent("stop", reason=r)] for r in ("user", "quit", "update")},
            "start --json": [agent("start")],
            "heal --json": [agent("heal", performed=False)],
        })

    def verbs(self):
        return [c for c in self.mc.changes() if c[0] in ("stop", "start", "heal")]


@pytest.fixture
def rec(fake_mc, tmp_path, monkeypatch):
    return Recorder(fake_mc, tmp_path, monkeypatch)


# ------------------------------------------------------------ the table

@pytest.mark.parametrize("ch,keep,reason,staged,verb,stopped", [
    ("app", None, "user", False, ["stop", "--reason", "quit", "--json"], "quit"),       # Q0: app default
    ("app", True, "user", False, None, None),                                           # Q1: keep on
    ("app", None, "logout", False, None, None),                                         # Q2: logout
    ("app", True, "update", False, ["stop", "--reason", "update", "--json"], "update"), # Q3: update
    ("app", True, "user", True, ["stop", "--reason", "update", "--json"], "update"),    # Q4: staged update
    ("brew", None, "signal", False, None, None),                                        # brew default: keep
    ("brew", False, "signal", False, ["stop", "--reason", "quit", "--json"], "quit"),   # brew, keep off
])
def test_quit_table(rec, monkeypatch, ch, keep, reason, staged, verb, stopped):
    monkeypatch.setenv("CONTORCH_CHANNEL", ch)
    if keep is not None:
        lifecycle.set_keep_recording_after_quit(keep)
    monkeypatch.setattr(lifecycle, "update_staged", staged)
    res = lifecycle.on_quit(reason)
    assert rec.verbs() == ([verb] if verb else [])
    assert ct.stopped_reason() == stopped, res


def test_brew_keep_off_sigterm_then_relaunch_resumes_the_recorder(rec, monkeypatch):
    """RL-18: in brew, keep off + SIGTERM (brew upgrade / services restart)
    must not stop recording forever."""
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    lifecycle.set_keep_recording_after_quit(False)
    lifecycle.on_quit("signal")
    assert ct.stopped_reason() == "quit"
    out = lifecycle.on_launch()
    assert out["resumed"] is True and ct.stopped_reason() is None
    assert rec.verbs()[-1] == ["start", "--json"]


@pytest.mark.parametrize("ch", ["app", "brew", "dev"])
def test_every_channel_resumes_a_quit_or_update_stop(rec, monkeypatch, ch):
    monkeypatch.setenv("CONTORCH_CHANNEL", ch)
    for reason in ("quit", "update"):
        ct.stop(log=lambda _: None, reason=reason)
        assert lifecycle.on_launch()["resumed"] is True
        assert not ct.is_stopped()


def test_the_users_stop_everything_is_respected_on_launch(rec, monkeypatch):
    ct.stop(log=lambda _: None)                                   # the menu's Stop everything
    assert ct.stopped_reason() == "user"
    assert lifecycle.on_launch()["resumed"] is None and ct.is_stopped()
    assert lifecycle.on_quit("user")["action"].startswith("none (already stopped")


def test_a_marker_from_before_reasons_counts_as_the_users(rec):
    ct.STATE_DIR.mkdir(parents=True, exist_ok=True)
    ct.STOPPED_MARKER.write_text(json.dumps({"at": 1, "labels": []}))
    assert ct.stopped_reason() == "user" and lifecycle.on_launch()["resumed"] is None


def test_a_mac_that_doesnt_record_has_nothing_to_stop(fake_mc, monkeypatch, tmp_path):
    Recorder(fake_mc, tmp_path, monkeypatch)
    fake_mc.set(lines={"config --json": [json.dumps({"schema": "meeting-capture.config/1", "ok": True,
                                                      "agent": {"installed": False}, "settings": {}})]})
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    assert lifecycle.on_quit("user")["action"] == "none (this Mac doesn't record)"


# ------------------------------------------------------------ updates

@pytest.mark.parametrize("recording,allowed,why", [(False, True, "idle"), (True, False, "recording"),
                                                   (None, False, "recording_unknown")])
def test_install_allowed_only_when_meeting_capture_says_idle(rec, recording, allowed, why):
    rec.recording = recording
    rec.sync()
    assert lifecycle.install_allowed() == (allowed, why)


def test_install_is_held_with_meeting_capture_0_7(rec):
    rec.mc.set(rc={"status --json": 2})
    assert lifecycle.install_allowed() == (False, "recording_unknown")


def test_prepare_update_stops_with_reason_update_and_v2_resumes(rec):
    assert lifecycle.prepare_update()
    assert ct.stopped_reason() == "update"
    assert lifecycle.on_launch()["resumed"] is True


def test_prepare_update_keeps_a_users_stop(rec):
    ct.stop(log=lambda _: None)
    lifecycle.prepare_update()
    assert ct.stopped_reason() == "user"


# ------------------------------------------------------------ launch inside the app

def test_launch_in_the_app_claims_heals_and_offers_setup(rec, tmp_path, monkeypatch):
    app = tmp_path / "Applications" / "Contorch.app"
    app.mkdir(parents=True)
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: app)
    monkeypatch.setattr(lifecycle, "location", lambda: "ok")
    monkeypatch.setattr(owners, "locate", lambda name: str(rec.mc.path) if name == "meeting-capture" else None)
    out = lifecycle.on_launch()
    assert out["needs_setup"] is True                             # no modules.json yet
    assert ["heal", "--json"] in rec.verbs()                      # only because recording is false
    from pipeline_monitor import channel
    assert channel.read()["owner"] == "app"


def test_no_heal_while_recording(rec, tmp_path, monkeypatch):
    rec.recording = True
    rec.sync()
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: tmp_path / "Contorch.app")
    monkeypatch.setattr(lifecycle, "location", lambda: "ok")
    monkeypatch.setattr(owners, "locate", lambda name: str(rec.mc.path) if name == "meeting-capture" else None)
    lifecycle.on_launch()
    assert ["heal", "--json"] not in rec.verbs()


def test_location(tmp_path, monkeypatch):
    assert lifecycle.location() == "not_in_app"
    for path, want in ((tmp_path / "Downloads" / "Contorch.app", "outside_applications"),
                       (tmp_path / "AppTranslocation" / "X" / "d" / "Contorch.app", "translocated")):
        path.mkdir(parents=True)
        monkeypatch.setattr(owners, "bundle_root", lambda executable=None, p=path: p)
        assert lifecycle.location() == want


# ------------------------------------------------------------ preferences

def test_preferences_defaults_per_channel_and_cli(monkeypatch, capfd):
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    assert lifecycle.keep_recording_after_quit() is False
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    assert lifecycle.keep_recording_after_quit() is True
    assert ct.main(["preferences", "set", "keep-recording-after-quit", "off", "--json"]) == 0
    doc = json.loads(capfd.readouterr().out)
    assert doc["keep_recording_after_quit"] is False and doc["update_feed"] == "stable"
    assert ct.main(["preferences", "set", "update-feed", "beta"]) == 0
    assert lifecycle.update_feed() == "beta"
    assert ct.main(["preferences", "set", "update-feed", "nightly"]) == 2


# ------------------------------------------------------------ the menu bar's hooks

def test_sigterm_goes_through_the_normal_quit(monkeypatch):
    import pipeline_monitor.app as app
    quits = []
    monkeypatch.setattr(app.rumps, "quit_application", lambda: quits.append(app.quit_reason()))
    old = signal.getsignal(signal.SIGTERM)
    try:
        monkeypatch.setitem(app._QUIT_REASON, "value", "user")
        app.install_sigterm()
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert quits == ["signal"]
    finally:
        signal.signal(signal.SIGTERM, old)


def test_before_quit_asks_lifecycle(monkeypatch):
    import pipeline_monitor.app as app
    seen = []
    monkeypatch.setattr(lifecycle, "on_quit", lambda reason: seen.append(reason) or {})
    monkeypatch.setitem(app._QUIT_REASON, "value", "user")
    app._before_quit()
    assert seen == ["user"]


# ------------------------------------------------------------ handover, Move to Applications

def test_handover_quit_stops_nothing(rec, monkeypatch):
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    out = lifecycle.on_quit("handover")
    assert out["action"].startswith("none (handed over") and rec.verbs() == []
    assert ct.stopped_reason() is None


def _fake_app(root):
    (root / "Contents" / "MacOS").mkdir(parents=True)
    (root / "Contents" / "Info.plist").write_text("<plist/>")
    (root / "Contents" / "MacOS" / "Contorch").write_text("#!/bin/sh\n")
    return root


def test_move_to_applications_copies_and_drops_quarantine(tmp_path, monkeypatch):
    src = _fake_app(tmp_path / "Volumes" / "Contorch" / "Contorch.app")
    subprocess.run(["xattr", "-w", "com.apple.quarantine", "0081;00000000;Safari;", str(src / "Contents" / "Info.plist")],
                   check=True)
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: src)
    dest_dir = tmp_path / "Applications"
    res = lifecycle.move_to_applications(dest_dir)
    assert res == {"ok": True, "dest": str(dest_dir / "Contorch.app"), "replaced": False}
    copied = dest_dir / "Contorch.app" / "Contents" / "Info.plist"
    assert copied.read_text() == "<plist/>"
    assert "com.apple.quarantine" not in subprocess.run(["xattr", str(copied)], capture_output=True, text=True).stdout
    assert (src / "Contents" / "Info.plist").exists()             # the original stays (a DMG is read-only anyway)


def test_move_to_applications_trashes_an_older_copy(tmp_path, monkeypatch):
    src = _fake_app(tmp_path / "Downloads" / "Contorch.app")
    old = _fake_app(tmp_path / "Applications" / "Contorch.app")
    (old / "Contents" / "old-marker").write_text("x")
    trashed = []

    def fake_trash(p):
        trashed.append(p)
        import shutil
        shutil.rmtree(p)
        return True
    monkeypatch.setattr(lifecycle, "_trash", fake_trash)
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: src)
    res = lifecycle.move_to_applications(tmp_path / "Applications")
    assert res["ok"] and res["replaced"] and trashed == [old]
    assert not (old / "Contents" / "old-marker").exists()


def test_move_to_applications_refuses_when_the_old_copy_stays(tmp_path, monkeypatch):
    src = _fake_app(tmp_path / "Downloads" / "Contorch.app")
    _fake_app(tmp_path / "Applications" / "Contorch.app")
    monkeypatch.setattr(lifecycle, "_trash", lambda p: False)
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: src)
    res = lifecycle.move_to_applications(tmp_path / "Applications")
    assert not res["ok"] and res["error"]["code"] == "exists"


def test_move_to_applications_when_already_there_is_a_noop(tmp_path, monkeypatch):
    app = _fake_app(tmp_path / "Applications" / "Contorch.app")
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: app)
    assert lifecycle.move_to_applications(tmp_path / "Applications")["noop"] is True


def test_move_outside_the_app(monkeypatch):
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: None)
    assert lifecycle.move_to_applications()["error"]["code"] == "not_in_app"
