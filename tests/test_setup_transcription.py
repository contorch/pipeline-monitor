"""`contorch setup`: the Gemini key is optional when this Mac transcribes
on-device. Everything external is faked — no daemons, no real sysaudio, no
real key file, no browser."""
from __future__ import annotations

import subprocess

import pytest

from conftest import calls, fake_helper, write_plist
from pipeline_monitor import contorch as ct
from pipeline_monitor import transcription as stt


class Answers:
    """Scripted terminal: input() answers in order; getpass() too."""

    def __init__(self, monkeypatch, inputs=(), keys=()):
        self.inputs, self.keys = list(inputs), list(keys)
        self.prompts: list[str] = []
        self.key_prompts = 0
        monkeypatch.setattr("builtins.input", self._input)
        monkeypatch.setattr(ct.getpass, "getpass", self._getpass)

    def _input(self, prompt=""):
        self.prompts.append(prompt)
        return self.inputs.pop(0) if self.inputs else ""

    def _getpass(self, prompt=""):
        self.key_prompts += 1
        return self.keys.pop(0) if self.keys else ""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated setup environment; returns a recorder of commands run."""
    ran: list[list[str]] = []

    def fake_run(cmd, timeout=300):
        ran.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ct, "_run", fake_run)
    monkeypatch.setattr(ct, "KEY_FILE", tmp_path / "google" / "key")
    monkeypatch.setattr(ct, "_interactive", lambda: True)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return ran


def _run_step(**kw):
    log, todo, done = [], [], []
    change = ct._setup_transcription(log.append, todo, done, **kw)
    return change, "\n".join(log), todo, done


# ------------------------------------------------------------ on-device works

def test_on_device_enter_skips_the_key_with_no_todo(tmp_path, monkeypatch, env):
    binary, _ = fake_helper(tmp_path)
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(binary),))      # fresh install: no plist yet
    term = Answers(monkeypatch, inputs=[""])
    change, out, todo, done = _run_step()
    assert change is None and todo == []
    assert done == ["Transcription on this Mac (en-US)"]
    assert term.key_prompts == 0 and ["open", ct.AI_STUDIO] not in env
    assert "audio never leaves this Mac" in out
    assert "sent to Google" not in out                            # disclosure matches the engine
    assert "optional upgrade" in out                              # the key is offered, not demanded


def test_on_device_non_interactive_needs_no_key(tmp_path, monkeypatch, env):
    binary, _ = fake_helper(tmp_path)
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(binary),))
    monkeypatch.setattr(ct, "_interactive", lambda: False)
    term = Answers(monkeypatch)
    change, out, todo, done = _run_step()
    assert change is None and todo == [] and term.prompts == [] and term.key_prompts == 0
    assert "sent to Google" not in out and "optional upgrade" not in out


def test_choosing_gemini_asks_for_the_key_and_switches_the_engine(tmp_path, monkeypatch, env):
    binary, _ = fake_helper(tmp_path)
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(binary),))
    term = Answers(monkeypatch, inputs=["2"], keys=["AIza-new"])
    change, out, todo, done = _run_step()
    assert change == "gemini" and todo == []
    assert ct.KEY_FILE.read_text() == "AIza-new" and oct(ct.KEY_FILE.stat().st_mode & 0o777) == "0o600"
    assert ["open", ct.AI_STUDIO] in env
    assert "Meeting audio is sent to Google Gemini" in out
    assert done == ["Transcription with Gemini"]


def test_choosing_gemini_then_skipping_the_key_stays_on_device(tmp_path, monkeypatch, env):
    binary, _ = fake_helper(tmp_path)
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(binary),))
    Answers(monkeypatch, inputs=["2"], keys=[""])
    change, out, todo, done = _run_step()
    assert change is None and todo == [] and not ct.KEY_FILE.exists()
    assert "stays on this Mac" in out and "Meeting audio is sent to Google" not in out
    assert done == ["Transcription on this Mac (en-US)"]


def test_existing_gemini_setting_is_the_default_and_can_go_back_on_device(tmp_path, monkeypatch, env):
    binary, _ = fake_helper(tmp_path)
    write_plist(stt.PLIST, {"MEETING_CAPTURE_STT": "gemini", "MEETING_CAPTURE_SYSAUDIO": str(binary)})
    ct.KEY_FILE.parent.mkdir(parents=True)
    ct.KEY_FILE.write_text("AIza-old")
    term = Answers(monkeypatch, inputs=[""])
    change, out, todo, _ = _run_step()                           # Enter keeps Gemini
    assert change is None and todo == [] and "2. Gemini — needs a free API key  (default)" in out
    assert "Meeting audio is sent to Google Gemini" in out and term.key_prompts == 0
    Answers(monkeypatch, inputs=["1"])
    change, out, todo, _ = _run_step()                           # back to this Mac
    assert change == "auto" and todo == []
    assert "Gemini" in out and "takes over" in out                # honest about the auto fallback


def test_missing_model_is_installed_first(tmp_path, monkeypatch, env):
    # 75 until --install has run, then 0.
    flag = tmp_path / "installed"
    binary = tmp_path / "sysaudio"
    log = tmp_path / "sysaudio.calls"
    binary.write_text(f"""#!/bin/sh
