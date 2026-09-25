"""ZCode 额度 / 余额 / 用量查询，以及账号状态判定。

在查询基础上提供「额度用完自动标记 exhausted」的监控能力。
"""

from __future__ import annotations

import asyncio
import time

import httpx

from . import logs, settings
from .models import Account, Status
from .store import store


def _auth_headers(account: Account) -> dict:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ZCode/3.11.2",
        "X-ZCode-App-Version": "3.11.2",
        "HTTP-Referer": "https://zcode.z.ai",
        "X-Title": "Z Code@electron",
        "X-Platform": "win32-x64",
        "X-Release-Channel": "stable",
        "X-Client-Language": "zh-CN",
    }
    if account.mode == "jwt" and account.jwt_token:
        headers["Authorization"] = f"Bearer {account.jwt_token}"
    elif account.api_key:
        headers["x-api-key"] = account.api_key
        headers["Authorization"] = f"Bearer {account.api_key}"
    return headers


async def fetch_quota(account: Account) -> dict:
    """拉取单个账号的 方案 / 余额 / 用量，写回账号状态并持久化。

    返回结构: {"billing":..., "balance":..., "usage":..., "error":...}
    """
    headers = _auth_headers(account)
    base = settings.ZCODE_BILLING_BASE
    result: dict = {}
    quota_map: dict = {}

    if account.mode == "jwt" or (account.secret and account.secret.count(".") == 2):
        async with httpx.AsyncClient(timeout=20) as client:
            async def _get(path: str):
                try:
                    return await client.get(f"{base}{path}", headers=headers)
                except httpx.HTTPError:
                    return None

            billing_res, balance_res, usage_res = await asyncio.gather(
                _get("/billing/current"),
                _get("/billing/balance?app_version=3.11.2"),
                _get("/usage"),
            )

        now = time.time()
        account.last_checked_at = now

        # 鉴权失败 → 标记 invalid 并记录明确原因
        if billing_res is not None and billing_res.status_code in (401, 403):
            body = (billing_res.text or "").lower()
            if "captcha" not in body and "verify" not in body:
                account.status = Status.INVALID
                account.last_error = f"Token 失效或无权限 (HTTP {billing_res.status_code})"
                store.update_account(account)
                return {"error": account.last_error}

        plans = []
        if billing_res is not None and billing_res.status_code == 200:
            try:
                data = billing_res.json()
                result["billing"] = data
                plans = (data.get("data") or {}).get("plans") or []
                account.plan = plans[0] if plans else {}
            except (ValueError, KeyError):
                pass

        if balance_res is not None and balance_res.status_code == 200:
            try:
                data = balance_res.json()
                result["balance"] = data
                for bal in (data.get("data") or {}).get("balances") or []:
                    name = bal.get("show_name") or bal.get("model") or "model"
                    quota_map[name] = {
                        "total": bal.get("total_units"),
                        "used": bal.get("used_units"),
                        "remaining": bal.get("remaining_units"),
                        "expires_at": bal.get("expires_at"),
                    }
            except (ValueError, KeyError):
                pass

        if usage_res is not None and usage_res.status_code == 200:
            try:
                account.usage = usage_res.json().get("data") or {}
                result["usage"] = account.usage
            except (ValueError, KeyError):
                pass

        # 若 balance 接口未返回余额明细（如 Start Plan 体验方案），从 plan.entitlements 中提取额度
        if not quota_map and account.plan:
            entitlements = account.plan.get("entitlements") or []
            for ent in entitlements:
                name = ent.get("show_name") or ent.get("meter") or "model"
                grant = ent.get("grant_units")
                if grant is not None:
                    quota_map[name] = {
                        "total": grant,
                        "used": 0,
                        "remaining": grant,
                        "expires_at": account.plan.get("ends_at"),
                    }

        # 尝试自动领取（若开启自动领取且账号未领套餐或额度已耗尽）
        if not quota_map and store.auto_claim() and account.mode == "jwt":
            try:
                from .claim import auto_claim_account
                claim_res = await auto_claim_account(account)
                if claim_res.get("ok"):
                    logs.info("quota", f"账号 {account.name} 自动领取成功，重新检测额度...")
                    # 重新拉取账单
                    async with httpx.AsyncClient(timeout=15) as c:
                        fresh_b = await c.get(f"{base}/billing/current", headers=headers)
                        if fresh_b.status_code == 200:
                            f_data = fresh_b.json()
                            f_plans = (f_data.get("data") or {}).get("plans") or []
                            if f_plans:
                                account.plan = f_plans[0]
                                for ent in account.plan.get("entitlements") or []:
                                    name = ent.get("show_name") or ent.get("meter") or "model"
                                    grant = ent.get("grant_units")
                                    if grant is not None:
                                        quota_map[name] = {
                                            "total": grant,
                                            "used": 0,
                                            "remaining": grant,
                                            "expires_at": account.plan.get("ends_at"),
                                        }
            except Exception as e:
                logs.err("quota", f"自动领取异常: {e}")

        # 若依然无额度，根据网络与上游响应明确标注原因
        if not quota_map:
            if billing_res is None:
                account.last_error = "连接超时，无法访问 ZCode 认证服务器"
            elif billing_res.status_code == 429:
                account.last_error = "官方限流 429，请稍后再试"
            elif billing_res.status_code != 200:
                account.last_error = f"官方账单服务异常 HTTP {billing_res.status_code}"
            else:
                b_code = (result.get("billing") or {}).get("code", 0)
                b_msg = (result.get("billing") or {}).get("msg") or (result.get("billing") or {}).get("message")
                if b_code != 0:
                    account.last_error = f"官方返回: {b_msg or f'code {b_code}'}"
                elif not plans:
                    account.last_error = "未开通方案（新号/未领取，需点击「刷新资格」领取 Start Plan）"
                else:
                    account.last_error = "当前方案无可用配额包"

    elif account.provider == "bigmodel" and account.api_key:
        now = time.time()
        account.last_checked_at = now
        async with httpx.AsyncClient(timeout=20) as client:
            bm_headers = {
                "Authorization": f"Bearer {account.api_key}",
                "User-Agent": "ZCode/3.11.2",
            }
            try:
                mon_res = await client.get("https://open.bigmodel.cn/api/monitor/usage/quota/limit", headers=bm_headers)
                if mon_res.status_code == 200:
                    mon_data = mon_res.json()
                    result["monitor"] = mon_data
                    limits = (mon_data.get("data") or {}).get("limits") or []
                    for l in limits:
                        typ = l.get("type", "")
                        name = "提示次数" if typ == "TOKENS_LIMIT" else ("使用时长" if typ == "TIME_LIMIT" else typ)
                        quota_map[name] = {
                            "total": l.get("usage"),
                            "used": l.get("currentValue"),
                            "remaining": l.get("remaining"),
                            "expires_at": l.get("nextResetTime"),
                        }
                    if not limits:
                        account.last_error = "开放平台未配置用量限制或未开通对应模型"
                elif mon_res.status_code in (401, 403):
                    account.status = Status.INVALID
                    account.last_error = f"智谱开放平台鉴权失败 HTTP {mon_res.status_code} (API Key 无效)"
                else:
                    account.last_error = f"智谱开放平台监控接口返回 HTTP {mon_res.status_code}"
            except Exception as err:
                account.last_error = f"智谱监控接口访问超时: {err}"
    elif account.mode == "apiKey":
        account.last_error = "API Key 模式（不支持官方用量接口，可直接调用）"

    if quota_map:
        account.quota = quota_map
        # 额度用完判定：所有模型剩余 <= 0
        remainings = [
            q.get("remaining") for q in quota_map.values() if q.get("remaining") is not None
        ]
        if remainings and all((r or 0) <= 0 for r in remainings):
            account.status = Status.EXHAUSTED
            account.last_error = "额度已用完 (0 剩余)"
        elif account.status in (Status.EXHAUSTED, Status.COOLING, Status.INVALID):
            # 额度恢复 → 重新激活
            account.status = Status.ACTIVE
            account.last_error = None
            account.cooling_until = None
        else:
            account.last_error = None

    store.update_account(account)
    return result or {"error": account.last_error or "无法获取额度数据"}


