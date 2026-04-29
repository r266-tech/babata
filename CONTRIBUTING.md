# Contributing to babata

Thanks for the interest. babata is a thin transport layer (TG ↔ Claude Code), so contributions tend to fall in three buckets:

1. **Transport-layer fixes/features** — new TG capabilities, voice/media handling, multi-instance support
2. **OSS hardening** — making install / config / defaults work for more users
3. **Channels** — WeChat improvements, or new transports (Slack, Discord, ...)

## Dev Setup

```bash
git clone https://github.com/r266-tech/babata.git
cd babata
bash install.sh        # detects deps, sets up venv, scaffolds .env
$EDITOR .env           # fill TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID, ANTHROPIC_API_KEY
```

Open this repo with Claude Code from inside the repo dir to load `babata/CLAUDE.md` automatically:

```bash
cd ~/code/babata
claude
```

## Testing

Before pushing, run the smoke test:

```bash
bash tests/smoke.sh
```

This validates that fresh-OSS-user defaults still hold (no V private leakage, no path collisions, ALLOWED_USER fail-closed, etc.). CI runs the same script on macOS + Linux for every PR — failing locally = failing CI.

## PR Workflow

1. Fork + branch off `main` (one feature/fix per branch)
2. Make changes, run `bash tests/smoke.sh` until green
3. Commit using [Conventional Commits](https://www.conventionalcommits.org/) — `fix(bot):`, `feat(cc):`, `docs:`, etc. (look at `git log --oneline` for style)
4. Push your branch + open a PR
5. CI must be green (`smoke` workflow on macOS + Linux)
6. Review by maintainer

## Code Conventions

- **Don't add features beyond what's required.** Three similar lines beats a premature abstraction.
- **Don't add comments that just restate code.** Write a comment only when the *why* is non-obvious (constraint, workaround, surprising behavior).
- **`bot.py` only does what CC physically cannot.** Format conversion, transport protocol, UI feedback. Anything that could be a CC tool/skill should be a CC tool/skill, not bot Python.
- **`.env` for secrets and per-user config.** Code defaults are isolated/safe; opt into V-private behavior via env (`BABATA_SHARED_CC`, `BABATA_FULL_TRUST`, `PROJECT_*`, `BABATA_LABEL_*`).

## Architecture Quick Reference

| File | Role |
|---|---|
| `bot.py` | Telegram transport (HTML, 4096-char chunks, reactions, auth) |
| `weixin_bot.py` | WeChat transport (iLink protocol, optional) |
| `cc.py` | Claude Code SDK wrapper, channel-agnostic |
| `bridge.py` / `weixin_bridge.py` | Unix socket so MCP tools can push to TG/WeChat |
| `tg_mcp.py` / `weixin_mcp.py` | MCP tools `tg_send_*` / `wx_send_*` for CC to call |
| `media.py` | OGG/SILK voice → text, image base64, video understanding |
| `constants.py` | Single source of truth for paths / labels (env-driven) |

## Reporting Bugs

Use the [bug report template](https://github.com/r266-tech/babata/issues/new?template=bug_report.yml). Include:
- `babata --version` (or commit SHA)
- macOS or Linux
- The smoke test output (`bash tests/smoke.sh`)
- Reproduction steps
