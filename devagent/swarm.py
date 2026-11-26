"""Swarm 模式：主 Agent 把任务拆给多个子 Agent 并行做，汇报拼成一块"共享看板"。

每个 worker 是一个新建的 Agent：自己的 messages（看不到主 Agent 的对话）、自己的 provider、
步数上限比主 Agent 小，拿不到 swarm 工具（不能套娃）。
worker 和主 Agent 一样加载扩展：危险命令确认、长输出截断、上下文压缩都要生效。

三个和并发 / 中断有关的细节：
- 确认提示：几个 worker 可能同时要求确认，终端只有一个。所有确认都走 ui.CONFIRM_LOCK，排队一个一个问。
- Ctrl+C：主 Agent 被中断时，这里给每个还没结束的 worker 线程抛 KeyboardInterrupt（和 repl.py 同一套办法），
  worker 由此在手上这一步内停下（正在跑的 bash 命令一起被杀掉）；cancel 事件让还没开始下一步的 worker 直接退出。
  下一次 swarm 用新的事件，不受影响。
- 命令和输出前带 worker 名字，并行打印时能分清是谁。
"""
import json
import threading
import time

from devagent import ui

WORKER_MAX_STEPS = 30    # 必须比 agent.MAX_STEPS 小，免得子 Agent 比主 Agent 还能耗
VERIFIER_MAX_STEPS = 20
MAX_WORKERS = 8          # 一次最多几个 worker，防止模型一口气开几十个把网关打爆

SWARM_TOOL = {
    "name": "swarm",
    "description": ("把任务拆给多个互相独立的子 Agent（worker）并行完成，它们看不到当前对话，"
                    "只能看到你写的 brief。全部 worker 结束后，它们的汇报会拼成一块共享看板返回给你。"),
    "parameters": {
        "type": "object",
        "properties": {
            "workers": {
                "type": "array",
                "description": "要并行跑的 worker，每项有 name（短名字，用于看板）和 brief（任务说明）",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "worker 的名字，例如 auth-reader"},
                        "brief": {"type": "string", "description": "这个 worker 负责什么、要交什么、怎样算完成"},
                    },
                    "required": ["name", "brief"],
                },
            },
            "verifier": {
                "type": "string",
                "description": ("可选。所有 worker 完成后，由一个检查者按这段说明检查它们的汇报，"
                                "例如要交叉验证的结论、要对照的验收标准"),
            },
        },
        "required": ["workers"],
    },
}

PROMPT_SECTION = """# Swarm 模式（可以开多个子 Agent 并行干活）
你现在有 swarm 工具，它开着。用法：
- 什么时候该拆：任务里有几个互不依赖的部分（例如同时读几个模块、同时调研几种方案、分别写不同文件），
  或者每个部分都很耗步数。彼此有先后依赖的步骤不要拆，自己顺序做。
- brief 怎么写：写清楚这个 worker 负责什么、要读/改哪些文件、要交出什么（具体文件路径或结论格式）、
  怎样算完成。worker 看不到你的对话，brief 里必须自带全部背景，不要用"上面说的"这类指代。
- 每个 worker 都在同一个当前目录里干活，并行运行。让它们的改动不要互相覆盖（一个文件只交给一个 worker）。
- 全部 worker 结束后你可以拿到一块共享看板（每个 worker 的汇报）。可选 verifier：
  让一个检查者按你写的说明检查所有汇报，适合需要交叉验证、对账的时候。
- 拿到看板后自己汇总、验证（例如自己跑一次测试），再给用户最终回答；不要直接把看板原样贴给用户。"""

WORKER_SECTION = """# 你是一个子 Agent（worker）
你被主 Agent 拆分出来独立完成一件小事，看不到主 Agent 的对话，只有下面这段任务说明。
- 只做 brief 里交给你的部分，不要扩大范围，不要动别的 worker 负责的文件。
- 做完后直接用文字汇报：改了什么（文件路径）、验证结果、结论或关键发现、没做完的地方。
  汇报会进入共享看板给主 Agent 看，要具体，不要只说"已完成"。
- 没有 swarm 工具，不能再拆分子任务。"""


def parse_workers(args):
    """把参数还原成 (workers, verifier, error)。workers 是 [(name, brief)]。
    workers 可能是数组，也可能是被序列化成 JSON 字符串的数组（模型常这么传），两种都认。"""
    if not isinstance(args, dict):
        return None, None, f"swarm 参数必须是对象，收到 {type(args).__name__}"
    raw = args.get("workers")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, None, "workers 不能为空，请传数组，每项有 name 和 brief"
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            return None, None, f"workers 是字符串但不是合法 JSON（{e}），请直接传数组"
    if not isinstance(raw, list) or not raw:
        return None, None, "workers 必须是非空数组，每项有 name 和 brief"

    workers = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return None, None, f"workers 第 {i} 项必须是对象（有 name 和 brief）"
        brief = item.get("brief")
        if not isinstance(brief, str) or not brief.strip():
            return None, None, f"workers 第 {i} 项缺少 brief（任务说明）"
        workers.append((str(item.get("name") or f"worker{i}"), brief.strip()))
    if len(workers) > MAX_WORKERS:
        return None, None, f"一次最多 {MAX_WORKERS} 个 worker，收到 {len(workers)} 个，请合并一些任务"

    verifier = args.get("verifier")
    verifier = verifier.strip() if isinstance(verifier, str) else None
    return workers, verifier or None, None


