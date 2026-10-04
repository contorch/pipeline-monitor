"""The channel rule (pipeline_monitor.channel): the only copy. It writes its
answer into the marker (writers, op, blocked_message); meeting-capture and
context-orchestrator only test membership against the shared fixtures in
contract/channel_guard/."""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline_monitor import channel, owners

FIXTURES = sorted((Path(__file__).resolve().parent.parent / "contract" / "channel_guard").glob("*.json"))
# Markers pm never writes; the readers must still refuse them safely.
READER_ROBUSTNESS = {"05-unset-is-dev-blocked", "12-op-token-not-member", "13-empty-writers",
                     "15-unreadable-marker"}


def _fixture(path):
    return json.loads(path.read_text())


# ------------------------------------------------------------ the rule table

@pytest.mark.parametrize("owner,state,to,writers,kind,by", [
    ("brew", "ok", None, ["brew"], None, None),
    ("app", "ok", None, ["app"], None, None),
    ("dev", "ok", None, ["dev"], None, None),
    ("brew", "adopting", "app", ["app", "brew"], "adopt", "app"),
    ("app", "adopting", "brew", ["brew", "app"], "adopt", "brew"),
    ("app", "adopting", "app", ["app"], "adopt", "app"),                 # resume an own adopt
    ("app", "uninstalling", None, ["app"], "uninstall", "app"),
])
def test_writers_for(owner, state, to, writers, kind, by):
    w, op = channel.writers_for(owner, state, to, op_id="op-1")
    assert w == writers
    assert (op is None) == (kind is None)
    if op:
        assert op == {"id": "op-1", "kind": kind, "by": by}


def test_writers_for_refuses_nonsense():
    with pytest.raises(ValueError):
        channel.writers_for("nightly")
    with pytest.raises(ValueError):
        channel.writers_for("brew", "adopting")
    with pytest.raises(ValueError):
        channel.writers_for("brew", "exploding")


def _rule_cases():
    for owner, state in itertools.product(owners.CHANNELS, channel.STATES):
        tos = owners.CHANNELS if state == "adopting" else (None,)
        for to in tos:
            w, op = channel.writers_for(owner, state, to, op_id="X")
            yield {"writers": w, "op": None if op is None else {"kind": op["kind"], "by": op["by"]}}


PRODUCED = [p for p in FIXTURES if p.stem not in READER_ROBUSTNESS and _fixture(p).get("marker")]


def test_the_robustness_list_is_exact():
    """Every fixture with a marker is either produced by the rule or listed
    as one pm never writes."""
    assert len(PRODUCED) + len(READER_ROBUSTNESS) + sum(
        1 for p in FIXTURES if "marker" in _fixture(p) and _fixture(p)["marker"] is None) == len(FIXTURES)


@pytest.mark.parametrize("path", PRODUCED, ids=lambda p: p.stem)
def test_every_fixture_marker_is_one_the_rule_produces(path):
    m = _fixture(path)["marker"]
    shape = {"writers": m["writers"],
             "op": None if m.get("op") is None else {"kind": m["op"]["kind"], "by": m["op"]["by"]}}
    assert shape in list(_rule_cases())


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_pm_membership_test_agrees_with_every_fixture(path):
    f = _fixture(path)
    if "marker_raw" in f:
        marker = None
        try:
            marker = json.loads(f["marker_raw"])
        except ValueError:
            assert f["expect"]["exit"] == 3            # unreadable: refused
            return
    else:
        marker = f["marker"]
    me = f["env"].get("CONTORCH_CHANNEL") or "dev"
    ok = channel.allowed(marker, me if me in owners.CHANNELS else "dev", f["env"].get("CONTORCH_OP"))
    assert (0 if ok else 3) == f["expect"]["exit"]


def test_a_built_marker_passes_the_readers_own_code(tmp_path):
    """pm's marker, read by meeting-capture's reader logic (fixture form)."""
    m = channel.build("brew", "adopting", adopting_to="app", op_id="op-9")
    assert m["writers"] == ["app", "brew"] and m["op"]["id"] == "op-9" and m["adopting_to"] == "app"
    assert channel.allowed(m, "brew", "op-9") and not channel.allowed(m, "brew", None)
    assert channel.allowed(m, "app", "op-9") and not channel.allowed(m, "dev", "op-9")
    assert m["blocked_message"] and m["schema"] == "contorch.channel/1"


# ------------------------------------------------------------ marker, check, claim

