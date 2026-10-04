"""`contorch setup`, status and doctor: how meetings are transcribed comes from
meeting-capture (`meeting-capture stt --json`, a fake one here), the choice is
applied through meeting-capture's own commands, and every word about where
audio goes comes from its answer. Nothing real is run: no daemons, no
sysaudio, no key file, no browser."""
from __future__ import annotations

import subprocess

import pytest

from conftest import DUTCH, LIVE_ON, NEEDS_MODEL, UNAVAILABLE, mc_json, write_plist
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
    """Isolated setup environment; returns a recorder of the commands it ran
    through ct._run (install, open, brew, claude — not meeting-capture's
    `stt`/`language`, which the fake meeting-capture logs itself)."""
    ran: list[list[str]] = []

    def fake_run(cmd, timeout=300):
        ran.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    monkeypatch.setattr(ct, "LAUNCH_AGENTS", agents)
    monkeypatch.setattr(ct, "_run", fake_run)
    monkeypatch.setattr(ct, "KEY_FILE", tmp_path / "google" / "key")
    monkeypatch.setattr(ct, "_interactive", lambda: True)
    return ran


def _choose(fake_mc):
    log, todo = [], []
    cmd = ct._choose_transcription(log.append, todo, str(fake_mc.path))
    return cmd, "\n".join(log), todo


def _report(fake_mc):
    log, todo, done = [], [], []
    ct._report_transcription(str(fake_mc.path), log.append, todo, done)
    return "\n".join(log), todo, done


def _key_file(text="AIza-file"):
    ct.KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    ct.KEY_FILE.write_text(text)
    ct.KEY_FILE.chmod(0o600)


# ------------------------------------------------------------ on-device works

def test_on_device_enter_needs_no_key_and_no_change(fake_mc, monkeypatch, env):
    term = Answers(monkeypatch, inputs=[""])
    cmd, out, todo = _choose(fake_mc)
    assert cmd is None and todo == [] and term.key_prompts == 0 and ["open", ct.AI_STUDIO] not in env
    assert "optional upgrade" in out and "1. On this Mac (en-US)  (default)" in out
    out, todo, done = _report(fake_mc)
    assert "✓ transcription: on this Mac (en-US)" in out
    assert "Meeting audio never leaves this Mac" in out and "Google" not in out
    assert done == ["Transcription: on this Mac (en-US)"] and todo == []
    assert fake_mc.changes() == []


def test_on_device_non_interactive_asks_nothing(fake_mc, monkeypatch, env):
    monkeypatch.setattr(ct, "_interactive", lambda: False)
    term = Answers(monkeypatch)
    cmd, out, todo = _choose(fake_mc)
    assert cmd is None and todo == [] and term.prompts == [] and term.key_prompts == 0
    assert "optional upgrade" not in out


def test_choosing_gemini_saves_the_key_and_switches_through_meeting_capture(fake_mc, monkeypatch, env):
    term = Answers(monkeypatch, inputs=["2"], keys=["AIza-new"])
    cmd, out, todo = _choose(fake_mc)
    assert cmd == [str(fake_mc.path), "stt", "gemini"] and todo == []
    assert ct.KEY_FILE.read_text() == "AIza-new" and oct(ct.KEY_FILE.stat().st_mode & 0o777) == "0o600"
    assert ["open", ct.AI_STUDIO] in env and term.key_prompts == 1


def test_choosing_gemini_then_skipping_the_key_stays_on_device(fake_mc, monkeypatch, env):
    Answers(monkeypatch, inputs=["2"], keys=[""])
    cmd, out, todo = _choose(fake_mc)
    assert cmd is None and todo == [] and not ct.KEY_FILE.exists()
    assert "stays on this Mac" in out


def test_gemini_chosen_before_is_the_default_and_on_device_uses_meeting_captures_hint(fake_mc, monkeypatch, env):
    fake_mc.set(json=mc_json(choice="gemini", engine="gemini", uploads=True, gemini_key=True,
                             on_device_hint="meeting-capture stt auto"))
    _key_file()
    term = Answers(monkeypatch, inputs=[""])
    cmd, out, todo = _choose(fake_mc)                         # Enter keeps Gemini
    assert cmd is None and todo == [] and "3. Gemini — meeting audio is uploaded to Google  (default)" in out
    assert term.key_prompts == 0
    Answers(monkeypatch, inputs=["2"])
    cmd, out, todo = _choose(fake_mc)                         # back to this Mac, Gemini as backup
    assert cmd == [str(fake_mc.path), "stt", "auto"]
    Answers(monkeypatch, inputs=["1"])
    cmd, out, todo = _choose(fake_mc)                         # this Mac only
    assert cmd == [str(fake_mc.path), "stt", "apple"]