echo "$*" >> '{log}'
case "$*" in
  *--install*) touch '{flag}'; echo '{{"installed":true,"locale":"hi-IN","seconds":14.6}}'; exit 0 ;;
  *--probe*) if [ -f '{flag}' ]; then echo '{{"available":true,"installed":true,"locale":"hi-IN"}}'; exit 0; fi
             echo '{{"available":true,"installed":false,"locale":"hi-IN","reason":"model not installed"}}'; exit 75 ;;
esac
exit 1
""")
    binary.chmod(0o755)
    write_plist(stt.PLIST, {"MEETING_CAPTURE_LOCALE": "hi-IN", "MEETING_CAPTURE_SYSAUDIO": str(binary)})
    monkeypatch.setattr(ct, "_interactive", lambda: False)
    change, out, todo, done = _run_step()
    assert calls(log) == ["transcribe --probe --locale hi-IN", "transcribe --install --locale hi-IN",
                          "transcribe --probe --locale hi-IN"]
    assert todo == [] and done == ["Transcription on this Mac (hi-IN)"]


# ------------------------------------------------------------ a key only the shell can see
# The recorder is a launchd agent: it never sees GEMINI_API_KEY / GOOGLE_API_KEY
# exported in ~/.zshrc, and `meeting-capture install` rewrites its plist env with
# only PATH and MEETING_CAPTURE_*. After setup the key file is all it can read.

def _daemon_view(monkeypatch, binary, change):
    """The engine the daemon ends up with: the plist as `meeting-capture stt`
    would leave it, read without this shell's environment."""
    write_plist(stt.PLIST, {"MEETING_CAPTURE_SYSAUDIO": str(binary),
                            **({"MEETING_CAPTURE_STT": change} if change else {})})
    monkeypatch.setattr(stt, "KEY_FILE", ct.KEY_FILE)
    for v in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    stt.clear_cache()
    return stt.current()


def test_shell_only_key_choose_gemini_saves_it_where_the_recorder_reads_it(tmp_path, monkeypatch, env):
    binary, _ = fake_helper(tmp_path)
    write_plist(stt.PLIST, {"MEETING_CAPTURE_SYSAUDIO": str(binary)})
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-in-zshrc")            # exported in the shell only
    term = Answers(monkeypatch, inputs=["2", ""])                     # Gemini; Enter = save it
    change, out, todo, done = _run_step()
    assert "GEMINI_API_KEY in your shell" in out and "launchd" in out
    assert any("Save that key" in p for p in term.prompts)
    assert ct.KEY_FILE.read_text() == "AIza-in-zshrc"
    assert oct(ct.KEY_FILE.stat().st_mode & 0o777) == "0o600"
    assert term.key_prompts == 0 and ["open", ct.AI_STUDIO] not in env  # no second key asked for
    assert change == "gemini" and todo == [] and done == ["Transcription with Gemini"]
    assert _daemon_view(monkeypatch, binary, change)["engine"] == "gemini"