def make_child(agent, label, extra_system, max_steps, cancel):
    """子 Agent：同一配置、同一目录、全新对话；步数上限更小、不能再开 swarm、不画转圈。
    扩展照常加载（危险命令确认、输出截断、上下文压缩对它同样生效），只是不触发 before_agent_start，
    这样它的系统提示词由这里拼（基础提示词 + worker 说明），不会被别的扩展改乱。"""
    from devagent.agent import Agent

    child = Agent(dict(agent.config), agent.cwd)
    child.is_worker = True
    child.label = label
    child.max_steps = max_steps
    child.system_extra = extra_system
    child.no_hooks = True           # 不触发 before_agent_start，system 由 system_extra 决定
    child.cancel_event = cancel     # 主 Agent 被中断时置位，worker 看到就退出
    child.plan_mode = agent.plan_mode  # 主 Agent 在计划模式下时，worker 也只能只读
    child.mcp = agent.mcp           # 共享已启动的 MCP 连接，不重复拉起 Server（请求按 id 分发的，可以并发）
    child.load_extensions()         # 和主 Agent 一样加载扩展：危险命令确认、输出截断、上下文压缩都要生效
    return child


def interrupt_threads(threads, finished, cancel, timeout=3.0):
    """置位 cancel 并给还在跑的线程抛 KeyboardInterrupt，让他们在手上这一步内停下。
    和 repl.py 的做法一致：线程每 0.1 秒回一次 Python，异常马上就能生效。

    这里不用 Thread.is_alive()：主线程是在 t.join(0.1) 里被中断的，CPython 的线程状态锁会留下错误结果，
    之后 is_alive() 可能对着还在跑的线程返回 False，异常就漏发了（漏发的那条命令会在后台跑完，连写出的文件都留下）。
    所以由每个 worker 在自己结束时置位一个 Event（finally 里），这里只看 Event。"""
    import ctypes

    cancel.set()
    for t, done in zip(threads, finished):
        if t.ident is not None and not done.is_set():
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(t.ident), ctypes.py_object(KeyboardInterrupt))
    deadline = time.monotonic() + timeout
    for done in finished:
        while not done.is_set() and time.monotonic() < deadline:
            done.wait(0.1)


def wait_all(finished):
    """和 wait_interruptibly 一样每 0.1 秒回一次 Python，Ctrl+C 才能马上生效。
    等的是 worker 自己置位的 Event，不走 Thread.join：join 被中断会留下错误结果（见 interrupt_threads）。"""
    for done in finished:
        while not done.is_set():
            done.wait(0.1)


def run_in_threads(agent, jobs, cancel):
    """jobs 是 [(label, extra_system, max_steps, task, name)]，并行跑，返回 [(name, status, answer)]。
    Ctrl+C 时把所有 worker 停下再抛出，保证中断后不留后台线程。"""
    results = [None] * len(jobs)
    lock = threading.Lock()
    finished = [threading.Event() for _ in jobs]  # 每个 worker 结束时置位；中断时不看 is_alive（原因见 interrupt_threads）

    def work(index, label, extra_system, max_steps, task, name):
        try:
            child = make_child(agent, label, extra_system, max_steps, cancel)
            try:
                answer = child.run_task(task)
                status = "完成" if answer else "完成（无文字汇报）"
            finally:
                with lock:
                    agent.add_usage(child.usage)  # worker 的花费算进会话总量，状态行才准
        except BaseException as e:  # 单个 worker 出错不能拖垮整次 swarm
            if isinstance(e, KeyboardInterrupt):
                answer, status = "（已中断，未完成）", "中断"
            else:
                answer, status = f"（执行出错：{e!r}）", "出错"
        finally:
            with lock:
                results[index] = (name, status, answer or "")
                finished[index].set()  # 先记下结果再置位，这样置位后 results[index] 一定已经写好

    threads = [threading.Thread(target=work, args=(i, *job), daemon=True) for i, job in enumerate(jobs)]
    try:
        for t in threads:
            t.start()
        wait_all(finished)
    except BaseException:
        interrupt_threads(threads, finished, cancel)
        raise
    return results


def run(agent, args):
    """执行一次 swarm 调用，返回给主 Agent 的共享看板文本。"""
    workers, verifier, error = parse_workers(args)
    if error:
        return f"swarm 参数有误：{error}"

    total = len(workers)
    ui.info(f"swarm：并行启动 {total} 个 worker" + ("，完成后由 verifier 检查" if verifier else ""))
    cancel = threading.Event()  # 每次调用一个新事件，上一条被中断的 swarm 不会影响这一次

    jobs = [(f"{i + 1}/{total} {name}", WORKER_SECTION, WORKER_MAX_STEPS, brief, name)
            for i, (name, brief) in enumerate(workers)]
    results = run_in_threads(agent, jobs, cancel)

    lines = ["# 共享看板", ""]
    for i, (name, status, answer) in enumerate(results, 1):
        lines += [f"## worker {i}：{name}", f"状态：{status}", answer, ""]

    if verifier:
        board = "\n".join(lines)
        task = (f"以下是其他 worker 的汇报（共享看板）。请按下面的检查说明核对它们，"
                f"指出问题、遗漏和结论不一致的地方，最后给出你的判断。\n\n"
                f"# 检查说明\n{verifier}\n\n{board}")
        (name, status, verdict), = run_in_threads(
            agent, [("verifier", WORKER_SECTION, VERIFIER_MAX_STEPS, task, "verifier")], cancel)
        lines += [f"## verifier", f"状态：{status}", verdict]

    return "\n".join(lines).rstrip() + "\n"
