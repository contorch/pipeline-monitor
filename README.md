<p align="left">
  <img src="assets/contorch-orange.png" width="96" alt="Contorch logo">
</p>

# pipeline-monitor

> Part of **Contorch** — the persistent context layer for Claude Code.

macOS menu bar app for the meeting-capture + context-orchestrator pipeline.

Glanceable state in the menu bar — `○` idle / `● REC` recording / `⚠` something broken — and a six-section dashboard when you click it.

## What it shows

| Section | Data |
|---|---|
| **NOW** | Recording state + current meeting (from the meeting-capture daemon log) · **Transcription:** `on this Mac (en-US)` / `Gemini` / `⚠ unavailable — <reason>`, as meeting-capture reports it (details: setting, locale, on-device state, key, where audio goes) |
| **RECENT SESSIONS** | Last 10 transcripts (from the context-orchestrator database; legacy `~/transcripts/*.md` too). Each has **Copy transcript** (whole text to the clipboard) and **Open in TextEdit**. |
| **INDEX HEALTH** | Chroma doc count + embedding dim · SQLite tasks/sources/insights · last insight age |
| **MCP / CONNECTIONS** | MCP server activity · last tool call (tool, result, latency, ago) · auto-context hook last fire (ago + latency + chars injected) · expandable timeline of last 20 calls |
| **SYSTEM HEALTH** | launchd daemon status with PIDs · disk usage per data dir |
| **Actions** | Refresh now · Run end-to-end smoke test · **Start new meeting** (`meeting-capture new` — next speech opens a new transcript) · **Recording settings…** (`meeting-capture ui`: source, USB-interface inputs, live levels) · Open latest transcript/CO dir/MCP log · Restart chroma daemon · capture mode · Stop/Resume everything · Quit |

End-to-end smoke test: inserts a marker doc → searches for it → deletes it. One-click "is the whole stack actually working right now?" check. Reports rank + latency in a notification.

### Transcription engine

