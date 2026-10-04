#!/usr/bin/env python3
"""Run the channel-guard READERS against this repo's fixtures
(contract/channel_guard/*.json): meeting-capture's channel_guard.allowed() and
context-orchestrator's sourced bash file, each fetched from its repo.

    python3 scripts/check_guard_readers.py --mc PATH/channel_guard.py --co PATH/contorch_channel_guard.sh

The bash reader runs under /bin/bash (macOS's 3.2), as the installers do.
Exit 0 when every reader agrees with every fixture's expected exit code (and,
for a refusal, prints the expected message on stderr)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

FIXTURES = sorted((Path(__file__).resolve().parent.parent / "contract" / "channel_guard").glob("*.json"))

MC_RUNNER = r'''
import importlib.util, sys
spec = importlib.util.spec_from_file_location("channel_guard", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ok, msg = m.allowed(sys.argv[2])
if not ok:
    sys.stderr.write((msg or "") + "\n"); sys.exit(3)
'''


def _write_marker(d: Path, f: dict) -> Path:
    p = d / "channel.json"
    if "marker_raw" in f:
        p.write_text(f["marker_raw"])
    elif f["marker"] is not None:
        p.write_text(json.dumps(f["marker"]))
    return p


def _env(f: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("CONTORCH_CHANNEL", "CONTORCH_OP")}
    env.update(f["env"])
    return env


def _check(name: str, f: dict, rc: int, err: str) -> list[str]:
    want = f["expect"]
    bad = []
    if rc != want["exit"]:
        bad.append(f"{name}: exit {rc}, expected {want['exit']} ({f['description']})")
    if want["exit"] == 3:
        if "stderr" in want and err.strip() != want["stderr"].strip():
            bad.append(f"{name}: stderr {err.strip()!r}, expected {want['stderr']!r}")
        if "stderr_contains" in want and want["stderr_contains"] not in err:
            bad.append(f"{name}: stderr lacks {want['stderr_contains']!r}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mc", help="meeting-capture's src/meeting_capture/channel_guard.py")
    ap.add_argument("--co", help="context-orchestrator's scripts/contorch_channel_guard.sh")
    a = ap.parse_args()
    failures: list[str] = []
    runs = 0
    for path in FIXTURES:
        f = json.loads(path.read_text())
        with tempfile.TemporaryDirectory() as d:
            marker = _write_marker(Path(d), f)
            if a.mc:
                r = subprocess.run([sys.executable, "-c", MC_RUNNER, a.mc, str(marker)], env=_env(f),
                                   capture_output=True, text=True)
                failures += _check(f"meeting-capture {path.stem}", f, r.returncode, r.stderr)
                runs += 1
            if a.co:
                env = {**_env(f), "CONTORCH_CHANNEL_MARKER": str(marker), "LC_ALL": "en_US.UTF-8"}
                r = subprocess.run(["/bin/bash", "-c", 'set -euo pipefail; . "$1"; contorch_channel_guard', "_", a.co],
                                   env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
                failures += _check(f"context-orchestrator {path.stem}", f, r.returncode, r.stderr)
                runs += 1
    for line in failures:
        print("✗", line)
    print(f"{runs - len(failures)}/{runs} reader runs agree with {len(FIXTURES)} fixtures")
    return 1 if failures or not runs else 0


if __name__ == "__main__":
    sys.exit(main())
