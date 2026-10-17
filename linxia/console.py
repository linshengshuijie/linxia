"""终端输出模块"""

from __future__ import annotations

import os
import sys
import threading
import time
import unicodedata
from typing import Optional

# 终端能力检测

# Windows 控制台启用 VT 处理需要的标志位
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
STD_OUTPUT_HANDLE = -11


def enable_ansi() -> bool:
    """尝试打开当前终端的 ANSI 转义支持。"""
    try:
        if not sys.stdout.isatty():
            return False
    except Exception:
        return False

    if os.name != "nt":
        return True

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:
        return False


# 颜色能力只探测一次，后面所有进度条共用结论
_COLOR_READY: Optional[bool] = None


def color_enabled() -> bool:
    """当前终端能不能上色。第一次调用时探测并缓存结果。"""
    global _COLOR_READY
    if _COLOR_READY is None:
        _COLOR_READY = enable_ansi()
    return _COLOR_READY


# ANSI 颜色码。只用于进度条，其余输出保持纯文本。
COLOR_RED = "\x1b[31m"
COLOR_GREEN = "\x1b[32m"
COLOR_RESET = "\x1b[0m"


def colorize(text: str, code: str, enabled: bool) -> str:
    """给一段文字上色。enabled 为假时原样返回。"""
    if not enabled or not code:
        return text
    return f"{code}{text}{COLOR_RESET}"

# 输出编码处理

# 中文 Windows 的默认代码页是 GBK。直接输出到真实控制台时，Python 会走
if not sys.stdout.isatty():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # 部分环境下 stdout 不支持重新配置，忽略即可，不影响主流程
        pass

# 输出宽度上限。超过这个宽度的进度条行会被裁剪，避免换行把界面刷乱。
MAX_LINE_WIDTH = 100

# 终端输出锁。多任务并发打印时，防止两行日志互相插进对方中间。
_PRINT_LOCK = threading.Lock()

# 当前正在单行刷新的进度条。
_ACTIVE_BAR: "Optional[ProgressBar]" = None


def register_active_bar(bar: "ProgressBar") -> None:
    """登记当前活跃的进度条，供日志输出时避让。"""
    global _ACTIVE_BAR
    _ACTIVE_BAR = bar


def unregister_active_bar(bar: "ProgressBar") -> None:
    """进度条结束后注销。只有自己还是活跃的那个才注销，避免误清别的任务。"""
    global _ACTIVE_BAR
    if _ACTIVE_BAR is bar:
        _ACTIVE_BAR = None


def _erase_active_bar_line() -> str:
    """返回一段用于擦除当前进度条行的控制序列。"""
    bar = _ACTIVE_BAR
    if bar is None or not bar.interactive or not bar.line_visible:
        return ""
    return "\r" + " " * MAX_LINE_WIDTH + "\r"


# 基础工具


def _write(line: str, end: str = "\n") -> None:
    """真正落盘输出的函数。"""
    with _PRINT_LOCK:
        prefix = _erase_active_bar_line()
        try:
            sys.stdout.write(prefix + line + end)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            safe = line.encode(encoding, errors="replace").decode(encoding, errors="replace")
            sys.stdout.write(prefix + safe + end)
        sys.stdout.flush()


def display_width(text: str) -> int:
    """计算字符串在终端里占多少列。"""
    width = 0
    for character in text:
        if unicodedata.combining(character):
            continue
        if unicodedata.east_asian_width(character) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def pad_to(text: str, width: int) -> str:
    """按显示宽度右补空格，保证中英混排也能对齐。"""
    return text + " " * max(width - display_width(text), 0)


def format_bytes(size: float) -> str:
    """把字节数格式化成人类可读的字符串，例如 1.4MB、820KB。"""
    if size is None or size <= 0:
        return "0B"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return f"{int(value)}B"
    return f"{value:.1f}{units[index]}"


def format_duration(seconds: float) -> str:
    """把秒数格式化成 分:秒 或 时:分:秒。"""
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


# 分等级日志


def info(message: str) -> None:
    """普通流程信息。"""
    _write(message)


def stage(message: str) -> None:
    """阶段性提示，用于标记任务进入了下一步。"""
    _write(message)


def detail(message: str, indent: int = 2) -> None:
    """补充细节，缩进显示，从视觉上从属于上一条主信息。"""
    _write(" " * indent + message)


