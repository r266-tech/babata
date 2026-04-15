"""Per-account persistence for the WeChat channel.

Layout (override with BABATA_WEIXIN_DIR env var):
    ~/.babata/weixin/
    ├── accounts.json                                 # [accountId, ...]
    └── accounts/
        ├── {accountId}.json                          # {token, baseUrl, userId, savedAt}
        ├── {accountId}.sync.json                     # {get_updates_buf}
        ├── {accountId}.context-tokens.json           # {userId: contextToken, ...}
        └── {accountId}.allow.json                    # {version, allowFrom: [userId, ...]}

Mirrors the openclaw-weixin plugin layout 1:1 but rooted at ~/.babata/ so the
two can coexist. All writes are atomic (tmp + rename).
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ROOT = Path(os.environ.get("BABATA_WEIXIN_DIR") or Path.home() / ".babata" / "weixin")


def _root() -> Path:
    _ROOT.mkdir(parents=True, exist_ok=True)
    (_ROOT / "accounts").mkdir(parents=True, exist_ok=True)
    return _ROOT


def _account_path(account_id: str, suffix: str) -> Path:
    return _root() / "accounts" / f"{account_id}{suffix}"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


# ── account index ────────────────────────────────────────────────────


def list_account_ids() -> list[str]:
    ids = _read_json(_root() / "accounts.json", [])
    return list(ids) if isinstance(ids, list) else []


def register_account(account_id: str) -> None:
    ids = list_account_ids()
    if account_id not in ids:
        ids.append(account_id)
        _write_json(_root() / "accounts.json", ids)


def unregister_account(account_id: str) -> None:
    ids = [i for i in list_account_ids() if i != account_id]
    _write_json(_root() / "accounts.json", ids)
    for suffix in (".json", ".sync.json", ".context-tokens.json", ".allow.json"):
        _account_path(account_id, suffix).unlink(missing_ok=True)


# ── token / base_url ─────────────────────────────────────────────────


def save_account(
    account_id: str,
    *,
    token: str,
    base_url: str,
    user_id: str | None = None,
) -> None:
    _write_json(
        _account_path(account_id, ".json"),
        {
            "token": token,
            "baseUrl": base_url,
            "userId": user_id,
            "savedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    )


def load_account(account_id: str) -> dict[str, Any] | None:
    data = _read_json(_account_path(account_id, ".json"), None)
    return data if isinstance(data, dict) and data.get("token") else None


# ── sync buf (getUpdates cursor) ─────────────────────────────────────


def save_sync_buf(account_id: str, buf: str) -> None:
    _write_json(_account_path(account_id, ".sync.json"), {"get_updates_buf": buf})


def load_sync_buf(account_id: str) -> str:
    data = _read_json(_account_path(account_id, ".sync.json"), {})
    return data.get("get_updates_buf", "") if isinstance(data, dict) else ""


# ── context tokens (per-peer, required for outbound reply) ───────────


def load_context_tokens(account_id: str) -> dict[str, str]:
    data = _read_json(_account_path(account_id, ".context-tokens.json"), {})
    return data if isinstance(data, dict) else {}


def set_context_token(account_id: str, user_id: str, ctx_token: str | None) -> None:
    if not user_id:
        return
    tokens = load_context_tokens(account_id)
    if ctx_token:
        tokens[user_id] = ctx_token
    else:
        tokens.pop(user_id, None)
    _write_json(_account_path(account_id, ".context-tokens.json"), tokens)


def get_context_token(account_id: str, user_id: str) -> str | None:
    return load_context_tokens(account_id).get(user_id)


def clear_context_tokens(account_id: str) -> None:
    _account_path(account_id, ".context-tokens.json").unlink(missing_ok=True)


# ── allowFrom (per-account authz) ────────────────────────────────────


def load_allow_from(account_id: str) -> list[str]:
    data = _read_json(_account_path(account_id, ".allow.json"), {})
    return list(data.get("allowFrom", [])) if isinstance(data, dict) else []


def add_allow_from(account_id: str, user_id: str) -> None:
    if not user_id:
        return
    allow = load_allow_from(account_id)
    if user_id not in allow:
        allow.append(user_id)
        _write_json(
            _account_path(account_id, ".allow.json"),
            {"version": 1, "allowFrom": allow},
        )


def is_allowed(account_id: str, user_id: str) -> bool:
    """Return True if user is allowed.

    Empty allowFrom = allow all (dev default — the iLink bot ID returned at
    login does not always match inbound from_user_id formatting, so the safe
    default is open; V adds entries manually via add_allow_from to lock down).
    """
    allow = load_allow_from(account_id)
    return (not allow) or (user_id in allow)


# ── multi-account cleanup ────────────────────────────────────────────


def clear_stale_for_user(keeping_account_id: str, user_id: str) -> list[str]:
    """When the same WeChat user re-scans a new bot, wipe prior bot accounts
    tied to the same user_id. Returns removed account_ids."""
    if not user_id:
        return []
    removed: list[str] = []
    for aid in list_account_ids():
        if aid == keeping_account_id:
            continue
        meta = load_account(aid)
        if meta and meta.get("userId") == user_id:
            unregister_account(aid)
            removed.append(aid)
            log.info("cleared stale account %s (userId=%s)", aid, user_id)
    return removed
