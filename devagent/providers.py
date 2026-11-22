"""模型适配层：内部消息格式 ↔ 三种协议（OpenAI Chat Completions / OpenAI Responses / Anthropic Messages）。

主循环只认内部格式，换协议、换模型时历史不用改：
    {"role": "user", "content": "..."}
    {"role": "assistant", "text": "...", "tool_calls": [{"id": "...", "name": "bash", "args": {...}}], "usage": {...}}
    {"role": "tool", "tool_call_id": "...", "content": "..."}
工具定义也用内部格式：{"name": ..., "description": ..., "parameters": JSON Schema}。

每个 Provider 只有一个方法 complete(system, messages, tools)，返回一条内部格式的 assistant 消息。
usage 统一成 4 个数：input（本次请求的全部输入，含缓存部分）、output、cache_read（命中缓存）、cache_write（写入缓存）。
"""
import json


def parse_args(raw):
    # 模型偶尔会输出不合法的 JSON，交给工具去报"缺少参数"，不让整个循环崩掉
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def make_usage(input=0, output=0, cache_read=0, cache_write=0):
    # 各家网关经常把没有的字段返回成 None
    return {"input": input or 0, "output": output or 0, "cache_read": cache_read or 0, "cache_write": cache_write or 0}


# 用户消息的内容有两种：纯字符串，或者 block 列表。block 只有两种，三种协议各自翻译：
#   {"type": "text", "text": "..."}
#   {"type": "image", "media_type": "image/png", "data": "<base64，不含 data: 前缀>"}
# 统一在这里转，各 Provider 不再自己拼图片格式，换协议时才不会出现"某个协议 400"。