def ok(message: str) -> None:
    """成功结果。"""
    _write(message)


def warn(message: str) -> None:
    """需要用户注意但不影响继续执行的问题。"""
    _write(f"警告：{message}")


def error(message: str) -> None:
    """错误。调用方决定是继续还是终止。"""
    _write(f"错误：{message}")


def blank() -> None:
    """输出一个空行，用于分隔不同任务。"""
    _write("")


def raw(text: str) -> None:
    """原样输出一段多行文本，不做任何前缀加工。用于打印帮助信息。"""
    _write(text)


def rule(char: str = "-", width: int = 60) -> None:
    """输出一条分隔线。只接受单个 ASCII 字符，防止传入奇怪符号。"""
    if not char or len(char) != 1 or ord(char) > 127:
        char = "-"
    _write(char * width)


class ProgressBar:
    """单行刷新的文本进度条。"""

    def __init__(
        self,
        label: str = "进度",
        total: int = 0,
        width: int = 26,
        indent: str = "  ",
        stream=None,
    ) -> None:
        self.label = label
        self.total = max(int(total or 0), 0)
        self.width = max(int(width), 8)
        self.indent = indent
        self.stream = stream or sys.stdout
        self.done = 0
        self.start_time = time.monotonic()
        self.last_time = self.start_time
        self.last_done = 0
        self.speed = 0.0
        self.closed = False
        # 结束后进度条要变绿。不能用"比例有没有到 100%"来判断
        self.finished = False
        self._last_render = 0.0
        self._last_percent_printed = -1
        self._line_visible = False
        self._last_line_length = 0
        self.use_color = color_enabled()
        # 交互式终端才做单行刷新
        try:
            self.interactive = bool(self.stream.isatty())
        except Exception:
            self.interactive = False

    @property
    def line_visible(self) -> bool:
        """当前是否已经有一行进度条留在屏幕上（供日志输出避让判断）。"""
        return self._line_visible


    def _compose(self) -> str:
        """拼出当前这一行的完整文本。"""
        elapsed = max(time.monotonic() - self.start_time, 0.001)
        speed = self.speed if self.speed > 0 else (self.done / elapsed)

        if self.total > 0:
            ratio = min(self.done / self.total, 1.0)
            filled = int(round(self.width * ratio))
            # 收尾时一律算完成，避免"进度到 100% 但还没 close"导致颜色不一致
            bar_text = self._bar_body(filled, self.finished or ratio >= 1.0)
            percent = ratio * 100
            remaining = (self.total - self.done) / speed if speed > 0 else 0
            text = (
                f"{self.indent}{self.label} {bar_text} "
                f"{percent:5.1f}%  {format_bytes(self.done)}/{format_bytes(self.total)}  "
                f"{format_bytes(speed)}/s  剩余 {format_duration(remaining)}"
            )
        else:
            # 总量未知（合流、拉流）：只能靠 finished 标记决定颜色
            filled = self.width if self.finished else int(self.width * 0.35)
            bar_text = self._bar_body(filled, self.finished)
            text = (
                f"{self.indent}{self.label} {bar_text} "
                f"已接收 {format_bytes(self.done)}  "
                f"{format_bytes(speed)}/s  用时 {format_duration(elapsed)}"
            )

        return text

    def _bar_body(self, filled: int, finished: bool) -> str:
        """生成进度条本体：[####----]。"""
        body = "#" * max(min(filled, self.width), 0) + "-" * max(self.width - filled, 0)
        plain = f"[{body}]"
        if not self.use_color:
            return plain
        return colorize(plain, COLOR_GREEN if finished else COLOR_RED, True)

    def _emit_line(self, text: str, final: bool = False) -> None:
        """刷一行进度条。"""
        if not self.interactive:
            self._emit(text, "\n")
            return
        padding = ""
        if len(text) < self._last_line_length:
            padding = " " * (self._last_line_length - len(text))
        self._emit("\r" + text + padding, "\n" if final else "")
        self._last_line_length = len(text)

    def _emit(self, text: str, end: str) -> None:
        with _PRINT_LOCK:
            try:
                self.stream.write(text + end)
            except UnicodeEncodeError:
                encoding = getattr(self.stream, "encoding", None) or "utf-8"
                safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
                self.stream.write(safe + end)
            self.stream.flush()


    def set_total(self, total: int) -> None:
        """补上总量。服务端一开始没给 content-length 时可以后补。"""
        if total and total > 0:
            self.total = int(total)
            self.render(force=True)

    def update(self, advance: int = 0, total: Optional[int] = None) -> None:
        """累加已完成的字节数并刷新显示。"""
        if total:
            self.set_total(total)
        if advance:
            self.done += int(advance)
        # 每 0.15 秒刷新一次，避免刷新过密拖慢下载
        now = time.monotonic()
        if now - self._last_render >= 0.15:
            self.render()

    def render(self, force: bool = False) -> None:
        """刷新进度条显示。"""
        if self.closed:
            return
        now = time.monotonic()

        # 用两次采样的差值估算瞬时速度，再做指数平滑，避免数字乱跳
        delta_time = now - self.last_time
        if delta_time >= 0.3:
            delta_bytes = self.done - self.last_done
            instant = delta_bytes / delta_time if delta_time > 0 else 0.0
            if instant > 0:
                self.speed = instant if self.speed <= 0 else self.speed * 0.7 + instant * 0.3
            self.last_time = now
            self.last_done = self.done

        text = self._compose()
        if self.interactive:
            register_active_bar(self)
            self._emit_line(text)
            self._line_visible = True
        else:
            # 非交互式：每跨越 10% 打印一行，避免刷屏
            if self.total > 0:
                percent_step = int(self.done / self.total * 100) // 10
                if force or percent_step > self._last_percent_printed:
                    self._last_percent_printed = percent_step
                    self._emit(text, "\n")
        self._last_render = now

    def close(self, note: str = "") -> None:
        """结束进度条，把最后一行固定下来。"""
        if self.closed:
            return
        self.closed = True
        # 先立完成标记再渲染，最后一行才会是绿色
        self.finished = True
        if self.total > 0:
            self.done = max(self.done, self.total)

        # 非交互式输出时，如果最后一个 10% 分段已经打印过，就没必要再打一次
        if (
            not self.interactive
            and not self._line_visible
            and not note
            and self.total > 0
            and self._last_percent_printed >= 10
        ):
            return

        text = self._compose()
        if note:
            text = f"{text}  {note}"
        if self._line_visible:
            self._emit_line(text, final=True)
        else:
            self._emit(text, "\n")
        # 注销自己，让后续日志不必再为这一行让位
        unregister_active_bar(self)


