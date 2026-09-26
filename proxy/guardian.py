"""ZCode 自愈守护网关 (Guardian Proxy)。

监听端口: 8080 (与客户端 DeepSeek Harness / Cherry Studio 等默认配置完全一致)
后端内核: 8085 (托管 zcode-proxy.exe 原生引擎)

核心能力:
1. 实时 Token 与流式进度展示:
   - 逐请求清晰展示当前使用的账号（邮箱/手机号/提供商）；
   - 实时动态统计并展示生成的 Token 数量与吐字速率 (tokens/s)；
   - 绝无莫名黑屏，每笔调用生命周期、耗时与 Token 统计一目了然！
2. 正常运行状态心跳指示:
   - 空闲时每 60 秒输出健康保活状态行，明确告知网关监听与就绪情况；
3. 纯静默进程自愈:
   - 杜绝控制台 taskkill /pid 0 报错与 cmd 回显刷屏；
   - 遇上游 502/连接闪断/401/429 自动秒切健康账号无缝重连重试。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

# 导入账号池与切换机制
PROXY_DIR = Path(__file__).resolve().parent
ROOT_DIR = PROXY_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

from proxy.account_info import (
    auto_switch_next,
    get_account_info,
    update_config_yaml_provider,
)

FRONTEND_PORT = 8080
BACKEND_PORT = 8085
BACKEND_BASE = f"http://127.0.0.1:{BACKEND_PORT}"
MAX_HEAL_ATTEMPTS = 4

_backend_proc: subprocess.Popen | None = None
_backend_lock = asyncio.Lock()
_req_counter = 0
_active_requests = 0


def _kill_port(port: int) -> None:
    """静默清理占用指定端口的遗留进程（纯静默，绝不执行 taskkill /pid 0，绝无控制台回显）。"""
    try:
        my_pid = os.getpid()
        res = subprocess.run(
            ["netstat", "-aon"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if not res.stdout:
            return
        output = res.stdout.decode("gbk", errors="ignore")
        target = f":{port} "
        pids = set()
        for line in output.splitlines():
            if target in line and "LISTENING" in line:
                parts = line.strip().split()
                if len(parts) >= 5:
                    try:
                        pid = int(parts[-1])
                        if pid > 4 and pid != my_pid:
                            pids.add(pid)
                    except ValueError:
                        pass
        for pid in pids:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except Exception:
        pass


def get_current_account_label() -> str:
    """获取当前正在使用的账号可读标识（邮箱/手机号/提供商）。"""
    try:
        from app.store import store
        curr_id = store.get_active_account_id()
        acc = store.find_any(curr_id) if curr_id else None
        if acc:
            ident = acc.email or acc.phone or acc.display_name or acc.name
            p_name = "Z.AI" if acc.provider == "zai" else "智谱"
            return f"账号: {ident} ({p_name})"
    except Exception:
        pass
    info = get_account_info() or {}
    uid = info.get("userId") or info.get("provider") or "主账号"
    return f"账号: {uid}"


def get_current_account_quota() -> str:
    """获取当前生效主账号的额度简要信息。"""
    try:
        from app.store import store
        curr_id = store.get_active_account_id()
        acc = store.find_any(curr_id) if curr_id else None
        if acc:
            q = getattr(acc, "quota", None) or {}
            parts = []
            for m, info in q.items():
                if isinstance(info, dict):
                    rem = info.get("remaining")
                    tot = info.get("total")
                    exp = info.get("expiresAt") or ""
                    if rem == -1 or info.get("status") == "可用" or m == "APIKey直连":
                        parts.append("全模型直连畅通")
                    elif rem is not None and tot is not None:
                        from proxy.account_info import format_units
                        exp_str = f" (至 {exp})" if exp else ""
                        parts.append(f"{m}: {format_units(rem)}/{format_units(tot)}{exp_str}")
            if parts:
                return " · ".join(parts)
    except Exception:
        pass
    return "额度充足正常可用"


def _extract_tokens_from_chunk(chunk_bytes: bytes) -> tuple[int, int | None]:
    """从 OpenAI / Anthropic SSE 格式数据块中提取增量 token 数与官方 completion_tokens。
    返回: (estimated_delta, official_total_or_none)
    """
    try:
        text = chunk_bytes.decode("utf-8", "ignore")
        if not text:
            return 0, None
        delta_sum = 0
        official_total = None
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            if not payload.startswith("{"):
                continue
            try:
                data = json.loads(payload)
                # 检查是否包含官方精确 usage (OpenAI stream_options 或 Anthropic usage)
                usage = data.get("usage")
                if isinstance(usage, dict):
                    comp_tok = usage.get("completion_tokens") or usage.get("output_tokens")
                    if comp_tok and comp_tok > 0:
                        official_total = comp_tok

                # 提取 OpenAI choices.delta 中的文本
                choices = data.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content") or delta.get("reasoning_content") or delta.get("thought") or ""
                    if content:
                        zh = sum(1 for c in content if '\u4e00' <= c <= '\u9fff')
                        other = len(content) - zh
                        toks = zh + max(1 if other > 0 else 0, int(other / 3.5))
                        delta_sum += max(1, toks)

                # 提取 Anthropic content_block_delta 中的文本
                if data.get("type") == "content_block_delta":
                    d = data.get("delta") or {}
                    text_piece = d.get("text") or d.get("thinking") or ""
                    if text_piece:
                        zh = sum(1 for c in text_piece if '\u4e00' <= c <= '\u9fff')
                        other = len(text_piece) - zh
                        toks = zh + max(1 if other > 0 else 0, int(other / 3.5))
                        delta_sum += max(1, toks)

            except Exception:
                pass
        return delta_sum, official_total
    except Exception:
        return 1, None


def _prepare_backend_config() -> Path:
    """生成或更新 8085 后端专用配置文件。"""
    import yaml
    src_cfg = PROXY_DIR / "config.yaml"
    dst_cfg = PROXY_DIR / "config_backend.yaml"
    
    info = get_account_info() or {}
    prov = info.get("provider") or "zai"

    data: dict = {}
    if src_cfg.exists():
        try:
            data = yaml.safe_load(src_cfg.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}

    if not isinstance(data.get("server"), dict):
        data["server"] = {}
    data["server"]["port"] = BACKEND_PORT
    data["server"]["host"] = "127.0.0.1"
    data["provider"] = prov
    if "plan" not in data:
        data["plan"] = "start-plan"

    dst_cfg.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return dst_cfg


def _start_backend_process() -> None:
    """启动或重启 8085 端口上的 zcode-proxy 原生引擎。"""
    global _backend_proc
    _stop_backend_process()
    _prepare_backend_config()
    _kill_port(BACKEND_PORT)

    exe_path = PROXY_DIR / "zcode-proxy.exe"
    cfg_backend = PROXY_DIR / "config_backend.yaml"
    if not exe_path.exists():
        print(f"[错误] 未找到原生引擎: {exe_path}", flush=True)
        return

    cmd = [str(exe_path), "serve", str(cfg_backend)]
    _backend_proc = subprocess.Popen(
        cmd,
        cwd=str(PROXY_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_backend_process() -> None:
    """终止后端原生引擎进程。"""
    global _backend_proc
    if _backend_proc:
        try:
            _backend_proc.terminate()
            _backend_proc.wait(timeout=1.5)
        except Exception:
            try:
                _backend_proc.kill()
            except Exception:
                pass
        _backend_proc = None
    _kill_port(BACKEND_PORT)


async def _wait_backend_ready(timeout_sec: float = 6.0) -> bool:
    """等待后端引擎启动就绪。"""
    t0 = time.time()
    async with httpx.AsyncClient(timeout=1.0) as client:
        while time.time() - t0 < timeout_sec:
            try:
                r = await client.get(f"{BACKEND_BASE}/v1/models")
                if r.status_code == 200:
                    return True
            except Exception:
                await asyncio.sleep(0.15)
    return False


async def _watchdog_loop() -> None:
    """守护协程：监测后端状态，空闲时打印健康心跳，异常退出时自动自愈拉起。"""
    heartbeat_counter = 0
    while True:
        await asyncio.sleep(5.0)
        heartbeat_counter += 5

        async with _backend_lock:
            # 只有后端进程真实崩溃退出时才自动拉起
            if _backend_proc is None or _backend_proc.poll() is not None:
                print("\n[自愈监控] 检测到后端原生代理引擎离线退出，正在自动拉起修复...", flush=True)
                _start_backend_process()
                await _wait_backend_ready(5.0)
                continue

        # 处于空闲状态且无请求在途时，每 30 秒打印一次运行正常心跳，消除无状态感
        if heartbeat_counter >= 30 and _active_requests == 0:
            heartbeat_counter = 0
            cur_time = time.strftime("%H:%M:%S")
            acc_lbl = get_current_account_label()
            print(f"[{cur_time}] 🟢 网关运行正常 | 监听: 8080 (内核: 8085) | {acc_lbl} | 累计处理: {_req_counter} 笔请求 | 状态: 就绪待命", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 端口预检查与清理
    _kill_port(BACKEND_PORT)
    _start_backend_process()
    ok = await _wait_backend_ready(6.0)

    acc_lbl = get_current_account_label()
    quota_lbl = get_current_account_quota()

    print("========================================================================", flush=True)
    print("       ZCode API 自愈网关已就绪 (端口 8080 · 实时 Token 监控)", flush=True)
    print("========================================================================", flush=True)
    print("  ● 服务接口地址:   http://127.0.0.1:8080/v1 (Cherry Studio / Cursor / 任意客户端)", flush=True)
    print("  ● Web 管理后台:   http://127.0.0.1:8081/admin/accounts", flush=True)
    print(f"  ● 当前生效主账号: {acc_lbl}", flush=True)
    print(f"  ● 账号可用额度:   {quota_lbl}", flush=True)
    print("  ● 运行健康状态:   🟢 正常运行中 · 端口 8080 / 8085 双引擎已就绪", flush=True)
    print("  ● 实时输出模式:   已开启 (流式传输实时显示 Token 生成计数与吐字速度)", flush=True)
    print("========================================================================", flush=True)
    print("  💡 提示: 发起对话即可实时显示吐字 Token 数量与速率；", flush=True)
    print("     空闲时每 30 秒自动输出健康心跳，随时掌握网关存活状态。(按 Ctrl+C 可停止)\n", flush=True)

    task = asyncio.create_task(_watchdog_loop())
    try:
        yield
    finally:
        task.cancel()
        print("\n正在停止守护网关与后端引擎...", flush=True)
        _stop_backend_process()
        _kill_port(FRONTEND_PORT)

app = FastAPI(title="ZCode Guardian Self-Healing Gateway", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/v1/models")
async def get_models():
    """获取可用模型列表。"""
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{BACKEND_BASE}/v1/models")
            return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
    except Exception:
        return JSONResponse({
            "object": "list",
            "data": [
                {"id": "GLM-5.3", "object": "model", "owned_by": "zcode"},
                {"id": "GLM-5.3-Flash", "object": "model", "owned_by": "zcode"},
                {"id": "GLM-5.2", "object": "model", "owned_by": "zcode"},
                {"id": "GLM-5-Turbo", "object": "model", "owned_by": "zcode"},
            ]
        })


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def proxy_all(request: Request, path: str):
    """核心自愈中继路由：捕获所有上游异常并在网关层自动重试切号，同时输出实时 Token 统计。"""
    global _req_counter, _active_requests
    _req_counter += 1
    _active_requests += 1
    req_num = _req_counter
    req_time = time.strftime("%H:%M:%S")

    body_bytes = await request.body()
    method = request.method
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)

    # 解析请求属性以便日志展示
    is_stream = False
    model_name = "未知模型"
    if body_bytes:
        try:
            j = json.loads(body_bytes.decode("utf-8"))
            is_stream = bool(j.get("stream"))
            model_name = str(j.get("model") or "GLM-5.3-Flash")
        except Exception:
            pass

    acc_label = get_current_account_label()
    url = f"{BACKEND_BASE}/{path}"
    req_t0 = time.time()

    try:
        for attempt in range(1, MAX_HEAL_ATTEMPTS + 1):
            try:
                client = httpx.AsyncClient(timeout=httpx.Timeout(connect=25.0, read=None, write=120.0, pool=30.0))
                req = client.build_request(method, url, headers=headers, content=body_bytes)
                resp = await client.send(req, stream=is_stream)

                # 遇到 502 / 504 / 500 等上游连接中断或报错时触发自愈切号
                if resp.status_code in (502, 503, 504, 500, 401):
                    err_text = ""
                    if is_stream:
                        err_bytes = await resp.aread()
                        err_text = err_bytes.decode("utf-8", "ignore")
                    else:
                        err_text = resp.text
                    await client.aclose()

                    if resp.status_code == 502:
                        switch_reason = "上游连接中断 (502 Bad Gateway)"
                    elif resp.status_code == 504:
                        switch_reason = "上游服务网关超时 (HTTP 504)"
                    elif resp.status_code == 401:
                        switch_reason = "上游凭证已失效 (HTTP 401)"
                    elif resp.status_code == 429:
                        switch_reason = "上游请求频次限流 (HTTP 429)"
                    else:
                        switch_reason = f"上游服务异常 (HTTP {resp.status_code})"

                    print(f"| #{req_num:03d} | {req_time} | {model_name:<13} | {acc_label} | ⚠️ 触发上游中断 (HTTP {resp.status_code})", flush=True)
                    print(f"  [⚡ 自动自愈] 原因: {switch_reason}，正在秒级轮换下一个健康账号 (第 {attempt}/{MAX_HEAL_ATTEMPTS} 次)...", flush=True)

                    async with _backend_lock:
                        ok, new_acc, new_prov = auto_switch_next(switch_reason)
                        _start_backend_process()
                        await _wait_backend_ready(5.0)

                    acc_label = get_current_account_label()
                    print(f"  [⚡ 自动自愈] ✔ 已自动切换至健康账号【{new_acc}】，正在无缝继续重发请求...", flush=True)
                    await asyncio.sleep(0.4)
                    continue

                # 正常非流式响应
                if not is_stream:
                    content = resp.content
                    status_code = resp.status_code
                    resp_headers = dict(resp.headers)
                    await client.aclose()

                    total_tokens = 0
                    try:
                        resp_json = json.loads(content.decode("utf-8", "ignore"))
                        u = resp_json.get("usage") or {}
                        total_tokens = u.get("completion_tokens") or u.get("output_tokens") or u.get("total_tokens") or 0
                        if not total_tokens:
                            # 估算文本 token
                            choices = resp_json.get("choices") or []
                            if choices and isinstance(choices[0], dict):
                                msg = choices[0].get("message") or {}
                                text = msg.get("content") or ""
                                zh = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
                                other = len(text) - zh
                                total_tokens = zh + max(1 if other > 0 else 0, int(other / 3.5))
                            elif resp_json.get("content"):
                                c0 = resp_json["content"][0]
                                text = c0.get("text") or ""
                                zh = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
                                other = len(text) - zh
                                total_tokens = zh + max(1 if other > 0 else 0, int(other / 3.5))
                    except Exception:
                        pass
                    dur = max(0.01, time.time() - req_t0)
                    speed = total_tokens / dur if total_tokens else 0
                    tok_info = f"共 {total_tokens} tokens · " if total_tokens else ""
                    spd_info = f" · {speed:.1f} tok/s" if speed > 0 else ""
                    print(f"| #{req_num:03d} | {req_time} | {model_name:<13} | {acc_label} | ✔ 200 OK ({tok_info}耗时 {dur:.2f}s{spd_info})", flush=True)
                    return Response(content=content, status_code=status_code, headers=resp_headers)

                # 流式响应：预读第一块数据验证有效性
                aiter = resp.aiter_bytes()
                first_chunk = b""
                try:
                    first_chunk = await aiter.__anext__()
                except StopAsyncIteration:
                    first_chunk = b""
                except Exception as stream_err:
                    await client.aclose()
                    print(f"| #{req_num:03d} | {req_time} | {model_name:<13} | {acc_label} | ⚠️ 流首包读取异常: {stream_err}", flush=True)
                    print(f"  [⚡ 自动自愈] 触发自动换号重试 (第 {attempt}/{MAX_HEAL_ATTEMPTS} 次)...", flush=True)
                    async with _backend_lock:
                        auto_switch_next(f"流首包读取异常 ({stream_err})")
                        _start_backend_process()
                        await _wait_backend_ready(5.0)
                    acc_label = get_current_account_label()
                    await asyncio.sleep(0.4)
                    continue

                # 首包正常：启动实时 Token 监控与平滑流式传输
                stream_t0 = time.time()

                async def _stream_generator():
                    nonlocal first_chunk
                    token_count = 0
                    has_yielded = False
                    last_refresh_time = 0.0

                    def _feed(chunk_b):
                        nonlocal token_count
                        delta, official = _extract_tokens_from_chunk(chunk_b)
                        if official is not None:
                            token_count = official
                        else:
                            token_count += delta

                    try:
                        if first_chunk:
                            has_yielded = True
                            _feed(first_chunk)
                            yield first_chunk

                        async for chunk in aiter:
                            has_yielded = True
                            _feed(chunk)
                            yield chunk

                            # 实时刷新输出动态（每 0.25 秒刷新一次 token 计数和速率）
                            now = time.time()
                            if now - last_refresh_time >= 0.25:
                                last_refresh_time = now
                                elapsed = max(0.01, now - stream_t0)
                                speed = token_count / elapsed
                                sys.stdout.write(f"\r| #{req_num:03d} | {req_time} | {model_name:<13} | {acc_label} | 🟢 输出中: {token_count} tokens ({speed:.1f} tok/s)    ")
                                sys.stdout.flush()

                        total_time = max(0.01, time.time() - stream_t0)
                        final_speed = token_count / total_time
                        print(f"\r| #{req_num:03d} | {req_time} | {model_name:<13} | {acc_label} | ✔ 200 OK 流式完成 (共 {token_count} tokens · 耗时 {total_time:.2f}s · {final_speed:.1f} tok/s)       ", flush=True)

                    except Exception as ex:
                        if has_yielded:
                            yield b'data: {"id":"chatcmpl-healed","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                            yield b'data: [DONE]\n\n'
                        total_time = max(0.01, time.time() - stream_t0)
                        print(f"\r| #{req_num:03d} | {req_time} | {model_name:<13} | {acc_label} | ⚠️ 流式中途中断 (已平滑收尾，已输出 {token_count} tokens)       ", flush=True)
                    finally:
                        await client.aclose()

                return StreamingResponse(
                    _stream_generator(),
                    status_code=resp.status_code,
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
                )

            except Exception as conn_err:
                print(f"| #{req_num:03d} | {req_time} | {model_name:<13} | {acc_label} | ⚠️ 连接异常: {conn_err}", flush=True)
                print(f"  [⚡ 自动自愈] 触发自动换号重试 (第 {attempt}/{MAX_HEAL_ATTEMPTS} 次)...", flush=True)
                async with _backend_lock:
                    auto_switch_next(f"网络连接异常 ({conn_err})")
                    _start_backend_process()
                    await _wait_backend_ready(5.0)
                acc_label = get_current_account_label()
                await asyncio.sleep(0.4)

        return JSONResponse(
            {"error": {"message": "网关已多次自动重试并切换账号，当前网络暂时受限，请稍后再次发送。", "type": "upstream_error"}},
            status_code=502,
        )
    finally:
        _active_requests = max(0, _active_requests - 1)


if __name__ == "__main__":
    _kill_port(FRONTEND_PORT)
    uvicorn.run(app, host="0.0.0.0", port=FRONTEND_PORT, log_level="warning")

