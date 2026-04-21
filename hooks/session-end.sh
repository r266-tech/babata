#!/usr/bin/env bash
# Fired when a session is closing (user /reset, /resume switch, or resume
# failure). Pushes "🔴 session 结束: <sid>" to the bot's default TG chat via
# bridge. Same silent-fail + async semantics as session-start.sh.

set -u
sid="${CLAUDE_SESSION_ID:-}"
sock="${BABATA_BRIDGE_SOCKET:-/tmp/babata-bridge.sock}"
[[ -z "$sid" ]] && exit 0
[[ ! -S "$sock" ]] && exit 0

SID="$sid" SOCK="$sock" TAG="🔴 session 结束" python3 - <<'PY' 2>/dev/null
import json, os, socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5)
try:
    s.connect(os.environ["SOCK"])
    payload = {"action": "send_text", "text": f"{os.environ['TAG']}: {os.environ['SID']}"}
    s.sendall((json.dumps(payload) + "\n").encode())
except Exception:
    pass
PY
exit 0