class StepTimer:
    """不确定时长的步骤计时器，配合进度条显示"已用时"。"""

    def __init__(self, label: str, indent: str = "  ") -> None:
        self.bar = ProgressBar(label=label, total=0, indent=indent)

    def tick(self, advance: int = 0) -> None:
        """推进一次。advance 可以是本轮新增的字节数，没数据就传 0。"""
        self.bar.update(advance)

    def close(self, note: str = "") -> None:
        """结束并输出最终用时。"""
        self.bar.close(note=note)

    @property
    def elapsed(self) -> float:
        """已用时间（秒）。"""
        return time.monotonic() - self.bar.start_time


class StatusLine:
    """单行原地刷新的状态文字。"""

    def __init__(self, label: str = "", indent: str = "  ", stream=None) -> None:
        self.label = label
        self.indent = indent
        self.stream = stream or sys.stdout
        self._line_visible = False
        self._closed = False
        try:
            self.interactive = bool(self.stream.isatty())
        except Exception:
            self.interactive = False

    @property
    def line_visible(self) -> bool:
        """当前是否有一行留在屏幕上，供日志输出避让判断。"""
        return self._line_visible

    def _emit(self, text: str, end: str) -> None:
        with _PRINT_LOCK:
            try:
                self.stream.write(text + end)
            except UnicodeEncodeError:
                encoding = getattr(self.stream, "encoding", None) or "utf-8"
                safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
                self.stream.write(safe + end)
            self.stream.flush()

    def update(self, text: str = "") -> None:
        """刷新这一行显示的文字。"""
        if self._closed:
            return
        body = f"{self.indent}{self.label}"
        if text:
            body = f"{body} {text}"
        body = body[:MAX_LINE_WIDTH]
        if self.interactive:
            register_active_bar(self)
            self._emit("\r" + body.ljust(MAX_LINE_WIDTH), "")
            self._line_visible = True
        else:
            # 非交互式终端不适合原地刷新，只在第一次输出一行当前状态
            if not self._line_visible:
                self._emit(body, "\n")
                self._line_visible = True

    def close(self, note: str = "") -> None:
        """结束状态行。"""
        if self._closed:
            return
        self._closed = True
        if self._line_visible and self.interactive:
            # 把这一行擦掉，避免和后面的结果行叠在一起
            self._emit("\r" + " " * MAX_LINE_WIDTH + "\r", "")
        self._line_visible = False
        unregister_active_bar(self)


