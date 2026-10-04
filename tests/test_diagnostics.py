"""`contorch smoke` is context-orchestrator's selftest; the diagnostics
bundle is the owners' own JSON plus redacted log tails."""
from __future__ import annotations

import json

from conftest import FakeOwner, on_brew
from pipeline_monitor import contorch, diagnostics


def _cm(tmp_path, selftest):
    o = FakeOwner(tmp_path / "co", "contorch-memory")
    o.answer("selftest", selftest, rc=0 if selftest.get("ok") else 1)
    o.answer("status", {"schema": "contorch-memory.status/1", "ok": True, "vector_index": "in_process"})
    on_brew(tmp_path, "contorch-memory", o.path)
    return o


def test_smoke_is_the_selftest(tmp_path, capfd):
    _cm(tmp_path, {"schema": "contorch-memory.selftest/1", "ok": True, "stage": "done", "ms": 840})
    d = diagnostics.smoke()
    assert d["ok"] and d["summary"] == "write → search → delete in 840 ms"
    assert contorch.main(["smoke", "--json"]) == 0
    assert json.loads(capfd.readouterr().out)["schema"] == "contorch.smoke/1"


def test_smoke_offline_says_keyword_search(tmp_path):
    _cm(tmp_path, {"schema": "contorch-memory.selftest/1", "ok": False, "stage": "embed", "ms": 10,
                   "error": {"code": "offline", "message": "no network"}})
    d = diagnostics.smoke()
    assert not d["ok"] and "keywords" in d["summary"]


def test_smoke_without_context_orchestrator():
    d = diagnostics.smoke()
    assert not d["ok"] and d["error"]["code"] == "owner_missing"


def test_the_old_smoke_test_is_gone():
    from pathlib import Path
    pkg = Path(diagnostics.__file__).parent
    assert not (pkg / "smoketest.py").exists()
    assert "_find_orch_root" not in "".join(p.read_text() for p in pkg.glob("*.py"))


def test_redaction():
    text = ("2026-10-04 INFO key=AIzaSyA-0123456789abcdefghijklmnopqrstu loaded\n"
            "2026-10-04 INFO mail from jane.doe@example.com\n"
            "2026-10-04 INFO chunk 9.1s [them] -> meeting-x (226 chars)\n"
            "[13:31:39] **Me:** the secret acquisition closes Friday\n"
            '2026-10-04 INFO live: "we should ship the zebra migration on thursday"\n')
    out = diagnostics.redact(text)
    assert "AIza" not in out and "jane.doe" not in out
    assert "secret acquisition" not in out and "zebra" not in out
    assert "chunk 9.1s [them] -> meeting-x (226 chars)" in out


def test_bundle_has_the_owners_json_and_no_secrets(tmp_path, fake_mc, capfd):
    _cm(tmp_path, {"schema": "contorch-memory.selftest/1", "ok": True})
    logdir = tmp_path / "home" / ".meeting-capture"
    logdir.mkdir(parents=True)
    (logdir / "daemon.log").write_text("INFO Gemini key AIzaSyA-0123456789abcdefghijklmnopqrstu\n"
                                       "**Them:** confidential numbers\n")
    assert contorch.main(["doctor", "--json", "--bundle"]) == 0
    doc = json.loads(capfd.readouterr().out)
    assert doc["schema"] == "contorch.diagnostics/1" and doc["versions"]["pipeline-monitor"]
    assert doc["owners"]["contorch_memory.status"]["vector_index"] == "in_process"
    assert doc["owners"]["meeting_capture.stt"]["engine"] == "apple"
    blob = json.dumps(doc)
    assert "AIza" not in blob and "confidential" not in blob
    assert "meeting-capture/daemon.log" in doc["logs"]
    assert contorch.main(["doctor", "--json"]) == 0
    assert "logs" not in json.loads(capfd.readouterr().out)
