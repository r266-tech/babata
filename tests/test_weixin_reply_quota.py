"""WeChat reply quota: about 10 sends per inbound message, then queue until the user writes again."""
from __future__ import annotations

import asyncio
import os
import stat

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("ALLOWED_USER_ID", "0")

import weixin_bot as wb
import weixin_ilink as wi


def _client(tmp_path, refuse_after=None):
    """Client whose server records each delivered text and can start refusing (ret=-2)."""
    client = wi.WeixinClient(base_url="https://b", token="t", quota_path=tmp_path / "quota.json")
    client.delivered = []

    async def fake_post(_endpoint, body, **_k):
        if refuse_after is not None and len(client.delivered) >= refuse_after:
            return {"ret": -2, "errmsg": "prepare failed"}
        item = body["msg"]["item_list"][0]
        client.delivered.append((body["msg"].get("context_token"), item["text_item"]["text"]))
        return {"message_id": len(client.delivered)}

    client._post = fake_post
    return client


def _send(client, text, token="tok1"):
    return asyncio.run(client.send_message("v", [wi.text_item(text)], context_token=token))


def test_tenth_send_becomes_the_notice_and_the_rest_queue(tmp_path):
    client = _client(tmp_path)
    for i in range(9):
        _send(client, f"m{i}")
    assert client.quota_left("tok1") == 0
    with pytest.raises(wi.WeixinQuotaExhausted):
        _send(client, "m9")
    with pytest.raises(wi.WeixinQuotaExhausted):
        _send(client, "m10")
    texts = [t for _, t in client.delivered]
    assert texts == [f"m{i}" for i in range(9)] + [wi.QUOTA_NOTICE]  # notice once, nothing else
    assert [b["items"][0]["text_item"]["text"] for b in client._quota["backlog"]] == ["m9", "m10"]
    assert stat.S_IMODE(os.stat(tmp_path / "quota.json").st_mode) == 0o600


def test_next_message_flushes_backlog_in_order_even_after_restart(tmp_path):
    client = _client(tmp_path)
    for i in range(9):
        _send(client, f"m{i}")
    for text in ("q1", "q2"):
        with pytest.raises(wi.WeixinQuotaExhausted):
            _send(client, text)
    restarted = _client(tmp_path)
    assert asyncio.run(restarted.flush_backlog("v", "tok2")) == 2
    assert restarted.delivered == [("tok2", "q1"), ("tok2", "q2")]
    assert restarted._quota["backlog"] == []
    assert restarted.quota_left("tok2") == 7


def test_long_backlog_spends_one_quota_and_keeps_the_rest(tmp_path):
    client = _client(tmp_path)
    client._quota["sent"]["old"] = wi.REPLY_QUOTA
    for i in range(12):
        with pytest.raises(wi.WeixinQuotaExhausted):
            _send(client, f"q{i}", token="old")
    assert asyncio.run(client.flush_backlog("v", "new")) == 9
    assert [t for _, t in client.delivered] == [f"q{i}" for i in range(9)] + [wi.QUOTA_NOTICE]
    assert [b["items"][0]["text_item"]["text"] for b in client._quota["backlog"]] == ["q9", "q10", "q11"]


def test_server_refusal_marks_quota_used_and_queues(tmp_path):
    client = _client(tmp_path, refuse_after=3)  # the server counted sends we never saw (e.g. before a restart)
    for i in range(3):
        _send(client, f"m{i}")
    with pytest.raises(wi.WeixinQuotaExhausted, match="ret=-2"):
        _send(client, "m3")
    assert client.quota_left("tok1") == 0
    with pytest.raises(wi.WeixinQuotaExhausted):
        _send(client, "m4")
    assert len(client.delivered) == 3  # no notice: the server is refusing anyway
    assert len(client._quota["backlog"]) == 2


def test_sends_without_context_token_are_untracked(tmp_path):
    client = _client(tmp_path)
    for i in range(12):
        asyncio.run(client.send_message("v", [wi.text_item(f"m{i}")]))
    assert len(client.delivered) == 12 and client.quota_left(None) is None


def test_queued_bubble_counts_as_handled_and_is_not_retried(tmp_path):
    calls = []

    class FakeClient:
        async def send_message(self, *_a, **_k):
            calls.append(1)
            raise wi.WeixinQuotaExhausted("queued")

    result = asyncio.run(wb._send_wx_bubble(FakeClient(), "v", "tok", "hello"))
    assert result == wb.WxBubbleSendResult(sent_any=True, ok=True)  # no replay of the final text
    assert calls == [1]


@pytest.mark.parametrize("left,expected", [
    (6, ["a", "b", "c"]),
    (5, ["a\n\nb\n\nc"]),
])
def test_low_quota_merges_the_rest_of_the_turn_into_one_bubble(left, expected):
    sent = []

    async def send_bubble(text):
        sent.append(text.strip())
        return wb.WxBubbleSendResult(sent_any=True, ok=True)

    async def run():
        coalescer = wb.WxStreamCoalescer(send_bubble, lambda: left)
        for chunk in ("a\n\n\n", "b\n\n\n", "c"):
            await coalescer.on_stream(None, None, chunk, None)
        await coalescer.drain(allow_hard_cut=True)

    asyncio.run(run())
    assert sent == expected
