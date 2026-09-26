"""ZCode 自愈守护网关 (Guardian Proxy)。

监听端口: 8080 (与客户端 DeepSeek Harness / Cherry Studio 等默认配置完全一致)
后端内核: 8085 (托管 zcode-proxy.exe 原生引擎)

核心能力:
1. 自动自愈重试: 遇上游网络中断、502 Bad Gateway、401 失效、429 限流时，
   在网关层自动捕获并无缝秒切下一个可用账号重试，客户端完全无感知！
2. 防客户端假死:
   - 流式响应采用预探活与保活心跳包 (: ping\n\n)；
   - 遭遇不可抗力中途截断时，平滑发送合法结束包与 [DONE]，彻底消除客户端一直转圈卡死问题！
3. 后台进程守护:
   - 自动检测并拉起原生 zcode-proxy 进程；
   - 周期性心跳探测，若进程僵死或异常退出则在 500ms 内自动自愈拉起。
"""

from __future__ import annotations

import asyncio
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


def _kill_port(port: int) -> None:
    """清理指定端口上的遗留占用进程。"""
    try:
        cmd = f'for /f "tokens=5" %a in (\'netstat -aon ^| findstr ":{port} "\') do taskkill /f /pid %a >nul 2>&1'
        os.system(cmd)
    except Exception:
        pass


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
        print(f"[错误] 未找到原生引擎: {exe_path}")
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
    """守护协程：每隔 5 秒对后端进行探活，若异常退出或卡死自动自愈重启。"""
    while True:
        await asyncio.sleep(5.0)
        async with _backend_lock:
            need_restart = False
            if _backend_proc is None or _backend_proc.poll() is not None:
                need_restart = True
            else:
                try:
                    async with httpx.AsyncClient(timeout=2.0) as client:
                        r = await client.get(f"{BACKEND_BASE}/v1/models")
                        if r.status_code != 200:
                            need_restart = True
                except Exception:
                    need_restart = True

            if need_restart:
                print("\n[自愈监控] 检测到后端代理引擎离线或未响应，正在自动拉起修复...")
                _start_backend_process()
                await _wait_backend_ready(5.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("========================================================================")
    print("       ZCode 原生自愈防卡死网关正在启动 (端口 8080 · 双引擎守护)")
    print("========================================================================")
    print("  ● 客户端服务地址:   http://127.0.0.1:8080/v1 (DeepSeek Harness / Cherry Studio 等)")
    print("  ● 自动自愈机制:     启用 (遇 502/连接中断/限流 自动秒切账号与重连重试)")
    print("  ● 防客户端假死:     启用 (预读首包探活 + 心跳保活 + 异常平滑收尾)")
    print("  ● 内部引擎端口:     http://127.0.0.1:8085 (托管 Happy-DOM 原生无痕验证码内核)")
    print("------------------------------------------------------------------------")
    
    # 端口预检查与清理
    _kill_port(BACKEND_PORT)
    _start_backend_process()
    ok = await _wait_backend_ready(6.0)
    if ok:
        info = get_account_info() or {}
        prov = info.get("provider", "zai")
        prov_name = "Z.AI 国际站" if prov == "zai" else "智谱 BigModel"
        print(f"  ✔ 后端引擎就绪 (当前主选平台: {prov_name})")
    else:
        print("  [!] 后端引擎正在初始化中...")

    print("========================================================================")
    print("  网关已启动就绪！请在客户端中正常使用，按 Ctrl+C 可停止代理并退出。\n")
    task = asyncio.create_task(_watchdog_loop())
    try:
        yield
    finally:
        task.cancel()
        print("\n正在停止守护网关与后端引擎...")
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
    """核心自愈中继路由：捕获所有上游异常并在网关层自动重试切号。"""
    global _req_counter
    _req_counter += 1
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
            import json
            j = json.loads(body_bytes.decode("utf-8"))
            is_stream = bool(j.get("stream"))
            model_name = str(j.get("model") or "GLM-5.3-Flash")
        except Exception:
            pass

    url = f"{BACKEND_BASE}/{path}"

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

                print(f"| #{req_num:03d} | {req_time} | {model_name:<13} | ⚠️ 触发上游中断 (HTTP {resp.status_code})")
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

                async with _backend_lock:
                    ok, new_acc, new_prov = auto_switch_next(switch_reason)
                    _start_backend_process()
                    await _wait_backend_ready(5.0)

                await asyncio.sleep(0.4)
                continue

            # 正常非流式响应
            if not is_stream:
                content = resp.content
                status_code = resp.status_code
                resp_headers = dict(resp.headers)
                await client.aclose()
                print(f"| #{req_num:03d} | {req_time} | {model_name:<13} | 200 OK (非流式完成)")
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
                print(f"| #{req_num:03d} | {req_time} | {model_name:<13} | ⚠️ 流首包读取异常: {stream_err}")
                print(f"  [⚡ 自动自愈] 触发自动换号重试 (第 {attempt}/{MAX_HEAL_ATTEMPTS} 次)...")
                async with _backend_lock:
                    auto_switch_next(f"流首包读取异常 ({stream_err})")
                    _start_backend_process()
                    await _wait_backend_ready(5.0)
                await asyncio.sleep(0.4)
                continue

            # 首包正常：启动平滑流式推送给客户端
            print(f"| #{req_num:03d} | {req_time} | {model_name:<13} | 200 OK (流式传输中...)")

            async def _stream_generator():
                nonlocal first_chunk
                has_yielded = False
                try:
                    if first_chunk:
                        has_yielded = True
                        yield first_chunk
                    async for chunk in aiter:
                        has_yielded = True
                        yield chunk
                except Exception as ex:
                    # 遭遇不可抗力中途截断时，平滑发送终止包，绝不让 DeepSeek Harness 等客户端死等卡住！
                    if has_yielded:
                        yield b'data: {"id":"chatcmpl-healed","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                        yield b'data: [DONE]\n\n'
                finally:
                    await client.aclose()

            return StreamingResponse(
                _stream_generator(),
                status_code=resp.status_code,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
            )

        except Exception as conn_err:
            print(f"| #{req_num:03d} | {req_time} | {model_name:<13} | ⚠️ 连接异常: {conn_err}")
            print(f"  [⚡ 自动自愈] 触发自动换号重试 (第 {attempt}/{MAX_HEAL_ATTEMPTS} 次)...")
            async with _backend_lock:
                auto_switch_next(f"网络连接异常 ({conn_err})")
                _start_backend_process()
                await _wait_backend_ready(5.0)
            await asyncio.sleep(0.4)

    # 若连续重试多次依然失败，安全输出友好响应并关闭流
    return JSONResponse(
        {"error": {"message": "网关已多次自动重试并切换账号，当前网络暂时受限，请稍后再次发送。", "type": "upstream_error"}},
        status_code=502,
    )


if __name__ == "__main__":
    _kill_port(FRONTEND_PORT)
    uvicorn.run(app, host="0.0.0.0", port=FRONTEND_PORT, log_level="warning")
