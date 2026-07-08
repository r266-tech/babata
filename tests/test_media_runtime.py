import asyncio
import inspect
import tomllib
from pathlib import Path

import pytest

import media


def test_stt_wav_requires_config_or_explicit_local(monkeypatch, tmp_path):
    monkeypatch.delenv("MIMO_API_URL", raising=False)
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.delenv("BABATA_STT_LOCAL", raising=False)

    with pytest.raises(RuntimeError, match="BABATA_STT_LOCAL=1"):
        asyncio.run(media._stt_wav(tmp_path / "voice.wav"))


def test_stt_wav_uses_local_only_when_explicit(monkeypatch, tmp_path):
    calls: list[Path] = []

    async def fake_stt_local(path: Path) -> str:
        calls.append(path)
        return "ok"

    monkeypatch.delenv("MIMO_API_URL", raising=False)
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.setenv("BABATA_STT_LOCAL", "1")
    monkeypatch.setattr(media, "_stt_local", fake_stt_local)

    wav = tmp_path / "voice.wav"
    assert asyncio.run(media._stt_wav(wav)) == "ok"
    assert calls == [wav]


def test_text_to_voice_uses_shared_tts_to_mp3_path():
    source = inspect.getsource(media.text_to_voice)

    assert "_tts_to_mp3" in source
    assert "_tts_mimo" not in source
    assert "_tts_openai" not in source
    assert "edge_tts.Communicate" not in source


def test_faster_whisper_is_optional_dependency():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert all("faster-whisper" not in dep for dep in pyproject["project"]["dependencies"])
    assert pyproject["project"]["optional-dependencies"]["stt-local"] == ["faster-whisper>=1.0"]
