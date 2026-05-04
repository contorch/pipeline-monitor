# pipeline-monitor

macOS menu bar app that surfaces the live state of your transcript pipeline:

- **Now** — am I recording? audio level, current session
- **Recent sessions** — last N captures with `recorded → transcribed → indexed → searchable` badges
- **Transcription pipeline** — Gemini queue, last call latency, last error
- **Index health** — chroma collection size, embedding model, dim, last indexed file, quick search
- **MCP / connections** — context-orchestrator MCP alive? recent tool calls? auto-context hook firing?
- **System health** — chroma daemon, watcher, meeting-capture daemons; disk usage; recent errors

Plus: a one-button **end-to-end smoke test**, **per-meeting cost rollup**, **MCP tool-call timeline**, and **macOS notifications** on failures.

## Install

```bash
cd ~/tasks/pipeline-monitor
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pipeline-monitor
```

To run at login, install as a launchd agent (`pipeline-monitor install` — TBD).

## Architecture

Read-only. Polls every 5s. Each subsystem has its own collector under `pipeline_monitor/collectors/` that fails silently if its data source is missing — so a half-installed pipeline still produces a useful dashboard.

Data sources:

| Subsystem | Source |
|---|---|
| Recording state | meeting-capture daemon log + active process check |
| Transcription | Gemini call records (TBD: meeting-capture log) |
| Index | Chroma HTTP at 127.0.0.1:8765, context-orch SQLite at ~/.context-orchestrator/context.db |
| MCP | ~/Library/Caches/claude-cli-nodejs/.../mcp-logs-context-orchestrator/*.jsonl |
| Auto-context hook | ~/.context-orchestrator/auto-context-heartbeat.json (written by the hook on each fire) |
| Daemons | `launchctl list` |
