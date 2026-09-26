"""账号与设置的持久化存储（SQLite）。

数据保存在项目本目录下的 data/accounts.db，采用 WAL 模式，
与 grok2api 的本地 (local) 账号后端保持一致。

运行期账号对象常驻内存（保证轮询游标与状态实时性），
每次变更同步落库；进程启动时从 SQLite 读取快照。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from contextlib import closing

from . import settings
from .models import PROVIDERS, Account, Status

_TBL = "accounts"
_META = "meta"


class Store:
    """线程安全的账号 / 设置存储，含轮询游标。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._accounts: dict[str, list[Account]] = {p: [] for p in PROVIDERS}
        self._settings: dict = {}
        self._rotation: dict[str, int] = {p: 0 for p in PROVIDERS}
        self._init_db()
        self._load()

    # ── SQLite 基础 ──────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS {_META} (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS {_TBL} (
                    id          TEXT PRIMARY KEY,
                    provider    TEXT NOT NULL,
                    name        TEXT,
                    mode        TEXT,
                    status      TEXT,
                    enabled     INTEGER NOT NULL DEFAULT 1,
                    created_at  REAL,
                    data        TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_acc_provider ON {_TBL} (provider);
                CREATE INDEX IF NOT EXISTS idx_acc_status   ON {_TBL} (status);
                """
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('admin_key', ?)",
                (settings.DEFAULT_ADMIN_KEY,),
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('gateway_key', '')"
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('quota_refresh_interval', ?)",
                (str(settings.QUOTA_REFRESH_INTERVAL),),
            )
            conn.commit()

    def _load(self) -> None:
        with closing(self._connect()) as conn:
            meta_rows = conn.execute(f"SELECT key, value FROM {_META}").fetchall()
            self._settings = {r["key"]: r["value"] for r in meta_rows}
            self._settings.setdefault("admin_key", settings.DEFAULT_ADMIN_KEY)
            current_gw = (self._settings.get("gateway_key") or "").strip()
            if not current_gw:
                current_gw = f"sk-zcode2api-{secrets.token_hex(16)}"
                self._settings["gateway_key"] = current_gw
                self._set_meta("gateway_key", current_gw)
            self._settings.setdefault("quota_refresh_interval", str(settings.QUOTA_REFRESH_INTERVAL))

            self._accounts = {p: [] for p in PROVIDERS}
            rows = conn.execute(
                f"SELECT data FROM {_TBL} ORDER BY created_at ASC"
            ).fetchall()
            for row in rows:
                try:
                    account = Account.from_dict(json.loads(row["data"]))
                except (json.JSONDecodeError, TypeError):
                    continue
                # 兼容修正历史存量中把 JWT 识别为 apiKey 的情况
                if account.secret and account.secret.count(".") == 2 and account.mode != "jwt":
                    account.mode = "jwt"
                    account.jwt_token = account.secret
                    account.api_key = None
                    self._persist_account(account)
                if account.provider in self._accounts:
                    self._accounts[account.provider].append(account)
            self._enrich_identities()
            self.deduplicate()

    def _enrich_identities(self) -> None:
        """自动从 JWT 荷载及本地 Z-Accounts 镜像中提取识别真实的邮箱、手机号、昵称与头像。"""
        try:
            import os
            import re
            import base64
            from pathlib import Path
            from .zcode_importer import decrypt_zcode_string, derive_zcode_fallback_key

            home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
            key = derive_zcode_fallback_key()
            id_map: dict[str, dict] = {}

            # 扫描 ~/.zcode-switch/accounts/*.json 提取真实账户信息
            switch_dir = home / ".zcode-switch" / "accounts"
            if switch_dir.exists():
                for f in switch_dir.glob("*.json"):
                    try:
                        d = json.loads(f.read_text(encoding="utf-8"))
                        txt = f.read_text(encoding="utf-8", errors="ignore")
                        phones = re.findall(r"1[3-9]\d{9}", txt)
                        phone = phones[0] if phones else ""
                        creds = d.get("credentials") or {}
                        for k, v in creds.items():
                            if "user_info" in k:
                                dec = decrypt_zcode_string(v, key)
                                if dec and dec.startswith("{"):
                                    ui = json.loads(dec)
                                    uid = ui.get("user_id") or ui.get("id")
                                    if uid:
                                        if phone and not ui.get("phone"):
                                            ui["phone"] = phone
                                        id_map[str(uid)] = ui
                        bm_at = creds.get("oauth:bigmodel:access_token")
                        if bm_at:
                            plain_at = decrypt_zcode_string(bm_at, key)
                            if plain_at:
                                try:
                                    import httpx
                                    r = httpx.get("https://bigmodel.cn/api/biz/customer/getCustomerInfo", headers={"Authorization": plain_at}, timeout=3)
                                    if r.status_code == 200:
                                        c_data = r.json().get("data") or {}
                                        c_num = str(c_data.get("customerNumber") or "")
                                        if c_num and c_num in id_map:
                                            nick = c_data.get("nickName") or ""
                                            c_name = c_data.get("customerName") or ""
                                            id_map[c_num]["displayName"] = f"{nick} ({c_name})".strip() if nick else c_name
                                            if c_data.get("avatar"):
                                                id_map[c_num]["avatarUrl"] = c_data["avatar"]
                                except Exception:
                                    pass
                    except Exception:
                        pass

            def _b64url(s: str) -> bytes:
                s += "=" * ((4 - len(s) % 4) % 4)
                return base64.urlsafe_b64decode(s)

            for p in PROVIDERS:
                for a in self._accounts[p]:
                    changed = False
                    uid = a.user_id or ""
                    # 1. 尝试从 JWT 提取 user_id / sub / email / phone
                    if a.jwt_token and a.jwt_token.count(".") == 2:
                        try:
                            payload = json.loads(_b64url(a.jwt_token.split(".")[1]).decode("utf-8", "ignore"))
                            if not uid:
                                uid = str(payload.get("user_id") or payload.get("sub") or "")
                            if not a.email and payload.get("email"):
                                a.email = str(payload["email"])
                                changed = True
                            if not a.phone and payload.get("phone"):
                                a.phone = str(payload["phone"])
                                changed = True
                        except Exception:
                            pass

                    # 2. 从映射表匹配
                    if not uid:
                        for k in id_map:
                            if k[:8] in a.id or k[:8] in a.name:
                                uid = k
                                break

                    if uid:
                        if not a.user_id:
                            a.user_id = uid
                            changed = True
                        info = id_map.get(uid, {})
                        if not a.email and info.get("email"):
                            a.email = str(info["email"])
                            changed = True
                        if not a.phone and info.get("phone"):
                            a.phone = str(info["phone"])
                            changed = True
                        if not a.avatar and (info.get("avatar") or info.get("avatarUrl")):
                            a.avatar = str(info.get("avatar") or info.get("avatarUrl"))
                            changed = True
                        if info.get("displayName") and a.display_name != info.get("displayName"):
                            a.display_name = str(info["displayName"])
                            changed = True
                        elif not a.display_name and (info.get("name") or info.get("username")):
                            a.display_name = str(info.get("name") or info.get("username"))
                            changed = True

                    # 3. 兜底解析 name
                    if not a.email and "@" in a.name:
                        a.email = a.name
                        changed = True

                    if changed:
                        self._persist_account(a)
        except Exception:
            pass

    def deduplicate(self) -> int:
        """根据 (provider, user_id/identity, mode) 自动识别并合并同一账号的重复凭据（保留最新有效的一份）。"""
        with self._lock:
            removed_count = 0
            for p in PROVIDERS:
                seen: dict[tuple[str, str], Account] = {}
                to_remove: list[str] = []
                for a in list(self._accounts[p]):
                    uid = a.user_id or a.email or a.phone or a.name or a.id
                    key = (uid, a.mode)
                    if key in seen:
                        prev = seen[key]
                        if (a.created_at or 0) >= (prev.created_at or 0):
                            to_remove.append(prev.id)
                            seen[key] = a
                        else:
                            to_remove.append(a.id)
                    else:
                        seen[key] = a

                for rid in to_remove:
                    self._delete_account(rid)
                    self._accounts[p] = [a for a in self._accounts[p] if a.id != rid]
                    removed_count += 1
            return removed_count

    def _persist_account(self, account: Account) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                f"""INSERT OR REPLACE INTO {_TBL}
                    (id, provider, name, mode, status, enabled, created_at, data)
                    VALUES (?,?,?,?,?,?,?,?)""",
                (
                    account.id, account.provider, account.name, account.mode,
                    account.status, 1 if account.enabled else 0, account.created_at,
                    json.dumps(account.to_dict(), ensure_ascii=False),
                ),
            )
            conn.commit()

    def _delete_account(self, account_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(f"DELETE FROM {_TBL} WHERE id = ?", (account_id,))
            conn.commit()

    def _set_meta(self, key: str, value: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {_META} (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    def save(self) -> None:
        """全量落库（兜底接口）。"""
        with self._lock:
            for accounts in self._accounts.values():
                for account in accounts:
                    self._persist_account(account)

    # ── 设置 ─────────────────────────────────────────────────────────────────
    def get_setting(self, key: str, default=None):
        with self._lock:
            try:
                with closing(self._connect()) as conn:
                    row = conn.execute(f"SELECT value FROM {_META} WHERE key = ?", (key,)).fetchone()
                    if row is not None:
                        self._settings[key] = row["value"]
                        return row["value"]
            except Exception:
                pass
            return self._settings.get(key, default)

    def set_setting(self, key: str, value) -> None:
        with self._lock:
            self._settings[key] = str(value)
            self._set_meta(key, str(value))

    def admin_key(self) -> str:
        return str(self.get_setting("admin_key", settings.DEFAULT_ADMIN_KEY) or "")

    def gateway_key(self) -> str:
        with self._lock:
            key = str(self._settings.get("gateway_key", "") or "").strip()
            if not key:
                key = f"sk-zcode2api-{secrets.token_hex(16)}"
                self._settings["gateway_key"] = key
                self._set_meta("gateway_key", key)
            return key

    def regenerate_gateway_key(self) -> str:
        with self._lock:
            key = f"sk-zcode2api-{secrets.token_hex(16)}"
            self._settings["gateway_key"] = key
            self._set_meta("gateway_key", key)
            return key

    def quota_refresh_interval(self) -> int:
        try:
            return max(0, int(self.get_setting("quota_refresh_interval", settings.QUOTA_REFRESH_INTERVAL)))
        except (TypeError, ValueError):
            return settings.QUOTA_REFRESH_INTERVAL

    def auto_claim(self) -> bool:
        return self.get_setting("auto_claim", "true").lower() in ("true", "1", "yes")

    def set_auto_claim(self, enabled: bool) -> None:
        self.set_setting("auto_claim", "true" if enabled else "false")

    def auto_switch(self) -> bool:
        return self.get_setting("auto_switch", "true").lower() in ("true", "1", "yes")

    def set_auto_switch(self, enabled: bool) -> None:
        self.set_setting("auto_switch", "true" if enabled else "false")

    # ── 账号读取 ─────────────────────────────────────────────────────────────
    def list_accounts(self, provider: str | None = None) -> list[Account]:
        with self._lock:
            if provider:
                return list(self._accounts.get(provider, []))
            return [a for p in PROVIDERS for a in self._accounts[p]]

    def find(self, provider: str, id_or_name: str) -> Account | None:
        with self._lock:
            return self._find_locked(provider, id_or_name)

    def find_any(self, id_or_name: str) -> Account | None:
        with self._lock:
            for p in PROVIDERS:
                for a in self._accounts[p]:
                    if a.id == id_or_name:
                        return a
        return None

    def _find_locked(self, provider: str, id_or_name: str) -> Account | None:
        for a in self._accounts.get(provider, []):
            if a.id == id_or_name or a.name == id_or_name:
                return a
        return None

    # ── 账号增删改 ───────────────────────────────────────────────────────────
    def add_account(self, provider: str, name: str, secret: str) -> Account:
        if provider not in PROVIDERS:
            raise ValueError(f"不支持的 provider: {provider}")
        account = Account.create(provider, name, secret)
        with self._lock:
            for a in self._accounts[provider]:
                if a.secret and a.secret == account.secret:
                    return a  # 跳过重复 token
            self._accounts[provider].append(account)
            self._persist_account(account)
        return account

    def remove_account(self, provider: str, id_or_name: str) -> bool:
        with self._lock:
            items = self._accounts.get(provider, [])
            target = next((a for a in items if a.id == id_or_name or a.name == id_or_name), None)
            if not target:
                return False
            self._accounts[provider] = [a for a in items if a.id != target.id]
            self._delete_account(target.id)
            return True

    def update_account(self, account: Account) -> None:
        """持久化某个账号的当前状态。"""
        with self._lock:
            self._persist_account(account)

    def set_enabled(self, provider: str, id_or_name: str, enabled: bool) -> bool:
        with self._lock:
            account = self._find_locked(provider, id_or_name)
            if not account:
                return False
            account.enabled = enabled
            if not enabled:
                account.status = Status.DISABLED
            elif account.status == Status.DISABLED:
                account.status = Status.ACTIVE
            self._persist_account(account)
            return True

    # ── 轮询选择 ─────────────────────────────────────────────────────────────
    # ── 轮询选择 ─────────────────────────────────────────────────────────────
    def select(self, provider: str, skip_ids: set[str] | None = None, model: str = "") -> Account | None:
        """按可用额度与轮询选择下一个可用账号。有可用额度的优先，用完/失效/冷却的自动跳过。"""
        skip_ids = skip_ids or set()
        now = time.time()
        with self._lock:
            pool = [
                a for a in self._accounts.get(provider, [])
                if a.is_selectable(now) and a.id not in skip_ids
            ]
            if not pool:
                return None

            def _remaining_tokens(acc: Account) -> int:
                if acc.mode == "apiKey":
                    return 999_999_999_999
                q = acc.quota or {}
                if not isinstance(q, dict) or not q:
                    return 0
                if model:
                    norm_m = model.upper()
                    for k, v in q.items():
                        if isinstance(v, dict) and (k.upper() == norm_m or norm_m in k.upper()):
                            rem = v.get("remaining")
                            if rem == -1:
                                return 999_999_999_999
                            return int(rem or 0)
                tot = 0
                for v in q.values():
                    if isinstance(v, dict):
                        rem = v.get("remaining")
                        if rem == -1:
                            return 999_999_999_999
                        if rem and int(rem) > 0:
                            tot += int(rem)
                return tot

            # 优先选择有剩余额度的账号
            has_quota = [a for a in pool if _remaining_tokens(a) > 0]
            candidates = has_quota if has_quota else pool

            idx = self._rotation.get(provider, 0) % len(candidates)
            account = candidates[idx]
            self._rotation[provider] = (idx + 1) % len(candidates)
            return account

    # ── 导入 / 导出 ─────────────────────────────────────────────────────────
    def export(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "exported_at": time.time(),
                "providers": {
                    p: [
                        {"name": a.name, "mode": a.mode, "secret": a.secret}
                        for a in self._accounts[p]
                    ]
                    for p in PROVIDERS
                },
            }

    def import_accounts(self, payload: dict) -> int:
        providers = payload.get("providers", {})
        count = 0
        for provider, items in providers.items():
            if provider not in PROVIDERS or not isinstance(items, list):
                continue
            for it in items:
                secret = it.get("secret") or it.get("token") or it.get("jwtToken") or it.get("apiKey")
                if not secret:
                    continue
                self.add_account(provider, it.get("name", provider), secret)
                count += 1
        return count


# 单例
store = Store()
