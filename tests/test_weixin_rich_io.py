"""WeChat rich I/O: Markdown compat, inbound media decode, quotes, tool cards."""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("ALLOWED_USER_ID", "0")

import weixin_account
import weixin_bot as wb
import weixin_ilink as wi


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(weixin_account, "_ROOT", tmp_path / "weixin")
    monkeypatch.setattr(wb, "_INBOUND_DIR", tmp_path / "inbound")


# ── outbound Markdown ────────────────────────────────────────────────


@pytest.mark.parametrize("src,expected", [
    ("**粗体** *斜体中文* *english*", "**粗体** 斜体中文 *english*"),
    ("***中文三星*** _中文_ snake_case_var", "中文三星 中文 snake_case_var"),
    ("##### 五级\n# 一级", "五级\n# 一级"),
    ("* bullet\n- item", "* bullet\n- item"),
    ("2 * 3 * 4 = 24", "2 * 3 * 4 = 24"),
    ("```\n*中文*\n```\n`*中文*` *中文*", "```\n*中文*\n```\n`*中文*` 中文"),
    ("| a | b |\n|---|---|", "| a | b |\n|---|---|"),
])
def test_wechat_markdown_keeps_rendered_syntax(src, expected):
    assert wi.wechat_markdown(src) == (expected, [])


def test_wechat_markdown_extracts_images():
    text, images = wi.wechat_markdown('看 ![a](~/x.png) 和 ![b](https://e.com/y.jpg "t")')
    assert text == "看  和 "
    assert images == ["~/x.png", "https://e.com/y.jpg"]


def test_send_bubble_delivers_markdown_image_and_remembers_text(tmp_path):
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n-bytes")

    class FakeClient:
        def __init__(self):
            self.sent = []
            self.uploads = []

        async def upload_media(self, data, **kwargs):
            self.uploads.append(data)
            return {"encrypt_query_param": "q", "aes_key": "k", "cipher_size": 16}

        async def send_message(self, to, items, **kwargs):
            self.sent.append(items[0]["type"])
            return {"message_id": 42 + len(self.sent)}

    async def run():
        client = FakeClient()
        result = await wb._send_wx_bubble(client, "u1", "ctx", f"图如下 ![c]({img})", "acc")
        assert result == wb.WxBubbleSendResult(sent_any=True, ok=True)
        assert client.sent == [wi.ITEM_TEXT, wi.ITEM_IMAGE]
        assert client.uploads == [b"\x89PNG\r\n\x1a\n-bytes"]
        assert weixin_account.recall_message("acc", 43) == "图如下"

    asyncio.run(run())


# ── turn gathering ───────────────────────────────────────────────────


class PollClient:
    def __init__(self, batches):
        self.batches = list(batches)
        self.bufs = []

    async def get_updates(self, buf):
        self.bufs.append(buf)
        if self.batches:
            return self.batches.pop(0)
        await asyncio.sleep(3600)


def _text(t):
    return {"message_type": 1, "from_user_id": "u", "item_list": [{"type": wi.ITEM_TEXT, "text_item": {"text": t}}]}


def _file():
    return {"message_type": 1, "from_user_id": "u", "item_list": [{"type": wi.ITEM_FILE, "file_item": {}}]}


def test_late_file_joins_the_text_that_mentions_it(monkeypatch):
    monkeypatch.setattr(wb, "_WX_GRACE_S", 0.05)
    monkeypatch.setattr(wb, "_WX_MEDIA_GRACE_S", 0.2)
    msgs = [_text("我发了个文件")]
    client = PollClient([{"get_updates_buf": "b2", "msgs": [_file()]}])
    buf = asyncio.run(wb._gather_stragglers(client, "b1", msgs))
    assert buf == "b2"
    assert [m["item_list"][0]["type"] for m in msgs] == [wi.ITEM_TEXT, wi.ITEM_FILE]
    assert [len(u) for u in wb._coalesce_update_msgs(msgs)] == [2]


def test_gathering_stops_after_quiet_window(monkeypatch):
    monkeypatch.setattr(wb, "_WX_GRACE_S", 0.05)
    msgs = [_text("你好")]
    client = PollClient([])
    assert asyncio.run(wb._gather_stragglers(client, "b1", msgs)) == "b1"
    assert client.bufs == ["b1"] and len(msgs) == 1


