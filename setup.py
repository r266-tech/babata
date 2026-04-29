"""babata 引导程序 — 全自动配置, 不让用户碰 .env.

跑这个: install.sh 装完 deps 后自动调; 也可以独立 `.venv/bin/python setup.py` 重跑.

三档 Claude 接入:
  [1] Anthropic 官方 API key — 验证后写 ANTHROPIC_API_KEY
  [2] 共享已登录的 Claude Code (OAuth keychain) — 写 BABATA_SHARED_CC=1
  [3] 第三方 Anthropic 兼容 endpoint — 填 URL+token, GET /v1/models 列模型选,
      探测不到模型让用户手填; 写 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN + ANTHROPIC_MODEL

TG channel: 引导拿 BotFather token + binding mode 自动捕获 user_id (不用 @userinfobot)
WX channel: 调 weixin_bot._interactive_login 出终端 ASCII QR 扫码

写 .env 用原子 rewrite — 替换已有 key, 追加新 key, 不动注释和无关字段.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

import httpx

REPO = Path(__file__).parent.resolve()
ENV_FILE = REPO / ".env"
ENV_EXAMPLE = REPO / ".env.example"
VENV_PY = REPO / ".venv" / "bin" / "python"
ANTHROPIC_OFFICIAL = "https://api.anthropic.com"


# ── UI helpers ─────────────────────────────────────────────────────────


def banner(text: str) -> None:
    print()
    bar = "─" * max(0, 60 - len(text) - 4)
    print(f"── {text} {bar}")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n中断.")
        sys.exit(130)
    return ans or default


def confirm(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    ans = ask(f"{prompt} {suffix}").lower()
    if not ans:
        return default
    return ans.startswith("y")


# ── .env 原子读写 ─────────────────────────────────────────────────────


def read_env() -> dict[str, str]:
    """Parse .env into key→value dict (skip comments / blank lines)."""
    out: dict[str, str] = {}
    if not ENV_FILE.exists():
        return out
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        # strip inline comment after value (only when not quoted)
        if v and v[0] not in ('"', "'") and "#" in v:
            v = v.split("#", 1)[0].strip()
        out[k] = v
    return out


def write_env(updates: dict[str, str]) -> None:
    """Atomic update: replace existing keys in place, append new ones at end.

    Empty-string value clears the key (writes ``KEY=`` so leftover values from
    an earlier mode don't leak in). Preserves comments / formatting / unrelated
    keys.
    """
    if not updates:
        return

    if not ENV_FILE.exists():
        if ENV_EXAMPLE.exists():
            shutil.copy(ENV_EXAMPLE, ENV_FILE)
        else:
            ENV_FILE.write_text("")

    text = ENV_FILE.read_text()
    lines = text.splitlines()
    seen: set[str] = set()

    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        new_lines.append(line)

    appends = [k for k in updates if k not in seen]
    if appends:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append("# ── added by setup.py ──")
        for k in appends:
            new_lines.append(f"{k}={updates[k]}")

    final = "\n".join(new_lines).rstrip() + "\n"

    fd, tmp_path = tempfile.mkstemp(prefix=".env.", dir=str(REPO))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(final)
        os.chmod(tmp_path, 0o600)  # token 在里面, 别 world-readable
        os.replace(tmp_path, ENV_FILE)
    except Exception:
        with suppress(Exception):
            os.unlink(tmp_path)
        raise


# ── auth probes ────────────────────────────────────────────────────────


def probe_anthropic_official(api_key: str) -> tuple[bool, str]:
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    try:
        r = httpx.get(f"{ANTHROPIC_OFFICIAL}/v1/models", headers=headers, timeout=15)
    except Exception as e:
        return False, f"网络错误: {e}"
    if r.status_code == 200:
        return True, "OK"
    body = r.text[:200].replace("\n", " ")
    return False, f"HTTP {r.status_code}: {body}"


_PROBE_STATUS_OK = "ok"            # 200, 拿到模型列表 (可能为空)
_PROBE_STATUS_AUTH = "auth_failed"  # 401 / 403
_PROBE_STATUS_MISSING = "missing"   # 404, endpoint 不实现 /v1/models (合法, 让用户手填)
_PROBE_STATUS_TRANSIENT = "transient"  # 408 / 429 / 5xx, 可能临时
_PROBE_STATUS_NETWORK = "network"   # 连不上 / 超时
_PROBE_STATUS_OTHER = "other"       # 其他 4xx, 不太合法


def _probe_with_auth(base: str, headers: dict[str, str]) -> tuple[str, list[str], str]:
    """单次 GET /v1/models. 返回 (status, models, message)."""
    try:
        r = httpx.get(f"{base}/v1/models", headers=headers, timeout=20)
    except Exception as e:
        return _PROBE_STATUS_NETWORK, [], f"网络错误: {e}"

    body = r.text[:200].replace("\n", " ")
    code = r.status_code

    if code == 200:
        try:
            data = r.json()
        except Exception:
            return _PROBE_STATUS_OK, [], "/v1/models 返回非 JSON, 手填 model 名"
        if not isinstance(data, dict):
            return _PROBE_STATUS_OK, [], "/v1/models 返回非标准结构, 手填 model 名"
        models: list[str] = []
        for m in data.get("data", []) or []:
            if isinstance(m, dict) and m.get("id"):
                models.append(str(m["id"]))
        return _PROBE_STATUS_OK, models, "OK"

    if code in (401, 403):
        return _PROBE_STATUS_AUTH, [], f"鉴权失败 HTTP {code}: {body}"
    if code == 404:
        return _PROBE_STATUS_MISSING, [], f"endpoint 不实现 /v1/models (HTTP 404)"
    if code == 408 or code == 429 or 500 <= code < 600:
        return _PROBE_STATUS_TRANSIENT, [], f"服务端临时错误 HTTP {code}: {body}"
    return _PROBE_STATUS_OTHER, [], f"HTTP {code}: {body}"


def probe_third_party(base_url: str, token: str) -> tuple[bool, list[str], str]:
    """Probe Anthropic-compatible endpoint. Returns (ok_to_save, models, message).

    部分网关只接受 x-api-key (Anthropic native), 部分只接受 Bearer (OpenAI-style 代理),
    同时发两个 header 反而被某些代理拒. 所以串行试: 先 Anthropic 风格, 401/403 再试 Bearer.

    ok_to_save=False 表示鉴权失败/网络挂/服务端 5xx — 不要保存到 .env.
    ok_to_save=True + models=[] 表示连通但 endpoint 不暴露 /v1/models, 让用户手填.
    """
    base = base_url.rstrip("/")
    common = {"anthropic-version": "2023-06-01"}

    # try Anthropic-native first
    status, models, msg = _probe_with_auth(base, {"x-api-key": token, **common})

    # 鉴权失败再试 Bearer (OpenAI-style 代理常见)
    if status == _PROBE_STATUS_AUTH:
        status2, models2, msg2 = _probe_with_auth(
            base, {"Authorization": f"Bearer {token}", **common}
        )
        if status2 != _PROBE_STATUS_AUTH:
            status, models, msg = status2, models2, msg2

    if status == _PROBE_STATUS_OK:
        return True, models, msg
    if status == _PROBE_STATUS_MISSING:
        # 404 endpoint 没实现 /v1/models — 合法, 但加警告
        return True, [], f"{msg}; 无法验证 token, 手填 model 名"
    # 其余 (auth / transient / network / other) 都不放行
    return False, [], msg


# ── step 1: model auth ─────────────────────────────────────────────────

# 完整 key 列表 — 选了某档就把其他档的字段清空, 防止上一次的残留串模式
_ALL_AUTH_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "BABATA_SHARED_CC",
)


def _empty_auth() -> dict[str, str]:
    return {k: "" for k in _ALL_AUTH_KEYS}


def step_model_auth() -> dict[str, str]:
    banner("Claude 模型 / Auth")
    print("babata 用 Claude Code 当内核. 三种接入方式:")
    print()
    print("  [1] Anthropic 官方 API key  (推荐, 最简单)")
    print("  [2] 共享你已经登录的 Claude Code  (跟你日常 CC 共用 OAuth + skill + settings)")
    print("  [3] 第三方 Anthropic 兼容 endpoint  (mimo / openrouter / 自建代理 / 等等)")
    print()

    while True:
        # 不给 default — 这是明确选项不是 yes/no, 默认 [1] 会让按 enter
        # 的用户被静默接走到官方 API key 路径.
        choice = ask("选 [1/2/3]")
        if choice in ("1", "2", "3"):
            break
        print("请输入 1, 2, 或 3")

    if choice == "1":
        return _step_official()
    if choice == "2":
        return _step_shared()
    return _step_third_party()


def _step_official() -> dict[str, str]:
    while True:
        key = ask("粘贴 Anthropic API key (sk-ant-...)")
        if not key:
            print("空值, 重输")
            continue
        print("验证中...")
        ok, msg = probe_anthropic_official(key)
        if ok:
            print("✓ key 验证通过")
            out = _empty_auth()
            out["ANTHROPIC_API_KEY"] = key
            return out
        print(f"✗ {msg}")
        if not confirm("再试一次?"):
            sys.exit(1)


def _step_shared() -> dict[str, str]:
    cc_dir = Path.home() / ".claude"
    if not cc_dir.exists():
        print("⚠️  ~/.claude 不存在 — 你还没登录过 Claude Code.")
        print("   先在终端跑 `claude` 走完 OAuth 登录, 然后再回来选 [2].")
        if not confirm("仍然写 BABATA_SHARED_CC=1 (babata 启动时会失败, 直到你登录)?", default=False):
            sys.exit(1)
    out = _empty_auth()
    out["BABATA_SHARED_CC"] = "1"
    print("✓ 共享模式已配置")
    return out


def _step_third_party() -> dict[str, str]:
    while True:
        base_url = ask("Endpoint URL (例: https://token-plan-cn.xiaomimimo.com/anthropic)")
        if base_url.startswith(("http://", "https://")):
            break
        print("URL 必须以 http:// 或 https:// 开头")

    while True:
        token = ask("Auth token")
        if token:
            break
        print("空值, 重输")

    print("探测 endpoint...")
    auth_ok, models, msg = probe_third_party(base_url, token)
    if not auth_ok:
        print(f"✗ {msg}")
        if not confirm("仍然保存 (跳过验证, 后续启动可能失败)?", default=False):
            sys.exit(1)
    else:
        print(f"✓ {msg}")

    if models:
        print()
        print(f"endpoint 暴露了 {len(models)} 个模型:")
        for i, m in enumerate(models, 1):
            print(f"  [{i}] {m}")
        print()
        while True:
            sel = ask("选编号, 或直接输入模型名 (不在列表里也行)")
            if sel.isdigit() and 1 <= int(sel) <= len(models):
                model = models[int(sel) - 1]
                break
            if sel:
                model = sel
                break
            print("空值, 重输")
    else:
        print()
        while True:
            model = ask("Model 名 (例: mimo-v2.5-pro)")
            if model:
                break
            print("空值, 重输")

    print(f"✓ model = {model}")
    out = _empty_auth()
    out["ANTHROPIC_BASE_URL"] = base_url
    out["ANTHROPIC_AUTH_TOKEN"] = token
    out["ANTHROPIC_MODEL"] = model
    return out


# ── step 2: TG channel ─────────────────────────────────────────────────


def step_tg() -> dict[str, str] | None:
    """配置 TG channel. 返回 None = 跳过 (调用者负责清空 placeholder).

    user_id 抓取走 nonce + 二次确认双闸: 防止"别人/旧群里其他人先发消息" race
    把 ALLOWED_USER_ID 写成错的人, 那相当于把 bot 控制权交出去.
    """
    banner("Telegram channel")
    if not confirm("装 TG channel? (babata 的主要触发渠道, 强烈建议装)"):
        return None

    print()
    print("拿 bot token (BotFather):")
    print("  1. 打开 Telegram, 搜 @BotFather")
    print("  2. 发 /newbot → 起 display name → 起 username (必须 bot 结尾)")
    print("  3. BotFather 回一个 token (像 1234567890:AA-BB...)")
    print()

    token, username = _prompt_and_verify_tg_token()
    if not token:
        return None

    print()
    print(f"✓ 连上 @{username}")

    # nonce + 二次确认 loop: 拿到 user_id 后必须确认是本人
    while True:
        nonce = "BIND-" + secrets.token_hex(3).upper()  # 6 hex chars 大写
        print()
        print(f"现在去 Telegram 找 @{username}, 把以下文字原样发给它 (作为绑定凭证):")
        print()
        print(f"    {nonce}")
        print()
        print("等待中... (Ctrl+C 取消整个 setup)")

        capture = _capture_tg_user_id(token, nonce)
        if capture is None:
            # _capture 内部已 print 了原因, 用户可能要换 nonce 重试
            if not confirm("重新生成绑定码再试?", default=True):
                return None
            continue

        user_id, who = capture
        print()
        print("✓ 收到来自:")
        print(f"    user_id:    {user_id}")
        print(f"    username:   @{who.get('username') or '(无)'}")
        full_name = " ".join(s for s in [who.get("first_name", ""), who.get("last_name", "")] if s).strip()
        print(f"    name:       {full_name or '(无)'}")
        print()
        if confirm("是你本人吗? 确认后这个 user_id 拥有 bot 全部权限.", default=True):
            return {"TELEGRAM_BOT_TOKEN": token, "ALLOWED_USER_ID": str(user_id)}
        print("拒绝. 重新生成绑定码...")


def _prompt_and_verify_tg_token() -> tuple[str, str]:
    while True:
        token = ask("粘贴 token")
        if ":" not in token:
            print("token 格式不对 (BotFather 给的 token 含一个冒号), 重输")
            continue
        try:
            r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
        except Exception as e:
            print(f"✗ 网络错误: {e}")
            if not confirm("再试一次?"):
                return "", ""
            continue
        if r.status_code != 200:
            print(f"✗ HTTP {r.status_code}: {r.text[:200]}")
            if not confirm("再试一次?"):
                return "", ""
            continue
        try:
            data = r.json()
        except Exception:
            print(f"✗ 非 JSON 响应: {r.text[:200]}")
            if not confirm("再试一次?"):
                return "", ""
            continue
        if not data.get("ok"):
            print(f"✗ Telegram 拒绝: {data}")
            if not confirm("再试一次?"):
                return "", ""
            continue
        return token, data["result"].get("username", "?")


_TG_MAX_POLL_ERRORS = 5
_TG_POLL_DEADLINE_SEC = 600  # 10 分钟没拿到匹配消息 = 给用户机会换 nonce 重试


def _capture_tg_user_id(token: str, nonce: str) -> tuple[int, dict] | None:
    """Long-poll until inbound message text == nonce. Returns (user_id, from_dict) or None.

    Drops pre-existing pending updates. Webhook clear (in case token was reused).
    Bounded retries + bounded total wait — 不让用户卡在死循环.
    Returns None on persistent error or timeout (调用者可重试).
    """
    import time

    base = f"https://api.telegram.org/bot{token}"

    # 旧 webhook + pending updates 一起清掉. 失败也继续, getUpdates 自己会报错.
    try:
        httpx.post(f"{base}/deleteWebhook", params={"drop_pending_updates": "true"}, timeout=10)
    except Exception:
        pass

    next_offset = 0
    try:
        r = httpx.get(f"{base}/getUpdates", params={"offset": -1, "timeout": 0}, timeout=15)
        results = r.json().get("result") or []
        if results:
            next_offset = results[-1]["update_id"] + 1
    except Exception:
        pass

    err_streak = 0
    deadline = time.time() + _TG_POLL_DEADLINE_SEC

    while time.time() < deadline:
        try:
            r = httpx.get(
                f"{base}/getUpdates",
                params={
                    "offset": next_offset,
                    "timeout": 30,
                    # Bot API 要求 JSON 数组, 不是裸字符串. 不传也行 (默认全部),
                    # 显式传是省 quota — 但格式必须对.
                    "allowed_updates": json.dumps(["message"]),
                },
                timeout=40,
            )
        except KeyboardInterrupt:
            print("\n中断.")
            sys.exit(130)
        except Exception as e:
            err_streak += 1
            print(f"  (poll error #{err_streak}: {e})")
            if err_streak >= _TG_MAX_POLL_ERRORS:
                print(f"  连续失败 {err_streak} 次, 放弃")
                return None
            continue

        if r.status_code != 200:
            err_streak += 1
            body = r.text[:200].replace("\n", " ")
            print(f"  (HTTP {r.status_code} #{err_streak}: {body})")
            if err_streak >= _TG_MAX_POLL_ERRORS:
                return None
            continue
        try:
            data = r.json()
        except Exception:
            err_streak += 1
            if err_streak >= _TG_MAX_POLL_ERRORS:
                return None
            continue
        if not data.get("ok"):
            err_streak += 1
            print(f"  (TG err: {data})")
            if err_streak >= _TG_MAX_POLL_ERRORS:
                return None
            continue

        err_streak = 0  # 一次成功响应清零

        for upd in data.get("result", []):
            next_offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            text = (msg.get("text") or "").strip()
            from_user = msg.get("from") or {}
            if not from_user.get("id"):
                continue
            # 只接受 nonce 匹配 — 防 race / 别人先发消息抢 user_id
            if text == nonce:
                return int(from_user["id"]), from_user
            # 收到了消息但不是 nonce — 可能是用户没看清, 提示一下继续等
            print(f"  (收到非绑定码消息 '{text[:30]}', 等真正的 {nonce})")

    print(f"  超时 {_TG_POLL_DEADLINE_SEC}s 没收到匹配消息")
    return None


# ── step 3: WX channel ────────────────────────────────────────────────


def step_wx() -> bool:
    banner("WeChat channel (可选)")
    print("用腾讯 iLink bot 协议. 终端打 ASCII QR, 微信扫码即可.")
    print("扫码的微信号自动加入 allowFrom — 之后只有它能给 bot 发消息触发 CC.")
    print()
    if not confirm("装微信 channel?", default=False):
        return False

    if not VENV_PY.exists():
        print(f"✗ 找不到 {VENV_PY}, 先跑 install.sh")
        return False

    # WX 依赖 (pilk + qrcode) 默认不装 — TG-only 用户不该被 C 扩展拖累.
    # 选 WX 才现装. uv 应在 PATH (install.sh 装的; 独立跑时用户自己 source 过).
    print("装 WX 依赖 (pilk + qrcode)...")
    sync = subprocess.run(
        ["uv", "sync", "--quiet", "--extra", "wx"],
        cwd=str(REPO),
    )
    if sync.returncode != 0:
        print("✗ WX 依赖装失败.")
        print("  pilk 是 C 扩展, 需要 gcc + Python headers. 装好 build 工具再重跑:")
        print("    Linux:  sudo apt install build-essential python3-dev")
        print("    macOS:  xcode-select --install")
        return False

    code = (
        "import asyncio, sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from weixin_bot import _interactive_login\n"
        "asyncio.run(_interactive_login())\n"
    )

    try:
        proc = subprocess.run([str(VENV_PY), "-c", code], cwd=str(REPO))
    except KeyboardInterrupt:
        print("\n微信登录中断.")
        return False

    if proc.returncode == 0:
        print("✓ 微信 channel 配置完成 (token 存在 ~/.babata/weixin/)")
        return True
    print("✗ 微信登录失败 (退出码", proc.returncode, ")")
    return False


# ── main ───────────────────────────────────────────────────────────────


def main() -> int:
    banner("babata 引导程序")
    print("自动配置 .env, 你不用手动改文件.")
    print("(已配过的 key 会被覆盖. Ctrl+C 随时退出, 已写入的部分会保留.)")

    # 渐进 write_env: 每个步骤完就落盘, Ctrl+C 不丢已验证的 auth/TG 配置.
    # write_env 是 atomic, 多次调用安全.

    # 1. Claude auth (落盘一次)
    auth = step_model_auth()
    write_env(auth)
    print(f"✓ .env 已写入 Claude auth")

    # 2. CLAUDE_CLI_PATH (落盘一次)
    claude_bin = shutil.which("claude")
    if claude_bin:
        write_env({"CLAUDE_CLI_PATH": claude_bin})
        print(f"✓ CLAUDE_CLI_PATH = {claude_bin}")
    else:
        print("⚠️  没找到 claude 命令 — 跑 install.sh 应该会装. 没装的话 babata 启动会失败.")

    # 3. TG channel — 不装就显式 clear placeholder, 否则 .env.example 里的 'your_bot_token_here'
    # 会让 bot.py 读出脏值 (虽然还是会 KeyError, 但日志更迷惑).
    tg = step_tg()
    if tg:
        write_env(tg)
        print(f"✓ .env 已写入 TG channel")
        tg_configured = True
    else:
        write_env({"TELEGRAM_BOT_TOKEN": "", "ALLOWED_USER_ID": ""})
        tg_configured = False

    # 4. WX channel (state 在 ~/.babata/weixin/, 不写 .env)
    wx_configured = step_wx()

    banner("配置完成" if (tg_configured or wx_configured) else "配置未完成")
    print()
    if tg_configured:
        print("启动 bot:  babata")
        print("(没装到 PATH? 用: .venv/bin/python bot.py)")
    elif wx_configured:
        print("⚠️  只装了微信 channel, 没装 TG. 当前 babata 启动入口是 bot.py (TG),")
        print("   纯微信启动需要单独跑: .venv/bin/python weixin_bot.py")
    else:
        print("⚠️  TG 和微信都没装, babata 现在跑不起来. 重跑 setup:")
        print("   .venv/bin/python setup.py")
    print()
    print("重跑 setup:  .venv/bin/python setup.py")
    print()
    # 退出码: 至少一个 channel 装了 → 0; 都没装 → 1 (install.sh 据此判断)
    return 0 if (tg_configured or wx_configured) else 1


if __name__ == "__main__":
    sys.exit(main())
