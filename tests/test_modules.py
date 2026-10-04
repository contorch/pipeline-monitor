"""The module registry (pipeline_monitor.modules): both channels, the user's
choice in ~/.contorch/modules.json, owners' commands in order, greyed menu
rows with a per-channel add hint, and no recorder without an engine."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline_monitor import modules as M
from pipeline_monitor import owners


@pytest.fixture
def mac(monkeypatch):
    """`have` = executables owners.locate finds (formulas installed)."""
    have: set[str] = set()
    monkeypatch.setattr(owners, "locate", lambda name: f"/opt/x/bin/{name}" if name in have else None)
    return have


def states(doc):
    return {r["id"]: r["state"] for r in doc["modules"]}


def test_fresh_app_needs_setup_and_menu_offers_setup(mac):
    doc = M.status("app")
    assert doc["set_up"] is False and set(states(doc).values()) == {"needs_setup"}
    assert M.menu_lines(doc) == [{"id": "setup", "enabled": True, "text": "Set up Contorch…",
                                  "action": {"command": "contorch setup"}}]


def test_dont_record_on_this_mac_app(mac):
    M.set_wanted(enable=["memory"], ch="app")
    doc = M.status("app", actual={"memory": True})
    assert states(doc) == {"memory": "on", "recorder": "available", "linein": "unavailable", "cli": "available"}
    assert doc["record_on_this_mac"] is False
    lines = {l["id"]: l for l in M.menu_lines(doc)}
    assert lines["recorder"]["add"]["text"] == "Turn on meeting recorder…"
    assert lines["recorder"]["add"]["command"] == "contorch modules enable recorder"
    assert lines["linein"]["text"] == "Audio interface (line-in) — needs meeting recorder"


def test_brew_memory_only_greys_recorder_with_install_line(mac):
    mac.update({"contorch-mcp", "contorch"})          # brew install contorch --without-meeting-capture
    M.set_wanted(enable=["memory"], ch="brew")
    doc = M.status("brew", actual={"memory": True})
    assert states(doc) == {"memory": "on", "recorder": "missing", "linein": "unavailable", "cli": "on"}
    rec = next(r for r in doc["modules"] if r["id"] == "recorder")
    assert rec["add"] == {"kind": "command", "command": "brew install contorch/tap/meeting-capture",
                          "text": "Meeting recorder isn't installed. In Terminal: brew install contorch/tap/meeting-capture"}


def test_brew_cannot_turn_on_missing_code(mac):
    mac.update({"contorch-mcp", "contorch"})
    with pytest.raises(RuntimeError, match="recorder"):
        M.set_wanted(enable=["linein"], ch="brew")
    p = M.plan(["linein"], ch="brew")
    assert p["missing_code"] == ["recorder", "linein"]
    assert p["add_code"] == ["brew install contorch/tap/meeting-capture"]


def test_requirements_both_ways_and_owner_steps_in_order(mac):
    M.set_wanted(enable=["memory"], ch="app")
    p = M.plan(enable=["linein"], ch="app")
    assert p["to"] == {"memory": True, "recorder": True, "linein": True, "cli": False}
    assert [s["argv"][:2] for s in p["steps"]] == [["meeting-capture", "install"]]
    M.set_wanted(enable=["linein"], ch="app")
    p = M.plan(disable=["recorder"], ch="app")
    assert p["to"]["linein"] is False and p["to"]["recorder"] is False
    assert [s["module"] for s in p["steps"]] == ["linein", "recorder"]   # leaves first


def test_no_engine_keeps_the_recorder_off(mac):
    """macOS 15 with no accepted Gemini key: nothing would transcribe it."""
    M.set_wanted(enable=["memory"], ch="app")
    p = M.plan(enable=["recorder", "linein"], ch="app", can_transcribe=False)
    assert p["to"]["recorder"] is False and p["to"]["linein"] is False
    assert p["held"]["code"] == "no_engine" and p["steps"] == []
    M.set_wanted(enable=["recorder"], ch="app", can_transcribe=False)
    doc = M.status("app", actual={"memory": True}, can_transcribe=False)
    assert states(doc)["recorder"] == "available"
    line = next(l for l in M.menu_lines(doc) if l["id"] == "recorder")
    assert line["text"].startswith("Meeting recorder: add a Gemini key to record") and line["enabled"] is False
    # an existing recorder is not switched off by the rule (only never turned on)
    M.set_wanted(enable=["recorder"], ch="app")
    assert M.plan(ch="app", can_transcribe=False)["to"]["recorder"] is True


def test_wanted_but_not_running_is_attention(mac):
    M.set_wanted(enable=["memory", "recorder"], ch="app")
    doc = M.status("app", actual={"memory": True, "recorder": False})
    assert states(doc)["recorder"] == "attention"
    assert any("Set up Contorch" in l["text"] for l in M.menu_lines(doc))


def test_existing_brew_install_migrates_without_a_question(mac):
    """No modules.json, but the recorder agent and MCP registration exist:
    what is set up is what was wanted."""
    mac.update({"contorch-mcp", "meeting-capture", "contorch"})
    doc = M.status("brew", actual={"memory": True, "recorder": True, "linein": False})
    assert doc["set_up"] and doc["inferred"]
    assert states(doc) == {"memory": "on", "recorder": "on", "linein": "available", "cli": "on"}
    assert not M.state_file().exists()                        # inferring writes nothing


def test_choice_is_shared_by_both_channels(mac):
    """Same HOME: adopting a brew install keeps 'don't record on this Mac'."""
    mac.update({"contorch-mcp", "contorch"})
    M.set_wanted(enable=["memory"], ch="brew")
    assert states(M.status("app", actual={"memory": True}))["recorder"] == "available"


