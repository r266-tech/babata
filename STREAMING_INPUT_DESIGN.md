# babata 流式 user input 改造 — Design Doc

**Branch**: `feature/streaming-input`  **Worktree**: `/tmp/babata-streaming`
**Owner / acceptance**: V  **Implementer first-pass**: babata (CC)
**Date**: 2026-04-24

---

## 目标 (one-liner)

让 TG bot 的"V 在 turn 进行中又发消息"行为跟终端 CC 完全一致：消息**不丢、不阻塞、不串话**，由模型在 turn 内的合适位置自己消费（继续 / 停 / 合并）。

## 当前 (main 分支) 行为复盘

`bot.py:_process()` 每条 message 直接 `await cc.query()`：
- `concurrent_updates(True)` + 没 lock → 两条消息**并发**跑两个 CC query
- 每次 query 内部 `_run()` 都新建 `ClaudeSDKClient` + connect + disconnect
- 两个 query 都 `--resume` 同一个 `session_id` → **同一个 session jsonl 写竞争**
- `bridge.set_context()` 是**全局单例** → 第二条消息一进来就覆盖第一条的 chat/reply_to → 第一条 turn 内的 MCP tool 调用 (`mcp__tg__tg_send_*`) reply 到错的 message
- 两条 reply 同时流式编辑 → TG 视觉混乱

## SDK 真相 (源码 verified)

`/Users/admin/code/babata/.venv/lib/python3.13/site-packages/claude_agent_sdk/client.py`
(line numbers verified against the installed SDK on 2026-04-25; the actual
methods may move slightly between SDK versions — check before citing again):

```python
# query() — async iterable prompt is the streaming-input entry point
async def query(self, prompt: str | AsyncIterable[dict], session_id: str = "default"):
    if isinstance(prompt, str):
        await self._transport.write(json.dumps({...user msg...}) + "\n")
    else:
        async for msg in prompt:                   # ← 关键: 持续 yield 持续 write
            await self._transport.write(json.dumps(msg) + "\n")

# interrupt() — ESC equivalent
async def interrupt(self):
    await self._query.interrupt()

# set_permission_mode / set_model — mid-session is allowed
```

→ 一次 `connect()` + 一次 `query(open_ended_iter)` + iterator 持续 yield = 持续往 CLI stdin 推 user message。CLI 自己在工具循环间隙消费这些消息，模型自己看上下文判断动作。这正是终端 CC 的内核机制。

## 新架构

```
ChannelWorker  (per-channel/instance, 长生命周期, asyncio task)
  ├─ LiveSession  (cc.py 新增)
  │    ├─ ClaudeSDKClient  ← connect 一次, 不 disconnect
  │    ├─ inbox: asyncio.Queue[dict | _Sentinel]
  │    ├─ _input_iter()    ← async gen, 持续 yield from inbox
  │    └─ events()         ← async gen, yield 解析后的 turn 内事件
  ├─ submit(payload):  inbox.put + bridge.set_context(payload)
  ├─ /stop / cmd_stop:  client.interrupt()
  ├─ event consumer task:  receive_messages → on_stream/on_tool/on_turn_end
  └─ graceful close:  inbox.put(_Sentinel) → iter 退出 → query() 返回 → disconnect
```

### 单条消息流

1. `on_text/on_voice/on_photo` → `_process()` 构造 `Payload` → `worker.submit(payload)`
2. `worker.submit`:
   - `bridge.set_context(bot, chat, msg_id)` 立即更新（mid-turn 来的 msg 也立刻覆盖，模型后续 mcp__tg 调用 reply 到最新 V 消息——符合直觉）
   - `inbox.put_nowait({"type":"user","message":{"role":"user","content":blocks}})`
   - 立即返回（不阻塞 PTB handler）
