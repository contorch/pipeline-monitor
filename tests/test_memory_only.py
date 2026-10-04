"""A memory-only Mac ("Record meetings on this Mac? No", or `brew install
contorch --without-meeting-capture`): no recorder, nothing in the background,
no error for the missing recorder (ported from the lab's
pm_memory_only_probe.py)."""
from __future__ import annotations

import json

import pytest

from conftest import FakeOwner, on_brew
from pipeline_monitor import contorch, modules, owners
from pipeline_monitor import status as st

MEMORY = {"schema": "contorch-memory.status/1", "ok": True, "embeddings": "gemini-embedding-001",
          "vector_index": "in_process", "docs": 12, "transcripts": 3, "index_compatible": True}


@pytest.fixture
def lite(tmp_path, monkeypatch):
    """brew install contorch --without-meeting-capture, set up for memory only."""
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    cm = FakeOwner(tmp_path / "co", "contorch-memory")
    cm.answer("status", MEMORY)
    cm.answer("claude status", {"schema": "contorch-memory.claude/1", "ok": True, "mcp": {"present": True,
                                                                                          "matches": True}})
    on_brew(tmp_path, "contorch-memory", cm.path)
    mcp = FakeOwner(tmp_path / "co", "contorch-mcp")
    on_brew(tmp_path, "contorch-mcp", mcp.path)
    modules._write({"memory": True, "recorder": False, "linein": False, "cli": True})
    return cm


def _snap():
    st.ownerstate.claude(wait=True)          # what the 5-second timer would have by now
    return st.collect(wait=True)


def test_overall_is_idle_and_the_headline_says_memory_only(lite):
    snap = _snap()
    assert snap.memory_only() and snap.headline() == "memory_only"
    assert snap.overall() == "idle"
    states = {r["id"]: r["state"] for r in snap.modules["modules"]}
    assert states == {"memory": "on", "recorder": "missing", "linein": "unavailable", "cli": "on"}


def test_the_menu(lite):
    import pipeline_monitor.app as app
    snap = _snap()
    assert app._build_status_line(snap).title == "Memory only — this Mac doesn't record"
    assert app._build_system_line(snap).title == "Background: nothing runs"
    assert app._build_index_line(snap).title.startswith("Index: 12 docs · Gemini · 3 transcripts")
    rows = [i.title for i in app._module_rows(snap)]
    assert rows == ["Meeting recorder", "    Copy: brew install contorch/tap/meeting-capture",
                    "Audio interface (line-in) — needs meeting recorder"]
    texts = " ".join(rows)
    assert "ConnectError" not in texts and "chroma" not in texts.lower()


def test_contorch_status_says_memory_only_and_exits_0(lite, capfd):
    """M3 exit criterion: `contorch status` on a memory-only scratch HOME."""
    assert contorch.main(["status"]) == 0
    out = capfd.readouterr().out
    assert out.splitlines()[0] == "Memory only — this Mac doesn't record"
    assert "background               nothing runs" in out and "12 docs" in out
    assert "transcription" not in out


def test_app_memory_only_recorder_row_offers_turning_it_on(lite, monkeypatch):
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: None)
    doc = modules.status("app", actual={"memory": True})
    rows = {l["id"]: l for l in modules.menu_lines(doc)}
    assert rows["recorder"]["add"]["text"] == "Turn on meeting recorder…"


def test_brews_always_on_cli_alone_is_not_a_set_up_mac():
    """Homebrew always provides the cli module; that alone mustn't read as
    'memory only, set up' on a Mac where nothing is set up yet."""
    assert modules.infer_wanted({"memory": False, "recorder": None, "linein": None, "cli": True}) is None


def test_the_menu_never_says_not_set_up_while_meeting_capture_is_still_answering(fake_mc, monkeypatch):
    """The first refresh asks meeting-capture in the background: until it has
    answered, the recorder is unknown, not 'on but not set up'."""
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    modules._write({"memory": True, "recorder": True, "linein": False, "cli": True})
    fake_mc.set(cmd_sleep={"config --json": 1.0})
    doc = st.modules_status(wait=False)
    assert {r["id"]: r["state"] for r in doc["modules"]}["recorder"] == "on"
