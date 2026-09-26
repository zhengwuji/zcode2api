"""核心网关：兼容 Anthropic Messages 协议的 /v1/messages。

实现多账号轮询 + 额度用完自动换号 + 阿里无痕验证自动续期。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import logs, settings
from ..agent import build_request
from ..auth_admin import verify_gateway_key
from ..captcha import captcha_manager
from ..models import Account, Status
from ..openai_bridge import (
    anthropic_to_openai_response,
    openai_to_anthropic_body,
    stream_anthropic_to_openai,
)
from ..quota import fetch_quota
from ..store import store

router = APIRouter()

MAX_CAPTCHA_RETRIES = 3
MAX_ACCOUNT_ATTEMPTS = 5

# Z.AI 上游模型名大小写敏感
MODEL_NAME_MAP = {
    "glm-5.3": "GLM-5.3",
    "glm-5.3-flash": "GLM-5.3-Flash",
    "glm-5.2": "GLM-5.2",
    "glm-5-turbo": "GLM-5-Turbo",
    "glm-turbo": "GLM-5-Turbo",
    "glm-5.1": "GLM-5.1",
    "glm-4.7": "GLM-4.7",
}

# /v1/models 对外公布的可用模型
AVAILABLE_MODELS = ["GLM-5.3", "GLM-5.3-Flash", "GLM-5.2", "GLM-5-Turbo"]

# 命中以下信号则认为账号额度用完
_EXHAUST_KEYWORDS = ("quota", "insufficient", "balance", "exhaust", "额度", "余额不足")
_RATE_LIMIT_KEYWORDS = ("rate limit", "too many requests", "frequency limit", "并发超限", "请求过于频繁", "限流", "429")


def _detect_provider(body: dict, headers) -> str:
    model = body.get("model") or ""
    if model.startswith("bigmodel/") or headers.get("x-provider") == "bigmodel":
        return "bigmodel"
    if headers.get("x-provider") == "zai":
        return "zai"

    def _total_quota(p: str) -> int:
        total = 0
        for a in store.list_accounts(p):
            if a.is_selectable() and a.quota and isinstance(a.quota, dict):
                for v in a.quota.values():
                    if isinstance(v, dict):
                        rem = int(v.get("remaining", 0) or 0)
                        if rem == -1 or a.mode == "apiKey":
                            total += 100_000_000
                        elif rem > 0:
                            total += rem
        return total

    zai_quota = _total_quota("zai")
    bigmodel_quota = _total_quota("bigmodel")

    # 优先选择拥有可用剩余额度的渠道
    if bigmodel_quota > 0 and zai_quota <= 0:
        return "bigmodel"
    if zai_quota > 0 and bigmodel_quota <= 0:
        return "zai"

    zai_available = any(a.is_selectable() for a in store.list_accounts("zai"))
    bigmodel_available = any(a.is_selectable() for a in store.list_accounts("bigmodel"))
    if not zai_available and bigmodel_available:
        return "bigmodel"
    return "zai"


def _normalize_body(body: dict) -> dict:
    model = body.get("model")
    if isinstance(model, str) and "/" in model:
        model = "/".join(model.split("/")[1:])
    if isinstance(model, str):
        model = MODEL_NAME_MAP.get(model.lower(), model)
        body["model"] = model

    messages = body.get("messages")
    if isinstance(messages, list):
        bridged = []
        for msg in messages:
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                bridged.append({**msg, "content": [{"type": "text", "text": msg["content"]}]})
            else:
                bridged.append(msg)
        body["messages"] = bridged
    return body


def _is_captcha_error(text: str) -> bool:
    low = text.lower()
    return "captcha" in low or "verify token" in low or "verify failed" in low


def _is_exhausted(status_code: int, text: str) -> bool:
    if status_code in (402,):
        return True
    low = text.lower()
    return any(k in low for k in _EXHAUST_KEYWORDS)


def _check_business_error(data: dict | None, text: str) -> tuple[bool, str, str]:
    """检查响应中是否隐藏了业务报错信息。返回 (is_error, error_type, message)。"""
    low_text = text.lower()
    if any(k in low_text for k in _RATE_LIMIT_KEYWORDS):
        msg = (data.get("msg") or data.get("message") or "上游限流或并发超限") if isinstance(data, dict) else "上游限流或并发超限"
        return True, "rate_limit", msg

    if _is_exhausted(200, text):
        msg = (data.get("msg") or data.get("message") or "当前账号额度已耗尽") if isinstance(data, dict) else "当前账号额度已耗尽"
        return True, "insufficient_quota", msg

    if not isinstance(data, dict):
        return False, "", ""

    # 1. BigModel / PaaS 协议错误码 (code != 0 and code != 200)
    code = data.get("code")
    if code is not None and code not in (0, 200, "0", "200"):
        msg = str(data.get("msg") or data.get("message") or f"上游错误码 {code}")
        low_msg = msg.lower()
        if any(k in low_msg for k in _RATE_LIMIT_KEYWORDS) or code in (1301, 1302):
            return True, "rate_limit", msg
        if any(k in low_msg for k in _EXHAUST_KEYWORDS) or code in (1214, 1215):
            return True, "insufficient_quota", msg
        return True, "upstream_error", msg

    # 2. Anthropic / OpenAI 显式 error 结构
    if "error" in data and isinstance(data["error"], (dict, str)):
        err = data["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        err_type = err.get("type", "upstream_error") if isinstance(err, dict) else "upstream_error"
        low_msg = msg.lower()
        if any(k in low_msg for k in _RATE_LIMIT_KEYWORDS) or err_type in ("rate_limit_error", "rate_limit"):
            return True, "rate_limit", msg
        if any(k in low_msg for k in _EXHAUST_KEYWORDS) or err_type == "insufficient_quota":
            return True, "insufficient_quota", msg
        return True, err_type, msg

    # 3. type == "error"
    if data.get("type") == "error":
        msg = str(data.get("message") or (data.get("error") or {}).get("message") or "上游请求失败")
        low_msg = msg.lower()
        if any(k in low_msg for k in _RATE_LIMIT_KEYWORDS):
            return True, "rate_limit", msg
        if any(k in low_msg for k in _EXHAUST_KEYWORDS):
            return True, "insufficient_quota", msg
        return True, "upstream_error", msg

    return False, "", ""


def _select_candidate(preferred_provider: str, tried: set[str], model: str = "") -> Account | None:
    """优先从首选平台选择可用账号；若无可用账号，自动跨平台无缝寻找可用账号。"""
    acc = store.select(preferred_provider, skip_ids=tried, model=model)
    if acc is not None:
        return acc
    for p in ("zai", "bigmodel"):
        if p != preferred_provider:
            alt = store.select(p, skip_ids=tried, model=model)
            if alt is not None:
                return alt
    return None


def _mark(account: Account, status_value: str, error: str | None = None) -> None:
    account.status = status_value
    account.last_error = error
    if error:
        account.last_switch_reason = error
        account.last_switched_at = time.time()
    if status_value == Status.COOLING:
        account.cooling_until = time.time() + settings.COOLING_SECONDS
    store.update_account(account)


def _last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    return part.get("text", "")
    return ""


@router.get("/v1/models", dependencies=[Depends(verify_gateway_key)])
async def list_models():
    """列出可用模型（Anthropic /v1/models 风格）。"""
    return {
        "object": "list",
        "data": [
            {"id": i, "type": "model", "display_name": i, "created_at": "2025-01-01T00:00:00Z"}
            for i in AVAILABLE_MODELS
        ],
    }


@router.post("/v1/chat/completions", dependencies=[Depends(verify_gateway_key)])
async def chat_completions(request: Request):
    """OpenAI 风格兼容端点：将 chat/completions 请求转为 Anthropic/ZCode 并在返回时桥接转换。"""
    try:
        raw_body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request_error"}}, status_code=400)

    model_name = str(raw_body.get("model") or "GLM-5.3")
    chat_id = f"chatcmpl-{secrets.token_hex(12)}"
    anthropic_body = openai_to_anthropic_body(raw_body)
    incoming_headers = dict(request.headers)
    provider = _detect_provider(anthropic_body, request.headers)
    body = _normalize_body(anthropic_body)
    port = request.url.port or settings.PORT
    payload = json.dumps(body).encode("utf-8")

    req_id = secrets.token_hex(3)
    logs.req(req_id, f"[OpenAI] {model_name}", bool(body.get("stream")), _last_user_text(body))

    tried: set[str] = set()
    total_accs = len(store.list_accounts())
    max_attempts = max(MAX_ACCOUNT_ATTEMPTS, total_accs) if store.auto_switch() else 1
    last_error_response = None

    for _ in range(max_attempts):
        account = _select_candidate(provider, tried, model=model_name)
        if account is None:
            break
        tried.add(account.id)
        needs_captcha = account.provider == "zai" and account.mode == "jwt"

        result = await _try_account(
            req_id, account, body, payload, incoming_headers, port, needs_captcha,
            is_openai=True, chat_id=chat_id, model_name=model_name
        )
        if result is _NEXT_ACCOUNT:
            continue
        if isinstance(result, tuple) and result[0] is _NEXT_ACCOUNT:
            last_error_response = result[1]
            continue
        return result

    if last_error_response is not None:
        return last_error_response

    logs.req_err(req_id, "无可用账号 / 额度均已耗尽")
    return JSONResponse(
        {"error": {"message": "所有账号均不可用或额度已用完，请在后台检查账号状态", "type": "insufficient_quota"}},
        status_code=503,
    )


@router.post("/v1/messages", dependencies=[Depends(verify_gateway_key)])
async def messages(request: Request):
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}}, status_code=400)

    incoming_headers = dict(request.headers)
    provider = _detect_provider(body, request.headers)
    body = _normalize_body(body)
    # 验证码页面由本服务托管，端口取实际请求端口（兼容任意启动端口）
    port = request.url.port or settings.PORT
    payload = json.dumps(body).encode("utf-8")
    req_model = str(body.get("model") or "")

    req_id = secrets.token_hex(3)
    logs.req(req_id, str(body.get("model") or "-"), bool(body.get("stream")), _last_user_text(body))

    tried: set[str] = set()
    total_accs = len(store.list_accounts())
    max_attempts = max(MAX_ACCOUNT_ATTEMPTS, total_accs) if store.auto_switch() else 1
    last_error_response = None

    for _ in range(max_attempts):
        account = _select_candidate(provider, tried, model=req_model)
        if account is None:
            break
        tried.add(account.id)
        needs_captcha = account.provider == "zai" and account.mode == "jwt"

        result = await _try_account(req_id, account, body, payload, incoming_headers, port, needs_captcha)
        if result is _NEXT_ACCOUNT:
            continue
        if isinstance(result, tuple) and result[0] is _NEXT_ACCOUNT:
            last_error_response = result[1]
            continue
        return result

    if last_error_response is not None:
        return last_error_response

    logs.req_err(req_id, "无可用账号 / 额度均已耗尽")
    return JSONResponse(
        {"error": {"message": "所有账号均不可用或额度已用完，请在后台检查账号状态", "type": "no_available_account"}},
        status_code=503,
    )


_NEXT_ACCOUNT = object()


async def _try_account(
    req_id, account, body, payload, incoming_headers, port, needs_captcha,
    is_openai: bool = False, chat_id: str = "", model_name: str = ""
):
    """尝试用单个账号转发，含验证码续期。返回 Response 或 _NEXT_ACCOUNT。"""
    for attempt in range(MAX_CAPTCHA_RETRIES):
        verify_param = None
        if needs_captcha:
            try:
                verify_param = await captcha_manager.get_verify_param(port)
            except Exception as err:  # noqa: BLE001
                logs.req_err(req_id, f"人机校验失败: {err}")
                account.fail_count += 1
                account.last_error = f"人机校验失败: {err}"
                if account.fail_count >= 2:
                    account.status = Status.COOLING
                    account.cooling_until = time.time() + 60
                store.update_account(account)

                err_resp = JSONResponse(
                    {"error": {"message": f"无法完成人机校验: {err}", "type": "captcha_error"}},
                    status_code=500,
                )
                if store.auto_switch():
                    logs.warn(req_id, f"账号 {account.name} 人机校验失败，自动切换下一个可用账号")
                    return (_NEXT_ACCOUNT, err_resp)
                return err_resp

        try:
            url, headers = build_request(account, body, verify_param, incoming_headers)
        except RuntimeError as err:
            _mark(account, Status.INVALID, str(err))
            logs.warn(req_id, f"账号 {account.name} 凭证无效，自动切换下一个可用账号")
            err_resp = JSONResponse({"error": {"message": str(err), "type": "invalid_credentials"}}, status_code=401)
            if store.auto_switch():
                return (_NEXT_ACCOUNT, err_resp)
            return err_resp

        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0))
        cm = client.stream("POST", url, headers=headers, content=payload)
        try:
            resp = await cm.__aenter__()
        except (httpx.HTTPError, asyncio.TimeoutError, Exception) as err:
            await client.aclose()
            account.fail_count += 1
            account.last_error = f"连接异常: {err}"
            # 临时冷却 60 秒，避免后续请求再次撞上该故障账号
            account.status = Status.COOLING
            account.cooling_until = time.time() + 60
            store.update_account(account)
            logs.warn(req_id, f"账号 {account.name} 连接异常: {err}，自动切换下一个可用账号")
            err_resp = JSONResponse({"error": {"message": f"账号连接失败: {err}", "type": "connection_error"}}, status_code=502)
            if store.auto_switch():
                return (_NEXT_ACCOUNT, err_resp)
            return err_resp

        status_code = resp.status_code

        # 4xx / 5xx 状态码故障转移
        if status_code >= 400:
            text = (await resp.aread()).decode("utf-8", "ignore")
            await cm.__aexit__(None, None, None)
            await client.aclose()

            if status_code == 403 and _is_captcha_error(text) and needs_captcha:
                captcha_manager.invalidate()
                logs.warn(req_id, f"账号 {account.name} 验证码失效，刷新重试")
                continue  # 同账号重试验证码

            if _is_exhausted(status_code, text):
                _mark(account, Status.EXHAUSTED, "额度已用完")
                logs.warn(req_id, f"账号 {account.name} 额度用完 (HTTP {status_code})，自动切换下一个可用账号")
                asyncio.create_task(_safe_refresh(account))
                err_resp = JSONResponse({"error": {"message": "当前账号额度已耗尽", "type": "insufficient_quota"}}, status_code=402)
                return (_NEXT_ACCOUNT, err_resp)

            if status_code in (401, 403):
                _mark(account, Status.INVALID, f"鉴权失败 HTTP {status_code}")
                logs.warn(req_id, f"账号 {account.name} 鉴权失败 HTTP {status_code}，自动切换下一个可用账号")
                err_resp = JSONResponse({"error": {"message": f"上游鉴权失败 HTTP {status_code}", "type": "auth_error"}}, status_code=status_code)
                return (_NEXT_ACCOUNT, err_resp)

            if status_code == 429:
                _mark(account, Status.COOLING, "上游限流 429")
                logs.warn(req_id, f"账号 {account.name} 被限流 429，自动切换下一个可用账号")
                err_resp = JSONResponse({"error": {"message": "上游限流 429", "type": "rate_limit_error"}}, status_code=429)
                if store.auto_switch():
                    return (_NEXT_ACCOUNT, err_resp)
                return err_resp

            # 其它错误（500/502/503/504 服务器错误或 400 业务报错等）：自动换号容灾
            account.fail_count += 1
            account.last_error = f"上游错误 HTTP {status_code}: {text[:200]}"
            if status_code >= 500 or account.fail_count >= 2:
                account.status = Status.COOLING
                account.cooling_until = time.time() + 60
            store.update_account(account)

            err_data = _safe_json(text) or {"error": {"message": text[:500], "type": "upstream_error"}}
            err_resp = JSONResponse(err_data, status_code=status_code)

            if store.auto_switch():
                logs.warn(req_id, f"账号 {account.name} 请求失败 HTTP {status_code}，自动切换下一个可用账号")
                return (_NEXT_ACCOUNT, err_resp)

            logs.req_err(req_id, f"上游错误 HTTP {status_code}（账号 {account.name}）")
            return err_resp

        # status_code < 400：检查响应中是否潜藏业务层报错或额度耗尽
        content_type = resp.headers.get("content-type", "")
        is_stream = bool(body.get("stream"))

        # 如果是非流式，或者虽然客户端请求流式但上游返回了 JSON（常见于报错分支）
        if not is_stream or "application/json" in content_type:
            text = (await resp.aread()).decode("utf-8", "ignore")
            await cm.__aexit__(None, None, None)
            await client.aclose()
            data = _safe_json(text)

            is_biz_err, err_type, err_msg = _check_business_error(data, text)
            if is_biz_err:
                if err_type == "rate_limit":
                    _mark(account, Status.COOLING, f"上游限流: {err_msg}")
                    logs.warn(req_id, f"账号 {account.name} 被限流（{err_msg}），自动切换下一个可用账号")
                    err_resp = JSONResponse({"error": {"message": err_msg, "type": "rate_limit_error"}}, status_code=429)
                elif err_type == "insufficient_quota":
                    _mark(account, Status.EXHAUSTED, err_msg)
                    logs.warn(req_id, f"账号 {account.name} 额度用完（业务返回: {err_msg}），自动切换下一个可用账号")
                    asyncio.create_task(_safe_refresh(account))
                    err_resp = JSONResponse({"error": {"message": err_msg, "type": "insufficient_quota"}}, status_code=402)
                else:
                    account.fail_count += 1
                    account.last_error = f"业务报错: {err_msg}"
                    account.status = Status.COOLING
                    account.cooling_until = time.time() + 60
                    store.update_account(account)
                    logs.warn(req_id, f"账号 {account.name} 业务报错（{err_msg}），自动切换下一个可用账号")
                    err_resp = JSONResponse(data if isinstance(data, dict) else {"error": {"message": err_msg, "type": err_type}}, status_code=400)

                if store.auto_switch():
                    return (_NEXT_ACCOUNT, err_resp)
                return err_resp

            # 请求真正成功：更新指标并清除冷却
            account.use_count += 1
            account.fail_count = 0
            account.last_used_at = time.time()
            if account.status in (Status.COOLING, Status.EXHAUSTED):
                account.status = Status.ACTIVE
                account.cooling_until = None
            store.set_active_account(account.id)
            store.update_account(account)
            asyncio.create_task(_safe_refresh(account))

            logs.req_ok(req_id)
            if is_openai:
                return JSONResponse(anthropic_to_openai_response(data or {}, model_name, chat_id), status_code=status_code)
            return JSONResponse(data or {}, status_code=status_code)

        # 真正流式传输（text/event-stream）：预读首块数据探活，确保未报错再向客户端发流
        aiter = resp.aiter_bytes()
        first_chunk = b""
        try:
            first_chunk = await aiter.__anext__()
        except StopAsyncIteration:
            first_chunk = b""
        except Exception as err:
            await cm.__aexit__(None, None, None)
            await client.aclose()
            account.fail_count += 1
            account.last_error = f"流首包读取失败: {err}"
            account.status = Status.COOLING
            account.cooling_until = time.time() + 60
            store.update_account(account)
            logs.warn(req_id, f"账号 {account.name} 流首包读取异常: {err}，自动切换下一个可用账号")
            err_resp = JSONResponse({"error": {"message": f"流读取异常: {err}", "type": "stream_read_error"}}, status_code=502)
            if store.auto_switch():
                return (_NEXT_ACCOUNT, err_resp)
            return err_resp

        # 检查首包是否含有明显的错误事件
        first_str = first_chunk.decode("utf-8", "ignore") if first_chunk else ""
        if "error" in first_str.lower() and ("event: error" in first_str or '"type": "error"' in first_str or '"type":"error"' in first_str):
            await cm.__aexit__(None, None, None)
            await client.aclose()
            low_first = first_str.lower()
            if any(k in low_first for k in _RATE_LIMIT_KEYWORDS):
                _mark(account, Status.COOLING, "上游流式限流 429")
                err_resp = JSONResponse({"error": {"message": "上游流式限流", "type": "rate_limit_error"}}, status_code=429)
                logs.warn(req_id, f"账号 {account.name} 流首包被限流，自动切换下一个可用账号")
            elif _is_exhausted(200, first_str):
                _mark(account, Status.EXHAUSTED, "额度已用完")
                err_resp = JSONResponse({"error": {"message": "当前账号额度已耗尽", "type": "insufficient_quota"}}, status_code=402)
                logs.warn(req_id, f"账号 {account.name} 流首包额度耗尽，自动切换下一个可用账号")
            else:
                account.fail_count += 1
                account.last_error = f"上游流式报错: {first_str[:200]}"
                account.status = Status.COOLING
                account.cooling_until = time.time() + 60
                store.update_account(account)
                err_resp = JSONResponse({"error": {"message": f"上游流式报错: {first_str[:200]}", "type": "upstream_error"}}, status_code=500)
                logs.warn(req_id, f"账号 {account.name} 流首包报错，自动切换下一个可用账号")
            if store.auto_switch():
                return (_NEXT_ACCOUNT, err_resp)
            return err_resp

        # 首包正常：记录指标
        account.use_count += 1
        account.fail_count = 0
        account.last_used_at = time.time()
        if account.status in (Status.COOLING, Status.EXHAUSTED):
            account.status = Status.ACTIVE
            account.cooling_until = None
        store.set_active_account(account.id)
        store.update_account(account)
        asyncio.create_task(_safe_refresh(account))

        async def _iter_with_first():
            if first_chunk:
                yield first_chunk
            async for chunk in aiter:
                yield chunk

        if is_openai:
            class _WrappedResp:
                def aiter_bytes(self):
                    return _iter_with_first()

            async def _openai_stream_wrap():
                try:
                    async for chunk in stream_anthropic_to_openai(_WrappedResp(), chat_id, model_name):
                        yield chunk.encode("utf-8")
                    logs.req_ok(req_id)
                except Exception as err:
                    logs.req_err(req_id, f"OpenAI 流传输中断: {err}")
                finally:
                    await cm.__aexit__(None, None, None)
                    await client.aclose()

            return StreamingResponse(
                _openai_stream_wrap(),
                status_code=status_code,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
            )

        async def _body_iter():
            try:
                async for chunk in _iter_with_first():
                    yield chunk
                logs.req_ok(req_id)
            except Exception as err:  # noqa: BLE001
                logs.req_err(req_id, f"流传输中断: {err}")
            finally:
                await cm.__aexit__(None, None, None)
                await client.aclose()

        out_headers = {"Cache-Control": "no-cache"}
        return StreamingResponse(_body_iter(), status_code=status_code,
                                 media_type=content_type, headers=out_headers)

    # 验证码连续失败
    account.fail_count += 1
    account.last_error = "验证码连续失败"
    account.status = Status.COOLING
    account.cooling_until = time.time() + 60
    store.update_account(account)
    logs.warn(req_id, f"账号 {account.name} 验证码连续失败，自动切换下一个可用账号")
    return _NEXT_ACCOUNT


def _safe_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


async def _safe_refresh(account: Account) -> None:
    try:
        if account.mode == "jwt" or (account.provider == "bigmodel" and account.api_key):
            await fetch_quota(account)
    except Exception:  # noqa: BLE001
        pass
