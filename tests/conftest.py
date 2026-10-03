"""Shared fixtures. Every test is isolated from this Mac's real meeting-capture
agent, Gemini key and sysaudio binary: nothing here may run the installed
helper or read the real plist."""
from __future__ import annotations

import json
import os
import plistlib
import stat
from pathlib import Path

import pytest

from pipeline_monitor import transcription as stt


@pytest.fixture(autouse=True)
def _isolate_transcription(tmp_path, monkeypatch):
    monkeypatch.setattr(stt, "PLIST", tmp_path / "no-agent.plist")
    monkeypatch.setattr(stt, "KEY_FILE", tmp_path / "no-key")
    monkeypatch.setattr(stt, "BREW_HELPERS", ())
    monkeypatch.setattr(stt, "_which", lambda name: None)
    # Setup reads MEETING_CAPTURE_* from this shell (install carries them into
    # the plist), so none of the developer's may leak in (e.g. MODE=live).
    for v in [k for k in os.environ if k.startswith("MEETING_CAPTURE_")] + [
            "GOOGLE_API_KEY", "GEMINI_API_KEY", "CONTORCH_NONINTERACTIVE"]:
        monkeypatch.delenv(v, raising=False)
    stt.clear_cache()
    yield
    stt.clear_cache()


def write_plist(path: Path, env: dict) -> Path:
    path.write_bytes(plistlib.dumps({"Label": "com.contorch.meeting-capture",
                                     "ProgramArguments": ["/x/python", "-m", "meeting_capture.daemon"],
                                     "EnvironmentVariables": env}))
    return path


def fake_helper(directory: Path, probe_rc: int = 0, probe: dict | None = None,
                install_rc: int = 0, install: dict | None = None, stderr: str = "",
                sleep: float = 0, name: str = "sysaudio") -> tuple[Path, Path]:
    """A stand-in for `sysaudio transcribe` that answers the contract and logs
    its argv (one line per call). Returns (binary, call log)."""
    if probe is None:
        probe = {"available": probe_rc in (0, 75), "reason": "", "os": "26.0", "arch": "arm64",
                 "locale": "en-US", "installed": probe_rc == 0, "supported": ["en-US", "hi-IN"],
                 "installed_locales": ["en-US"] if probe_rc == 0 else []}
    if install is None:
        install = {"installed": install_rc == 0, "locale": "en-US", "seconds": 0.1}
    log = directory / f"{name}.calls"
    binary = directory / name
    binary.write_text(f"""#!/bin/sh
echo "$*" >> '{log}'
[ "{sleep}" != "0" ] && sleep {sleep}
case "$*" in
  *--probe*)   printf '%s\\n' '{json.dumps(probe)}'; printf '%s' '{stderr}' >&2; exit {probe_rc} ;;
  *--install*) printf '%s\\n' '{json.dumps(install)}'; printf '%s' '{stderr}' >&2; exit {install_rc} ;;
esac
exit 1
""")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary, log


def calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []
