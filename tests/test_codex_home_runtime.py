import stat
import tomllib

import pytest

import codex_home_runtime


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_materialize_copies_required_and_optional_files_with_safe_modes(tmp_path):
    desktop_home = tmp_path / "desktop"
    headless_home = tmp_path / "headless"
    desktop_home.mkdir()

    (desktop_home / "auth.json").write_text('{"token":"secret"}')
    (desktop_home / "config.toml").write_text('model = "gpt-5.5"\n')
    (desktop_home / "provider-slots.json").write_text("{}")
    (desktop_home / "AGENTS.md").write_text("agent instructions")

    synced = codex_home_runtime.materialize(headless_home, desktop_home)

    assert synced == ["config.toml", "auth.json", "provider-slots.json"]
    assert (headless_home / "auth.json").read_text() == '{"token":"secret"}'
    assert (headless_home / "config.toml").read_text() == (
        'model = "gpt-5.5"\n\n[features]\nmemories = false\n'
    )
    assert _mode(headless_home) == 0o700
    assert _mode(headless_home / "auth.json") == 0o600
    assert _mode(headless_home / "config.toml") == 0o600
    assert _mode(headless_home / "provider-slots.json") == 0o600
    assert not (headless_home / "AGENTS.md").exists()
    for name in ("sessions", "log", "tmp"):
        assert (headless_home / name).is_dir()
        assert _mode(headless_home / name) == 0o700


def test_materialize_refreshes_headless_auth_after_desktop_account_switch(tmp_path):
    desktop_home = tmp_path / "desktop"
    headless_home = tmp_path / "headless"
    desktop_home.mkdir()
    (desktop_home / "auth.json").write_text('{"account":"first"}')
    (desktop_home / "config.toml").write_text('model = "gpt-5.6-sol"\n')

    codex_home_runtime.materialize(headless_home, desktop_home)
    (desktop_home / "auth.json").write_text('{"account":"latest"}')
    codex_home_runtime.materialize(headless_home, desktop_home)

    assert (headless_home / "auth.json").read_text() == '{"account":"latest"}'
    assert _mode(headless_home / "auth.json") == 0o600


def test_materialize_does_not_copy_desktop_prompt_adapter():
    assert "AGENTS.md" not in codex_home_runtime.OPTIONAL_FILES


def test_materialize_disables_existing_desktop_memories_feature(tmp_path):
    desktop_home = tmp_path / "desktop"
    headless_home = tmp_path / "headless"
    desktop_home.mkdir()
    (desktop_home / "auth.json").write_text("{}")
    (desktop_home / "config.toml").write_text(
        'model = "gpt-5.5"\n\n[features]\nhooks = true\nmemories = true\nchronicle = true\n',
        encoding="utf-8",
    )

    codex_home_runtime.materialize(headless_home, desktop_home)

    assert (headless_home / "config.toml").read_text(encoding="utf-8") == (
        'model = "gpt-5.5"\n\n[features]\nhooks = true\nmemories = false\nchronicle = true\n'
    )


def test_materialize_fails_when_required_file_is_missing(tmp_path):
    desktop_home = tmp_path / "desktop"
    desktop_home.mkdir()
    (desktop_home / "auth.json").write_text("{}")

    with pytest.raises(RuntimeError, match="required Codex file missing"):
        codex_home_runtime.materialize(tmp_path / "headless", desktop_home)


def _write_restricted_desktop_config(desktop_home, provider_body=None):
    desktop_home.mkdir()
    if provider_body is None:
        provider_body = """\
name = "OpenAI-compatible"
base_url = "https://provider.invalid/v1"
experimental_bearer_token = "provider-secret"
wire_api = "responses"
requires_openai_auth = true
supports_standalone_web_search = true
"""
    (desktop_home / "config.toml").write_text(
        f"""\
model = "gpt-5.6-sol"
model_provider = "OpenAI"
api_base_url = "https://private.invalid/backend"

[model_providers.OpenAI]
{provider_body}
[model_providers.unused]
name = "Unused"
base_url = "https://unused.invalid/v1"
experimental_bearer_token = "unused-secret"
wire_api = "responses"
requires_openai_auth = false

[mcp_servers.private]
command = "dangerous-mcp"

[apps.private]
enabled = true

[plugins.private]
enabled = true

[hooks]
session_start = ["dangerous-hook"]

[projects."/private/project"]
trust_level = "trusted"
""",
        encoding="utf-8",
    )
    (desktop_home / "auth.json").write_text(
        '{"tokens":{"access_token":"auth-secret"}}',
        encoding="utf-8",
    )
    (desktop_home / "provider-slots.json").write_text(
        '{"slot":"private"}',
        encoding="utf-8",
    )


def test_restricted_provider_materializes_only_selected_provider(tmp_path):
    desktop_home = tmp_path / "desktop"
    restricted_home = tmp_path / "restricted"
    _write_restricted_desktop_config(desktop_home)

    synced = codex_home_runtime.materialize(
        restricted_home,
        desktop_home,
        mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
    )

    assert synced == ["config.toml"]
    assert tomllib.loads(
        (restricted_home / "config.toml").read_text(encoding="utf-8")
    ) == {
        "model_provider": "OpenAI",
        "model_providers": {
            "OpenAI": {
                "name": "OpenAI-compatible",
                "base_url": "https://provider.invalid/v1",
                "experimental_bearer_token": "provider-secret",
                "wire_api": "responses",
                "requires_openai_auth": True,
                "supports_standalone_web_search": True,
            }
        },
    }
    assert _mode(restricted_home) == 0o700
    assert _mode(restricted_home / "config.toml") == 0o600
    assert _mode(restricted_home / codex_home_runtime.RESTRICTED_HOME_MARKER) == 0o600
    assert not (restricted_home / "auth.json").exists()
    assert not (restricted_home / "provider-slots.json").exists()
    assert not (restricted_home / "sessions").exists()


@pytest.mark.parametrize(
    ("provider_body", "match"),
    [
        (
            """\
name = "OpenAI-compatible"
base_url = "https://provider.invalid/v1"
experimental_bearer_token = "provider-secret"
wire_api = "responses"
requires_openai_auth = true
http_headers = { Authorization = "extra-secret" }
""",
            "approved schema",
        ),
        (
            """\
name = "OpenAI-compatible"
base_url = "https://provider.invalid/v1"
wire_api = "responses"
requires_openai_auth = true
""",
            "approved schema",
        ),
        (
            """\
name = "OpenAI-compatible"
base_url = "not-a-url"
experimental_bearer_token = "provider-secret"
wire_api = "responses"
requires_openai_auth = true
""",
            "absolute HTTP URL",
        ),
        (
            """\
name = "OpenAI-compatible"
base_url = "https://provider.invalid/v1"
experimental_bearer_token = "provider-secret"
wire_api = "chat"
requires_openai_auth = true
""",
            "wire_api",
        ),
        (
            """\
name = "OpenAI-compatible"
base_url = "https://provider.invalid/v1"
experimental_bearer_token = "provider-secret"
wire_api = "responses"
requires_openai_auth = "true"
""",
            "must be a boolean",
        ),
    ],
)
def test_restricted_provider_rejects_unapproved_or_invalid_fields(
    tmp_path,
    provider_body,
    match,
):
    desktop_home = tmp_path / "desktop"
    _write_restricted_desktop_config(desktop_home, provider_body)

    with pytest.raises(RuntimeError, match=match):
        codex_home_runtime.materialize(
            tmp_path / "restricted",
            desktop_home,
            mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
        )


def test_restricted_provider_rejects_missing_selected_provider(tmp_path):
    desktop_home = tmp_path / "desktop"
    desktop_home.mkdir()
    (desktop_home / "config.toml").write_text(
        'model_provider = "missing"\n[model_providers.other]\nname = "Other"\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="definition is missing"):
        codex_home_runtime.materialize(
            tmp_path / "restricted",
            desktop_home,
            mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
        )


def test_restricted_provider_rejects_nonempty_uninitialized_target(tmp_path):
    desktop_home = tmp_path / "desktop"
    restricted_home = tmp_path / "ordinary-headless"
    _write_restricted_desktop_config(desktop_home)
    restricted_home.mkdir()
    (restricted_home / "auth.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="must be empty or already initialized"):
        codex_home_runtime.materialize(
            restricted_home,
            desktop_home,
            mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
        )


def test_restricted_provider_rejects_forbidden_entries_on_reuse(tmp_path):
    desktop_home = tmp_path / "desktop"
    restricted_home = tmp_path / "restricted"
    _write_restricted_desktop_config(desktop_home)
    codex_home_runtime.materialize(
        restricted_home,
        desktop_home,
        mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
    )
    (restricted_home / "hooks.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="forbidden capability"):
        codex_home_runtime.materialize(
            restricted_home,
            desktop_home,
            mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
        )


def test_restricted_provider_preserves_reviewed_runtime_state_on_reuse(tmp_path):
    desktop_home = tmp_path / "desktop"
    restricted_home = tmp_path / "restricted"
    _write_restricted_desktop_config(desktop_home)
    codex_home_runtime.materialize(
        restricted_home,
        desktop_home,
        mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
    )
    for name in ("shell_snapshots", "skills", "thread-writer-locks", "tmp"):
        (restricted_home / name).mkdir()
    (restricted_home / "state_5.sqlite").write_text("runtime", encoding="utf-8")
    (restricted_home / "state_5.sqlite-wal").write_text("runtime", encoding="utf-8")
    (restricted_home / "thread_history_1.sqlite").write_text(
        "runtime", encoding="utf-8"
    )
    (restricted_home / "installation_id").write_text("runtime", encoding="utf-8")

    synced = codex_home_runtime.materialize(
        restricted_home,
        desktop_home,
        mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
    )

    assert synced == ["config.toml"]
    assert (restricted_home / "state_5.sqlite").read_text(encoding="utf-8") == "runtime"
    assert not (restricted_home / "auth.json").exists()


def test_restricted_provider_normalizes_legacy_runtime_directories(tmp_path):
    desktop_home = tmp_path / "desktop"
    restricted_home = tmp_path / "restricted"
    _write_restricted_desktop_config(desktop_home)
    codex_home_runtime.materialize(
        restricted_home,
        desktop_home,
        mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
    )
    for name in codex_home_runtime.RESTRICTED_RUNTIME_DIRECTORIES:
        directory = restricted_home / name
        directory.mkdir()
        directory.chmod(0o755)

    codex_home_runtime.materialize(
        restricted_home,
        desktop_home,
        mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
    )

    assert all(
        _mode(restricted_home / name) == 0o700
        for name in codex_home_runtime.RESTRICTED_RUNTIME_DIRECTORIES
    )


def test_restricted_provider_rejects_broader_runtime_directory_mode(tmp_path):
    desktop_home = tmp_path / "desktop"
    restricted_home = tmp_path / "restricted"
    _write_restricted_desktop_config(desktop_home)
    codex_home_runtime.materialize(
        restricted_home,
        desktop_home,
        mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
    )
    sessions = restricted_home / "sessions"
    sessions.mkdir()
    sessions.chmod(0o777)

    with pytest.raises(RuntimeError, match="runtime directory mode is unsafe"):
        codex_home_runtime.materialize(
            restricted_home,
            desktop_home,
            mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
        )


def test_restricted_provider_rejects_unknown_runtime_entry(tmp_path):
    desktop_home = tmp_path / "desktop"
    restricted_home = tmp_path / "restricted"
    _write_restricted_desktop_config(desktop_home)
    codex_home_runtime.materialize(
        restricted_home,
        desktop_home,
        mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
    )
    (restricted_home / "unknown-runtime.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unexpected entries"):
        codex_home_runtime.materialize(
            restricted_home,
            desktop_home,
            mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
        )


def test_full_materialize_rejects_restricted_target(tmp_path):
    desktop_home = tmp_path / "desktop"
    restricted_home = tmp_path / "restricted"
    _write_restricted_desktop_config(desktop_home)
    codex_home_runtime.materialize(
        restricted_home,
        desktop_home,
        mode=codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
    )

    with pytest.raises(RuntimeError, match="refuses a restricted target home"):
        codex_home_runtime.materialize(restricted_home, desktop_home)

    assert not (restricted_home / "auth.json").exists()


def test_restricted_provider_cli_mode_does_not_print_secrets(tmp_path, capsys):
    desktop_home = tmp_path / "desktop"
    restricted_home = tmp_path / "restricted"
    _write_restricted_desktop_config(desktop_home)

    rc = codex_home_runtime.main(
        [
            "--home",
            str(restricted_home),
            "--desktop-home",
            str(desktop_home),
            "--mode",
            codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "codex_home_runtime: synced config.toml\n"
    assert "secret" not in captured.out
    assert captured.err == ""


def test_restricted_provider_cli_error_does_not_print_secret_values(tmp_path, capsys):
    desktop_home = tmp_path / "desktop"
    restricted_home = tmp_path / "restricted"
    _write_restricted_desktop_config(
        desktop_home,
        """\
name = "OpenAI-compatible"
base_url = "https://provider.invalid/v1"
experimental_bearer_token = "provider-secret"
wire_api = "responses"
requires_openai_auth = true
http_headers = { Authorization = "extra-secret" }
""",
    )

    rc = codex_home_runtime.main(
        [
            "--home",
            str(restricted_home),
            "--desktop-home",
            str(desktop_home),
            "--mode",
            codex_home_runtime.MATERIALIZE_MODE_RESTRICTED_PROVIDER,
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "approved schema" in captured.err
    assert "provider-secret" not in captured.err
    assert "extra-secret" not in captured.err
    assert "provider.invalid" not in captured.err


@pytest.mark.parametrize("config", [
    'cli_auth_credentials_store = "keyring"\n',
    'model_provider = "custom"\n[model_providers.custom]\n'
    'name = "Provider"\nbase_url = "https://provider.invalid/v1"\n'
    'wire_api = "responses"\nrequires_openai_auth = false\n'
    'experimental_bearer_token = "test-provider-secret"\n',
])
def test_materialize_without_auth_file_defers_authentication_to_codex(tmp_path, config):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    (desktop / "config.toml").write_text(config)
    headless = tmp_path / "headless"

    synced = codex_home_runtime.materialize(headless, desktop)

    assert synced == ["config.toml"]
    result = tomllib.loads((headless / "config.toml").read_text())
    assert {key: result[key] for key in tomllib.loads(config)} == tomllib.loads(config)
    assert not (headless / "auth.json").exists()


def test_materialize_removes_old_auth_when_desktop_no_longer_has_it(tmp_path):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    (desktop / "config.toml").write_text('model = "gpt-6-astra"\n')
    (desktop / "auth.json").write_text('{"account":"old-account"}')
    headless = tmp_path / "headless"
    codex_home_runtime.materialize(headless, desktop)
    (desktop / "auth.json").unlink()

    codex_home_runtime.materialize(headless, desktop)

    assert not (headless / "auth.json").exists()
    assert not (desktop / "auth.json").exists()
    assert (headless / "config.toml").exists()


def test_materialize_copies_relative_model_catalog_referenced_by_config(tmp_path):
    desktop_home = tmp_path / "desktop"
    headless_home = tmp_path / "headless"
    desktop_home.mkdir()
    (desktop_home / "config.toml").write_text(
        'model = "gpt-6.1-sol"\nmodel_catalog_json = "cc-switch-model-catalog.json"\n'
    )
    (desktop_home / "cc-switch-model-catalog.json").write_text('{"models":[]}')

    synced = codex_home_runtime.materialize(headless_home, desktop_home)

    assert "cc-switch-model-catalog.json" in synced
    assert (headless_home / "cc-switch-model-catalog.json").read_text() == '{"models":[]}'
    assert _mode(headless_home / "cc-switch-model-catalog.json") == 0o600

    (desktop_home / "cc-switch-model-catalog.json").unlink()
    codex_home_runtime.materialize(headless_home, desktop_home)
    assert not (headless_home / "cc-switch-model-catalog.json").exists()


@pytest.mark.parametrize("value", ["/abs/catalog.json", "../outside.json", "~/catalog.json"])
def test_materialize_ignores_catalog_paths_outside_codex_home(tmp_path, value):
    desktop_home = tmp_path / "desktop"
    headless_home = tmp_path / "headless"
    desktop_home.mkdir()
    (desktop_home / "config.toml").write_text(f'model_catalog_json = "{value}"\n')

    synced = codex_home_runtime.materialize(headless_home, desktop_home)

    assert synced == ["config.toml"]
