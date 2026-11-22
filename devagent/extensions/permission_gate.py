"""危险命令确认：tool_call 钩子的示范。命中模式的命令先问用户，拒绝就返回原因，Agent 不会执行。"""
import re

from devagent import ui

DANGEROUS = re.compile(r"\brm\s+-\w*[rf]|\bsudo\b|\bmkfs\b|\bdd\s+if=|\bshutdown\b|\breboot\b|>\s*/dev/sd|\bgit\s+push\b")


def check(_, call):
    command = call["args"].get("command", "")
    if DANGEROUS.search(command) and not ui.confirm(f"[危险命令] {command}\n确认执行？"):
        return "用户拒绝执行该命令"
    return None


def register(agent):
    agent.on("tool_call", check)
