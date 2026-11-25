"""Agent 核心：事件总线 + 扩展加载 + agent loop。

生命周期（每个事件上挂的函数按加载顺序执行）：
    session_start                         启动时一次
    before_agent_start(system) → system   每个任务开始，可改系统提示词
      turn_start(step)                    每次调用模型前
      tool_call(None, call) → 拒绝原因     执行工具前，返回字符串表示拦截
      tool_result(output, call) → output  执行工具后，可改结果
    agent_end(answer)                     任务结束
"""
import base64
import importlib.util
import mimetypes
import re
import threading
from collections import defaultdict
from pathlib import Path

from devagent import ui
from devagent.bash import BASH_TOOL, run_bash
from devagent.config import BUILTIN_EXTENSIONS, search_dirs
from devagent.mcp import McpManager
from devagent.prompt import build_system_prompt
from devagent.providers import make_provider

def parse_user_content(task, cwd=None):
    if not isinstance(task, str):
        return task
    pattern = r'\[(?:image|图片)\s*:\s*([^\]]+)\]'
    matches = list(re.finditer(pattern, task))
    if not matches:
        return task

    image_blocks = []
    for m in matches:
        raw_path = m.group(1).strip().strip("\"'")
        p = Path(raw_path)
        if not p.is_absolute() and cwd:
            p = Path(cwd) / p
        if p.exists() and p.is_file():
            mime, _ = mimetypes.guess_type(str(p))
            if not mime or not mime.startswith("image/"):
                mime = "image/png"
            try:
                b64 = base64.b64encode(p.read_bytes()).decode("ascii")
                # 内部格式，三种协议各自翻译（见 providers.py 的 to_*_content）
                image_blocks.append({"type": "image", "media_type": mime, "data": b64})
            except Exception:
                pass

    if not image_blocks:
        return task

    clean_text = re.sub(r"[ \t]{2,}", " ", re.sub(pattern, "", task)).strip()
    blocks = [{"type": "text", "text": clean_text or "（用户发送了图片）"}]
    blocks.extend(image_blocks)
    return blocks


MAX_STEPS = 90  # 防止模型陷入死循环；到了上限不直接停，让模型总结后交还给用户
SUMMARY_PROMPT = f"""# 已达到本次任务的最大步数 {MAX_STEPS}
不要再调用任何工具。用文字向用户汇报：
1. 已经完成了什么（改了哪些文件、验证结果如何）
2. 还没完成什么，卡在哪里
3. 你建议的下一步，并询问用户是否继续或换个做法"""


def wait_interruptibly(fn, *args):
    """在另一个线程里跑 fn，自己每 0.1 秒醒一次。
    模型请求会一直阻塞在网络读取上，Ctrl+C 发来的 KeyboardInterrupt 要等它返回才生效；
    换成这样等，中断马上就能生效。被丢下的请求线程是 daemon，结果直接丢弃。"""
    box = {}

    def target():
        try:
            box["value"] = fn(*args)
        except BaseException as e:
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.1)
    if "error" in box:
        raise box["error"]
    return box["value"]


