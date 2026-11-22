"""一个最简的标准 MCP Server 示范（纯 Python 标准库），供测试与演示。

通过 Stdio 处理 JSON-RPC 2.0 请求：
- initialize: 握手并声明工具能力
- tools/list: 声明 calculate 和 get_system_time 两个工具
- tools/call: 执行工具逻辑并返回结果
"""
import datetime
import json
import math
import sys

TOOLS = [
    {
        "name": "get_system_time",
        "description": "获取当前精确的系统时间和时区",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "calculate",
        "description": "精确计算数学表达式，支持基本算术与 math 库函数（如 sqrt, sin, pi）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "要计算的数学表达式，如 '2 ** 10' 或 'math.sqrt(144) * 5'",
                }
            },
            "required": ["expression"],
        },
    },
]


def handle_request(req):
    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "demo-tools-server", "version": "1.0.0"},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        params = req.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {})

        if name == "get_system_time":
            now = datetime.datetime.now().astimezone()
            text = f"当前系统时间为：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}"
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": text}]},
            }

        if name == "calculate":
            expr = args.get("expression", "")
            try:
                # 仅允许受限的安全命名空间
                safe_env = {"__builtins__": None, "math": math}
                val = eval(expr, safe_env, {})
                text = f"{expr} = {val}"
            except Exception as e:
                text = f"计算出错：{e}"
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": text}]},
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"未知的工具：{name}"},
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"未知的请求方法：{method}"},
    }


def main():
    # 逐行读取 stdin 上的 JSON-RPC 请求
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
