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
| **NOW** | Recording state + current meeting (from the meeting-capture daemon log) · **Transcription:** `on this Mac (en-US)` / `Gemini` / `⚠ unavailable — <reason>` (details: setting, locale, on-device state, key) |
| **RECENT SESSIONS** | Last 10 transcripts (from the context-orchestrator database; legacy `~/transcripts/*.md` too). Each has **Copy transcript** (whole text to the clipboard) and **Open in TextEdit**. |
| **INDEX HEALTH** | Chroma doc count + embedding dim · SQLite tasks/sources/insights · last insight age |
| **MCP / CONNECTIONS** | MCP server activity · last tool call (tool, result, latency, ago) · auto-context hook last fire (ago + latency + chars injected) · expandable timeline of last 20 calls |
| **SYSTEM HEALTH** | launchd daemon status with PIDs · disk usage per data dir |
| **Actions** | Refresh now · Run end-to-end smoke test · **Start new meeting** (`meeting-capture new` — next speech opens a new transcript) · **Recording settings…** (`meeting-capture ui`: source, USB-interface inputs, live levels) · Open latest transcript/CO dir/MCP log · Restart chroma daemon · capture mode · Stop/Resume everything · Quit |

End-to-end smoke test: inserts a marker doc → searches for it → deletes it. One-click "is the whole stack actually working right now?" check. Reports rank + latency in a notification.

### Transcription engine

meeting-capture transcribes on this Mac (Apple's on-device speech model: macOS 26+ on Apple silicon, no key, audio never leaves the Mac) or with Gemini (optional; needs a key). `meeting-capture stt auto|apple|gemini` and `meeting-capture language LOCALE` set it; `contorch setup` offers Gemini only as an optional upgrade when on-device works, and asks for a key only when it doesn't. `contorch status` and `contorch doctor` show the engine and locale.

The menu bar reads it cheaply and never edits it: the setting and locale come from the meeting-capture agent's plist env (`MEETING_CAPTURE_STT`, `MEETING_CAPTURE_LOCALE`); whether this Mac can do it comes from one `sysaudio transcribe --probe` run on a background thread, cached for 15 minutes per (helper binary, locale) — so a `brew upgrade` or a language change is picked up on the next refresh. The daemon log is not parsed for this. See `pipeline_monitor/transcription.py`.

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

Read-only. Polls every 5s (the transcription probe runs at most every 15 min, off the main thread). Each subsystem has its own collector function in `pipeline_monitor/status.py` — independent, fail-silent. The smoke test spawns through context-orchestrator's venv to avoid duplicating chromadb/google-genai deps.

## Why it exists

Three independent daemons (chroma, transcript-watcher, meeting-capture) plus an MCP server spawned by Claude Code plus a UserPromptSubmit hook is too many moving parts to keep in your head. The dashboard answers four questions at a glance:

- **Am I recording right now?**
- **Is the index up to date?**
- **Is Claude Code actually using the auto-context hook + MCP search?**
- **Are any of the daemons down?**

## See also

- [`stirredo/context-orchestrator`](https://github.com/contorch/context-orchestrator) — task + context store, MCP server, auto-context hook
- meeting-capture — audio capture daemon (sysaudio / ScreenCaptureKit)
