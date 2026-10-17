"""终端界面模块（TUI）"""

from __future__ import annotations

import sys
from typing import List, Optional

from . import console

# 界面里用到的 ANSI 样式。全部是 ASCII 转义序列，不涉及图形符号。
STYLE_RESET = "\x1b[0m"
STYLE_PROMPT = "\x1b[1;32m"
CLEAR_SCREEN = "\x1b[2J\x1b[H"

# 输入行提示符
PROMPT_TEXT = "LinXia > "

# 退出界面的关键词
EXIT_WORDS = {"exit", "quit", "q", "退出", "再见"}

# 进入界面时提示符上方留的空行数，避免贴着窗口最顶端
TOP_BLANK_LINES = 2


# 基础绘制


def clear_screen() -> None:
    """清空可见区域。保留回滚缓冲区，用户仍可向上翻看历史输出。"""
    sys.stdout.write(CLEAR_SCREEN)
    sys.stdout.flush()


def clear_or_separate(ansi_ready: bool) -> None:
    """刷新界面。"""
    if ansi_ready:
        clear_screen()
        return
    sys.stdout.write("\n" + "-" * 60 + "\n")
    sys.stdout.flush()


def render_home(extra_blank: int = 0) -> str:
    """进入界面时的初始画面。"""
    return "\n" * (TOP_BLANK_LINES + max(extra_blank, 0))


def split_command_line(text: str) -> List[str]:
    """把用户输入的一行拆成参数列表。"""
    tokens: List[str] = []
    current: List[str] = []
    in_quotes = False

    for character in text:
        if character == '"':
            in_quotes = not in_quotes
            continue
        if character.isspace() and not in_quotes:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(character)

    if current:
        tokens.append("".join(current))
    return tokens


class _ExitInterface(Exception):
    """用户要求退出界面时抛出，用于跳出输入等待。"""


def _read_command(use_color: bool = True) -> str:
    """读取一行输入。用户按 Ctrl+C 或输入流结束时抛出退出信号。"""
    prompt = f"{STYLE_PROMPT}{PROMPT_TEXT}{STYLE_RESET}" if use_color else PROMPT_TEXT
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise _ExitInterface()


def run_tui(force: bool = False) -> Optional[int]:
    """进入交互界面。"""
    ansi_ready = console.enable_ansi()
    if not ansi_ready and not force:
        return None
    # 强制模式（-ui）在非交互环境下仍然渲染，主要用于自动化测试
    use_color = ansi_ready

    try:
        clear_or_separate(ansi_ready)
        sys.stdout.write(render_home())
        sys.stdout.flush()

        while True:
            raw = _read_command(use_color).strip()
            if not raw:
                continue

            tokens = split_command_line(raw)
            if not tokens:
                continue
            if tokens[0].lower() in EXIT_WORDS:
                break

            # 延迟导入，避免 cli 与 tui 互相导入形成循环
            from .cli import main as cli_main, strip_program_name

            # 用户可能在提示符后面连程序名一起敲（"linxia -login douyin"）
            tokens = strip_program_name(tokens)
            if not tokens:
                continue

            # 命令回显在提示符那一行，换行后再开始输出，视觉上更清楚
            sys.stdout.write("\n")
            sys.stdout.flush()

            try:
                # help_on_error=False：界面里敲错命令只报错，不倒整屏帮助
                cli_main(tokens, help_on_error=False)
            except KeyboardInterrupt:
                console.blank()
                console.warn("任务已中断")
            except SystemExit:

                pass

            # 执行完直接回到提示符：不重画界面、不再要求按回车
            console.blank()
            sys.stdout.flush()
    except _ExitInterface:
        pass

    clear_or_separate(ansi_ready)
    console.info("已退出 LinXia 界面，再见")
    return 0
