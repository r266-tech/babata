#!/usr/bin/env bash
# babata auto-update — git pull + uv sync + claude update + restart service.
# Cross-platform (Linux systemd / macOS launchd). Idempotent: 没变化就早退.
# Triggered by systemd timer (Linux) 或 launchd StartCalendarInterval (macOS),
# 配 hourly. install.sh 末尾自动配好.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR" || exit 1

UPGRADE_SDK=0
while [ $# -gt 0 ]; do
    case "$1" in
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
STATE_DIR_R="${PROJECT_STATE_DIR:-$SCRIPT_DIR/state}"
RESTART_IDLE_WAIT_SECONDS="${RESTART_IDLE_WAIT_SECONDS:-3600}"

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

CODE_CHANGED=0
DEPS_CHANGED=0
CLI_CHANGED=0
SDK_CHANGED=0

runtime_file_for_label() {
    local label="$1"
    local instance=""
    if [ "$label" != "$LABEL_PREFIX" ]; then
        instance="${label#"$LABEL_PREFIX".}"
    fi
    printf '%s/runtime-status-%s.json\n' "$STATE_DIR_R" "$instance"
}

running_launchd_labels() {
    launchctl list | awk -v prefix="$LABEL_PREFIX" '
        $1 ~ /^[0-9]+$/ && $3 ~ ("^" prefix "($|[.])") {print $3}
    '
}

wait_runtime_idle() {
    local label="$1"
    local runtime_file
    runtime_file="$(runtime_file_for_label "$label")"
    local deadline=$(( $(date +%s) + RESTART_IDLE_WAIT_SECONDS ))
    while [ -f "$runtime_file" ]; do
        busy=$(python3 - "$runtime_file" <<'PY' 2>/dev/null || echo 0
import json, sys, time
try:
    data=json.load(open(sys.argv[1]))
    fresh=time.time()-float(data.get("ts", 0)) < 120
    print(1 if fresh and int(data.get("in_flight") or 0) > 0 and not data.get("shutdown_requested") else 0)
except Exception:
    print(0)
PY
)
        [ "$busy" != "1" ] && return 0
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "runtime busy for ${RESTART_IDLE_WAIT_SECONDS}s; skip restart: $label"
            return 1
        fi
        sleep 1
    done
    return 0
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

# 3) claude update (CC native installer 自带升级)
if [ -x "$CLAUDE_BIN" ]; then
    OLD_CLI=$("$CLAUDE_BIN" --version 2>/dev/null | awk '{print $1}')
    "$CLAUDE_BIN" update 2>&1 | tail -3 || true
    NEW_CLI=$("$CLAUDE_BIN" --version 2>/dev/null | awk '{print $1}')
    if [ "$OLD_CLI" != "$NEW_CLI" ]; then
        echo "claude: $OLD_CLI -> $NEW_CLI"
        CLI_CHANGED=1
    fi
fi

# 4) Optional SDK upgrade for manual/self-ops/root compatibility paths.
if [ "$UPGRADE_SDK" = "1" ]; then
    if [ -x "$UV" ] && [ -x "$VENV_PY" ]; then
        OLD_SDK=$("$VENV_PY" -c "import claude_agent_sdk; print(claude_agent_sdk.__version__)" 2>/dev/null)
        "$UV" pip install --python "$VENV_PY" --upgrade claude-agent-sdk 2>&1 | tail -5
        "$UV" --directory "$SCRIPT_DIR" lock --upgrade-package claude-agent-sdk 2>&1 | tail -3 \
            || echo "WARN: uv lock failed after claude-agent-sdk upgrade"
        NEW_SDK=$("$VENV_PY" -c "import claude_agent_sdk; print(claude_agent_sdk.__version__)" 2>/dev/null)
        if [ "$OLD_SDK" != "$NEW_SDK" ]; then
            echo "claude-agent-sdk: $OLD_SDK -> $NEW_SDK"
            SDK_CHANGED=1
        fi
    else
        echo "WARN: --upgrade-sdk requested but uv or venv python is missing"
    fi
fi

# 5) 重启 service (代码 / deps / cli / sdk 任一变了)
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

    case "$(uname -s)" in
        Linux)
            if command -v systemctl >/dev/null 2>&1; then
                systemctl --user restart "$SERVICE" 2>&1 && echo "systemd restarted: $SERVICE"
            fi
            ;;
        Darwin)
            LABELS=$(running_launchd_labels)
            if [ -z "$LABELS" ]; then
                echo "WARNING: no running ${LABEL_PREFIX}* agents, nothing to restart"
            else
                for label in $LABELS; do
                    if [ "$label" != "com.${PROJECT_NAMESPACE}.weixin" ]; then
                        mkdir -p "$STATE_DIR_R" 2>/dev/null && \
                            printf '%s\n' "$REASON" > "$STATE_DIR_R/restart-reason-${label}.txt"
                    fi
                    if wait_runtime_idle "$label"; then
                        launchctl kickstart -k "gui/$UID/$label" && echo "launchd kickstarted: $label"
                    fi
                done
            fi
            ;;
    esac
fi

echo "done. (code=$CODE_CHANGED deps=$DEPS_CHANGED cli=$CLI_CHANGED sdk=$SDK_CHANGED)"
