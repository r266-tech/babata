# Streaming Input Changelog

## Design

- Added `cc.LiveSession`: one long-lived `ClaudeSDKClient`, one open-ended inbox iterator, and one `events()` async generator.
- `LiveSession.submit()` enqueues user messages synchronously; the SDK input task writes them to Claude Code stdin in FIFO order.
- `LiveSession.events()` converts SDK messages into `Event` objects: `text_delta`, `tool_use`, `tool_result`, `turn_end`, `session_changed`, and `error`.
- Kept the old `CC` class and `CC.query()` path intact for one-shot callers.
- Added `bot.ChannelWorker`: PTB handlers now enqueue `Payload` and return; background event consumption owns TG live text editing, tool status, final HTML formatting, accounting, and in-flight drain state.
- Added `/stop`, mapped to `ClaudeSDKClient.interrupt()`.
- `/new` and `/resume` reconnect the live CLI subprocess because those operations change the underlying session boundary.
- `/context` now uses the SDK context-usage control API instead of spawning a competing one-shot query against the same session.

## Preserved Behavior

- Streaming text message edit with the same 2s throttle.
- Verbose tool status message, flash/delete mode, and tool error surfacing.
- `Response` accounting fields: model, context window, max output, tokens, cost, sid.
- Session state persistence via `session_id` and `recent_sids`.
- `session-start.sh` on sid changes and `session-end.sh` on live close/reset/resume boundaries.
- Resume failure recovery: when the resumed subprocess errors before the first successful result, LiveSession reconnects fresh, injects `_recent_turns_summary()`, replays the sent user input, and surfaces `resume_note`.
- Media albums still enter as one payload with N image blocks.
- Bridge context updates on every submit, including mid-turn messages.

## Verification

- `pytest -q` -> `7 passed` (after P1 fixes — added 1 new race test + split back-to-back test in two)
- `/Users/admin/code/babata/.venv/bin/python -m py_compile cc.py bot.py`

## Adversarial Review Pass 2026-04-25

Codex (gpt-5.5 xhigh) flagged 4 P1 races / leaks; all fixed in this branch:

- **P1.1** — `cc.LiveSession._stop_client_locked` now drains `_inbox` before
  enqueueing `_STOP`, so user messages queued just before `/new` or `/resume`
  no longer leak into the OLD Claude subprocess. Test:
  `tests/test_live_session.py::test_live_session_reset_drops_pending_inbox`.
- **P1.2** — Un-recovered stream errors now call `_mark_dead_after_error()` to
  tear down the broken client + queue (so `submit()` raises instead of
  silently enqueuing into a dead inbox), and `bot.ChannelWorker._consume_events`
  is wrapped in a supervisor loop that reconnects the SDK with backoff.
- **P1.3** — `bot.ChannelWorker._handle_turn_end` wraps finalization in
  `try/finally` so a TG edit failure can't leave `_in_flight` stuck and hang
  graceful shutdown.
- **P1.4** — `bot.ChannelWorker` now tracks `_latest_payload` separately from
  `_turn_payload`; mid-turn submits update `_latest_payload`; `_handle_turn_end`
  re-`_begin_turn` with `_latest_payload` if it diverged from the turn's
  starting anchor, so the second SDK turn has a valid TG reply target.
  `_handle_text_delta` / `_handle_tool_event` also fall back to
  `_latest_payload` as a belt-and-suspenders. Test:
  `tests/test_channel_worker.py::test_channel_worker_back_to_back_submits_promote_next_anchor`.

P2/P3 doc fixes also applied (design doc: clarified single-reply-per-turn
matches implementation; SDK line numbers softened to "verify before citing").

## Known Trade-offs

- A message submitted while TG finalization is editing the previous turn waits for that short finalization critical section before being enqueued. This avoids losing the reply anchor for the next turn.
- `/context` output is now a compact structured summary from `get_context_usage()` rather than the exact CLI slash-command table.
- Unit tests use local stubs for `claude_agent_sdk` and `telegram` so `pytest -q` can run under the system Python without loading the Python 3.13 venv's binary wheels into Python 3.14.