3. 后台 worker._input_iter() 从 inbox 取出 → SDK 写到 CLI stdin
4. CLI 在 next step 把这条 user message 注入对话流
5. 模型生成响应 → SDK receive_messages 流出 `AssistantMessage` / `StreamEvent` / `ToolUseBlock` / `ResultMessage`
6. event consumer 把流式事件 dispatch 到 streaming reply（同 turn 内编辑同一条 TG message）
7. `ResultMessage` 标记 turn 结束 → 持久化 sid / fire hooks / 清理 turn-bound state（text_message buffer 等）

### 多条消息（V 在 turn 进行中又发）

每条独立 `worker.submit(payload)`，**不**做 bot 端合并。让 CLI/模型自己在 turn 内合适的位置消费。模型上下文里能看到："用户在我刚才回答时又说了 X"，自己决定回应。

终端体验 1:1 复刻。

### Reply 锚点 (实现版本)

- **第一条** user input 触发 turn 时, worker 把它设为 `_turn_payload` 锚点; 模型流出 text/tool 都编辑这条 reply
- **mid-turn 第二条** user input 不创建独立 reply (跟终端 CC 单 stream 同构); 但记到 `_latest_payload` —— 当前 turn `turn_end` 时, 如果 `_latest_payload != anchor_at_start` 说明 mid-turn 又有新消息进来, 自动 promote 第二条为下个 SDK turn 的锚点 (P1.4 修复保证). 第二条不会被静默丢弃.
- 设计取舍: 不给每条 user input 独立 "thinking..." 占位 reply. 终端 CC 也没这个 — 单 stream output 即可.

### /stop 命令

新增 `cmd_stop` handler：
```python
await worker.interrupt()  # → client.interrupt() → CLI 中断当前 turn
await update.message.reply_text("⏹ 中断当前 turn")
```

`set_permission_mode` / `set_model` 是 SDK 的 bonus：未来可以在 V 发 `/safe` `/opus` 这种命令时 mid-session 切，不用 reset session。**初版不做**，留 hook。

## 不变的 (保留)

- `cc.py` 的 `CC.query()` / `_run()` / `Response` / hooks / state persistence / resume failure recovery — cron 等独立调用还在用（如 babata cron skills）
- session 持久化 schema (`{"session_id": ..., "recent_sids": [...]}`)
- session-start / session-end hook 触发时机（sid 变化时）
- bridge socket 协议、tg_mcp.py
- 4 个 launchd label 共享同一份代码（多实例 = 多进程 = 各自一份 worker）
- TG 命令 `/status` `/resume` `/restart` `/new` `/reset` 等
- `_in_flight` 计数（worker 运行 turn 时 +1，turn 结束 -1，graceful shutdown 仍能 drain）
- `concurrent_updates(True)` 保留（PTB handler 不阻塞，确保 V 发消息能立即被接收）

## 改动清单

| 文件 | 性质 | 估行数 |
|---|---|---|
| `cc.py` | 加 `LiveSession` 类（保留 `CC` 类不动） | +200 |
| `bot.py` | 加 `ChannelWorker` 类、`_process` 改 enqueue、`main()` 启 worker、加 `cmd_stop`、graceful shutdown 适配 | +180 / -40 |
| `STREAMING_INPUT_DESIGN.md` | 本文档 | new |
| `RFC_NOTES.md` | 实施过程的关键决策 / 偏离 | new (动手时记) |

## 关键设计决策

