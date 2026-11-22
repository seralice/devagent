"""交互模式：输入框常驻在终端最底部，对话内容打印在它上方。布局参考 opencode：

    ┃  输入内容…                                   ← 深色底的输入框，左边一条主色竖条
    ┃  chat · gemini-3.8-flash                     ← 框内第二行：协议和模型
     ⠋ 思考中 3s · ctrl+c 中断              上下文 2.7k · 目录   ← 框下状态行

- 用 prompt_toolkit 的 Application，但不开全屏：打印过的内容留在终端自己的滚动区，鼠标滚轮、选中复制照常可用。
- Agent 在后台线程里跑，主线程一直负责输入框，所以模型思考时也能看计时、中断、提前打下一句。
- 后台线程的打印经过 patch_stdout 转发，出现在输入框上方，不会把输入框冲乱。
"""
import ctypes
import threading
import time
from contextlib import contextmanager

from prompt_toolkit.application import Application
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.defaults import load_key_bindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window, WindowAlign
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import AfterInput, AppendAutoSuggestion, ConditionalProcessor
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.text import Text

from devagent import ui
from devagent.commands import COMMANDS, run_command
from devagent.config import HOME_DIR

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
EXIT_WINDOW = 2.0  # 两次 Ctrl+C 间隔在这个秒数内才退出，防止误触

STYLE = Style.from_dict({
    "box": f"bg:{ui.BG_ELEMENT} {ui.TEXT}",
    "bar": f"bg:{ui.BG_ELEMENT} {ui.PRIMARY}",
    "bar.ask": f"bg:{ui.BG_ELEMENT} {ui.ORANGE}",
    "meta": f"bg:{ui.BG_ELEMENT} {ui.MUTED}",
    "meta.mode": f"bg:{ui.BG_ELEMENT} {ui.PRIMARY} bold",
    "placeholder": f"bg:{ui.BG_ELEMENT} {ui.BORDER}",
    "auto-suggestion": ui.BORDER,
    "status": ui.MUTED,
    "status.key": f"{ui.TEXT} bold",
    "status.busy": ui.PRIMARY,
    "status.warn": f"{ui.ORANGE} bold",
    "completion-menu": f"bg:{ui.BG_PANEL} {ui.TEXT}",
    "completion-menu.completion.current": f"bg:{ui.BG_ELEMENT} {ui.PRIMARY} bold",
    "completion-menu.meta.completion": f"bg:{ui.BG_PANEL} {ui.MUTED}",
    "completion-menu.meta.completion.current": f"bg:{ui.BG_ELEMENT} {ui.TEXT}",
    "scrollbar.background": f"bg:{ui.BG_PANEL}",
    "scrollbar.button": f"bg:{ui.BORDER}",
})


class CommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            for name, (_, desc) in COMMANDS.items():
                if name.startswith(text):
                    yield Completion(name, start_position=-len(text), display_meta=desc)


