#!/usr/bin/env bash
# babata runtime auto-update — git pull + uv sync + restart service.
# Cross-platform (Linux systemd / macOS launchd). Idempotent: 没变化就早退.
# Triggered by systemd timer (Linux) 或 launchd StartCalendarInterval (macOS),
# 配 hourly. install.sh 末尾自动配好.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR" || exit 1

UPGRADE_SDK=0
UPGRADE_CLAUDE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --upgrade-claude) UPGRADE_CLAUDE=1; shift ;;
        --upgrade-sdk) UPGRADE_SDK=1; shift ;;
        *) shift ;;
    esac
done

# Pull just PROJECT_STATE_DIR from .env (avoid blanket-exporting tokens
# to subprocess). Caller-set PROJECT_STATE_DIR wins.
ENV_FILE="$SCRIPT_DIR/.env"
if [ -f "$ENV_FILE" ] && [ -z "${PROJECT_STATE_DIR:-}" ]; then
    PROJECT_STATE_DIR=$(grep -m1 '^PROJECT_STATE_DIR=' "$ENV_FILE" 2>/dev/null \
        | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r' || true)
    [ -n "$PROJECT_STATE_DIR" ] && export PROJECT_STATE_DIR
fi

PROJECT_NAMESPACE="${PROJECT_NAMESPACE:-babata}"
LABEL_PREFIX="com.${PROJECT_NAMESPACE}"
SERVICE="${PROJECT_NAMESPACE}.service"
RESTART_IDLE_WAIT_SECONDS="${RESTART_IDLE_WAIT_SECONDS:-3600}"
VERSION_WATCH_HARD_TIMEOUT_SECONDS="${BABATA_VERSION_WATCH_HARD_TIMEOUT_SECONDS:-1800}"
PLATFORM="${BABATA_PLATFORM:-$(uname -s)}"
LAUNCHCTL_BIN="${BABATA_LAUNCHCTL:-launchctl}"
BABATA_RESTART_LABELS="${BABATA_RESTART_LABELS:-$LABEL_PREFIX}"

LOG="$SCRIPT_DIR/logs/auto-update.log"
mkdir -p "$(dirname "$LOG")"
exec >> "$LOG" 2>&1

echo ""
echo "=== $(date -Iseconds) ==="

# CC shell pypi env 污染防御 (用户 env 可能继承公司 nexus)
unset UV_INDEX_URL PIP_INDEX_URL UV_EXTRA_INDEX_URL PIP_EXTRA_INDEX_URL 2>/dev/null || true

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

UV=$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")
CLAUDE_BIN="${CLAUDE_CLI_PATH:-$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")}"
VENV_PY="$SCRIPT_DIR/.venv/bin/python"
TIMEOUT_PYTHON="${BABATA_TIMEOUT_PYTHON:-$(command -v python3 2>/dev/null || true)}"

case "$VERSION_WATCH_HARD_TIMEOUT_SECONDS" in
    ''|*[!0-9]*|0)
        echo "WARN: invalid BABATA_VERSION_WATCH_HARD_TIMEOUT_SECONDS=$VERSION_WATCH_HARD_TIMEOUT_SECONDS; using 1800"
        VERSION_WATCH_HARD_TIMEOUT_SECONDS=1800
        ;;
esac

CODE_CHANGED=0
DEPS_CHANGED=0
CLI_CHANGED=0
SDK_CHANGED=0

running_launchd_labels() {
    local loaded label
    if ! loaded=$("$LAUNCHCTL_BIN" list | awk '$1 ~ /^[0-9]+$/ {print $3}'); then
        echo "ERROR: launchctl list failed; refusing to claim restart success" >&2
        return 1
    fi
    for label in $BABATA_RESTART_LABELS; do
        case "$label" in
            "$LABEL_PREFIX"|"$LABEL_PREFIX".*) ;;
            *)
                echo "WARN: ignoring restart label outside namespace: $label" >&2
                continue
                ;;
        esac
        if printf '%s\n' "$loaded" | grep -Fqx "$label"; then
            printf '%s\n' "$label"
        fi
    done
}

