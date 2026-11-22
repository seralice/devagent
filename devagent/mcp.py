"""极简 MCP (Model Context Protocol) 客户端：基于 Stdio 的 JSON-RPC 2.0 实现。

遵循 Anthropic MCP 规范（2024-11-05）：
1. 启动外部 MCP Server 进程，建立标准输入输出管道；
2. 发送 initialize 完成版本与能力握手；
3. 发送 tools/list 动态拉取工具元数据，转换为统一的参数规范；
4. 当模型发起工具调用时，通过 tools/call 发送给对应的 MCP Server 执行。
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from devagent import ui


class McpClient:
    """单个 MCP Server 进程的客户端连接。"""

    def __init__(self, name, command, args=None, env=None, cwd=None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = {**os.environ, **(env or {})}
        self.cwd = cwd
        self.process = None
        self.request_id = 0
        self.pending = {}  # id -> (threading.Event, box_dict)
        self.lock = threading.Lock()
        self.tools = []    # 从该 server 拿到的工具列表

    def start(self):
        cmd = [self.command, *self.args]
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # 不能用 PIPE：没人读的话，Server 日志写满管道缓冲区后会卡住，响应永远发不出来
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            cwd=self.cwd,
            env=self.env,
            bufsize=1,
        )
        t = threading.Thread(target=self._reader_loop, daemon=True)
        t.start()
        self._initialize()
        self._load_tools()

    def _reader_loop(self):
        """后台线程读取 MCP Server 的 stdout（逐行 JSON-RPC 响应）。"""
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue  # 忽略非 JSON 行（比如 Server 偶发的调试信息）
            req_id = data.get("id")
            if req_id is not None and req_id in self.pending:
                event, box = self.pending[req_id]
                box["response"] = data
                event.set()

    def _send_request(self, method, params=None, timeout=10):
        with self.lock:
            self.request_id += 1
            req_id = self.request_id
            payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
            event = threading.Event()
            box = {}
            self.pending[req_id] = (event, box)

        data_str = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            self.process.stdin.write(data_str)
            self.process.stdin.flush()
        except Exception as e:
            raise RuntimeError(f"向 MCP Server [{self.name}] 发送消息失败：{e}")

        try:
            # 分段等待，理由同 bash.run_bash：让 Ctrl+C 能及时打断
            deadline = time.monotonic() + timeout
            while not event.wait(0.1):
                if time.monotonic() > deadline:
                    raise TimeoutError(f"MCP Server [{self.name}] 响应超时 ({timeout}s)")
        finally:
            self.pending.pop(req_id, None)
        resp = box.get("response", {})
        if "error" in resp:
            raise RuntimeError(f"MCP Server [{self.name}] 报错：{resp['error'].get('message', resp['error'])}")
        return resp.get("result", {})

    def _send_notification(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except Exception:
            pass

    def _initialize(self):
        self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "devagent", "version": "0.1.0"}
        })
        self._send_notification("notifications/initialized")

    def _load_tools(self):
        res = self._send_request("tools/list", {})
        self.tools = res.get("tools", [])

    def call_tool(self, tool_name, arguments, timeout=60):
        res = self._send_request("tools/call", {"name": tool_name, "arguments": arguments}, timeout=timeout)
        parts = []
        for item in res.get("content", []):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(parts) if parts else "（工具执行成功，无输出）"

    def stop(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=1)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass


class McpManager:
    """管理所有配置的 MCP Server，聚合工具并分发调用。"""

    def __init__(self):
        self.clients = {}         # server_name -> McpClient
        self.tool_map = {}        # tool_name -> McpClient

    def load_from_config(self, mcp_servers_dict):
        """读取形如 {"server_a": {"command": "...", "args": [...]}} 的配置并启动。"""
        for name, cfg in mcp_servers_dict.items():
            if name in self.clients:
                continue
            command = cfg.get("command")
            if not command:
                continue
            args = cfg.get("args", [])
            env = cfg.get("env")
            cwd = cfg.get("cwd")
            client = McpClient(name, command, args, env, cwd)
            try:
                client.start()
                self.clients[name] = client
                for t in client.tools:
                    # 如果工具名重复，加上 server 前缀防止冲突
                    t_name = t["name"] if t["name"] not in self.tool_map else f"{name}__{t['name']}"
                    self.tool_map[t_name] = (client, t)
            except Exception as e:
                ui.error(f"[MCP Server {name} 启动失败，已跳过] {e}")

    def get_tool_specs(self):
        """把所有 MCP 工具转换为模型需要的标准 function 格式。"""
        specs = []
        for t_name, (client, t) in self.tool_map.items():
            specs.append({
                "name": t_name,
                "description": f"[{client.name}] {t.get('description', '')}",
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
            })
        return specs

    def has_tool(self, name):
        return name in self.tool_map

    def call_tool(self, name, arguments):
        client, original_tool = self.tool_map[name]
        return client.call_tool(original_tool["name"], arguments)

    def close(self):
        for client in self.clients.values():
            client.stop()
        self.clients.clear()
        self.tool_map.clear()
