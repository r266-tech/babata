#!/usr/bin/env bash
# Fired when a new CC session is established (fresh sid observed in
# ResultMessage, /resume to a different sid, or resume-failure retry).
# Pushes "🟢 session 开始: <sid>" to the bot's default TG chat via bridge.
#
# Silent no-op when: sid empty, bridge socket missing, bot down, network blip.
# Running async (launched with start_new_session=True), exit fast — the bot's
# event loop is busy handing back the user-visible reply.

set -u
sid="${CLAUDE_SESSION_ID:-}"
sock="${BABATA_BRIDGE_SOCKET:-/tmp/babata-bridge.sock}"
[[ -z "$sid" ]] && exit 0
[[ ! -S "$sock" ]] && exit 0

SID="$sid" SOCK="$sock" TAG="🟢 session 开始" python3 - <<'PY' 2>/dev/null
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
