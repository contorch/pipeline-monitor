"""`contorch` — one command for the whole contorch stack.

    contorch setup      configure everything after `brew install contorch/tap/contorch`
    contorch doctor     health-check every component
    contorch status     what is installed, running, stopped
    contorch stop       stop every background daemon, and keep them stopped across login
    contorch resume     start them again, in dependency order, and check they came up

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
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

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


def _have_key() -> bool:
    if os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"):
        return True
    try:
        return KEY_FILE.is_file() and KEY_FILE.read_text().strip() != ""
    except OSError:
        return False


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


def _claude_md_template() -> Path | None:
    brew = shutil.which("brew")
    if brew:
        res = _run([brew, "--prefix", "context-orchestrator"])
        cand = Path(res.stdout.strip()) / "share" / "context-orchestrator" / "claude-md-template.md"
        if res.returncode == 0 and cand.is_file():
            return cand
    return None


def setup(log=print) -> bool:
    """Everything scriptable, in order, then an honest list of what is left."""
    todo: list[str] = []
    done: list[str] = []

    def step(title: str) -> None:
        log(f"\n▶ {title}")

    # 0. What is installed?
    step("Checking components")
    bins = {n: shutil.which(n) for n in ("meeting-capture", "context-orchestrator-chroma",
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

    log("\n  contorch records meeting audio when another app uses your microphone.")
    log("  Audio is sent to Google Gemini for transcription; transcripts and the")
    log("  search index stay on this Mac.")

    # 1. Gemini key
    step("Gemini API key (needed for transcription)")
    if _have_key():
        log(f"  ✓ found ({'environment' if not KEY_FILE.is_file() else KEY_FILE})")
        done.append("Gemini key")
    elif _interactive():
        log(f"  Opening {AI_STUDIO} — create a key, then paste it here.")
        _run(["open", AI_STUDIO])
        key = getpass.getpass("  Gemini API key (input hidden, Enter to skip): ").strip()
        if key:
            _write_key(key)
            log(f"  ✓ saved to {KEY_FILE} (readable only by you)")
            done.append("Gemini key")
        else:
            todo.append(f"Add a Gemini key: write it to {KEY_FILE} (chmod 600), then run `contorch setup` again")
    else:
        log(f"  ✗ no key found, and no terminal to ask on — meeting transcription stays off")
        todo.append(f"Add a Gemini key: write it to {KEY_FILE} (chmod 600), then run `contorch setup` again")

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
        _run([claude, "mcp", "remove", "--scope", "user", MCP_NAME])
        cmd = [claude, "mcp", "add", "--scope", "user"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [MCP_NAME, "--", bins["contorch-mcp"]]
        res = _run(cmd)
        if res.returncode == 0:
            log(f"  ✓ registered `{MCP_NAME}` → {bins['contorch-mcp']}"
                + (f" (kept {', '.join(env)})" if env else ""))
            done.append("Claude Code registration")
        else:
            log("  ✗ claude mcp add failed: " + (res.stderr or res.stdout).strip()[-300:])
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


def doctor() -> int:
    rc = _print_status()
    for name in ("meeting-capture", "transcript-watcher"):
        b = shutil.which(name)
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

def _print_status() -> int:
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
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="contorch", description="Control the whole contorch stack.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="configure everything after installing (safe to re-run)")
    sub.add_parser("doctor", help="health-check every component")
    sub.add_parser("status", help="show every contorch daemon and whether it is running")
    sub.add_parser("stop", help="stop every contorch daemon and keep them stopped across login")
    sub.add_parser("resume", help="start every contorch daemon again, in order")
    args = p.parse_args(argv)
    if args.cmd == "setup":
        return 0 if setup() else 1
    if args.cmd == "doctor":
        return doctor()
    if args.cmd == "status":
        return _print_status()
    if args.cmd == "stop":
        print("Stopping contorch…")
        ok = stop()
        print("\nStopped. Nothing records, indexes, or answers searches until `contorch resume`."
              if ok else "\nStopped with errors (above).")
        return 0 if ok else 1
    if args.cmd == "resume":
        print("Resuming contorch…")
        ok = resume()
        print("\nRunning." if ok else "\nResumed with errors (above). `contorch status` for details.")
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