class CountBar:
    """按"第几项 / 共几项"推进的进度条。"""

    def __init__(
        self,
        label: str = "下载",
        total: int = 0,
        width: int = 22,
        indent: str = "  ",
        stream=None,
    ) -> None:
        self.label = label
        self.total = max(int(total or 0), 1)
        self.done = 0
        self.bytes_done = 0
        self.width = max(int(width), 8)
        self.indent = indent
        self.stream = stream or sys.stdout
        self.start_time = time.monotonic()
        self._line_visible = False
        self._closed = False
        self.finished = False
        self._last_line_length = 0
        self.use_color = color_enabled()
        try:
            self.interactive = bool(self.stream.isatty())
        except Exception:
            self.interactive = False

    @property
    def line_visible(self) -> bool:
        """当前是否有一行留在屏幕上。"""
        return self._line_visible

    def _compose(self) -> str:
        elapsed = max(time.monotonic() - self.start_time, 0.001)
        ratio = min(self.done / self.total, 1.0)
        filled = int(round(self.width * ratio))
        if self.finished:
            # 收尾时补满，视觉上是一个完成的条
            filled = self.width
        body = "#" * max(min(filled, self.width), 0) + "-" * max(self.width - filled, 0)
        plain = f"[{body}]"
        bar_text = plain
        if self.use_color:
            complete = self.finished or ratio >= 1.0
            bar_text = colorize(plain, COLOR_GREEN if complete else COLOR_RED, True)

        speed = self.bytes_done / elapsed
        text = (
            f"{self.indent}{self.label} {bar_text} "
            f"{self.done}/{self.total}  {format_bytes(self.bytes_done)}  "
            f"{format_bytes(speed)}/s"
        )
        return text

    def _emit_line(self, text: str, final: bool = False) -> None:
        """刷一行，交互式下顺手擦掉上一次更长的残留字符。"""
        if not self.interactive:
            self._emit(text, "\n")
            return
        padding = ""
        if len(text) < self._last_line_length:
            padding = " " * (self._last_line_length - len(text))
        self._emit("\r" + text + padding, "\n" if final else "")
        self._last_line_length = len(text)

    def _emit(self, text: str, end: str) -> None:
        with _PRINT_LOCK:
            try:
                self.stream.write(text + end)
            except UnicodeEncodeError:
                encoding = getattr(self.stream, "encoding", None) or "utf-8"
                safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
                self.stream.write(safe + end)
            self.stream.flush()

    def step(self, bytes_done: int = 0) -> None:
        """完成一项，并累加这一项写入了多少字节。"""
        if self._closed:
            return
        self.done += 1
        self.bytes_done += max(int(bytes_done or 0), 0)
        text = self._compose()
        if self.interactive:
            register_active_bar(self)
            self._emit_line(text)
            self._line_visible = True
            return

        # 非交互式（日志、管道）：每 25% 打一行就够了，不必每张图打一行
        step_every = max(1, self.total // 4)
        if self.done >= self.total or self.done % step_every == 0:
            self._emit(text, "\n")
            self._line_visible = True

    def close(self, note: str = "") -> None:
        """结束并固定最后一行。"""
        if self._closed:
            return
        self._closed = True
        # 先立完成标记再渲染，最后一行才会是绿色
        self.finished = True
        text = self._compose()
        if note:
            text = f"{text}  {note}"

        if self.interactive:
            self._emit_line(text, final=True)
        elif not self._line_visible or note:
            # 非交互式下，最后一步通常已经打过一行了，这里不重复；
            self._emit(text, "\n")
        unregister_active_bar(self)