def test_shell_only_key_choose_gemini_not_saved_stays_on_device(tmp_path, monkeypatch, env):
    """The review's repro: before the fix this returned "gemini" with no key the
    daemon could see, and every chunk was parked untranscribed."""
    binary, _ = fake_helper(tmp_path)
    write_plist(stt.PLIST, {"MEETING_CAPTURE_SYSAUDIO": str(binary)})
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-in-zshrc")
    term = Answers(monkeypatch, inputs=["2", "n"])
    change, out, todo, done = _run_step()
    assert change is None and todo == [] and not ct.KEY_FILE.exists()
    assert done == ["Transcription on this Mac (en-US)"]
    assert "Meeting audio is sent to Google" not in out and "stays on this Mac" in out
    assert term.key_prompts == 0 and ["open", ct.AI_STUDIO] not in env
    assert _daemon_view(monkeypatch, binary, change)["engine"] == "apple"


def test_shell_only_key_is_not_promised_as_the_auto_fallback(tmp_path, monkeypatch, env):
    binary, _ = fake_helper(tmp_path)
    write_plist(stt.PLIST, {"MEETING_CAPTURE_STT": "gemini", "MEETING_CAPTURE_SYSAUDIO": str(binary)})
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-in-zshrc")
    Answers(monkeypatch, inputs=["1"])                               # back to this Mac (auto)
    change, out, todo, done = _run_step()
    assert change == "auto" and todo == [] and "takes over" not in out
    assert not ct.KEY_FILE.exists()


def test_shell_only_key_non_interactive_set_to_gemini_is_a_todo(tmp_path, monkeypatch, env):
    binary, _ = fake_helper(tmp_path)
    write_plist(stt.PLIST, {"MEETING_CAPTURE_STT": "gemini", "MEETING_CAPTURE_SYSAUDIO": str(binary)})
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-in-zshrc")
    monkeypatch.setattr(ct, "_interactive", lambda: False)
    term = Answers(monkeypatch)
    change, out, todo, done = _run_step()
    assert change is None and done == [] and term.prompts == [] and not ct.KEY_FILE.exists()
    assert len(todo) == 1 and "GEMINI_API_KEY in your shell" in todo[0] and str(ct.KEY_FILE) in todo[0]
    assert "Transcription with Gemini" not in out


def test_key_in_the_plist_env_is_moved_to_the_key_file(tmp_path, monkeypatch, env):
    # `meeting-capture install` (run by setup) would drop it from the plist.
    binary, _ = fake_helper(tmp_path)
    write_plist(stt.PLIST, {"MEETING_CAPTURE_STT": "gemini", "MEETING_CAPTURE_SYSAUDIO": str(binary),
                            "GOOGLE_API_KEY": "AIza-in-plist"})
    term = Answers(monkeypatch, inputs=["", ""])                      # keep Gemini; save it
    change, out, todo, done = _run_step()
    assert "The recorder can't use GOOGLE_API_KEY in its launchd plist" in out
    assert ct.KEY_FILE.read_text() == "AIza-in-plist" and term.key_prompts == 0
    assert change is None and todo == [] and done == ["Transcription with Gemini"]


def test_key_file_wins_over_a_shell_key_without_asking(tmp_path, monkeypatch, env):
    binary, _ = fake_helper(tmp_path)
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(binary),))
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-in-zshrc")
    ct.KEY_FILE.parent.mkdir(parents=True)
    ct.KEY_FILE.write_text("AIza-file")
    term = Answers(monkeypatch, inputs=["2"])
    change, out, todo, done = _run_step()
    assert change == "gemini" and done == ["Transcription with Gemini"] and todo == []
    assert ct.KEY_FILE.read_text() == "AIza-file" and len(term.prompts) == 1
    assert f"Gemini key found ({ct.KEY_FILE})" in out and "your shell" not in out


# ------------------------------------------------------------ on-device unavailable

def test_old_sysaudio_falls_back_to_the_key_as_before(tmp_path, monkeypatch, env):
    old = tmp_path / "sysaudio"
    old.write_text('#!/bin/sh\necho "unknown arg: $1" >&2\nexit 1\n')
    old.chmod(0o755)
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(old),))
    monkeypatch.setattr(ct, "_interactive", lambda: False)
    change, out, todo, done = _run_step()
    assert change is None
    assert "predates on-device transcription" in out
    assert "Meeting audio is sent to Google Gemini" in out
    assert any("Add a Gemini key" in t for t in todo)


