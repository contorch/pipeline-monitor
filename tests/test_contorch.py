"""contorch stop/resume against a fake launchctl (no real daemons touched)."""
from __future__ import annotations

import subprocess

import pytest

from pipeline_monitor import contorch as ct


class FakeLaunchd:
    def __init__(self, labels):
        self.running = {l: 100 + i for i, l in enumerate(labels)}
        self.disabled: set[str] = set()
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        cmd, rest = args[0], args[1:]
        rc, out = 0, ""
        if cmd == "list":
            pid = self.running.get(rest[0])
            if rest[0] not in self.running:
                rc = 113
            elif pid:
                out = f'{{\n\t"PID" = {pid};\n\t"Label" = "{rest[0]}";\n}};'
        elif cmd == "disable":
            self.disabled.add(rest[0].rsplit("/", 1)[1])
        elif cmd == "enable":
            self.disabled.discard(rest[0].rsplit("/", 1)[1])
        elif cmd == "bootout":
            self.running.pop(rest[0].rsplit("/", 1)[1], None)
        elif cmd == "bootstrap":
            label = rest[1].rsplit("/", 1)[1].removesuffix(".plist")
            self.running[label] = 900 + len(self.calls)
        elif cmd == "print-disabled":
            out = "\n".join(f'\t"{l}" => disabled' for l in self.disabled)
        return subprocess.CompletedProcess(args, rc, out, "")


@pytest.fixture
def stack(tmp_path, monkeypatch):
    agents = tmp_path / "LaunchAgents"; agents.mkdir()
    labels = ["com.contorch.meeting-capture", "com.contorch.transcript-watcher",
              "com.contorch.context-orchestrator-chroma"]
    for l in labels:
        (agents / f"{l}.plist").write_text("")
    fake = FakeLaunchd(labels)
    monkeypatch.setattr(ct, "LAUNCH_AGENTS", agents)
    monkeypatch.setattr(ct, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(ct, "STOPPED_MARKER", tmp_path / "state" / "stopped.json")
    monkeypatch.setattr(ct, "_launchctl", fake)
    monkeypatch.setattr(ct, "_chroma_up", lambda timeout_s: "com.contorch.context-orchestrator-chroma" in fake.running)
    monkeypatch.setattr(ct.time, "sleep", lambda s: None)
    return fake


def test_stop_disables_and_boots_out_in_order(stack):
    assert ct.stop(log=lambda *_: None)
    assert stack.running == {}
    assert len(stack.disabled) == 3
    booted = [c[1].rsplit("/", 1)[1] for c in stack.calls if c[0] == "bootout"]
    assert booted == ["com.contorch.meeting-capture", "com.contorch.transcript-watcher",
                      "com.contorch.context-orchestrator-chroma"]
    assert ct.is_stopped()


def test_resume_reverses_order_and_clears_marker(stack):
    ct.stop(log=lambda *_: None)
    stack.calls.clear()
    assert ct.resume(log=lambda *_: None)
    started = [c[2].rsplit("/", 1)[1] for c in stack.calls if c[0] == "bootstrap"]
    assert started[0].startswith("com.contorch.context-orchestrator-chroma")
    assert started[-1].startswith("com.contorch.meeting-capture")
    assert stack.disabled == set() and len(stack.running) == 3
    assert not ct.is_stopped()


def test_status_reports_stopped(stack):
    ct.stop(log=lambda *_: None)
    rows = ct.status()
    assert all(r["pid"] is None and r["disabled"] for r in rows)


def test_legacy_label_is_picked_up(tmp_path, monkeypatch):
    agents = tmp_path / "LaunchAgents"; agents.mkdir()
    (agents / "com.stirredo.transcript-watcher.plist").write_text("")
    monkeypatch.setattr(ct, "LAUNCH_AGENTS", agents)
    assert [a["label"] for a in ct.agents()] == ["com.stirredo.transcript-watcher"]


def test_nothing_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "LAUNCH_AGENTS", tmp_path)
    assert ct.stop(log=lambda *_: None) is False