def test_a_missing_model_is_set_up_with_meeting_captures_install_hint(fake_mc, monkeypatch, env):
    fake_mc.set(json=mc_json(**NEEDS_MODEL),
                lines={"language en-US": ["Downloading the on-device speech model for en-US from Apple (one time)…",
                                          "  en-US model download 0%", "  en-US model download 50%",
                                          "transcription: Automatic (stt=auto), language en-US — now On this "
                                          "Mac — nothing is uploaded; daemon restarted"]},
                after={"language en-US": mc_json(locale_source="setting")})
    monkeypatch.setattr(ct, "_interactive", lambda: False)
    cmd, out, todo = _choose(fake_mc)
    assert cmd == [str(fake_mc.path), "language", "en-US"] and todo == []
    log, todo = [], []
    ct._apply_stt(cmd, log.append, todo)
    assert todo == [] and log[0] == "  $ meeting-capture language en-US"
    assert "      en-US model download 50%" in log                # meeting-capture's lines, as they come
    out, todo, done = _report(fake_mc)
    assert done == ["Transcription: on this Mac (en-US)"] and "never leaves this Mac" in out


def test_a_change_that_hangs_is_stopped_and_says_it_timed_out(fake_mc, monkeypatch, env):
    fake_mc.set(lines={"language hi-IN": ["Downloading the on-device speech model for hi-IN…"]},
                cmd_sleep={"language hi-IN": 30}, spawn=True)
    monkeypatch.setattr(ct, "APPLY_TIMEOUT_S", 1)
    log, todo = [], []
    ct._apply_stt([str(fake_mc.path), "language", "hi-IN"], log.append, todo)
    assert any("timed out after" in l and "meeting-capture stt" in l for l in log)
    assert todo == ["Set up transcription (it timed out): meeting-capture language hi-IN"]
    pid = int((fake_mc.root / "grandchild.pid").read_text())
    import os
    import time
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        pytest.fail("the download it started outlived the timeout")


def test_a_failed_change_is_a_todo(fake_mc, env):
    fake_mc.set(lines={"language hi-IN": ["can't use that language: installing failed"]},
                rc={"language hi-IN": 1})
    log, todo = [], []
    ct._apply_stt([str(fake_mc.path), "language", "hi-IN"], log.append, todo)
    assert "    can't use that language: installing failed" in log
    assert todo == ["Set up transcription: meeting-capture language hi-IN"]


# ------------------------------------------------------------ the Dutch Mac (the drift this fixes)

def test_dutch_mac_with_a_key_keeps_gemini_and_says_audio_is_uploaded(fake_mc, monkeypatch, env):
    """Before: pipeline-monitor assumed en-US, installed it and said "audio
    never leaves this Mac" while meeting-capture's auto picked Gemini."""
    fake_mc.set(json=mc_json(**DUTCH))
    _key_file()
    for interactive in (False, True):
        monkeypatch.setattr(ct, "_interactive", lambda: interactive)
        Answers(monkeypatch, inputs=[""])                     # Enter: Gemini is the default here
        cmd, out, todo = _choose(fake_mc)
        assert cmd is None and todo == []                     # auto already uses Gemini: nothing to change
        assert "nl-NL" in out and "Gemini detects the language itself" in out
        out, todo, done = _report(fake_mc)
        assert "never leaves" not in out and "on this Mac (" not in out
        assert "Meeting audio is uploaded to Google Gemini for transcription" in out
        assert done == ["Transcription: Gemini"]
    assert fake_mc.changes() == []


def test_dutch_mac_choosing_on_device_with_gemini_as_backup_runs_the_hint(fake_mc, monkeypatch, env):
    """Gemini stays the backup (auto + a key): the report says audio stays on
    this Mac unless on-device stops working — never "never leaves"."""
    fake_mc.set(json=mc_json(**DUTCH), after={"language en-US": mc_json(locale_source="setting",
                                                                        gemini_key=True, gemini_fallback=True)})
    _key_file()
    Answers(monkeypatch, inputs=["2"])
    cmd, out, todo = _choose(fake_mc)
    assert cmd == [str(fake_mc.path), "language", "en-US"]
    ct._apply_stt(cmd, lambda _: None, [])
    out, todo, done = _report(fake_mc)
    assert "never leaves" not in out
    assert ("On this Mac — but if on-device transcription stops working, Gemini takes over (uploaded); "
            "transcripts and the search index stay on this Mac.") in out
    assert "(`meeting-capture stt apple` never uploads.)" in out
    assert done == ["Transcription: on this Mac (en-US)"]


