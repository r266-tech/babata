#!/usr/bin/env bash
# Fresh-OSS-user smoke test. Validates default behavior with no V private envs:
#   - paths derive from repo (not ~/cc-workspace)
#   - INSTANCE_LABELS in English
#   - cc.py defaults to isolated mode (setting_sources=[], cwd=repo, permission_mode=default)
#   - bot.py _allowed() fails closed when ALLOWED_USER_ID==0
#   - /provider gracefully degrades when BABATA_CC_ROUTER_DIR unset
#
# Run from anywhere — the script finds the repo and uses its installed .venv.
# Exits non-zero on any failure for CI.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$REPO_DIR/.venv/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
    echo "❌ $VENV_PY not found. Run install.sh first." >&2
    exit 1
fi

TEST_DIR=$(mktemp -d -t babata-smoke.XXXXXX)
TEST_HOME=$(mktemp -d -t babata-smoke-home.XXXXXX)
trap "rm -rf $TEST_DIR $TEST_HOME" EXIT

cp -R "$REPO_DIR/." "$TEST_DIR/"
rm -rf "$TEST_DIR/.venv" "$TEST_DIR/.git" "$TEST_DIR/state"

# Fake .env — enough to import without crashing, no real tokens
cat > "$TEST_DIR/.env" <<EOF
TELEGRAM_BOT_TOKEN=fake_test_token
ALLOWED_USER_ID=12345
CLAUDE_CLI_PATH=/usr/bin/false
ANTHROPIC_API_KEY=sk-fake
EOF

# Run with all V private envs stripped, mocked HOME, cwd = fresh dir.
# Use the venv binary (Python deps come from there) but put fresh dir on
# sys.path[0] so `import constants` etc resolve to the fresh copy.
cd "$TEST_DIR"
env -i \
    PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/local/bin \
    HOME="$TEST_HOME" \
    SHELL=/bin/bash \
    TERM=xterm \
    "$VENV_PY" -c "
import sys, os
sys.path.insert(0, '$TEST_DIR')

from dotenv import load_dotenv
load_dotenv('$TEST_DIR/.env')

# Force fresh dir resolution (drop any cached V module imports)
for m in list(sys.modules):
    if m in ('constants', 'cc', 'bot'):
        del sys.modules[m]

import constants, cc, bot

failures = []
def check(name, actual, expected):
    ok = (actual == expected) if not callable(expected) else expected(actual)
    status = 'PASS ✓' if ok else 'FAIL ✗'
    print(f'  {status}  {name}: {actual!r}')
    if not ok:
        failures.append(name)

print('═══ constants.py defaults ═══')
check('STATE_DIR contains repo dir', str(constants.STATE_DIR), lambda v: '/state' in v and 'cc-workspace' not in v)
check('SKILL_HOOKS_DIR is empty/. (no-op)', str(constants.SKILL_HOOKS_DIR), lambda v: v in ('', '.'))
check('INSTANCE_LABELS main', constants.INSTANCE_LABELS[''], 'babata')
check('INSTANCE_LABELS no Chinese', constants.INSTANCE_LABELS, lambda d: not any('巴' in v for v in d.values()))
check('NAMESPACE', constants.NAMESPACE, 'babata')

print()
print('═══ cc.py isolation defaults ═══')
check('setting_sources empty', cc._SETTING_SOURCES, [])
check('cwd is repo, not HOME', cc._DEFAULT_CWD, lambda v: '/Users/' not in v or v.startswith('$TEST_DIR'))
check('permission_mode default', cc._PERMISSION_MODE, 'default')

print()
print('═══ bot.py defaults ═══')
check('cc_router empty (graceful degrade)', bot._CC_ROUTER_CLI, '')

print()
print('═══ Security: ALLOWED_USER==0 fail-closed ═══')
import importlib
os.environ['ALLOWED_USER_ID'] = '0'
importlib.reload(bot)
class U: id = 999
class Upd:
    effective_user = U()
check('ALLOWED_USER==0 reloaded', bot.ALLOWED_USER, 0)
check('_allowed(stranger) denied', bot._allowed(Upd()), False)

print()
print('═══ /provider graceful degrade ═══')
import asyncio
rc, body = asyncio.run(bot._run_cc_router_switch('foo'))
check('rc=2', rc, 2)
check('body says 未配置', body, lambda v: '未配置' in v)

print()
if failures:
    print(f'═══ {len(failures)} TEST(S) FAILED ═══')
    for f in failures:
        print(f'  - {f}')
    sys.exit(1)
else:
    print('═══ ALL SMOKE TESTS PASS ═══')
"