class Repl:
    def __init__(self, agent, run_one):
        self.agent, self.run_one = agent, run_one
        self.worker = None          # 正在跑任务的后台线程
        self.busy_since = None      # 不为 None 时，状态行显示转圈和计时
        self.pending_ask = None     # 扩展在等用户回答 y/N 时：(Event, 存答案的 dict)
        self.last_ctrl_c = 0.0      # 上一次在空闲时按 Ctrl+C 的时间，用来判断"连按两次退出"

        self.buffer = Buffer(multiline=True, history=FileHistory(str(HOME_DIR / "history")),
                             completer=CommandCompleter(), complete_while_typing=True,
                             auto_suggest=AutoSuggestFromHistory(), accept_handler=self.on_accept)
        self.app = Application(layout=self.build_layout(), key_bindings=merge_key_bindings([load_key_bindings(), self.keys()]),
                               style=STYLE, full_screen=False, refresh_interval=0.15, erase_when_done=True)

        # 交互模式下，转圈和确认都改走输入框；rich 的 Live 和 input() 会和输入框抢终端
        ui.thinking = self.thinking
        ui.confirm = self.ask

    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    # ---------- 布局 ----------

    def build_layout(self):
        def with_bar(content):
            bar = FormattedTextControl(lambda: [("class:bar.ask" if self.pending_ask else "class:bar", "┃ ")])
            return VSplit([Window(bar, width=3, dont_extend_width=True, style="class:box"), content])

        # 没输入内容时，在光标后面显示灰色提示（和 PromptSession 的 placeholder 一样的做法）
        placeholder = ConditionalProcessor(AfterInput(lambda: self.placeholder_text(), style="class:placeholder"),
                                           filter=Condition(lambda: not self.buffer.text))
        input_row = Window(BufferControl(self.buffer, input_processors=[placeholder, AppendAutoSuggestion()]),
                           height=Dimension(min=1, max=10), wrap_lines=True, dont_extend_height=True, style="class:box")
        blank = lambda: Window(height=1, style="class:box")
        box = HSplit([
            with_bar(blank()),
            with_bar(input_row),
            with_bar(Window(FormattedTextControl(self.meta_text), height=1, style="class:box")),
            with_bar(blank()),
        ])
        # 补全菜单排在输入框上方（而不是浮在光标下面），不会挡住状态行
        menu = ConditionalContainer(CompletionsMenu(max_height=8), filter=Condition(lambda: self.buffer.complete_state is not None))
        status = VSplit([Window(FormattedTextControl(self.status_left), height=1),
                         Window(FormattedTextControl(self.status_right), height=1, align=WindowAlign.RIGHT)])
        return Layout(HSplit([menu, box, status]), focused_element=input_row)

    def placeholder_text(self):
        if self.pending_ask:
            return "输入 y 确认，其他任意内容拒绝"
        if self.busy():
            return "可以先输入下一句，任务结束后再发送"
        return '问点什么，例如 "当前目录有哪些文件"'

    def meta_text(self):
        a = self.agent
        return [("class:meta.mode", a.config["api"]), ("class:meta", f" · {a.config['model']}")]

    def status_left(self):
        if self.time_to_exit():
            return [("class:status.warn", " 再按一次 Ctrl+C 退出")]
        if self.busy_since is not None:
            frame = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
            return [("class:status.busy", f" {frame} 思考中 {time.monotonic() - self.busy_since:.0f}s"),
                    ("class:status", " · "), ("class:status.key", "ctrl+c"), ("class:status", " 中断")]
        if self.busy():
            return [("class:status.busy", " ● 执行命令中"), ("class:status", " · "), ("class:status.key", "ctrl+c"),
                    ("class:status", " 中断")]
        return [("class:status", " "), ("class:status.key", "enter"), ("class:status", " 发送 · "),
                ("class:status.key", "\\ enter"), ("class:status", " 换行 · "),
                ("class:status.key", "/"), ("class:status", " 命令")]

    def status_right(self):
        return [("class:status", f"上下文 {ui.k(self.agent.context_tokens)} · {self.agent.cwd.name or self.agent.cwd} ")]

    def time_to_exit(self):
        return time.monotonic() - self.last_ctrl_c < EXIT_WINDOW

    # ---------- 按键 ----------

    def keys(self):
        kb = KeyBindings()

        @kb.add("enter")
        def _(event):
            buffer = event.current_buffer
            # 补全菜单开着时，回车是"选中"（没用方向键挑过就选第一项），不是发送
            state = buffer.complete_state
            if state and state.completions:
                buffer.apply_completion(state.current_completion or state.completions[0])
            # 行尾的 \ 加回车表示换行，方便输入多行任务
            elif buffer.document.text_before_cursor.endswith("\\"):
                buffer.delete_before_cursor()
                buffer.insert_text("\n")
            else:
                buffer.validate_and_handle()

        @kb.add("c-c")
        def _(event):
            # 优先级：拒绝确认 > 中断任务 > 清空输入框 > 连按两次退出
            if self.pending_ask:
                self.answer_ask("n")
            elif self.busy():
                self.interrupt()
            elif self.buffer.text:
                self.buffer.reset()
            elif self.time_to_exit():
                event.app.exit()
            else:
                self.last_ctrl_c = time.monotonic()

        @kb.add("c-d")
        def _(event):
            if not self.buffer.text:
                event.app.exit()

        return kb

    # ---------- 输入处理 ----------

    def on_accept(self, buffer):
        text = buffer.text.strip()
        if self.pending_ask:
            self.answer_ask(text)
        elif not text:
            pass
        elif self.busy():
            return True  # 任务还没结束，保留输入框里的内容，不用重打
        elif text.startswith("/"):
            ui.user(text)
            try:
                run_command(self.agent, text)
            except SystemExit:
                self.app.exit()
            except Exception as e:
                ui.error(f"命令出错：{e!r}")
        else:
            ui.user(text)
            self.worker = threading.Thread(target=self.run_one, args=(self.agent, text), daemon=True)
            self.worker.start()
        return False  # 清空输入框

    def interrupt(self):
        """往后台线程里抛 KeyboardInterrupt。它要等线程回到 Python 代码时才生效，
        所以模型请求、bash 命令、MCP 调用都改成了每 0.1 秒醒一次的等法（见 agent.wait_interruptibly）。"""
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(self.worker.ident), ctypes.py_object(KeyboardInterrupt))
        ui.info("正在中断…")

    # ---------- 给 ui 用的两个钩子（在后台线程里被调用） ----------

    @contextmanager
    def thinking(self):
        self.busy_since = time.monotonic()
        try:
            yield
        finally:
            self.busy_since = None
            self.app.invalidate()

    def ask(self, question):
        """扩展（如危险命令确认）要用户回答 y/N：把问题打印出来，等用户在输入框里回答。"""
        ui.console.print(Text(f"\n{' ' * ui.INDENT}{question} [y/N]", f"bold {ui.ORANGE}"))
        done, box = threading.Event(), {}
        self.pending_ask = (done, box)
        self.app.invalidate()
        done.wait()
        return box["answer"]

    def answer_ask(self, text):
        done, box = self.pending_ask
        self.pending_ask = None
        box["answer"] = text.strip().lower() == "y"
        done.set()

    def run(self):
        with patch_stdout(raw=True):
            self.app.run()
