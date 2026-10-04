"""The other Contorch packages, as pipeline-monitor finds and asks them.

pipeline-monitor owns the menu bar, `contorch`, the module registry and the
channel rule. Everything else has another owner, and pm only READS their JSON
and calls their verbs (SPEC-v2 §2.3):

    meeting-capture        the recorder: status/config/check/where --json,
                           start|stop|restart|install|uninstall|heal --json,
                           skill install|uninstall --json, stt --json
    context-orchestrator   memory: contorch-memory status|selftest|where --json,
                           backup|restore|index migrate --json,
                           claude install|uninstall|status --json,
                           contorch-transcripts import --json

This module is the only place pm locates their executables (locate()) and the
one way it runs a JSON verb (call()).

Channel. Every process learns its install channel from $CONTORCH_CHANNEL only:
`app` (Contorch.app's stubs), `brew` (the formula wrappers), anything else or
unset = `dev`. No path guessing decides anything; path_hint() only feeds a
doctor warning when the environment and the interpreter's location disagree.

Older owners. A verb an owner doesn't have yet is an argparse usage error
(exit 2), reported as status "old"; callers fall back (meeting-capture 0.7 has
only `stt --json`) and never claim more than they know.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

CHANNELS = ("app", "brew", "dev")
HOME = Path.home()

# Which formula ships each command (Homebrew: <prefix>/opt/<formula>/bin/<cmd>,
# stable across upgrades and `brew unlink`).
FORMULA = {
    "meeting-capture": "meeting-capture",
    "sysaudio": "meeting-capture",
    "contorch-mcp": "context-orchestrator",
    "contorch-memory": "context-orchestrator",
    "contorch-transcripts": "context-orchestrator",
    "contorch-hook": "context-orchestrator",
    "context-orchestrator-chroma": "context-orchestrator",
    "contorch": "contorch",
}
# Per-user venvs the formula wrappers build (and source installs use).
USER_VENVS = {
    "meeting-capture": Path(os.environ.get("MEETING_CAPTURE_VENV") or HOME / ".meeting-capture" / "venv"),
    "context-orchestrator": Path(os.environ.get("CONTEXT_ORCHESTRATOR_VENV")
                                 or HOME / ".context-orchestrator" / "venv"),
    "contorch": Path(os.environ.get("CONTORCH_VENV") or HOME / ".contorch" / "venv"),
}
# A dev venv: the commands installed beside this interpreter.
SELF_BIN = Path(sys.executable).parent
BREW_PREFIXES = tuple(dict.fromkeys(p for p in (os.environ.get("HOMEBREW_PREFIX"), "/opt/homebrew", "/usr/local")
                                    if p))
TIMEOUT_S = 60.0


# ------------------------------------------------------------------ where am I

def channel() -> str:
    """app | brew | dev, from $CONTORCH_CHANNEL only (unset or unknown = dev)."""
    v = (os.environ.get("CONTORCH_CHANNEL") or "").strip()
    return v if v in CHANNELS else "dev"


def bundle_root(executable: str | None = None) -> Path | None:
    """/X/Contorch.app when this interpreter runs from inside an app bundle
    (Contents/MacOS/contorch-python), else None. A location, not a channel."""
    parts = Path(os.path.abspath(executable or sys.executable)).parts
    for i, part in enumerate(parts):
        if part.endswith(".app") and i + 1 < len(parts) and parts[i + 1] == "Contents":
            return Path(*parts[: i + 1])
    return None


def brew_prefix() -> str:
    """The Homebrew prefix that has an opt/ tree (Apple silicon first)."""
    for p in BREW_PREFIXES:
        if (Path(p) / "opt").is_dir():
            return p
    return BREW_PREFIXES[0] if BREW_PREFIXES else "/opt/homebrew"


def path_hint() -> str | None:
    """Where this interpreter seems to come from, for a doctor WARNING only
    (never a decision): "app" inside a bundle, "brew" in a formula venv, else
    None."""
    if bundle_root():
        return "app"
    exe = os.path.abspath(sys.executable)
    if any(exe.startswith(str(v) + os.sep) for v in USER_VENVS.values()) or "/Cellar/" in exe:
        return "brew"
    return None


def channel_warning() -> str | None:
    """A doctor line when $CONTORCH_CHANNEL disagrees with where this runs."""
    hint, ch = path_hint(), channel()
    if hint and hint != ch:
        return (f"$CONTORCH_CHANNEL says {ch!r} but this contorch runs from a {hint} install "
                f"({sys.executable}); the {hint} launcher should export CONTORCH_CHANNEL={hint}")
    return None


# ------------------------------------------------------------------ locate

def _which(name: str) -> str | None:
    return shutil.which(name)


def _exe(p: str | os.PathLike | None) -> str | None:
    return str(p) if p and os.path.isfile(p) and os.access(p, os.X_OK) else None


def locate(name: str) -> str | None:
    """The one way pm finds a sibling executable.

    app channel: only the bundle's Contents/Resources/bin (the stubs put it
    on PATH too). Otherwise: Homebrew's opt/ path (launchd gives the menu bar
    no shell PATH), then PATH, then beside this interpreter (a dev venv),
    then the owner's per-user venv (a source install)."""
    if channel() == "app":
        root = bundle_root()
        return _exe(root / "Contents" / "Resources" / "bin" / name) if root else _which(name)
    formula = FORMULA.get(name)
    cands: list[Any] = []
    if formula:
        cands += [Path(p) / "opt" / formula / "bin" / name for p in BREW_PREFIXES]
    cands += [_which(name), SELF_BIN / name]
    if formula in USER_VENVS:
        cands.append(USER_VENVS[formula] / "bin" / name)
    for c in cands:
        found = _exe(c)
        if found:
            return found
    return None


