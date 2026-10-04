"""Shared fixtures. Every test is isolated from this Mac's real install:
HOME is a scratch directory (so ~/.contorch, ~/.meeting-capture, ~/.claude
are never the real ones), owners.locate() finds nothing but what a test puts
in its scratch Homebrew prefix, and no Contorch channel variables leak in.
Transcription is asked from a fake `meeting-capture` executable that prints
canned `stt --json` answers (FakeMeetingCapture)."""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline_monitor import mcconfig, owners, ownerstate
from pipeline_monitor import transcription as stt


# Never run these for real from a test: they change this Mac's launchd jobs,
# Homebrew, Claude Code or privacy settings. Tests replace the helper that
# would call them (contorch._launchctl, a fake runner) or put a fake on PATH
# inside their scratch directory.
FORBIDDEN = {"launchctl", "brew", "tccutil", "claude", "osascript", "open", "pbcopy"}
_RealPopen = subprocess.Popen


class _GuardedPopen(_RealPopen):
    def __init__(self, args, *a, **kw):
        argv0 = args if isinstance(args, str) else (args[0] if args else "")
        name = os.path.basename(str(argv0).split(" ")[0])
        if name in FORBIDDEN and not str(argv0).startswith(os.environ.get("PM_TEST_TMP", "\0")):
            raise AssertionError(f"a test tried to run the real {name}: {args!r}")
        super().__init__(args, *a, **kw)


