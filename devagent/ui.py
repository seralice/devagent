"""终端界面：配色和排版参考 opencode 默认主题（中性灰 + 桃色主色），不再用紫色。

对话内容直接打印进终端自己的滚动区，不开全屏界面，所以鼠标滚轮、选中复制都由终端处理。
交互模式下输入框由 repl.py 常驻在最底部，打印的内容会出现在它上方。
"""
import threading
import time

from rich.console import Console, Group
from rich.markdown import Heading, Markdown
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from rich.theme import Theme

# opencode 默认主题的暗色色值（packages/tui/src/theme/assets/opencode.json）
TEXT = "#eeeeee"
MUTED = "#808080"
PRIMARY = "#fab283"      # 主色：品牌、提示符、用户消息左边的竖条
SECONDARY = "#5c9cf5"    # 链接
RED = "#e06c75"
ORANGE = "#f5a742"       # 警告：拦截、等待确认
GREEN = "#7fd88f"
CYAN = "#56b6c2"
BG_PANEL = "#141414"     # 用户消息底色
BG_ELEMENT = "#1e1e1e"   # 输入框、补全菜单底色
BORDER = "#484848"
BORDER_SUBTLE = "#3c3c3c"

console = Console(theme=Theme({
    "markdown.code": GREEN,
    "markdown.h1": f"bold {TEXT}",
    "markdown.h2": f"bold {TEXT}",
    "markdown.h3": f"bold {TEXT}",
    "markdown.strong": f"bold {TEXT}",
    "markdown.em": f"italic {ORANGE}",
    "markdown.link": SECONDARY,
    "markdown.link_url": SECONDARY,
    "markdown.item.bullet": PRIMARY,
    "markdown.item.number": PRIMARY,
    "markdown.block_quote": MUTED,
    "markdown.hr": BORDER,
    "markdown.code_block": "none",
}))
MAX_LINES = 6  # 命令输出在屏幕上只显示前几行，发给模型的仍是完整输出（截断由 output_limit 扩展决定）
MAX_COMMAND_LINES = 3
INDENT = 3     # 回答、工具调用统一缩进 3 格，和用户消息的竖条对齐


class LeftHeading(Heading):
    """rich 默认把一级标题居中并加框，在终端里很突兀；统一改成左对齐。"""

    def __rich_console__(self, console, options):
        yield self.text.copy()


class AnswerMarkdown(Markdown):
    elements = {**Markdown.elements, "heading_open": LeftHeading}

    def __init__(self, text):
        # 代码块用 one-dark 高亮，monokai 的深灰底块在终端上很突兀
        super().__init__(text, code_theme="one-dark")


class Gutter:
    """左边留一条"沟"、右边是内容：先按"总宽度 - 沟宽"渲染内容，再给每一行拼上左边的部分。
    first / rest 是第一行和其余行左边显示的文字；bg 不为空时整行铺底色（用户消息用）。
    不用 Table：Table 的列遇到中文长句会截成省略号，而不是换行。"""

    def __init__(self, body, first, rest=None, mark_style="", bg=None):
        self.body, self.first, self.rest = body, first, first if rest is None else rest
        self.mark_style, self.bg = mark_style, bg

    def __rich_console__(self, console, options):
        width = len(self.first)
        bg = Style(bgcolor=self.bg) if self.bg else None
        lines = console.render_lines(self.body, options.update_width(options.max_width - width), style=bg, pad=bool(bg))
        # 去掉末尾的空行（Markdown 代码块会在后面多补一行）
        while lines and not "".join(seg.text for seg in lines[-1]).strip():
            lines.pop()
        mark_style = Style.parse(self.mark_style) + bg if bg else Style.parse(self.mark_style)
        for i, line in enumerate(lines):
            yield Segment(self.first if i == 0 else self.rest, mark_style)
            yield from line
            yield Segment.line()


def user(text):
    """用户消息：左边一条桃色竖条 + 深色底，和模型的回答一眼区分开。"""
    console.print()
    console.print(Gutter(Text(f" {text} ", TEXT), "┃ ", mark_style=PRIMARY, bg=BG_PANEL))


