"""配置与目录。

配置优先级：环境变量 DEVAGENT_API / DEVAGENT_MODEL / DEVAGENT_BASE_URL / DEVAGENT_API_KEY
         > ~/.devagent/config.json > 默认值。
只能写在 config.json 里的：mcp_servers；anthropic_input_includes_cache（网关返回的 Anthropic input_tokens
已含缓存部分时设为 true，否则命中率会被算低，见 providers.py）。

工具和扩展分两级（和 Pi 一样）：~/.devagent/ 下的全局生效，当前目录 .devagent/ 下的只对这个项目生效。
"""
import json
import os
from pathlib import Path

HOME_DIR = Path.home() / ".devagent"
BUILTIN_EXTENSIONS = Path(__file__).parent / "extensions"
DEFAULTS = {"api": "chat", "model": "gpt-4o-mini", "base_url": None, "api_key": None}


def load_config():
    config = dict(DEFAULTS)
    file = HOME_DIR / "config.json"
    if file.is_file():
        config.update(json.loads(file.read_text(encoding="utf-8")))
    for key in DEFAULTS:
        if os.getenv(f"DEVAGENT_{key.upper()}"):
            config[key] = os.getenv(f"DEVAGENT_{key.upper()}")
    return config


def search_dirs(cwd, kind):
    """kind 是 "tools" 或 "extensions"，先全局后项目，同名时项目的排在后面。"""
    return [HOME_DIR / kind, cwd / ".devagent" / kind]
