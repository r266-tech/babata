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

# 3) ffmpeg — 语音 STT/TTS pipeline 必需 (OGG → WAV / TTS mp3 → SILK).
# 默认装, 不让用户手敲. 100MB 但开箱即用. 自己有就 skip.
if ! need ffmpeg; then
    if [[ $PLATFORM == linux ]] && need apt-get; then
        echo "Installing ffmpeg (语音消息转录需要)..."
        sudo apt-get install -y ffmpeg
    elif [[ $PLATFORM == mac ]] && need brew; then
        echo "Installing ffmpeg (语音消息转录需要)..."
        brew install ffmpeg
    else
        echo "⚠️  ffmpeg not found, 自动装失败 (没 apt-get / brew). 手动装一下:"
        [[ $PLATFORM == mac ]]   && echo "   brew install ffmpeg"
        [[ $PLATFORM == linux ]] && echo "   你的发行版包管理器装 ffmpeg"
    fi
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
    # 配 systemd (Linux) / launchd (macOS) auto-start — 装完后台常驻,
    # 关终端不影响, 重启机器自动起, crash 自动重启. 真用户友好.
    SVC_OK=0
    if [[ $PLATFORM == linux ]] && need systemctl; then
        SERVICE="$HOME/.config/systemd/user/babata.service"
        mkdir -p "$(dirname "$SERVICE")"
        cat > "$SERVICE" <<SVCEOF
[Unit]
Description=babata bot (TG + WX)
After=network.target

[Service]
ExecStart=$REPO_DIR/.venv/bin/babata
Restart=always
RestartSec=5
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
SVCEOF
        systemctl --user daemon-reload && \
          systemctl --user enable --now babata.service 2>/dev/null && SVC_OK=1
        # enable-linger: user 没登录 systemd 也跑. 容器里可能没 /var/lib/systemd/linger 写权限, 失败不致命.
        sudo loginctl enable-linger "$USER" 2>/dev/null || true
        if [[ $SVC_OK -eq 1 ]]; then
            cat <<EOF
── Install done. Bot 后台常驻已启动 (systemd). ──────
  - 看 log:      journalctl --user -u babata -f
  - 状态:        systemctl --user status babata
  - 停:          systemctl --user stop babata
  - 重启:        systemctl --user restart babata

EOF
        fi
    elif [[ $PLATFORM == mac ]]; then
        PLIST="$HOME/Library/LaunchAgents/com.babata.plist"
        mkdir -p "$(dirname "$PLIST")"
        cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.babata</string>
    <key>ProgramArguments</key>
    <array><string>$REPO_DIR/.venv/bin/babata</string></array>
    <key>WorkingDirectory</key><string>$REPO_DIR</string>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$HOME/babata.log</string>
    <key>StandardErrorPath</key><string>$HOME/babata.log</string>
    <key>EnvironmentVariables</key>
    <dict><key>PATH</key><string>$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict>
</plist>
PLISTEOF
        # 已 load 的先 bootout (idempotent), 再 bootstrap. 旧 launchctl 用 load fallback.
        launchctl bootout "gui/$UID/com.babata" 2>/dev/null || true
        if launchctl bootstrap "gui/$UID" "$PLIST" 2>/dev/null; then
            SVC_OK=1
        elif launchctl load -w "$PLIST" 2>/dev/null; then
            SVC_OK=1
        fi
        if [[ $SVC_OK -eq 1 ]]; then
            cat <<EOF
── Install done. Bot 后台常驻已启动 (launchd). ──────
  - 看 log:      tail -f ~/babata.log
  - 状态:        launchctl print gui/\$UID/com.babata | head -20
  - 停:          launchctl bootout gui/\$UID/com.babata
  - 重启:        launchctl kickstart -k gui/\$UID/com.babata

EOF
        fi
    fi
    if [[ $SVC_OK -ne 1 ]]; then
        # 没 systemd / launchd, 或 service 配失败 → 沦为 foreground demo + 提示
        echo "⚠️  没探测到 systemd/launchd, 装到 auto-start 失败. Foreground 启动:"
        echo
        if [[ -t 0 ]] && [[ -t 1 ]]; then
            exec "$REPO_DIR/.venv/bin/babata"
        fi
        cat <<EOF
  - 启动 bot:         $REPO_DIR/.venv/bin/babata
  - 重跑引导:         $REPO_DIR/.venv/bin/python $REPO_DIR/wizard.py

EOF
    fi
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
