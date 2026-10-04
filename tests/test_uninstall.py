"""`contorch uninstall` in both channels: an ordering of owner verbs; data
kept unless --remove-data (then each OWNER removes its own)."""
from __future__ import annotations

import json

import pytest

from conftest import cm_claude, mc_agent
from test_adopt import World, brew_owned
from pipeline_monitor import adopt, channel, contorch, uninstall


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_brew_uninstall(world):
    brew_owned(world)
    plan = uninstall.plan_uninstall()
    assert plan["ok"], plan
    res = uninstall.execute(plan)
    assert res["ok"], res
    assert world.changes() == [
        "brew:meeting-capture uninstall",
        "brew:meeting-capture skill uninstall",
        "brew:contorch-memory claude uninstall --channel brew",
    ]
    for c in world.calls():
        if c["cmd"].startswith(("uninstall", "skill", "claude uninstall")):
            assert c["op"] == plan["op"] and c["channel"] == "brew"
    assert ["brew", "services", "stop", "contorch/tap/contorch"] in world.system
    assert channel.read() is None                                       # marker gone
    codes = {t["code"] for t in res["todo"]}
    assert {"brew_uninstall", "data_kept"} <= codes
    assert not any(s[0] == "tccutil" for s in world.system)            # brew has no app rows


def test_app_uninstall_resets_the_apps_privacy_rows_and_cli_links(world, monkeypatch):
    world.as_app()
    channel.claim()
    monkeypatch.setattr(channel, "layout", lambda: {"bundle_root": str(world.app), "bundle_id": "com.contorch.labtest.app",
                                                    "brew_prefix": str(world.prefix)})
    res = uninstall.execute(uninstall.plan_uninstall())
    assert res["ok"], res
    assert world.changes()[0] == "app:meeting-capture uninstall"
    assert ["tccutil", "reset", "All", "com.contorch.labtest.app"] in world.system
    assert not any(s[0] == "brew" for s in world.system)                # brew's install is left alone
    assert {"trash_app", "login_item"} <= {t["code"] for t in res["todo"]}


def test_remove_data_is_asked_of_each_owner_and_kept_when_unsupported(world):
    brew_owned(world)
    world.brew["meeting-capture"].set(old=["uninstall --remove-data"])
    world.brew["contorch-memory"].set(old=["claude uninstall --channel brew --remove-data"])
    res = uninstall.execute(uninstall.plan_uninstall(remove_data=True))
    assert res["ok"], res
    ch = world.changes()
    assert "brew:meeting-capture uninstall --remove-data" in ch and "brew:meeting-capture uninstall" in ch
    assert "brew:contorch-memory claude uninstall --channel brew" in ch            # still disconnected
    kept = [t for t in res["todo"] if t["code"] == "data_kept"]
    assert len(kept) == 2 and all("kept" in t["message"] for t in kept)


def test_meeting_capture_0_7_uninstalls_with_its_plain_command(world):
    brew_owned(world)
    world.brew["meeting-capture"].set(old=["uninstall", "skill", "status", "config"])
    plan = uninstall.plan_uninstall(not_recording=True)
    res = uninstall.execute(plan)
    assert res["ok"], res
    assert [world.brew["meeting-capture"].path.name, "uninstall"] in world.system  # the text verb, via run_system
    assert channel.read() is None


def test_another_owners_install_is_not_uninstalled_from_here(world):
    brew_owned(world)
    world.as_app()
    plan = uninstall.plan_uninstall()
    assert plan["ok"] is False and plan["error"]["code"] == "channel_conflict"


def test_an_owner_that_is_gone_can_be_cleaned_up(world, monkeypatch):
    world.as_app()
    channel.claim()
    m = channel.read()
    m["layout"]["bundle_root"] = str(world.tmp / "Trash" / "Contorch.app")      # the app was trashed
    channel.write(m)
    world.as_brew()
    assert channel.check()["code"] == "owner_gone"
    assert uninstall.plan_uninstall()["ok"]


def test_refuses_while_recording(world):
    brew_owned(world)
    world.brew["meeting-capture"].answer("status", {"schema": "meeting-capture.status/1", "ok": True,
                                                     "recording": True})
    assert uninstall.plan_uninstall()["error"]["code"] == "recording_in_progress"


def test_an_interrupted_uninstall_is_finished_by_running_it_again(world):
    brew_owned(world)
    world.brew["contorch-memory"].answer("claude uninstall", cm_claude("uninstall", ok=False,
                                                                       error={"code": "settings_invalid",
                                                                              "message": "bad json"}), rc=1)
    first = uninstall.plan_uninstall()
    res = uninstall.execute(first)
    assert res["ok"] is False and res["error"]["code"] == "settings_invalid"
    assert channel.read()["state"] == "uninstalling"
    world.defaults(world.brew["contorch-memory"])
    again = uninstall.plan_uninstall()
    assert again["resume"] and again["op"] == first["op"]
    assert uninstall.execute(again)["ok"] and channel.read() is None


def test_cli_plan_json(world, capfd):
    brew_owned(world)
    assert contorch.main(["uninstall", "--plan", "--json"]) == 0
    doc = json.loads(capfd.readouterr().out)
    assert doc["schema"] == "contorch.uninstall.plan/1" and doc["steps"][0]["kind"] == "mark_uninstalling"
    assert channel.read()["owner"] == "brew"
