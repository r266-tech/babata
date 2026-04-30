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

# 4) Claude Code CLI — babata 强依赖, 没装就直接装, 不 ask (跟 uv 一样)
if ! need claude; then
    echo "Installing Claude Code CLI (babata 内核)..."
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

# 6) .env scaffold (wizard.py 会自动填; 这里只确保文件存在)
if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "✓ .env created from template"
else
    echo "✓ .env exists (wizard.py 会按需更新)"
fi

# 7) Expose `babata` as a global command (mirrors hermes / openclaw)
mkdir -p "$HOME/.local/bin"
ln -sf "$REPO_DIR/.venv/bin/babata" "$HOME/.local/bin/babata"
echo "✓ symlinked $HOME/.local/bin/babata → $REPO_DIR/.venv/bin/babata"

# PATH 没 ~/.local/bin 就自动 append 到 shell rc — 不让用户手改文件 (iron rule).
# 幂等: grep 检查已存在不重复 append.
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
       # 同时 append 到多处: interactive non-login (.bashrc/.zshrc) + login shell
       # (.profile/.zprofile). OrbStack ssh / docker exec 进 sandbox 是 login shell,
       # 只读 .profile 不读 .bashrc — 单写 .bashrc 用户开新 shell 还是没 PATH.
       SHELL_NAME="$(basename "${SHELL:-/bin/bash}")"
       EXPORT_LINE='export PATH="$HOME/.local/bin:$PATH"'
       case "$SHELL_NAME" in
           zsh)  RCS=("$HOME/.zshrc" "$HOME/.zprofile") ;;
           bash) RCS=("$HOME/.bashrc" "$HOME/.profile") ;;
           fish) RCS=("$HOME/.config/fish/config.fish")
                 EXPORT_LINE='set -gx PATH $HOME/.local/bin $PATH' ;;
           *)    RCS=("$HOME/.profile") ;;
       esac
       for RC in "${RCS[@]}"; do
           mkdir -p "$(dirname "$RC")"
           touch "$RC"
           if grep -qF -- "$EXPORT_LINE" "$RC" 2>/dev/null; then
               echo "✓ \$HOME/.local/bin 已在 $RC"
           else
               {
                   echo ""
                   echo "# babata install.sh — 自动加 ~/.local/bin"
                   echo "$EXPORT_LINE"
               } >> "$RC"
               echo "✓ 已加 \$HOME/.local/bin 到 $RC"
           fi
       done
       echo "  开新终端 or 跑: source ${RCS[0]}"
       ;;
esac

# 8) Hand off to wizard.py — interactive guided config (auth + TG + WX)
echo
echo "── 进入引导程序 wizard.py (可 Ctrl+C 跳过, 之后再跑 .venv/bin/python wizard.py) ──"
echo

SETUP_EXIT=0
if [[ -t 0 ]] && [[ -t 1 ]]; then
    # 不能 set -e 时直接跑 wizard.py — 失败会让整个 install.sh exit. 显式捕获退出码.
    "$REPO_DIR/.venv/bin/python" "$REPO_DIR/wizard.py" || SETUP_EXIT=$?
else
    echo "(非交互终端, 跳过引导. 手动跑: .venv/bin/python wizard.py)"
    SETUP_EXIT=99  # 显式标记: 没跑过, 待用户手动跑
fi

echo
if [[ $SETUP_EXIT -eq 0 ]]; then
    # 装完直接启动 bot, 不再 prompt — 装到这一步就是为了用.
    # 非交互终端 (CI / pipe) 才打印 hint, 不 spawn 卡住的 foreground.
    if [[ -t 0 ]] && [[ -t 1 ]]; then
        echo "── Install done. 启动 bot (Ctrl+C 退出). ─────────────"
        echo
        exec "$REPO_DIR/.venv/bin/babata"
    fi
    cat <<EOF
── Install done. ────────────────────────────────
  - 启动 bot:         $REPO_DIR/.venv/bin/babata
  - 重跑引导:         $REPO_DIR/.venv/bin/python $REPO_DIR/wizard.py

EOF
elif [[ $SETUP_EXIT -eq 99 ]]; then
    cat <<EOF
── Install 完成, 配置待手动 ──────────────────
  - 跑引导配置:       $REPO_DIR/.venv/bin/python $REPO_DIR/wizard.py
  - 后台常驻 (macOS): docs/persist-macos.md

EOF
else
    cat <<EOF
── Install 完成, 但配置未完成 (wizard.py exit=$SETUP_EXIT) ──
  - 重跑引导:         $REPO_DIR/.venv/bin/python $REPO_DIR/wizard.py
  - 没装任何 channel babata 跑不起来.

EOF
    exit $SETUP_EXIT
fi