def locate_in(ch: str, name: str, layout: dict | None = None) -> str | None:
    """`name` as installed by channel `ch` — adopt and rollback run one
    channel's copy explicitly (the old channel's `meeting-capture stop`, the
    target's `contorch-memory`). `layout` is a marker's (bundle_root,
    brew_prefix). This install's own channel is plain locate()."""
    layout = layout or {}
    if ch == channel():
        return locate(name)
    if ch == "app":
        root = layout.get("bundle_root")
        return _exe(Path(root) / "Contents" / "Resources" / "bin" / name) if root else None
    if ch == "brew":
        formula = FORMULA.get(name)
        prefixes = [layout.get("brew_prefix")] if layout.get("brew_prefix") else list(BREW_PREFIXES)
        for p in prefixes:
            found = _exe(Path(p) / "opt" / (formula or name) / "bin" / name) if formula else None
            if found:
                return found
        return None
    # dev: a source checkout's own venv, or its per-user venv
    formula = FORMULA.get(name)
    for c in (_which(name), USER_VENVS[formula] / "bin" / name if formula in USER_VENVS else None):
        found = _exe(c)
        if found and not is_brew_wrapper(found) and bundle_root(found) is None:
            return found
    return None


def is_brew_wrapper(path: str) -> bool:
    """A Homebrew bin/ or opt/ wrapper (its venv is rebuilt on upgrade)."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return "/Cellar/" in real


def background(name: str) -> tuple[str | None, str | None]:
    """(what a background read runs, why not). A read from the menu bar or
    `contorch status` must never run a Homebrew wrapper: after a `brew
    upgrade` the wrapper deletes and rebuilds the venv the daemons run from.
    So a wrapper is replaced by its venv's own executable; a venv that isn't
    built yet means no read."""
    path = locate(name)
    if path is None:
        return None, "missing"
    if not is_brew_wrapper(path):
        return path, None
    venv = USER_VENVS.get(FORMULA.get(name, ""))
    found = _exe(venv / "bin" / name) if venv else None
    return (found, None) if found else (None, "not_built")


# ------------------------------------------------------------------ call a verb

def _run(argv: list[str], timeout: float, env: dict | None) -> tuple[int, str, str]:
    """Run argv in its own process group; on timeout kill the whole group."""
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace", env=env, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        raise
    return p.returncode, out, err


def parse_json(out: str) -> Any:
    """One JSON document, or the last line of JSON Lines (`{"event": "result"}`)."""
    text = (out or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise ValueError("no JSON document on stdout")


def call(name: str, *args: str, schema: str | None = None, timeout: float = TIMEOUT_S,
         foreground: bool = True, env: dict | None = None, exe: str | None = None) -> dict[str, Any]:
    """Run `<name> <args…>` and read its JSON answer.

    -> {"status": ok|old|error|missing|not_built, "data", "rc", "error", "argv"}
      ok        a JSON document (whatever the exit code: owners exit 1 or 3
                with a document that says why)
      old       usage error (exit 2): the owner predates this verb
      error     it failed, timed out or printed no JSON / the wrong schema
      missing   not installed; not_built: a brew venv not built yet (background)
    foreground=False runs the venv's own executable instead of a Homebrew
    wrapper (background()). `exe` pins the executable (adopt runs one
    channel's copy explicitly). `env` is added to this process's environment."""
    if exe is None:
        if foreground:
            exe, why = locate(name), "missing"
        else:
            exe, why = background(name)
        if exe is None:
            return {"status": why or "missing", "data": None, "rc": None, "argv": [name, *args],
                    "error": f"{name} is not installed" if why == "missing"
                    else f"{name} isn't set up yet (run `{name}` once, or `contorch setup`)"}
    argv = [exe, *args]
    run_env = {**os.environ, **env} if env else None
    try:
        rc, out, err = _run(argv, timeout, run_env)
    except subprocess.TimeoutExpired:
        return {"status": "error", "data": None, "rc": None, "argv": argv,
                "error": f"`{name} {' '.join(args)}` timed out after {timeout:.0f}s"}
    except OSError as e:
        return {"status": "error", "data": None, "rc": None, "argv": argv,
                "error": f"can't run {exe}: {e.strerror or e}"}
    if rc == 2:
        return {"status": "old", "data": None, "rc": rc, "argv": argv,
                "error": f"this {name} has no `{' '.join(a for a in args if not a.startswith('-'))}` "
                         "(upgrade it)", "stderr": err[-400:]}
    try:
        data = parse_json(out)
    except ValueError:
        tail = (err or out or "").strip().splitlines()
        return {"status": "error", "data": None, "rc": rc, "argv": argv,
                "error": f"`{name} {' '.join(args)}` failed (exit {rc}): "
                         f"{tail[-1][:200] if tail else 'no output'}"}
    if not isinstance(data, dict) or (schema and not str(data.get("schema", "")).startswith(schema)):
        return {"status": "error", "data": None, "rc": rc, "argv": argv,
                "error": f"`{name} {' '.join(args)}` answered with an unexpected document "
                         f"(schema {data.get('schema') if isinstance(data, dict) else None!r})"}
    return {"status": "ok", "data": data, "rc": rc, "argv": argv, "error": None}


def error_of(res: dict) -> dict:
    """{code, message} for a call() result that didn't succeed."""
    d = res.get("data") or {}
    if isinstance(d.get("error"), dict):
        return {"code": d["error"].get("code") or "failed", "message": d["error"].get("message") or ""}
    return {"code": {"old": "owner_too_old", "missing": "owner_missing",
                     "not_built": "owner_not_built"}.get(res["status"], "owner_failed"),
            "message": res.get("error") or ""}