def test_media_hint_only_for_text_without_media():
    assert wb._awaits_media([_text("看下这张截图")]) is True
    assert wb._awaits_media([_text("你好")]) is False
    assert wb._awaits_media([_text("文件"), _file()]) is False


# ── inbound decode ───────────────────────────────────────────────────


class MediaClient:
    def __init__(self, payload=b"data", fail=False):
        self.payload = payload
        self.fail = fail
        self.calls = []

    async def download_media(self, media, **kwargs):
        self.calls.append(media)
        if self.fail or media.get("bad"):
            raise RuntimeError("cdn down")
        return self.payload


def test_voice_with_server_text_skips_download():
    client = MediaClient()
    item = {"type": wi.ITEM_VOICE, "voice_item": {"text": "你好", "playtime": 3200}}
    assert asyncio.run(wb._decode_item(client, item)) == ("[语音 3s] 你好", [])
    assert client.calls == []


def test_voice_transcribe_failure_keeps_audio(monkeypatch):
    async def boom(_path):
        raise RuntimeError("stt off")

    monkeypatch.setattr(wb, "transcribe_silk", boom)
    item = {"type": wi.ITEM_VOICE, "voice_item": {"media": {"aes_key": "k"}}}
    text, _ = asyncio.run(wb._decode_item(MediaClient(b"silk"), item))
    assert "语音转文字失败" in text and ".silk" in text
    path = text.rsplit("原始音频: ", 1)[1].rstrip("]")
    assert open(path, "rb").read() == b"silk"


def test_voice_non_silk_goes_through_ffmpeg_path(monkeypatch):
    seen = []

    async def fake_voice(path):
        seen.append(path.suffix)
        return "mp3 text"

    monkeypatch.setattr(wb, "transcribe_voice", fake_voice)
    item = {"type": wi.ITEM_VOICE, "voice_item": {"media": {}, "encode_type": 7}}
    assert asyncio.run(wb._decode_item(MediaClient(), item)) == ("[语音] mp3 text", [])
    assert seen == [".mp3"]


def test_video_is_kept_on_disk_without_video_model(monkeypatch):
    async def no_model(_path):
        return None

    monkeypatch.setattr(wb, "understand_video", no_model)
    item = {"type": wi.ITEM_VIDEO, "video_item": {"media": {}, "play_length": 12}}
    text, images = asyncio.run(wb._decode_item(MediaClient(b"mp4"), item))
    assert text.startswith("[视频: ") and ", 12s, 3B]" in text
    path = text[len("[视频: "):].split(",")[0]
    assert open(path, "rb").read() == b"mp4"
    assert images == []


def test_image_falls_back_to_thumbnail():
    client = MediaClient(b"\x89PNG....")
    item = {"type": wi.ITEM_IMAGE, "image_item": {"media": {"bad": 1}, "thumb_media": {}}}
    text, images = asyncio.run(wb._decode_item(client, item))
    assert text.startswith("[图片: ") and text.endswith(".png]")
    assert images[0]["media_type"] == "image/png"


def test_image_file_is_also_inlined():
    item = {"type": wi.ITEM_FILE, "file_item": {"media": {}, "file_name": "截图.png"}}
    text, images = asyncio.run(wb._decode_item(MediaClient(b"\x89PNG...."), item))
    assert "截图.png" in text and "(8B)" in text
    assert len(images) == 1


def test_unknown_item_type_exposes_raw_payload():
    item = {"type": 99, "link_item": {"url": "https://x"}}
    text, _ = asyncio.run(wb._decode_item(MediaClient(), item))
    assert "type=99" in text and "https://x" in text


# ── quotes ───────────────────────────────────────────────────────────


def test_svr_id_only_quote_resolves_from_cache():
    weixin_account.remember_message("acc", "777", "昨天那段回复")
    assert wb._describe_ref({"svr_id": "777"}, "acc") == "[引用: 昨天那段回复]"
    assert wb._describe_ref({"svr_id": "888"}, "acc") == "[引用了一条消息，原文未缓存]"


