# CC TG Bot

Thin Telegram transport for Claude Code. 817 lines, 5 files. TG is just a wire — same CC, different channel.

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
Create a launchd plist at `~/Library/LaunchAgents/com.cc-tg.plist` with:
- ProgramArguments: path to `.venv/bin/python` and `bot.py`
- WorkingDirectory: this project's path
- KeepAlive: true
- PATH must include the directory containing `claude`, `ffmpeg`

Then: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cc-tg.plist`

## Architecture

```
TG message → bot.py (transport) → cc.py (SDK) → your terminal's Claude Code CLI
                                      ↕
                              tg_mcp.py (stdio MCP server, gives CC TG capabilities)
                                      ↕ unix socket
                              bridge.py (renders buttons in TG, returns user choice)

media.py: voice → MiMo-v2-Omni STT → text, image → base64
```

## Files

| File | Lines | Why it exists |
|------|-------|---------------|
| bot.py | 368 | TG transport, formatting (physical: TG HTML + 4096 limit), reactions, auth |
| cc.py | 165 | CC SDK call, session resume, streaming |
| bridge.py | 124 | Unix socket bridge between MCP process and TG bot (physical: cross-process) |
| tg_mcp.py | 89 | MCP tool `tg_send_buttons` — capability for CC, not instructions |
| media.py | 71 | Voice transcription, image base64 (physical: CC can't receive OGG) |

## Voice Requirements (optional)
- `ffmpeg` — converts TG voice (OGG) to 16kHz mono WAV
- `VIDEO_API_URL` + `VIDEO_API_KEY` in `.env` — MiMo-v2-Omni endpoint (same as video understanding)

Without these, text and image still work. Voice messages fail loud (reply 转录失败: <reason>) — no silent fallback.

## Commands
- `/new` — reset session
- `/verbose` — cycle tool display: 0=hidden / 1=flash / 2=keep
