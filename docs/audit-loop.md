# babata Audit Loop

babata's hook/review layer is CPU-neutral. Telegram, WeChat, Sidebar, terminal
Codex, and terminal Claude keep their own session state and tool surfaces; the
audit loop records turns and runs blocking review before a code-changing turn is
allowed to complete.

## Layers

1. Turn ledger

   Every Claude/Codex turn records the CPU, channel, cwd, git baseline,
   prompt/final hashes and byte counts, tools, changed files, deterministic
   guard findings, declared check results, and review tasks.

   Default path:

   - `state/audit/<namespace>-turn-ledger.jsonl`

2. Deterministic guards

   Guards are local, physical boundaries rather than taste or style rules:

   - token-like secret patterns in changed files
   - `.env` changes
   - launchd mutation outside `scripts/self-ops.sh`
   - raw/archive layer changes
   - dangerous git and destructive memory/archive commands

   Default mode is observe. Set `BABATA_DETERMINISTIC_GUARDS=enforce` to deny
   matching Claude SDK permission requests before the tool runs. Post-turn file
   findings are still ledgered because Codex CLI and completed writes cannot be
   retroactively denied.

3. Declared checks

   Checks run only when the repo declares `.babata/checks.json` before the turn
   starts, and babata skips the checks if that config is created or modified
   during the same turn. babata does not assume global npm, TypeScript, Python,
   or project-specific test commands.

   Example:

   ```json
   {
     "checks": [
       {
         "name": "compile",
        "command": ".venv/bin/python -m py_compile cc.py codex_engine.py turn_audit.py blocking_review.py",
         "when": ["python", "security"],
         "timeout_seconds": 60
       }
     ]
   }
   ```

   `when` can be a string or list. Supported context tags include `always`,
   `python`, `javascript`, `node`, `docs`, `prompt`, `ops`, `hooks`, and
   `security`.

4. Blocking review gate

   Code-changing turns pass through a synchronous Stop-style gate after the
   ledger, guards, and declared checks finish. The gate has no async advisory
   insertion path: if review returns findings, babata feeds those findings back
   into the same session as an internal repair prompt and withholds final
   completion until the next review pass succeeds or the configured repair round
   limit is reached.

   The default reviewer after deterministic local checks is an independent Codex
   process using `gpt-5.6-sol` with `max` reasoning through `codex exec` in
   read-only mode. This remains the default regardless of the authoring CPU while
   Claude is unavailable; `BABATA_BLOCKING_REVIEW_CPU=claude` is an explicit
   opt-in to the legacy `cc-worker` reviewer. The original CPU still performs any
   repair in its own session; the reviewer only returns a verdict and findings.
   babata sets
   `BABATA_BLOCKING_REVIEW=0` and increments `BABATA_BLOCKING_REVIEW_DEPTH` for
   reviewer child processes so review does not recursively call another review.

   Deterministic guard failures and failed declared checks block before the
   counterpart CPU is called. With `BABATA_BLOCKING_REVIEW_CMD`, babata uses that
   command instead of the built-in counterpart reviewer, runs it synchronously,
   and sends the review payload as JSON on stdin. The command should return JSON
   such as:

   ```json
   {
     "status": "needs_fix",
     "findings": [
       {"severity": "high", "rule": "review", "message": "Fix the bug."}
     ]
   }
   ```

   Return `{"status":"passed"}` to allow completion.

   The reviewer may receive the response draft as bounded input, but
   `blocking-review.jsonl` records only the scrubbed result plus response
   hash/byte metadata; echoed drafts are omitted before the result is returned or
   persisted.

5. Review bus

   The review bus is now optional audit plumbing, not the default review
   mechanism. Enable it only when another local process needs a JSONL trail of
   review tasks; it must not inject findings back into a later user turn.

   - `state/audit/<namespace>-review-bus.jsonl`
   - `state/audit/<namespace>-blocking-review.jsonl`

## Environment

- `BABATA_TURN_LEDGER=0` disables the whole audit loop.
- `BABATA_AUDIT_DIR=/path` overrides the ledger/review-bus directory.
- `BABATA_DETERMINISTIC_GUARDS=observe|enforce|off` controls guard behavior.
- `BABATA_DECLARED_CHECKS=0` disables `.babata/checks.json`.
- `BABATA_BLOCKING_REVIEW=0` disables the blocking review gate.
- `BABATA_BLOCKING_REVIEW_AGENT=deterministic` disables counterpart CPU review.
- `BABATA_BLOCKING_REVIEW_COUNTERPART=0` also disables counterpart CPU review.
- `BABATA_BLOCKING_REVIEW_CPU=codex|claude|counterpart` selects the review CPU;
  default is `codex`, while `counterpart` explicitly restores opposite-CPU routing.
  Unsupported values fail safely back to Codex instead of skipping model review.
- `BABATA_BLOCKING_REVIEW_MAX_DEPTH=1` prevents nested counterpart review.
- `BABATA_BLOCKING_REVIEW_CMD=<command>` runs a synchronous external reviewer.
- `BABATA_BLOCKING_REVIEW_MAX_ROUNDS=2` bounds internal repair loops.
- `BABATA_BLOCKING_REVIEW_INFRA_STRICT=1` makes missing counterpart reviewer
  infrastructure fail closed.
- `BABATA_CC_WORKER=/path/to/cc-worker` overrides the Claude reviewer CLI.
- `BABATA_CODEX_REVIEW_CLI=/path/to/codex` overrides the Codex reviewer CLI.
- `BABATA_CODEX_REVIEW_SANDBOX=read-only` controls the Codex reviewer sandbox.
- `BABATA_REVIEW_HEALTH_DEEP=1` lets health probes run `cc-worker verify`;
  default health checks stay lightweight.
- `BABATA_REVIEW_BUS=queue|off` controls optional review task enqueueing.

`/status` and `state/runtime-status-<instance>.json` include blocking-review
health. Health status is `ok`, `degraded`, `block`, `disabled`, or
`deterministic-only`; `block` means strict mode is enabled and a counterpart
reviewer probe failed.

## Isolation

This layer does not set `BABATA_SHARED_CC`, does not load user-level Claude
settings, and does not install or enable Claude plugins. Private deployments can
still opt into Claude Code security-guidance through their own Claude settings;
babata's default architecture remains ledger plus local guards plus declared
repo checks plus a blocking review gate.
