#!/usr/bin/env bash
# Block local private strings from reaching the public repo.
# Modes are chosen by explicit flag, NOT stdin TTY heuristics (CI shells
# also lack a TTY, which would silently put us in range-mode reading empty
# stdin and pass everything):
#   - default: scan tracked files in working tree (CI / manual run)
#   - --pre-push: read git pre-push protocol from stdin, patch-scan each
#     commit in the push range. Catches "add PII → commit → delete from
#     worktree → push" — the added line lives in the bad commit's patch.
#   - --history: patch-scan all reachable commits; run before publishing a
#     repository whose history may be exposed.
#
# Forks: keep generic patterns here. Put machine-private regexes in
# .git/info/babata-leak-patterns or set BABATA_LEAK_GUARD_PATTERNS to another
# newline-delimited regex file.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

SCAN_MODE="tree"
case "${1:-}" in
    --pre-push)
        SCAN_MODE="range"
        shift
        ;;
    --history)
        SCAN_MODE="history"
        shift
        ;;
esac

FIXED_BLACKLIST=()
REGEX_BLACKLIST=()
if [ -n "${HOME:-}" ]; then
    FIXED_BLACKLIST+=("$HOME")
fi

PATTERN_FILES=()
if [ -n "${BABATA_LEAK_GUARD_PATTERNS:-}" ]; then
    PATTERN_FILES+=("$BABATA_LEAK_GUARD_PATTERNS")
fi
PATTERN_FILES+=("$REPO_DIR/.git/info/babata-leak-patterns")

for pattern_file in "${PATTERN_FILES[@]}"; do
    [ -f "$pattern_file" ] || continue
    while IFS= read -r pattern || [ -n "$pattern" ]; do
        pattern=${pattern%$'\r'}
        case "$pattern" in
            ""|\#*) continue ;;
        esac
        REGEX_BLACKLIST+=("$pattern")
    done < "$pattern_file"
done

ZERO_SHA="0000000000000000000000000000000000000000"
PATHSPEC=('.')

HITS=0

scan_revs() {
    # Scan a single commit's *patch* (added lines only), not its full tree.
    # Tree mode would re-flag any historical leak that survives in unchanged
    # files, blocking pushes forever. Patch mode catches what THIS commit
    # introduces — including the "add PII → commit → delete in next commit"
    # trick (the added line still exists in the first commit's patch).
    local rev="$1"
    local context="$2"
    local added
    added=$(git show "$rev" --format='' --no-color --unified=0 \
        -- "${PATHSPEC[@]}" 2>/dev/null \
        | grep -E '^\+[^+]' || true)
    [ -z "$added" ] && return

    [ ${#FIXED_BLACKLIST[@]} -eq 0 ] && [ ${#REGEX_BLACKLIST[@]} -eq 0 ] && return
    local i p matches
    for i in "${!FIXED_BLACKLIST[@]}"; do
        p="${FIXED_BLACKLIST[$i]}"
        matches=$(echo "$added" | grep -nF -- "$p" || true)
        if [ -n "$matches" ]; then
            echo "✗ leak: fixed pattern #$((i + 1)) [$context]"
            echo "$matches" | sed -E 's/^([0-9]+):.*$/    patch-line \1: <redacted>/'
            HITS=$((HITS + 1))
        fi
    done
    for i in "${!REGEX_BLACKLIST[@]}"; do
        p="${REGEX_BLACKLIST[$i]}"
        matches=$(echo "$added" | grep -nE -- "$p" || true)
        if [ -n "$matches" ]; then
            echo "✗ leak: regex pattern #$((i + 1)) [$context]"
            echo "$matches" | sed -E 's/^([0-9]+):.*$/    patch-line \1: <redacted>/'
            HITS=$((HITS + 1))
        fi
    done
}

scan_tree() {
    [ ${#FIXED_BLACKLIST[@]} -eq 0 ] && [ ${#REGEX_BLACKLIST[@]} -eq 0 ] && return
    local i p matches
    for i in "${!FIXED_BLACKLIST[@]}"; do
        p="${FIXED_BLACKLIST[$i]}"
        matches=$(git grep -n -I -F -e "$p" -- "${PATHSPEC[@]}" 2>/dev/null || true)
        if [ -n "$matches" ]; then
            echo "✗ leak: fixed pattern #$((i + 1))"
            echo "$matches" | sed -E 's/^([^:]+:[0-9]+):.*$/    \1: <redacted>/'
            HITS=$((HITS + 1))
        fi
    done
    for i in "${!REGEX_BLACKLIST[@]}"; do
        p="${REGEX_BLACKLIST[$i]}"
        matches=$(git grep -n -I -E -e "$p" -- "${PATHSPEC[@]}" 2>/dev/null || true)
        if [ -n "$matches" ]; then
            echo "✗ leak: regex pattern #$((i + 1))"
            echo "$matches" | sed -E 's/^([^:]+:[0-9]+):.*$/    \1: <redacted>/'
            HITS=$((HITS + 1))
        fi
    done
}

if [ "$SCAN_MODE" = "range" ]; then
    while read -r local_ref local_sha _remote_ref remote_sha; do
        # Empty line / branch deletion (local_sha == zero) → skip
        [ -z "${local_sha:-}" ] && continue
        [ "$local_sha" = "$ZERO_SHA" ] && continue

        if [ "$remote_sha" = "$ZERO_SHA" ]; then
            # New branch — scan commits unique to this push
            # (anything reachable from local_sha but not from any other remote ref)
            commits=$(git rev-list "$local_sha" --not --remotes 2>/dev/null)
        else
            commits=$(git rev-list "${remote_sha}..${local_sha}" 2>/dev/null)
        fi

        for c in $commits; do
            scan_revs "$c" "${c:0:7} on ${local_ref##refs/heads/}"
        done
    done
elif [ "$SCAN_MODE" = "history" ]; then
    commits=$(git rev-list --all 2>/dev/null)
    for c in $commits; do
        scan_revs "$c" "${c:0:7} history"
    done
else
    scan_tree
fi

if [ $HITS -gt 0 ]; then
    echo ""
    echo "🚫 $HITS private string match(es). Refusing to proceed."
    echo "   Scrub values & rewrite history (interactive rebase / filter-branch),"
    echo "   or update BLACKLIST if a hit is intentional."
    echo "   Emergency bypass: git push --no-verify"
    exit 1
fi

echo "✓ leak_guard: clean"
