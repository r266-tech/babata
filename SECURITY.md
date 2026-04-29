# Security Policy

## Reporting Vulnerabilities

**Don't open public issues for security problems.** Report via [GitHub Security Advisories](https://github.com/r266-tech/babata/security/advisories/new) — private to maintainers, lets us coordinate a fix before disclosure.

Include:
- Affected file path + line range (e.g. `bot.py:1416-1450`)
- Reproduction steps against `main`
- What trust boundary was crossed
- Output of `babata --version` (or commit SHA)

## Trust Model

babata is a **personal-use bot** with one trusted operator (`ALLOWED_USER_ID`).

- **Single tenant**: anyone with `ALLOWED_USER_ID`'s Telegram account effectively gets shell access to the host (especially under `BABATA_FULL_TRUST=1`). Treat the TG bot token + chat ID as host credentials.
- **Default isolation**: babata defaults to `setting_sources=[]` + `cwd=repo` + `permission_mode=default`. Doesn't read user `~/.claude/`, doesn't access user `$HOME`. Power-user mode (`BABATA_FULL_TRUST=1`) opts into broader access.
- **Auth fail-closed**: `ALLOWED_USER_ID` unset = bot rejects everyone (including the bot's own owner). Never deploy without setting it.

## Out of Scope

These are documented behavior, not bugs:

- **Full-trust mode access**: `BABATA_FULL_TRUST=1` is intentionally `cwd=$HOME` + bypass-permissions. Anyone who can DM the bot effectively has shell. By design.
- **Shared-CC mode quota**: `BABATA_SHARED_CC=1` shares user's CC OAuth keychain. Bot's queries count against user's CC quota. Documented.
- **Bot token exposure**: `.env` is in `.gitignore`; protecting it is the operator's responsibility.

## Disclosure

90-day coordinated disclosure window or until a fix ships, whichever first. Reporters credited in release notes unless they request anonymity.
