#!/usr/bin/env bash
# babata self-maintenance helper.
# Bot launchd/dependency/CPU-binary mutations go through this detached path so
# service SIGTERM does not interrupt the active turn.

set -euo pipefail

DELAY="${DELAY:-5}"
UID_N=$(id -u)
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Pull just PROJECT_STATE_DIR from .env (avoid blanket-exporting tokens).
ENV_FILE="$REPO_DIR/.env"
if [ -f "$ENV_FILE" ] && [ -z "${PROJECT_STATE_DIR:-}" ]; then
    PROJECT_STATE_DIR=$(grep -m1 '^PROJECT_STATE_DIR=' "$ENV_FILE" 2>/dev/null \
        | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r' || true)
    [ -n "$PROJECT_STATE_DIR" ] && export PROJECT_STATE_DIR
fi

LABEL_PREFIX="com.${PROJECT_NAMESPACE:-babata}"
RESTART_IDLE_WAIT_SECONDS="${RESTART_IDLE_WAIT_SECONDS:-3600}"
DISABLE_WAIT_SECONDS="${DISABLE_WAIT_SECONDS:-60}"
ENABLE_ROLLBACK_WAIT_SECONDS="${ENABLE_ROLLBACK_WAIT_SECONDS:-15}"
LAUNCHCTL_BIN="${BABATA_LAUNCHCTL:-launchctl}"
PLIST_IDENTITY_BIN="$REPO_DIR/scripts/plist-identity.py"
SELF_OPS_RESULT_DIR="${SELF_OPS_RESULT_DIR:-${PROJECT_STATE_DIR:-$REPO_DIR/state}/self-ops}"

runtime_file_for_label() {
    local label="$1"
    local prefix="$LABEL_PREFIX"
    local instance=""
    if [ "$label" != "$prefix" ]; then
        instance="${label#"$prefix".}"
    fi
    local state_dir="${PROJECT_STATE_DIR:-$REPO_DIR/state}"
    printf '%s/runtime-status-%s.json\n' "$state_dir" "$instance"
}

restart() {
    local label="${1:-$LABEL_PREFIX}"
    shift || true
    local reason="${*:-manual: self-ops restart}"

    # Self-suicide guard: if the caller is itself running under the launchd
    # service we are about to kickstart -k, we'd SIGKILL our own ancestor before
    # the active turn finishes. Detect by walking the parent PID chain looking
    # for the launchd-managed bot process. If found, refuse inline restart and
    # defer (e.g. via TG /restart, daily-restart 4am, or `at now+1m`).
    #
    # Heuristic: launchctl list <label> reports the live PID. Walk our own
    # process ancestry (ps -o ppid=) up to PID 1; if any ancestor matches
    # the live PID, we're inside the target service. 5-step depth cap
    # guards against ps loops; macOS PID space is small but bounded walks
    # are cheap.
    # `|| true` on both pipeline assignments below is critical:
    # under `set -euo pipefail` (line 6), a non-zero exit from
    # launchctl/ps would abort the function before the guard logic
    # could reach kickstart — turning a probe failure into a silent restart
    # failure.
    local target_pid
    target_pid=$(launchctl list "$label" 2>/dev/null | awk -F'"' '/"PID"/{print $3; exit}' | tr -d '=; \t') || true
    if [ -n "$target_pid" ] && [ "$target_pid" != "-" ]; then
        local p=$$
        local depth=0
        while [ "$p" != "1" ] && [ "$p" != "0" ] && [ "$depth" -lt 12 ]; do
            if [ "$p" = "$target_pid" ]; then
                echo "DETACH: caller runs under $label; scheduling out-of-band helper." >&2
                break
            fi
            # Dead ancestor mid-walk → ps returns non-zero. `|| break`
            # turns that into "stop walking, proceed to kickstart"
            # rather than letting errexit abort the whole script.
            p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ') || break
            [ -z "$p" ] && break
            depth=$((depth + 1))
        done
    fi

    # Restart reason channel: bot reads STATE_DIR/restart-reason-{label}.txt at
    # graceful shutdown to surface trigger in TG alert. Wrap in best-effort
    # group — under set -euo pipefail, an mkdir/printf failure must NOT abort
    # the kickstart below. Worst case: V loses the reason line, still sees
    # restart alert.
    #
    # Skip wx label: weixin_bot has no TG alert consumer for the restart-reason
    # channel — writing the file would be unread + stale-on-first-consume hazard
    # if a wx alert is added later. Same skip lives in babata-daily-restart.sh
    # and babata-daily-restart.sh.
    if [[ "$label" == "$LABEL_PREFIX" || "$label" == "$LABEL_PREFIX".* ]] && \
            [ "$label" != "${LABEL_PREFIX}.weixin" ]; then
        local state_dir="${PROJECT_STATE_DIR:-$REPO_DIR/state}"
        {
            mkdir -p "$state_dir" && \
            printf '%s\n' "$reason" > "$state_dir/restart-reason-${label}.txt"
        } || echo "WARN: failed to write restart-reason file, kickstarting anyway"
    fi
    local runtime_file
    runtime_file="$(runtime_file_for_label "$label")"
    local helper
    helper="${label}.restart.$(date +%s).$$"
    launchctl submit -l "$helper" -- /bin/bash -lc '
        delay="$1"
        uid_n="$2"
        label="$3"
        helper="$4"
        runtime_file="$5"
        wait_s="$6"
        sleep "$delay"
        deadline=$(( $(date +%s) + wait_s ))
        while [ -f "$runtime_file" ]; do
            busy=$(python3 - "$runtime_file" <<'"'"'PY'"'"' 2>/dev/null || echo 0
import json, sys, time
try:
    data=json.load(open(sys.argv[1]))
    fresh=time.time()-float(data.get("ts", 0)) < 120
    print(1 if fresh and int(data.get("in_flight") or 0) > 0 else 0)
except Exception:
    print(0)
PY
)
            [ "$busy" != "1" ] && break
            if [ "$(date +%s)" -ge "$deadline" ]; then
                echo "WARN: $label still in_flight after ${wait_s}s; skip restart"
                launchctl remove "$helper" >/dev/null 2>&1 || true
                exit 1
            fi
            sleep 1
        done
        launchctl kickstart -k "gui/$uid_n/$label"
        launchctl remove "$helper" >/dev/null 2>&1 || true
    ' bash "$DELAY" "$UID_N" "$label" "$helper" "$runtime_file" "$RESTART_IDLE_WAIT_SECONDS"
    echo "已排队: ${DELAY}s 后 kickstart -k $label"
}

reload_plist() {
    local label="${1:-$LABEL_PREFIX}"
    local plist="${2:-$HOME/Library/LaunchAgents/${label}.plist}"
    if [ ! -f "$plist" ]; then
        echo "ERR: plist not found: $plist" >&2
        return 1
    fi
    if [[ "$label" == "$LABEL_PREFIX" || "$label" == "$LABEL_PREFIX".* ]] && \
            [ "$label" != "${LABEL_PREFIX}.weixin" ]; then
        local state_dir="${PROJECT_STATE_DIR:-$REPO_DIR/state}"
        {
            mkdir -p "$state_dir" && \
            printf '%s\n' "manual: self-ops reload-plist" > "$state_dir/restart-reason-${label}.txt"
        } || echo "WARN: failed to write restart-reason file, reloading anyway"
    fi
    local helper
    helper="${label}.reload.$(date +%s).$$"
    local runtime_file
    runtime_file="$(runtime_file_for_label "$label")"
    local pending_file=""
    if [ "$label" = "${LABEL_PREFIX}.weixin" ]; then
        pending_file="${PROJECT_STATE_DIR:-$REPO_DIR/state}/${PROJECT_NAMESPACE:-babata}-weixin-pending-updates.json"
    fi
    "$LAUNCHCTL_BIN" submit -l "$helper" -- /bin/bash -lc '
        delay="$1"
        uid_n="$2"
        label="$3"
        plist="$4"
        helper="$5"
        runtime_file="$6"
        wait_s="$7"
        launchctl_bin="$8"
        pending_file="$9"
        sleep "$delay"
        deadline=$(( $(date +%s) + wait_s ))
        while true; do
            busy=$(python3 - "$runtime_file" "$pending_file" <<'"'"'PY'"'"' 2>/dev/null || echo 1
import json, os, pathlib, sys
try:
    runtime=pathlib.Path(sys.argv[1])
    data=json.loads(runtime.read_text()) if runtime.exists() else {}
    alive=False
    if data.get("pid"):
        try:
            os.kill(int(data["pid"]), 0)
            alive=True
        except ProcessLookupError:
            pass
    pending=pathlib.Path(sys.argv[2]) if sys.argv[2] else None
    queued=json.loads(pending.read_text()).get("pending", {}) if pending and pending.exists() else {}
    print(1 if (alive and int(data.get("in_flight") or 0) > 0) or queued else 0)
except Exception:
    print(1)
PY
)
            [ "$busy" != "1" ] && break
            if [ "$(date +%s)" -ge "$deadline" ]; then
                echo "WARN: $label still in_flight after ${wait_s}s; skip reload"
                "$launchctl_bin" remove "$helper" >/dev/null 2>&1 || true
                exit 1
            fi
            sleep 1
        done
        "$launchctl_bin" bootout "gui/$uid_n/$label" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
            "$launchctl_bin" print "gui/$uid_n/$label" >/dev/null 2>&1 || break
            sleep 1
        done
        if "$launchctl_bin" print "gui/$uid_n/$label" >/dev/null 2>&1; then
            echo "WARN: $label still loaded after bootout; skip bootstrap"
            "$launchctl_bin" remove "$helper" >/dev/null 2>&1 || true
            exit 1
        fi
        "$launchctl_bin" bootstrap "gui/$uid_n" "$plist"
        "$launchctl_bin" remove "$helper" >/dev/null 2>&1 || true
    ' bash "$DELAY" "$UID_N" "$label" "$plist" "$helper" "$runtime_file" "$RESTART_IDLE_WAIT_SECONDS" "$LAUNCHCTL_BIN" "$pending_file"
    echo "已排队: ${DELAY}s 后 reload $label from $plist"
}

enable_full_trust() {
    if [ "$#" -ne 1 ]; then
        echo "ERR: enable-full-trust requires one channel service label" >&2
        return 2
    fi
    configure_channel "$1" ""
}

set_reasoning() {
    if [ "$#" -ne 2 ] || [ -z "$2" ]; then
        echo "ERR: set-reasoning requires a channel service label and reasoning level" >&2
        return 2
    fi
    configure_channel "$1" "$2"
}

set_model() {
    if [ "$#" -lt 2 ] || [ "$#" -gt 3 ] || [ -z "$2" ]; then
        echo "ERR: set-model requires a channel service label, model ID, and optional reasoning level" >&2
        return 2
    fi
    configure_channel "$1" "${3:-}" "$2"
}

configure_channel() {
    local label="$1"
    local reasoning="${2:-}"
    local model="${3:-}"
    local operation="full-trust"
    [ -z "$reasoning" ] || operation="reasoning"
    [ -z "$model" ] || operation="model"
    local runner="$REPO_DIR/scripts/full-trust.py"
    local python="$REPO_DIR/.venv/bin/python"
    "$python" "$runner" validate "$REPO_DIR" "$LABEL_PREFIX" "$label" --reasoning "$reasoning" --model "$model" || return $?
    local result_dir="$SELF_OPS_RESULT_DIR/full-trust"
    (umask 077; mkdir -p "$result_dir")
    local helper="${label}.${operation}.$(date +%s).$$"
    local receipt="$result_dir/$helper.json"
    local run_log="$result_dir/$helper.log"
    (umask 077; : > "$run_log")
    "$LAUNCHCTL_BIN" submit -l "$helper" -o "$run_log" -e "$run_log" -- /bin/bash -c '
        umask 077
        "$1" "$2" apply "$3" "$4" "$5" --receipt "$6" --launchctl "$7" --wait "$9" --delay "${10}" --state "${11}" --reasoning "${12}" --model "${13}"
        "$7" remove "$8" >/dev/null 2>&1 || true
        exit 0
    ' bash "$python" "$runner" "$REPO_DIR" "$LABEL_PREFIX" "$label" "$receipt" "$LAUNCHCTL_BIN" "$helper" "$RESTART_IDLE_WAIT_SECONDS" "$DELAY" "${PROJECT_STATE_DIR:-$REPO_DIR/state}" "$reasoning" "$model"
    echo "已排队: $operation $label; receipt=$receipt"
}

bootstrap_plist() {
    local plist="${1:?plist path required}"
    nohup bash -c "launchctl bootstrap gui/$UID_N '$plist'" >/dev/null 2>&1 &
    disown
    echo "已排队: bootstrap $plist"
}

kickstart_system() {
    local label="${1:?system launchd label required}"
    if [ "$#" -ne 1 ]; then
        echo "ERR: kickstart-system accepts exactly one launchd label" >&2
        return 1
    fi
    case "$label" in
        *[!A-Za-z0-9._-]*|'')
            echo "ERR: invalid system launchd label: $label" >&2
            return 1
            ;;
    esac
    "$LAUNCHCTL_BIN" kickstart -k "system/$label"
    echo "已 kickstart -k system/$label"
}

