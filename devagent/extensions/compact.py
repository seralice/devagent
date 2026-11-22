"""上下文压缩：turn_start 钩子。上下文超过阈值时，把较早的消息交给模型总结成一段摘要；
原文存到 .devagent/history/ 下，摘要里写明路径，模型需要细节时可以自己 grep 找回。"""
import json
import os
import time

from devagent import ui

MAX_CONTEXT = int(os.getenv("DEVAGENT_MAX_CONTEXT", "50000"))  # 上一次请求的输入 + 输出超过这个数就压缩
KEEP_RECENT = 6  # 最近几条消息保留原文

SUMMARY_PROMPT = """把下面的对话记录压缩成一段摘要，后续对话会用这段摘要代替原文。必须保留：
1. 用户的原始要求和后来补充的要求，尽量用原话
2. 具体的值：数字、金额、文件路径、命令、报错信息、ID
3. 已经做完了什么、得出了什么结论、还剩什么没做
如果记录开头已经有一段之前的摘要，把它的内容也合并进来。只输出摘要本身。

对话记录：
"""


def find_cut(messages, keep):
    """返回切点下标 cut：messages[:cut] 拿去总结，messages[cut:] 保留原文。
    先取 len(messages) - keep；如果切点落在 tool 消息上就往前退，直到不是 tool 为止，
    这样 assistant 和它的工具结果总在同一边。没东西可压缩时返回 0。"""
    cut = len(messages) - keep
    while cut > 0 and messages[cut]["role"] == "tool":
        cut -= 1
    return max(cut, 0)


def strip_images(messages):
    """摘要用纯文本：base64 图片塞进去会让总结请求本身爆掉上下文，换成占位符。"""
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            m = dict(m)
            m["content"] = [{"type": "text", "text": f"[图片 {b['media_type']}]"} if b["type"] == "image" else b
                            for b in content]
        out.append(m)
    return out


def summarize(agent, old):
    # 转成纯文本发过去：原始消息里有 tool_use，不带 tools 参数直接发，Anthropic 会拒绝
    text = json.dumps(strip_images(old), ensure_ascii=False)
    reply = agent.provider.complete("你负责压缩对话记录。", [{"role": "user", "content": SUMMARY_PROMPT + text}], [])
    agent.add_usage(reply["usage"])
    return reply["text"]


def archive(agent, old):
    """被压缩掉的原文存成文件，返回路径。"""
    folder = agent.cwd / ".devagent" / "history"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.as_posix()


def register(agent):
    def compact(step):
        if agent.context_tokens <= MAX_CONTEXT:
            return
        cut = find_cut(agent.messages,KEEP_RECENT)
        if cut == 0:
            return
        old = agent.messages[:cut]
        old = agent.messages[:cut]
        path = archive(agent, old)
        summary = summarize(agent, old)
        agent.messages = [{"role": "user", "content": f"上下文已压缩，原会话存储在:{path} 摘要:{summary}"}] + agent.messages[cut:]
        ui.info(f"上下文已压缩：{len(old)} 条消息 → 摘要")



    agent.on("turn_start", compact)