def _tag(label):
    """子 Agent（swarm 的 worker / verifier）并行打印时，前面加个名字，分得清是谁。"""
    return Text(f"[{label}] ", CYAN) if label else Text()


def command(cmd, label=""):
    lines = cmd.splitlines() or [""]
    shown = "\n".join(lines[:MAX_COMMAND_LINES]) + (" …" if len(lines) > MAX_COMMAND_LINES else "")
    console.print()
    console.print(Gutter(_tag(label) + Text("$ ", MUTED) + Text(shown, TEXT), " " * INDENT))


def output(text, label=""):
    # run_bash 的结果第一行是 exit=N；成功时不显示，失败时标红
    first, _, rest = text.partition("\n")
    head = Text()
    if first.startswith("exit="):
        if first != "exit=0":
            head.append(f"✗ {first}\n", RED)
        text = rest
    color = ORANGE if text.startswith("已拦截") else MUTED
    lines = text.rstrip("\n").splitlines() or ["（无输出）"]
    body = head + _tag(label) + Text("\n".join(lines[:MAX_LINES]), color)
    if len(lines) > MAX_LINES:
        body.append(f"\n… 还有 {len(lines) - MAX_LINES} 行", f"italic {MUTED}")
    console.print(Gutter(body, " " * INDENT + "│ ", mark_style=BORDER_SUBTLE))


def answer(text):
    console.print()
    console.print(Gutter(AnswerMarkdown(text or "（模型没有给出文字回答）"), " " * INDENT))


def info(text, label=""):
    console.print(_tag(label) + Text(" " * INDENT + text, CYAN))


def error(text):
    console.print(Text(" " * INDENT + text, f"bold {RED}"))


def plain(text):
    console.print(Gutter(Text(text, TEXT), " " * INDENT))


def clear():
    console.clear()


# 确认提示要独占终端：swarm 的多个 worker 可能同时命中危险命令，不加锁就会有两个提示抢同一行输入。
# 扩展调 ui.confirm 时不用自己加锁，这里已经加好了
CONFIRM_LOCK = threading.Lock()


def confirm(question):
    with CONFIRM_LOCK:
        try:
            return console.input(Text(f"\n{question} [y/N] ", f"bold {ORANGE}")).strip().lower() == "y"
        except EOFError:
            return False  # 输入是管道、已经读完时当作拒绝，不让整个任务报错


class Elapsed:
    """转圈动画旁边的文字。rich 每次刷新都会重新调用 __rich__，所以秒数会自己往上走。"""

    def __init__(self):
        self.start = time.monotonic()

    def __rich__(self):
        return Text(f"思考中 {time.monotonic() - self.start:.0f}s", PRIMARY) + Text(" · Ctrl+C 中断", MUTED)


def thinking():
    """单任务模式用 rich 自己转圈；交互模式下 repl.py 会把它替换成输入框里的转圈。"""
    return console.status(Elapsed(), spinner="dots", spinner_style=PRIMARY)


def k(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def status(delta, seconds, model):
    """每个任务结束后，在回答下面打一行：模型、用时、本次的输入 / 输出 / 缓存命中。"""
    hit = f"{delta['cache_read'] / delta['input']:.0%}" if delta["input"] else "-"
    console.print()
    console.print(Text(" " * INDENT) + Text("▣ ", PRIMARY) + Text(
        f"{model} · {seconds:.0f}s · ↑{k(delta['input'])} ↓{k(delta['output'])} · 缓存 {k(delta['cache_read'])} ({hit})",
        MUTED))


def banner_renderable(agent):
    def row(key, desc):
        return Text(" " * INDENT) + Text(f"{key:<10}", f"bold {TEXT}") + Text(desc, MUTED)

    return Group(
        Text(" " * INDENT) + Text("dev", MUTED) + Text("agent", f"bold {TEXT}") + Text("  v0.1  ", MUTED)
        + Text("只有一个 bash 工具的自举 Agent", MUTED),
        Text(),
        row("/help", "查看所有命令"),
        row("/model", "切换模型"),
        row("↑ ↓", "翻输入历史"),
        row("Ctrl+C", "中断任务；空输入时连按两次退出"),
        Text(),
    )


def banner(agent):
    console.print(banner_renderable(agent))