# Run the complete version-watch process tree in its own session. The outer
# timeout is intentionally independent of any broker/CLI timeout inside the
# workflow, so a wedged wrapper cannot indefinitely block the required restart.
run_with_process_group_timeout() {
    local timeout_seconds="$1"
    shift
    "$TIMEOUT_PYTHON" - "$timeout_seconds" "$@" <<'PY'
import os
import signal
import subprocess
import sys

timeout_seconds = int(sys.argv[1])
command = sys.argv[2:]
process = subprocess.Popen(command, start_new_session=True)
try:
    return_code = process.wait(timeout=timeout_seconds)
except subprocess.TimeoutExpired:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    # The session leader may exit on SIGTERM before one of its descendants.
    # Always sweep the process group with SIGKILL after the grace period/leader
    # exit so an ignoring grandchild cannot outlive auto-update.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait()
    raise SystemExit(124)

raise SystemExit(return_code if return_code >= 0 else 128 - return_code)
PY
}

# 1) git pull (ff-only, 本地有 modify 时 skip 避冲突)
if [ -d .git ]; then
    if ! git diff-index --quiet HEAD -- 2>/dev/null; then
        echo "本地有未 commit 修改, skip git pull"
    else
        git fetch origin --quiet 2>/dev/null || echo "git fetch failed"
        LOCAL=$(git rev-parse HEAD 2>/dev/null)
        REMOTE=$(git rev-parse "@{u}" 2>/dev/null || echo "$LOCAL")
        if [ "$LOCAL" != "$REMOTE" ]; then
            echo "新 commit: ${LOCAL:0:7} -> ${REMOTE:0:7}"
            if git pull --ff-only --quiet 2>&1; then
                CODE_CHANGED=1
            else
                echo "git pull --ff-only 失败 (非 ff?), skip"
            fi
        fi
    fi
fi

# 2) uv sync (代码 / lock 变了)
if [ "$CODE_CHANGED" = "1" ] && [ -x "$UV" ]; then
    "$UV" sync --quiet 2>&1 | tail -3
    DEPS_CHANGED=1
fi

# 3) Claude compatibility maintenance is explicit opt-in while Codex is the
# default CPU. The normal runtime updater must not restart channels because a
# disabled provider changed.
if [ "$UPGRADE_CLAUDE" = "1" ] && [ -x "$CLAUDE_BIN" ]; then
    OLD_CLI=$("$CLAUDE_BIN" --version 2>/dev/null | awk '{print $1}')
    "$CLAUDE_BIN" update 2>&1 | tail -3 || true
    NEW_CLI=$("$CLAUDE_BIN" --version 2>/dev/null | awk '{print $1}')
    if [ "$OLD_CLI" != "$NEW_CLI" ]; then
        echo "claude: $OLD_CLI -> $NEW_CLI"
        CLI_CHANGED=1
    fi
fi

# 4) Optional SDK upgrade for explicit compatibility maintenance paths.
# Runtime self-update must not rewrite tracked source files. The venv can move to
# the latest SDK; lockfile bumps belong to an intentional code/dependency change.
if [ "$UPGRADE_SDK" = "1" ]; then
    if [ -x "$UV" ] && [ -x "$VENV_PY" ]; then
        OLD_SDK=$("$VENV_PY" -c "import claude_agent_sdk; print(claude_agent_sdk.__version__)" 2>/dev/null)
        "$UV" pip install --python "$VENV_PY" --upgrade claude-agent-sdk 2>&1 | tail -5
        NEW_SDK=$("$VENV_PY" -c "import claude_agent_sdk; print(claude_agent_sdk.__version__)" 2>/dev/null)
        if [ "$OLD_SDK" != "$NEW_SDK" ]; then
            echo "claude-agent-sdk: $OLD_SDK -> $NEW_SDK"
            SDK_CHANGED=1
        fi
    else
        echo "WARN: --upgrade-sdk requested but uv or venv python is missing"
    fi
fi

