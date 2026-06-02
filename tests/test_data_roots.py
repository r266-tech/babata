from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]


def _constants_paths(env: dict[str, str]) -> dict[str, str]:
    code = """
import json
from constants import SIDEBAR_DATA_DIR, STATE_DIR, WEIXIN_DATA_DIR
print(json.dumps({
    "state": str(STATE_DIR),
    "sidebar": str(SIDEBAR_DATA_DIR),
    "weixin": str(WEIXIN_DATA_DIR),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def _base_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env.pop("BABATA_SIDEBAR_DATA_DIR", None)
    env.pop("BABATA_WEIXIN_DIR", None)
    env.pop("PROJECT_STATE_DIR", None)
    return env


def test_data_roots_default_under_project_state_dir(tmp_path: Path):
    env = _base_env(tmp_path)
    env["PROJECT_STATE_DIR"] = str(tmp_path / "state")

    paths = _constants_paths(env)

    assert paths["state"] == str(tmp_path / "state")
    assert paths["sidebar"] == str(tmp_path / "state" / "sidebar")
    assert paths["weixin"] == str(tmp_path / "state" / "weixin")


def test_data_roots_explicit_env_overrides_state_dir(tmp_path: Path):
    env = _base_env(tmp_path)
    env["PROJECT_STATE_DIR"] = str(tmp_path / "state")
    env["BABATA_SIDEBAR_DATA_DIR"] = str(tmp_path / "custom-sidebar")
    env["BABATA_WEIXIN_DIR"] = str(tmp_path / "custom-weixin")

    paths = _constants_paths(env)

    assert paths["sidebar"] == str(tmp_path / "custom-sidebar")
    assert paths["weixin"] == str(tmp_path / "custom-weixin")


def test_data_roots_resolve_migrated_legacy_symlink(tmp_path: Path):
    env = _base_env(tmp_path)
    env["PROJECT_STATE_DIR"] = str(tmp_path / "state")
    sidebar_target = tmp_path / "cc-workspace-state" / "sidebar"
    weixin_target = tmp_path / "cc-workspace-state" / "weixin"
    sidebar_target.mkdir(parents=True)
    weixin_target.mkdir(parents=True)
    legacy_root = tmp_path / "home" / ".babata"
    legacy_root.mkdir(parents=True)
    (legacy_root / "sidebar").symlink_to(sidebar_target)
    (legacy_root / "weixin").symlink_to(weixin_target)

    paths = _constants_paths(env)

    assert paths["sidebar"] == str(sidebar_target)
    assert paths["weixin"] == str(weixin_target)
