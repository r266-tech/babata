"""Project-wide naming. Single source of truth is PROJECT_NAMESPACE env var.

OSS fork: set PROJECT_NAMESPACE=yourname (in shell or per-plist) to derive all
internal paths — launchd labels become `com.yourname.*`, state files become
`yourname-*.json`, socket becomes `/tmp/yourname-bridge.sock`, etc.

Default "babata" preserves the original author's layout so `git pull` on the
upstream deployment is a no-op. Env vars carry historical `BABATA_*` names
(INSTANCE, BRIDGE_SOCKET) — forks may keep them verbatim or sed-rename;
either works because only the VALUES flow through to paths, not the names.
"""
import os
from pathlib import Path

# Single source of truth.
PROJECT = os.environ.get("PROJECT_NAMESPACE", "babata")

# macOS launchd label prefix. auto-update.sh reads the same PROJECT_NAMESPACE
# env var and builds `com.${PROJECT_NAMESPACE}` itself (can't import Python
# from bash) — keep the two in sync manually.
LAUNCHD_PREFIX = f"com.{PROJECT}"

# State directory. Default = V's cross-project workspace (~/cc-workspace/state).
# OSS users typically want state inside the repo — set PROJECT_STATE_DIR.
STATE_DIR = Path(os.environ.get(
    "PROJECT_STATE_DIR",
    str(Path.home() / "cc-workspace" / "state"),
))

# Per-instance namespace. Empty BABATA_INSTANCE (env name kept verbatim for
# backward compat) → just PROJECT. Non-empty → PROJECT-<inst> so multiple
# bots share one venv + code but isolated state/socket.
INSTANCE = os.environ.get("BABATA_INSTANCE", "").strip()
NAMESPACE = f"{PROJECT}-{INSTANCE}" if INSTANCE else PROJECT

# Human-readable channel labels for the /resume picker and cross-channel tags.
# Key = BABATA_INSTANCE value ("" = main bot, non-empty = secondary instances).
# Single source of truth — cc.py derives labels from state-file stems via this
# map, bot.py _RESUME_CATEGORIES pulls the same values so TG category filter
# stays in sync. To rename: edit here, restart all 3 bots.
INSTANCE_LABELS: dict[str, str] = {
    "":       "巴巴塔",    # @WzrClaude_bot (main)
    "vvv":    "巴巴塔2",   # @vvvbabata_bot
    "vvvv":   "巴巴塔3",   # @vvvvbabata_bot
    "weixin": "wx",        # WeChat bot (not a TG instance but shares state dir)
}

# Files / sockets derived from NAMESPACE. Modules import these rather than
# reconstructing paths independently (risk: typo drift between modules,
# e.g. bot writes `babata-session.json` but cc reads `babata_session.json`).
SESSION_FILE = STATE_DIR / f"{NAMESPACE}-session.json"
STATE_FILE = STATE_DIR / f"{NAMESPACE}-state.json"
BRIDGE_SOCKET = os.environ.get(
    "BABATA_BRIDGE_SOCKET",
    f"/tmp/{NAMESPACE}-bridge.sock",
)

# Skill-evolve hooks — V-private (babata-skills repo). OSS users don't have
# this path; cc.py's fire code is guarded by is_file() so missing = silent no-op.
SKILL_HOOKS_DIR = Path(os.environ.get(
    "PROJECT_SKILL_HOOKS_DIR",
    str(Path.home() / "cc-workspace/skills-evolution/scripts/hooks"),
))

# Project-local lifecycle hooks — lives in the repo (checked in), so OSS forks
# get them for free. cc.py fires session-start.sh / session-end.sh from here on
# every session boundary (new sid observed, /reset, /resume). Scripts receive
# CLAUDE_SESSION_ID + BABATA_BRIDGE_SOCKET env vars. is_file() guarded.
HOOKS_DIR = Path(__file__).parent / "hooks"
