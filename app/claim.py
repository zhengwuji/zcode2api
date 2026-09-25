"""ZCode 资格检测与自动领取服务。

对齐 Z-Accounts 机制：
1. 检测 /billing/preview 端点资格与待领取套餐
2. 自动求解阿里云无痕验证码并调用 /billing/claim 完成领取
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import httpx

from . import logs, settings
from .captcha import captcha_manager
from .models import Account, Status
from .quota import _auth_headers, fetch_quota
from .store import store

BILLING_PREVIEW_URL = "https://zcode.z.ai/api/v1/zcode-plan/billing/preview"
BILLING_CLAIM_URL = "https://zcode.z.ai/api/v1/zcode-plan/billing/claim"
CLIENT_CONFIGS_URL = "https://zcode.z.ai/api/v1/client/configs"


def get_device_mid() -> str:
    """获取或初始化持久化 Device MID。"""
    user_home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    candidates = [
        user_home / ".zcode" / "v2" / "telemetry-state.json",
        user_home / ".zcode" / "telemetry-state.json",
        settings.DATA_DIR / "telemetry-state.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                mid = data.get("deviceMid")
                if mid and isinstance(mid, str) and len(mid) > 8:
                    return mid
            except Exception:
                pass

    # 首次自动生成并保存在 data 目录
    new_mid = str(uuid.uuid4())
    save_path = settings.DATA_DIR / "telemetry-state.json"
    try:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        save_path.write_text(json.dumps({"deviceMid": new_mid}, indent=2), encoding="utf-8")
    except Exception:
        pass
    return new_mid


def _claim_headers(account: Account) -> dict:
    headers = _auth_headers(account)
    headers["X-Device-Mid"] = get_device_mid()
    headers["X-Platform"] = "win32-x64"
    return headers


async def preview_plans(account: Account) -> dict:
    """查询账号当前可领取的方案列表及资格状态。"""
    if account.mode != "jwt" and not (account.secret and account.secret.count(".") == 2):
        return {"ok": False, "code": -1, "message": "仅 JWT 模式账号支持方案资格检测", "plans": []}

    headers = _claim_headers(account)
    url = f"{BILLING_PREVIEW_URL}?app_version=3.11.2&platform=win32-x64"

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            res = await client.get(url, headers=headers)
        except Exception as err:
            return {"ok": False, "code": -1, "message": f"连接上游超时: {err}", "plans": []}

    try:
        data = res.json()
    except Exception:
        return {"ok": False, "code": res.status_code, "message": f"HTTP {res.status_code}", "plans": []}

    code = data.get("code", -1)
    if code != 0:
        msg = data.get("msg") or data.get("message") or "资格不可用"
        if code == 1005:
            msg = "当前方案尚未到期，暂无需领取"
        elif code == 3001:
            msg = "参数校验错误"
        return {"ok": False, "code": code, "message": msg, "plans": []}

    plans = (data.get("data") or {}).get("plans") or []
    server_time = (data.get("data") or {}).get("server_time")
    return {"ok": True, "code": 0, "plans": plans, "server_time": server_time}


async def submit_claim(account: Account, plan_id: str) -> dict:
    """自动求解阿里云验证码并提交套餐领取请求。"""
    if account.mode != "jwt" and not (account.secret and account.secret.count(".") == 2):
        return {"ok": False, "message": "仅 JWT 模式账号支持领取套餐"}

    logs.info("claim", f"开始为账号 {account.name} 领取方案: {plan_id}...")
    try:
        captcha_param = await captcha_manager.solve()
    except Exception as err:
        logs.err("claim", f"验证码求解失败: {err}")
        return {"ok": False, "message": f"验证码求解失败: {err}"}

    headers = _claim_headers(account)
    headers["X-Aliyun-Captcha-Verify-Param"] = captcha_param

    async with httpx.AsyncClient(timeout=25) as client:
        try:
            res = await client.post(BILLING_CLAIM_URL, headers=headers, json={"plan_id": plan_id})
            data = res.json()
        except Exception as err:
            logs.err("claim", f"提交领取失败: {err}")
            return {"ok": False, "message": f"请求失败: {err}"}

    code = data.get("code", -1)
    if code == 0:
        logs.info("claim", f"账号 {account.name} 领取方案 {plan_id} 成功！")
        # 刷新额度
        await fetch_quota(account)
        return {"ok": True, "data": data}

    msg = data.get("msg") or data.get("message") or "领取失败"
    if code == 1005:
        msg = "当前方案尚未到期"
    logs.warn("claim", f"账号 {account.name} 领取方案失败 ({code}): {msg}")
    return {"ok": False, "code": code, "message": msg}


async def auto_claim_account(account: Account) -> dict:
    """自动检测并领取符合条件的套餐。"""
    if not store.auto_claim():
        return {"ok": False, "message": "自动领取未开启"}
    if account.mode != "jwt" and not (account.secret and account.secret.count(".") == 2):
        return {"ok": False, "message": "非 JWT 账号"}

    now = time.time()
    plan = account.plan or {}
    ends_at = plan.get("ends_at")
    # 如果当前方案有效且额度充足，则暂不需要重复抢领
    if ends_at and ends_at > now and account.status not in (Status.EXHAUSTED, Status.INVALID):
        return {"ok": True, "message": "当前方案仍有效，无需领取"}

    prev = await preview_plans(account)
    if not prev.get("ok"):
        return prev

    plans = prev.get("plans") or []
    if not plans:
        return {"ok": True, "message": "暂无待领取套餐"}

    # 按 priority 降序挑选最高优套餐
    plans.sort(key=lambda p: p.get("priority", 0), reverse=True)
    target_plan = plans[0]
    plan_id = target_plan.get("plan_id")
    if not plan_id:
        return {"ok": False, "message": "方案缺少 plan_id"}

    return await submit_claim(account, plan_id)
