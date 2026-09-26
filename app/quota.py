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
    mid = ""
    try:
        from proxy.account_info import get_device_mid
        mid = get_device_mid()
    except Exception:
        pass

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ZCode/3.14.0",
        "X-ZCode-App-Version": "3.14.0",
        "HTTP-Referer": "https://zcode.z.ai",
        "X-Title": "Z Code@cli",
        "X-Platform": "win32-x64",
        "X-Release-Channel": "stable",
        "X-Client-Language": "zh-CN",
    }
    if mid:
        headers["X-Device-Mid"] = mid

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
                _get("/billing/balance?app_version=3.14.0&platform=win32-x64"),
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
        if balance_res is not None and balance_res.status_code == 200:
            try:
                b_data = balance_res.json()
                result["balance"] = b_data
                plans = (b_data.get("data") or {}).get("plans") or []
            except Exception:
                pass

        if not plans and billing_res is not None and billing_res.status_code == 200:
            try:
                data = billing_res.json()
                result["billing"] = data
                plans = (data.get("data") or {}).get("plans") or []
            except Exception:
                pass

        if plans:
            account.plan = dict(plans[0])
            account.plan["all_plans"] = plans

        if balance_res is not None and balance_res.status_code == 200:
            try:
                data = balance_res.json()
                result["balance"] = data
                balances = (data.get("data") or {}).get("balances") or []
                if account.plan and isinstance(account.plan, dict):
                    account.plan["balances"] = balances

                for bal in balances:
                    name = bal.get("show_name") or bal.get("model") or "model"
                    tot = bal.get("total_units") or 0
                    used = bal.get("used_units") or 0
                    rem = bal.get("remaining_units") or 0
                    exp = bal.get("expires_at")

                    if name not in quota_map:
                        quota_map[name] = {
                            "total": tot,
                            "used": used,
                            "remaining": rem,
                            "expires_at": exp,
                        }
                    else:
                        quota_map[name]["total"] = (quota_map[name].get("total") or 0) + tot
                        quota_map[name]["used"] = (quota_map[name].get("used") or 0) + used
                        quota_map[name]["remaining"] = (quota_map[name].get("remaining") or 0) + rem
                        if rem > 0 and exp:
                            curr_exp = quota_map[name].get("expires_at")
                            if not curr_exp or exp > curr_exp:
                                quota_map[name]["expires_at"] = exp
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
            all_p = account.plan.get("all_plans") or [account.plan]
            for p in all_p:
                for ent in p.get("entitlements") or []:
                    name = ent.get("show_name") or ent.get("meter") or "model"
                    grant = ent.get("grant_units")
                    if grant is not None:
                        if name not in quota_map:
                            quota_map[name] = {
                                "total": grant,
                                "used": 0,
                                "remaining": grant,
                                "expires_at": p.get("ends_at"),
                            }
                        else:
                            quota_map[name]["total"] = (quota_map[name].get("total") or 0) + grant
                            quota_map[name]["remaining"] = (quota_map[name].get("remaining") or 0) + grant

        # 尝试自动领取（若开启自动领取且账号未领套餐或额度已耗尽）
        if not quota_map and store.auto_claim() and account.mode == "jwt":
            try:
                from .claim import auto_claim_account
                claim_res = await auto_claim_account(account)
                if claim_res.get("ok"):
                    logs.info("quota", f"账号 {account.name} 自动领取成功，重新检测额度...")
                    async with httpx.AsyncClient(timeout=15) as c:
                        fresh_b = await c.get(f"{base}/billing/current", headers=headers)
                        if fresh_b.status_code == 200:
                            f_data = fresh_b.json()
                            f_plans = (f_data.get("data") or {}).get("plans") or []
                            if f_plans:
                                account.plan = dict(f_plans[0])
                                account.plan["all_plans"] = f_plans
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
        async with httpx.AsyncClient(timeout=15) as client:
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
                elif mon_res.status_code in (401, 403):
                    account.status = Status.INVALID
                    account.last_error = f"智谱开放平台鉴权失败 HTTP {mon_res.status_code} (API Key 无效)"
            except Exception:
                pass

            if not quota_map and account.status != Status.INVALID:
                try:
                    v_res = await client.get("https://open.bigmodel.cn/api/anthropic/v1/models", headers=bm_headers)
                    if v_res.status_code == 200:
                        quota_map["APIKey直连"] = {
                            "total": None,
                            "used": None,
                            "remaining": -1,
                            "status": "可用",
                        }
                        account.status = Status.ACTIVE
                        account.last_error = None
                        account.plan = {
                            "name": "智谱开放平台 API Key",
                            "description": "平台团队/按需配额，已验证全模型直连畅通",
                        }
                    elif v_res.status_code in (401, 403):
                        account.status = Status.INVALID
                        account.last_error = f"智谱开放平台鉴权失败 HTTP {v_res.status_code} (Key 无效)"
                    else:
                        account.last_error = f"智谱开放平台响应异常 HTTP {v_res.status_code}"
                except Exception as e:
                    account.last_error = f"智谱模型接口连接失败: {e}"

    elif account.provider == "zai" and account.mode == "apiKey" and account.api_key:
        now = time.time()
        account.last_checked_at = now
        async with httpx.AsyncClient(timeout=15) as client:
            zai_headers = {
                "Authorization": f"Bearer {account.api_key}",
                "User-Agent": "ZCode/3.14.0",
            }
            try:
                v_res = await client.get("https://api.z.ai/api/anthropic/v1/models", headers=zai_headers)
                if v_res.status_code == 200:
                    quota_map["APIKey直连"] = {
                        "total": None,
                        "used": None,
                        "remaining": -1,
                        "status": "可用",
                    }
                    account.status = Status.ACTIVE
                    account.last_error = None
                    account.plan = {
                        "name": "Z.AI 开发者 / 团队 API Key",
                        "description": "直连调用畅通，支持 GLM-4.5 / Claude 协议",
                    }
                elif v_res.status_code in (401, 403):
                    account.status = Status.INVALID
                    account.last_error = f"Z.AI 鉴权失败 HTTP {v_res.status_code} (Key 无效)"
                else:
                    account.last_error = f"Z.AI 模型接口异常 HTTP {v_res.status_code}"
            except Exception as e:
                account.last_error = f"Z.AI 接口连接失败: {e}"
    elif account.mode == "apiKey":
        account.last_error = "API Key 模式（不支持官方用量接口，可直接调用）"

    if quota_map:
        account.quota = quota_map
        result["quota"] = quota_map
        result["status"] = account.status
        remainings = [
            q.get("remaining") for q in quota_map.values() if q.get("remaining") is not None
        ]
        has_positive = any(r > 0 or r == -1 for r in remainings)
        if remainings and not has_positive:
            account.status = Status.EXHAUSTED
            account.last_error = "额度已用完 (0 剩余)"
        elif account.status in (Status.EXHAUSTED, Status.COOLING, Status.INVALID):
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
