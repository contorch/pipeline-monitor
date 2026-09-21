#!/usr/bin/env bash
# install.sh — set up pipeline-monitor on this laptop.
#
# Idempotent. Safe to re-run.
#
# Usage:
#   ./install.sh                # create venv + install + launch once
#   ./install.sh --autostart    # also install launchd agent for login auto-start
#   ./install.sh --uninstall    # uninstall launchd agent + stop running app

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO_ROOT/.venv"
PYTHON="${PYTHON:-python3}"
PLIST_LABEL="com.contorch.pipeline-monitor"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
LOG_DIR="$HOME/Library/Logs/pipeline-monitor"

ACTION="install"
AUTOSTART=0

for arg in "$@"; do
    case "$arg" in
        --autostart) AUTOSTART=1 ;;
        --uninstall) ACTION="uninstall" ;;
        -h|--help)
            sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "unknown arg: $arg (try --help)" >&2; exit 2 ;;
    esac
done

ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
fail() { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# ============================================================ uninstall
if [ "$ACTION" = "uninstall" ]; then
    if [ -f "$PLIST_PATH" ]; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        rm -f "$PLIST_PATH"
        ok "removed launchd agent"
    fi
    pkill -f "pipeline-monitor" 2>/dev/null && ok "stopped running app" || ok "no running app to stop"
    ok "uninstall complete (venv left intact at $VENV; rm -rf manually if you want)"
    exit 0
fi

# ============================================================ install
command -v "$PYTHON" >/dev/null || fail "$PYTHON not found in PATH"

if [ ! -d "$VENV" ]; then
    "$PYTHON" -m venv "$VENV"
    ok "created venv at $VENV"
else
    ok "venv exists"
fi

"$VENV/bin/pip" install --quiet --upgrade pip wheel >/dev/null 2>&1 || true
"$VENV/bin/pip" install --quiet -e "$REPO_ROOT" >/dev/null
ok "installed deps + pipeline-monitor (editable)"

# Verify it can boot without crashing: run it for 3s and require that it is
# still alive at the deadline. (The old `timeout 2 … || true` passed every exit
# code — and stock macOS has no `timeout` — so a crash on launch printed
# "passed" and the user got no icon and no clue.)
"$VENV/bin/python" - "$VENV/bin/pipeline-monitor" <<'PYEOF' || fail "pipeline-monitor crashed on launch (see output above) — fix that before installing the login item"
import subprocess, sys
p = subprocess.Popen([sys.argv[1]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    out, _ = p.communicate(timeout=3)
except subprocess.TimeoutExpired:
    p.kill(); p.wait()
    sys.exit(0)          # still running at the deadline: booted fine
print(out[-2000:]); sys.exit(1)
PYEOF
ok "boot smoke test passed (alive after 3s)"

if [ "$AUTOSTART" = 1 ]; then
    mkdir -p "$LOG_DIR" "$(dirname "$PLIST_PATH")"
    cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV/bin/pipeline-monitor</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/stderr.log</string>
</dict>
</plist>
EOF
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH" || fail "launchctl load failed for $PLIST_PATH"
    # launchd reports load success even if the app then dies; check it stayed up.
    sleep 3
    if launchctl list "$PLIST_LABEL" 2>/dev/null | grep -q '"PID"'; then
        ok "installed launchd agent — pipeline-monitor is running and will auto-start at login"
    else
        fail "launchd agent installed but the app is not running — check $LOG_DIR/stderr.log"
    fi
fi

# Launch once now (unless autostart launchd just did it)
if [ "$AUTOSTART" != 1 ]; then
    if pgrep -f "pipeline-monitor" >/dev/null; then
        ok "already running (skipping launch)"
    else
        nohup "$VENV/bin/pipeline-monitor" > /tmp/pipeline-monitor.log 2>&1 &
        disown
        sleep 1
        pgrep -f "pipeline-monitor" >/dev/null && ok "launched (PID $(pgrep -f pipeline-monitor | head -1))" \
            || warn "launch failed — check /tmp/pipeline-monitor.log"
    fi
fi

cat <<EOF

Done. Look for ○ in your menu bar (top-right of the screen).

Useful:
  ./install.sh --autostart    # add to login items via launchd
  ./install.sh --uninstall    # remove launchd agent + stop app
  pkill -f pipeline-monitor   # stop without uninstalling
EOF