class Agent:
    def __init__(self, config, cwd):
        self.config = config  # api / model / base_url / api_key
        self.cwd = cwd
        self.provider = make_provider(config)
        self.messages = []  # 内部格式的对话历史，见 providers.py
        self.handlers = defaultdict(list)
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}  # 整个会话累计
        self.context_tokens = 0  # 最近一次请求的输入 + 输出，约等于当前上下文大小
        self.mcp = McpManager()

    def switch_model(self, api=None, model=None):
        """换协议或模型：只重建 provider。对话历史是内部格式，不用改。协议名不对时抛 ValueError，原配置不变。"""
        new = dict(self.config)
        if api:
            new["api"] = api
        if model:
            new["model"] = model
        self.provider = make_provider(new)
        self.config = new

    def on(self, event, fn):
        self.handlers[event].append(fn)

    def emit(self, event, value=None, **ctx):
        """按顺序调用挂在 event 上的函数；返回值不是 None 就替换 value 传给下一个（链式修改）。"""
        for fn in self.handlers[event]:
            result = fn(value, **ctx)
            if result is not None:
                value = result
        return value

    def load_extensions(self):
        """清空钩子后重新加载所有扩展，模型新写的扩展不用重启就能生效。"""
        self.handlers.clear()
        for d in [BUILTIN_EXTENSIONS, *search_dirs(self.cwd, "extensions")]:
            for f in sorted(d.glob("*.py")):
                spec = importlib.util.spec_from_file_location(f"devagent_ext_{f.stem}", f)
                if not spec or not spec.loader:
                    continue
                module = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(module)
                    module.register(self)
                except Exception as e:
                    # 模型写坏的扩展不能拖垮整个 Agent，跳过并提示
                    ui.error(f"[扩展 {f.name} 加载失败，已跳过] {e!r}")

    def add_usage(self, usage):
        for k in usage:
            self.usage[k] += usage[k]

    def call_model(self, system):
        tools = [BASH_TOOL, *self.mcp.get_tool_specs()]
        with ui.thinking():
            reply = wait_interruptibly(self.provider.complete, system, self.messages, tools)
        self.add_usage(reply["usage"])
        self.context_tokens = reply["usage"]["input"] + reply["usage"]["output"]
        return reply

    def run_task(self, task):
        """Agent loop：调用模型 → 执行它要的命令 → 把结果喂回去，直到模型不再调用工具。"""
        self.load_extensions()
        system = self.emit("before_agent_start", build_system_prompt(self.cwd))
        user_content = parse_user_content(task, self.cwd)
        self.messages.append({"role": "user", "content": user_content})

        answer = None
        for step in range(MAX_STEPS):
            self.emit("turn_start", step)
            reply = self.call_model(system)
            self.messages.append(reply)
            if not reply["tool_calls"]:
                answer = reply["text"]
                break
            for call in reply["tool_calls"]:
                name = call["name"]
                args = call["args"]
                command = args.get("command", "")
                ui.command(command if name == "bash" else f"{name} {args}")
                reason = self.emit("tool_call", None, call=call)
                try:
                    if reason:
                        output = f"已被拦截:{reason}"
                    elif name == "bash":
                        if not command:
                            output = "缺少command参数"
                        else:
                            output = run_bash(command,self.cwd)
                    elif self.mcp.has_tool(name):
                        output = self.mcp.call_tool(name, args)
                    else:
                        output = f"未知工具:{name}"
                except Exception as e:
                    output = f"工具:{name},执行出错:{e!r},检查参数后重试!"
                
                output = self.emit("tool_result", output, call=call)
                ui.output(output)
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})

        if answer is None:
            answer = self.summarize(system)
        self.emit("agent_end", answer)
        return answer

    def summarize(self, system):
        """步数用完时再调用一次模型，只要文字总结。说明放在 system 末尾而不是追加 user 消息，
        避免和工具结果形成连续两条 user（部分网关要求严格交替）。"""
        reply = self.call_model(system + "\n\n" + SUMMARY_PROMPT)
        # 模型不听话仍然调用了工具：丢掉这些调用，否则历史里会留下没有结果的工具调用，下一轮请求被拒绝
        reply["tool_calls"] = []
        # 模型连总结也没给（返回空文字）：由程序兜底。兜底文字也写进历史，空的 assistant 消息下一轮可能被拒绝
        if not reply["text"]:
            reply["text"] = self.fallback_summary()
        self.messages.append(reply)
        return reply["text"]

    def fallback_summary(self, n=5):
        """列出最后 n 个执行过的操作，至少让用户知道停在了哪里。"""
        calls = [c for m in self.messages if m["role"] == "assistant" for c in m["tool_calls"]][-n:]
        lines = [f"已达到最大步数 {MAX_STEPS}，模型没有给出总结。最后执行的 {len(calls)} 个操作："]
        for c in calls:
            text = c["args"].get("command") or c["name"]
            lines.append("- " + " ".join(text.split())[:100])  # 多行命令压成一行，太长截断
        lines.append("可以回复“继续”让它接着做，或者换个说法重新描述任务。")
        return "\n".join(lines)

    def close_dangling_calls(self):
        """任务被 Ctrl+C 或报错打断时，给还没有结果的工具调用补一个结果，否则下一次请求会因为调用和结果对不上被拒绝。"""
        last = next((m for m in reversed(self.messages) if m["role"] == "assistant"), None)
        if not last:
            return
        done = {m["tool_call_id"] for m in self.messages if m["role"] == "tool"}
        for call in last["tool_calls"]:
            if call["id"] not in done:
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": "（被用户中断，未执行）"})
