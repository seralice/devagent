"""输出截断：tool_result 钩子。命令输出过长时截断，避免撑爆上下文。"""

MAX_OUTPUT = 4000

def truncate(output, call):
    length = len(output)
    if length > MAX_OUTPUT:
        return output[:MAX_OUTPUT] + f"\n...（输出过长，已截断，共 {length} 字符）"
    return None


def register(agent):
    agent.on("tool_result", truncate)
