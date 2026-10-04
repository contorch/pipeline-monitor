"""Set up Contorch… from Contorch.app: `contorch setup` in Terminal.

Setup is the interactive `contorch setup` (questions, progress, the
permission prompts), so the app opens it in Terminal instead of drawing it.
The bundle carries no .command file: this module WRITES one at runtime,

    ~/Library/Application Support/Contorch/Set Up Contorch.command
        #!/bin/sh
        exec "<bundle>/Contents/Resources/bin/contorch" setup

(mode 0755) and opens it with `open -a Terminal`. A file the app writes
itself carries no quarantine attribute, so Gatekeeper has nothing to check
when Terminal runs it (SPEC-v2 §2.1; the at-the-screen measurement is M0.4,
carried into the M5 lab). Should a quarantine attribute ever appear on it
anyway, write() removes it: the file is ours.

Rewritten on every launch, so it always points at the app that runs now
(after a move or an update) — never at a copy that is gone.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path

from . import owners

NAME = "Set Up Contorch.command"
QUARANTINE = "com.apple.quarantine"


def command_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "Contorch" / NAME


def _sh_quote(s: str) -> str:
    """A double-quoted /bin/sh word: \\ " $ ` escaped."""
    out = s
    for ch in ("\\", '"', "$", "`"):
        out = out.replace(ch, "\\" + ch)
    return f'"{out}"'


def content(contorch: str) -> str:
    return f"#!/bin/sh\nexec {_sh_quote(contorch)} setup\n"


def contorch_bin(bundle: Path | None = None) -> Path | None:
    root = bundle or owners.bundle_root()
    if root is None:
        return None
    return Path(root) / "Contents" / "Resources" / "bin" / "contorch"


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def has_quarantine(path: Path) -> bool:
    libc = _libc()
    libc.getxattr.restype = ctypes.c_ssize_t
    n = libc.getxattr(os.fsencode(str(path)), QUARANTINE.encode(), None, ctypes.c_size_t(0),
                      ctypes.c_uint32(0), ctypes.c_int(0))
    return n >= 0


def _drop_quarantine(path: Path) -> None:
    _libc().removexattr(os.fsencode(str(path)), QUARANTINE.encode(), ctypes.c_int(0))


def write(bundle: Path | None = None) -> Path:
    """Write the .command for this app (0755, no quarantine) and return it."""
    exe = contorch_bin(bundle)
    if exe is None:
        raise RuntimeError("Set Up Contorch… opens setup from Contorch.app only (elsewhere: run `contorch setup`)")
    p = command_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(content(str(exe)))
    os.chmod(tmp, 0o755)
    os.replace(tmp, p)
    if has_quarantine(p):
        _drop_quarantine(p)
    return p


def launch(bundle: Path | None = None) -> dict:
    """Write the .command and open it in Terminal."""
    try:
        p = write(bundle)
    except (OSError, RuntimeError) as e:
        return {"ok": False, "error": str(e)}
    res = subprocess.run(["open", "-a", "Terminal", str(p)], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        return {"ok": False, "path": str(p), "error": (res.stderr or res.stdout).strip()[-200:]}
    return {"ok": True, "path": str(p)}
