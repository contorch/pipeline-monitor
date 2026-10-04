"""`contorch` — one command for the whole contorch stack.

    contorch setup      configure everything after `brew install contorch/tap/contorch`
    contorch doctor     health-check every component (incl. the transcription engine)
    contorch status     what is installed, running, stopped; how meetings are transcribed
    contorch stop       stop every background daemon, and keep them stopped across login
    contorch resume     start them again, in dependency order, and check they came up
    contorch channel    which install (app / brew / dev) owns Contorch on this Mac
    contorch modules    memory, recorder, line-in, terminal commands: present / wanted / on
    contorch cli        put Contorch.app's commands on your PATH (app only)
    contorch adopt      make this install own Contorch here (backs up your data first)
    contorch rollback   Contorch.app: go back to the Homebrew install it adopted
    contorch uninstall  remove Contorch (your meetings and memory are kept)

The daemons are per-user launchd agents written by each component's own
installer (meeting-capture, context-orchestrator's chroma server and
transcript watcher). `stop` boots each one out *and* disables it in launchd,
so a stopped stack stays stopped after a reboot instead of silently coming
back at login; `resume` re-enables and bootstraps them. The menu-bar app is
not stopped — it is where you resume from.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import owners
from . import transcription as stt

LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
STATE_DIR = Path.home() / ".contorch"
STOPPED_MARKER = STATE_DIR / "stopped.json"

# Stop order: the thing producing data first, the index last. Resume reverses it.
COMPONENTS = [
    ("meeting-capture", "meeting capture"),
    ("transcript-watcher", "transcript indexer"),
    ("context-orchestrator-chroma", "search index (chroma)"),
]
ORGS = ("contorch", "stirredo")  # stirredo = pre-rebrand labels still on older installs


def _uid() -> int:
    return os.getuid()


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def agents() -> list[dict]:
    """Installed agents, in stop order. One entry per component whose plist exists."""
    out = []
    for suffix, desc in COMPONENTS:
        for org in ORGS:
            label = f"com.{org}.{suffix}"
            plist = LAUNCH_AGENTS / f"{label}.plist"
            if plist.is_file():
                out.append({"component": suffix, "desc": desc, "label": label, "plist": plist})
                break
    return out


def _pid(label: str) -> int | None:
    res = _launchctl("list", label)
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith('"PID"'):
            try:
                return int(line.split("=")[1].strip(" ;"))
            except ValueError:
                return None
    return None


def _disabled_labels() -> set[str]:
    res = _launchctl("print-disabled", f"gui/{_uid()}")
    out = set()
    for line in res.stdout.splitlines():
        # "com.contorch.meeting-capture" => disabled   (older macOS: => true)
        if "=>" in line:
            label, _, state = line.partition("=>")
            if state.strip() in ("disabled", "true"):
                out.add(label.strip().strip('"'))
    return out


def is_stopped() -> bool:
    return STOPPED_MARKER.is_file()


def _chroma_up(timeout_s: float) -> bool:
    port = os.environ.get("CO_CHROMA_PORT", "8765")
    url = f"http://127.0.0.1:{port}/api/v2/heartbeat"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


# ------------------------------------------------------------------ commands

def status() -> list[dict]:
    disabled = _disabled_labels()
    rows = []
    for a in agents():
        pid = _pid(a["label"])
        rows.append({**a, "pid": pid, "disabled": a["label"] in disabled})
    return rows


def stop(log=print) -> bool:
    found = agents()
    if not found:
        log("No contorch daemons are installed.")
        return False
    ok = True
    for a in found:
        target = f"gui/{_uid()}/{a['label']}"
        _launchctl("disable", target)          # stays stopped across login
        res = _launchctl("bootout", target)    # SIGTERM; the daemons shut down cleanly
        # 3 / 113 / "No such process": already not running — fine.
        if res.returncode not in (0, 3, 36, 113) and "No such process" not in res.stderr:
            ok = False
            log(f"  ✗ {a['desc']}: {res.stderr.strip() or res.returncode}")
            continue
        log(f"  ■ {a['desc']} stopped")
    # launchd reports success before the process has actually exited.
    for _ in range(10):
        if not any(_pid(a["label"]) for a in found):
            break
        time.sleep(0.5)
    still = [a["desc"] for a in found if _pid(a["label"])]
    if still:
        ok = False
        log(f"  ✗ still running: {', '.join(still)}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STOPPED_MARKER.write_text(json.dumps({"at": time.time(), "labels": [a["label"] for a in found]}))
    return ok


def _cli_stack(sub) -> None:
    sub.add_parser("stop", help="stop every contorch daemon and keep them stopped across login").set_defaults(
        func=_cmd_stop)
    sub.add_parser("resume", help="start every contorch daemon again, in order").set_defaults(func=_cmd_resume)


def _cmd_stop(args) -> int:
    print("Stopping contorch…")
    ok = stop()
    print("\nStopped. Nothing records, indexes, or answers searches until `contorch resume`."
          if ok else "\nStopped with errors (above).")
    return 0 if ok else 1


def _cmd_resume(args) -> int:
    print("Resuming contorch…")
    ok = resume()
    print("\nRunning." if ok else "\nResumed with errors (above). `contorch status` for details.")
    return 0 if ok else 1


def resume(log=print) -> bool:
    found = list(reversed(agents()))  # index first, capture last
    if not found:
        log("No contorch daemons are installed.")
        return False
    ok = True
    for a in found:
        target = f"gui/{_uid()}/{a['label']}"
        _launchctl("enable", target)
        if _pid(a["label"]) is None:
            res = _launchctl("bootstrap", f"gui/{_uid()}", str(a["plist"]))
            # 5 / 17 / 37: already loaded (e.g. loaded but not running) — kick it instead.
            if res.returncode != 0:
                _launchctl("kickstart", target)
        if a["component"] == "context-orchestrator-chroma":
            if _chroma_up(60):
                log(f"  ▶ {a['desc']} running")
            else:
                ok = False
                log(f"  ✗ {a['desc']} did not answer its heartbeat within 60s")
            continue
        for _ in range(20):
            if _pid(a["label"]):
                break
            time.sleep(0.5)
        if _pid(a["label"]):
            log(f"  ▶ {a['desc']} running")
        else:
            ok = False
            log(f"  ✗ {a['desc']} did not start — see its log")
    STOPPED_MARKER.unlink(missing_ok=True)
    return ok


# ------------------------------------------------------------------ setup

KEY_FILE = Path.home() / ".config" / "google" / "key"
CHROMA_DIR = Path.home() / ".context-orchestrator" / "chroma"
CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"
CLAUDE_JSON = Path.home() / ".claude.json"
MCP_NAME = "context-orchestrator"   # tool names in CLAUDE.md guidance depend on it
AI_STUDIO = "https://aistudio.google.com/apikey"
TCC_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"


def _interactive() -> bool:
    return sys.stdin.isatty() and os.environ.get("CONTORCH_NONINTERACTIVE") != "1"


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


KEY_VARS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")   # meeting-capture's lookup order


def _have_key() -> bool:
    """A key in this shell or the key file. Right for things started from the
    shell; NOT for the recorder, which runs under launchd — see _recorder_key()."""
    if any(os.environ.get(v) for v in KEY_VARS):
        return True
    return _key_file_has_key()


def _key_file_has_key() -> bool:
    try:
        return KEY_FILE.is_file() and KEY_FILE.read_text().strip() != ""
    except OSError:
        return False


MC_LABEL = "com.contorch.meeting-capture"


def _key_out_of_reach() -> tuple[str, str, str] | None:
    """A Gemini key that exists here but that the recorder will not have once
    setup is done, as (key, where it is, why the recorder can't use it); None
    if there is none.

    The recorder is a launchd agent: it never sees this shell's environment
    (a key exported in ~/.zshrc), and `meeting-capture install`, which setup
    runs, rewrites the agent's plist env with only PATH and MEETING_CAPTURE_*,
    so a key put there by hand is dropped too. After setup the key file is the
    only place it reads a key from. (Setup provisions keys, so it needs to know
    where one must go; whether the recorder sees one *now* is meeting-capture's
    answer, `stt --json` "gemini_key".)"""
    sources = (
        (os.environ, "your shell",
         "it runs in the background under launchd and never sees your shell's environment"),
        (_plist_env(MC_LABEL), "its launchd plist",
         "`meeting-capture install`, which setup runs, rewrites that plist without it"),
    )
    for source, where, why in sources:
        for var in KEY_VARS:
            v = str(source.get(var) or "").strip()
            if v:
                return v, f"{var} in {where}", why
    return None


def _recorder_key(log) -> bool:
    """Will the recorder find a Gemini key after setup? The key file, or a key
    from the shell / plist env that the user agrees to save there."""
    if _key_file_has_key():
        log(f"  ✓ Gemini key found ({KEY_FILE})")
        return True
    found = _key_out_of_reach()
    if found is None:
        return False
    key, where, why = found
    log(f"  ! The recorder can't use {where}:")
    log(f"    {why}.")
    log(f"    It reads its Gemini key from {KEY_FILE}.")
    if not _interactive():
        return False
    ans = input(f"  Save that key to {KEY_FILE} (readable only by you)? [Y/n] ").strip().lower()
    if ans in ("", "y", "yes"):
        _write_key(key)
        log(f"  ✓ saved to {KEY_FILE} (readable only by you)")
        return True
    log("  · not saved")
    return False


def _key_todo() -> str:
    """How to give the recorder a key, naming a key it can't see if there is one."""
    found = _key_out_of_reach()
    note = f" (the recorder can't use {found[1]})" if found else ""
    return f"write the key to {KEY_FILE} (chmod 600){note}"


def _write_key(key: str) -> None:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = KEY_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # never world-readable, even briefly
    with os.fdopen(fd, "w") as f:
        f.write(key.strip())
    os.replace(tmp, KEY_FILE)


def _plist_env(label: str) -> dict:
    import plistlib
    try:
        return plistlib.loads((LAUNCH_AGENTS / f"{label}.plist").read_bytes()).get("EnvironmentVariables") or {}
    except Exception:
        return {}


def _plist_program(label: str) -> str:
    import plistlib
    try:
        return plistlib.loads((LAUNCH_AGENTS / f"{label}.plist").read_bytes())["ProgramArguments"][0]
    except Exception:
        return ""


def _existing_mcp_env() -> dict:
    """CO_* settings from an existing registration (the key itself is NOT carried —
    it belongs in the key file, not in ~/.claude.json)."""
    try:
        srv = json.loads(CLAUDE_JSON.read_text()).get("mcpServers", {}).get(MCP_NAME) or {}
    except Exception:
        return {}
    return {k: v for k, v in (srv.get("env") or {}).items() if k.startswith("CO_")}


def _existing_mcp_entry() -> dict | None:
    try:
        return json.loads(CLAUDE_JSON.read_text()).get("mcpServers", {}).get(MCP_NAME)
    except Exception:
        return None


def mcp_add_cmd(claude: str, env: dict, server: str) -> list[str]:
    """`claude mcp add` argv. The server name must come BEFORE the -e options:
    -e is variadic in the Claude CLI and swallows everything up to `--`,
    including a name placed after it ("missing required argument")."""
    cmd = [claude, "mcp", "add", "--scope", "user", MCP_NAME]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    return cmd + ["--", server]


def _claude_md_template() -> Path | None:
    brew = shutil.which("brew")
    if brew:
        res = _run([brew, "--prefix", "context-orchestrator"])
        cand = Path(res.stdout.strip()) / "share" / "context-orchestrator" / "claude-md-template.md"
        if res.returncode == 0 and cand.is_file():
            return cand
    return None


EMBEDDING_CHOICES = (
    ("gemini", "Gemini — best at paraphrased questions; needs the key, sends text to Google"),
    ("local", "Local model — offline, no key; ~80 MB download; weaker on paraphrases"),
    ("none", "None — keyword search only; nothing leaves this Mac"),
)


def _contorch_memory_bin() -> str | None:
    return owners.locate("contorch-memory")


def _setup_embeddings(log, todo: list, done: list, choice: str | None = None) -> None:
    """Ask gemini / local / none and store it with `contorch-memory embeddings`.
    Full-text search is on whatever the answer. Non-interactive runs keep the
    current setting (default: Gemini when a key exists, else local)."""
    cm = _contorch_memory_bin()
    if not cm:
        log("  · this context-orchestrator predates the choice — using Gemini when a key exists")
        return
    current = _run([cm, "embeddings"]).stdout.strip()
    if choice is None and _interactive():
        log(f"  Now: {current}")
        default = "gemini" if _have_key() else "local"
        for i, (name, desc) in enumerate(EMBEDDING_CHOICES, 1):
            log(f"    {i}. {desc}{'  (default)' if name == default else ''}")
        ans = input("  Choose 1-3 (Enter for default): ").strip()
        choice = {"1": "gemini", "2": "local", "3": "none"}.get(ans, default)
    if choice is None:
        log(f"  ✓ {current} (unchanged — `contorch-memory embeddings gemini|local|none` to change)")
        done.append("Search embeddings")
        return
    res = _run([cm, "embeddings", choice])
    if res.returncode == 0:
        log("  ✓ " + res.stdout.strip().replace("\n", "\n    "))
        done.append("Search embeddings")
    else:
        log("  ✗ " + (res.stderr or res.stdout).strip()[-400:])
        todo.append(f"Set search embeddings: contorch-memory embeddings gemini|local|none")


STT_UPGRADE = (
    "Gemini is an optional upgrade: it gets names and codenames right more often,",
    "labels who is speaking, and writes Hindi in Devanagari (on-device Hindi comes",
    "out romanized). It also works on older Macs. With Gemini, meeting audio is",
    "uploaded to Google for transcription.",
)


def _ask_for_key(log) -> bool:
    """Open AI Studio and read a key (hidden). True if one was saved."""
    log(f"  Opening {AI_STUDIO} — create a key, then paste it here.")
    _run(["open", AI_STUDIO])
    key = getpass.getpass("  Gemini API key (input hidden, Enter to skip): ").strip()
    if not key:
        return False
    _write_key(key)
    log(f"  ✓ saved to {KEY_FILE} (readable only by you)")
    return True


LIVE_KEEP_LOCAL = "(`meeting-capture mode batch` or `meeting-capture stt apple` keeps audio on this Mac)."
APPLY_TIMEOUT_S = 45 * 60      # a first download of a language's model can take a while


def _need_key(log, todo: list) -> bool:
    """Gemini is the only way to transcribe here: make sure the recorder will
    find a key (the key file), asking for one on a terminal."""
    if _recorder_key(log) or (_interactive() and _key_out_of_reach() is None and _ask_for_key(log)):
        return True
    if not _interactive():
        log("  ✗ no key the recorder can use, and no terminal to ask on — meeting transcription stays off")
    todo.append(f"Add a Gemini key: {_key_todo()}, then run `contorch setup` again")
    return False


def _choose_transcription(log, todo: list, mc: str) -> list[str] | None:
    """How should meetings be transcribed?

    meeting-capture decides what every setting means and says so with
    `meeting-capture stt --json` (pipeline_monitor.transcription). This step
    only asks, makes sure a Gemini key is where the recorder reads it when
    Gemini is wanted, and returns the meeting-capture command that applies
    the answer — taken from the JSON's own hints — to run once the recorder is
    installed (_apply_stt), or None to leave it as it is. Where the audio goes
    is said only afterwards, from a fresh answer (_report_transcription)."""
    log("  Asking meeting-capture what this Mac can do (its first run sets itself up — a minute or so)…")
    t = stt.fresh(mc)
    if t["status"] == "old":
        log(f"  This meeting-capture transcribes with Google Gemini only ({stt.OLD_LABEL}).")
        log("  That needs a Gemini API key; meeting audio is uploaded to Google for it.")
        _need_key(log, todo)
        return None
    if t["status"] != "ok":
        log(f"  ! couldn't ask meeting-capture how it transcribes: {t['error']}")
        todo.append("Check how meetings are transcribed: meeting-capture stt (then run contorch setup again)")
        return None
    d = t["data"]
    a = d.get("apple") or {}
    if not (a.get("usable") or a.get("installable")):
        reason = a.get("reason") or "unavailable"
        log(f"  · On-device transcription isn't available here: {reason}")
        if d.get("choice") == "apple":
            log("    It is set to on-device only, so recordings wait on this Mac until it works.")
            todo.append(f"Transcription is waiting for on-device speech ({reason}). "
                        "To use Gemini instead: meeting-capture stt auto, and add a Gemini key")
            return None
        log("  Transcription then needs Gemini (a Gemini API key); meeting audio is uploaded")
        log("  to Google for it. Transcripts and the search index stay on this Mac.")
        _need_key(log, todo)
        return None

    loc = d.get("locale") or "?"
    log(f"  ✓ This Mac can transcribe meetings itself (Apple on-device speech, {loc}) — no API key needed.")
    if d.get("locale_guessed"):
        log(f"    ({d['locale_why']}. Gemini detects the language itself.)")
    if d["live"]["active"]:
        log("    But live mode is on, and it streams every call to Google Gemini whichever")
        log("    engine you pick here.")
    # With a key the recorder can see, "on this Mac" is two settings: only
    # (`stt apple`, never uploads) or with Gemini as backup (`stt auto`: when
    # on-device fails, meeting-capture sends the chunk to Gemini by itself).
    # Without one they upload the same (never), so it's one choice that keeps
    # whichever is set. The commands are the JSON's own hints.
    key = bool(d.get("gemini_key"))
    only = "mac_only" if key or d.get("choice") == "apple" else "mac"
    # Default: what transcribes now — unless Gemini is only standing in until
    # the on-device model arrives (needs_model), then this Mac.
    if d.get("choice") == "gemini" or (d["engine"] == "gemini" and not d.get("needs_model")):
        want = "gemini"
    else:
        want = "mac_only" if d.get("choice") == "apple" else "mac"
    if _interactive():
        log("")
        for line in STT_UPGRADE:
            log("  " + line)
        if key:
            options = [("mac_only", f"On this Mac only ({loc}) — never uploads"),
                       ("mac", f"On this Mac ({loc}), Gemini as backup — uploads only if on-device "
                               "transcription stops working"),
                       ("gemini", "Gemini — meeting audio is uploaded to Google")]
        else:
            options = [(only, f"On this Mac ({loc})"), ("gemini", "Gemini — needs a free API key")]
        default = next((str(i) for i, (w, _) in enumerate(options, 1) if w == want), "1")
        for i, (_, text) in enumerate(options, 1):
            log(f"    {i}. {text}{'  (default)' if str(i) == default else ''}")
        ans = input(f"  Choose 1-{len(options)} (Enter for default): ").strip() or default
        want = dict((str(i), w) for i, (w, _) in enumerate(options, 1)).get(ans, options[int(default) - 1][0])
    if want == "gemini":
        if _recorder_key(log) or (_interactive() and _key_out_of_reach() is None and _ask_for_key(log)):
            # Already Gemini, and staying so (not just until a missing on-device
            # model arrives): leave the setting alone. Otherwise pick it.
            keep = d["engine"] == "gemini" and d["ready"] and not d.get("needs_model")
            return None if keep else [mc, "stt", "gemini"]
        if not _interactive():
            # Gemini is the default here (chosen before, or meeting-capture's
            # pick for this Mac's language) but the recorder won't have a key.
            log("  ✗ Gemini transcribes here, but the recorder will have no Gemini key")
            fix = d.get("on_device_hint") or "meeting-capture stt auto"
            todo.append(f"Transcription uses Gemini but the recorder has no key: {_key_todo()}, "
                        f"or transcribe on this Mac: {fix}")
            return None
        log("  · no key the recorder can use — transcription stays on this Mac")
        want = "mac_only" if d.get("choice") == "apple" else "mac"
    return stt.command(d.get("on_device_only_hint" if want == "mac_only" else "on_device_hint"), mc)


def _apply_stt(cmd: list[str] | None, log, todo: list) -> None:
    """Run the meeting-capture command _choose_transcription picked (`stt …`
    or `language …`: meeting-capture owns the plist, sets up the speech model
    and restarts its recorder), showing its progress lines as they come."""
    if not cmd:
        return
    shown = " ".join(["meeting-capture", *cmd[1:]])
    log(f"  $ {shown}")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", start_new_session=True)
    except OSError as e:
        log(f"  ✗ could not run it: {e.strerror or e}")
        todo.append(f"Set up transcription: {shown}")
        return
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        try:
            os.killpg(proc.pid, signal.SIGKILL)     # and the helper's model download it started
        except OSError:
            pass
    killer = threading.Timer(APPLY_TIMEOUT_S, _kill)
    killer.start()
    try:
        for line in proc.stdout:
            log(f"    {line.rstrip()}")
        rc = proc.wait()
    finally:
        killer.cancel()
    if timed_out.is_set():
        log(f"  ✗ `{shown}` timed out after {APPLY_TIMEOUT_S // 60} min and was stopped — "
            "check with: meeting-capture stt")
        todo.append(f"Set up transcription (it timed out): {shown}")
    elif rc != 0:
        log(f"  ✗ `{shown}` failed (exit {rc})")
        todo.append(f"Set up transcription: {shown}")


def _report_transcription(mc: str, log, todo: list, done: list) -> None:
    """How meetings will be transcribed and where the audio goes, from
    meeting-capture's answer once setup has changed what it changes. The one
    place setup says so; the wording comes from transcription.privacy() (the
    JSON's "live.active", "uploads" and "may_upload")."""
    t = stt.fresh(mc)
    if t["status"] not in ("ok", "old"):
        log(f"  ✗ couldn't ask meeting-capture how it transcribes: {t['error']}")
        todo.append("Check how meetings are transcribed: meeting-capture stt")
        return
    d = t["data"] or {}
    log(f"  {'✗' if t['attention'] else '✓'} transcription: {t['label']}")
    if t["privacy"]:
        tail = ("; transcripts and the search index stay here too." if t["may_leave_mac"] is False
                else "; transcripts and the search index stay on this Mac.")
        log(f"    {t['privacy'][0].upper()}{t['privacy'][1:]}{tail}")
    if t["privacy_fix"]:
        log(f"    ({t['privacy_fix']}.)")
    live = d.get("live") or {}
    if live.get("active") and (d.get("apple") or {}).get("usable"):
        log(f"    {LIVE_KEEP_LOCAL}")
    elif live.get("requested") and not live.get("active"):
        log(f"    · Live mode is requested, but the recorder records in batch: {live.get('blocker')}.")
    if t["note"]:
        log(f"    ! {t['note']}")
    if t["status"] == "old":
        log("    Upgrade for on-device transcription: brew upgrade meeting-capture")
        if not _key_file_has_key():
            return                      # Gemini-only and no key: step 1 left the to-do
    if t["attention"]:
        hint = d.get("install_hint")
        todo.append(f"Transcription can't run yet: {d.get('reason')}" + (f" — run: {hint}" if hint else ""))
    else:
        done.append(f"Transcription: {t['label']}")


def _cli_setup(sub) -> None:
    sub.add_parser("setup", help="configure everything after installing (safe to re-run)").set_defaults(
        func=lambda args: 0 if setup() else 1)


def setup(log=print) -> bool:
    """Everything scriptable, in order, then an honest list of what is left."""
    todo: list[str] = []
    done: list[str] = []

    def step(title: str) -> None:
        log(f"\n▶ {title}")

    # 0. What is installed?
    step("Checking components")
    bins = {n: owners.locate(n) for n in ("meeting-capture", "context-orchestrator-chroma",
                                          "transcript-watcher", "contorch-mcp")}
    missing = [n for n, p in bins.items() if not p]
    if missing:
        log(f"  ✗ not on PATH: {', '.join(missing)}")
        log("    Install everything with: brew install contorch/tap/contorch")
        return False
    for n, p in bins.items():
        log(f"  ✓ {n}")
    claude = shutil.which("claude")
    log("  ✓ Claude Code" if claude else "  ! Claude Code not found — meetings will be captured but not connected to Claude")

    log("\n  contorch records meeting audio when another app uses your microphone")
    log("  and turns it into searchable transcripts.")

    # 1. Transcription: on this Mac when it can (no key), else Gemini (key).
    #    Applied and reported once the recorder is installed (step 5).
    step("Transcription")
    stt_cmd = _choose_transcription(log, todo, bins["meeting-capture"])

    # 1b. How search understands questions (context-orchestrator >= 0.4).
    step("Search embeddings")
    _setup_embeddings(log, todo, done)

    # 2. Move an older source install over, with a backup of the index.
    old = [a for a in agents()
           if "/.context-orchestrator/venv/" not in _plist_program(a["label"])
           and "/.meeting-capture/venv/" not in _plist_program(a["label"])]
    if old:
        step("Moving your existing daemons onto this install")
        for a in old:
            log(f"  · {a['desc']}: {_plist_program(a['label'])}")
        stop(log=lambda m: log("  " + m.strip()))
        backup = CHROMA_DIR.parent / "chroma.backup-before-contorch-setup"
        if CHROMA_DIR.is_dir() and not backup.exists():
            shutil.copytree(CHROMA_DIR, backup)
            log(f"  ✓ backed up the search index to {backup}")

    # 3. Search index + indexer
    step("Search index")
    res = _run([bins["context-orchestrator-chroma"], "install"], timeout=900)
    if res.returncode != 0:
        log("  ✗ chroma install failed:\n" + (res.stderr or res.stdout)[-800:])
        return False
    _launchctl("enable", f"gui/{_uid()}/com.contorch.context-orchestrator-chroma")
    if _chroma_up(90):
        log("  ✓ chroma running")
    else:
        log("  ✗ chroma did not answer within 90s — see ~/.context-orchestrator/chroma-daemon.log")
        return False
    # No indexer daemon: the MCP server indexes new transcripts on demand
    # (context-orchestrator 0.3+). Retire an agent from an older install.
    for org in ORGS:
        plist = LAUNCH_AGENTS / f"com.{org}.transcript-watcher.plist"
        if plist.is_file():
            _launchctl("bootout", f"gui/{_uid()}/com.{org}.transcript-watcher")
            plist.unlink()
            log("  ✓ removed the old transcript-watcher daemon (indexing is on demand now)")
    done.append("search index")

    # 4. Claude Code
    step("Claude Code connection")
    if claude:
        env = _existing_mcp_env()
        previous = _existing_mcp_entry()
        _run([claude, "mcp", "remove", "--scope", "user", MCP_NAME])
        res = _run(mcp_add_cmd(claude, env, bins["contorch-mcp"]))
        if res.returncode == 0:
            log(f"  ✓ registered `{MCP_NAME}` → {bins['contorch-mcp']}"
                + (f" (kept {', '.join(env)})" if env else ""))
            done.append("Claude Code registration")
        else:
            log("  ✗ claude mcp add failed: " + (res.stderr or res.stdout).strip()[-300:])
            if previous:
                # Never leave Claude with no server: put the old entry back.
                _run([claude, "mcp", "add-json", "--scope", "user", MCP_NAME, json.dumps(previous)])
                log("    restored your previous registration")
            todo.append("Register the MCP server: claude mcp add --scope user "
                        f"{MCP_NAME} -- {bins['contorch-mcp']}")
        tpl = _claude_md_template()
        # Skip if CLAUDE.md already covers it — the template's marker, or the
        # user's own hand-written guidance (don't give them two copies).
        existing = CLAUDE_MD.read_text().lower() if CLAUDE_MD.is_file() else ""
        if tpl and "context-orchestrator" not in existing and "context orchestrator" not in existing:
            CLAUDE_MD.parent.mkdir(parents=True, exist_ok=True)
            with CLAUDE_MD.open("a") as f:
                f.write("\n" + tpl.read_text())
            log(f"  ✓ added usage guidance to {CLAUDE_MD}")
    else:
        todo.append("Install Claude Code, then run `contorch setup` again to connect it")

    # 5. Meeting capture
    step("Meeting capture")
    res = _run([bins["meeting-capture"], "install"])
    if res.returncode != 0:
        log("  ✗ meeting-capture install failed:\n" + (res.stderr or res.stdout)[-800:])
        return False
    _launchctl("enable", f"gui/{_uid()}/com.contorch.meeting-capture")
    sysaudio = _plist_env("com.contorch.meeting-capture").get("MEETING_CAPTURE_SYSAUDIO", "")
    log("  ✓ capture daemon running")
    _apply_stt(stt_cmd, log, todo)
    _report_transcription(bins["meeting-capture"], log, todo, done)

    # 6. The one permission macOS will not let us grant
    step("Allow system-audio recording (macOS requires you to do this)")
    if sysaudio:
        subprocess.run(["pbcopy"], input=sysaudio, text=True)
        log("  The sysaudio path is on your clipboard:")
        log(f"    {sysaudio}")
    log("  1. System Settings → Privacy & Security → Screen & System Audio Recording")
    log("  2. Click +, press ⌘⇧G, then ⌘V, and choose sysaudio")
    log("  3. Turn it on (also under “System Audio Recording Only” if that list is shown)")
    log("  4. macOS 15+: on your first call, click Allow when sysaudio asks for the Microphone")
    if _interactive():
        _run(["open", TCC_PANE])
        input("\n  Press Enter when sysaudio is added and turned on… ")
        _launchctl("kickstart", "-k", f"gui/{_uid()}/com.contorch.meeting-capture")
        done.append("Screen & System Audio Recording (granted by you; confirmed on your first call)")
    else:
        todo.append(f"Grant Screen & System Audio Recording to {sysaudio or 'sysaudio'}")

    # 7. Menu bar
    step("Menu bar")
    brew = shutil.which("brew")
    if brew and _run([brew, "list", "--versions", "contorch"]).returncode == 0:
        res = _run([brew, "services", "restart", "contorch/tap/contorch"], timeout=120)
        log("  ✓ ○ is in your menu bar (starts at login)" if res.returncode == 0
            else "  ! could not start it: brew services start contorch/tap/contorch")
        if res.returncode != 0:
            todo.append("Start the menu bar: brew services start contorch/tap/contorch")
    else:
        log("  · not installed via Homebrew — start the menu bar with: pipeline-monitor &")

    STOPPED_MARKER.unlink(missing_ok=True)

    # 8. Truth
    log("\n" + "─" * 60)
    for d in done:
        log(f"  ✓ {d}")
    for t in todo:
        log(f"  ✗ {t}")
    log("\nNext:")
    if claude:
        log("  1. Restart Claude Code so it loads the contorch MCP server.")
    log("  2. Join a short call and say something. Then: meeting-capture last")
    if claude:
        log("  3. In Claude Code ask: “Search contorch for my latest meeting. Cite the transcript.”")
    log("\n  Health check any time: contorch doctor · Pause everything: contorch stop")
    return not todo


def _transcription() -> dict | None:
    """meeting-capture's answer (transcription.current), or None when its
    agent isn't installed."""
    if not stt.PLIST.exists():
        return None
    return stt.current(wait=True)


def _print_transcription_detail() -> int:
    print("\n── transcription")
    t = _transcription()
    if t is None:
        print("  ✗ meeting-capture's agent is not installed — run contorch setup")
        return 1
    good = t["status"] == "old" or (t["status"] == "ok" and not t["attention"])
    print(f"  {'✓' if good else '✗'} {t['label']}")
    d = t["data"]
    if t["status"] == "old":
        print("    upgrade for on-device transcription: brew upgrade meeting-capture")
    if d:
        print(f"    setting: {d.get('choice')} · locale: {d.get('locale')} ({d.get('locale_why')}) · "
              f"Gemini key: {'yes' if d.get('gemini_key') else 'none'}")
        a = d.get("apple") or {}
        print(f"    on-device: {'ready' if a.get('usable') else a.get('reason')}"
              + (f" ({a['helper']})" if a.get("helper") else ""))
        if a.get("installed_locales"):
            print(f"    installed speech models: {', '.join(a['installed_locales'])}")
        if d.get("needs_model") and d.get("install_hint"):
            print(f"    set up the speech model: {d['install_hint']}")
        live = d.get("live") or {}
        if live.get("active"):
            print("    live mode: on — every call streams to Gemini as it happens (uploaded); "
                  "the engine above only transcribes parked audio")
        elif live.get("requested"):
            print(f"    live mode: requested, but {live.get('blocker')} — running batch")
        if d.get("notice"):
            print(f"    note: {d['notice']}")
    if t["privacy"]:
        print(f"    audio: {t['privacy']}" + (f" ({t['privacy_fix']})" if t["privacy_fix"] else ""))
    if t["note"]:
        print(f"    ! {t['note']}")
    if d:
        print("    change it: meeting-capture stt auto|apple|gemini · meeting-capture language LOCALE"
              " · meeting-capture mode batch|live")
    return 0 if good else 1


def _cli_doctor(sub) -> None:
    sub.add_parser("doctor", help="health-check every component").set_defaults(func=lambda args: doctor())


def doctor() -> int:
    rc = _print_status(transcription=False)
    rc = _print_transcription_detail() or rc
    for name in ("meeting-capture", "transcript-watcher"):
        b = owners.locate(name)
        if not b:
            print(f"\n✗ {name} not on PATH")
            rc = 1
            continue
        print(f"\n── {name} doctor")
        res = subprocess.run([b, "doctor"])
        rc = rc or res.returncode
    claude = shutil.which("claude")
    if claude:
        res = _run([claude, "mcp", "get", MCP_NAME])
        print(f"\n── Claude Code\n  {'✓ MCP server registered' if res.returncode == 0 else '✗ MCP server not registered — run contorch setup'}")
    return rc


# ------------------------------------------------------------------ CLI

def _cli_status(sub) -> None:
    sub.add_parser("status", help="show every contorch daemon, whether it is running, and the "
                                  "transcription engine").set_defaults(func=lambda args: _print_status())


def _print_status(transcription: bool = True) -> int:
    rows = status()
    if not rows:
        print("No contorch daemons are installed. Run the installer first.")
        return 1
    if is_stopped():
        print("contorch is STOPPED (run `contorch resume` to start it again)\n")
    for r in rows:
        if r["pid"]:
            state = f"running (pid {r['pid']})"
        elif r["disabled"]:
            state = "stopped"
        else:
            state = "not running"
        print(f"  {r['desc']:<24} {state:<22} {r['label']}")
    t = _transcription() if transcription else None
    if t is not None:
        d = t["data"]
        print(f"\n  {'transcription':<24} {t['label']}"
              + (f"  [setting: {d['choice']}, locale {d['locale']}]" if d else ""))
        if t["privacy"]:
            print(f"  {'':<24} audio: {t['privacy']}" + (f" ({t['privacy_fix']})" if t["privacy_fix"] else ""))
        if t["note"]:
            print(f"  {'':<24} ! {t['note']}")
    return 0


# Commands that live in their own modules: each defines add_cli(subparsers)
# and sets `func`. A module that isn't in this install is skipped.
CLI_MODULES = ("channel", "modules", "adopt", "uninstall", "lifecycle", "diagnostics")


def _module_clis(sub) -> None:
    import importlib
    import importlib.util
    for name in CLI_MODULES:
        if importlib.util.find_spec(f"pipeline_monitor.{name}") is not None:
            importlib.import_module(f"pipeline_monitor.{name}").add_cli(sub)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="contorch", description="Control the whole contorch stack.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for add in (_cli_setup, _cli_doctor, _cli_status, _cli_stack):
        add(sub)
    _module_clis(sub)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