def test_no_marker_everyone_may_write_and_claim_takes_ownership(monkeypatch):
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    assert channel.read() is None and channel.check()["ok"]
    res = channel.claim()
    assert res["ok"] and res["changed"]
    m = channel.read()
    assert m["owner"] == "brew" and m["writers"] == ["brew"] and m["state"] == "ok"
    assert "op" not in m and m["layout"]["brew_prefix"]
    assert channel.claim()["changed"] is False                     # idempotent
    assert oct(channel.marker_path().stat().st_mode)[-3:] in ("644", "600", "664")


def test_another_owner_is_a_channel_conflict_and_claim_never_takes_over(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    (tmp_path / "brew" / "opt" / "contorch").mkdir(parents=True)     # the brew install is there
    channel.write(channel.build("brew"))
    res = channel.check()
    assert res == {**res, "ok": False, "code": "channel_conflict", "owner": "brew", "me": "app"}
    assert "Homebrew" in res["message"]
    assert channel.claim()["ok"] is False and channel.read()["owner"] == "brew"
    (tmp_path / "brew" / "opt" / "contorch").rmdir()                  # brew uninstall contorch
    assert channel.check()["code"] == "owner_gone"


def test_an_interrupted_operation_blocks_everyone_but_its_children(monkeypatch):
    channel.write(channel.build("brew", "adopting", adopting_to="app", op_id="op-5"))
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    assert channel.check()["code"] == "interrupted"
    monkeypatch.setenv("CONTORCH_OP", "op-5")
    assert channel.check()["ok"]
    # a child of the op never rewrites the marker with claim()
    assert channel.claim()["marker"]["state"] == "adopting"


def test_unreadable_marker(monkeypatch):
    channel.marker_path().parent.mkdir(parents=True)
    channel.marker_path().write_text("{nope")
    assert channel.check()["code"] == "marker_unreadable"
    assert channel.attention() == [{"code": "marker_unreadable", "path": str(channel.marker_path())}]


def test_owner_gone_app_trashed(tmp_path, monkeypatch):
    app = tmp_path / "Contorch.app"
    m = channel.build("app")
    m["layout"] = {"bundle_root": str(app), "brew_prefix": str(tmp_path / "brew")}
    channel.write(m)
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    assert channel.check()["code"] == "owner_gone"
    assert {"code": "owner_gone", "owner": "app"} in channel.attention()
    app.mkdir()
    assert channel.check()["code"] == "channel_conflict"


def test_attention_mixed_channels_and_brew_relinked(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    m = channel.build("app")
    app = tmp_path / "Contorch.app"
    app.mkdir()
    m["layout"] = {"bundle_root": str(app), "brew_prefix": str(tmp_path / "brew")}
    channel.write(m)
    assert channel.attention() == []
    a = channel.attention(recorder_backend="launchctl", mcp_present=True, mcp_matches=False)
    assert a[0]["code"] == "mixed_channels" and {f["surface"] for f in a[0]["foreign"]} == {"recorder", "mcp"}
    linked = tmp_path / "brew" / "var" / "homebrew" / "linked"
    linked.mkdir(parents=True)
    (linked / "contorch").symlink_to(tmp_path)
    assert any(x["code"] == "brew_relinked" and x["formulas"] == ["contorch"] for x in channel.attention())


def test_marker_write_is_atomic_and_leaves_no_temp_files():
    channel.write(channel.build("dev"))
    assert [p.name for p in channel.marker_path().parent.iterdir()] == ["channel.json"]


# ------------------------------------------------------------ `contorch channel --json`

def _contorch(*args, env=None):
    e = {**os.environ, **(env or {})}
    return subprocess.run([sys.executable, "-m", "pipeline_monitor.contorch", *args],
                          capture_output=True, text=True, env=e, timeout=60)


def test_cli_on_a_brew_shaped_home(monkeypatch):
    """M3 exit criterion: `contorch channel --json | jq -e '.owner=="brew" and .writers==["brew"]'`."""
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    channel.claim()
    r = _contorch("channel", "--json", env={"CONTORCH_CHANNEL": "brew"})
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["schema"] == "contorch.channel.status/1"
    assert doc["owner"] == "brew" and doc["writers"] == ["brew"] and doc["may_write"] is True
    r = _contorch("channel", env={"CONTORCH_CHANNEL": "app"})
    assert "owner:         brew" in r.stdout and r.stdout.strip()
    assert json.loads(_contorch("channel", "--json", env={"CONTORCH_CHANNEL": "app"}).stdout)["may_write"] is False