@pytest.fixture(autouse=True)
def _no_real_system_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_TEST_TMP", str(tmp_path))
    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for v in ("CONTORCH_CHANNEL", "CONTORCH_OP", "HOMEBREW_PREFIX"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(owners, "BREW_PREFIXES", (str(tmp_path / "brew"),))
    monkeypatch.setattr(owners, "_which", lambda name: None)
    monkeypatch.setattr(owners, "SELF_BIN", tmp_path / "self-bin")
    monkeypatch.setattr(owners, "USER_VENVS", {k: tmp_path / "venvs" / k for k in owners.USER_VENVS})
    monkeypatch.setattr(mcconfig, "LEGACY_PLIST", tmp_path / "no-agent.plist")
    mcconfig.clear_cache()
    ownerstate.clear()
    # contorch's own launchd view: a scratch LaunchAgents and a launchctl
    # that knows no job (tests that need one replace it)
    from pipeline_monitor import contorch as ct
    monkeypatch.setattr(ct, "LAUNCH_AGENTS", tmp_path / "LaunchAgents")
    monkeypatch.setattr(ct, "STATE_DIR", home / ".contorch")
    monkeypatch.setattr(ct, "STOPPED_MARKER", home / ".contorch" / "stopped.json")
    monkeypatch.setattr(ct, "_launchctl", lambda *a: subprocess.CompletedProcess(a, 113, "", "not found"))
    monkeypatch.setattr(stt, "PLIST", tmp_path / "no-agent.plist")
    monkeypatch.setattr(stt, "KEY_FILE", tmp_path / "no-key")
    monkeypatch.setattr(stt, "MC_VENV", tmp_path / "no-venv" / "meeting-capture")
    monkeypatch.setattr(stt, "MC_STAMP", tmp_path / "no-venv" / ".formula-version")
    # Setup reads keys from this shell, so none of the developer's may leak in.
    for v in [k for k in os.environ if k.startswith("MEETING_CAPTURE_")] + [
            "GOOGLE_API_KEY", "GEMINI_API_KEY", "CONTORCH_NONINTERACTIVE"]:
        monkeypatch.delenv(v, raising=False)
    stt.clear_cache()
    yield
    stt.clear_cache()
    mcconfig.clear_cache()
    ownerstate.clear()


def on_brew(tmp_path: Path, name: str, target: Path) -> Path:
    """Put `target` where owners.locate() finds `name` first: the scratch
    Homebrew prefix's opt/<formula>/bin (a symlink, so the target's own
    directory stays its working directory)."""
    link = tmp_path / "brew" / "opt" / owners.FORMULA.get(name, name) / "bin" / name
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    return link


def write_plist(path: Path, env: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({"Label": "com.contorch.meeting-capture",
                                     "ProgramArguments": ["/x/python", "-m", "meeting_capture.daemon"],
                                     "EnvironmentVariables": env}))
    return path


# ------------------------------------------------------------ canned answers

def mc_json(**over) -> dict:
    """A schema-1 `meeting-capture stt --json` answer: by default this Mac
    transcribes on-device in en-US, nothing uploads, no key, batch mode."""
    apple = {"available": True, "usable": True, "installable": False, "installed": True,
             "reason": "on-device model for en-US is installed", "locale": "en-US", "exit_code": 0,
             "supported": ["en-GB", "en-US", "hi-IN"], "installed_locales": ["en-US"],
             "os": "26.0", "arch": "arm64", "helper": "/opt/homebrew/opt/meeting-capture/bin/sysaudio"}
    apple.update(over.pop("apple", {}))
    live = {"requested": False, "active": False, "blocker": None}
    live.update(over.pop("live", {}))
    d = {"schema": 1, "version": "0.7.0", "agent_installed": True,
         "choice": "auto", "choice_label": "Automatic", "engine": "apple", "engine_label": "On this Mac",
         "ready": True, "reason": "on-device model for en-US is installed",
         "locale": "en-US", "locale_source": "mac", "locale_why": "this Mac's language",
         "locale_guessed": False, "mac_language": "en-US",
         "uploads": False, "gemini_fallback": False, "gemini_key": False, "notice": None,
         "needs_model": False, "install_hint": None, "on_device_hint": None,
         "on_device_only_hint": "meeting-capture stt apple",
         "apple": apple, "live": live}
    d.update(over)
    if "may_upload" not in over:        # what meeting-capture computes (its README "Contract")
        d["may_upload"] = bool(d["uploads"] or d["live"]["active"] or d["gemini_fallback"])
    return d


# The review's case: a Dutch Mac, a Gemini key, nobody picked an engine. Dutch
# isn't an on-device language, so meeting-capture's auto uses Gemini.
DUTCH = dict(engine="gemini", engine_label="Gemini", uploads=True, gemini_key=True,
             locale="en-US", locale_source="default", locale_guessed=True, mac_language="nl-NL",
             locale_why="default — this Mac's language (nl-NL) can't be transcribed on this Mac",
             reason="this Mac's language (nl-NL) can't be transcribed on this Mac, so Gemini (it detects "
                    "the language) transcribes; `meeting-capture language LOCALE` picks an on-device language",
             on_device_hint="meeting-capture language en-US")
LIVE_ON = dict(gemini_key=True, gemini_fallback=True, live={"requested": True, "active": True})
NEEDS_MODEL = dict(engine="none", engine_label="None", ready=False, needs_model=True,
                   reason="on this Mac isn't available (the on-device model for en-US isn't installed yet) "
                          "and no Gemini API key is set",
                   install_hint="meeting-capture language en-US", on_device_hint="meeting-capture language en-US",
                   apple={"usable": False, "installable": True, "installed": False, "exit_code": 75,
                          "installed_locales": [],
                          "reason": "the on-device model for en-US isn't installed yet"})
UNAVAILABLE = dict(engine="none", engine_label="None", ready=False,
                   reason="on this Mac isn't available (needs macOS 26 or later) and no Gemini API key is set",
                   apple={"available": False, "usable": False, "installable": False, "installed": False,
                          "exit_code": 69, "supported": [], "installed_locales": [],
                          "reason": "on-device transcription needs macOS 26 or later on Apple silicon"})


FAKE_MC = r'''#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.realpath(__file__))
state_path = os.path.join(here, "mc-state.json")
state = json.load(open(state_path))
args = sys.argv[1:]
with open(os.path.join(here, "mc-calls.jsonl"), "a") as f:
    f.write(json.dumps(args) + "\n")
with open(os.path.join(here, "mc-env.json"), "w") as f:
    json.dump({"MEETING_CAPTURE_SYSAUDIO": os.environ.get("MEETING_CAPTURE_SYSAUDIO")}, f)
if state.get("spawn"):           # a grandchild (pip, the helper's download) that outlives a plain kill
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(os.path.join(here, "grandchild.pid"), "w").write(str(child.pid))
if args == ["stt", "--json"]:
    mode = state.get("mode", "ok")
    time.sleep(state.get("sleep", 0))
    if mode == "old":            # meeting-capture <= 0.6: argparse has no `stt`
        sys.stderr.write("usage: meeting-capture [-h] {status,...}\n"
                         "meeting-capture: error: argument cmd: invalid choice: 'stt'\n")
        sys.exit(2)
    if mode == "garbage":
        print("meeting-capture: setting up environment...\nnot json")
        sys.exit(0)
    if mode == "fail":
        sys.stderr.write("Traceback: boom\n")
        sys.exit(1)
    print(json.dumps(state["json"]))
    sys.exit(0)
key = " ".join(args)
for line in state.get("lines", {}).get(key, []):
    print(line, flush=True)
time.sleep(state.get("cmd_sleep", {}).get(key, 0))
if key in state.get("after", {}):
    state["json"] = state["after"][key]
    json.dump(state, open(state_path, "w"))
sys.exit(state.get("rc", {}).get(key, 0))
'''


class FakeMeetingCapture:
    """A `meeting-capture` executable: `stt --json` prints state["json"] (or
    acts old / garbage / failing / slow); any other command prints
    state["lines"][cmd], switches the answer to state["after"][cmd] and exits
    state["rc"][cmd] (default 0). Every call is logged."""

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "meeting-capture"
        self.state_path = root / "mc-state.json"
        self.calls_path = root / "mc-calls.jsonl"

    def set(self, **state) -> "FakeMeetingCapture":
        cur = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        cur.update(state)
        self.state_path.write_text(json.dumps(cur))
        return self

    def calls(self) -> list[list[str]]:
        if not self.calls_path.exists():
            return []
        return [json.loads(l) for l in self.calls_path.read_text().splitlines() if l.strip()]

    def reads(self) -> int:
        return sum(c == ["stt", "--json"] for c in self.calls())

    def changes(self) -> list[list[str]]:
        return [c for c in self.calls() if c != ["stt", "--json"]]


@pytest.fixture
def fake_mc(tmp_path, monkeypatch):
    """A fake meeting-capture found where brew puts it (the scratch prefix's
    opt/meeting-capture/bin), answering mc_json()."""
    root = tmp_path / "brew" / "opt" / "meeting-capture" / "bin"
    root.mkdir(parents=True)
    mc = FakeMeetingCapture(root)
    mc.path.write_text(FAKE_MC.replace("{python}", sys.executable))
    mc.path.chmod(0o755)
    mc.set(json=mc_json())
    return mc


# ------------------------------------------------------------ fake owners (adopt / uninstall / setup)

FAKE_OWNER = r'''#!{python}
"""A fake owner CLI. answers[key] = JSON document (or list: JSON Lines),
rc[key] = exit code, old = keys that are a usage error (exit 2), guarded =
keys that test the channel marker like meeting-capture's reader (exit 3)."""
import json, os, sys
here = os.path.dirname(os.path.realpath(__file__))
name = os.path.basename(__file__)
state = json.load(open(os.path.join(here, name + ".json")))
args = sys.argv[1:]
key = " ".join(a for a in args if a != "--json")
with open(os.environ.get("FAKE_OWNER_LOG") or os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"exe": os.path.abspath(__file__), "name": name, "args": args,
                        "channel": os.environ.get("CONTORCH_CHANNEL"), "op": os.environ.get("CONTORCH_OP")}) + "\n")
def match(table):
    for k in sorted(table, key=len, reverse=True):
        if key == k or key.startswith(k + " "):
            return table[k]
    return None
if match({k: 1 for k in state.get("old", [])}):
    sys.stderr.write("usage: %s: error: unrecognized arguments\n" % name); sys.exit(2)
if match({k: 1 for k in state.get("guarded", [])}):
    marker = os.path.join(os.environ["HOME"], ".contorch", "channel.json")
    if os.path.exists(marker):
        m = json.load(open(marker))
        me = os.environ.get("CONTORCH_CHANNEL") or "dev"
        me = me if me in ("app", "brew", "dev") else "dev"
        op = m.get("op")
        ok = me in (m.get("writers") or []) and (op is None or os.environ.get("CONTORCH_OP") == op.get("id"))
        if not ok:
            print(json.dumps({"schema": "x/1", "ok": False, "error": {"code": "channel_conflict",
                              "message": m.get("blocked_message")}}))
            sys.exit(3)
ans = match(state.get("answers", {}))
for doc in (ans if isinstance(ans, list) else [ans] if ans is not None else []):
    print(json.dumps(doc))
sys.exit(match(state.get("rc", {})) or 0)
'''


class FakeOwner:
    """One fake executable (meeting-capture, contorch-memory, contorch …) in
    `bindir`, configured through <name>.json; every call is logged (argv,
    $CONTORCH_CHANNEL, $CONTORCH_OP) to bindir/calls.jsonl."""

    def __init__(self, bindir: Path, name: str, **state):
        bindir.mkdir(parents=True, exist_ok=True)
        self.bindir, self.name = bindir, name
        self.path = bindir / name
        self.path.write_text(FAKE_OWNER.replace("{python}", sys.executable))
        self.path.chmod(0o755)
        self.state = {"answers": {}, "rc": {}, "old": [], "guarded": []}
        self.set(**state)

    def set(self, **state) -> "FakeOwner":
        self.state.update(state)
        (self.bindir / f"{self.name}.json").write_text(json.dumps(self.state))
        return self

    def answer(self, key: str, doc, rc: int = 0) -> "FakeOwner":
        self.state["answers"][key] = doc
        if rc:
            self.state["rc"][key] = rc
        return self.set()

    def calls(self) -> list[dict]:
        f = self.bindir / "calls.jsonl"
        if not f.exists():
            return []
        return [c for c in (json.loads(l) for l in f.read_text().splitlines() if l.strip())
                if c["name"] == self.name]


def mc_agent(action: str, ok: bool = True, **extra) -> dict:
    return {"schema": "meeting-capture.agent/1", "ok": ok, "action": action, "backend": "launchctl",
            "label": "com.contorch.meeting-capture", "performed": ok, **extra}


def cm_claude(action: str, ok: bool = True, **extra) -> dict:
    part = {"present": action != "uninstall", "matches": action != "uninstall"}
    return {"schema": "contorch-memory.claude/1", "ok": ok, "action": action, "channel": "app",
            "mcp": dict(part), "hook": dict(part, legacy_copy="none"), "claude_md": dict(part),
            "skill": dict(part), "blocked_by_managed_settings": False, "todo": [], "changed": [], **extra}