disable_plist() {
    local label="${1:?launchd label required}"
    local plist="${2:-$HOME/Library/LaunchAgents/${label}.plist}"
    local disabled_plist="${plist}.disabled"
    if [ ! -f "$plist" ]; then
        echo "ERR: plist not found: $plist" >&2
        return 1
    fi
    if [ -e "$disabled_plist" ]; then
        echo "ERR: disabled plist already exists: $disabled_plist" >&2
        return 1
    fi

    local helper
    helper="${label}.disable.$(date +%s).$$"
    launchctl submit -l "$helper" -- /bin/bash -lc '
        set -u
        delay="$1"
        uid_n="$2"
        label="$3"
        plist="$4"
        disabled_plist="$5"
        helper="$6"
        wait_s="$7"
        domain="gui/$uid_n"
        sleep "$delay"

        # Disable first so a periodic job cannot restart while an existing
        # invocation gets a bounded chance to exit naturally.
        if ! launchctl disable "$domain/$label"; then
            echo "ERROR: failed to disable $domain/$label" >&2
            launchctl remove "$helper" >/dev/null 2>&1 || true
            exit 1
        fi
        deadline=$(( $(date +%s) + wait_s ))
        while launchctl print "$domain/$label" 2>/dev/null | grep -q "pid ="; do
            if [ "$(date +%s)" -ge "$deadline" ]; then
                echo "WARN: $label still running after ${wait_s}s; left disabled, not killed" >&2
                launchctl remove "$helper" >/dev/null 2>&1 || true
                exit 1
            fi
            sleep 1
        done

        launchctl bootout "$domain/$label" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            launchctl print "$domain/$label" >/dev/null 2>&1 || break
            sleep 1
        done
        if launchctl print "$domain/$label" >/dev/null 2>&1; then
            echo "WARN: $label still loaded after bootout; plist left in place" >&2
            launchctl remove "$helper" >/dev/null 2>&1 || true
            exit 1
        fi

        if ! mv "$plist" "$disabled_plist"; then
            echo "ERROR: failed to preserve disabled plist at $disabled_plist" >&2
            launchctl remove "$helper" >/dev/null 2>&1 || true
            exit 1
        fi
        launchctl remove "$helper" >/dev/null 2>&1 || true
    ' bash "$DELAY" "$UID_N" "$label" "$plist" "$disabled_plist" "$helper" "$DISABLE_WAIT_SECONDS"
    echo "已排队: disable $label; plist 将保留为 $disabled_plist"
}