def test_unavailable_interactive_key_entered(tmp_path, monkeypatch, env):
    binary, _ = fake_helper(tmp_path, probe_rc=69, probe={"available": False, "reason": "needs Apple silicon"})
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(binary),))
    term = Answers(monkeypatch, keys=["AIza-new"])
    change, out, todo, done = _run_step()
    assert change is None and todo == [] and done == ["Gemini key"] and term.key_prompts == 1
    assert "needs Apple silicon" in out and term.prompts == []    # no engine choice to offer


def test_unavailable_shell_only_key_is_saved_for_the_recorder(tmp_path, monkeypatch, env):
    binary, _ = fake_helper(tmp_path, probe_rc=69, probe={"available": False, "reason": "needs Apple silicon"})
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(binary),))
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-in-zshrc")
    term = Answers(monkeypatch, inputs=["y"])
    change, out, todo, done = _run_step()
    assert change is None and todo == [] and done == ["Gemini key"]
    assert ct.KEY_FILE.read_text() == "AIza-in-zshrc" and term.key_prompts == 0
    assert "(environment)" not in out


def test_unavailable_shell_only_key_non_interactive_is_a_todo(tmp_path, monkeypatch, env):
    """Before the fix: '✓ Gemini key found (environment)' and done=['Gemini key'],
    with a daemon that has no key and transcribes nothing."""
    binary, _ = fake_helper(tmp_path, probe_rc=69, probe={"available": False, "reason": "needs Apple silicon"})
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(binary),))
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-in-zshrc")
    monkeypatch.setattr(ct, "_interactive", lambda: False)
    term = Answers(monkeypatch)
    change, out, todo, done = _run_step()
    assert change is None and done == [] and term.prompts == [] and not ct.KEY_FILE.exists()
    assert len(todo) == 1 and todo[0].startswith("Add a Gemini key") and "GOOGLE_API_KEY in your shell" in todo[0]


def test_unavailable_shell_only_key_declined_is_a_todo_without_opening_the_browser(tmp_path, monkeypatch, env):
    binary, _ = fake_helper(tmp_path, probe_rc=69, probe={"available": False, "reason": "needs Apple silicon"})
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(binary),))
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-in-zshrc")
    term = Answers(monkeypatch, inputs=["n"])
    change, out, todo, done = _run_step()
    assert done == [] and not ct.KEY_FILE.exists() and term.key_prompts == 0
    assert ["open", ct.AI_STUDIO] not in env
    assert len(todo) == 1 and "GEMINI_API_KEY in your shell" in todo[0]


def test_on_device_only_setting_never_asks_for_a_key(tmp_path, monkeypatch, env):
    binary, _ = fake_helper(tmp_path, probe_rc=69, probe={"available": False, "reason": "needs macOS 26 or later"})
    write_plist(stt.PLIST, {"MEETING_CAPTURE_STT": "apple", "MEETING_CAPTURE_SYSAUDIO": str(binary)})
    term = Answers(monkeypatch)
    change, out, todo, done = _run_step()
    assert change is None and term.key_prompts == 0
    assert "sent to Google" not in out
    assert todo and "meeting-capture stt auto" in todo[0]


def test_apply_stt_goes_through_meeting_capture(env):
    todo, log = [], []
    ct._apply_stt("/x/meeting-capture", None, log.append, todo)
    assert env == []
    ct._apply_stt("/x/meeting-capture", "gemini", log.append, todo)
    assert env == [["/x/meeting-capture", "stt", "gemini"]] and todo == []


# ------------------------------------------------------------ whole setup

@pytest.fixture
def stack(tmp_path, monkeypatch, env):
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    bins = {n: f"/x/{n}" for n in ("meeting-capture", "context-orchestrator-chroma",
                                   "transcript-watcher", "contorch-mcp", "claude")}
    monkeypatch.setattr(ct.shutil, "which", lambda n: bins.get(n))     # no brew
    monkeypatch.setattr(ct, "CLAUDE_JSON", tmp_path / "claude.json")
    monkeypatch.setattr(ct, "CLAUDE_MD", tmp_path / "CLAUDE.md")
    monkeypatch.setattr(ct, "LAUNCH_AGENTS", agents)
    monkeypatch.setattr(ct, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(ct, "STOPPED_MARKER", tmp_path / "state" / "stopped.json")
    monkeypatch.setattr(ct, "CHROMA_DIR", tmp_path / "chroma")
    monkeypatch.setattr(ct, "_launchctl", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(ct, "_chroma_up", lambda timeout_s: True)
    monkeypatch.setattr(ct, "_contorch_memory_bin", lambda: None)
    return env


def test_setup_succeeds_without_a_key_when_this_mac_transcribes(tmp_path, monkeypatch, stack):
    binary, _ = fake_helper(tmp_path)
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(binary),))
    term = Answers(monkeypatch)                                      # Enter at every prompt
    out: list[str] = []
    assert ct.setup(log=out.append) is True
    text = "\n".join(out)
    assert term.key_prompts == 0 and not ct.KEY_FILE.exists()
    assert "sent to Google" not in text and "✗" not in text
    assert "✓ Transcription on this Mac (en-US)" in text
    assert ["/x/meeting-capture", "install"] in stack
    assert not any(c[1:2] == ["stt"] for c in stack)                 # the default needs no change