def test_on_this_mac_only_with_a_key_runs_stt_apple_and_never_uploads(fake_mc, monkeypatch, env):
    """The review's case: English Mac, key in the file, stt unset. "On this
    Mac only" must drop auto's Gemini fallback (`stt apple`), and only then
    does the report say "never leaves this Mac"."""
    fake_mc.set(json=mc_json(choice="auto", gemini_key=True, gemini_fallback=True),
                after={"stt apple": mc_json(choice="apple", gemini_key=True, on_device_only_hint=None)})
    _key_file()
    for interactive, answer, want in ((True, "", None), (False, "", None), (True, "1", "apple")):
        monkeypatch.setattr(ct, "_interactive", lambda: interactive)
        Answers(monkeypatch, inputs=[answer])
        cmd, out, todo = _choose(fake_mc)
        if want is None:                          # the default keeps what is set (auto)…
            assert cmd is None
            out, _, _ = _report(fake_mc)          # …and says so honestly
            assert "never leaves" not in out and "Gemini takes over (uploaded)" in out
            continue
        assert "1. On this Mac only (en-US) — never uploads" in out
        assert "2. On this Mac (en-US), Gemini as backup" in out and "(default)" in out.split("2. On")[1]
        assert cmd == [str(fake_mc.path), "stt", "apple"]
        ct._apply_stt(cmd, lambda _: None, [])
        out, todo, done = _report(fake_mc)
        assert "Meeting audio never leaves this Mac; transcripts and the search index stay here too." in out
        assert "takes over" not in out and "never uploads.)" not in out


# ------------------------------------------------------------ live mode

def test_live_mode_is_said_before_the_choice_and_in_the_report(fake_mc, monkeypatch, env):
    fake_mc.set(json=mc_json(**LIVE_ON))
    _key_file()
    Answers(monkeypatch, inputs=[""])
    cmd, out, todo = _choose(fake_mc)
    assert out.index("live mode is on") < out.index("1. On this Mac")
    out, todo, done = _report(fake_mc)
    assert "never leaves" not in out
    assert "Every call streams to Google Gemini as it happens (live mode)" in out and ct.LIVE_KEEP_LOCAL in out
    assert done == ["Transcription: on this Mac (en-US) · live: calls stream to Gemini"]


def test_live_mode_that_runs_batch_is_explained(fake_mc, env):
    fake_mc.set(json=mc_json(live={"requested": True, "active": False,
                                   "blocker": "transcription is set to on this Mac only (stt apple), "
                                              "which never uploads"}))
    out, todo, done = _report(fake_mc)
    assert "Meeting audio never leaves this Mac" in out
    assert "Live mode is requested, but the recorder records in batch: transcription is set to on this Mac" in out


# ------------------------------------------------------------ on-device unavailable / unknown

def test_unavailable_interactive_key_entered(fake_mc, monkeypatch, env):
    fake_mc.set(json=mc_json(**UNAVAILABLE))
    term = Answers(monkeypatch, keys=["AIza-new"])
    cmd, out, todo = _choose(fake_mc)
    assert cmd is None and todo == [] and term.key_prompts == 1 and term.prompts == []
    assert "needs macOS 26" in out and ct.KEY_FILE.read_text() == "AIza-new"


def test_unavailable_non_interactive_without_a_key_is_a_todo(fake_mc, monkeypatch, env):
    fake_mc.set(json=mc_json(**UNAVAILABLE))
    monkeypatch.setattr(ct, "_interactive", lambda: False)
    cmd, out, todo = _choose(fake_mc)
    assert cmd is None and len(todo) == 1 and todo[0].startswith("Add a Gemini key")
    out, todo, done = _report(fake_mc)
    assert "✗ transcription: unavailable" in out and "Nothing transcribes yet" in out
    assert done == [] and todo[0].startswith("Transcription can't run yet")


