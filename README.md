# DevAgent

只内置一个 `bash` 工具、能自举扩展的极简命令行 Coding Agent（Python）。设计参考 [Pi](https://github.com/badlogic/pi-mono)。

- **能力自举**：模型需要的新能力，自己写成脚本存进 `tools/`，之后的任务会在系统提示词里看到这些脚本，直接复用。
- **行为自举**：需要改变自身行为时（比如禁止某类命令），模型自己写一个扩展存进 `extensions/`，挂到生命周期钩子上，下一个任务开始时自动加载。

工具和扩展分两级：`~/.devagent/` 下的全局生效，当前目录 `.devagent/` 下的只对当前项目生效。

## 架构

```
cli.py / repl.py        命令行入口；常驻底部输入框，Agent 在后台线程运行，可随时 Ctrl+C 中断
      │
agent.py                agent loop + 事件总线 + 扩展加载 + 工具分发（bash / MCP）
      │
      ├── providers.py  内部消息格式 ↔ OpenAI Chat / OpenAI Responses / Anthropic Messages
      ├── bash.py       唯一的内置工具：Git Bash / bash，可中断，超时后结束整个进程树
      ├── mcp.py        零依赖的 MCP 客户端（JSON-RPC 2.0 over Stdio）
      ├── prompt.py     系统提示词：环境、已有工具、技能清单、扩展写法、当前目录的 AGENTS.md / CLAUDE.md
      ├── skills.py     读取 ~/.devagent/skills 和 .devagent/skills 下的 SKILL.md（只解析 frontmatter）
      └── extensions/   内置扩展：危险命令确认、长输出截断、上下文压缩
```

### 多协议适配

对话历史统一存成内部格式，每种协议各写一个适配器，负责请求转换和 usage 归一化（input / output / cache_read / cache_write）。所以中途用 `/api` 切换协议、用 `/model` 切换模型时，历史不会丢。

Anthropic 协议会手动设置 `cache_control` 断点：静态的系统提示词放在前面，动态内容放在后面。实测一个 14 步的长任务（逐个读 9 个源文件），整体缓存命中率 88%，后半程单步 97%～99%。

### 生命周期钩子

| 事件 | 时机 | 返回值 |
|---|---|---|
| `session_start` | 启动时一次 | — |
| `before_agent_start(system)` | 每个任务开始 | 新的系统提示词 |
| `turn_start(step)` | 每次调用模型前 | — |
| `tool_call(None, call)` | 执行工具前 | 字符串表示拦截，作为拒绝原因返回给模型 |
| `tool_result(output, call)` | 执行工具后 | 新的工具输出 |
| `agent_end(answer)` | 任务结束 | — |

同一事件上的函数按加载顺序链式执行：返回值不是 `None` 就替换掉当前值，再传给下一个。每个任务开始时都会重新加载扩展，所以模型新写的扩展不用重启就能生效。扩展就是一个定义了 `register(agent)` 的 Python 文件：

```python
def register(agent):
    def block_curl(_, call):
        if "curl" in call["args"].get("command", ""):
            return "禁止使用 curl"
    agent.on("tool_call", block_curl)
```

内置的三个扩展都是这样实现的：

- `permission_gate`：危险命令（`rm -rf`、`sudo`、`git push` 等）先请用户确认，拒绝则拦截
- `output_limit`：工具输出超过 4000 字符时截断
- `compact`：上下文超过阈值时，总结较早的消息（保留最近几条原文，切点不落在工具结果上），原文存档到 `.devagent/history/`，摘要里写明存档路径

### Skills（技能）

一个 skill 是 `<目录>/<名字>/SKILL.md`：开头是 `---` 包起来的 frontmatter，至少有 `name` 和 `description`，后面是正文。全局的放 `~/.devagent/skills/`，项目的放当前目录的 `.devagent/skills/`，同名时项目的生效。

```markdown
---
name: release
description: 打版本号、改 CHANGELOG、发测试包
---
# 步骤
1. 改 pyproject.toml 的 version
...
```

系统提示词里只列每个 skill 的 name、description 和 SKILL.md 路径，正文不加载，模型判断任务和某个描述相符时再用 bash 读那个文件。格式不对（没有 frontmatter、缺 name / description）的文件跳过并打一行警告，不影响运行。

### MCP 客户端

`mcp.py` 不依赖第三方库：用子进程管道收发 JSON-RPC 2.0 消息，完成 `initialize` 握手、`tools/list`、`tools/call`。请求按 id 登记，由后台读线程配对响应。多个 Server 的工具合并后交给模型，调用时按工具名路由；工具重名时加上 Server 前缀。

### 可中断

Ctrl+C 通过向工作线程注入 `KeyboardInterrupt` 实现，但线程阻塞在网络或子进程的 C 调用里时收不到。所以模型请求、bash 子进程、MCP 调用都改成了 0.1 秒一次的短超时等待：模型请求放到 daemon 线程里跑，外面循环 `join(0.1)`；子进程用 `communicate(timeout=0.1)`；MCP 用 `event.wait(0.1)`。这样无论在等什么，都能在 1 秒内停下。结束 bash 时，Windows 上用 `taskkill /T` 结束整个进程树，不留残留进程。

## 安装与运行

需要 Python 3.10+。Windows 上需要安装 Git Bash。

```bash
pip install -e .
devagent                     # 交互模式
devagent "统计当前目录下 Python 文件的总行数"   # 单次任务
```

配置优先级：环境变量 > `~/.devagent/config.json` > 默认值。

```bash
export DEVAGENT_API=anthropic          # chat / responses / anthropic
export DEVAGENT_MODEL=claude-sonnet-4-5
export DEVAGENT_BASE_URL=https://api.anthropic.com
export DEVAGENT_API_KEY=...
```

MCP Server 写在 `~/.devagent/config.json` 里（仓库自带一个示范 Server，提供 `calculate`、`get_system_time` 两个工具）：

```json
{
  "mcp_servers": {
    "demo": { "command": "python", "args": ["-m", "devagent.demo_server"] }
  }
}
```

交互模式下的命令：`/help`、`/model`、`/api`、`/usage`、`/clear`、`/system`、`/exit`。

## License

Apache-2.0