enable_plist() {
    local label="${1:?launchd label required}"
    local plist="${2:-$HOME/Library/LaunchAgents/${label}.plist}"
    local disabled_plist="${plist}.disabled"
    if [ -e "$plist" ]; then
        echo "ERR: active plist already exists: $plist" >&2
        return 1
    fi
    if [ ! -f "$disabled_plist" ]; then
        echo "ERR: disabled plist not found: $disabled_plist" >&2
        return 1
    fi
    case "$ENABLE_ROLLBACK_WAIT_SECONDS" in
        ''|*[!0-9]*)
            echo "ERR: ENABLE_ROLLBACK_WAIT_SECONDS must be an integer from 0 to 300" >&2
            return 1
            ;;
    esac
    if [ "$ENABLE_ROLLBACK_WAIT_SECONDS" -gt 300 ]; then
        echo "ERR: ENABLE_ROLLBACK_WAIT_SECONDS must be an integer from 0 to 300" >&2
        return 1
    fi

    local identity disabled_sha declared_label tab
    if ! identity=$(PYTHONDONTWRITEBYTECODE=1 python3 "$PLIST_IDENTITY_BIN" "$disabled_plist"); then
        echo "ERR: disabled plist is invalid or has no Label: $disabled_plist" >&2
        return 1
    fi
    tab=$(printf "\t")
    disabled_sha=${identity%%"$tab"*}
    declared_label=${identity#*"$tab"}
    if [ "$declared_label" != "$label" ]; then
        echo "ERR: disabled plist Label is $declared_label, expected $label" >&2
        return 1
    fi

    local helper result_path
    helper="${label}.enable.$(date +%s).$$"
    if ! mkdir -p "$SELF_OPS_RESULT_DIR" || ! chmod 700 "$SELF_OPS_RESULT_DIR"; then
        echo "ERR: cannot prepare self-ops result directory: $SELF_OPS_RESULT_DIR" >&2
        return 1
    fi
    result_path="$SELF_OPS_RESULT_DIR/${helper}.result"
    "$LAUNCHCTL_BIN" submit -l "$helper" -- /bin/bash -lc '
        set -u
        umask 077
        delay="$1"
        uid_n="$2"
        label="$3"
        plist="$4"
        disabled_plist="$5"
        helper="$6"
        launchctl_bin="$7"
        rollback_wait_s="$8"
        expected_sha="$9"
        identity_bin="${10}"
        result_path="${11}"
        domain="gui/$uid_n"
        staging_plist="${plist}.enable-staging.${helper}"
        backup_plist="${plist}.enable-backup.${helper}"
        rejected_prefix="${plist}.enable-rejected.${helper}"
        tab=$(printf "\t")
        completed=0
        mutation_started=0
        launchctl_touched=0
        active_sha=""

        plist_identity() {
            PYTHONDONTWRITEBYTECODE=1 python3 "$identity_bin" "$1"
        }
        write_result() {
            local status="$1" stage="$2" detail="$3" cleanup_state="$4" tmp
            tmp="${result_path}.tmp.$$"
            {
                printf "status=%s\n" "$status"
                printf "stage=%s\n" "$stage"
                printf "detail=%s\n" "$detail"
                printf "helper_cleanup=%s\n" "$cleanup_state"
                printf "label=%s\n" "$label"
                printf "expected_sha=%s\n" "$expected_sha"
                printf "active_sha=%s\n" "$active_sha"
            } > "$tmp" && mv "$tmp" "$result_path"
        }
        finish() {
            local status="$1" stage="$2" detail="$3" cleanup_state="queued"
            completed=1
            write_result "$status" "$stage" "$detail" pending || true
            if [ "$status" = success ]; then
                if ! rm -f "$backup_plist" || [ -e "$backup_plist" ]; then
                    status=error
                    stage=backup_cleanup_failed
                    detail="service loaded but immutable plist backup could not be removed"
                    write_result "$status" "$stage" "$detail" pending || true
                else
                    mutation_started=0
                fi
            fi
            if ! "$launchctl_bin" remove "$helper" >/dev/null 2>&1; then
                cleanup_state=failed
            fi
            write_result "$status" "$stage" "$detail" "$cleanup_state" || true
            trap - EXIT
            exit 0
        }
        preserve_candidate() {
            local candidate="$1" suffix="$2" identity sha candidate_label rejected
            [ -e "$candidate" ] || return 0
            identity=$(plist_identity "$candidate" 2>/dev/null || true)
            sha=${identity%%"$tab"*}
            candidate_label=${identity#*"$tab"}
            if [ -n "$sha" ] && [ "$sha" = "$expected_sha" ] \
                    && [ "$candidate_label" = "$label" ]; then
                rm -f "$candidate"
                return $?
            fi
            rejected="${rejected_prefix}.${suffix}"
            if [ -e "$rejected" ] || ! mv "$candidate" "$rejected"; then
                echo "ERROR: failed to quarantine changed plist from $candidate" >&2
                return 1
            fi
            chmod 600 "$rejected" 2>/dev/null || true
            echo "WARN: changed plist quarantined at $rejected" >&2
        }
        rollback() {
            local rollback_failed=0 deadline backup_identity backup_sha backup_label restored_identity restored_sha restored_label
            backup_identity=$(plist_identity "$backup_plist" 2>/dev/null || true)
            backup_sha=${backup_identity%%"$tab"*}
            backup_label=${backup_identity#*"$tab"}
            if [ -z "$backup_sha" ] || [ "$backup_sha" != "$expected_sha" ] \
                    || [ "$backup_label" != "$label" ]; then
                echo "ERROR: immutable plist backup is missing or changed; preserving current state" >&2
                return 1
            fi
            if [ "$launchctl_touched" -eq 1 ]; then
                if ! "$launchctl_bin" disable "$domain/$label"; then
                    echo "ERROR: rollback could not disable $domain/$label" >&2
                    rollback_failed=1
                fi
                "$launchctl_bin" bootout "$domain/$label" >/dev/null 2>&1 || true
                deadline=$(( $(date +%s) + rollback_wait_s ))
                while "$launchctl_bin" print "$domain/$label" >/dev/null 2>&1; do
                    if [ "$(date +%s)" -ge "$deadline" ]; then
                        echo "ERROR: rollback left $label loaded; active plist preserved at $plist" >&2
                        return 1
                    fi
                    sleep 1
                done
            fi
            preserve_candidate "$plist" active || return 1
            preserve_candidate "$staging_plist" staging || return 1
            preserve_candidate "$disabled_plist" disabled || return 1
            if ! mv "$backup_plist" "$disabled_plist" || ! chmod 600 "$disabled_plist"; then
                echo "ERROR: failed to restore immutable plist backup at $disabled_plist" >&2
                return 1
            fi
            restored_identity=$(plist_identity "$disabled_plist" 2>/dev/null || true)
            restored_sha=${restored_identity%%"$tab"*}
            restored_label=${restored_identity#*"$tab"}
            if [ "$restored_sha" != "$expected_sha" ] || [ "$restored_label" != "$label" ] \
                    || [ -e "$plist" ] || [ -e "$staging_plist" ] || [ -e "$backup_plist" ]; then
                echo "ERROR: rollback plist identity invariant failed" >&2
                return 1
            fi
            active_sha=""
            if [ "$rollback_failed" -eq 0 ]; then
                mutation_started=0
            fi
            return "$rollback_failed"
        }
        unexpected_exit() {
            local rc=$? stage=unexpected_exit detail cleanup_state=queued
            [ "${BASH_SUBSHELL:-0}" -eq 0 ] || return 0
            if [ "$completed" -eq 1 ]; then
                trap - EXIT HUP INT TERM
                exit 0
            fi
            completed=1
            detail="worker exited unexpectedly rc=$rc before plist mutation"
            if [ "$mutation_started" -eq 1 ]; then
                if rollback; then
                    stage=unexpected_exit_rolled_back
                    detail="worker exited unexpectedly rc=$rc; rollback restored the validated plist"
                else
                    stage=rollback_incomplete
                    detail="worker exited unexpectedly rc=$rc; rollback could not be proven"
                fi
            elif ! rm -f "$backup_plist"; then
                stage=cleanup_incomplete
                detail="worker exited unexpectedly rc=$rc; pre-mutation backup cleanup failed"
            fi
            write_result error "$stage" "$detail" pending || true
            if ! "$launchctl_bin" remove "$helper" >/dev/null 2>&1; then
                cleanup_state=failed
            fi
            write_result error "$stage" "$detail" "$cleanup_state" || true
            trap - EXIT HUP INT TERM
            exit 0
        }
        trap unexpected_exit EXIT
        trap "exit 129" HUP
        trap "exit 130" INT
        trap "exit 143" TERM
        sleep "$delay"
        if [ -e "$plist" ] || [ -e "$staging_plist" ] || [ ! -f "$disabled_plist" ]; then
            echo "ERROR: plist state changed before enabling $label" >&2
            finish error preflight_state_changed "plist state changed before enable"
        fi
        if "$launchctl_bin" print "$domain/$label" >/dev/null 2>&1; then
            echo "ERROR: $label is already loaded; disabled plist left untouched" >&2
            finish error preflight_loaded "target was already loaded"
        fi
        if ! cp "$disabled_plist" "$backup_plist" || ! chmod 600 "$backup_plist"; then
            rm -f "$backup_plist" 2>/dev/null || true
            echo "ERROR: failed to create immutable plist backup for $label" >&2
            finish error backup_failed "validated plist backup could not be created"
        fi
        backup_identity=$(plist_identity "$backup_plist" 2>/dev/null || true)
        backup_sha=${backup_identity%%"$tab"*}
        backup_label=${backup_identity#*"$tab"}
        if [ -z "$backup_sha" ] || [ "$backup_sha" != "$expected_sha" ] \
                || [ "$backup_label" != "$label" ]; then
            rm -f "$backup_plist" 2>/dev/null || true
            echo "ERROR: disabled plist identity changed while creating backup for $label" >&2
            finish error identity_changed "backup did not match the validated identity"
        fi
        mutation_started=1
        if ! mv "$disabled_plist" "$staging_plist"; then
            echo "ERROR: failed to stage disabled plist at $staging_plist" >&2
            if rollback; then
                finish error stage_failed "disabled plist could not be staged; rollback complete"
            fi
            finish error rollback_incomplete "disabled plist could not be staged and rollback was incomplete"
        fi
        current_identity=$(plist_identity "$staging_plist" 2>/dev/null || true)
        current_sha=${current_identity%%"$tab"*}
        current_label=${current_identity#*"$tab"}
        if [ -z "$current_sha" ] || [ "$current_sha" != "$expected_sha" ] \
                || [ "$current_label" != "$label" ]; then
            echo "ERROR: disabled plist identity changed before enabling $label" >&2
            if rollback; then
                finish error identity_changed "staged plist did not match the validated identity"
            fi
            echo "ERROR: enable rollback incomplete for $label; manual inspection required" >&2
            finish error rollback_incomplete "identity changed and rollback was incomplete"
        fi
        if ! mv "$staging_plist" "$plist" || ! chmod 600 "$plist"; then
            echo "ERROR: failed to install verified active plist at $plist" >&2
            if rollback; then
                finish error install_failed "verified plist could not be installed"
            fi
            finish error rollback_incomplete "verified plist install failed and rollback was incomplete"
        fi
        launchctl_touched=1
        if ! "$launchctl_bin" enable "$domain/$label"; then
            echo "ERROR: failed to enable $domain/$label; rolling back" >&2
            if rollback; then
                finish error enable_failed "persistent enable failed; rollback complete"
            fi
            echo "ERROR: enable rollback incomplete for $label; manual inspection required" >&2
            finish error rollback_incomplete "persistent enable failed and rollback was incomplete"
        fi
        active_identity=$(plist_identity "$plist" 2>/dev/null || true)
        active_sha=${active_identity%%"$tab"*}
        active_label=${active_identity#*"$tab"}
        if [ "$active_sha" != "$expected_sha" ] || [ "$active_label" != "$label" ]; then
            echo "ERROR: active plist identity changed before bootstrap $label" >&2
            if rollback; then
                finish error active_identity_changed "active plist changed before bootstrap"
            fi
            finish error rollback_incomplete "active plist changed and rollback was incomplete"
        fi
        if ! "$launchctl_bin" bootstrap "$domain" "$plist"; then
            echo "ERROR: failed to bootstrap $label; rolling back" >&2
            if rollback; then
                finish error bootstrap_failed "bootstrap failed; rollback complete"
            fi
            echo "ERROR: enable rollback incomplete for $label; manual inspection required" >&2
            finish error rollback_incomplete "bootstrap failed and rollback was incomplete"
        fi
        if ! "$launchctl_bin" print "$domain/$label" >/dev/null 2>&1; then
            echo "ERROR: $label not loaded after bootstrap; rolling back" >&2
            if rollback; then
                finish error loaded_verification_failed "job was not loaded; rollback complete"
            fi
            echo "ERROR: enable rollback incomplete for $label; manual inspection required" >&2
            finish error rollback_incomplete "loaded verification failed and rollback was incomplete"
        fi
        final_identity=$(plist_identity "$plist" 2>/dev/null || true)
        final_sha=${final_identity%%"$tab"*}
        final_label=${final_identity#*"$tab"}
        if [ "$final_sha" != "$expected_sha" ] || [ "$final_label" != "$label" ]; then
            echo "ERROR: active plist identity changed after bootstrap $label" >&2
            if rollback; then
                finish error final_identity_changed "active plist changed after bootstrap"
            fi
            finish error rollback_incomplete "post-bootstrap identity changed and rollback was incomplete"
        fi
        active_sha="$final_sha"
        finish success loaded "verified plist enabled, bootstrapped, and loaded"
    ' bash "$DELAY" "$UID_N" "$label" "$plist" "$disabled_plist" "$helper" \
        "$LAUNCHCTL_BIN" "$ENABLE_ROLLBACK_WAIT_SECONDS" "$disabled_sha" \
        "$PLIST_IDENTITY_BIN" "$result_path"
    echo "已排队: enable $label from $disabled_plist"
    echo "result: $result_path"
}

sync_wx_deps() (
    # Install only the two optional WeChat packages. A full project sync can
    # downgrade unrelated packages in the live bot environment.
    local uv_bin="${BABATA_UV:-uv}"
    local python_bin="$REPO_DIR/.venv/bin/python"
    local requirements
    case "${1:-}" in
        '') [ "$#" -eq 0 ] || return 2 ;;
        --dry-run) [ "$#" -eq 1 ] || return 2 ;;
        *) echo "ERR: sync-wx-deps accepts only --dry-run" >&2; return 2 ;;
    esac
    if [ ! -x "$python_bin" ]; then
        echo "ERR: bot Python not found: $python_bin" >&2
        return 1
    fi
    requirements=$(mktemp "${TMPDIR:-/tmp}/babata-wx-requirements.XXXXXX")
    trap 'rm -f "$requirements"' EXIT
    "$python_bin" - "$REPO_DIR/uv.lock" > "$requirements" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as handle:
    packages = tomllib.load(handle)["package"]
for name in ("pilk", "qrcode"):
    matches = [package for package in packages if package["name"] == name]
    if len(matches) != 1:
        raise SystemExit(f"ERR: expected exactly one {name} version in uv.lock")
    print(f"{name}=={matches[0]['version']}")
PY
    unset UV_INDEX_URL PIP_INDEX_URL UV_EXTRA_INDEX_URL PIP_EXTRA_INDEX_URL
    "$uv_bin" --no-config pip install --python "$python_bin" --no-deps \
        --index-url https://pypi.org/simple --requirements "$requirements" "$@"
)

update_claude() {
    # 走 canonical auto-update 脚本而非 `claude update`; install.sh 也注册这一路径.
    nohup "$REPO_DIR/scripts/auto-update.sh" --upgrade-claude --upgrade-sdk >/dev/null 2>&1 &
    disown
    echo "已排队: scripts/auto-update.sh"
}

update_tools() {
    # Package/CPU updates outlive their calling channel or scheduled wrapper.
    # The deterministic owner records actual outcomes; dispatch is not success.
    local selected=()
    local strict_plan=()
    local update_timeout=()
    while [ "$#" -gt 0 ]; do
        if [ "$1" = --strict-brew-plan ]; then
            if [ "$#" -lt 2 ] || [ "${#strict_plan[@]}" -ne 0 ] || [[ "$2" != /* ]] || [ ! -f "$2" ] || [ -L "$2" ]; then
                echo "ERR: --strict-brew-plan requires one regular absolute plan path" >&2
                return 2
            fi
            strict_plan+=(--strict-brew-plan "$2")
        elif [ "$1" = --timeout ]; then
            if [ "$#" -lt 2 ] || [ "${#update_timeout[@]}" -ne 0 ] || ! [[ "$2" =~ ^[1-9][0-9]*$ ]]; then
                echo "ERR: --timeout requires one positive integer number of seconds" >&2
                return 2
            fi
            update_timeout+=(--timeout "$2")
        elif [ "$1" = --only ] && [ "$#" -ge 2 ] && [ -n "$2" ] && [[ "$2" != -* ]]; then
            selected+=(--only "$2")
        else
            echo "ERR: update-tools accepts --only <manager-or-item-id>, --strict-brew-plan <path>, and --timeout <seconds>" >&2
            return 2
        fi
        shift 2
    done
    if [ "${#strict_plan[@]}" -ne 0 ] && [ "${#selected[@]}" -eq 0 ]; then
        echo "ERR: --strict-brew-plan requires exact --only Brew targets" >&2
        return 2
    fi
    local maintenance_state="${BABATA_TOOL_MAINTAIN_STATE_DIR:-$HOME/cc-workspace/state/tool-maintenance}"
    if ! PYTHONDONTWRITEBYTECODE=1 python3 "$HOME/cc-workspace/tools/tool-maintenance/scope_hold.py" \
        "$maintenance_state" ${selected[@]+"${selected[@]}"}; then
        echo "维护范围尚未放行；读取 $maintenance_state/hold.json" >&2
        return 75
    fi
    local runner="${BABATA_TOOL_MAINTAIN_BIN:-$HOME/cc-workspace/bin/babata-tool-maintain}"
    local log_dir="${BABATA_TOOL_MAINTAIN_LOG_DIR:-$HOME/cc-workspace/logs/tool-maintenance}"
    if [ ! -x "$runner" ]; then
        echo "ERR: tool maintenance owner missing: $runner" >&2
        return 1
    fi
    mkdir -p "$log_dir" || return 1
    chmod 700 "$log_dir" || return 1
    local request_key
    request_key="$(python3 -c 'import hashlib,json,sys; a=sys.argv[1:]; print(hashlib.sha256(json.dumps(sorted(zip(a[::2],a[1::2]))).encode()).hexdigest()[:24])' ${selected[@]+"${selected[@]}"} ${strict_plan[@]+"${strict_plan[@]}"} ${update_timeout[@]+"${update_timeout[@]}"})" || return 2
    local helper="com.babata.tool-maintenance.$request_key"
    if "$LAUNCHCTL_BIN" print "gui/$(id -u)/$helper" >/dev/null 2>&1; then
        echo "已合并到现有维护请求: $helper; 用 babata-tool-maintain status 读取实际结果"
        return 0
    fi
    local run_log="$log_dir/${helper}.$(date +%s).$$.log"
    (umask 077; : > "$run_log") || return 1
    if ! "$LAUNCHCTL_BIN" submit -l "$helper" -o "$run_log" -e "$run_log" -- \
        /bin/bash -c '
            umask 077
            export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            status=0
            PYTHONDONTWRITEBYTECODE=1 BABATA_TOOL_MAINTAIN_DISPATCHED=1 "$1" run "${@:4}" || status=$?
            printf "tool maintenance finished; exit=%s; inspect owner receipt\n" "$status"
            "$2" remove "$3" >/dev/null 2>&1 || true
            exit 0
        ' bash "$runner" "$LAUNCHCTL_BIN" "$helper" ${selected[@]+"${selected[@]}"} ${strict_plan[@]+"${strict_plan[@]}"} ${update_timeout[@]+"${update_timeout[@]}"}; then
        if "$LAUNCHCTL_BIN" print "gui/$(id -u)/$helper" >/dev/null 2>&1; then
            echo "已合并到并发维护请求: $helper; 用 babata-tool-maintain status 读取实际结果"
            return 0
        fi
        echo "ERR: tool maintenance dispatch failed" >&2
        return 1
    fi
    echo "已排队: $helper; 用 babata-tool-maintain status 读取实际结果"
}

case "${1:-}" in
    restart)        shift; restart "$@" ;;
    reload-plist)   shift; reload_plist "$@" ;;
    enable-full-trust) shift; enable_full_trust "$@" ;;
    set-reasoning)  shift; set_reasoning "$@" ;;
    set-model)      shift; set_model "$@" ;;
    bootstrap)      shift; bootstrap_plist "$@" ;;
    kickstart-system) shift; kickstart_system "$@" ;;
    disable-plist)  shift; disable_plist "$@" ;;
    enable-plist)   shift; enable_plist "$@" ;;
    sync-wx-deps)   shift; sync_wx_deps "$@" ;;
    update-claude)  update_claude ;;
    update-tools)   shift; update_tools "$@" ;;
    deploy-codex-launcher) shift; BABATA_SELF_OPS_CODEX_LAUNCHER=1 PYTHONDONTWRITEBYTECODE=1 python3 "$HOME/cc-workspace/tools/tool-maintenance/deploy_codex_launcher.py" "$@" ;;
    accept-playwright-desktop) shift; BABATA_SELF_OPS_PLAYWRIGHT_ACCEPTANCE=1 PYTHONDONTWRITEBYTECODE=1 python3 "$HOME/cc-workspace/tools/tool-maintenance/playwright_uv_adoption.py" "$@" ;;
    pause-tools)    shift; python3 "$HOME/cc-workspace/tools/tool-maintenance/control.py" pause "$@" ;;
    resume-tools)   shift; python3 "$HOME/cc-workspace/tools/tool-maintenance/control.py" resume "$@" ;;
    cancel-tools)   shift; python3 "$HOME/cc-workspace/tools/tool-maintenance/control.py" cancel "$@" ;;
    *) echo "Usage: $0 {restart [<label> [reason...]] | reload-plist [<label> [<plist>]] | enable-full-trust <label> | set-reasoning <label> <level> | set-model <label> <model> [<level>] | bootstrap <plist> | kickstart-system <label> | disable-plist <label> [<plist>] | enable-plist <label> [<plist>] | sync-wx-deps [--dry-run] | update-claude | update-tools | deploy-codex-launcher | accept-playwright-desktop | pause-tools <pid> | resume-tools <pid> | cancel-tools <pid>}" >&2; exit 1 ;;
esac
