"""唯一的工具：bash。"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT = 60  # 单条命令超时秒数

BASH_TOOL = {
    "name": "bash",
    "description": "在当前目录执行一条 bash 命令，返回退出码和输出",
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "要执行的 bash 命令"}},
        "required": ["command"],
    },
}


def find_bash():
    """Windows 上直接写 "bash" 会先找到 System32 里的 WSL bash，命令跑进 Linux 子系统，所以要显式用 Git Bash。"""
    if os.getenv("DEVAGENT_BASH"):
        return os.getenv("DEVAGENT_BASH")
    if os.name != "nt":
        return "bash"
    # git.exe 可能在 Git/cmd 或 Git/mingw64/bin 下，往上找到 Git 安装目录；用 bin/bash.exe 而不是 usr/bin 的，它会配好 PATH
    git = shutil.which("git")
    for parent in Path(git).resolve().parents if git else []:
        if (parent / "bin" / "bash.exe").exists():
            return str(parent / "bin" / "bash.exe")
    sys.exit("没找到 Git Bash，请安装 Git for Windows，或用 DEVAGENT_BASH 指定 bash.exe 路径")


BASH = find_bash()


def kill_tree(p):
    """Windows 上 kill 只杀 bash.exe，它启动的子进程（如 sleep、python）会留在后台，要连进程树一起杀。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        p.kill()


def run_bash(command, cwd):
    p = subprocess.Popen([BASH, "-c", command], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace")
    deadline = time.monotonic() + TIMEOUT
    try:
        # 每 0.1 秒回到 Python 一次，Ctrl+C 塞进来的 KeyboardInterrupt 才能马上生效（一直阻塞在 C 里会等到命令结束）
        while True:
            try:
                out, err = p.communicate(timeout=0.1)
                return f"exit={p.returncode}\n{out}{err}"
            except subprocess.TimeoutExpired:
                if time.monotonic() > deadline:
                    kill_tree(p)
                    p.communicate()
                    return f"命令超过 {TIMEOUT} 秒未结束，已终止"
    except BaseException:
        kill_tree(p)  # 被中断时把命令也杀掉，不留在后台继续跑
        raise