def test_setup_applies_gemini_after_installing_the_recorder(tmp_path, monkeypatch, stack):
    binary, _ = fake_helper(tmp_path)
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(binary),))
    Answers(monkeypatch, inputs=["2"], keys=["AIza-new"])
    assert ct.setup(log=lambda *_: None) is True
    mc = [c for c in stack if c[0] == "/x/meeting-capture"]
    assert mc == [["/x/meeting-capture", "install"], ["/x/meeting-capture", "stt", "gemini"]]


def test_setup_never_switches_to_gemini_on_a_key_only_the_shell_has(tmp_path, monkeypatch, stack):
    binary, _ = fake_helper(tmp_path)
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(binary),))
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-in-zshrc")
    Answers(monkeypatch, inputs=["2", "n"])                          # Gemini, but don't save the key
    out: list[str] = []
    assert ct.setup(log=out.append) is True
    text = "\n".join(out)
    assert not any(c[1:2] == ["stt"] for c in stack)                 # stays on this Mac
    assert "✓ Transcription with Gemini" not in text and "✓ Transcription on this Mac (en-US)" in text


def test_setup_saves_a_shell_key_before_switching_to_gemini(tmp_path, monkeypatch, stack):
    binary, _ = fake_helper(tmp_path)
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(binary),))
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-in-zshrc")
    Answers(monkeypatch, inputs=["2", ""])
    assert ct.setup(log=lambda *_: None) is True
    assert ct.KEY_FILE.read_text() == "AIza-in-zshrc"
    assert ["/x/meeting-capture", "stt", "gemini"] in stack


def test_setup_without_on_device_and_without_a_key_still_reports_the_todo(tmp_path, monkeypatch, stack):
    binary, _ = fake_helper(tmp_path, probe_rc=69, probe={"available": False, "reason": "needs macOS 26 or later"})
    monkeypatch.setattr(stt, "BREW_HELPERS", (str(binary),))
    Answers(monkeypatch, keys=[""])
    out: list[str] = []
    assert ct.setup(log=out.append) is False
    assert any("✗ Add a Gemini key" in line for line in out)


# ------------------------------------------------------------ status / doctor

def test_status_and_doctor_show_the_engine(tmp_path, monkeypatch, capsys):
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.contorch.meeting-capture.plist").write_text("")
    monkeypatch.setattr(ct, "LAUNCH_AGENTS", agents)
    monkeypatch.setattr(ct, "STOPPED_MARKER", tmp_path / "stopped.json")
    monkeypatch.setattr(ct, "_launchctl", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))
    binary, _ = fake_helper(tmp_path, probe={"available": True, "installed": True, "locale": "en-GB",
                                             "installed_locales": ["en-GB", "en-US"]})
    write_plist(stt.PLIST, {"MEETING_CAPTURE_SYSAUDIO": str(binary), "MEETING_CAPTURE_LOCALE": "en-GB"})
    assert ct.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "transcription" in out and "on this Mac (en-GB)" in out and "setting: auto" in out

    monkeypatch.setattr(ct.shutil, "which", lambda n: None)
    stt.clear_cache()
    write_plist(stt.PLIST, {"MEETING_CAPTURE_STT": "gemini", "MEETING_CAPTURE_SYSAUDIO": str(binary)})
    rc = ct.doctor()
    out = capsys.readouterr().out
    assert "── transcription" in out and "✗ unavailable — set to Gemini, but there is no Gemini API key" in out
    assert rc == 1
