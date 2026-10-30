import os
import shutil
import subprocess
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _commit(repo: Path, message: str, env: dict[str, str]) -> None:
    subprocess.run(["git", "add", "."], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, env=env, check=True)


def test_leak_guard_history_mode_catches_removed_secret(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    shutil.copy2(_REPO / "tests" / "leak_guard.sh", repo / "tests" / "leak_guard.sh")

    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["BABATA_LEAK_GUARD_PATTERNS"] = str(tmp_path / "patterns")
    Path(env["BABATA_LEAK_GUARD_PATTERNS"]).write_text("secret-token-123\n")

    subprocess.run(["git", "init"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, env=env, check=True)
    (repo / "README.md").write_text("safe\n")
    _commit(repo, "initial", env)

    (repo / "leak.txt").write_text("secret-token-123\n")
    _commit(repo, "leak", env)
    (repo / "leak.txt").unlink()
    _commit(repo, "remove leak", env)

    tree = _run(["bash", "tests/leak_guard.sh"], cwd=repo, env=env)
    history = _run(["bash", "tests/leak_guard.sh", "--history"], cwd=repo, env=env)

    assert tree.returncode == 0
    assert "leak_guard: clean" in tree.stdout
    assert history.returncode == 1
    assert "history" in history.stdout
    assert "<redacted>" in history.stdout
    assert "secret-token-123" not in history.stdout
