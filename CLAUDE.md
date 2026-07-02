# babata transport adapter

This repo is babata's transport shell for Telegram, WeChat, Sidebar, and
terminal-adjacent channel plumbing. Shared identity, philosophy, memory,
personal-context routing, and public-action permissions come from
`~/cc-workspace/AGENTS.md` / `~/cc-workspace/CLAUDE.md` plus rendered memory
context; do not duplicate them here.

For Codex terminal sessions, load shared context first when acting as babata:

```bash
~/cc-workspace/bin/babata-memory-context --profile standing --cpu codex --source terminal --include-top skip
```

## Boundary

- Keep this repo thin: channel protocol, formatting, auth, media conversion,
  MCP/bridge exposure, restart safety, and state handoff.
- Entrypoints: `bot.py` (Telegram), `weixin_bot.py` (WeChat),
  `sidebar_bot.py` (Sidebar).
- CPU adapters: `cc.py`, `codex_engine.py`, selector in `engine.py`.
- Bridge/MCP surfaces: `bridge.py`, `tg_mcp.py`, `weixin_bridge.py`,
  `weixin_mcp.py`, `sidebar_mcp.py`.

## Safety

- Do not put secrets or private identifiers in public repo files, logs,
  generated artifacts, PRs, or issues.
- Raw records and archives are append-only.
- Self-modification touching launchd services, CPU binaries, dependencies, or
  bot `ProgramArguments` goes through `scripts/self-ops.sh`.

Setup and public architecture belong in `README.md`, `CONTRIBUTING.md`, or
`docs/`. Durable facts belong in shared memory/brain, not repo prompt files.
