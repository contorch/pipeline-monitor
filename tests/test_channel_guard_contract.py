"""The channel-guard reader contract (contract/channel_guard/*.json).

pipeline-monitor owns the rule and writes the marker; meeting-capture and
context-orchestrator only test membership and run these same fixtures. This
test checks every fixture against a reference reader written from the rule
in contract/channel_guard/README.md, so a fixture can't contradict it."""
import json
from pathlib import Path

import pytest

FIXTURES = sorted((Path(__file__).resolve().parent.parent / "contract" / "channel_guard").glob("*.json"))


def reference_reader(marker_text, env: dict) -> int:
    if marker_text is None:
        return 0
    try:
        m = json.loads(marker_text)
    except ValueError:
        return 3
    me = env.get("CONTORCH_CHANNEL") or "dev"
    if me not in ("app", "brew", "dev"):
        me = "dev"
    writers = m.get("writers") if isinstance(m.get("writers"), list) else []
    op = m.get("op")
    ok = me in writers
    if op is not None and (not isinstance(op, dict) or env.get("CONTORCH_OP") != op.get("id")):
        ok = False
    return 0 if ok else 3


def test_there_are_fixtures():
    assert len(FIXTURES) >= 15


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture_matches_the_rule(path):
    f = json.loads(path.read_text())
    assert set(f) <= {"description", "env", "marker", "marker_raw", "expect"}
    assert ("marker" in f) != ("marker_raw" in f)
    text = f["marker_raw"] if "marker_raw" in f else (
        None if f["marker"] is None else json.dumps(f["marker"]))
    assert reference_reader(text, f["env"]) == f["expect"]["exit"]
    if f["expect"]["exit"] == 3:
        assert "stderr" in f["expect"] or "stderr_contains" in f["expect"]