def test_quoted_image_is_downloaded_into_turn():
    client = MediaClient(b"\x89PNG....")
    msg = {
        "from_user_id": "u1",
        "message_id": 5,
        "item_list": [{
            "type": wi.ITEM_TEXT,
            "text_item": {"text": "这张图是什么"},
            "ref_msg": {"message_item": {"type": wi.ITEM_IMAGE, "image_item": {"media": {}}}},
        }],
    }
    _from, _ctx, prompt, images = asyncio.run(wb._collect_wx_turn_inputs(client, [msg], "acc"))
    assert prompt.startswith("[引用了一条图片] [图片: ")
    assert prompt.endswith("这张图是什么")
    assert len(images) == 1
    assert weixin_account.recall_message("acc", 5) == "这张图是什么"


def test_quote_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(weixin_account, "_QUOTE_KEEP", 3)
    for i in range(5):
        weixin_account.remember_message("acc", i, f"m{i}")
    assert weixin_account.recall_message("acc", 0) is None
    assert weixin_account.recall_message("acc", 4) == "m4"


# ── protocol layer ───────────────────────────────────────────────────


def test_download_without_full_url_uses_cdn_download_endpoint(monkeypatch):
    seen = {}

    class FakeResp:
        content = b"plain-bytes"

        def raise_for_status(self):
            pass

    class FakeHttp:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen["url"] = url
            return FakeResp()

    monkeypatch.setattr(wi.httpx, "AsyncClient", FakeHttp)
    client = wi.WeixinClient(base_url="https://b", token="t", cdn_base_url="https://cdn/c2c")
    data = asyncio.run(client.download_media({"encrypt_query_param": "a/b+c="}))
    assert data == b"plain-bytes"  # no key → plaintext image
    assert seen["url"] == "https://cdn/c2c/download?encrypted_query_param=a%2Fb%2Bc%3D"


def test_send_message_raises_on_nonzero_ret(monkeypatch):
    client = wi.WeixinClient(base_url="https://b", token="t")

    async def fake_post(*_a, **_k):
        return {"ret": -2, "errmsg": "bad"}

    monkeypatch.setattr(client, "_post", fake_post)
    with pytest.raises(wi.WeixinApiError, match="ret=-2"):
        asyncio.run(client.send_message("u", [wi.text_item("x")]))


def test_markdown_image_refuses_non_image_file(tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_text("-----BEGIN KEY-----")

    class FakeClient:
        def __init__(self):
            self.texts = []

        async def upload_media(self, *_a, **_k):
            raise AssertionError("must not upload")

        async def send_message(self, _to, items, **_k):
            self.texts.append(items[0]["text_item"]["text"])
            return {}

    client = FakeClient()
    asyncio.run(wb._send_wx_bubble(client, "u1", "ctx", f"![x]({secret})"))
    assert client.texts == ["[图片发送失败]"]


def test_image_signatures_are_strict():
    assert wi._is_image_bytes(b"\x89PNG\r\n\x1a\nrest")
    assert not wi._is_image_bytes(b"BM secret that starts with BM")
    bmp = b"BM" + (10).to_bytes(4, "little") + b"\0" * 4
    assert wi._is_image_bytes(bmp)


# ── heartbeat ────────────────────────────────────────────────────────


def _run_heartbeat(monkeypatch, tmp_path, ages):
    """Drive _heartbeat_loop through one tick per peer age; return alerts sent."""
    import time as _time

    me, peer = tmp_path / "me", tmp_path / "peer"
    peer.touch()
    monkeypatch.setattr(wb, "_HEARTBEAT_ME", me)
    monkeypatch.setattr(wb, "_HEARTBEAT_PEER", peer)
    monkeypatch.setattr(wb, "load_allow_from", lambda _a: ["u"])
    ticks = iter(ages)

    async def tick(_s):
        try:
            age = next(ticks)
        except StopIteration:
            raise asyncio.CancelledError
        os.utime(peer, (_time.time() - age, _time.time() - age))

    sent = []

    class Client:
        async def send_message(self, to, items, **_k):
            sent.append(items[0]["text_item"]["text"])

    monkeypatch.setattr(wb.asyncio, "sleep", tick)
    os.utime(peer, (_time.time() - ages[0],) * 2)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(wb._heartbeat_loop(Client(), "acc"))
    return sent


def test_retired_peer_never_alerts(monkeypatch, tmp_path):
    assert _run_heartbeat(monkeypatch, tmp_path, [9999, 9999, 9999]) == []


def test_peer_that_dies_mid_run_alerts_once(monkeypatch, tmp_path):
    sent = _run_heartbeat(monkeypatch, tmp_path, [10, 500, 600, 700])
    assert len(sent) == 1 and "心跳" in sent[0]