meeting-capture transcribes on this Mac (Apple's on-device speech model: macOS 26+ on Apple silicon, no key, audio never leaves the Mac) or with Gemini (optional; needs a key). `meeting-capture stt auto|apple|gemini` and `meeting-capture language LOCALE` set it. `contorch setup` offers Gemini only as an optional upgrade when on-device works, and asks for a key only when it doesn't. With a key the recorder can see, it offers “on this Mac only” (`stt apple`, never uploads) apart from “on this Mac, Gemini as backup” (`stt auto`, which uploads a chunk if on-device transcription stops working). It applies the choice by running those meeting-capture commands (their progress, a model download included, streams into setup's output), then asks again before it says where the audio goes. `contorch status` and `contorch doctor` show the engine, the language and where audio goes.

Live mode (`meeting-capture mode live`, or the menu's capture-mode toggle) streams every call to Gemini as it happens, whatever the engine is. While it does, the menu bar's Transcription line, Details, `contorch status`/`doctor` add “live: calls stream to Gemini”, and `contorch setup` says so instead of “the audio never leaves this Mac”.

pipeline-monitor has no rules of its own for any of this. It asks meeting-capture (see [Contract](#contract)) and shows the answer. The menu bar asks on a background thread and caches the answer per (the agent's plist, the meeting-capture executable and its venv's version stamp, the key file) for 10 minutes; a failed read is retried after a minute. A `meeting-capture stt|language|mode` change or a `brew upgrade` therefore shows up on the next refresh. A read never runs Homebrew's wrapper (see [Contract](#contract)): after a `brew upgrade` it reads the venv the recorder still runs and adds “run `meeting-capture install` between meetings”. See `pipeline_monitor/transcription.py`. If meeting-capture is missing, the line is left out. A meeting-capture older than 0.7 (no `stt --json`) shows as “Gemini (meeting-capture < 0.7 — upgrade for on-device)”. An answer that can't be read shows as “unknown”. Neither fallback ever claims on-device transcription.

## Contract

**`meeting-capture stt --json`** is the single source of truth for how meetings are transcribed. meeting-capture implements the rules once, in its `transcriber.py`, and pipeline-monitor reads the answer in `pipeline_monitor/transcription.py` instead of mirroring them. A change on either side updates the other, and the field list in meeting-capture's README "Contract" section, together. The command prints one JSON object on stdout, `"schema": 1`. Adding a field keeps the schema; removing or redefining one bumps it, and this side then reports “unknown”. The fields pipeline-monitor relies on are:

- `engine` (`apple` | `gemini` | `none`), `ready` and `reason`
- `locale`, `locale_why` and `locale_guessed`
- `uploads`, `live.{requested, active, blocker}` and `may_upload`
- `gemini_key` and `gemini_fallback`
- `apple.{usable, installable, reason, installed_locales, helper}`
- `needs_model` and `install_hint`
- `on_device_hint`, `on_device_only_hint`, `choice` and `notice`

Where audio goes is worded only from `live.active`, `uploads` and `may_upload` (`transcription.privacy()`): “never leaves this Mac” only when `may_upload` is false. `may_upload` is also true for `auto` with a key while on-device runs (`gemini_fallback`), because meeting-capture then sends a chunk to Gemini by itself when on-device transcription fails; that case reads “on this Mac — but if on-device transcription stops working, Gemini takes over (uploaded)”, plus the JSON's `on_device_only_hint` (`meeting-capture stt apple` never uploads). A usage error (exit 2) means meeting-capture < 0.7. Reads run the venv's own `~/.meeting-capture/venv/bin/meeting-capture` (what the recorder runs), never the Homebrew wrapper, which after a `brew upgrade` deletes and rebuilds that venv under the running recorder; only `contorch setup`, in the foreground, goes through the wrapper. Setup changes things only through meeting-capture's own commands: `meeting-capture stt gemini`, or the JSON's `on_device_only_hint` (“on this Mac only”: `meeting-capture stt apple`), `on_device_hint` (“on this Mac, Gemini as backup”: `meeting-capture language L`, `meeting-capture stt auto [--language L]`). Those commands print progress lines on stdout, never prompt, and exit 0 when applied (1 when refused).

## Modules and channels

Contorch is made of **modules** — memory (always), the meeting recorder, a USB audio interface (line-in) and the terminal commands — and is installed through one **channel**: `app` (Contorch.app), `brew` (Homebrew formulas) or `dev` (a source checkout). Both are declared once, here:

- `pipeline_monitor/modules.py`: the registry. `contorch modules [list|enable|disable|plan|brew-spec] [--json]` (`contorch.modules/1`). The user's choice lives in `~/.contorch/modules.json` (`{schema, wanted{memory, recorder, linein, cli}, embeddings_source?}`); with no file, what is set up is what was wanted (existing installs migrate without a question). Turning a module on runs its owner's command. On a Mac with no transcription engine (macOS 15 without an accepted Gemini key) the recorder stays off. The menu bar greys out modules that aren't on, with the way to add each: `brew install contorch/tap/meeting-capture` with Homebrew, "Turn on meeting recorder…" in the app. `brew install contorch/tap/contorch --without-meeting-capture` is a memory-only Mac.
- `pipeline_monitor/channel.py`: the owner of `~/.contorch/channel.json` (`contorch.channel/1`) and the **only** copy of the guard rule. It writes the rule's answer into the marker — `writers` (who may change Contorch's surfaces), an optional operation token `op`, `blocked_message` — so meeting-capture and context-orchestrator's installers only test membership (fixtures: `contract/channel_guard/`). `contorch channel [--json]` (`contorch.channel.status/1`) shows the owner, the writers and what needs attention (`interrupted`, `owner_gone`, `mixed_channels`, `brew_relinked`).
- `pipeline_monitor/owners.py`: the only place pm finds a sibling executable (`locate()`: the app's bundle; else Homebrew's `opt/` path, PATH, a dev venv, the per-user venv) and the one way it runs an owner's `--json` verb. A process learns its channel from `$CONTORCH_CHANNEL` only (unset = `dev`); `contorch channel` warns when that disagrees with where it runs.
- `pipeline_monitor/mcconfig.py`: meeting-capture's settings from `meeting-capture config --json` (cached on its `watch_paths`); with meeting-capture 0.7, from its plist.

## Install

```bash
git clone https://github.com/contorch/pipeline-monitor.git ~/tasks/pipeline-monitor
cd ~/tasks/pipeline-monitor
./install.sh                # create venv, install deps, launch once
./install.sh --autostart    # add launchd agent so it starts at login
./install.sh --uninstall    # remove launchd agent + stop app
```

That's it — look for `○` in the menu bar.

## Where it expects things to live

It auto-locates context-orchestrator via `$CO_REPO` env or these well-known paths (in order):

- `~/tasks/vector_databases_experiments`
- `~/tasks/context-orchestrator`
- `~/src/context-orchestrator`
- `~/code/context-orchestrator`

If yours lives elsewhere, set `CO_REPO=/path/to/repo` in your shell rc.

Other paths (hard-coded, all standard for the pipeline):

- `~/.context-orchestrator/` — chroma data, SQLite, daemon logs, hook heartbeat
- `~/.context-orchestrator/context.db` — transcripts (table `transcripts`, meeting-capture ≥ 0.5); `~/transcripts/` only on older installs
- `~/.meeting-capture/daemon.log` — recording state source-of-truth
- `~/Library/Caches/claude-cli-nodejs/.../mcp-logs-context-orchestrator/` — MCP tool-call timeline source

Each collector fails silently if its data source is missing — a half-installed pipeline still produces a useful dashboard, not a wall of red.

## Architecture

Python 3.10+, pure stdlib + [`rumps`](https://github.com/jaredks/rumps) (NSStatusItem wrapper) + [`httpx`](https://www.python-httpx.org/) for the chroma daemon heartbeat.

Read-only. Polls every 5s (meeting-capture is asked how it transcribes at most every 10 min, off the main thread). Each subsystem has its own collector function in `pipeline_monitor/status.py` — independent, fail-silent. The smoke test spawns through context-orchestrator's venv to avoid duplicating chromadb/google-genai deps.

## Why it exists

Three independent daemons (chroma, transcript-watcher, meeting-capture) plus an MCP server spawned by Claude Code plus a UserPromptSubmit hook is too many moving parts to keep in your head. The dashboard answers four questions at a glance:

- **Am I recording right now?**
- **Is the index up to date?**
- **Is Claude Code actually using the auto-context hook + MCP search?**
- **Are any of the daemons down?**

## See also

- [`stirredo/context-orchestrator`](https://github.com/contorch/context-orchestrator) — task + context store, MCP server, auto-context hook
- meeting-capture — audio capture daemon (sysaudio / ScreenCaptureKit)