def test_on_device_only_setting_never_asks_for_a_key(fake_mc, monkeypatch, env):
    fake_mc.set(json=mc_json(**dict(UNAVAILABLE, choice="apple", engine="apple")))
    term = Answers(monkeypatch)
    cmd, out, todo = _choose(fake_mc)
    assert cmd is None and term.key_prompts == 0 and "Google" not in out
    assert todo and "meeting-capture stt auto" in todo[0]


def test_an_old_meeting_capture_needs_the_key_and_is_never_called_on_device(fake_mc, monkeypatch, env):
    fake_mc.set(mode="old")
    monkeypatch.setattr(ct, "_interactive", lambda: False)
    cmd, out, todo = _choose(fake_mc)
    assert cmd is None and "meeting-capture < 0.7" in out and any("Add a Gemini key" in t for t in todo)
    out, todo, done = _report(fake_mc)
    assert done == [] and todo == []                                  # no key: step 1's to-do stands
    _key_file()
    out, todo, done = _report(fake_mc)
    assert "on this Mac (" not in out and "uploaded to Google Gemini" in out
    assert "brew upgrade meeting-capture" in out
    assert done == ["Transcription: Gemini (meeting-capture < 0.7 — upgrade for on-device)"]


@pytest.mark.parametrize("mode", ["garbage", "fail"])
def test_an_unreadable_answer_claims_nothing(fake_mc, monkeypatch, env, mode):
    fake_mc.set(mode=mode)
    term = Answers(monkeypatch)
    cmd, out, todo = _choose(fake_mc)
    assert cmd is None and term.prompts == [] and "couldn't ask meeting-capture" in out
    out, todo, done = _report(fake_mc)
    assert "never leaves" not in out and "uploaded" not in out and done == []
    assert todo == ["Check how meetings are transcribed: meeting-capture stt"]


# ------------------------------------------------------------ a key only the shell can see
# The recorder is a launchd agent: it never sees GEMINI_API_KEY / GOOGLE_API_KEY
# exported in ~/.zshrc, and `meeting-capture install` rewrites its plist env with
# only PATH and MEETING_CAPTURE_*. After setup the key file is all it can read.

def test_shell_only_key_choose_gemini_saves_it_where_the_recorder_reads_it(fake_mc, monkeypatch, env):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-in-zshrc")
    term = Answers(monkeypatch, inputs=["2", ""])                     # Gemini; Enter = save it
    cmd, out, todo = _choose(fake_mc)
    assert "GEMINI_API_KEY in your shell" in out and "launchd" in out
    assert any("Save that key" in p for p in term.prompts)
    assert ct.KEY_FILE.read_text() == "AIza-in-zshrc" and oct(ct.KEY_FILE.stat().st_mode & 0o777) == "0o600"
    assert term.key_prompts == 0 and ["open", ct.AI_STUDIO] not in env
    assert cmd == [str(fake_mc.path), "stt", "gemini"] and todo == []


def test_shell_only_key_not_saved_stays_on_device(fake_mc, monkeypatch, env):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-in-zshrc")
    term = Answers(monkeypatch, inputs=["2", "n"])
    cmd, out, todo = _choose(fake_mc)
    assert cmd is None and todo == [] and not ct.KEY_FILE.exists() and "stays on this Mac" in out
    assert term.key_prompts == 0 and ["open", ct.AI_STUDIO] not in env


def test_shell_only_key_non_interactive_gemini_default_is_a_todo(fake_mc, monkeypatch, env):
    fake_mc.set(json=mc_json(choice="gemini", engine="gemini", uploads=True,
                             on_device_hint="meeting-capture stt auto"))
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-in-zshrc")
    monkeypatch.setattr(ct, "_interactive", lambda: False)
    term = Answers(monkeypatch)
    cmd, out, todo = _choose(fake_mc)
    assert cmd is None and term.prompts == [] and not ct.KEY_FILE.exists()
    assert len(todo) == 1 and "GEMINI_API_KEY in your shell" in todo[0] and str(ct.KEY_FILE) in todo[0]
    assert "meeting-capture stt auto" in todo[0]


