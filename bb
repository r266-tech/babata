#!/bin/bash
# bb — babata 终端渠道入口
#
# 设计: TG / 微信 / 终端 都是 babata 的通信渠道, 同一个 CC 内核.
# bot.py 在 $HOME 起 claude, session 落 ~/.claude/projects/-Users-admin/ bucket.
# 终端用 `bb` 起也落同一 bucket → `/resume` 跨渠道互相可见.
#
# 用法:
#   bb                    # 起新 session, 跟 bot 同 bucket
#   bb --resume           # 列出 bucket 里所有最近 session 让你选 (TG 开的 / 终端开的都能看见)
#   bb -c "some prompt"   # one-shot, 非交互
#
# 原生 `claude` 命令不动, 你在项目 dir 下跑项目内工作仍用它 (独立 bucket).
set -euo pipefail

cd "$HOME"
exec claude "$@"
