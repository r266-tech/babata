"""Voice transcription, TTS, and image processing for TG media."""

import asyncio
import base64
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


async def _whisper(wav_path: Path) -> str | None:
    """Run whisper-cli on a 16kHz mono wav → text."""
    proc = await asyncio.create_subprocess_exec(
        "whisper-cli", "-m",
        str(Path.home() / ".cache/whisper-cpp/ggml-base.bin"),
        "-f", str(wav_path), "--no-timestamps", "-l", "auto",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
    lines = []
    for line in stdout.decode().strip().split("\n"):
        line = line.strip()
        if line.startswith("["):
            idx = line.find("]")
            if idx != -1:
                line = line[idx + 1:].strip()
        if line:
            lines.append(line)
    return " ".join(lines) if lines else None


async def transcribe_voice(ogg_path: Path) -> str | None:
    """OGG voice → text via ffmpeg + whisper-cli."""
    wav_path = ogg_path.with_suffix(".wav")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(ogg_path),
            "-ar", "16000", "-ac", "1", str(wav_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)
        if not wav_path.exists():
            return None
        return await _whisper(wav_path)
    except Exception as e:
        log.warning("Voice transcription failed: %s", e)
        return None
    finally:
        wav_path.unlink(missing_ok=True)


_VIDEO_API_URL = os.environ.get("VIDEO_API_URL")
_VIDEO_MODEL = os.environ.get("VIDEO_MODEL", "mimo-v2-omni")
_VIDEO_API_KEY = os.environ.get("VIDEO_API_KEY")


async def understand_video(video_path: Path, question: str = "") -> str | None:
    """Send video to an OpenAI-compatible multimodal endpoint → text description.

    Physical: CC SDK doesn't accept video. Delegate to a video-native model
    (e.g. mimo-v2-omni), feed the textual summary back to CC.
    Returns None if no endpoint configured or call fails.
    """
    if not _VIDEO_API_URL:
        return None
    size = video_path.stat().st_size
    if size > 10 * 1024 * 1024:
        return f"[Video too large for base64 upload: {size // 1024 // 1024}MB > 10MB]"

    import httpx
    data_url = f"data:video/mp4;base64,{base64.b64encode(video_path.read_bytes()).decode()}"
    prompt = question or "请详细描述这段视频的画面和声音内容。"

    headers = {"Content-Type": "application/json"}
    if _VIDEO_API_KEY:
        headers["api-key"] = _VIDEO_API_KEY
        headers["Authorization"] = f"Bearer {_VIDEO_API_KEY}"

    body = {
        "model": _VIDEO_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "video_url",
                        "video_url": {"url": data_url},
                        "fps": 2,
                        "media_resolution": "default",
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_completion_tokens": 2048,
    }

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(
                f"{_VIDEO_API_URL.rstrip('/')}/chat/completions",
                json=body, headers=headers,
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"].get("content") or None
    except Exception as e:
        log.warning("Video understanding failed: %s", e)
        return None


def image_to_base64(path: Path) -> dict[str, str]:
    """Image file → {media_type, data} for CC SDK."""
    media_type = _MEDIA_TYPES.get(path.suffix.lower(), "image/jpeg")
    data = base64.b64encode(path.read_bytes()).decode()
    return {"media_type": media_type, "data": data}


_MD_STRIP = [
    (re.compile(r"```.*?```", re.DOTALL), ""),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"), r"\1"),
    (re.compile(r"~~(.+?)~~"), r"\1"),
    (re.compile(r"^#{1,6}\s*", re.MULTILINE), ""),
    (re.compile(r"!?\[([^\]]*)\]\([^)]+\)"), r"\1"),
    (re.compile(r"https?://\S+"), ""),
    (re.compile(r"^[-*+]\s+", re.MULTILINE), ""),
    (re.compile(r"^-{3,}$", re.MULTILINE), ""),
    (re.compile(r"\|.*?\|", re.MULTILINE), ""),
]


def _strip_md(text: str) -> str:
    for pat, repl in _MD_STRIP:
        text = pat.sub(repl, text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_TTS_URL = os.environ.get("TTS_URL")
_TTS_MODEL = os.environ.get("TTS_MODEL", "tts-1")
_TTS_VOICE = os.environ.get("TTS_VOICE", "nova")
_TTS_API_KEY = os.environ.get("TTS_API_KEY")
_TTS_BACKEND = os.environ.get("TTS_BACKEND", "openai")  # "mimo" | "openai"


async def _tts_mimo(text: str, voice: str) -> bytes | None:
    """Mimo-v2-tts native chat/completions — script layer wraps CC's simple text
    (which may include <style>...</style> prefix and (cue) markers) into the
    official mimo request spec."""
    import httpx
    headers = {"Content-Type": "application/json"}
    if _TTS_API_KEY:
        headers["api-key"] = _TTS_API_KEY  # mimo uses api-key header, not Bearer
    body = {
        "model": _TTS_MODEL,
        "messages": [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": text},
        ],
        "audio": {"format": "mp3", "voice": voice or "mimo_default"},
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{_TTS_URL.rstrip('/')}/chat/completions",
            json=body, headers=headers,
        )
        if r.status_code != 200:
            log.warning("mimo TTS %d: %s", r.status_code, r.text[:400])
            return None
        audio_b64 = r.json()["choices"][0]["message"]["audio"]["data"]
        return base64.b64decode(audio_b64)


async def _tts_openai(text: str, voice: str) -> bytes | None:
    """OpenAI-compatible /audio/speech."""
    import httpx
    headers = {"Content-Type": "application/json"}
    if _TTS_API_KEY:
        headers["Authorization"] = f"Bearer {_TTS_API_KEY}"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{_TTS_URL.rstrip('/')}/audio/speech",
            json={"model": _TTS_MODEL, "input": text, "voice": voice,
                  "response_format": "mp3"},
            headers=headers,
        )
        r.raise_for_status()
        return r.content


async def text_to_voice(text: str, voice: str | None = None) -> Path | None:
    """Text → OGG/Opus voice file. Returns path or None.

    Backend priority: TTS_URL (mimo/openai auto-detected) else free edge-tts.
    """
    import tempfile
    import uuid

    clean = _strip_md(text)[:4000]
    if not clean:
        return None

    tmp = Path(tempfile.gettempdir())
    mp3 = tmp / f"tts_{uuid.uuid4().hex}.mp3"
    ogg = mp3.with_suffix(".ogg")

    try:
        if _TTS_URL:
            v = voice or _TTS_VOICE
            if _TTS_BACKEND == "mimo":
                audio_bytes = await _tts_mimo(clean, v)
            else:
                audio_bytes = await _tts_openai(clean, v)
            if not audio_bytes:
                return None
            mp3.write_bytes(audio_bytes)
        else:
            import edge_tts
            communicator = edge_tts.Communicate(clean, voice or "zh-CN-XiaoxiaoNeural")
            await communicator.save(str(mp3))

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(mp3),
            "-c:a", "libopus", "-b:a", "64k", "-vbr", "off",
            "-ar", "48000", "-ac", "1",
            str(ogg),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=60)

        return ogg if ogg.exists() else None
    except Exception as e:
        log.warning("TTS failed: %s", e)
        return None
    finally:
        mp3.unlink(missing_ok=True)
