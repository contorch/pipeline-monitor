"""Uninstall Contorch from this Mac, in either channel.

    contorch uninstall [--plan] [--yes] [--json] [--remove-data] [--not-recording]

An ordering of OWNER VERBS (SPEC-v2 §4.3), like adopt:

     1  refuse while a meeting is being recorded (or can't be told)
     2  mark ~/.contorch/channel.json `uninstalling` (owner = this install)
     3  `meeting-capture uninstall --json` (it leaves no disabled launchd
        override behind); meeting-capture 0.7: its plain `uninstall`
     4  `meeting-capture skill uninstall --json`
     5  `contorch-memory claude uninstall --channel <me> --json` (MCP entry,
        hook, CLAUDE.md block, transcripts skill — each only if it is ours)
     6  the menu bar's autostart: `brew services stop contorch` (brew); the
        app's login item is the app's (Contorch › Uninstall Contorch…)
     7  the CLI links (app)
     8  remove the marker
     9  app: `tccutil reset All <bundle id>` for the app's own privacy rows

Data stays unless --remove-data, which asks each OWNER to move its own data
to the Trash (`--remove-data` on its uninstall). An owner that doesn't
support it yet leaves its folder in place and says so in the to-do list;
pipeline-monitor never deletes another package's data itself.

Another channel's install can't be uninstalled from here (channel_conflict),
unless it is gone (owner_gone) — then this one cleans up what is left.
"""
from __future__ import annotations

from . import adopt, channel, owners
from .adopt import OpError, _owner, _step


def plan_uninstall(me: str | None = None, remove_data: bool = False, not_recording: bool = False) -> dict:
    me = me or owners.channel()
    m = channel.read()
    if m and m.get("unreadable"):
        return adopt._refuse("marker_unreadable", f"{channel.marker_path()} can't be read; fix or delete it.")
    own = (m or {}).get("owner")
    resuming = bool(m and m.get("state") == "uninstalling" and own == me)
    if m and m.get("state") not in (None, "ok") and not resuming:
        return adopt._refuse("interrupted", m.get("blocked_message") or "an operation is in progress")
    if own not in (None, me) and not channel.owner_gone(m):
        return adopt._refuse("channel_conflict",
                             f"Contorch here is managed by {channel.WHERE.get(own, own)}; uninstall it from "
                             "there (its `contorch uninstall`, or the app's menu).")
    try:
        adopt.recording(me, None, not_recording)
    except OpError as e:
        return {"ok": False, "error": e.as_json()}
    op_id = (m or {}).get("op", {}).get("id") if resuming else channel.new_op_id()
    data = ["--remove-data"] if remove_data else []
    mc_argv = ["uninstall", *data, "--json"]
    cm_argv = ["claude", "uninstall", "--channel", me, *data, "--json"]
    steps = [
        _step("mark_uninstalling", "an interrupted uninstall is detected and finished", op_id=op_id),
        # meeting-capture 0.7 has a plain `uninstall` (no --json): the last fallback.
        _owner("meeting-capture", mc_argv, me, "the recorder agent (no disabled override is left behind)",
               schema="meeting-capture.agent/", on_missing="skip",
               fallbacks=([["uninstall", "--json"]] if data else []) + [["uninstall"]]),
        _owner("meeting-capture", ["skill", "uninstall", "--json"], me, "the /meeting skill link (yours is kept)",
               schema="meeting-capture.skill/", on_old="skip", on_missing="skip"),
        _owner("contorch-memory", cm_argv, me,
               "Claude Code: MCP entry, hook, CLAUDE.md block, transcripts skill (only Contorch's)",
               schema="contorch-memory.claude/", on_old="skip", on_missing="skip",
               fallbacks=[["claude", "uninstall", "--channel", me, "--json"]] if data else []),
    ]
    if me == "brew" and "contorch" in adopt.brew_formulas():
        steps.append(_step("brew", "the menu bar's autostart", argv=["services", "stop", "contorch/tap/contorch"],
                           ok_rc=(0, 1)))
    if me == "app":
        steps.append(_step("cli_links", "the commands on your PATH", action="uninstall"))
    steps.append(_step("delete_marker", "nothing owns Contorch here any more"))
    bundle_id = (channel.layout().get("bundle_id") if me == "app" else None)
    if bundle_id:
        steps.append(_step("tccutil", "the app's rows in Privacy & Security", bundle_id=bundle_id))
    todo = []
    if me == "brew":
        todo.append({"code": "brew_uninstall",
                     "message": "brew uninstall " + " ".join(adopt.brew_formulas() or adopt.FORMULAS)})
    if me == "app":
        todo.append({"code": "login_item", "message": "Turn off Contorch in System Settings › General › Login "
                                                      "Items if it is still listed"})
        todo.append({"code": "trash_app", "message": "Move Contorch.app to the Trash."})
    if not remove_data:
        todo.append({"code": "data_kept",
                     "message": "Your meetings and memory are kept (~/.context-orchestrator, ~/.meeting-capture); "
                                "`contorch uninstall --remove-data` moves them to the Trash."})
    return {"ok": True, "owner": own, "me": me, "op": op_id, "resume": resuming, "remove_data": remove_data,
            "steps": steps, "todo": todo}


def _do_extra(st: dict, run: adopt.Run):
    """Steps only uninstall has; everything else is adopt's engine."""
    k, a = st["kind"], st["args"]
    if k == "mark_uninstalling":
        cur = channel.read() or {}
        if cur.get("state") == "uninstalling" and (cur.get("op") or {}).get("id") == a["op_id"]:
            return "resumed"
        me = owners.channel()
        channel.write(channel.build(me, "uninstalling", op_id=a["op_id"],
                                    extra={"layout": cur.get("layout")}))
        return None
    if k == "delete_marker":
        channel.remove()
        return None
    if k == "tccutil":
        r = adopt.run_system(["tccutil", "reset", "All", a["bundle_id"]], timeout=30)
        return "reset" if r.returncode == 0 else f"tccutil exit {r.returncode}"
    return adopt._do(st, run)


def execute(plan: dict, log=lambda e: None) -> dict:
    if not plan.get("ok"):
        return plan
    run = adopt.Run(plan)
    done = []
    steps = plan["steps"]
    for i, st in enumerate(steps):
        log({"event": "progress", "step": i + 1, "of": len(steps), "kind": st["kind"], "why": st["why"]})
        try:
            res = _do_extra(st, run)
        except OpError as e:
            return {"ok": False, "error": e.as_json(), "failed_step": st, "done": done, "todo": run.todo}
        except Exception as e:   # noqa: BLE001
            return {"ok": False, "error": {"code": "internal", "message": f"{type(e).__name__}: {e}"},
                    "failed_step": st, "done": done, "todo": run.todo}
        done.append({"kind": st["kind"], **({"result": res} if res is not None else {})})
    pids = adopt.mcp_processes()
    if pids:
        run.todo.append({"code": "restart_claude_code", "pids": pids,
                         "message": "Quit and reopen Claude Code (its MCP server is gone)."})
    return {"ok": True, "done": done, "todo": run.todo}


def add_cli(sub) -> None:
    p = sub.add_parser("uninstall", help="remove Contorch from this Mac (keeps your meetings and memory "
                                         "unless --remove-data)")
    adopt._common_flags(p)
    p.add_argument("--remove-data", action="store_true",
                   help="also move your meetings and memory to the Trash (each owner its own)")
    p.set_defaults(func=_cmd)


def _cmd(args) -> int:
    plan = plan_uninstall(remove_data=args.remove_data, not_recording=args.not_recording)
    return adopt.run_cli("uninstall", plan, args, execute=execute)
