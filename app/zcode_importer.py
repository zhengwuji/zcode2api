"""ZCode & Z-Accounts 本地凭证解密、捕获与自动导入模块。

参考并兼容 Z-Accounts (https://github.com/Kang-code-sudo/Z-Accounts) 核心原理：
1. 本地读取 ~/.zcode/v2/credentials.json 并自动解密 enc:v1 凭据
2. 扫描 ~/.zcode-switch/accounts/*.json 导入已持久化归档的账号
3. 自动同步生成 ~/.zcode/v2/config.json 与 ~/.zcode-proxy/credentials.json
   彻底修复旧版 zcode-proxy 无法读取 config.json 的报错问题。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .models import Account


def get_user_home() -> Path:
    """获取用户主目录，优先使用 USERPROFILE 或 HOME。"""
    userprofile = os.environ.get("USERPROFILE")
    if userprofile and os.path.exists(userprofile):
        return Path(userprofile)
    return Path(os.path.expanduser("~"))


def get_current_username() -> str:
    """获取系统当前登录用户名。"""
    return (
        os.environ.get("USERNAME")
        or os.environ.get("USER")
        or os.environ.get("LOGNAME")
        or "unknown"
    )


def derive_zcode_fallback_key(home: Path | None = None, username: str | None = None) -> bytes:
    """按 ZCode 与 Z-Accounts 官方标准推导解密 Key。

    Secret 格式: zcode-credential-fallback:{platform}:{home}:{username}
    Key: Sha256(Secret)
    """
    home_path = home or get_user_home()
    user = username or get_current_username()
    platform = "win32" if sys.platform == "win32" else ("darwin" if sys.platform == "darwin" else "linux")
    secret = f"zcode-credential-fallback:{platform}:{home_path}:{user}"
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _b64url_decode(s: str) -> bytes:
    """带自动补齐的 Base64URL 解码。"""
    s = s.strip()
    rem = len(s) % 4
    if rem:
        s += "=" * (4 - rem)
    return base64.urlsafe_b64decode(s)


def decrypt_zcode_string(val: str, key: bytes | None = None) -> str:
    """解密 enc:v1:<nonce>.<tag>.<ciphertext> 密文字符串。"""
    if not isinstance(val, str) or not val.startswith("enc:v1:"):
        return val
    parts = val[7:].split(".")
    if len(parts) != 3:
        return val
    try:
        nonce = _b64url_decode(parts[0])
        tag = _b64url_decode(parts[1])
        ct = _b64url_decode(parts[2])
        aesgcm = AESGCM(key or derive_zcode_fallback_key())
        return aesgcm.decrypt(nonce, ct + tag, None).decode("utf-8")
    except Exception:
        return val


def parse_jwt_payload(jwt_token: str) -> dict[str, Any]:
    """无需外部库安全解析 JWT Payload。"""
    if not jwt_token or jwt_token.count(".") < 2:
        return {}
    parts = jwt_token.split(".")
    try:
        data = _b64url_decode(parts[1])
        return json.loads(data.decode("utf-8"))
    except Exception:
        return {}


def parse_all_zcode_credentials(creds_raw: dict[str, Any], name_hint: str = "") -> list[dict[str, Any]]:
    """解密并提取凭据对象中的全部账号（支持多账号同时存在于同一 credentials 中）。"""
    if not isinstance(creds_raw, dict):
        return []

    key = derive_zcode_fallback_key()
    decrypted: dict[str, Any] = {}
    for k, v in creds_raw.items():
        if isinstance(v, str):
            decrypted[k] = decrypt_zcode_string(v, key)
        else:
            decrypted[k] = v

    accounts: list[dict[str, Any]] = []
    seen_secrets: set[str] = set()

    # 1. 提取 Z.AI 渠道 JWT (zcodejwttoken / oauth:zai:access_token)
    zai_token = decrypted.get("zcodejwttoken") or decrypted.get("oauth:zai:access_token")
    if zai_token and isinstance(zai_token, str) and zai_token.strip():
        token = zai_token.strip()
        seen_secrets.add(token)
        ui_raw = decrypted.get("oauth:zai:user_info")
        ui = {}
        if ui_raw:
            try:
                ui = json.loads(ui_raw) if isinstance(ui_raw, str) else ui_raw
            except Exception:
                pass
        jwt_info = parse_jwt_payload(token)
        email = str(ui.get("email") or jwt_info.get("email") or "")
        uid = str(ui.get("user_id") or ui.get("id") or jwt_info.get("user_id") or "")
        dname = str(ui.get("name") or ui.get("displayName") or jwt_info.get("name") or "")
        acc_name = name_hint or email or dname or (f"zai-{uid[:8]}" if uid else "zai-jwt-account")
        accounts.append({
            "provider": "zai",
            "name": acc_name,
            "secret": token,
            "jwt_token": token,
            "api_key": None,
            "mode": "jwt",
            "user_id": uid,
            "email": email,
            "display_name": dname,
            "raw_decrypted": decrypted,
        })

    # 2. 提取 BigModel 渠道 JWT (oauth:bigmodel:access_token)
    bm_token = decrypted.get("oauth:bigmodel:access_token")
    if bm_token and isinstance(bm_token, str) and bm_token.strip():
        token = bm_token.strip()
        if token not in seen_secrets:
            seen_secrets.add(token)
            ui_raw = decrypted.get("oauth:bigmodel:user_info")
            ui = {}
            if ui_raw:
                try:
                    ui = json.loads(ui_raw) if isinstance(ui_raw, str) else ui_raw
                except Exception:
                    pass
            jwt_info = parse_jwt_payload(token)
            email = str(ui.get("email") or jwt_info.get("email") or "")
            uid = str(ui.get("user_id") or ui.get("id") or jwt_info.get("user_id") or "")
            dname = str(ui.get("name") or ui.get("displayName") or jwt_info.get("name") or "")
            acc_name = (name_hint if not zai_token else "") or email or dname or (f"bigmodel-{uid[:8]}" if uid else "bigmodel-jwt-account")
            accounts.append({
                "provider": "bigmodel",
                "name": acc_name,
                "secret": token,
                "jwt_token": token,
                "api_key": None,
                "mode": "jwt",
                "user_id": uid,
                "email": email,
                "display_name": dname,
                "raw_decrypted": decrypted,
            })

    # 3. 提取所有单独绑定的 API-Key
    for k, v in decrypted.items():
        if k.startswith("account-provider:") and k.endswith(":api-key") and isinstance(v, str) and v.strip():
            secret_key = v.strip()
            if secret_key in seen_secrets:
                continue
            seen_secrets.add(secret_key)
            prov = "bigmodel" if "bigmodel" in k else "zai"
            parts = k.split(":")
            tag = parts[-2] if len(parts) >= 2 else "apikey"
            cat = "team" if "team" in k else "plan"
            key_name = f"{prov}-{cat}-{tag[:8]}" if len(tag) > 6 else f"{prov}-{tag}"
            if name_hint and not zai_token and not bm_token:
                key_name = name_hint
            accounts.append({
                "provider": prov,
                "name": key_name,
                "secret": secret_key,
                "jwt_token": None,
                "api_key": secret_key,
                "mode": "apiKey",
                "user_id": tag,
                "email": "",
                "display_name": key_name,
                "raw_decrypted": decrypted,
            })

    return accounts


def parse_zcode_credentials_dict(creds_raw: dict[str, Any], name_hint: str = "") -> dict[str, Any] | None:
    """提取首选主账号（优先考虑有 JWT 或匹配当前 active_provider 的账号）。"""
    all_accs = parse_all_zcode_credentials(creds_raw, name_hint)
    if not all_accs:
        return None
    # 优先选 mode=="jwt" 的，或者排第一的
    jwt_accs = [a for a in all_accs if a.get("mode") == "jwt"]
    return jwt_accs[0] if jwt_accs else all_accs[0]


def capture_live_zcode_all() -> list[dict[str, Any]]:
    """从 ~/.zcode/v2/credentials.json 捕获所有已登录的 ZCode 账号。"""
    home = get_user_home()
    creds_path = home / ".zcode" / "v2" / "credentials.json"
    if not creds_path.exists():
        return []

    try:
        raw_text = creds_path.read_text("utf-8")
        creds_json = json.loads(raw_text)
        return parse_all_zcode_credentials(creds_json)
    except Exception:
        return []


def capture_live_zcode() -> dict[str, Any] | None:
    """从 ~/.zcode/v2/credentials.json 捕获当前主要登录的 ZCode 账号。"""
    all_accs = capture_live_zcode_all()
    if not all_accs:
        return None
    jwt_accs = [a for a in all_accs if a.get("mode") == "jwt"]
    return jwt_accs[0] if jwt_accs else all_accs[0]


def scan_zaccounts_snapshots() -> list[dict[str, Any]]:
    """扫描 ~/.zcode-switch/accounts/*.json (Z-Accounts 历史账号快照)。"""
    home = get_user_home()
    acc_dir = home / ".zcode-switch" / "accounts"
    if not acc_dir.exists():
        return []

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in sorted(acc_dir.glob("*.json")):
        try:
            d = json.loads(p.read_text("utf-8"))
            name = d.get("name") or p.stem
            creds = d.get("credentials")
            if isinstance(creds, dict):
                accs = parse_all_zcode_credentials(creds, name_hint=name)
                for a in accs:
                    sec = a.get("secret")
                    if sec and sec not in seen:
                        seen.add(sec)
                        results.append(a)
        except Exception:
            continue
    return results


def capture_all_local_accounts() -> list[dict[str, Any]]:
    """聚合获取本地所有 ZCode 实时账号与 Z-Accounts 快照账号，去重返回。"""
    all_accs: list[dict[str, Any]] = []
    seen: set[str] = set()

    for acc in capture_live_zcode_all():
        sec = acc.get("secret")
        if sec and sec not in seen:
            seen.add(sec)
            all_accs.append(acc)

    for acc in scan_zaccounts_snapshots():
        sec = acc.get("secret")
        if sec and sec not in seen:
            seen.add(sec)
            all_accs.append(acc)

    return all_accs


def sync_zcode_config_and_proxy(info: dict[str, Any]) -> None:
    """自动生成 ~/.zcode/v2/config.json 并更新 ~/.zcode-proxy/credentials.json。

    彻底解决旧版 zcode-proxy.exe 报错问题。
    """
    home = get_user_home()
    provider = info.get("provider") or "bigmodel"
    secret = info.get("secret") or ""
    api_key = info.get("api_key") or secret
    jwt = info.get("jwt_token") or (secret if secret.count(".") == 2 else "")
    user_id = info.get("user_id") or ""

    # 1. 写入 ~/.zcode/v2/config.json (zcode-proxy.exe 依赖的格式)
    config_dir = home / ".zcode" / "v2"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.json"

    cfg_data: dict[str, Any] = {"provider": {}}
    if config_file.exists():
        try:
            cfg_data = json.loads(config_file.read_text("utf-8"))
            if not isinstance(cfg_data, dict):
                cfg_data = {"provider": {}}
            elif "provider" not in cfg_data or not isinstance(cfg_data["provider"], dict):
                cfg_data["provider"] = {}
        except Exception:
            cfg_data = {"provider": {}}

    # 填充 provider entry (支持同时填充 bigmodel 与 zai)
    raw = info.get("raw_decrypted") or {}
    for k, v in raw.items():
        if k.startswith("account-provider:") and k.endswith(":api-key") and isinstance(v, str):
            if "bigmodel" in k:
                cfg_data["provider"]["builtin:bigmodel-coding-plan"] = {"options": {"apiKey": v}}
            elif "zai" in k:
                cfg_data["provider"]["builtin:zai-coding-plan"] = {"options": {"apiKey": v}}

    # 当前主 provider 兜底确保
    coding_key = f"builtin:{provider}-coding-plan"
    start_key = f"builtin:{provider}-start-plan"
    if coding_key not in cfg_data["provider"]:
        cfg_data["provider"][coding_key] = {"options": {"apiKey": api_key}}
    if jwt:
        cfg_data["provider"][start_key] = {"options": {"apiKey": jwt}}

    try:
        config_file.write_text(json.dumps(cfg_data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    # 2. 写入 ~/.zcode-proxy/credentials.json (zcode-proxy 运行时凭证)
    proxy_dir = home / ".zcode-proxy"
    proxy_dir.mkdir(parents=True, exist_ok=True)
    proxy_file = proxy_dir / "credentials.json"

    try:
        proxy_seed = f"{home}-win32-x64"
        proxy_key = hashlib.sha256(proxy_seed.encode("utf-8")).digest()
        proxy_aes = AESGCM(proxy_key)

        cred_payload = {
            "apiKey": api_key,
            "provider": provider,
            "userId": user_id,
            "jwt": jwt or api_key,
            "expiresAt": int(time.time()) + 86400 * 30,
        }
        iv = os.urandom(12)
        ct = proxy_aes.encrypt(iv, json.dumps(cred_payload).encode("utf-8"), None)
        enc_str = base64.b64encode(iv + ct).decode("utf-8")
        proxy_file.write_text(json.dumps({"encrypted": enc_str}, indent=2), encoding="utf-8")
    except Exception:
        pass


def save_zaccounts_snapshot(account_data: dict[str, Any]) -> str:
    """保存或更新 Z-Accounts 格式快照至 ~/.zcode-switch/accounts/<id>.json。

    使 Z-Accounts 桌面工具、官方 ZCode 与本网关完全兼容互通。
    """
    import uuid
    from datetime import datetime

    home = get_user_home()
    acc_dir = home / ".zcode-switch" / "accounts"
    acc_dir.mkdir(parents=True, exist_ok=True)

    secret = account_data.get("secret") or ""
    snap_id = str(uuid.uuid4())

    # 查重：若已有该密钥的快照文件则复用 ID 进行覆盖更新
    for p in acc_dir.glob("*.json"):
        try:
            d = json.loads(p.read_text("utf-8"))
            if secret and (secret in json.dumps(d.get("credentials") or {})):
                snap_id = p.stem
                break
        except Exception:
            pass

    snap_path = acc_dir / f"{snap_id}.json"
    ts = datetime.now().isoformat()
    raw_creds = account_data.get("raw_decrypted") or {}
    prov = account_data.get("provider", "zai")
    if not raw_creds and secret:
        raw_creds = {
            "oauth:active_provider": prov,
            f"oauth:{prov}:access_token": secret,
            "zcodejwttoken": secret if secret.count(".") == 2 else "",
        }

    snapshot = {
        "id": snap_id,
        "name": account_data.get("name", f"Account-{snap_id[:8]}"),
        "createdAt": ts,
        "updatedAt": ts,
        "hash": hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16] if secret else "",
        "credentials": raw_creds,
        "config": None,
    }
    try:
        snap_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return snap_id


def capture_and_import_live(store_instance) -> tuple[bool, str, list[Account]]:
    """一键从当前已运行/已登录的 ZCode 捕获所有账号并存入 store，并同步至 Z-Accounts 快照。"""
    all_accs = capture_all_local_accounts()
    if not all_accs:
        return False, "未检测到已登录的 ZCode 凭据。请先打开 ZCode 登录账号后再试！", []

    added: list[Account] = []
    primary = all_accs[0]
    for info in all_accs:
        try:
            acc = store_instance.add_account(info["provider"], info["name"], info["secret"])
            added.append(acc)
            save_zaccounts_snapshot(info)
        except Exception:
            pass

    # 同步主要账号到 proxy 配置
    sync_zcode_config_and_proxy(primary)

    count = len(added)
    names = ", ".join(a.name for a in added[:3])
    if count > 3:
        names += f" 等共 {count} 个"
    return True, f"已成功保存 ZCode 本地账号（{names}）", added


def import_all_from_local(store_instance) -> tuple[int, list[str]]:
    """同时导入 ZCode 实时账号与 Z-Accounts 本地全部存档。"""
    all_accs = capture_all_local_accounts()
    imported_names: list[str] = []

    for s in all_accs:
        try:
            acc = store_instance.add_account(s["provider"], s["name"], s["secret"])
            imported_names.append(f"{acc.name} ({acc.provider})")
            save_zaccounts_snapshot(s)
        except Exception:
            pass

    if all_accs:
        sync_zcode_config_and_proxy(all_accs[0])

    return len(imported_names), imported_names

