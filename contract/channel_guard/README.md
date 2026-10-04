# Channel guard reader contract

`~/.contorch/channel.json` (`contorch.channel/1`) is written only by
`pipeline_monitor.channel`, which holds the rule and stores its answer in the
marker: `writers` (the channels allowed to write), an optional `op` token, and
`blocked_message`.

The readers only test membership:

- meeting-capture: `channel_guard.allowed()`
- context-orchestrator: `scripts/contorch_channel_guard.sh`, sourced by its bash installers

The rule, in full:

1. No marker means allowed.
2. Otherwise, allowed if and only if `$CONTORCH_CHANNEL` (unset or unknown means `dev`) is in `writers`, **and** `op` is absent (or `null`) or `$CONTORCH_OP == op.id`.
3. Anything else blocks: print `blocked_message` (or the reader's own when it is missing) and exit 3. An unreadable marker also blocks.

Each fixture here is `{description, env, marker | marker_raw, expect{exit, stderr | stderr_contains}}`:

- `marker` is the JSON to write, or `null` for no file at all;
- `marker_raw` is written verbatim.

All three repos run every fixture in CI. pipeline-monitor owns them; the readers pin a commit and check its sha.