def test_modules_json_shape_and_embeddings_source(mac):
    M.set_wanted(enable=["memory"], ch="app")
    M.set_embeddings_source("imported")
    d = json.loads(M.state_file().read_text())
    assert d["schema"] == "contorch.modules/1"
    assert d["wanted"] == {"memory": True, "recorder": False, "linein": False, "cli": False}
    assert d["embeddings_source"] == "imported"
    M.set_wanted(enable=["cli"], ch="app")                      # keeps the source
    assert M.embeddings_source() == "imported"
    with pytest.raises(ValueError):
        M.set_embeddings_source("magic")


def test_channel_is_the_environment_only(monkeypatch):
    """No path heuristic: a dev venv on Homebrew's Python is dev; inside an
    app bundle without the variable is dev too (doctor warns)."""
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    assert M.channel() == "app"
    monkeypatch.setenv("CONTORCH_CHANNEL", "nightly")
    assert M.channel() == "dev"
    monkeypatch.delenv("CONTORCH_CHANNEL")
    monkeypatch.setattr(owners.sys, "executable", "/opt/homebrew/Cellar/python@3.12/3.12.15/bin/python3.12")
    assert M.channel() == "dev"
    assert "CONTORCH_CHANNEL=brew" in owners.channel_warning()
    monkeypatch.setattr(owners.sys, "executable", "/Applications/Contorch.app/Contents/MacOS/contorch-python")
    assert M.channel() == "dev" and "CONTORCH_CHANNEL=app" in owners.channel_warning()
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    assert owners.channel_warning() is None


def test_json_cli(mac, capfd, monkeypatch):
    mac.update({"contorch-mcp", "contorch"})
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    monkeypatch.setattr(M, "observe", lambda ch=None: {"memory": True, "recorder": False, "linein": False,
                                                        "cli": True})
    from pipeline_monitor import contorch
    assert contorch.main(["modules", "enable", "recorder", "--json"]) == 1
    err = json.loads(capfd.readouterr().out)
    assert err["ok"] is False and err["error"]["code"] == "module_missing"
    mac.add("meeting-capture")                                  # brew install contorch/tap/meeting-capture
    assert contorch.main(["modules", "enable", "recorder", "--json"]) == 0
    doc = json.loads(capfd.readouterr().out)
    assert doc["schema"] == "contorch.modules/1" and states(doc)["linein"] == "available"
    assert contorch.main(["modules", "brew-spec"]) == 0
    spec = json.loads(capfd.readouterr().out)
    assert {"formula": "contorch", "provides": ["cli", "menu bar"],
            "depends_on": ["context-orchestrator", "meeting-capture => :recommended"]} in spec


# ------------------------------------------------------------ the cli module (app)

def test_cli_links_in_the_app_channel(tmp_path, monkeypatch):
    app = tmp_path / "Contorch.app"
    bindir = app / "Contents" / "Resources" / "bin"
    bindir.mkdir(parents=True)
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: app)
    local = M.cli_dir()
    local.mkdir(parents=True)
    (local / "meeting-capture").write_text("mine")              # the user's own file
    res = M.cli_install()
    assert res["ok"] and set(res["linked"]) == {str(local / n) for n in ("contorch", "contorch-transcripts",
                                                                          "contorch-memory")}
    assert res["kept_user_files"] == [str(local / "meeting-capture")]
    assert M.cli_install()["action"] == "none"                  # idempotent
    assert M.cli_links()["linked"] and (local / "meeting-capture").read_text() == "mine"
    assert M.cli_uninstall()["action"] == "removed"
    assert sorted(p.name for p in local.iterdir()) == ["meeting-capture"]


def test_cli_install_outside_the_app_is_a_no_op(monkeypatch):
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    assert M.cli_install()["action"] == "none"


# ------------------------------------------------------------ observe() asks the owners

def test_observe_reads_owner_json(tmp_path, monkeypatch, fake_mc):
    from conftest import on_brew
    cm = tmp_path / "cm" / "contorch-memory"
    cm.parent.mkdir()
    cm.write_text("#!/bin/sh\necho '{\"schema\":\"contorch-memory.claude/1\",\"ok\":true,"
                  "\"mcp\":{\"present\":true,\"matches\":true}}'\n")
    cm.chmod(0o755)
    on_brew(tmp_path, "contorch-memory", cm)
    fake_mc.set(json={})            # `config --json` isn't an stt read: rc 0, no JSON → falls back
    obs = M.observe("dev")
    assert obs["memory"] is True
    assert obs["recorder"] is False                              # no plist, no config doc
