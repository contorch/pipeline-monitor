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


def test_mcp_add_puts_name_before_variadic_env():
    cmd = ct.mcp_add_cmd("claude", {"CO_A": "1", "CO_B": "2"}, "/bin/contorch-mcp")
    assert cmd[:6] == ["claude", "mcp", "add", "--scope", "user", ct.MCP_NAME]
    assert cmd[-2:] == ["--", "/bin/contorch-mcp"]
    assert cmd.index(ct.MCP_NAME) < cmd.index("-e")


def test_setup_embeddings_writes_choice_and_keeps_it_when_non_interactive(monkeypatch):
    calls = []

    class R:
        def __init__(self, out=""): self.returncode, self.stdout, self.stderr = 0, out, ""

    def fake_run(cmd, timeout=300):
        calls.append(cmd)
        return R("none — keyword (full-text) search only" if cmd[-1] == "embeddings" else "embeddings: none")
    monkeypatch.setattr(ct, "_run", fake_run)
    monkeypatch.setattr(ct, "_contorch_memory_bin", lambda: "/x/contorch-memory")
    monkeypatch.setattr(ct, "_interactive", lambda: False)
    todo, done, log = [], [], []
    ct._setup_embeddings(log.append, todo, done)                 # non-interactive: no change
    assert calls == [["/x/contorch-memory", "embeddings"]] and done == ["Search embeddings"]
    ct._setup_embeddings(log.append, todo, done, choice="none")  # explicit choice is written
    assert calls[-1] == ["/x/contorch-memory", "embeddings", "none"]


def test_start_new_meeting_runs_meeting_capture_new(monkeypatch):
    import pipeline_monitor.app as app
    calls, notes = [], []
    monkeypatch.setattr(app, "_meeting_capture_bin", lambda: "/x/meeting-capture")

    class R:
        returncode, stdout, stderr = 0, "new meeting", ""
    monkeypatch.setattr(app.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or R())
    monkeypatch.setattr(app.rumps, "notification", lambda *a: notes.append(a))
    app.PipelineMonitor._on_new_meeting(object(), None)
    assert calls == [["/x/meeting-capture", "new"]]
    assert notes and notes[0][1] == "New meeting started"


def test_copy_transcript_puts_the_whole_text_on_the_clipboard(monkeypatch):
    import pipeline_monitor.app as app
    body = "# Meeting transcript m\n\n[13:31:39] **Me:** hello there\n\n[13:32:41] **Them:** hi back\n"
    monkeypatch.setattr(app.st, "transcript_text", lambda mid: body if mid == "m" else None)
    clip, notes = [], []
    monkeypatch.setattr(app.subprocess, "run",
                        lambda cmd, input=None, check=False: clip.append((cmd, input)) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(app.rumps, "notification", lambda *a: notes.append(a))
    app._copy_transcript_callback({"meeting_id": "m", "title": "m"})(None)
    assert clip == [(["pbcopy"], body.encode())]
    assert notes[0][1] == "Transcript copied"
    app._copy_transcript_callback({"meeting_id": "missing", "title": "x"})(None)
    assert notes[-1][1] == "Transcript not found" and len(clip) == 1


def test_recent_sessions_each_have_copy_and_open(monkeypatch):
    import pipeline_monitor.app as app
    snap = app.st.Snapshot(recordings={"ok": True, "sessions": [
        {"name": "m1", "title": "m1", "meeting_id": "m1", "path": None, "size": 2048, "age_s": 60}]})
    menu = app._build_recent_submenu(snap)
    (item,) = list(menu.values())
    assert [i.title for i in item.values()] == ["Copy transcript", "Open in TextEdit"]
