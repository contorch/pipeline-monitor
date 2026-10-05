"""`contorch setup`'s permission step asks for what meeting-capture says the
recorder needs: with its Core Audio taps backend that is System Audio
Recording Only (row `system_audio`), not Screen & System Audio Recording.
Fake meeting-capture answers only; nothing is requested from macOS."""
from __future__ import annotations

import pytest

from pipeline_monitor import contorch as ct
from pipeline_monitor import ownerstate


def _row(pid, status, required, can=True):
    return {"id": pid, "status": status, "required": required, "can_request": can,
            "hint": None if status == "granted" else f"fix {pid}",
            "settings_url": f"x-apple.systempreferences:{pid}"}


def _doc(*rows):
    return {"schema": "meeting-capture.permissions/1", "ok": True, "channel": "app", "permissions": list(rows)}


@pytest.fixture
def mc(monkeypatch):
    """owners.call stand-in: answers `check --json` from `state`, records
    each --request and flips that row to granted."""
    state = {"rows": []}
    asked: list[str] = []

    def call(name, *args, **kw):
        assert name == "meeting-capture" and args[:2] == ("check", "--json")
        if "--request" in args:
            perm = args[args.index("--request") + 1]
            asked.append(perm)
            state["rows"] = [{**r, "status": "granted", "hint": None} if r["id"] == perm else r
                             for r in state["rows"]]
        return {"status": "ok", "data": _doc(*state["rows"])}

    monkeypatch.setattr(ct.owners, "call", call)
    monkeypatch.setattr(ct, "_interactive", lambda: True)
    monkeypatch.setattr(ct, "_run", lambda *a, **k: None)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    return state, asked


def _setup(mc_state):
    log, todo, done = [], [], []
    ct._permissions("/x/meeting-capture", log.append, todo, done)
    return log, todo, done


def test_taps_backend_asks_for_system_audio_then_the_mic(mc):
    state, asked = mc
    state["rows"] = [_row("screen_audio", "not_granted", False),
                     _row("system_audio", "not_determined", True),
                     _row("microphone", "not_determined", True)]
    log, todo, done = _setup(state)
    assert asked == ["system_audio", "microphone"]
    assert done == ["Permissions"] and todo == []
    assert "  ✓ System Audio Recording Only" in log


def test_unknown_system_audio_is_still_asked(mc):
    """sysaudio couldn't read the state (TCC preflight missing) but can ask."""
    state, asked = mc
    state["rows"] = [_row("screen_audio", "not_granted", False),
                     _row("system_audio", "unknown", True),
                     _row("microphone", "granted", True)]
    _setup(state)
    assert asked == ["system_audio"]


def test_rows_not_needed_are_not_asked(mc):
    """A Mac on sck: system audio is optional, so setup doesn't prompt for it."""
    state, asked = mc
    state["rows"] = [_row("screen_audio", "granted", True),
                     _row("system_audio", "not_determined", False),
                     _row("microphone", "granted", True)]
    log, todo, done = _setup(state)
    assert asked == [] and done == ["Permissions"]


def test_a_denied_required_row_becomes_a_todo_with_its_title(mc):
    state, asked = mc
    state["rows"] = [_row("system_audio", "denied", True, can=False), _row("microphone", "granted", True)]
    log, todo, done = _setup(state)
    assert asked == [] and done == []
    assert todo == ["Allow System Audio Recording Only: fix system_audio"]


def test_titles():
    assert ownerstate.PERMISSION_TITLES["system_audio"] == "System Audio Recording Only"
    probs = ownerstate.permission_problems(_doc(_row("system_audio", "denied", True)))
    assert probs[0]["title"] == "System Audio Recording Only"
