"""OpenAI Chat Completions 协议与 Anthropic / ZCode 协议双向适配器。

参考 cmdcode2api 理念，使任何使用 OpenAI 标准 SDK / 工具（如 One-API, NextChat,
Cherry Studio, Chatbox, Cursor, LangChain 等）的客户端都能无缝接入。
"""

from __future__ import annotations

import json
import time


def openai_to_anthropic_body(body: dict) -> dict:
    """将 OpenAI 格式的 /v1/chat/completions 请求体转换为 Anthropic /v1/messages 格式。"""
    openai_msgs = body.get("messages") or []
    system_prompts: list[str] = []
    anthropic_msgs: list[dict] = []

    for msg in openai_msgs:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            if isinstance(content, str):
                system_prompts.append(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        system_prompts.append(p.get("text", ""))
        elif role in ("user", "assistant"):
            if isinstance(content, str):
                anthropic_msgs.append({
                    "role": role,
                    "content": [{"type": "text", "text": content}],
                })
            elif isinstance(content, list):
                # 兼容多模态图文输入
                parts = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    ptype = p.get("type")
                    if ptype == "text":
                        parts.append({"type": "text", "text": p.get("text", "")})
                    elif ptype == "image_url":
                        img_info = p.get("image_url") or {}
                        url = img_info.get("url", "")
                        if url.startswith("data:image/"):
                            header, _, data = url.partition(";base64,")
                            media_type = header.replace("data:", "")
                            parts.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type or "image/jpeg",
                                    "data": data,
                                },
                            })
                anthropic_msgs.append({"role": role, "content": parts})

    result = {
        "model": body.get("model", "GLM-5.3"),
        "messages": anthropic_msgs,
        "stream": bool(body.get("stream", False)),
    }
    if system_prompts:
        result["system"] = "\n\n".join(system_prompts)
    if "max_tokens" in body:
        result["max_tokens"] = body["max_tokens"]
    elif "max_completion_tokens" in body:
        result["max_tokens"] = body["max_completion_tokens"]
    else:
        result["max_tokens"] = 4096
    if "temperature" in body:
        result["temperature"] = body["temperature"]
    if "top_p" in body:
        result["top_p"] = body["top_p"]

    return result


def anthropic_to_openai_response(data: dict, model: str, chat_id: str) -> dict:
    """将 Anthropic JSON 响应转换为 OpenAI /v1/chat/completions 响应格式。"""
    content = ""
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            content += block.get("text", "")

    usage = data.get("usage") or {}
    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0

    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop" if data.get("stop_reason") == "end_turn" else data.get("stop_reason", "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def make_openai_chunk(chat_id: str, model: str, delta_text: str | None = None, finish_reason: str | None = None) -> str:
    """构造 OpenAI SSE 流式 chunk。"""
    delta: dict = {}
    if delta_text is not None:
        delta["content"] = delta_text
    chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


async def stream_anthropic_to_openai(resp, chat_id: str, model: str):
    """逐行读取 Anthropic SSE 流并转换为 OpenAI 兼容的 SSE chunk 流。"""
    buffer = ""
    async for chunk in resp.aiter_bytes():
        buffer += chunk.decode("utf-8", "ignore")
        while "\n" in buffer:
            line, _, buffer = buffer.partition("\n")
            line = line.strip()
            if not line.startswith("data:"):
                continue
            raw_data = line[5:].strip()
            if not raw_data or raw_data == "[DONE]":
                continue
            try:
                ev = json.loads(raw_data)
            except Exception:
                continue

            etype = ev.get("type")
            if etype == "content_block_delta":
                delta_text = (ev.get("delta") or {}).get("text", "")
                if delta_text:
                    yield make_openai_chunk(chat_id, model, delta_text=delta_text)
            elif etype == "message_delta":
                stop_r = (ev.get("delta") or {}).get("stop_reason")
                finish_r = "stop" if stop_r == "end_turn" else (stop_r or "stop")
                yield make_openai_chunk(chat_id, model, finish_reason=finish_r)

    yield "data: [DONE]\n\n"

