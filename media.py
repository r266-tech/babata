"""Voice transcription and image processing for TG media."""

import asyncio
import base64
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


async def transcribe_voice(ogg_path: Path) -> str | None:
    """OGG voice → text via ffmpeg + whisper-cli."""
    wav_path = ogg_path.with_suffix(".wav")

    try:
        # OGG → WAV (16kHz mono)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(ogg_path),
            "-ar", "16000", "-ac", "1", str(wav_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)

        if not wav_path.exists():
            return None

        # Transcribe
        proc = await asyncio.create_subprocess_exec(
            "whisper-cli", "-m",
            str(Path.home() / ".cache/whisper-cpp/ggml-base.bin"),
            "-f", str(wav_path), "--no-timestamps", "-l", "auto",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)

        # Clean whisper output (strip timestamp prefixes)
        lines = []
        for line in stdout.decode().strip().split("\n"):
            line = line.strip()
            if line.startswith("["):
                idx = line.find("]")
                if idx != -1:
                    line = line[idx + 1 :].strip()
            if line:
                lines.append(line)

        return " ".join(lines) if lines else None

    except Exception as e:
        log.warning("Voice transcription failed: %s", e)
        return None

    finally:
        wav_path.unlink(missing_ok=True)


def image_to_base64(path: Path) -> dict[str, str]:
    """Image file → {media_type, data} for CC SDK."""
    media_type = _MEDIA_TYPES.get(path.suffix.lower(), "image/jpeg")
    data = base64.b64encode(path.read_bytes()).decode()
    return {"media_type": media_type, "data": data}
