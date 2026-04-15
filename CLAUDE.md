# babata (CC 个人助手 — 通讯层)

babata = CC 个人助手. 以 Claude Code 为内核, 围绕其构建记忆层 / skill 进化机制 / 通讯层. 本 repo 是**通讯层**部分, 两个独立渠道: TG (bot.py) + 微信 (weixin_bot.py). 两 channel 各自持有独立 CC session, 独立 MCP tool surface, 独立 bridge socket. 物理分工: 壳只做 CC 做不到的事 (格式转换 / UI / 渠道接入), 不替 CC 做决定.

## Philosophy

Bot only does what CC physically cannot. Give CC capabilities, never tell it how to use them. Test: if AI were 100x smarter, would this line of code still need to exist? Yes → keep. No → delete.

## Setup Guide (for CC helping a new user)

When a user clones this repo and asks for help setting it up, follow these steps:

### 1. Create Telegram Bot
Tell the user to:
1. Open Telegram, find @BotFather
2. Send `/newbot`, follow prompts, get the bot token
3. Send `/mybots` → select bot → "Bot Settings" → note the username

Also have them find their Telegram user ID:
- Send a message to @userinfobot, it returns their numeric ID

### 2. Find Claude CLI Path
Run: `which claude`
If not found, the user needs Claude Code installed: `npm install -g @anthropic-ai/claude-code`

### 3. Create .env
```bash
cp .env.example .env
```
Fill in:
- `TELEGRAM_BOT_TOKEN` — from BotFather
- `ALLOWED_USER_ID` — their Telegram user ID
- `CLAUDE_CLI_PATH` — output of `which claude`

### 4. Install Dependencies
```bash
uv venv && uv pip install --index-url https://pypi.org/simple/ python-telegram-bot python-dotenv claude-agent-sdk
```
Or with pip:
```bash
python -m venv .venv && .venv/bin/pip install python-telegram-bot python-dotenv claude-agent-sdk
```

### 5. Run
```bash
.venv/bin/python bot.py
```

### 6. Persistent (optional, macOS)
Create a launchd plist at `~/Library/LaunchAgents/com.babata.plist` with:
- ProgramArguments: path to `.venv/bin/python` and `bot.py`
- WorkingDirectory: this project's path
- KeepAlive: true
- PATH must include the directory containing `claude`, `ffmpeg`

Then: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.babata.plist`

## WeChat Setup (optional — independent channel from TG)

微信走腾讯官方 iLink bot 协议 (MIT, 官方支持), 扫码授权后拿 bot token 长轮询.

### 1. Install Dependencies (if not already)
`pilk` 解微信 SILK 语音, `qrcode` 终端渲 ASCII QR:
```
.venv/bin/pip install pilk qrcode
```

### 2. First-time Login
```
.venv/bin/python weixin_bot.py
```
- 终端打 ASCII QR → 用微信扫码确认授权
- 扫码的微信号自动进 allowFrom (后续只有这个账号能给 bot 发消息触发 CC)
- Token 存 `~/.babata/weixin/accounts/`, 重启自动复用
- 再加一个微信号: `.venv/bin/python weixin_bot.py --login`

### 3. Bot 出现在哪
扫码后, 授权的微信里会有一个叫「微信 ClawBot」的对话 (腾讯后台默认名字, 可登 iLink 后台改名/头像). 发消息给它 = 触发 CC.

### 4. Persistent (optional, macOS)
类似 TG, 新建一个 `com.babata.weixin.plist`, `ProgramArguments` 指向 `weixin_bot.py`. TG 和微信是两个独立进程, 各自管自己的 state.

## Architecture

两 channel 独立跑, 各自持有 CC SDK 实例 + MCP server + bridge socket:

```
TG message    → bot.py         → cc.py ──┐
                  ↕                      │
                tg_mcp.py (stdio)        ├── spawns Claude Code subprocess
                  ↕ /tmp/babata-bridge.sock  │   (shares ~/.claude/ settings + skills)
                bridge.py                │
                                         │
WeChat inbound → weixin_bot.py → cc.py ──┘
  (getUpdates)    ↕
                weixin_mcp.py (stdio)
                  ↕ /tmp/babata-weixin-bridge.sock
                weixin_bridge.py
                  ↕
                weixin_ilink.py (HTTP + AES-128-ECB + QR login)

media.py: TG OGG → ffmpeg + MiMo STT, WeChat SILK → pilk + MiMo STT, images → base64
weixin_account.py: ~/.babata/weixin/ (tokens, sync_buf, contextTokens, allowFrom)
```

## Files

| File | Why it exists |
|------|---------------|
| bot.py | TG transport, formatting (TG HTML + 4096 limit), reactions, auth |
| cc.py | CC SDK wrapper, channel-agnostic (takes state_file + source_prompt + mcp_servers) |
| bridge.py | Unix socket bridge for TG MCP actions (`/tmp/babata-bridge.sock`) |
| tg_mcp.py | MCP tools `tg_send_*` — capability for CC, not instructions |
| media.py | OGG/SILK voice transcription, image base64, video understanding |
| weixin_bot.py | WeChat long-poll main loop, inbound decode, stream coalesce, auth |
| weixin_ilink.py | iLink bot protocol (5 HTTP endpoints + QR login + CDN AES) |
| weixin_bridge.py | Unix socket bridge for WeChat MCP actions (`/tmp/babata-weixin-bridge.sock`) |
| weixin_mcp.py | MCP tools `wx_send_*` |
| weixin_account.py | Per-account persistence (token/sync/contextTokens/allowFrom) |

## Voice Requirements (optional)
- `ffmpeg` — converts TG voice (OGG) to 16kHz mono WAV
- `VIDEO_API_URL` + `VIDEO_API_KEY` in `.env` — MiMo-v2-Omni endpoint (same as video understanding)

Without these, text and image still work. Voice messages fail loud (reply 转录失败: <reason>) — no silent fallback.

## Commands
- `/new` — reset session
- `/verbose` — cycle tool display: 0=hidden / 1=flash / 2=keep
