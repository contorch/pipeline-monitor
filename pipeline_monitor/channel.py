"""Which install channel owns Contorch on this Mac, and the guard rule.

    app   Contorch.app (DMG): everything runs from the bundle.
    brew  Homebrew formulas (wrappers in <prefix>/opt/<formula>/bin).
    dev   a source checkout (bootstrap.sh, setup.sh, pip install -e).

The marker ~/.contorch/channel.json (`contorch.channel/1`) names the owner.
This module is its ONLY writer and holds the ONLY copy of the rule. It stores
the rule's answer in the marker, so readers never re-derive it:

    writers           the channels that may change Contorch's surfaces now
    op                {id, kind, by} while an adopt/uninstall runs; then only
                      the children pm launched for it ($CONTORCH_OP == op.id)
                      may write
    blocked_message   what a refused reader prints (exit 3)

Readers — meeting-capture's channel_guard.allowed() and context-orchestrator's
sourced scripts/contorch_channel_guard.sh — only test membership. The shared
fixtures are contract/channel_guard/*.json; tests/test_channel.py checks that
writers_for() produces every one of them.

The rule (writers_for):
    no marker                 everyone may write
    state ok                  [owner]
    state adopting            [adopting_to, owner] + op {kind: adopt, by: adopting_to}
    state uninstalling        [owner] + op {kind: uninstall, by: owner}

A marker whose owner is gone (the app trashed, the formula uninstalled) is
pm's own case: attention() reports `owner_gone` and adopt/uninstall rewrite
the marker. Readers never special-case it.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from . import owners

SCHEMA = "contorch.channel/1"
STATES = ("ok", "adopting", "uninstalling")
WHERE = {"app": "Contorch.app", "brew": "Homebrew (brew install contorch/tap/contorch)",
         "dev": "a source checkout"}


def marker_path() -> Path:
    return Path.home() / ".contorch" / "channel.json"


# ------------------------------------------------------------------ the rule

def writers_for(owner: str, state: str = "ok", adopting_to: str | None = None,
                op_id: str | None = None) -> tuple[list[str], dict | None]:
    """(writers, op) for a marker. The one implementation of the rule."""
    if owner not in owners.CHANNELS:
        raise ValueError(f"unknown owner {owner!r}")
    if state == "ok":
        return [owner], None
    if state == "adopting":
        if adopting_to not in owners.CHANNELS:
            raise ValueError("adopting needs adopting_to")
        writers = [adopting_to] + ([owner] if owner != adopting_to else [])
        return writers, {"id": op_id or new_op_id(), "kind": "adopt", "by": adopting_to}
    if state == "uninstalling":
        return [owner], {"id": op_id or new_op_id(), "kind": "uninstall", "by": owner}
    raise ValueError(f"unknown state {state!r}")


def new_op_id() -> str:
    return "op-" + secrets.token_hex(4)


def blocked_message(owner: str, state: str = "ok", adopting_to: str | None = None) -> str:
    """What a refused reader prints: who manages Contorch here and the one
    command that resolves it."""
    if state == "adopting":
        return (f"Contorch on this Mac is moving from {WHERE[owner]} to {WHERE[adopting_to or owner]}. "
                f"Finish it: contorch adopt --yes (from the {adopting_to} install).")
    if state == "uninstalling":
        return f"Contorch is being uninstalled from this Mac. Finish it: contorch uninstall --yes."
    if owner == "app":
        return ("Contorch on this Mac is managed by Contorch.app. Use the app's menu "
                "(Contorch › Set Up Contorch…), or run `contorch adopt` from the install that should take over.")
    if owner == "brew":
        return ("Contorch on this Mac is managed by Homebrew (brew install contorch/tap/contorch). "
                "Use `contorch setup` from it, or `contorch adopt` from the install that should take over.")
    return ("Contorch on this Mac is managed by a source checkout. Use its setup.sh / `contorch setup`, "
            "or `contorch adopt` from the install that should take over.")


def allowed(marker: dict | None, me: str | None = None, op_env: str | None = None) -> bool:
    """The readers' membership test, for pm's own use (and the fixtures'
    reference): no marker, or me in writers and (no op or the op's token)."""
    if marker is None:
        return True
    me = me if me in owners.CHANNELS else "dev"
    writers = marker.get("writers") if isinstance(marker.get("writers"), list) else []
    op = marker.get("op")
    if me not in writers:
        return False
    if op is not None:
        return isinstance(op, dict) and bool(op.get("id")) and op_env == op.get("id")
    return True


# ------------------------------------------------------------------ the marker

def layout() -> dict:
    """Where this install lives: only the bundle root (+ its id) and the brew
    prefix. Owners report every other path themselves (`where --json`)."""
    root = owners.bundle_root()
    out: dict[str, Any] = {"brew_prefix": owners.brew_prefix()}
    if root:
        out["bundle_root"] = str(root)
        try:
            from .notify import bundle_identifier
            bid = bundle_identifier()
        except Exception:
            bid = None
        if bid:
            out["bundle_id"] = bid
    return out


def read() -> dict | None:
    """The marker, None when there is none, {"unreadable": True} when it
    can't be parsed (readers then refuse; pm reports it)."""
    p = marker_path()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"unreadable": True}
    return d if isinstance(d, dict) else {"unreadable": True}


