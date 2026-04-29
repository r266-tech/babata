#!/usr/bin/env bash
# babata-poll-healthcheck — 检测 4 个 bot 的 long-poll 是否 alive,
# stale 就 SIGKILL 让 launchd respawn.
#
# 触发场景: Mac sleep / WiFi 断 / VPN 重连 — bot.py 进程活但 polling
# silently die. PTB 22.7 把 TimedOut 内吞, in-process error_callback 行不通
# (codex review 2026-04-28 验证), 走外部独立通路监控.
#
# 哲学: 监控不能依赖被监控组件 (feedback_monitoring_separation). launchd
# 是独立 supervisor, 跟 bot.py 解耦.
#
# 触发: launchd com.babata.poll-watchdog.plist, StartInterval=60.

set -euo pipefail

LOG_DIR="$HOME/Library/Logs"
STALE_S="${BABATA_POLL_STALE_S:-90}"  # log 多久没更新算 hang. long-poll
                                       # 默认 10s, 90s = 9 个周期没动 = 真 hang.
NOW=$(date +%s)
UID_N=$(id -u)
EXIT_CODE=0

# log_stem → launchd label. 只覆盖 3 个 TG bot.
# weixin 故意排除: 它的 ilink long-poll 35s + claude 处理期间 stall 是正常行为,
# 实测可见 12 分钟无 log 仍健康 (2026-04-28 验证). 90s 阈值会频繁误杀.
# 此外 weixin 半夜 V 不发消息也无 sleep wake 风险 — 它有 inbound 触发自动恢复.
declare -a CHECKS=(
    "babata|com.babata"
    "babata-vvv|com.babata.vvv"
    "babata-vvvv|com.babata.vvvv"
)

for entry in "${CHECKS[@]}"; do
    log_stem="${entry%%|*}"
    label="${entry#*|}"
    log_file="$LOG_DIR/$log_stem.err.log"

    if [[ ! -f "$log_file" ]]; then
        echo "[skip] $log_file 不存在"
        continue
    fi

    # macOS stat -f %m: epoch mtime. coreutils stat 不同, plist env PATH
    # 已确保走 macOS native /usr/bin/stat.
    last_mtime=$(stat -f %m "$log_file")
    age=$((NOW - last_mtime))

    if (( age <= STALE_S )); then
        # alive, skip silently (watchdog log 简洁)
        continue
    fi

    # Stale — find current PID via launchctl. 第三列是 label, 第一列 PID.
    pid=$(launchctl list | awk -v lbl="$label" '$3 == lbl {print $1}')
    if [[ -z "$pid" || "$pid" == "-" ]]; then
        echo "[$label] log ${age}s stale 但 launchd 无 PID, 可能正在 respawn, skip"
        continue
    fi

    echo "[$label] log ${age}s stale (>${STALE_S}s), kill -9 PID $pid → launchd respawn"
    if kill -9 "$pid" 2>/dev/null; then
        EXIT_CODE=1  # 标记本轮有干预 (运维监控用)
    else
        echo "[$label] kill failed (PID $pid 已退出?)"
    fi
done

exit $EXIT_CODE
