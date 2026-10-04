"""owners: pm's one locator and the one way it runs an owner's JSON verb."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from conftest import on_brew
from pipeline_monitor import jsonout, owners


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_call_reads_one_document_whatever_the_exit_code(tmp_path):
    exe = _script(tmp_path / "mc", "echo progress >&2\necho '{\"schema\":\"meeting-capture.agent/1\",\"ok\":false,"
                                   "\"error\":{\"code\":\"channel_conflict\",\"message\":\"no\"}}'\nexit 3\n")
    r = owners.call("meeting-capture", "install", "--json", schema="meeting-capture.agent/", exe=str(exe))
    assert r["status"] == "ok" and r["rc"] == 3
    assert owners.error_of(r) == {"code": "channel_conflict", "message": "no"}


def test_call_reads_the_result_line_of_json_lines(tmp_path):
    exe = _script(tmp_path / "t", "echo '{\"event\":\"start\"}'\necho '{\"event\":\"result\",\"ok\":true}'\n")
    assert owners.call("contorch-transcripts", "import", exe=str(exe))["data"] == {"event": "result", "ok": True}


def test_usage_error_is_an_older_owner(tmp_path):
    exe = _script(tmp_path / "mc", "echo 'usage: meeting-capture …' >&2\nexit 2\n")
    r = owners.call("meeting-capture", "status", "--json", exe=str(exe))
    assert r["status"] == "old" and "upgrade" in r["error"]
    assert owners.error_of(r)["code"] == "owner_too_old"


@pytest.mark.parametrize("body,why", [
    ("echo 'not json'\nexit 0\n", "failed"),
    ("echo '{\"schema\":\"something.else/1\"}'\n", "unexpected document"),
    ("echo '[1,2]'\n", "unexpected document"),
])
def test_garbage_is_an_error(tmp_path, body, why):
    exe = _script(tmp_path / "x", body)
    r = owners.call("meeting-capture", "config", "--json", schema="meeting-capture.config/", exe=str(exe))
    assert r["status"] == "error" and why in r["error"]


def test_a_hung_owner_is_killed_with_its_children(tmp_path):
    pidfile = tmp_path / "child.pid"
    exe = _script(tmp_path / "slow", f"sleep 60 &\necho $! > {pidfile}\nwait\n")
    t0 = time.monotonic()
    r = owners.call("meeting-capture", "status", "--json", exe=str(exe), timeout=1)
    assert r["status"] == "error" and "timed out" in r["error"] and time.monotonic() - t0 < 10
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the grandchild survived")


def test_missing_and_env(tmp_path):
    assert owners.call("meeting-capture", "status", "--json")["status"] == "missing"
    exe = _script(tmp_path / "e", 'printf \'{"op":"%s","ch":"%s"}\' "$CONTORCH_OP" "$CONTORCH_CHANNEL"\n')
    on_brew(tmp_path, "meeting-capture", exe)
    r = owners.call("meeting-capture", "x", env={"CONTORCH_OP": "op-1", "CONTORCH_CHANNEL": "brew"})
    assert r["data"] == {"op": "op-1", "ch": "brew"}


def test_background_reads_never_run_a_homebrew_wrapper(tmp_path, monkeypatch):
    cellar = _script(tmp_path / "brew" / "Cellar" / "meeting-capture" / "0.8.0" / "bin" / "meeting-capture",
                     "echo WRAPPER\n")
    on_brew(tmp_path, "meeting-capture", cellar)                # opt/ → Cellar, as Homebrew links it
    assert owners.is_brew_wrapper(owners.locate("meeting-capture"))
    assert owners.background("meeting-capture") == (None, "not_built")
    assert owners.call("meeting-capture", "status", "--json", foreground=False)["status"] == "not_built"
    venv = _script(owners.USER_VENVS["meeting-capture"] / "bin" / "meeting-capture", "echo '{\"ok\":true}'\n")
    assert owners.background("meeting-capture") == (str(venv), None)
    assert owners.call("meeting-capture", "status", "--json", foreground=False)["data"] == {"ok": True}


def test_locate_in_the_app_channel_is_the_bundle_only(tmp_path, monkeypatch):
    app = tmp_path / "Contorch.app"
    shim = _script(app / "Contents" / "Resources" / "bin" / "contorch-memory", "")
    _script(tmp_path / "brew" / "opt" / "context-orchestrator" / "bin" / "contorch-memory", "")
    monkeypatch.setattr(owners.sys, "executable", str(app / "Contents" / "MacOS" / "contorch-python"))
    assert owners.bundle_root() == app
    assert owners.locate("contorch-memory").endswith("/opt/context-orchestrator/bin/contorch-memory")  # dev
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    assert owners.locate("contorch-memory") == str(shim)
    assert owners.locate("meeting-capture") is None              # never brew's from the app


# ------------------------------------------------------------ jsonout.py: the shared copy (Q13)

JSONOUT_SHA256 = "c83cc7365d0f5d1e3f9618511e4497efe2b2a7f720836e21c376c22377846114"


def test_jsonout_is_byte_identical_to_the_other_repos_copies():
    """meeting-capture and context-orchestrator carry the same file (sha256
    above); contorch-macos's release.lock.json will record it."""
    src = Path(jsonout.__file__).read_bytes()
    assert hashlib.sha256(src).hexdigest() == JSONOUT_SHA256


def test_jsonout_golden_output(capfd):
    with jsonout.reserved_stdout() as out:
        print("stray print goes to stderr")
        jsonout.emit({"schema": "x/1", "ok": True, "é": "ü"}, out)
    o, e = capfd.readouterr()
    assert o == '{"schema":"x/1","ok":true,"é":"ü"}\n' and "stray print" in e
    assert jsonout.error("c", "m", n=1) == {"code": "c", "message": "m", "n": 1}


def test_the_suite_cannot_run_real_system_commands():
    """conftest's guard: launchctl, brew, tccutil, claude … never run for real."""
    import subprocess
    for argv in (["launchctl", "list"], ["/bin/launchctl", "print", "gui/501"], ["brew", "services", "list"],
                 ["tccutil", "reset", "All", "com.contorch.app"]):
        with pytest.raises(AssertionError, match="real"):
            subprocess.run(argv, capture_output=True)
