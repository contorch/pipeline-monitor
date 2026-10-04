"""`contorch smoke` and the diagnostics bundle (`contorch doctor --json --bundle`).

smoke   context-orchestrator tests its own memory end to end
        (`contorch-memory selftest --json`: write a marker, embed, search,
        delete). pipeline-monitor used to run its own copy through a source
        checkout's venv (smoketest.py); that needed a checkout on disk and
        re-implemented CO's search.

bundle  what a bug report needs, in one JSON document: versions, the
        channel and modules, and the owners' own JSON (status, check, config,
        where, stt, contorch-memory status, claude status), plus the last
        lines of the logs — REDACTED: no API keys, no e-mail addresses, no
        transcript text, home folder shown as ~. The menu's Diagnostics ›
        Copy diagnostics puts it on the clipboard (Phase 1 M4).
"""
from __future__ import annotations

import os
import platform
import re
import sys
from pathlib import Path
from typing import Any

from . import owners

SELFTEST_SCHEMA = "contorch-memory.selftest/"


def smoke(timeout: float = 120) -> dict[str, Any]:
    """{"schema", "ok", "stage", "ms", "error"?, "summary"} from
    `contorch-memory selftest --json`."""
    res = owners.call("contorch-memory", "selftest", "--json", schema=SELFTEST_SCHEMA, timeout=timeout)
    doc: dict[str, Any] = {"schema": "contorch.smoke/1", "ok": False}
    if res["status"] == "old":
        doc.update(error={"code": "owner_too_old",
                          "message": "context-orchestrator < 0.5 has no selftest — upgrade it"})
    elif res["status"] != "ok":
        doc.update(error={"code": "owner_" + res["status"], "message": res.get("error") or ""})
    else:
        d = res["data"]
        doc.update(ok=bool(d.get("ok")), stage=d.get("stage"), ms=d.get("ms"))
        if d.get("error"):
            doc["error"] = d["error"]
    if doc["ok"]:
        doc["summary"] = f"write → search → delete in {doc.get('ms')} ms"
    else:
        e = doc.get("error") or {}
        hint = {"offline": " (no network: search uses keywords until it is back)",
                "proxy": " (a proxy blocks the model download: keyword search until it can)",
                "key": " (the Gemini key was rejected)"}.get(e.get("code"), "")
        doc["summary"] = f"failed at {doc.get('stage') or '?'}: {e.get('code')}: {e.get('message')}{hint}"
    return doc


# ------------------------------------------------------------------ the bundle

_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{20,}|sk-[0-9A-Za-z_\-]{20,}|(?i:bearer)\s+[0-9A-Za-z._\-]{16,}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_QUOTED = re.compile(r'"[^"\n]{24,}"|\'[^\'\n]{24,}\'')
_TEXTY = re.compile(r"\*\*(?:me|them):?\*\*|\btranscript text\b|\btext=|\bsaid:", re.I)


def redact(text: str) -> str:
    """Log text safe to paste into an issue: keys, e-mails, long quoted
    strings (spoken words) and transcript-like lines removed."""
    home = str(Path.home())
    out = []
    for line in (text or "").splitlines():
        if _TEXTY.search(line):
            out.append("[line removed: may contain transcript text]")
            continue
        line = _KEY.sub("[key]", line)
        line = _EMAIL.sub("[email]", line)
        line = _QUOTED.sub('"[…]"', line)
        out.append(line.replace(home, "~")[:300])
    return "\n".join(out)


def _tail(path: Path, lines: int = 60) -> str | None:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - 65536))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    return redact("\n".join(data.splitlines()[-lines:]))


def _scrub(obj: Any) -> Any:
    """Owner JSON with string values redacted (paths keep their shape)."""
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


OWNER_DOCS = (
    ("meeting_capture.status", "meeting-capture", ("status", "--json"), "meeting-capture.status/"),
    ("meeting_capture.check", "meeting-capture", ("check", "--json"), "meeting-capture.permissions/"),
    ("meeting_capture.config", "meeting-capture", ("config", "--json"), "meeting-capture.config/"),
    ("meeting_capture.where", "meeting-capture", ("where", "--json"), "meeting-capture.where/"),
    ("meeting_capture.stt", "meeting-capture", ("stt", "--json"), None),
    ("contorch_memory.status", "contorch-memory", ("status", "--json"), "contorch-memory.status/"),
    ("contorch_memory.claude", "contorch-memory", ("claude", "status", "--json"), "contorch-memory.claude/"),
    ("contorch_memory.where", "contorch-memory", ("where", "--json"), "contorch-memory.where/"),
)


def bundle() -> dict[str, Any]:
    from . import __version__, channel, modules
    docs: dict[str, Any] = {}
    for key, name, args, schema in OWNER_DOCS:
        res = owners.call(name, *args, schema=schema, foreground=False, timeout=60)
        docs[key] = _scrub(res["data"]) if res["status"] == "ok" else {"status": res["status"],
                                                                        "error": redact(res.get("error") or "")}
    home = Path.home()
    logs = {
        "meeting-capture/daemon.log": _tail(home / ".meeting-capture" / "daemon.log"),
        "contorch/menubar.log": _tail(home / "Library" / "Logs" / "Contorch" / "menubar.log"),
        "pipeline-monitor/stderr.log": _tail(home / "Library" / "Logs" / "pipeline-monitor" / "stderr.log"),
    }
    try:
        mods = modules.status()
    except Exception as e:  # noqa: BLE001
        mods = {"error": f"{type(e).__name__}: {e}"}
    return {"schema": "contorch.diagnostics/1", "ok": True,
            "versions": {"pipeline-monitor": __version__, "python": sys.version.split()[0],
                         "macos": platform.mac_ver()[0], "arch": platform.machine()},
            "channel": _scrub(channel.status_doc()), "channel_warning": owners.channel_warning(),
            "modules": _scrub(mods), "owners": docs, "logs": {k: v for k, v in logs.items() if v is not None},
            "env": {k: os.environ.get(k) for k in ("CONTORCH_CHANNEL", "HOMEBREW_PREFIX") if os.environ.get(k)}}


# ------------------------------------------------------------------ CLI

def add_cli(sub) -> None:
    p = sub.add_parser("smoke", help="test the memory end to end (contorch-memory selftest)")
    p.add_argument("--json", action="store_true", help="one JSON document (contorch.smoke/1)")
    p.set_defaults(func=_cmd_smoke)


def _cmd_smoke(args) -> int:
    from . import jsonout
    doc = smoke()
    if args.json:
        with jsonout.reserved_stdout() as out:
            jsonout.emit(doc, out)
    else:
        print(("✓ " if doc["ok"] else "✗ ") + doc["summary"])
    return 0 if doc["ok"] else 1
