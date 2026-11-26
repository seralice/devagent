"""计划模式：只允许只读命令，并给出给模型的说明。

检查分两步，先粗后细：
1. 逐字符扫一遍，跟住引号状态。引号外的 < > ( ) 和任何位置的反引号、$( 一律拦住。
   只认白名单不够，这几种写法都能绕过去：`cat x > y` 是重定向，
   `ls <(touch x)` 是进程替换，`grep --exclude-from=<(touch x)` 把进程替换塞进了参数值，
   `cat k |& tee x` 用 |& 把输出又接到下一段。引号里的这些字符是普通字符，不拦，
   所以 `grep '^<div' f`、`grep -e 'a|b' f` 照常放行。
2. 用 shlex 按换行和 | |& || && ; & 切成若干段（引号里的 | ; 不会被切开），
   每段的第一条命令都必须在白名单里，白名单命令里会执行外部程序或写文件的参数也要拦。
"""
import shlex

# 允许的简单命令（只看每段的第一条命令）
SIMPLE_COMMANDS = {"ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "pwd"}
# git 只允许这几个只读子命令
GIT_SUBCOMMANDS = {"status", "diff", "log", "show"}
# 分段用的操作符。`;` 和 `&` 可以出现在末尾（`ls;`、`ls &`），管道和 && 不行：`ls |&` 少一条命令
SEPARATORS = {"|", "|&", "||", "&&", ";", "&"}
TRAILING_OK = {";", "&"}
# 这些参数会执行外部程序或写文件，精确匹配（取 = 前面那段再比）
FORBIDDEN_ARGS = {
    "find": {"-exec", "-execdir", "-ok", "-okdir", "-delete",
             "-fls", "-fprint", "-fprint0", "-fprintf"},
    # rg 的 --pre 每处理一个文件都先跑一次外部程序
    "rg": {"--pre", "--pre-glob"},
    # git 的 --ext-diff / --textconv 会调用外部 diff 程序
    "git": {"--ext-diff", "--textconv", "--output", "--open-files-in-pager"},
}


def allowed_summary():
    names = "、".join(sorted(SIMPLE_COMMANDS))
    return f"{names}，以及 {' / '.join('git ' + s for s in sorted(GIT_SUBCOMMANDS))}"


def _scan(line):
    """引号外出现重定向、括号，或任何位置出现命令替换，就返回原因。"""
    i, quote = 0, None
    while i < len(line):
        c = line[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif quote == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                quote = None
            elif c == "`":
                return "命令里出现了反引号"
            elif c == "$" and line[i + 1:i + 2] == "(":
                return "命令里出现了 $(...)"
        elif c == "\\":
            i += 1
        elif c in "'\"":
            quote = c
        elif c == "`":
            return "命令里出现了反引号"
        elif c == "$" and line[i + 1:i + 2] == "(":
            return "命令里出现了 $(...)"
        elif c in "<>":
            return f"命令里出现了重定向符号 {c}"
        elif c in "()":
            return f"命令里出现了括号 {c}（子 shell 或进程替换）"
        i += 1
    return None


def _check_segment(segment):
    """segment 是一段命令的 token 列表。不合规返回原因，合规返回 None。"""
    shown = " ".join(segment)
    name = segment[0].rsplit("/", 1)[-1]  # /bin/ls 也当 ls
    if name == "git":
        if len(segment) < 2 or segment[1] not in GIT_SUBCOMMANDS:
            return f"`{shown}` 里的 git 子命令不是只读的"
        args = segment[2:]
    elif name in SIMPLE_COMMANDS:
        args = segment[1:]
    else:
        return f"`{name}` 不在计划模式的白名单里"
    for arg in args:
        if arg.split("=", 1)[0] in FORBIDDEN_ARGS.get(name, ()):
            return f"`{shown}` 里的 {arg} 会执行外部命令或写文件"
    return None


def _check_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    reason = _scan(line)
    if reason:
        return reason
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError as e:
        return f"`{line}` 无法解析（{e}），计划模式下只接受简单命令"

    segments, current = [], []
    for token in tokens:
        if token in SEPARATORS:
            if current:
                segments.append(current)
                current = []
            elif token != tokens[-1] or token not in TRAILING_OK:
                return f"`{line}` 里有空的命令片段"
        elif token.strip():
            current.append(token)
    if tokens and tokens[-1] in SEPARATORS and tokens[-1] not in TRAILING_OK:
        return f"`{line}` 末尾的 {tokens[-1]} 后面没有命令"
    if current:
        segments.append(current)
    if not segments:
        return f"`{line}` 不是一条命令"
    for segment in segments:
        reason = _check_segment(segment)
        if reason:
            return reason
    return None


def violation(command):
    """命令能执行返回 None，否则返回拦截原因（作为工具结果交给模型）。"""
    for line in (command or "").splitlines():
        reason = _check_line(line)
        if reason:
            return (f"{reason}。计划模式只允许只读命令：{allowed_summary()}；"
                    f"不要重定向、不要用命令替换或进程替换、不要执行会调外部程序的参数。"
                    f"需要改文件请让用户先执行 /plan off")
    if not (command or "").strip():
        return "命令为空"
    return None


PROMPT_SECTION = """# 当前是计划模式（只调研，不改文件）
用户开启了计划模式，这次你只能调研，不能修改任何东西：
- bash 只能执行只读命令：ls、cat、head、tail、grep、rg、find、wc、pwd，以及 git status / git diff / git log / git show。
  其他命令会被拦截；重定向（> >> < <<）、命令替换（$(...) 和反引号）、进程替换（<(...)）也会被拦截；
  find 的 -exec / -delete、rg 的 --pre、git 的 --ext-diff / --textconv 这类会执行外部程序的参数同样会被拦截。
- 不要创建、修改、删除任何文件，不要提交、不要推送到远端。要改动的地方先写进计划。
- 调研尽量做全：读关键文件、看 git 历史和现状、确认调用关系，再下结论。
- 最后输出一份编号的实施计划：每一步写清改哪个文件、具体改什么、怎么验证；
  后面列出你的假设、风险和需要用户确认的选择。用户执行 /plan off 之后你再动手。"""
