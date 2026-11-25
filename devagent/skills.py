"""Skills（技能）：一个 skill 是 <目录>/<名字>/SKILL.md，开头是 --- 包起来的 frontmatter，至少有 name 和 description。

全局的放 ~/.devagent/skills/，项目的放 当前目录/.devagent/skills/，同名时项目的生效。
系统提示词里只列出 name、description 和路径，正文让模型自己用 bash 读，省上下文。
格式不对的文件跳过并警告，不让程序崩溃。
"""
from devagent import ui
from devagent.config import search_dirs

MAX_DESC_CHARS = 300  # 描述太长会挤占系统提示词


def parse_frontmatter(text):
    """返回 frontmatter 的字段 dict；没有 frontmatter 或写坏了返回 None。只认简单的 key: value，不引入 YAML 依赖。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    meta = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return meta
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            return None
        meta[key.strip()] = value.strip().strip("\"'")
    return None  # 没有闭合的 ---，算格式不对


def load_skills(cwd):
    """读取全局和项目的 skill，返回 name -> {"name", "description", "path"}。先全局后项目，同名时项目的覆盖全局的。"""
    found = {}
    for d in search_dirs(cwd, "skills"):
        for f in sorted(d.glob("*/SKILL.md")):
            try:
                meta = parse_frontmatter(f.read_text(encoding="utf-8"))
            except Exception as e:
                ui.error(f"[skill {f} 读取失败，已跳过] {e!r}")
                continue
            if meta is None:
                ui.error(f"[skill {f} 没有合法的 --- frontmatter，已跳过]")
                continue
            name, desc = meta.get("name", ""), meta.get("description", "")
            if not name or not desc:
                ui.error(f"[skill {f} 缺少 name 或 description，已跳过]")
                continue
            if len(desc) > MAX_DESC_CHARS:
                desc = desc[:MAX_DESC_CHARS] + "…"
            found[name] = {"name": name, "description": desc, "path": f.as_posix()}
    return found


def format_skills(cwd):
    """系统提示词里的一段；没有 skill 时返回空串。"""
    skills = load_skills(cwd)
    if not skills:
        return ""
    lines = ["# 技能（Skills）",
             "下面每个 skill 的正文没有加载进上下文。任务和某个 skill 的描述相符时，先用 bash 读取它的 SKILL.md，再按里面的要求做。"]
    for s in skills.values():
        lines.append(f"- {s['name']}：{s['description']}\n  路径：{s['path']}")
    return "\n".join(lines)
