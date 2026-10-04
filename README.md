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
| **NOW** | The headline: ● Recording / ○ Idle / "? Can't tell" from `meeting-capture status --json` (only `recording: true` is ● REC; meeting-capture 0.7: the daemon log), or **Memory only — this Mac doesn't record** · the recorder's permission rows from `meeting-capture check --json` (both Screen & System Audio Recording and Microphone, with meeting-capture's own per-channel hint; a click opens the Privacy pane) · greyed rows for modules that aren't on, each with the way to add it · **Transcription:** `on this Mac (en-US)` / `Gemini` / `⚠ unavailable — <reason>`, as meeting-capture reports it (details: setting, locale, on-device state, key, where audio goes) |
| **RECENT SESSIONS** | Last 10 transcripts (from the context-orchestrator database; legacy `~/transcripts/*.md` too). Each has **Copy transcript** (whole text to the clipboard) and **Open in TextEdit**. |
| **INDEX HEALTH** | From `contorch-memory status --json`: documents, embeddings, in-process / keyword-only, transcripts (pm no longer mirrors the embedding rules) · SQLite insights |
| **MCP / CONNECTIONS** | MCP server activity · last tool call (tool, result, latency, ago) · auto-context hook last fire (ago + latency + chars injected) · expandable timeline of last 20 calls |
| **SYSTEM HEALTH** | "Background: recorder running (pid …)" or, on a memory-only Mac, "Background: nothing runs" (the index is in-process: no chroma daemon) · disk usage per data dir |
| **Actions** | Refresh now · Run smoke test · **Start new meeting** (`meeting-capture new` — next speech opens a new transcript) · **Recording settings…** (`meeting-capture ui`: source, USB-interface inputs, live levels) · capture mode — these three only when this Mac records · **Import transcripts…** (memory-only Macs: `contorch-transcripts import --json`) · Open latest transcript/CO dir/MCP log · Stop/Resume everything · Quit |

Smoke test = context-orchestrator's own end-to-end check (`contorch-memory selftest --json`: write a marker, search for it, delete it), also `contorch smoke [--json]`. `contorch doctor --json [--bundle]` prints one document for bug reports: versions, channel, modules and the owners' own JSON, plus (with `--bundle`) the last log lines — redacted: no keys, no e-mail addresses, no transcript text.

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

## Quit, launch, updates (`pipeline_monitor/lifecycle.py`)

Python decides; the menu bar only reports the event.

- **Quit** (`on_quit(reason)`): the Quit item and a quit Apple Event reach rumps' `before_quit`; **SIGTERM** (`brew services stop|restart`, `brew upgrade`, `launchctl bootout`) is routed to the same quit (a handler plus a 0.25 s timer). A quit Apple Event with `kAEQuitReason` is a logout: nothing to do. An update always stops the recorder (`contorch stop --reason update`). Otherwise the recorder stops (`--reason quit`) unless **Keep recording after Quit** is on — default off in Contorch.app, on with Homebrew/source (today's behaviour). The menu has the checkbox (hidden on a Mac that doesn't record); `contorch preferences set keep-recording-after-quit on|off`.
- **Launch** (`on_launch()`): in **every** channel, a stack stopped by a quit or an update is resumed — a user's "Stop everything" is not. So `brew upgrade` with keep-recording off doesn't leave the recorder off. Inside Contorch.app only: `location()` (translocated / read-only / outside Applications registers nothing), the channel marker (claim + attention), heal (`meeting-capture heal --json` and, if Claude Code's entries are off, `contorch-memory claude install`) only while meeting-capture says nothing is being recorded, and whether to offer setup.
- **Updates**: `install_allowed()` is true only when `meeting-capture status --json` says `recording: false` (recording or can't-tell holds the install); `prepare_update()` stops with `reason=update`, which the new version resumes. (Sparkle itself is M4.)
- **`contorch stop|resume [--json] [--reason user|quit|update]`** (`contorch.stack/1`): the recorder through meeting-capture's own `stop --json --reason` / `start --json` (either backend; `start` re-enables a job a legacy stop disabled); `launchctl` only for retired agents (an old chroma server, transcript-watcher) and meeting-capture 0.7. `~/.contorch/stopped.json` records the reason.
- `contorch lifecycle launch|quit|install-allowed --json` exposes the same decisions to the app's future SwiftUI shell.

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

The other packages are found with `owners.locate()` (the app's bundle; Homebrew's `opt/` paths; PATH; a per-user venv) and asked through their `--json` verbs. Read directly (read-only, display only):

- `~/.context-orchestrator/context.db` — Recent sessions (table `transcripts`, meeting-capture ≥ 0.5); `~/transcripts/` only on older installs
- `~/.meeting-capture/daemon.log` — the recent-activity display (and recording state for meeting-capture 0.7, which has no `status --json`)
- `~/Library/Caches/claude-cli-nodejs/.../mcp-logs-context-orchestrator/` — MCP tool-call timeline source

Each collector fails silently if its data source is missing — a half-installed pipeline still produces a useful dashboard, not a wall of red.

## Architecture

Python 3.10+, pure stdlib + [`rumps`](https://github.com/jaredks/rumps) (NSStatusItem wrapper).

Read-only. Polls every 5s; the owners are asked on background threads and cached (`pipeline_monitor/ownerstate.py`: `meeting-capture status --json` every 20 s or when its state file changes, `check --json` every 10 min or when the log shows a refusal, `contorch-memory status --json` every minute; `stt --json` every 10 min). Each subsystem has its own collector function in `pipeline_monitor/status.py` — independent, fail-silent.

## Why it exists

Three independent daemons (chroma, transcript-watcher, meeting-capture) plus an MCP server spawned by Claude Code plus a UserPromptSubmit hook is too many moving parts to keep in your head. The dashboard answers four questions at a glance:

- **Am I recording right now?**
- **Is the index up to date?**
- **Is Claude Code actually using the auto-context hook + MCP search?**
- **Are any of the daemons down?**

## See also

- [`stirredo/context-orchestrator`](https://github.com/contorch/context-orchestrator) — task + context store, MCP server, auto-context hook
- meeting-capture — audio capture daemon (sysaudio / ScreenCaptureKit)
