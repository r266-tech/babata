#!/usr/bin/env bash
# babata self-modification helper — see CLAUDE.md 铁律段.
# 所有会改写 bot 自身 (launchd / claude binary / deps) 的操作走本脚本,
# 内部 `nohup & disown` 脱离 bot 进程管辖, SIGTERM 不连坐.

set -euo pipefail

DELAY="${DELAY:-5}"
UID_N=$(id -u)
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL_PREFIX="com.${PROJECT_NAMESPACE:-babata}"

restart() {
    local label="${1:-$LABEL_PREFIX}"
    nohup bash -c "sleep $DELAY && launchctl kickstart -k gui/$UID_N/$label" >/dev/null 2>&1 &
    disown
    echo "已排队: ${DELAY}s 后 kickstart -k $label"
}

bootstrap_plist() {
    local plist="${1:?plist path required}"
    nohup bash -c "launchctl bootstrap gui/$UID_N '$plist'" >/dev/null 2>&1 &
    disown
    echo "已排队: bootstrap $plist"
}

update_claude() {
    # 走 auto-update.sh 而非 `claude update` — 前者含 npm 防护 / symlink 自愈 / 变更时 kickstart.
    nohup "$REPO_DIR/auto-update.sh" >/dev/null 2>&1 &
    disown
    echo "已排队: auto-update.sh"
}

case "${1:-}" in
    restart)        shift; restart "$@" ;;
    bootstrap)      shift; bootstrap_plist "$@" ;;
    update-claude)  update_claude ;;
    *) echo "Usage: $0 {restart [<label>] | bootstrap <plist> | update-claude}" >&2; exit 1 ;;
esac