1. **`LiveSession` 跟 `CC` 并存**，不替换。`CC.query()` 给 cron 等"一次性"调用；`LiveSession` 给 TG/WX 等"长连接对话"调用。
2. **每条 user input 独立 yield，不做 bot 端合并**。把"看情况合并"的判断完全留给模型。
3. **bridge.set_context 在 submit 时立即更新**。即便 mid-turn 来的 msg 也覆盖；模型后续 mcp__tg 调用 reply 到 V 最新一条，符合直觉。
4. **Turn reply 单 message 流式编辑**，跟终端的单 stream output 同构。
5. **session_id 经由 ResultMessage 持久化**，仍然 fire session-start hook 在 sid 变化时（首次启动 / SDK fork 新 sid）。
6. **interrupt() 暴露成 `/stop` 命令**，给 V 终端 ESC 的等价手段。
7. **graceful shutdown**：`_inflight_enter` 在 worker 启 turn 时调；turn 完调 `_inflight_exit`。`_wait_inflight_drain` 仍能等到所有 turn 结束才放 launchd 重拉。
8. **inbox 不持久化**：进程重启会丢未消费的消息（V 必须重发）。后续如果发现痛点可以加。
9. **resume failure recovery**：`LiveSession.connect()` 内部仍跑 `CC.query()` 那种 try/except 逻辑——resume 老 sid 失败时 inject 最近 N 轮 history 到 system_prompt 重连。

## 风险 / Open questions

- **CLI 在 turn 进行中能否真的注入 user message**？SDK transport 写 stdin 是确定的，但 CLI 内部消费这条 message 的时机是黑箱。**初步信心高**（终端 CC V 已经验证有此行为，终端 CC 走的也是这套 SDK 内核）。**实测**：单元/集成测试见下。
- **`receive_messages` 是 single-shot async generator 还是持续**？需要源码确认。如果 single-shot 必须每个 turn 重新调用——但 client 长连接没 disconnect 应该可以连续 yield 多个 ResultMessage。
- **多 message 进来时 SDK transport `write()` 是否线程安全 / async-safe**？看源码确认；初步认为 asyncio 单线程下 sequential await 没问题。
- **error 场景**：CLI 子进程崩溃 / 网络 hiccup → `receive_messages` 抛错？需要 worker 自动重连 + state 恢复。初版做最小：worker task crash 时记 log + TG 推 V，让 launchd 重拉。

## 实施步骤

阶段 A — 实现:
1. ✅ Worktree + design doc (this commit)
2. cc.py 加 `LiveSession` 类
3. bot.py 加 `ChannelWorker` 类
4. `_process` 改 enqueue、`main()` 启 worker、加 `cmd_stop`
5. 自检：`uv run python -c "import bot, cc"` 不报 ImportError；syntax check

阶段 B — 验证:
6. 写一个最小 demo `scripts/demo_live_session.py` 直接跑 LiveSession 不上 TG，验证 SDK turn 内注入行为
7. spawn codex (gpt-5.4 xhigh) adversarial review 整个 diff，重点找 race / leak / hook 缺漏 / shutdown bug
8. 应用 codex feedback

阶段 C — 部署:
9. git diff `feature/streaming-input` vs `main` 给 V 看
10. V apply / cherry-pick / `self-ops.sh restart` 4 个 instance
11. 实战验证：V 在 TG 发消息中途打断 / 多消息流 / `/stop` / `/restart`

## 测试 / 验收 checklist (V)

- [ ] 单条消息：行为跟现在一样（reply 流式编辑、session 持续）
- [ ] 多条消息：第二条进来时第一条不被打断；第二条立即可见模型已注意到
- [ ] `/stop` 命令：当前 turn 立刻终止，模型停止输出
- [ ] V 重启 launchd → 当前 turn 等完才退（graceful drain）
- [ ] `/resume` `/status` `/new` `/reset` 等命令仍工作
- [ ] cron 跑 babata-cron 的脚本不受影响（cron 走 `CC.query()` 不走 LiveSession）
- [ ] 24h 长跑无 leak（client 不重建，但 inbox 应被 drain）

---

## 未决 (V 可补)

(V 没回答前面两个细节问题，按推荐方向走：)
- ESC 等价 = `/stop` 命令 ✓
- 多消息策略 = 每条独立 yield，不 bot 端合并 ✓

如果 V 后续想改，加 `/stop` 改名 / 调多消息合并策略，都是局部调整。
