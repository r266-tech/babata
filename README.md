# babata

CC 个人助手 — 通讯层 (TG + 微信). Claude Code 是内核, babata 是围绕它构建的记忆/skill/通讯壳. 当前 repo 是通讯层 (TG 优先, 微信 WIP).

## Quick Start

Give this repo to your Claude Code:

```
clone https://github.com/r266-tech/babata and set it up for me
```

CC will read `CLAUDE.md` and walk you through everything.

## What This Is

Your terminal Claude Code, accessible from Telegram. Not a wrapper, not a reimplementation — just a transport layer.

- **Same CC binary** — same version, same memory, same hooks, same skills
- **Same everything** — the only difference is the wire
- **817 lines** — because a transport layer shouldn't be 21,000 lines

## Philosophy

The bot only does what CC physically cannot: convert TG media formats, render HTML, enforce TG's 4096-char limit, provide TG-native UI feedback.

It gives CC capabilities (MCP tools for TG buttons), but never tells CC how or when to use them.

Test for every line of code: *if AI were 100x smarter, would this still need to exist?*

## License

MIT