def test_key_in_the_plist_env_is_moved_to_the_key_file(fake_mc, monkeypatch, env):
    # `meeting-capture install` (run by setup) would drop it from the plist.
    write_plist(ct.LAUNCH_AGENTS / "com.contorch.meeting-capture.plist",
                {"MEETING_CAPTURE_STT": "gemini", "GOOGLE_API_KEY": "AIza-in-plist"})
    fake_mc.set(json=mc_json(choice="gemini", engine="gemini", uploads=True, gemini_key=True))
    term = Answers(monkeypatch, inputs=["", ""])                      # keep Gemini; save it
    cmd, out, todo = _choose(fake_mc)
    assert "The recorder can't use GOOGLE_API_KEY in its launchd plist" in out
    assert ct.KEY_FILE.read_text() == "AIza-in-plist" and term.key_prompts == 0
    assert cmd is None and todo == []


def test_unavailable_shell_only_key_non_interactive_is_a_todo(fake_mc, monkeypatch, env):
    fake_mc.set(json=mc_json(**UNAVAILABLE))
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-in-zshrc")
    monkeypatch.setattr(ct, "_interactive", lambda: False)
    term = Answers(monkeypatch)
    cmd, out, todo = _choose(fake_mc)
    assert cmd is None and term.prompts == [] and not ct.KEY_FILE.exists()
    assert len(todo) == 1 and todo[0].startswith("Add a Gemini key") and "GOOGLE_API_KEY in your shell" in todo[0]


# ------------------------------------------------------------ whole setup

