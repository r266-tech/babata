from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_package_has_simple_entrypoints():
    assert (ROOT / "codex-provider.ps1").is_file()
    assert (ROOT / "codex-provider.cmd").is_file()
    assert (ROOT / "install.ps1").is_file()
    assert (ROOT / "README.md").is_file()
    assert not (ROOT / "set-codex-provider.ps1").exists()
    assert not (ROOT / "setup-codex-provider.cmd").exists()


def test_visible_ui_is_chinese_and_simple():
    script = read("codex-provider.ps1")
    readme = read("README.md")

    for text in (script, readme):
        assert "Codex 渠道切换" in text
        assert "新增 API" in text
        assert "新增账号" in text
        assert "切换渠道" in text
        assert "当前状态" in text
        assert "codex-provider add-api" in text
        assert "codex-provider use" in text

    assert "Normalize Sessions" not in readme
    assert "Provider paths" not in readme


def test_core_windows_paths_and_session_handling_are_present():
    script = read("codex-provider.ps1")

    assert 'Join-Path $HomeDir ".codex"' in script
    assert 'Join-Path $CodexHome "state_5.sqlite"' in script
    assert 'Join-Path $CodexHome "sessions"' in script
    assert 'Join-Path $CodexHome "archived_sessions"' in script
    assert "sqlite3" in script
    assert '"model_provider":"openai"' in script
    assert '"model_provider":"OpenAI"' in script
    assert "Start-Codex" in script
    assert "Stop-Codex" in script


def test_no_real_secret_examples():
    text_files = [".ps1", ".cmd", ".md", ".txt", ".py", ".gitignore"]
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in ROOT.rglob("*")
        if path.is_file()
        and "tests" not in path.relative_to(ROOT).parts
        and (path.suffix in text_files or path.name == ".gitignore")
    )

    assert "sk-xxxxxx" in combined
    assert "zhongrun" not in combined.lower()
    assert "shanda" not in combined.lower()
    assert "sk-proj-" not in combined
