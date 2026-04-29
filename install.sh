#!/usr/bin/env bash
# babata installer — clone → bash install.sh → fill .env → run.
# Detects what's missing, walks user through the gaps. macOS-first; Linux mostly works.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo
echo "── babata install ──────────────────────────────"
echo

case "$(uname -s)" in
    Darwin) PLATFORM=mac ;;
    Linux)  PLATFORM=linux ;;
    *) echo "Unsupported OS: $(uname -s)"; exit 1 ;;
esac
echo "Platform: $PLATFORM"

need() { command -v "$1" >/dev/null 2>&1; }

# 1) Python 3.11+
if ! need python3; then
    echo "❌ python3 not found"
    [[ $PLATFORM == mac ]]   && echo "   Install: brew install python@3.11   (need Homebrew first: https://brew.sh)"
    [[ $PLATFORM == linux ]] && echo "   Install: sudo apt install python3 python3-venv  (or your distro)"
    exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')
if [[ "$PY_OK" != "1" ]]; then
    echo "❌ python3 $PY_VER too old (need >= 3.11)"
    exit 1
fi
echo "✓ python3 $PY_VER"

# 2) uv (fast Python package manager)
if ! need uv; then
    echo "Installing uv (Python package manager)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
echo "✓ uv $(uv --version 2>&1 | awk '{print $2}')"

# 3) ffmpeg (optional, for voice transcription)
if ! need ffmpeg; then
    echo "⚠️  ffmpeg not found (语音消息转录需要; 文字/图片/视频不受影响)"
    [[ $PLATFORM == mac ]]   && echo "   Install: brew install ffmpeg"
    [[ $PLATFORM == linux ]] && echo "   Install: sudo apt install ffmpeg"
fi

# 4) Claude Code CLI
if ! need claude; then
    echo
    echo "Claude Code CLI not found. babata uses it as the engine."
    read -r -p "Install now? [Y/n] " ans
    if [[ ! "$ans" =~ ^[Nn] ]]; then
        curl -fsSL https://claude.ai/install.sh | bash
        export PATH="$HOME/.local/bin:$PATH"
        if ! need claude; then
            echo "❌ Claude Code install failed. Manual: https://claude.ai/download"
            exit 1
        fi
    else
        echo "❌ babata 需要 Claude Code. 装好后再跑此脚本."
        exit 1
    fi
fi
CLAUDE_BIN=$(command -v claude)
CLAUDE_VER=$(claude --version 2>/dev/null | awk '{print $1}' || echo "?")
echo "✓ claude $CLAUDE_VER  at $CLAUDE_BIN"

# 5) Python venv + deps (uv sync from pyproject.toml + uv.lock)
echo
echo "Installing Python deps..."
uv sync --quiet
echo "✓ venv ready at .venv/"

# 6) .env scaffold
if [[ ! -f .env ]]; then
    cp .env.example .env
    # Pre-fill detected paths
    sed -i.bak "s|^CLAUDE_CLI_PATH=.*|CLAUDE_CLI_PATH=$CLAUDE_BIN|" .env
    rm -f .env.bak
    echo "✓ .env created (with CLAUDE_CLI_PATH pre-filled)"
    echo
    echo "  ── Required: edit .env to fill ──"
    echo "    TELEGRAM_BOT_TOKEN  ← get from @BotFather in Telegram"
    echo "    ALLOWED_USER_ID     ← get from @userinfobot in Telegram"
    echo "    ANTHROPIC_API_KEY   ← from https://console.anthropic.com"
    echo "                          (or skip + set BABATA_SHARED_CC=1 to share with logged-in CC)"
else
    echo "✓ .env exists (skipped)"
fi

cat <<EOF

── Install done. Next: ─────────────────────────
  1. Edit .env (see above)
  2. Test run:    .venv/bin/python bot.py
                  → message your bot in Telegram
  3. Persist:     docs/persist-macos.md  (launchd, optional)

EOF
