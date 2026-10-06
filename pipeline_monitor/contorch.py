"""`contorch` — one command for the whole contorch stack.

    contorch setup      configure everything after `brew install contorch/tap/contorch`
    contorch doctor     health-check every component (incl. the transcription engine)
    contorch status     what is installed, running, stopped; how meetings are transcribed
    contorch stop       stop every background daemon, and keep them stopped across login
    contorch resume     start them again, in dependency order, and check they came up
    contorch channel    which install (app / brew / dev) owns Contorch on this Mac
    contorch modules    memory, recorder, line-in, terminal commands: present / wanted / on
    contorch cli        put Contorch.app's commands on your PATH (app only)

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

from . import channel as chan
from . import mcconfig, modules, owners
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


STOP_REASONS = ("user", "quit", "update")      # stopped.json "reason"; only quit/update auto-resume


def _recorder_verb(verb: str, reason: str | None, log) -> bool | None:
    """The recorder goes through meeting-capture (`stop|start --json
    [--reason]`; its supervisor handles either backend, the app's
    SMAppService agent included). None when this meeting-capture predates
    the verbs (0.7) or isn't installed: the caller falls back to launchctl
    for its legacy plist."""
    if owners.locate("meeting-capture") is None:
        return None
    args = [verb, *(["--reason", reason] if verb == "stop" and reason else []), "--json"]
    res = owners.call("meeting-capture", *args, schema="meeting-capture.agent/", timeout=120)
    if res["status"] == "old":
        return None
    if res["status"] != "ok":
        log(f"  ✗ meeting capture: {res['error']}")
        return False
    d = res["data"]
    if not d.get("ok"):
        log(f"  ✗ meeting capture: {(d.get('error') or {}).get('message')}")
        return False
    if d.get("performed") or d.get("why") is None:
        log(f"  {'■' if verb == 'stop' else '▶'} meeting capture {'stopped' if verb == 'stop' else 'running'}")
    else:
        log(f"  · meeting capture: {d.get('why')}")
    return True


def _launchctl_stop(a: dict, log) -> bool:
    target = f"gui/{_uid()}/{a['label']}"
    _launchctl("disable", target)          # stays stopped across login
    res = _launchctl("bootout", target)    # SIGTERM; the daemons shut down cleanly
    # 3 / 113 / "No such process": already not running — fine.
    if res.returncode not in (0, 3, 36, 113) and "No such process" not in res.stderr:
        log(f"  ✗ {a['desc']}: {res.stderr.strip() or res.returncode}")
        return False
    log(f"  ■ {a['desc']} stopped")
    return True


def stop(log=print, reason: str = "user") -> bool:
    """Stop the recorder (meeting-capture's own `stop --json --reason`) and
    any retired agent an older install left (launchctl, legacy labels only),
    and remember why in ~/.contorch/stopped.json: a user's stop stays until
    `contorch resume`; a quit or an update resumes on the next launch."""
    reason = reason if reason in STOP_REASONS else "user"
    found = agents()
    rec = _recorder_verb("stop", reason, log)
    legacy = [a for a in found if not (a["component"] == "meeting-capture" and rec is not None)]
    if rec is None and not found:
        log("No contorch daemons are installed.")
        return False
    ok = rec is not False
    for a in legacy:
        ok = _launchctl_stop(a, log) and ok
    # launchd reports success before the process has actually exited.
    for _ in range(10):
        if not any(_pid(a["label"]) for a in legacy):
            break
        time.sleep(0.5)
    still = [a["desc"] for a in legacy if _pid(a["label"])]
    if still:
        ok = False
        log(f"  ✗ still running: {', '.join(still)}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STOPPED_MARKER.write_text(json.dumps({"at": time.time(), "reason": reason,
                                          "labels": [a["label"] for a in legacy]
                                          + ([MC_LABEL] if rec is not None else [])}))
    return ok


def stopped_reason() -> str | None:
    """Why the stack is stopped (user | quit | update), None when it isn't.
    A marker from before reasons existed counts as the user's."""
    if not STOPPED_MARKER.is_file():
        return None
    try:
        r = json.loads(STOPPED_MARKER.read_text()).get("reason")
    except (OSError, ValueError):
        r = None
    return r if r in STOP_REASONS else "user"


def _cli_stack(sub) -> None:
    for name, help_ in (("stop", "stop every contorch daemon and keep them stopped across login"),
                        ("resume", "start every contorch daemon again, in order")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--json", action="store_true", help="one JSON document (contorch.stack/1)")
        if name == "stop":
            p.add_argument("--reason", choices=STOP_REASONS, default="user",
                           help="why (quit/update resume on the next launch; user waits for `contorch resume`)")
        p.set_defaults(func=_cmd_stop if name == "stop" else _cmd_resume)


def _stack_json(action: str, fn, **kw) -> int:
    from . import jsonout
    lines: list[str] = []
    with jsonout.reserved_stdout() as out:
        ok = fn(log=lines.append, **kw)
        jsonout.emit({"schema": "contorch.stack/1", "ok": ok, "action": action, **kw,
                      "stopped": is_stopped(), "reason": stopped_reason(), "lines": lines}, out)
    return 0 if ok else 1


def _cmd_stop(args) -> int:
    if args.json:
        return _stack_json("stop", stop, reason=args.reason)
    print("Stopping contorch…")
    ok = stop(reason=args.reason)
    print("\nStopped. Nothing records, indexes, or answers searches until `contorch resume`."
          if ok else "\nStopped with errors (above).")
    return 0 if ok else 1


def _cmd_resume(args) -> int:
    if args.json:
        return _stack_json("resume", resume)
    print("Resuming contorch…")
    ok = resume()
    print("\nRunning." if ok else "\nResumed with errors (above). `contorch status` for details.")
    return 0 if ok else 1


def resume(log=print) -> bool:
    """Start what stop() stopped, index first: a retired chroma server an
    older install still has (launchctl), then the recorder through
    meeting-capture (`start --json`: it re-enables a job a legacy stop
    disabled). meeting-capture 0.7: launchctl, as before."""
    found = list(reversed(agents()))  # index first, capture last
    has_mc = owners.locate("meeting-capture") is not None
    if not found and not has_mc:
        log("No contorch daemons are installed.")
        return False
    ok = True
    for a in found:
        if a["component"] == "meeting-capture":
            continue
        ok = _launchctl_start(a, log) and ok
    rec = _recorder_verb("start", None, log) if has_mc else None
    if rec is None:
        for a in found:
            if a["component"] == "meeting-capture":
                ok = _launchctl_start(a, log) and ok
    else:
        ok = rec and ok
    STOPPED_MARKER.unlink(missing_ok=True)
    return ok


def _launchctl_start(a: dict, log) -> bool:
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
            return True
        log(f"  ✗ {a['desc']} did not answer its heartbeat within 60s")
        return False
    for _ in range(20):
        if _pid(a["label"]):
            break
        time.sleep(0.5)
    if _pid(a["label"]):
        log(f"  ▶ {a['desc']} running")
        return True
    log(f"  ✗ {a['desc']} did not start — see its log")
    return False


# ------------------------------------------------------------------ setup

KEY_FILE = Path.home() / ".config" / "google" / "key"
MCP_NAME = "context-orchestrator"   # the MCP server's name in Claude Code (context-orchestrator's)
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


def _check_key(mc: str, log, todo: list) -> str:
    """Ask meeting-capture whether Google accepts the key the recorder will
    use (`meeting-capture stt --json --check-key`: one tiny read-only call).
    -> accepted | rejected | missing | unreachable | unknown (an older
    meeting-capture can't check)."""
    res = owners.call("meeting-capture", "stt", "--json", "--check-key", exe=mc, timeout=60)
    if res["status"] != "ok" or not isinstance((res["data"] or {}).get("key_check"), dict):
        return "unknown"
    kc = res["data"]["key_check"]
    verdict = kc.get("key") or "unknown"
    if verdict == "accepted":
        log("  ✓ Google accepted the Gemini key")
    elif verdict == "rejected":
        log(f"  ✗ Google rejected the Gemini key{': ' + kc['message'] if kc.get('message') else ''}")
        todo.append(f"The Gemini key was rejected — put a valid one in {KEY_FILE} "
                    "(https://aistudio.google.com/apikey), then run contorch setup again")
    elif verdict == "unreachable":
        log("  · couldn't reach Google to check the key (offline?); it will be tried when a meeting needs it")
    return verdict


LIVE_KEEP_LOCAL = "(`meeting-capture mode batch` or `meeting-capture stt apple` keeps audio on this Mac)."
APPLY_TIMEOUT_S = 45 * 60      # a first download of a language's model can take a while


# What setup learned about the Gemini key this run (`stt --json --check-key`).
KEY_VERDICT: dict = {"key": None}


def _key_ok(log, todo: list, mc: str | None) -> bool:
    """A key the recorder can use, which Google doesn't reject."""
    if mc is None:
        return True
    KEY_VERDICT["key"] = _check_key(mc, log, todo)
    return KEY_VERDICT["key"] != "rejected"


def _need_key(log, todo: list, mc: str | None = None) -> bool:
    """Gemini is the only way to transcribe here: make sure the recorder will
    find a key (the key file), asking for one on a terminal, and that Google
    accepts it (meeting-capture checks it)."""
    if _recorder_key(log) or (_interactive() and _key_out_of_reach() is None and _ask_for_key(log)):
        return _key_ok(log, todo, mc)
    if not _interactive():
        log("  ✗ no key the recorder can use, and no terminal to ask on — meeting transcription stays off")
    todo.append(f"Add a Gemini key: {_key_todo()}, then run `contorch setup` again")
    return False


def _on_device_line(d: dict) -> tuple[str, bool]:
    """meeting-capture's `on_device_line` / `on_device_for_mac_language`
    (≥ 0.8), shown as it is. meeting-capture 0.7 has neither: a neutral
    sentence that claims nothing, covering the Mac's language only when the
    language wasn't a guess."""
    if isinstance(d.get("on_device_line"), str) and isinstance(d.get("on_device_for_mac_language"), bool):
        return d["on_device_line"], d["on_device_for_mac_language"]
    a = d.get("apple") or {}
    if not (a.get("usable") or a.get("installable")):
        return f"On-device transcription isn't available on this Mac: {a.get('reason') or 'unavailable'}", False
    if d.get("locale_guessed"):
        return (f"On-device transcription here covers {d.get('locale')}, not this Mac's language "
                f"({d.get('mac_language') or 'unknown'})."), False
    return f"On-device transcription can run on this Mac ({d.get('locale')}).", True


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
        _need_key(log, todo, mc)
        return None
    if t["status"] != "ok":
        log(f"  ! couldn't ask meeting-capture how it transcribes: {t['error']}")
        todo.append("Check how meetings are transcribed: meeting-capture stt (then run contorch setup again)")
        return None
    d = t["data"]
    a = d.get("apple") or {}
    line, for_mac = _on_device_line(d)
    if not (a.get("usable") or a.get("installable")):
        reason = a.get("reason") or "unavailable"
        log(f"  · {line}")
        if d.get("choice") == "apple":
            log("    It is set to on-device only, so recordings wait on this Mac until it works.")
            todo.append(f"Transcription is waiting for on-device speech ({reason}). "
                        "To use Gemini instead: meeting-capture stt auto, and add a Gemini key")
            return None
        log("  Transcription then needs Gemini (a Gemini API key); meeting audio is uploaded")
        log("  to Google for it. Transcripts and the search index stay on this Mac.")
        _need_key(log, todo, mc)
        return None

    loc = d.get("locale") or "?"
    # meeting-capture's own sentence: it never says "this Mac can transcribe"
    # when on-device can't do the Mac's language (then auto keeps Gemini).
    log(f"  {'✓' if for_mac else '·'} {line}")
    if not for_mac:
        log("    Gemini detects the language itself.")
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
        if not for_mac:
            # On-device can't do this Mac's own language: Gemini is offered first.
            options = [o for o in options if o[0] == "gemini"] + [o for o in options if o[0] != "gemini"]
        default = next((str(i) for i, (w, _) in enumerate(options, 1) if w == want), "1")
        for i, (_, text) in enumerate(options, 1):
            log(f"    {i}. {text}{'  (default)' if str(i) == default else ''}")
        ans = input(f"  Choose 1-{len(options)} (Enter for default): ").strip() or default
        want = dict((str(i), w) for i, (w, _) in enumerate(options, 1)).get(ans, options[int(default) - 1][0])
    if want == "gemini":
        if (_recorder_key(log) or (_interactive() and _key_out_of_reach() is None and _ask_for_key(log))) \
                and _key_ok(log, todo, mc):
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
    p = sub.add_parser("setup", help="set up (or repair) Contorch on this Mac — safe to re-run; an interrupted "
                                     "run resumes")
    p.add_argument("--record", choices=("yes", "no"),
                   help="record meetings on this Mac (default: ask; without a terminal: keep what is set up)")
    p.add_argument("--linein", choices=("yes", "no"), help="record from a USB audio interface")
    p.add_argument("--cli", choices=("yes", "no"), help="Contorch.app: put the commands on your PATH")
    p.add_argument("--embeddings", choices=("local", "gemini", "none", "imported"),
                   help="how search understands questions (imported: vectors come from another Mac)")
    p.set_defaults(func=lambda args: 0 if setup(record=args.record, linein=args.linein, cli=args.cli,
                                                 embeddings=args.embeddings) else 1)


RECORDING_NOTE = ("  When another app uses your microphone (a call), Contorch records that meeting's audio,\n"
                  "  turns it into a transcript, and keeps both on this Mac for Claude Code to search.")
MEMORY_ONLY_EMBEDDINGS = (
    ("local", "Local model — offline after one ~80 MB download; no key"),
    ("gemini", "Gemini — needs a key on this Mac; sends text to Google"),
    ("imported", "Imported from your recording Mac — use that Mac's model (usually Gemini); without a key "
                 "here, search is keyword-only"),
)


def _yes(prompt: str, default: bool) -> bool:
    ans = input(f"{prompt} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not ans else ans in ("y", "yes")


def _choose_modules(log, me: str, mc: str | None, record: str | None, linein: str | None,
                    cli: str | None) -> dict[str, bool]:
    """Step 0: which modules this Mac wants. Defaults: what is chosen or set
    up already (modules.json, or what the owners report), else the
    channel's defaults. Without a terminal and without flags: keep it."""
    current = modules.wanted() or modules.infer_wanted(modules.observe(me))
    want = dict(current) if current else {m: modules.MODULES[m].default.get(me, False) for m in modules.ORDER}
    want["memory"] = True
    if me == "brew":
        want["cli"] = True
    if mc is None:
        if want.get("recorder"):
            log("  · the meeting recorder isn't installed here "
                f"({modules.MODULES['recorder'].brew_add}) — memory only")
        want["recorder"] = want["linein"] = False
    elif record is not None:
        want["recorder"] = record == "yes"
    elif _interactive():
        log(RECORDING_NOTE)
        want["recorder"] = _yes("  Record meetings on this Mac?", bool(want.get("recorder", True)))
    if not want["recorder"]:
        want["linein"] = False
    elif linein is not None:
        want["linein"] = linein == "yes"
    elif _interactive() and current is None:
        want["linein"] = _yes("  Do you record from a USB audio interface (line-in)?", bool(want.get("linein")))
    if me == "app":
        if cli is not None:
            want["cli"] = cli == "yes"
        elif _interactive():
            want["cli"] = _yes("  Put the contorch commands on your PATH (~/.local/bin)?", bool(want.get("cli")))
    log("  ✓ " + ", ".join(modules.MODULES[m].title.lower() for m in modules.ORDER if want.get(m))
        + ("" if want["recorder"] else " — this Mac doesn't record (memory only)"))
    return want


def _memory_only_embeddings(log, todo: list, done: list, choice: str | None) -> None:
    """A Mac that doesn't record: where search's vectors come from (§4.2.1),
    stored in modules.json and applied by context-orchestrator."""
    if choice is None and _interactive():
        default = "gemini" if _have_key() else "local"
        for i, (name, desc) in enumerate(MEMORY_ONLY_EMBEDDINGS, 1):
            log(f"    {i}. {desc}{'  (default)' if name == default else ''}")
        ans = input("  Choose 1-3 (Enter for default): ").strip()
        choice = {"1": "local", "2": "gemini", "3": "imported"}.get(ans, default)
    if choice is None:
        _setup_embeddings(log, todo, done)
        return
    if choice in modules.EMBEDDINGS_SOURCES:
        modules.set_embeddings_source(choice)
    _setup_embeddings(log, todo, done, choice="gemini" if choice == "imported" else choice)
    if choice == "imported" and not _have_key():
        log("    Without a Gemini key on this Mac, search uses keywords (the imported vectors need the "
            "same model to search).")


def _selftest(log, todo: list) -> None:
    """context-orchestrator tests the memory end to end. A blocked download
    (offline, a proxy) is a to-do, not a setup failure: keyword search works."""
    res = owners.call("contorch-memory", "selftest", "--json", schema="contorch-memory.selftest/", timeout=600)
    if res["status"] != "ok":
        if res["status"] != "old":
            log(f"  ! couldn't run the memory self-test: {res['error']}")
        return
    d = res["data"]
    if d.get("ok"):
        log(f"  ✓ memory works end to end ({d.get('ms')} ms)")
        return
    code = (d.get("error") or {}).get("code")
    if code in ("offline", "proxy", "tls"):
        log(f"  · the search model couldn't be downloaded ({code})")
        todo.append("Search will use keywords until the model downloads (check the network or proxy, then "
                    "`contorch smoke`)")
    else:
        log(f"  ✗ memory self-test failed at {d.get('stage')}: {(d.get('error') or {}).get('message')}")
        todo.append("The memory self-test failed: contorch smoke (then contorch setup again)")


def _search_index(log, todo: list, done: list) -> bool:
    """Daemon-free memory: context-orchestrator moves a chroma-server install
    to the in-process index itself (`index migrate --in-process`), after a
    verified backup of context.db and the index into ~/.contorch/backups.
    Nothing to do on an install that is already in-process."""
    bdir = STATE_DIR / "backups" / time.strftime("setup-%Y%m%d-%H%M%S")
    res = owners.call("contorch-memory", "index", "migrate", "--in-process", "--backup-dir", str(bdir), "--json",
                      schema="contorch-memory.backup/", timeout=1800)
    if res["status"] == "old":
        log("  ! this context-orchestrator predates the in-process index — brew upgrade context-orchestrator")
        todo.append("Upgrade context-orchestrator (≥ 0.5), then run contorch setup again")
        return True
    if res["status"] != "ok":
        log(f"  ✗ {res['error']}")
        todo.append("Move the search index in-process: contorch-memory index migrate --in-process")
        return False
    d = res["data"]
    if not d.get("ok"):
        err = d.get("error") or {}
        log(f"  ✗ {err.get('code')}: {err.get('message')} — nothing was changed")
        todo.append(f"Move the search index in-process ({err.get('code')}): contorch-memory index migrate "
                    "--in-process")
        return False
    if d.get("performed"):
        log(f"  ✓ backed up your memory to {bdir} and retired the chroma server (the index is in-process now)")
    else:
        log("  ✓ in-process (no background server)")
    for row in _retired_agents():
        log(f"  ✓ removed the old {row} daemon (indexing is on demand now)")
    done.append("Search index (in-process)")
    return True


def _retired_agents() -> list[str]:
    """Remove a transcript-watcher an older install left (context-orchestrator
    0.3+ indexes on demand). Legacy cleanup is the only launchctl setup uses."""
    out = []
    for org in ORGS:
        plist = LAUNCH_AGENTS / f"com.{org}.transcript-watcher.plist"
        if plist.is_file():
            _launchctl("bootout", f"gui/{_uid()}/com.{org}.transcript-watcher")
            plist.unlink()
            out.append("transcript-watcher")
    return out


def _claude_code(log, todo: list, done: list, me: str, recorder: bool) -> None:
    """Claude Code is context-orchestrator's to connect (`contorch-memory
    claude install`: MCP server, hook, CLAUDE.md block, transcripts skill)
    and meeting-capture's /meeting skill its own. pm only reads their
    answers."""
    res = owners.call("contorch-memory", "claude", "install", "--channel", me, "--json",
                      schema="contorch-memory.claude/", timeout=300)
    if res["status"] == "old":
        log("  ! this context-orchestrator can't connect Claude Code itself — brew upgrade context-orchestrator")
        todo.append("Upgrade context-orchestrator (≥ 0.5), then run contorch setup again to connect Claude Code")
    elif res["status"] != "ok":
        log(f"  ✗ {res['error']}")
        todo.append(f"Connect Claude Code: contorch-memory claude install --channel {me}")
    else:
        d = res["data"]
        for part, title in (("mcp", "MCP server"), ("hook", "auto-context hook"), ("claude_md", "CLAUDE.md guidance"),
                            ("skill", "transcripts skill")):
            row = d.get(part) or {}
            log(f"  {'✓' if row.get('matches') else '·'} {title}"
                + ("" if row.get("matches") else " — not set up"))
        if (d.get("hook") or {}).get("legacy_copy") == "kept_custom":
            log("    (your own ~/.claude/hooks/auto-context.py was kept beside it)")
        if d.get("blocked_by_managed_settings"):
            log("  ! Claude Code's managed settings block part of this: " + "; ".join(d.get("managed_reasons") or []))
        for item in d.get("todo") or []:
            todo.append(str(item))
        if d.get("ok"):
            done.append("Claude Code connection")
        elif d.get("error"):
            log(f"  ✗ {d['error'].get('message')}")
            todo.append(f"Connect Claude Code: contorch-memory claude install --channel {me}")
    if recorder:
        sk = owners.call("meeting-capture", "skill", "install", "--json", schema="meeting-capture.skill/", timeout=60)
        if sk["status"] == "ok" and sk["data"].get("ok"):
            action = sk["data"].get("action")
            log("  ✓ /meeting skill" + (" (your own copy kept)" if action == "kept_user_copy" else ""))


def _agent(mc: str, verb: str, log, *extra: str) -> bool | None:
    """meeting-capture's agent verbs (`install|restart --json`); None when
    this meeting-capture predates them (0.7: plain `install`)."""
    res = owners.call("meeting-capture", verb, *extra, "--json", schema="meeting-capture.agent/", exe=mc,
                      timeout=300)
    if res["status"] == "old":
        return None
    if res["status"] != "ok":
        log(f"  ✗ meeting-capture {verb}: {res['error']}")
        return False
    d = res["data"]
    if not d.get("ok"):
        log(f"  ✗ meeting-capture {verb}: {(d.get('error') or {}).get('message')}")
        return False
    return True


def _install_recorder(mc: str, log) -> bool:
    ok = _agent(mc, "install", log)
    if ok is None:                                  # meeting-capture 0.7
        res = _run([mc, "install"])
        if res.returncode != 0:
            log("  ✗ meeting-capture install failed:\n" + (res.stderr or res.stdout)[-800:])
            return False
        _launchctl("enable", f"gui/{_uid()}/{MC_LABEL}")
        ok = True
    if ok:
        mcconfig.clear_cache()
        log("  ✓ capture daemon running")
    return ok


def _permissions(mc: str, log, todo: list, done: list) -> None:
    """The recorder's permissions, as meeting-capture asks for them and words
    their fixes (each row's per-channel hint; ≥ 0.8). Every row the recorder
    needs (`required`) that macOS can still ask about (`can_request`) is
    requested in meeting-capture's order: Screen & System Audio Recording or
    System Audio Recording Only (whichever its capture backend needs), then
    the microphone. Only undecided rows are asked ("unknown" = sysaudio can
    ask but can't read the state first). meeting-capture 0.7: the sysaudio
    steps, by hand."""
    first = owners.call("meeting-capture", "check", "--json", schema="meeting-capture.permissions/", exe=mc,
                        timeout=60)
    if first["status"] == "old":
        _legacy_permission_steps(log, todo, done)
        return
    if first["status"] != "ok" or not first["data"].get("ok"):
        err = first.get("error") or ((first.get("data") or {}).get("error") or {}).get("message")
        log(f"  ✗ couldn't ask meeting-capture for the permissions: {err}")
        todo.append("Check the recorder's permissions: meeting-capture check")
        return
    doc = first["data"]
    order = [r.get("id") for r in doc.get("permissions") or [] if r.get("id")]
    for perm in order:
        row = next((r for r in doc.get("permissions") or [] if r.get("id") == perm), None)
        if (row and row.get("required") and row.get("can_request")
                and row.get("status") in ("not_determined", "unknown") and _interactive()):
            asked = owners.call("meeting-capture", "check", "--json", "--request", perm,
                                schema="meeting-capture.permissions/", exe=mc, timeout=300)
            if asked["status"] == "ok" and asked["data"].get("ok"):
                doc = asked["data"]
    missing = []
    for row in doc.get("permissions") or []:
        from .ownerstate import PERMISSION_TITLES
        title = PERMISSION_TITLES.get(row["id"], row["id"])
        if row.get("status") == "granted":
            log(f"  ✓ {title}")
        elif row.get("required"):
            log(f"  ✗ {title}: {row.get('status')}" + (f" — {row['hint']}" if row.get("hint") else ""))
            missing.append((title, row))
    if not missing:
        done.append("Permissions")
        return
    if _interactive():
        for title, row in missing:
            if row.get("settings_url"):
                _run(["open", row["settings_url"]])
        input("\n  Press Enter when they are allowed… ")
        again = owners.call("meeting-capture", "check", "--json", schema="meeting-capture.permissions/", exe=mc,
                            timeout=60)
        rows = ((again.get("data") or {}).get("permissions") or []) if again["status"] == "ok" else []
        still = [r for r in rows if r.get("required") and r.get("status") != "granted"]
        if not still and rows:
            done.append("Permissions")
            return
    for title, row in missing:
        todo.append(f"Allow {title}: {row.get('hint') or 'System Settings › Privacy & Security'}")


def _legacy_permission_steps(log, todo: list, done: list) -> None:
    """meeting-capture 0.7 (no `check --json`): the manual sysaudio steps."""
    sysaudio = mcconfig.agent().get("sysaudio") or ""
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
        done.append("Screen & System Audio Recording (granted by you; confirmed on your first call)")
    else:
        todo.append(f"Grant Screen & System Audio Recording to {sysaudio or 'sysaudio'}")


def _can_transcribe(mc: str) -> bool | None:
    """Will anything transcribe what the recorder records? On-device (now or
    once its model is downloaded) or a Gemini key Google didn't reject —
    from meeting-capture's own answer. None: it couldn't say."""
    view = stt.fresh(mc)
    if view["status"] == "old":
        return _key_file_has_key() and KEY_VERDICT["key"] != "rejected"
    d = view.get("data")
    if not d:
        return None
    a = d.get("apple") or {}
    if a.get("usable") or a.get("installable"):
        return True
    return bool(d.get("gemini_key")) and KEY_VERDICT["key"] != "rejected"


def _menu_bar(log, todo: list, me: str) -> None:
    if me == "brew":
        brew = shutil.which("brew") or next((str(Path(p) / "bin" / "brew") for p in owners.BREW_PREFIXES
                                             if (Path(p) / "bin" / "brew").exists()), None)
        if brew and _run([brew, "list", "--versions", "contorch"]).returncode == 0:
            res = _run([brew, "services", "restart", "contorch/tap/contorch"], timeout=120)
            log("  ✓ ○ is in your menu bar (starts at login)" if res.returncode == 0
                else "  ! could not start it: brew services start contorch/tap/contorch")
            if res.returncode != 0:
                todo.append("Start the menu bar: brew services start contorch/tap/contorch")
            return
    if me == "app":
        from . import loginitem
        res = loginitem.register()       # SMAppService.mainApp; only from a copy in Applications
        if res["ok"]:
            log("  ✓ Contorch opens at login (System Settings › General › Login Items)")
        elif res["status"] == "requires_approval":
            log("  ! Contorch is turned off in System Settings › General › Login Items")
            todo.append("Turn Contorch on in System Settings › General › Login Items (to start it at login)")
        else:
            msg = (res.get("error") or {}).get("message") or res["status"]
            log(f"  ! Open at Login wasn't turned on: {msg}")
            todo.append(f"Turn on Open at Login from the Contorch menu ({msg})")
        try:
            from .notify import request_authorization
            request_authorization()            # asked once, here
        except ImportError:
            pass
        return
    log("  · not installed via Homebrew — start the menu bar with: pipeline-monitor &")


def setup(log=print, record: str | None = None, linein: str | None = None, cli: str | None = None,
          embeddings: str | None = None) -> bool:
    """Everything scriptable, in order, then an honest list of what is left.

    Resumable and idempotent: every step asks the owner what is there before
    changing it, so a closed Terminal is recovered by running it again. The
    channel guard is asked first; the marker is claimed at the end."""
    todo: list[str] = []
    done: list[str] = []
    KEY_VERDICT["key"] = None
    me = owners.channel()

    def step(title: str) -> None:
        log(f"\n▶ {title}")

    step("Checking this Mac")
    guard = chan.check(me)
    if not guard["ok"]:
        log(f"  ✗ {guard['message']}")
        return False
    warn = owners.channel_warning()
    if warn:
        log(f"  ! {warn}")
    if not owners.locate("contorch-memory") or not owners.locate("contorch-mcp"):
        log("  ✗ Contorch's memory (context-orchestrator) isn't installed")
        log("    Install it with: brew install contorch/tap/contorch")
        return False
    mc = owners.locate("meeting-capture")
    log("  ✓ memory (context-orchestrator)")
    log(f"  {'✓' if mc else '·'} meeting recorder (meeting-capture)" + ("" if mc else " — not installed"))
    if not shutil.which("claude"):
        log("  ! Claude Code not found — meetings will be kept but not connected to Claude yet")

    step("What should Contorch do on this Mac?")
    want = _choose_modules(log, me, mc, record, linein, cli)

    before = modules.wanted() or modules.infer_wanted(modules.observe(me)) or {}
    stt_cmd = None
    if want["recorder"] and mc:
        step("Transcription")
        stt_cmd = _choose_transcription(log, todo, mc)
        if not before.get("recorder") and _can_transcribe(mc) is False:
            # macOS 15 with no accepted key: nothing would transcribe it, so a
            # new recorder stays off (modules.plan holds it). An existing one
            # keeps recording; its audio waits for a key, as before.
            held = modules.plan(enable=["recorder"], ch=me, can_transcribe=False).get("held") or {}
            log(f"  · the recorder stays off: {modules.NO_ENGINE_TEXT}")
            want["recorder"] = want["linein"] = False
            todo.append(f"Meeting recorder: {modules.NO_ENGINE_TEXT}, then run contorch setup again"
                        + (f" ({held['code']})" if held.get("code") else ""))
            stt_cmd = None

    step("Search embeddings")
    if want["recorder"]:
        _setup_embeddings(log, todo, done, choice=None if embeddings in (None, "imported") else embeddings)
    else:
        _memory_only_embeddings(log, todo, done, embeddings)

    step("Search index")
    if not _search_index(log, todo, done):
        return False
    _selftest(log, todo)

    step("Claude Code connection")
    _claude_code(log, todo, done, me, want["recorder"])

    if want["recorder"] and mc:
        step("Meeting capture")
        if not _install_recorder(mc, log):
            return False
        _apply_stt(stt_cmd, log, todo)
        _report_transcription(mc, log, todo, done)
        if want["linein"]:
            log("  · choose your audio interface and its inputs on the Recording settings page "
                "(menu bar › Recording settings…, or `meeting-capture ui`)")

        step("Permissions (macOS asks you; nothing can grant them for you)")
        _permissions(mc, log, todo, done)
        restarted = _agent(mc, "restart", log)
        if restarted is None:                        # meeting-capture 0.7
            _launchctl("kickstart", "-k", f"gui/{_uid()}/{MC_LABEL}")
    elif mc:
        if before.get("recorder") and mcconfig.installed():
            step("Meeting capture")
            ok = _agent(mc, "uninstall", log)
            if ok is None:
                ok = _run([mc, "uninstall"]).returncode == 0
            log("  ✓ the recorder is off on this Mac" if ok else "  ✗ couldn't remove the recorder agent")
            if not ok:
                todo.append("Turn the recorder off: meeting-capture uninstall")

    if me == "app":
        if want.get("cli"):
            res = modules.cli_install()
            log(f"  ✓ commands linked into {res.get('dir')}" if res.get("ok") else
                f"  ✗ {res.get('error', {}).get('message')}")
        else:
            modules.cli_uninstall()

    step("Menu bar")
    _menu_bar(log, todo, me)

    modules._write(want)
    claimed = chan.claim(me)
    if not claimed["ok"]:
        todo.append(claimed.get("message") or "Contorch is owned by another install here")
    STOPPED_MARKER.unlink(missing_ok=True)

    log("\n" + "─" * 60)
    for d in done:
        log(f"  ✓ {d}")
    for t_ in todo:
        log(f"  ✗ {t_}")
    log("\nNext:")
    log("  1. Restart Claude Code so it loads the contorch MCP server.")
    if want["recorder"]:
        log("  2. Join a short call and say something. Then: meeting-capture last")
        log("  3. In Claude Code ask: “Search contorch for my latest meeting. Cite the transcript.”")
    else:
        log("  2. Import transcripts from your recording Mac: menu bar › Import transcripts…")
    log("\n  Health check any time: contorch doctor · Pause everything: contorch stop")
    return not todo


def _transcription() -> dict | None:
    """meeting-capture's answer (transcription.current), or None when its
    agent isn't installed (meeting-capture's own `config --json`)."""
    from . import mcconfig
    if not mcconfig.installed():
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
    p = sub.add_parser("doctor", help="health-check every component")
    p.add_argument("--json", action="store_true",
                   help="one JSON document: versions, channel, modules and the owners' own JSON (redacted)")
    p.add_argument("--bundle", action="store_true", help="with --json: also the last log lines (redacted)")
    p.set_defaults(func=_cmd_doctor)


def _cmd_doctor(args) -> int:
    if not args.json:
        return doctor()
    from . import diagnostics, jsonout
    with jsonout.reserved_stdout() as out:
        doc = diagnostics.bundle()
        if not args.bundle:
            doc.pop("logs", None)
        jsonout.emit(doc, out)
    return 0


def doctor() -> int:
    """Everything `contorch status` shows, then each owner's own answers:
    the permission rows (`meeting-capture check --json`), Claude Code's
    integration (`contorch-memory claude status --json`, compared field by
    field), the memory end to end (`contorch-memory selftest`) and
    meeting-capture's own doctor."""
    from . import channel as chan, diagnostics, ownerstate
    from . import status as st
    rc = _print_status(transcription=False)
    warn = owners.channel_warning()
    if warn:
        print(f"\n! {warn}")
    snap = st.collect(wait=True)
    if snap.recorder_on():
        rc = _print_transcription_detail() or rc
        print("\n── audio source (meeting-capture)")
        if _print_source(snap, doctor=True):
            rc = 1
        print("\n── permissions (meeting-capture check)")
        perms = snap.permissions
        if perms.get("source") == "log":
            print("  · meeting-capture < 0.8 can't report them; `meeting-capture doctor` below checks sysaudio")
        elif not perms.get("ok"):
            print(f"  ✗ couldn't ask meeting-capture: {perms.get('error')}")
            rc = 1
        elif not perms["problems"]:
            print("  ✓ Screen & System Audio Recording and Microphone are allowed")
        for p in perms.get("problems") or []:
            print(f"  ✗ {p['title']}: {p['status']}" + (f" — {p['hint']}" if p.get("hint") else ""))
            rc = 1
    print("\n── memory (context-orchestrator)")
    m = snap.memory
    data = m.get("data") or {}
    if m.get("ok"):
        print(f"  ✓ index {data.get('vector_index')}: {data.get('docs')} docs, {data.get('transcripts')} transcripts, "
              f"embeddings {data.get('embeddings')}")
    else:
        print(f"  ✗ {m.get('error') or 'unavailable'}")
        rc = 1
    claude = ownerstate.claude(wait=True)
    if claude["status"] == "ok":
        d = claude["data"]
        for part, title in (("mcp", "MCP server"), ("hook", "auto-context hook"), ("claude_md", "CLAUDE.md block"),
                            ("skill", "transcripts skill")):
            row = d.get(part) or {}
            mark = "✓" if row.get("matches") else ("·" if not row.get("present") else "!")
            print(f"  {mark} {title}" + ("" if row.get("matches") else
                                         (" — not installed" if not row.get("present") else " — points elsewhere")
                                         + " (contorch setup fixes it)"))
        if d.get("blocked_by_managed_settings"):
            print("  ! Claude Code's managed settings block Contorch's hook or MCP server: "
                  + "; ".join(d.get("managed_reasons") or []))
        if not d.get("ok"):
            rc = 1
    elif claude["status"] == "old":
        print("  · context-orchestrator < 0.5 can't report Claude Code's integration — upgrade it")
    else:
        print(f"  ✗ Claude Code integration: {claude.get('error')}")
    sm = diagnostics.smoke()
    print(f"  {'✓' if sm['ok'] else '✗'} end to end: {sm['summary']}")
    if not sm["ok"] and (sm.get("error") or {}).get("code") not in ("offline", "proxy"):
        rc = 1
    for a in chan.attention():
        print(f"  ! {a['code']}: {a.get('message') or ''}")
    mc = owners.locate("meeting-capture")
    if snap.recorder_on() and mc:
        print("\n── meeting-capture doctor")
        res = subprocess.run([mc, "doctor"])
        rc = rc or res.returncode
    return rc


# ------------------------------------------------------------------ CLI

def _cli_status(sub) -> None:
    sub.add_parser("status", help="what this Mac has (modules), whether it is recording, the memory, and how "
                                  "meetings are transcribed").set_defaults(func=lambda args: _print_status())


HEADLINES = {"needs_setup": "Contorch isn't set up on this Mac yet — run `contorch setup`",
             "memory_only": "Memory only — this Mac doesn't record",
             "recording": "● Recording a meeting",
             "recording_unknown": "? Can't tell whether a meeting is being recorded",
             "idle": "○ Idle — ready to record"}


def _print_source(snap, doctor: bool = False) -> bool:
    """`source  line-in — UMC404HD 192k (Me in 1 · Them in 2)` / `⚠ UMC404HD
    192k not connected — not recording (since 10:37)`, as meeting-capture
    reports it (pipeline_monitor.source). Returns whether there is a problem."""
    from . import source as src
    s = snap.source or {}
    line = src.text(s, (snap.recording or {}).get("recording"))
    if not line:
        return False
    body = line.removeprefix("Source: ")
    if src.warn(s, (snap.recording or {}).get("recording")):
        since = src.problem_since(s)
        body = f"⚠ {body}" + (f" (since {since})" if since else "")
    if doctor:
        print(f"  {'✗' if s.get('problem') else '✓'} source: {body.removeprefix('⚠ ')}")
        for d in src.details(s):
            print(f"    {d.strip()}")
    else:
        print(f"  {'source':<24} {body}")
    return bool(s.get("problem"))


def _print_status(transcription: bool = True) -> int:
    """The headline, the modules, what runs, the memory and the
    transcription engine — from the owners' answers (status.collect)."""
    from . import status as st
    snap = st.collect(wait=True)
    head = snap.headline()
    print(HEADLINES[head] + (f" ({snap.recording.get('reason')})" if head == "recording_unknown" else ""))
    if is_stopped():
        print("contorch is STOPPED (run `contorch resume` to start it again)")
    print()
    for r in snap.modules.get("modules") or []:
        line = f"  {r['title']:<28} {r['state']}"
        if r.get("no_engine"):
            line += f" — {r['no_engine']}"
        elif r.get("add"):
            line += f"   ({r['add']['command']})"
        print(line)
    print()
    if snap.recorder_on():
        rec = snap.recording
        if rec.get("pid") and rec.get("reason") != "daemon_not_running":
            print(f"  {'recorder':<24} running (pid {rec['pid']})")
        for row in status():
            if row["component"] == "meeting-capture" and rec.get("source") != "owner":
                state = (f"running (pid {row['pid']})" if row["pid"] else
                         "stopped" if row["disabled"] else "not running")
                print(f"  {'recorder':<24} {state}")
        for p in snap.permissions.get("problems") or []:
            print(f"  {'':<24} ⚠ {p['title']}: {p['status']}" + (f" — {p['hint']}" if p.get("hint") else ""))
        _print_source(snap)
    else:
        print(f"  {'background':<24} nothing runs")
    for row in status():                       # retired agents an older install left behind
        if row["component"] != "meeting-capture":
            state = f"running (pid {row['pid']})" if row["pid"] else "not running"
            print(f"  {row['desc']:<24} {state}   (retired: `contorch setup` removes it)")
    m = snap.memory
    data = m.get("data") or {}
    if m.get("ok"):
        idx = "keyword search only" if data.get("vector_index") == "none" else \
            f"{data.get('docs')} docs ({data.get('vector_index')}, embeddings {data.get('embeddings')})"
        print(f"  {'memory':<24} {idx}; {data.get('transcripts')} transcripts")
    else:
        print(f"  {'memory':<24} ✗ {m.get('error') or 'unavailable'}")
    t = _transcription() if transcription and snap.recorder_on() else None
    if t is not None:
        d = t["data"]
        print(f"\n  {'transcription':<24} {t['label']}"
              + (f"  [setting: {d['choice']}, locale {d['locale']}]" if d else ""))
        if t["privacy"]:
            print(f"  {'':<24} audio: {t['privacy']}" + (f" ({t['privacy_fix']})" if t["privacy_fix"] else ""))
        if t["note"]:
            print(f"  {'':<24} ! {t['note']}")
    nothing = head == "needs_setup" and not owners.locate("contorch-memory") and not owners.locate("meeting-capture")
    return 1 if nothing else 0


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
