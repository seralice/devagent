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
import itertools
import mimetypes
import re
import threading
from collections import defaultdict
from contextlib import nullcontext
from pathlib import Path

from devagent import plan, swarm, ui
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


# 步数上限只用于无人值守（单任务、管道输入）：交互模式下用户就是监督者，跑偏了按 Ctrl+C，不限步数（和 Claude Code 一样）。
# 到了上限不直接停，让模型总结后交还给用户
MAX_STEPS = 90
SUMMARY_PROMPT = """# 已达到本次任务的最大步数 {max_steps}
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
        self.max_steps = None  # None 表示不限步数；无人值守的入口设成 MAX_STEPS（见 cli.py）
        # 运行时可开关的模式（/plan、/swarm），以及子 Agent（swarm 的 worker / verifier）用到的几项
        self.plan_mode = False    # 计划模式：bash 只放行只读命令
        self.swarm_mode = False   # swarm 模式：工具列表里多一个 swarm
        self.is_worker = False    # 子 Agent：不能再调 swarm、不画转圈
        self.no_hooks = False     # 子 Agent 不触发 before_agent_start，系统提示词由 system_extra 决定
        self.label = ""           # 子 Agent 在自己打印的命令 / 输出前加的名字
        self.system_extra = ""    # 追加到系统提示词末尾（子 Agent 的角色和任务说明）
        self.cancel_event = None  # threading.Event，主 Agent 被中断时置位，子 Agent 看到就停

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

    def check_cancel(self):
        """主 Agent 按了 Ctrl+C 之后，子 Agent 在每个新步骤开始前看到就停，别再发请求、别再执行命令。"""
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise KeyboardInterrupt

    def tool_specs(self):
        """发给模型的工具列表。swarm 模式开着、且不是子 Agent 时多一个 swarm 工具；关掉就消失。"""
        specs = [BASH_TOOL, *self.mcp.get_tool_specs()]
        if self.swarm_mode and not self.is_worker:
            specs.append(swarm.SWARM_TOOL)
        return specs

    def build_system(self):
        """系统提示词 = 基础提示词 → before_agent_start 钩子 → 模式说明 → 子 Agent 的角色说明。
        模式说明在钩子之后拼，这样扩展改提示词不会把模式说明冲掉。"""
        system = build_system_prompt(self.cwd)
        if not self.no_hooks:
            system = self.emit("before_agent_start", system)
        sections = []
        if self.plan_mode:
            sections.append(plan.PROMPT_SECTION)
        if self.swarm_mode and not self.is_worker:
            sections.append(swarm.PROMPT_SECTION)
        if self.system_extra:
            sections.append(self.system_extra)
        return (chr(10) * 2).join([system, *sections]) if sections else system

    def call_model(self, system, allow_tools=True):
        tools = self.tool_specs()
        # 子 Agent 并行跑时共用同一个终端，转圈会互相打架，所以只有主 Agent 画
        with (nullcontext() if self.is_worker else ui.thinking()):
            reply = wait_interruptibly(self.provider.complete, system, self.messages, tools, allow_tools)
        self.add_usage(reply["usage"])
        self.context_tokens = reply["usage"]["input"] + reply["usage"]["output"]
        return reply

    def dispatch(self, call):
        """执行一个工具调用，返回要交给模型的工具结果文字。"""
        name, args = call["name"], call["args"]
        command = args.get("command", "")
        if self.plan_mode and name == "bash":
            # 计划模式的拦截放在钩子之前：这里已经判定过，就不必再让 permission_gate 之类的扩展去问用户
            blocked = plan.violation(command)
            if blocked:
                return f"已被拦截:{blocked}"
        reason = self.emit("tool_call", None, call=call)
        if reason:
            return f"已被拦截:{reason}"
        if name == "bash":
            if not command:
                return "缺少command参数"
            return run_bash(command, self.cwd)
        if name == "swarm" and self.swarm_mode and not self.is_worker:
            return swarm.run(self, args)
        if self.mcp.has_tool(name):
            return self.mcp.call_tool(name, args)
        return f"未知工具:{name}"

    def run_task(self, task):
        """Agent loop：调用模型 → 执行它要的命令 → 把结果喂回去，直到模型不再调用工具。"""
        if not self.is_worker:
            self.load_extensions()  # 子 Agent 用自己的钩子副本（见 swarm.make_child），这里不再重载
        system = self.build_system()
        user_content = parse_user_content(task, self.cwd)
        self.messages.append({"role": "user", "content": user_content})

        answer = None
        for step in itertools.count() if self.max_steps is None else range(self.max_steps):
            self.check_cancel()  # 主 Agent 被 Ctrl+C 后，子 Agent 不再开始新的一步
            self.emit("turn_start", step)
            reply = self.call_model(system)
            self.messages.append(reply)
            if not reply["tool_calls"]:
                answer = reply["text"]
                break
            for call in reply["tool_calls"]:
                name, args = call["name"], call["args"]
                ui.command(args.get("command", "") if name == "bash" else f"{name} {args}", self.label)
                try:
                    output = self.dispatch(call)
                except Exception as e:
                    output = f"工具:{name},执行出错:{e!r},检查参数后重试!"

                output = self.emit("tool_result", output, call=call)
                ui.output(output, self.label)
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})

        if answer is None:
            self.check_cancel()
            answer = self.summarize(system)
        self.emit("agent_end", answer)
        return answer

    def summarize(self, system):
        """步数用完时再调用一次模型，只要文字总结（参考 Claude Code 的压缩请求）：
        system 和工具定义都不变，请求前缀和上一步一样，缓存能命中；总结说明作为一条 user 消息追加在历史末尾；
        工具定义照发，但用 tool_choice 禁止调用。之前把说明拼在 system 末尾、又允许调工具，
        实测模型两次接着调 bash、文字为空，缓存也全部失效。"""
        self.messages.append({"role": "user", "content": SUMMARY_PROMPT.format(max_steps=self.max_steps)})
        reply = self.call_model(system, allow_tools=False)
        # 个别网关不认 tool_choice，模型仍然调了工具：丢掉这些调用，否则历史里会留下没有结果的工具调用，下一轮请求被拒绝
        reply["tool_calls"] = []
        # 模型连总结也没给（返回空文字）：由程序兜底。兜底文字也写进历史，空的 assistant 消息下一轮可能被拒绝
        if not reply["text"]:
            reply["text"] = self.fallback_summary()
        self.messages.append(reply)
        return reply["text"]

    def fallback_summary(self, n=5):
        """列出最后 n 个执行过的操作，至少让用户知道停在了哪里。"""
        calls = [c for m in self.messages if m["role"] == "assistant" for c in m["tool_calls"]][-n:]
        lines = [f"已达到最大步数 {self.max_steps}，模型没有给出总结。最后执行的 {len(calls)} 个操作："]
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
