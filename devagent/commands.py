"""斜杠命令：以 / 开头的输入不发给模型，由这里处理。每个命令是一个函数 fn(agent, arg)，arg 是命令后面的文字。"""
from devagent import ui
from devagent.prompt import build_system_prompt
from devagent.providers import PROVIDERS


# ↓ 示范：每个命令是一个函数 (agent, arg)，arg 是命令后面的文字

def cmd_help(agent, arg):
    for name, (_, desc) in COMMANDS.items():
        ui.plain(f"{name:<10}{desc}")


def cmd_system(agent, arg):
    ui.plain(build_system_prompt(agent.cwd))


def cmd_exit(agent, arg):
    raise SystemExit


# 切换模型 / 协议、查看用量、清空上下文

def cmd_model(agent, arg):
    # 不带参数时显示当前协议和模型，否则切换模型
    if not arg:
        ui.info(f"当前协议为: {agent.config['api']} 当前模型为: {agent.config['model']}")
    else:
        agent.switch_model(model = arg)
        ui.info(f"切换成功! 当前协议为: {agent.config['api']} 当前模型为: {agent.config['model']}")



def cmd_api(agent, arg):
    # 切换协议，保留历史（内部消息格式与协议无关）
    if arg not in PROVIDERS:
        ui.error(f"不支持的协议，请在 {' / '.join(PROVIDERS)} 中选择")
    else:
        agent.switch_model(api=arg)
        ui.info(f"已切换协议为：{arg}")


def cmd_usage(agent, arg):
    u = agent.usage
    ui.info(f"输入: {ui.k(u['input'])} | 输出: {ui.k(u['output'])} | 缓存读: {ui.k(u['cache_read'])}")


def cmd_clear(agent, arg):
    agent.messages.clear()
    agent.context_tokens = 0
    ui.info("已清空对话历史")


# 运行时开关：/plan、/swarm。不带参数时切换，on / off 显式设置

def _toggle(arg):
    """只认 on / off；不认识的写法返回 None。"""
    return {"on": True, "off": False}.get(arg.strip().lower())


def cmd_plan(agent, arg):
    want = _toggle(arg) if arg else (not agent.plan_mode)
    if want is None:
        ui.error("用法：/plan on | /plan off（不带参数则切换）")
        return
    agent.plan_mode = want
    if want:
        ui.info("计划模式：已开启（只调研不改文件，bash 只放行只读命令）")
    else:
        ui.info("计划模式：已关闭（命令恢复正常执行）")


def cmd_swarm(agent, arg):
    want = _toggle(arg) if arg else (not agent.swarm_mode)
    if want is None:
        ui.error("用法：/swarm on | /swarm off（不带参数则切换）")
        return
    agent.swarm_mode = want
    if want:
        ui.info("Swarm 模式：已开启（模型可以调用 swarm 并行开多个子 Agent）")
    else:
        ui.info("Swarm 模式：已关闭（swarm 工具已从工具列表移除）")


# 命令名 → (处理函数, 说明)
COMMANDS = {
    "/help": (cmd_help, "显示所有命令"),
    "/model": (cmd_model, "查看或切换模型，例：/model gpt-4o"),
    "/api": (cmd_api, "切换协议：chat / responses / anthropic"),
    "/usage": (cmd_usage, "查看本次会话的 token 和缓存统计"),
    "/clear": (cmd_clear, "清空对话历史，开始新话题"),
    "/system": (cmd_system, "查看当前的系统提示词"),
    "/plan": (cmd_plan, "计划模式开关：/plan on | /plan off，不带参数则切换"),
    "/swarm": (cmd_swarm, "Swarm 模式开关：/swarm on | /swarm off，不带参数则切换"),
    "/exit": (cmd_exit, "退出"),
}


def run_command(agent, line):
    name, _, arg = line.partition(" ")
    if name not in COMMANDS:
        ui.error(f"未知命令 {name}，输入 /help 查看")
        return
    COMMANDS[name][0](agent, arg.strip())
