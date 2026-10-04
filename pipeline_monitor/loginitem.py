"""Open at Login for Contorch.app: SMAppService.mainApp (SPEC-v2 §4.1).

Inside the app only. Homebrew's menu bar starts at login through
`brew services` (sh.brew.contorch); a source checkout through
install.sh --autostart. Elsewhere available() is False and nothing here runs.

    status()      enabled | requires_approval | not_registered | not_found |
                  unavailable — read at most every CACHE_S seconds (the menu
                  repaints every few seconds; force=True when the menu opens
                  or after a change). not_found means off.
    is_on()       enabled (requires_approval = the user turned Contorch off
                  in System Settings › General › Login Items: off, with that
                  pane as the fix)
    register()    only from a copy in /Applications or ~/Applications
                  (lifecycle.location() == "ok"): a login item for a copy on
                  the DMG or a translocated copy would point at nothing later.
    unregister()

It works from the menu bar process and from the bundle's contorch-python
(`contorch setup` in Terminal), both measured on a labtest build: inside
contorch-python NSBundle.mainBundle() carries the stub's embedded identity,
but its bundle path is the app, which is what SMAppService registers.
PyObjC's ServiceManagement comes with the `app` extra; the bundle has it.
"""
from __future__ import annotations

import time
from typing import Any

from . import owners

CACHE_S = 10.0
STATUS = {0: "not_registered", 1: "enabled", 2: "requires_approval", 3: "not_found"}
_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _sm():
    import ServiceManagement as SM          # pyobjc-framework-ServiceManagement (the app extra)
    return SM


def available() -> bool:
    if owners.channel() != "app" or owners.bundle_root() is None:
        return False
    try:
        _sm()
        return True
    except ImportError:
        return False


def _service():
    return _sm().SMAppService.mainAppService()


def clear_cache() -> None:
    _cache.update(at=0.0, value=None)


def status(force: bool = False) -> str:
    if not available():
        return "unavailable"
    now = time.monotonic()
    if not force and _cache["value"] is not None and now - _cache["at"] < CACHE_S:
        return _cache["value"]
    try:
        raw = int(_service().status())
        value = STATUS.get(raw, f"unknown_{raw}")
    except Exception:
        value = "unavailable"
    _cache.update(at=now, value=value)
    return value


def is_on(force: bool = False) -> bool:
    return status(force) == "enabled"


def _err(e) -> dict | None:
    if e is None:
        return None
    return {"code": "sm_error", "domain": str(e.domain()), "number": int(e.code()),
            "message": str(e.localizedDescription())}


def _change(verb: str) -> dict:
    if not available():
        return {"ok": False, "status": "unavailable",
                "error": {"code": "not_app", "message": "Open at Login is Contorch.app's (Homebrew: brew services)"}}
    if verb == "register":
        from . import lifecycle
        loc = lifecycle.location()
        if loc != "ok":
            return {"ok": False, "status": status(force=True),
                    "error": {"code": "location", "location": loc,
                              "message": "Move Contorch to Applications first"}}
    svc = _service()
    fn = svc.registerAndReturnError_ if verb == "register" else svc.unregisterAndReturnError_
    try:
        ok, err = fn(None)
    except Exception as e:  # noqa: BLE001
        ok, err = False, None
        clear_cache()
        return {"ok": False, "status": status(force=True), "error": {"code": "sm_error", "message": repr(e)}}
    clear_cache()
    st = status(force=True)
    if verb == "unregister" and st in ("not_registered", "not_found"):
        ok = True                         # already off is off
    return {"ok": bool(ok), "status": st, "error": None if ok else _err(err)}


def register() -> dict:
    return _change("register")


def unregister() -> dict:
    return _change("unregister")


def set_on(on: bool) -> dict:
    return register() if on else unregister()


def open_settings() -> bool:
    """System Settings › General › Login Items (where requires_approval is fixed)."""
    try:
        _sm().SMAppService.openSystemSettingsLoginItems()
        return True
    except Exception:
        return False
