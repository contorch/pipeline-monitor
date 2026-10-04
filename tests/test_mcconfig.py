"""mcconfig: meeting-capture's settings from `meeting-capture config --json`,
cached on its watch_paths; meeting-capture 0.7 (no config --json) falls back
to the plist."""
from __future__ import annotations

import json
import os

from conftest import write_plist
from pipeline_monitor import mcconfig


def _doc(tmp_path, **settings):
    env = tmp_path / "mc-env"
    env.write_text("x")
    return {"schema": "meeting-capture.config/1", "ok": True, "file": str(env), "watch_paths": [str(env)],
            "restart_pending": False,
            "agent": {"backend": "launchctl", "installed": True, "sysaudio": "/x/sysaudio", "plist": "/x.plist"},
            "settings": {k: {"value": v, "source": "file"} for k, v in settings.items()}, "overridden": []}


def test_reads_config_json_and_caches_until_a_watched_file_changes(tmp_path, fake_mc):
    doc = _doc(tmp_path, mode="live", source="linein", stt=None)
    fake_mc.set(lines={"config --json": [json.dumps(doc)]})
    assert mcconfig.setting("mode") == "live" and mcconfig.setting("source") == "linein"
    assert mcconfig.setting("stt", "auto") == "auto"
    assert mcconfig.installed() and mcconfig.agent()["sysaudio"] == "/x/sysaudio"
    assert fake_mc.changes() == [["config", "--json"]]           # one read, cached
    doc["settings"]["mode"]["value"] = "batch"
    fake_mc.set(lines={"config --json": [json.dumps(doc)]})
    assert mcconfig.setting("mode") == "live"                    # nothing watched changed
    os.utime(doc["watch_paths"][0], (1, 1))
    assert mcconfig.setting("mode") == "batch"


def test_meeting_capture_0_7_falls_back_to_the_plist(tmp_path, fake_mc):
    fake_mc.set(rc={"config --json": 2})
    assert mcconfig.doc() is None and mcconfig.installed() is False
    write_plist(mcconfig.LEGACY_PLIST, {"MEETING_CAPTURE_MODE": "live", "MEETING_CAPTURE_SYSAUDIO": "/o/sysaudio"})
    assert mcconfig.setting("mode") == "live" and mcconfig.installed()
    assert mcconfig.agent() == {"backend": "launchctl", "installed": True, "sysaudio": "/o/sysaudio",
                                "plist": str(mcconfig.LEGACY_PLIST), "legacy": True}
    n = len(fake_mc.changes())
    mcconfig.setting("source")
    assert len(fake_mc.changes()) == n                           # an old owner isn't asked every refresh


def test_no_meeting_capture():
    assert mcconfig.doc() is None and mcconfig.setting("mode", "batch") == "batch"
    assert mcconfig.agent()["backend"] == "none"