# 5) CLI / SDK 真变化时同步做影响分析。分析失败不阻断必要的 service restart；
# version-watch 自己会把失败写进 cron index，交给 health-check 报警。
if [ "$CLI_CHANGED" = "1" ] || [ "$SDK_CHANGED" = "1" ]; then
    VERSION_WATCH="${BABATA_VERSION_WATCH:-$HOME/cc-workspace/cron-skills/version-watch/run.sh}"
    if [ -x "$VERSION_WATCH" ]; then
        if [ -z "$TIMEOUT_PYTHON" ] || [ ! -x "$TIMEOUT_PYTHON" ]; then
            echo "WARN: version-watch skipped because python3 is unavailable; continuing to restart"
        elif run_with_process_group_timeout "$VERSION_WATCH_HARD_TIMEOUT_SECONDS" \
                "$VERSION_WATCH" \
                --cc-old "${OLD_CLI:-}" --cc-new "${NEW_CLI:-}" \
                --sdk-old "${OLD_SDK:-}" --sdk-new "${NEW_SDK:-}"; then
            :
        else
            version_watch_status=$?
            if [ "$version_watch_status" = "124" ]; then
                echo "WARN: version-watch timed out after ${VERSION_WATCH_HARD_TIMEOUT_SECONDS}s; process tree terminated; continuing to restart"
            else
                echo "WARN: version-watch failed after CLI/SDK update (exit=$version_watch_status); continuing to restart"
            fi
        fi
    else
        echo "WARN: version-watch missing or not executable: $VERSION_WATCH; continuing to restart"
    fi
fi

# 6) 重启 service (代码 / deps / cli / sdk 任一变了)
if [ "$CODE_CHANGED" = "1" ] || [ "$DEPS_CHANGED" = "1" ] || [ "$CLI_CHANGED" = "1" ] || [ "$SDK_CHANGED" = "1" ]; then
    # Build a one-line reason for bot.py's restart-reason channel — V 看到的
    # TG alert 会拼上这串, 知道为啥重启 (而不是空洞的 "launchd 自愈").
    reason_parts=""
    [ "$CODE_CHANGED" = "1" ] && reason_parts="code"
    if [ "$DEPS_CHANGED" = "1" ]; then
        [ -n "$reason_parts" ] && reason_parts="$reason_parts+"
        reason_parts="${reason_parts}deps"
    fi
    if [ "$CLI_CHANGED" = "1" ]; then
        [ -n "$reason_parts" ] && reason_parts="$reason_parts+"
        reason_parts="${reason_parts}cli"
    fi
    if [ "$SDK_CHANGED" = "1" ]; then
        [ -n "$reason_parts" ] && reason_parts="$reason_parts+"
        reason_parts="${reason_parts}sdk"
    fi
    REASON="auto-update (scripts/): $reason_parts"

    case "$PLATFORM" in
        Linux)
            if command -v systemctl >/dev/null 2>&1; then
                systemctl --user restart "$SERVICE" 2>&1 && echo "systemd restarted: $SERVICE"
            fi
            ;;
        Darwin)
            if ! LABELS=$(running_launchd_labels); then
                echo "ERROR: cannot enumerate launchd restart targets"
                exit 1
            fi
            if [ -z "$LABELS" ]; then
                echo "WARNING: no running ${LABEL_PREFIX}* agents, nothing to restart"
            else
                SELF_OPS="$SCRIPT_DIR/scripts/self-ops.sh"
                if [ ! -x "$SELF_OPS" ]; then
                    echo "WARNING: self-ops helper missing or not executable: $SELF_OPS"
                    exit 1
                fi
                restart_failed=0
                for label in $LABELS; do
                    if DELAY=0 RESTART_IDLE_WAIT_SECONDS="$RESTART_IDLE_WAIT_SECONDS" \
                            "$SELF_OPS" restart "$label" "$REASON"; then
                        echo "launchd restart queued via self-ops: $label"
                    else
                        echo "ERROR: self-ops restart failed for $label"
                        restart_failed=1
                    fi
                done
                [ "$restart_failed" -eq 0 ] || exit 1
            fi
            ;;
    esac
fi

echo "done. (code=$CODE_CHANGED deps=$DEPS_CHANGED cli=$CLI_CHANGED sdk=$SDK_CHANGED)"
