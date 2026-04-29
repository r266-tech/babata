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
    # 非交互 (CI / `bash install.sh < /dev/null`) 时, read 会立刻拿到 EOF + set -e
    # 让脚本提前退出, 不到下面的引导. 显式 TTY guard 给非交互 case 明确指引.
    if [[ -t 0 ]] && [[ -t 1 ]]; then
        read -r -p "Install now? [Y/n] " ans
        if [[ "$ans" =~ ^[Nn] ]]; then
            echo "❌ babata 需要 Claude Code. 装好后再跑此脚本."
            exit 1
        fi
    else
        echo "(非交互终端, 自动装 Claude Code)"
    fi
    curl -fsSL https://claude.ai/install.sh | bash
    export PATH="$HOME/.local/bin:$PATH"
    if ! need claude; then
        echo "❌ Claude Code install failed. Manual: https://claude.ai/download"
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

# 6) .env scaffold (setup.py 会自动填; 这里只确保文件存在)
if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "✓ .env created from template"
else
    echo "✓ .env exists (setup.py 会按需更新)"
fi

# 7) Expose `babata` as a global command (mirrors hermes / openclaw)
mkdir -p "$HOME/.local/bin"
ln -sf "$REPO_DIR/.venv/bin/babata" "$HOME/.local/bin/babata"
echo "✓ symlinked $HOME/.local/bin/babata → $REPO_DIR/.venv/bin/babata"

# Hint user if ~/.local/bin not on PATH
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo
       echo "  ⚠️  $HOME/.local/bin is not on your PATH. Add to your shell rc:"
       echo "       export PATH=\"\$HOME/.local/bin:\$PATH\""
       ;;
esac

# 8) Hand off to setup.py — interactive guided config (auth + TG + WX)
echo
echo "── 进入引导程序 setup.py (可 Ctrl+C 跳过, 之后再跑 .venv/bin/python setup.py) ──"
echo

SETUP_EXIT=0
if [[ -t 0 ]] && [[ -t 1 ]]; then
    # 不能 set -e 时直接跑 setup.py — 失败会让整个 install.sh exit. 显式捕获退出码.
    "$REPO_DIR/.venv/bin/python" "$REPO_DIR/setup.py" || SETUP_EXIT=$?
else
    echo "(非交互终端, 跳过引导. 手动跑: .venv/bin/python setup.py)"
    SETUP_EXIT=99  # 显式标记: 没跑过, 待用户手动跑
fi

echo
if [[ $SETUP_EXIT -eq 0 ]]; then
    cat <<EOF
── Install done. Next: ─────────────────────────
  - 启动 bot:         babata           (foreground, Ctrl+C to stop)
  - 重跑引导配置:     .venv/bin/python setup.py
  - 后台常驻 (macOS): docs/persist-macos.md

EOF
elif [[ $SETUP_EXIT -eq 99 ]]; then
    cat <<EOF
── Install 完成, 配置待手动 ──────────────────
  - 跑引导配置:       .venv/bin/python setup.py
  - 后台常驻 (macOS): docs/persist-macos.md

EOF
else
    cat <<EOF
── Install 完成, 但配置未完成 (setup.py exit=$SETUP_EXIT) ──
  - 重跑引导:         .venv/bin/python setup.py
  - 没装任何 channel babata 跑不起来.

EOF
    exit $SETUP_EXIT
fi
