"""命令行入口：devagent 进入交互模式；devagent "任务" 只执行一个任务就退出。在哪个目录启动，就在哪个目录干活。"""
import sys
import time
from pathlib import Path

from devagent import ui
from devagent.agent import Agent
from devagent.commands import run_command
from devagent.config import HOME_DIR, load_config


def run_one(agent, task):
    before, start = dict(agent.usage), time.monotonic()
    try:
        ui.answer(agent.run_task(task))
    except KeyboardInterrupt:
        agent.close_dangling_calls()
        ui.error("已中断")
    except Exception as e:
        agent.close_dangling_calls()
        ui.error(f"出错：{e!r}")
    ui.status({key: agent.usage[key] - before[key] for key in before}, time.monotonic() - start, agent.config["model"])


def main():
    config = load_config()
    if not config["api_key"]:
        sys.exit(f"没有配置 API key：设置环境变量 DEVAGENT_API_KEY，或写进 {HOME_DIR / 'config.json'}")
    for d in ["tools", "extensions"]:
        (HOME_DIR / d).mkdir(parents=True, exist_ok=True)

    agent = Agent(config, Path.cwd())
    agent.mcp.load_from_config(config.get("mcp_servers", {}))
    agent.load_extensions()
    agent.emit("session_start")

    if len(sys.argv) > 1:
        run_one(agent, " ".join(sys.argv[1:]))
        return

    if sys.stdin.isatty():
        ui.clear()
    ui.banner(agent)
    if sys.stdin.isatty():
        from devagent.repl import Repl
        Repl(agent, run_one).run()
    else:
        read_piped(agent)


def read_piped(agent):
    """输入被管道重定向时（如测试）没有终端，用不了输入框，一行一行读。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        ui.user(line)
        if not line.startswith("/"):
            run_one(agent, line)
            continue
        try:
            run_command(agent, line)
        except SystemExit:
            break


if __name__ == "__main__":
    main()