def build(owner: str, state: str = "ok", *, adopting_to: str | None = None, op_id: str | None = None,
          extra: dict | None = None) -> dict:
    writers, op = writers_for(owner, state, adopting_to, op_id)
    m: dict[str, Any] = {"schema": SCHEMA, "owner": owner, "state": state, "writers": writers,
                         "blocked_message": blocked_message(owner, state, adopting_to),
                         "layout": layout() if state == "ok" else (extra or {}).get("layout") or layout()}
    if op:
        m["op"] = op
    if adopting_to:
        m["adopting_to"] = adopting_to
    for k, v in (extra or {}).items():
        if k not in m and v is not None:
            m[k] = v
    m["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return m


def write(marker: dict) -> dict:
    """Atomic write (tmp + os.replace): readers never see half a file."""
    p = marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return marker


def remove() -> None:
    marker_path().unlink(missing_ok=True)


# ------------------------------------------------------------------ pm's questions

def owner_gone(marker: dict | None) -> bool:
    """The owner install isn't on this Mac any more (app trashed / moved,
    formula uninstalled). pm's case, never a reader's."""
    if not marker or marker.get("unreadable"):
        return False
    lay = marker.get("layout") or {}
    if marker.get("owner") == "app":
        root = lay.get("bundle_root")
        return bool(root) and not Path(root).is_dir()
    if marker.get("owner") == "brew":
        prefix = lay.get("brew_prefix") or owners.brew_prefix()
        return not (Path(prefix) / "opt" / "contorch").exists()
    return False


def check(me: str | None = None) -> dict:
    """May this install (me) change Contorch's surfaces here? The answer
    setup asks before any step: {ok, me, owner, code?, message?}."""
    me = me or owners.channel()
    m = read()
    base = {"me": me, "owner": (m or {}).get("owner")}
    if m is None:
        return {**base, "ok": True}
    if m.get("unreadable"):
        return {**base, "ok": False, "code": "marker_unreadable",
                "message": f"{marker_path()} can't be read; fix or delete it, then run this again."}
    if allowed(m, me, os.environ.get("CONTORCH_OP")):
        return {**base, "ok": True}
    if m.get("state") in ("adopting", "uninstalling"):
        return {**base, "ok": False, "code": "interrupted", "message": m.get("blocked_message") or ""}
    if owner_gone(m):
        return {**base, "ok": False, "code": "owner_gone",
                "message": f"{WHERE.get(m.get('owner'), m.get('owner'))} managed Contorch here but is gone. "
                           f"Take over with `contorch adopt`, or clean up with `contorch uninstall`."}
    return {**base, "ok": False, "code": "channel_conflict",
            "message": m.get("blocked_message") or blocked_message(m.get("owner") or "dev")}


def claim(me: str | None = None) -> dict:
    """Make this install the owner when nobody is, or refresh its own marker
    (the layout moves with the app). Never takes over another owner: that is
    `contorch adopt`. -> check()'s shape, plus the marker."""
    me = me or owners.channel()
    res = check(me)
    if not res["ok"]:
        return res
    m = read()
    if m is not None and m.get("state") != "ok":       # pm's own op child: leave it to the op
        return {**res, "marker": m}
    keep = {k: v for k, v in (m or {}).items() if k in ("adopted_from",)}
    new = build(me, "ok", extra=keep)
    if m and all(m.get(k) == new.get(k) for k in ("owner", "state", "writers", "layout")):
        return {**res, "marker": m, "changed": False}
    write(new)
    return {**res, "owner": me, "marker": new, "changed": True}


def attention(marker: dict | None = None, *, recorder_backend: str | None = None,
              mcp_matches: bool | None = None, mcp_present: bool | None = None) -> list[dict]:
    """Conditions the menu bar and doctor show (pm decides; shells render).
    The owners' facts come in as arguments: `recorder_backend` from
    `meeting-capture config --json` (agent.backend), `mcp_*` from this
    install's `contorch-memory claude status --json`."""
    m = read() if marker is None else marker
    out: list[dict] = []
    if not m:
        return out
    if m.get("unreadable"):
        return [{"code": "marker_unreadable", "path": str(marker_path())}]
    own = m.get("owner")
    if m.get("state") in ("adopting", "uninstalling"):
        out.append({"code": "interrupted", "state": m["state"], "message": m.get("blocked_message")})
    if owner_gone(m):
        out.append({"code": "owner_gone", "owner": own})
    me = owners.channel()
    foreign = []
    if recorder_backend:
        if own == "app" and recorder_backend == "launchctl":
            foreign.append({"surface": "recorder", "is": "legacy plist (brew/dev)"})
        if own in ("brew", "dev") and recorder_backend == "app":
            foreign.append({"surface": "recorder", "is": "Contorch.app's agent"})
    if own == me and mcp_present and mcp_matches is False:
        foreign.append({"surface": "mcp", "is": "another install's contorch-mcp"})
    if foreign:
        out.append({"code": "mixed_channels", "owner": own, "foreign": foreign})
    if own == "app":
        prefix = Path((m.get("layout") or {}).get("brew_prefix") or owners.brew_prefix())
        relinked = [f for f in ("contorch", "meeting-capture", "context-orchestrator")
                    if (prefix / "var" / "homebrew" / "linked" / f).exists()]
        if relinked:
            out.append({"code": "brew_relinked", "formulas": relinked,
                        "message": "Homebrew's Contorch commands are back on PATH beside the app's: "
                                   "brew unlink " + " ".join(relinked)})
    return out


def status_doc() -> dict:
    """`contorch channel --json` (contorch.channel.status/1)."""
    m = read()
    me = owners.channel()
    doc: dict[str, Any] = {"schema": "contorch.channel.status/1", "ok": True, "me": me,
                           "owner": (m or {}).get("owner"), "state": (m or {}).get("state"),
                           "writers": (m or {}).get("writers"), "op": (m or {}).get("op"),
                           "marker": str(marker_path()) if m is not None else None,
                           "may_write": check(me)["ok"], "attention": attention(m)}
    warn = owners.channel_warning()
    if warn:
        doc["warning"] = warn
    return doc


# ------------------------------------------------------------------ CLI

def add_cli(sub) -> None:
    p = sub.add_parser("channel", help="which install owns Contorch on this Mac (and what needs attention)")
    p.add_argument("--json", action="store_true", help="one JSON document (contorch.channel.status/1)")
    p.set_defaults(func=_cmd_channel)


def _cmd_channel(args) -> int:
    from . import jsonout
    if args.json:
        with jsonout.reserved_stdout() as out:
            jsonout.emit(status_doc(), out)
        return 0
    d = status_doc()
    print(f"this install:  {d['me']}")
    if d["owner"] is None:
        print("owner:         nobody yet (contorch setup claims it)")
    else:
        print(f"owner:         {d['owner']} — {WHERE.get(d['owner'], d['owner'])}"
              + ("" if d["state"] == "ok" else f" ({d['state']})"))
        print(f"may change it: {', '.join(d['writers'] or []) or 'nobody'}")
    for a in d["attention"]:
        print(f"! {a['code']}: {a.get('message') or a.get('foreign') or a.get('owner') or ''}")
    if d.get("warning"):
        print(f"! {d['warning']}")
    return 0