async def refresh_accounts(accounts: list[Account]) -> dict:
    """并发刷新一批账号，返回汇总。"""
    if not accounts:
        return {"ok": 0, "fail": 0}
    sem = asyncio.Semaphore(8)

    async def _one(acc: Account) -> bool:
        async with sem:
            res = await fetch_quota(acc)
            return "error" not in res

    results = await asyncio.gather(*[_one(a) for a in accounts], return_exceptions=True)
    ok = sum(1 for r in results if r is True)
    return {"ok": ok, "fail": len(accounts) - ok}


class QuotaMonitor:
    """后台周期性刷新可管理账号的额度，实现实时用量监控。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _loop(self) -> None:
        # 启动后先等几秒，避免与服务启动争抢
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            interval = store.quota_refresh_interval()  # 实时读取设置，改后即生效
            if interval > 0:
                try:
                    accounts = [
                        a for a in store.list_accounts()
                        if (a.mode == "jwt" or (a.provider == "bigmodel" and a.api_key))
                        and a.status != Status.DISABLED
                    ]
                    if accounts:
                        await refresh_accounts(accounts)
                except Exception as err:  # noqa: BLE001 - 后台任务需吞掉异常继续运行
                    logs.err("quota", f"后台刷新出错: {err}")
            # interval<=0 视为关闭：仍周期性回看设置，便于随时启用
            wait = interval if interval > 0 else 30
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                continue

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


monitor = QuotaMonitor()
