#!/usr/bin/env python3
"""End-to-end smoke test for the 5 wx_send_* MCP actions.

Runs against the bot's already-running bridge socket — we are a pure MCP
client here, no bridge of our own. Prerequisites:
  1. weixin_bot.py is currently running
  2. V sent at least one message to the bot recently (so bridge.set_context
     was called — otherwise the bridge has no conversation context and all
     actions respond "Error: no weixin conversation context")

Usage:
    .venv/bin/python smoke_test_weixin_mcp.py
"""

import asyncio
import base64
import json
import subprocess
import tempfile
from pathlib import Path

SOCKET_PATH = "/tmp/babata-weixin-bridge.sock"


async def call(action: str, **kwargs) -> str:
    reader, writer = await asyncio.open_unix_connection(SOCKET_PATH)
    try:
        writer.write(json.dumps({"action": action, **kwargs}).encode() + b"\n")
        await writer.drain()
        resp = await asyncio.wait_for(reader.readline(), timeout=180)
        return json.loads(resp.decode()).get("result", "(no result)")
    finally:
        writer.close()
        await writer.wait_closed()


def _make_png() -> Path:
    path = Path(tempfile.gettempdir()) / "wx-mcp-smoke.png"
    path.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
    ))
    return path


def _make_txt() -> Path:
    path = Path(tempfile.gettempdir()) / "wx-mcp-smoke.txt"
    path.write_text("babata wx_send_file smoke payload\n" * 3)
    return path


def _make_mp4() -> Path | None:
    path = Path(tempfile.gettempdir()) / "wx-mcp-smoke.mp4"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1:r=10",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(path),
            ],
            check=True, timeout=30,
        )
        return path if path.exists() else None
    except Exception as e:
        print(f"(ffmpeg synth failed: {e} — video test skipped)")
        return None


async def main() -> None:
    if not Path(SOCKET_PATH).exists():
        print(f"✗ bridge socket {SOCKET_PATH} not found — is weixin_bot.py running?")
        return

    img, doc, vid = _make_png(), _make_txt(), _make_mp4()

    tests: list[tuple[str, str, dict]] = [
        ("wx_send_typing on",  "send_typing", {"status": 1}),
        ("wx_send_text",       "send_text",   {"text": "[smoke 1/5] text"}),
        ("wx_send_image",      "send_image",  {"path": str(img), "caption": "[smoke 2/5] image"}),
        ("wx_send_file",       "send_file",   {"path": str(doc), "file_name": "smoke.txt", "caption": "[smoke 3/5] file"}),
    ]
    if vid:
        tests.append(("wx_send_video", "send_video", {"path": str(vid), "caption": "[smoke 4/5] video"}))
    else:
        tests.append(("wx_send_video (skipped — no ffmpeg)", None, {}))
    tests.append(("wx_send_typing off", "send_typing", {"status": 2}))

    print("── driving bridge via MCP-equivalent actions ──")
    results: list[tuple[str, str, bool]] = []
    for label, action, kw in tests:
        if action is None:
            results.append((label, "skipped", True))
            continue
        try:
            res = await call(action, **kw)
            ok = "Error" not in res and "Unknown" not in res
            print(f"  {'✓' if ok else '✗'} {label:30s} → {res}")
            results.append((label, res, ok))
        except Exception as e:
            print(f"  ✗ {label:30s} → EXCEPTION: {e}")
            results.append((label, f"exception: {e}", False))
        await asyncio.sleep(1)  # let server breathe between sends

    print()
    print("── SUMMARY ──")
    failed = [l for l, _, ok in results if not ok]
    for label, result, ok in results:
        mark = "✓" if ok else "✗"
        print(f"  {mark}  {label:35s}  {result}")
    if failed:
        print(f"\n{len(failed)} failure(s): {failed}")
    else:
        print("\nall green — check WeChat to confirm actual delivery.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")
