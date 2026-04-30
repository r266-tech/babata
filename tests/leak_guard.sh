#!/usr/bin/env bash
# Block V's private strings from reaching the public repo.
# Two modes — chosen by explicit flag, NOT stdin TTY heuristics (CI shells
# also lack a TTY, which would silently put us in range-mode reading empty
# stdin and pass everything):
#   - default: scan tracked files in working tree (CI / manual run)
#   - --pre-push: read git pre-push protocol from stdin, patch-scan each
#     commit in the push range. Catches "add PII → commit → delete from
#     worktree → push" — the added line lives in the bad commit's patch.
#
# Forks: edit BLACKLIST below to your own private strings, or delete this file
# and the CI step / pre-push hook to disable entirely.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

SCAN_MODE="tree"
if [ "${1:-}" = "--pre-push" ]; then
    SCAN_MODE="range"
    shift
fi

BLACKLIST=(
    '/Users/admin'
    'wzrrrrrrr'
    '13008131315'
    '2668940489'
    'zhongrun\.wu'
    'r2668940489'
    '吴中润'
    'wuzhongrundeMacBook'
    'SH-WuZhongRun'
    '8229866476'
    '\btanka\b'
    'grandmirror'
    'Evo26689'
)

ZERO_SHA="0000000000000000000000000000000000000000"
PATHSPEC=(':!tests/leak_guard.sh')

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

    local p
    for p in "${BLACKLIST[@]}"; do
        local matches
        matches=$(echo "$added" | grep -nE -- "$p" || true)
        if [ -n "$matches" ]; then
            echo "✗ leak: '$p' [$context]"
            echo "$matches" | sed 's/^/    /'
            HITS=$((HITS + 1))
        fi
    done
}

scan_tree() {
    local p
    for p in "${BLACKLIST[@]}"; do
        local matches
        matches=$(git grep -n -I -E -e "$p" -- "${PATHSPEC[@]}" 2>/dev/null || true)
        if [ -n "$matches" ]; then
            echo "✗ leak: '$p'"
            echo "$matches" | sed 's/^/    /'
            HITS=$((HITS + 1))
        fi
    done
}

if [ "$SCAN_MODE" = "range" ]; then
    while read -r local_ref local_sha remote_ref remote_sha; do
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