@pytest.fixture
def stack(tmp_path, monkeypatch, env, fake_mc):
    bins = {"meeting-capture": str(fake_mc.path), **{n: f"/x/{n}" for n in (
        "context-orchestrator-chroma", "transcript-watcher", "contorch-mcp", "claude")}}
    monkeypatch.setattr(ct.shutil, "which", lambda n: bins.get(n))     # no brew
    monkeypatch.setattr(ct, "CLAUDE_JSON", tmp_path / "claude.json")
    monkeypatch.setattr(ct, "CLAUDE_MD", tmp_path / "CLAUDE.md")
    monkeypatch.setattr(ct, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(ct, "STOPPED_MARKER", tmp_path / "state" / "stopped.json")
    monkeypatch.setattr(ct, "CHROMA_DIR", tmp_path / "chroma")
    monkeypatch.setattr(ct, "_launchctl", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(ct, "_chroma_up", lambda timeout_s: True)
    monkeypatch.setattr(ct, "_contorch_memory_bin", lambda: None)
    return env


def test_setup_succeeds_without_a_key_when_this_mac_transcribes(fake_mc, monkeypatch, stack):
    term = Answers(monkeypatch)                                      # Enter at every prompt
    out: list[str] = []
    assert ct.setup(log=out.append) is True
    text = "\n".join(out)
    assert term.key_prompts == 0 and not ct.KEY_FILE.exists()
    assert "Google Gemini for transcription" not in text and "✗" not in text
    assert "✓ Transcription: on this Mac (en-US)" in text
    assert [str(fake_mc.path), "install"] in stack
    assert fake_mc.changes() == []                                   # the default needs no change


def test_setup_applies_gemini_after_installing_the_recorder_then_rereads(fake_mc, monkeypatch, stack):
    fake_mc.set(after={"stt gemini": mc_json(choice="gemini", engine="gemini", uploads=True, gemini_key=True)})
    Answers(monkeypatch, inputs=["2"], keys=["AIza-new"])
    out: list[str] = []
    assert ct.setup(log=out.append) is True
    text = "\n".join(out)
    assert fake_mc.changes() == [["stt", "gemini"]]
    assert text.index("✓ capture daemon running") < text.index("$ meeting-capture stt gemini")
    assert "Meeting audio is uploaded to Google Gemini" in text and "✓ Transcription: Gemini" in text


def test_setup_on_a_dutch_mac_never_promises_on_device(fake_mc, monkeypatch, stack):
    fake_mc.set(json=mc_json(**DUTCH))
    _key_file()
    Answers(monkeypatch)
    out: list[str] = []
    assert ct.setup(log=out.append) is True
    text = "\n".join(out)
    assert "never leaves" not in text and "✓ Transcription: Gemini" in text
    assert fake_mc.changes() == []


def test_setup_in_live_mode_reports_the_stream_in_the_summary(fake_mc, monkeypatch, stack):
    fake_mc.set(json=mc_json(**LIVE_ON))
    _key_file()
    Answers(monkeypatch)
    out: list[str] = []
    assert ct.setup(log=out.append) is True
    text = "\n".join(out)
    assert "never leaves this Mac" not in text
    assert "✓ Transcription: on this Mac (en-US) · live: calls stream to Gemini" in text


def test_setup_without_on_device_and_without_a_key_reports_the_todo(fake_mc, monkeypatch, stack):
    fake_mc.set(json=mc_json(**UNAVAILABLE))
    Answers(monkeypatch, keys=[""])
    out: list[str] = []
    assert ct.setup(log=out.append) is False
    assert any("✗ Add a Gemini key" in line for line in out)


# ------------------------------------------------------------ status / doctor

@pytest.fixture
def agent(tmp_path, monkeypatch, fake_mc):
    agents = tmp_path / "LaunchAgents"
    agents.mkdir(exist_ok=True)
    (agents / "com.contorch.meeting-capture.plist").write_text("")
    monkeypatch.setattr(ct, "LAUNCH_AGENTS", agents)
    monkeypatch.setattr(ct, "STOPPED_MARKER", tmp_path / "stopped.json")
    monkeypatch.setattr(ct, "_launchctl", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(ct.shutil, "which", lambda n: None)
    write_plist(stt.PLIST, {})
    return fake_mc


def _doctor(capsys):
    """`contorch doctor`'s transcription section and its verdict (the rest of
    doctor runs other components' doctors, absent here)."""
    ct.doctor()
    out = capsys.readouterr().out.split("── transcription")[1].split("\n✗ meeting-capture not on PATH")[0]
    rc = ct._print_transcription_detail()
    capsys.readouterr()
    return rc, out


def test_status_and_doctor_show_meeting_captures_answer(agent, capsys):
    agent.set(json=mc_json(locale="en-GB", locale_source="setting", locale_why="chosen with `meeting-capture "
                           "language`", apple={"installed_locales": ["en-GB", "en-US"]}))
    assert ct.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "on this Mac (en-GB)  [setting: auto, locale en-GB]" in out
    rc, out = _doctor(capsys)
    assert rc == 0 and "✓ on this Mac (en-GB)" in out and "installed speech models: en-GB, en-US" in out
    assert "audio: meeting audio never leaves this Mac" in out
    assert agent.reads() == 1                                        # status and doctor share the answer


def test_status_and_doctor_hedge_auto_with_a_key(agent, capsys):
    """The review's blocker in status/doctor: auto + a key + on-device ready."""
    agent.set(json=mc_json(gemini_key=True, gemini_fallback=True))
    assert ct.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "never leaves" not in out and "Gemini takes over (uploaded)" in out
    assert "`meeting-capture stt apple` never uploads" in out
    rc, out = _doctor(capsys)
    assert rc == 0 and "never leaves" not in out
    assert ("audio: on this Mac — but if on-device transcription stops working, Gemini takes over "
            "(uploaded) (`meeting-capture stt apple` never uploads)") in out


def test_doctor_on_a_dutch_mac_says_uploaded(agent, capsys):
    agent.set(json=mc_json(**DUTCH))
    rc, out = _doctor(capsys)
    assert rc == 0 and "✓ Gemini" in out and "audio: meeting audio is uploaded to Google Gemini" in out
    assert "nl-NL" in out


def test_doctor_live_mode(agent, capsys):
    agent.set(json=mc_json(**LIVE_ON))
    rc, out = _doctor(capsys)
    assert "✓ on this Mac (en-US) · live: calls stream to Gemini" in out
    assert "live mode: on — every call streams to Gemini" in out
    stt.clear_cache()
    agent.set(json=mc_json(live={"requested": True, "active": False,
                                 "blocker": "transcription is set to on this Mac only (stt apple), "
                                            "which never uploads"}))
    rc, out = _doctor(capsys)
    assert "✓ on this Mac (en-US)\n" in out
    assert "live mode: requested, but transcription is set to on this Mac only" in out


def test_doctor_fails_when_nothing_transcribes_and_names_the_fix(agent, capsys):
    agent.set(json=mc_json(**NEEDS_MODEL))
    rc, out = _doctor(capsys)
    assert rc == 1 and "✗ unavailable" in out and "set up the speech model: meeting-capture language en-US" in out


def test_doctor_with_an_old_or_unreadable_meeting_capture(agent, capsys):
    agent.set(mode="old")
    rc, out = _doctor(capsys)
    assert rc == 0 and "✓ Gemini (meeting-capture < 0.7 — upgrade for on-device)" in out
    assert "on this Mac (" not in out
    stt.clear_cache()
    agent.set(mode="garbage")
    rc, out = _doctor(capsys)
    assert rc == 1 and "✗ unknown — " in out and "audio:" not in out
