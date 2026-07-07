#!/usr/bin/env bash
# babata-poll-healthcheck — 检测 4 个 bot 的 TG long-poll 是否 alive,
# stale 就 SIGKILL 让 launchd respawn.
#
# 触发场景: Mac sleep / WiFi 断 / VPN 重连 — bot.py 进程活但 polling
# silently die. PTB 22.7 把 TimedOut 内吞, in-process error_callback 行不通
# (codex review 2026-04-28 验证), 走外部独立通路监控.
#
# Boundary: monitoring cannot depend on the monitored component
# (feedback_monitoring_separation). launchd is the independent supervisor.
#
# 触发: launchd com.babata.poll-watchdog.plist, StartInterval=60.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Pull just PROJECT_STATE_DIR from .env (avoid blanket-exporting tokens).
ENV_FILE="$REPO_DIR/.env"
if [ -f "$ENV_FILE" ] && [ -z "${PROJECT_STATE_DIR:-}" ]; then
    PROJECT_STATE_DIR=$(grep -m1 '^PROJECT_STATE_DIR=' "$ENV_FILE" 2>/dev/null \
        | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r' || true)
    [ -n "$PROJECT_STATE_DIR" ] && export PROJECT_STATE_DIR
fi

LOG_DIR="$HOME/Library/Logs"
STATE_DIR_R="${PROJECT_STATE_DIR:-$REPO_DIR/state}"
STALE_S="${BABATA_POLL_STALE_S:-90}"  # poll heartbeat 多久没更新算 hang.
                                       # 默认 10s, 90s = 9 个周期没动 = 真 hang.
# 一个卡住的 in_flight=1 还能保护多久不被杀. runtime-status 的 ts 在 inflight-enter
# 时打、turn 进行中不刷新, 所以这实际是个"最大 turn 天花板": 我们只有在 poll 心跳已
# stale 时才走到这里, 若此时 in_flight 还=1 且 ts 比这个窗口更老 = turn 跟 polling 一起
# 卡死 → 该杀. 不设上限就是原 critical bug (看门狗在该触发时自我缴械). 兄弟脚本
# self-ops / auto-update 用 120s 是另一用途; 看门狗要够大才不会误杀正常长 turn.
INFLIGHT_STALE_S="${BABATA_INFLIGHT_STALE_S:-600}"
NOW=$(date +%s)
UID_N=$(id -u)
EXIT_CODE=0

# poll heartbeat → process heartbeat → legacy err.log, runtime status, launchd label.
# 只覆盖 TG bot. poll heartbeat 来自 httpx getUpdates 成功记录; process
# heartbeat 只用于新代码发布前的兼容窗口, 防止静默日志变更触发误杀风暴.
# weixin 故意排除: 它的 ilink long-poll 35s + claude 处理期间 stall 是正常行为,
# 实测可见 12 分钟无 log 仍健康 (2026-04-28 验证). 90s 阈值会频繁误杀.
# 此外 weixin 半夜 V 不发消息也无 sleep wake 风险 — 它有 inbound 触发自动恢复.
declare -a CHECKS=(
    "babata-tg-poll-heartbeat|babata-tg-heartbeat|babata|runtime-status-.json|com.babata"
    "babata-tg-vvv-poll-heartbeat|babata-tg-vvv-heartbeat|babata-vvv|runtime-status-vvv.json|com.babata.vvv"
    "babata-tg-vvvv-poll-heartbeat|babata-tg-vvvv-heartbeat|babata-vvvv|runtime-status-vvvv.json|com.babata.vvvv"
    "babata-tg-vvvvv-poll-heartbeat|babata-tg-vvvvv-heartbeat|babata-vvvvv|runtime-status-vvvvv.json|com.babata.vvvvv"
)

for entry in "${CHECKS[@]}"; do
    IFS='|' read -r poll_stem process_stem log_stem runtime_stem label <<< "$entry"
    poll_file="$STATE_DIR_R/$poll_stem"
    process_file="$STATE_DIR_R/$process_stem"
    runtime_file="$STATE_DIR_R/$runtime_stem"
    log_file="$LOG_DIR/$log_stem.err.log"
    sensor_file=""
    sensor_desc=""

    if [[ -f "$poll_file" ]]; then
        sensor_file="$poll_file"
        sensor_desc="$poll_stem"
    elif [[ -f "$process_file" ]]; then
        sensor_file="$process_file"
        sensor_desc="$process_stem (compat)"
    elif [[ -f "$log_file" ]]; then
        sensor_file="$log_file"
        sensor_desc="$log_stem.err.log (legacy)"
    else
        echo "[skip] no health sensor for $label"
        continue
    fi

    # macOS stat -f %m: epoch mtime. coreutils stat 不同, plist env PATH
    # 已确保走 macOS native /usr/bin/stat.
    last_mtime=$(stat -f %m "$sensor_file")
    age=$((NOW - last_mtime))

    if (( age <= STALE_S )); then
        # alive, skip silently (watchdog log 简洁)
        continue
    fi

    if [[ -f "$runtime_file" ]]; then
        in_flight=$(/usr/bin/python3 - "$runtime_file" "$INFLIGHT_STALE_S" <<'PY' 2>/dev/null || printf '0\n'
import json
import sys
import time

try:
    data = json.load(open(sys.argv[1]))
    fresh_window = float(sys.argv[2])
    age = time.time() - float(data.get("ts") or 0)
    busy = int(data.get("in_flight") or 0) > 0 and not data.get("shutdown_requested")
    # A stale in_flight (ts older than the ceiling) means the turn is hung along
    # with polling; do NOT let it suppress the kill — that was the original bug
    # where the watchdog disarmed itself in exactly the hang it exists to catch.
    print(1 if busy and age < fresh_window else 0)
except Exception:
    print(0)
PY
)
        if [[ "$in_flight" == "1" ]]; then
            echo "[$label] ${sensor_desc} ${age}s stale 但 in_flight>0, skip kill"
            continue
        fi
    fi

    # Stale — find current PID via launchctl. 第三列是 label, 第一列 PID.
    pid=$(launchctl list | awk -v lbl="$label" '$3 == lbl {print $1}')
    if [[ -z "$pid" || "$pid" == "-" ]]; then
        echo "[$label] ${sensor_desc} ${age}s stale 但 launchd 无 PID, 可能正在 respawn, skip"
        continue
    fi

    echo "[$label] ${sensor_desc} ${age}s stale (>${STALE_S}s), kill -9 PID $pid → launchd respawn"
    # Restart reason channel: SIGKILL 跳过 graceful shutdown, 所以 bot 在重启
    # 后的 startup alert 里读这个 file 报告. 写在 kill 前确保 file 存在.
    # set -e 防御: 单独一个 group + `|| true`, write 任何环节失败都不阻断 kill —
    # V 至少看到 "上线" alert (只是缺 reason 行), 比 watchdog exit 不杀进程好.
    state_dir_r="$STATE_DIR_R"
    {
        mkdir -p "$state_dir_r" && \
        printf '%s\n' "watchdog: ${sensor_desc} ${age}s stale (>${STALE_S}s), 强杀让 launchd 拉起" \
            > "$state_dir_r/restart-reason-${label}.txt"
    } || echo "[$label] WARN: failed to write restart-reason file, killing anyway"
    if kill -9 "$pid" 2>/dev/null; then
        EXIT_CODE=1  # 标记本轮有干预 (运维监控用)
    else
        echo "[$label] kill failed (PID $pid 已退出?)"
    fi
done

exit $EXIT_CODE