def to_chat_content(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content:
        if b["type"] == "text":
            out.append({"type": "text", "text": b["text"]})
        else:
            out.append({"type": "image_url", "image_url": {"url": f"data:{b['media_type']};base64,{b['data']}"}})
    return out


def to_responses_content(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content:
        if b["type"] == "text":
            out.append({"type": "input_text", "text": b["text"]})
        else:
            out.append({"type": "input_image", "image_url": f"data:{b['media_type']};base64,{b['data']}"})
    return out


def to_anthropic_content(content):
    # Anthropic 的消息内容必须永远是 block 列表（cache_control 只能挂在 block 上），字符串也要包一层
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    for b in content:
        if b["type"] == "text":
            out.append({"type": "text", "text": b["text"]})
        else:
            out.append({"type": "image", "source": {"type": "base64", "media_type": b["media_type"], "data": b["data"]}})
    return out


class ChatProvider:
    """OpenAI Chat Completions：system 是第一条消息，工具结果用 role=tool 回填。前缀缓存由服务端自动完成。"""

    def __init__(self, model, base_url, api_key):
        from openai import OpenAI
        self.client, self.model = OpenAI(base_url=base_url, api_key=api_key), model

    def complete(self, system, messages, tools):
        wire = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "assistant":
                calls = [{"id": c["id"], "type": "function",
                          "function": {"name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                         for c in m["tool_calls"]]
                wire.append({"role": "assistant", "content": m["text"] or None, **({"tool_calls": calls} if calls else {})})
            elif m["role"] == "tool":
                wire.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                wire.append({"role": "user", "content": to_chat_content(m["content"])})
        specs = [{"type": "function", "function": t} for t in tools]

        resp = self.client.chat.completions.create(model=self.model, messages=wire, tools=specs)
        reply, u = resp.choices[0].message, resp.usage
        usage = make_usage(u.prompt_tokens, u.completion_tokens,
                           getattr(u.prompt_tokens_details, "cached_tokens", 0)) if u else make_usage()
        return {"role": "assistant", "text": reply.content or "", "usage": usage,
                "tool_calls": [{"id": c.id, "name": c.function.name, "args": parse_args(c.function.arguments)}
                               for c in reply.tool_calls or []]}


class ResponsesProvider:
    """OpenAI Responses：输入是一串 item，工具调用和结果各是独立的 item，用 call_id 对应。"""

    def __init__(self, model, base_url, api_key):
        from openai import OpenAI
        self.client, self.model = OpenAI(base_url=base_url, api_key=api_key), model

    def complete(self, system, messages, tools):
        items = []
        for m in messages:
            if m["role"] == "assistant":
                if m["text"]:
                    items.append({"role": "assistant", "content": m["text"]})
                items += [{"type": "function_call", "call_id": c["id"], "name": c["name"],
                           "arguments": json.dumps(c["args"], ensure_ascii=False)} for c in m["tool_calls"]]
            elif m["role"] == "tool":
                items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
            else:
                items.append({"role": "user", "content": to_responses_content(m["content"])})
        # Responses 的工具定义是扁平的，没有 Chat 那层 "function"
        specs = [{"type": "function", **t} for t in tools]

        # store=False：不依赖服务端保存的会话，每轮都带完整历史，和另外两种协议保持一致
        resp = self.client.responses.create(model=self.model, instructions=system, input=items, tools=specs, store=False)
        u = resp.usage
        usage = make_usage(u.input_tokens, u.output_tokens,
                           getattr(u.input_tokens_details, "cached_tokens", 0)) if u else make_usage()
        calls = [{"id": o.call_id, "name": o.name, "args": parse_args(o.arguments)}
                 for o in resp.output if o.type == "function_call"]
        return {"role": "assistant", "text": resp.output_text or "", "tool_calls": calls, "usage": usage}


class AnthropicProvider:
    """Anthropic Messages：system 单独传，工具调用是 assistant 的 tool_use 块，结果放进 user 消息的 tool_result 块。
    缓存不是自动的，要自己打 cache_control 断点。"""

    def __init__(self, model, base_url, api_key, input_includes_cache=False):
        from anthropic import Anthropic
        # SDK 会自己拼 /v1/messages，三种协议共用一个 base_url 时要去掉结尾的 /v1
        base_url = base_url.rstrip("/").removesuffix("/v1") if base_url else None
        self.client, self.model = Anthropic(base_url=base_url, api_key=api_key), model
        # 官方 API 的 input_tokens 不含缓存部分；部分网关按 OpenAI 口径返回，已经含了。
        # 自动判断会在"新增内容比缓存还多"的那一步误判，所以由配置指定
        self.input_includes_cache = input_includes_cache

    def complete(self, system, messages, tools):
        wire = []
        for m in messages:
            if m["role"] == "assistant":
                blocks = [{"type": "text", "text": m["text"]}] if m["text"] else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]} for c in m["tool_calls"]]
                wire.append({"role": "assistant", "content": blocks})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                # 一轮里的多个工具结果合并进同一条 user 消息。现在的官方 API 会自动合并连续的同角色消息，
                # 但旧版 API 和部分网关要求 user / assistant 严格交替，自己合并在哪都能用
                if wire and wire[-1]["role"] == "user":
                    wire[-1]["content"].append(block)
                else:
                    wire.append({"role": "user", "content": [block]})
            else:
                wire.append({"role": "user", "content": to_anthropic_content(m["content"])})
        specs = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]

        # 两个断点：system（连同前面的 tools）基本不变；最后一条消息之前的历史下一轮会原样再发，打上断点下一轮就能命中
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if wire:
            wire[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}

        resp = self.client.messages.create(model=self.model, system=system_blocks, messages=wire, tools=specs, max_tokens=8192)
        u = resp.usage
        read, write = u.cache_read_input_tokens or 0, u.cache_creation_input_tokens or 0
        # 统一成 OpenAI 口径（input 含缓存部分），命中率才能都用 cache_read / input 算
        total = (u.input_tokens or 0) if self.input_includes_cache else (u.input_tokens or 0) + read + write
        usage = make_usage(total, u.output_tokens, read, write)
        return {"role": "assistant", "usage": usage,
                "text": "".join(b.text for b in resp.content if b.type == "text"),
                "tool_calls": [{"id": b.id, "name": b.name, "args": b.input} for b in resp.content if b.type == "tool_use"]}


PROVIDERS = {"chat": ChatProvider, "responses": ResponsesProvider, "anthropic": AnthropicProvider}


def make_provider(config):
    api = config["api"]
    if api not in PROVIDERS:
        raise ValueError(f"不支持的协议 {api!r}，可选：{', '.join(PROVIDERS)}")
    args = [config["model"], config["base_url"], config["api_key"]]
    if api == "anthropic":
        args.append(config.get("anthropic_input_includes_cache", False))
    return PROVIDERS[api](*args)
