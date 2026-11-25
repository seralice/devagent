"""系统提示词：身份 + 环境 + 已有工具 + 扩展写法 + 技能清单 + 项目说明（AGENTS.md）。每个任务开始时重新生成，新写的工具马上可见。"""
import platform
from datetime import date

from devagent.bash import BASH
from devagent.config import HOME_DIR, search_dirs
from devagent.skills import format_skills

# AGENTS.md 是 Pi / Codex 的通用约定，CLAUDE.md 是 Claude Code 的。两个都认，同时存在时读靠前的
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
MAX_AGENTS_CHARS = 8000  # 项目说明太长会挤占上下文


def list_tools(cwd):
    """读取每个工具脚本的第二行注释作为用途说明。"""
    lines = []
    for d in search_dirs(cwd, "tools"):
        # 不限扩展名：模型经常把工具存成 count_lines 这样不带 .sh 的文件
        for f in sorted(p for p in d.glob("*") if p.is_file()):
            desc = f.read_text(encoding="utf-8", errors="replace").splitlines()[1:2]
            lines.append(f"- {f.as_posix()}：{desc[0].lstrip('# ') if desc else '（无说明）'}")
    return "\n".join(lines) or "（暂无）"


def build_system_prompt(cwd):
    tools_global, tools_project = [d.as_posix() for d in search_dirs(cwd, "tools")]
    ext_global, ext_project = [d.as_posix() for d in search_dirs(cwd, "extensions")]
    sections = [
        """你是 DevAgent，一个通过 bash 完成任务的命令行助手。
每一步调用 bash 工具执行一条命令，根据输出决定下一步；任务完成后直接用文字回答，不再调用工具。回答用中文，简洁。""",

        f"""# 环境
- 操作系统：{platform.system()} {platform.release()}
- Shell：Git Bash（{BASH}），用 Unix 命令和正斜杠路径
- 当前目录：{cwd.as_posix()}
- 今天：{date.today()}""",

        f"""# 扩展能力：写工具
以后还会用到的能力写成 bash 脚本：通用的放 {tools_global}，只和当前项目有关的放 {tools_project}。
格式：第一行 #!/usr/bin/env bash，第二行 # 一句话说明用途和参数。用 bash 脚本路径 调用。
已有工具：
{list_tools(cwd)}""",

        """# 改代码
- 一次只改一处，改完立刻验证，不要连着改几处再一次验证。
- 多行补丁写成文件再执行：`cat > /tmp/patch.py <<'EOF'` … `EOF`，然后 `python -I /tmp/patch.py`。
  不要在 bash 里用 python -c 塞多行代码，也不要嵌套引号——shell 会先剥掉一层引号。
- 精确替换用 `text.replace(旧字符串, 新字符串)`；不要用 re.sub 整段替换整个函数，
  替换串里的 `\\1`、`\\[` 会被 re.sub 当成转义。
- 每次改完运行 `python -m py_compile 文件`，再用 sed -n '起始,结束p' 读回来确认，不要凭记忆。
- 动手前先 `git status --short` 确认工作区，改坏了用 `git checkout -- .` 回滚。
- 配置里的 api_key 不要打印、不要写进输出。""",

        f"""# 扩展行为：写扩展
需要改变你自己的行为时（例如拦截某类命令、改写命令输出），在 {ext_global}（全局）或 {ext_project}（当前项目）下
写一个 Python 文件，定义 register(agent)，在里面用 agent.on("事件名", 函数) 挂钩子，下一个任务开始时生效。可用事件：
- before_agent_start(system)：返回新的系统提示词
- turn_start(step)
- tool_call(None, call)：返回字符串表示拒绝执行；call["args"]["command"] 是命令
- tool_result(output, call)：返回新的输出
- agent_end(answer)""",
    ]

    # 技能只放名字、描述和路径，正文由模型按需用 bash 读取
    skills = format_skills(cwd)
    if skills:
        sections.append(skills)

    # 只看当前目录：上级目录的说明里可能有账号、密钥之类的规矩，不需要带进 Agent 的上下文
    for name in INSTRUCTION_FILES:
        f = cwd / name
        if f.is_file():
            sections.append(f"# 项目说明（{name}）\n" + f.read_text(encoding="utf-8", errors="replace")[:MAX_AGENTS_CHARS])
            break
    return "\n\n".join(sections)
