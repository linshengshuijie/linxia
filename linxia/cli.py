"""命令行入口模块"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

from . import __version__, console
from .config import (
    DEFAULT_JOBS,
    DOWNLOAD_DIR,
    SNIFF_TIMEOUT_S,
)
from .login import (
    DEFAULT_LOGIN_TIMEOUT_S,
    LoginOptions,
    reset_session,
    run_login,
    show_sessions,
)
from .platforms import (
    all_platforms,
    get_platform,
    is_url_like,
    normalize_url,
)
from .tasks import TaskOptions, run_sniff_tasks, summarize_results

# 模式常量，避免在代码里到处写裸字符串
MODE_HELP = "help"
MODE_VERSION = "version"
MODE_SESSIONS = "sessions"
MODE_LOGIN = "login"
MODE_RESET = "reset"
MODE_SNIFF = "sniff"
MODE_WEBSITE = "website"
MODE_UI = "ui"

# 退出码约定
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

# 登录指令的各种写法。
LOGIN_KEYWORDS = {
    "join",
    "login",
    "logon",
    "signin",
    "登录",
    "登陆",
    "登入",
}

# 删除凭据指令的各种写法
RESET_KEYWORDS = {
    "reset",
    "logout",
    "signout",
    "登出",
    "注销",
    "重置",
}

# 进入图形界面的写法
UI_KEYWORDS = {"ui", "tui", "console", "界面", "面板"}

# "用已保存的登录态开个浏览器自己逛"的写法
WEBSITE_KEYWORDS = {"website", "web", "site", "browse", "网页", "浏览"}


PROGRAM_NAMES = {"linxia", "linxia.exe", "linxia.cmd", "linxia.bat", "linxia.py"}


def strip_program_name(tokens: List[str]) -> List[str]:
    """去掉参数开头可能出现的程序名。"""
    if tokens and tokens[0].lower() in PROGRAM_NAMES:
        return tokens[1:]
    return tokens


# 帮助文本


def build_help_text() -> str:
    """生成帮助文本。"""
    usage_rows = [
        ("linxia", "进入交互界面"),
        ('linxia "链接" [更多链接 ...]', "通用嗅探下载（自动识别平台）"),
        ('linxia -平台名 "链接" [...]', "指定平台嗅探"),
        ("linxia -login <平台名|网址>", "登录平台"),
        ("linxia -reset <平台名|网址>", "清除登录状态"),
        ("linxia -website [平台名|网址]", "用已保存的登录态打开浏览器"),
        ("linxia -sessions", "查看已保存的登录态"),
        ("linxia -help", "显示本帮助"),
        ("linxia -version", "显示版本号"),
    ]
    usage_width = max(console.display_width(left) for left, _ in usage_rows)
    usage_block = "\n".join(
        "  " + console.pad_to(left, usage_width + 2) + right for left, right in usage_rows
    )

    option_rows = [
        ("-o, -output 目录", f"保存目录，默认 {DOWNLOAD_DIR}"),
        ("-timeout 秒", "单条链接嗅探上限；-website 模式下是浏览时长"),
        ("-jobs 数量", f"同时处理的链接数，默认 {DEFAULT_JOBS}"),
        ("-show", "显示浏览器窗口"),
        ("-verbose", "打印嗅探细节"),
    ]
    option_width = max(console.display_width(left) for left, _ in option_rows)
    option_block = "\n".join(
        "  " + console.pad_to(left, option_width + 2) + right for left, right in option_rows
    )

    platform_rows = [(f"-{platform.key}", platform.name) for platform in all_platforms()]
    platform_width = max(console.display_width(left) for left, _ in platform_rows)
    platform_block = "\n".join(
        "  " + console.pad_to(left, platform_width + 2) + right
        for left, right in platform_rows
    )

    return f"""
LinXia  版本 {__version__}

用法
{usage_block}

选项
{option_block}

平台
{platform_block}

示例
  linxia -login douyin
  linxia "https://www.douyin.com/video/7412345678901234567"
  linxia -bilibili "https://www.bilibili.com/video/BV1xx411c7mD"
  linxia -jobs 3 "链接1" "链接2"
""".strip("\n")

def print_help() -> None:
    """输出帮助文本。"""
    console.raw(build_help_text())


# 参数解析


@dataclass
class ParsedArguments:
    """解析结果。"""

    mode: str = MODE_HELP
    targets: List[str] = field(default_factory=list)

    original_targets: List[str] = field(default_factory=list)
    platform_key: str = ""
    output_dir: str = DOWNLOAD_DIR
    sniff_timeout: float = SNIFF_TIMEOUT_S
    # 用户是否显式写了 -timeout。
    timeout_explicit: bool = False
    jobs: int = DEFAULT_JOBS
    show_browser: bool = False
    verbose: bool = False
    force_ui: bool = False
    # 用户输入的参数里有多少段文字不是链接（粘贴分享文案时常见）
    ignored_count: int = 0
    error: str = ""
    hint: str = ""

    def to_task_options(self) -> TaskOptions:
        """转成抓取任务需要的选项对象。"""
        return TaskOptions(
            output_dir=self.output_dir,
            sniff_timeout=self.sniff_timeout,
            jobs=max(1, self.jobs),
            show_browser=self.show_browser,
            platform_key=self.platform_key,
            verbose=self.verbose,
        )


# 需要读取一个值的选项：选项名 -> ParsedArguments 上的字段名
VALUE_OPTIONS = {
    "o": "output_dir",
    "output": "output_dir",
    "out": "output_dir",
    "timeout": "sniff_timeout",
    "jobs": "jobs",
    "j": "jobs",
}

# 开关型选项：选项名 -> ParsedArguments 上的字段名
FLAG_OPTIONS = {
    "show": "show_browser",
    "verbose": "verbose",
    "v": "verbose",
}


def describe_bad_target(token: str) -> str:
    """针对"看起来不是网址"的参数给出一句具体的纠正建议。"""
    platform = get_platform(token)
    if platform is not None:
        return (
            f'"{token}" 是平台简写，不能单独当链接用。'
            f'用法：linxia -{platform.key} "视频链接" 嗅探下载，'
            f"或 linxia -login {platform.key} 登录该平台"
        )
    if token.lower() in LOGIN_KEYWORDS:
        return (
            f'"{token}" 是登录指令，后面要跟平台名或链接。'
            f"正确写法：linxia -login douyin"
        )
    return f'"{token}" 看起来不是网页链接。链接一般形如 https://www.example.com/video/123'


def parse_arguments(argv: List[str]) -> ParsedArguments:
    """解析命令行参数。"""
    parsed = ParsedArguments()

    if not argv:
        # 不带任何参数时进入交互界面，终端不支持则退回打印帮助
        parsed.mode = MODE_UI
        return parsed

    index = 0
    login_mode = False
    reset_mode = False
    website_mode = False
    while index < len(argv):
        token = argv[index]
        index += 1

        if not token:
            continue

        lowered = token.lower()

        # 非选项：可能是登录指令，也可能是链接
        if not token.startswith("-"):
            if lowered in LOGIN_KEYWORDS:
                login_mode = True
                continue
            parsed.targets.append(token)
            continue

        name = token.lstrip("-").lower()

        # 帮助与版本类选项
        if name in ("help", "h", "?", "帮助"):
            parsed.mode = MODE_HELP
            return parsed
        if name in ("version", "ver", "版本"):
            parsed.mode = MODE_VERSION
            return parsed

        # 登录模式（-login / --login / -join 都认）
        if name in LOGIN_KEYWORDS:
            login_mode = True
            continue

        # 删除凭据模式（-reset / -logout / -登出 都认）
        if name in RESET_KEYWORDS:
            reset_mode = True
            continue

        # 用已保存的登录态开浏览器（-website / -web / -浏览 都认）
        if name in WEBSITE_KEYWORDS:
            website_mode = True
            continue

        # 强制进入交互界面
        if name in UI_KEYWORDS:
            parsed.mode = MODE_UI
            parsed.force_ui = True
            return parsed

        # 查看凭据
        if name in ("sessions", "session", "list"):
            parsed.mode = MODE_SESSIONS
            return parsed

        # 需要取值的选项
        if name in VALUE_OPTIONS:
            if index >= len(argv):
                parsed.error = f"选项 -{name} 后面缺少值"
                return parsed
            raw_value = argv[index]
            index += 1
            if not apply_value_option(parsed, VALUE_OPTIONS[name], raw_value, name):
                return parsed
            continue

        # 开关型选项
        if name in FLAG_OPTIONS:
            setattr(parsed, FLAG_OPTIONS[name], True)
            continue

        # 平台简写
        platform = get_platform(name)
        if platform is not None:
            parsed.platform_key = platform.key
            continue

        parsed.error = f"无法识别的选项: {token}"
        parsed.hint = "输入 linxia -help 查看全部命令"
        return parsed

    if reset_mode:
        parsed.mode = MODE_RESET
        if not parsed.targets:
            parsed.error = "删除指令后面需要跟一个平台名或链接"
            parsed.hint = "例如：linxia -reset douyin"
        elif len(parsed.targets) > 1:
            parsed.error = "一次只能删除一个平台的凭据"
            parsed.hint = "例如：linxia -reset douyin"
        return parsed

    if website_mode:
        parsed.mode = MODE_WEBSITE
        # 目标可以不写（开个空白页），也可以给平台名或网址
        if len(parsed.targets) > 1:
            parsed.error = "浏览模式一次只能指定一个目标"
            parsed.hint = "例如：linxia -website douyin"
        return parsed

    if login_mode:
        parsed.mode = MODE_LOGIN
        if not parsed.targets:
            parsed.error = "登录指令后面需要跟一个平台名或链接"
            parsed.hint = "例如：linxia -login douyin"
        elif len(parsed.targets) > 1:
            parsed.error = "一次只能登录一个平台"
            parsed.hint = "例如：linxia -login douyin"
        return parsed

    parsed.mode = MODE_SNIFF
    if not parsed.targets:
        parsed.error = "没有提供链接"
        parsed.hint = '例如：linxia "https://www.douyin.com/video/123"'
        return parsed

    # 把目标分成两类：像链接的，和不像链接的。
    url_targets = [item for item in parsed.targets if is_url_like(item)]
    junk_targets = [item for item in parsed.targets if not is_url_like(item)]

    if not url_targets:
        # 一个链接都没有，按原来的方式报错并给出纠正建议
        parsed.error = describe_bad_target(parsed.targets[0])
        parsed.hint = "输入 linxia -help 查看全部命令"
        return parsed

    parsed.original_targets = list(parsed.targets)
    parsed.ignored_count = len(junk_targets)

    parsed.targets = [normalize_url(item) for item in url_targets]

    return parsed


def apply_value_option(parsed: ParsedArguments, field_name: str, raw_value: str, option_name: str) -> bool:
    """把选项的值写进解析结果，顺带做类型校验。"""
    if field_name in ("sniff_timeout", "jobs"):
        try:
            number = float(raw_value)
        except ValueError:
            parsed.error = f"选项 -{option_name} 的值必须是数字，收到的是: {raw_value}"
            return False
        if number <= 0:
            parsed.error = f"选项 -{option_name} 的值必须大于 0"
            return False
        if field_name == "jobs":
            parsed.jobs = int(number)
        else:
            parsed.sniff_timeout = number
            parsed.timeout_explicit = True
        return True

    if field_name == "output_dir":
        if not raw_value.strip():
            parsed.error = "-output 的目录不能为空"
            return False
        # 相对路径按当前工作目录展开，方便用户直接写 -o videos
        parsed.output_dir = os.path.abspath(os.path.expanduser(raw_value))
        return True

    return True


# 各模式的执行入口


def announce_extracted_links(parsed: ParsedArguments) -> None:
    """如果用户粘进来的是整段分享文案，告诉他实际用哪个链接。"""
    originals = parsed.original_targets or parsed.targets
    for target in parsed.targets:
        if target in originals:
            continue
        console.info(f"从输入内容里提取到链接: {target}")

    if parsed.ignored_count:
        console.warn(f"输入里有 {parsed.ignored_count} 段文字不是链接，已忽略")


def run_sniff_mode(parsed: ParsedArguments) -> int:
    """执行嗅探下载模式。"""
    options = parsed.to_task_options()
    announce_extracted_links(parsed)
    try:
        results = asyncio.run(run_sniff_tasks(parsed.targets, options))
    except KeyboardInterrupt:
        console.blank()
        console.warn("已手动中断，浏览器已关闭，未完成的下载会留下 .part 临时文件，可以直接删除")
        return EXIT_FAILED
    return summarize_results(results, quiet=not parsed.verbose)


def run_login_mode(parsed: ParsedArguments) -> int:
    """执行临时浏览器登录模式。"""
    if parsed.error:
        console.error(parsed.error)
        if parsed.hint:
            console.info(parsed.hint)
        return EXIT_USAGE
    announce_extracted_links(parsed)
    target = parsed.targets[0]
    # 登录超时沿用 -timeout，但只在用户确实写了这个选项时才沿用。
    if parsed.timeout_explicit:
        login_timeout = max(parsed.sniff_timeout, 10.0)
    else:
        login_timeout = DEFAULT_LOGIN_TIMEOUT_S
    login_options = LoginOptions(timeout=login_timeout)
    try:
        saved = asyncio.run(run_login(target, login_options))
    except KeyboardInterrupt:
        console.blank()
        console.warn("登录流程被手动中断，凭据没有保存，临时浏览器数据已清理")
        return EXIT_FAILED
    return EXIT_OK if saved else EXIT_FAILED


def run_reset_mode(parsed: ParsedArguments) -> int:
    """执行删除登录凭据模式。"""
    if parsed.error:
        console.error(parsed.error)
        if parsed.hint:
            console.info(parsed.hint)
        return EXIT_USAGE
    removed = reset_session(parsed.targets[0])
    return EXIT_OK if removed else EXIT_FAILED


def run_website_mode(parsed: ParsedArguments) -> int:
    """执行"用已保存的登录态开浏览器"模式。"""
    if parsed.error:
        console.error(parsed.error)
        if parsed.hint:
            console.info(parsed.hint)
        return EXIT_USAGE

    from .browse import BrowseOptions, run_website

    # timeout 在浏览模式下表示"最多开多久"，方便自动化测试或者定时收工
    if parsed.timeout_explicit:
        options = BrowseOptions(max_seconds=parsed.sniff_timeout)
    else:
        options = BrowseOptions()

    target = parsed.targets[0] if parsed.targets else ""
    try:
        opened = asyncio.run(run_website(target, options))
    except KeyboardInterrupt:
        console.blank()
        console.warn("浏览被手动中断，临时浏览器数据已清理")
        return EXIT_FAILED
    return EXIT_OK if opened else EXIT_FAILED


def run_ui_mode(force: bool = False) -> int:
    """进入交互界面。"""
    from .tui import run_tui

    result = run_tui(force=force)
    if result is None:
        console.warn("当前终端不支持交互界面，改为显示命令帮助")
        console.blank()
        print_help()
        return EXIT_OK
    return result


def main(argv: Optional[List[str]] = None, help_on_error: bool = True) -> int:
    """程序主入口。"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    # 先剥掉误写进来的程序名，再进入正常解析
    parsed = parse_arguments(strip_program_name(arguments))

    # 解析阶段的错误统一在这里收口：说清楚问题，再给一条纠正建议
    if parsed.error:
        console.error(parsed.error)
        if parsed.hint:
            console.info(parsed.hint)
        if help_on_error:
            console.blank()
            print_help()
        return EXIT_USAGE

    if parsed.mode == MODE_UI:
        # 用户显式要求界面时强制渲染，否则由终端能力决定
        return run_ui_mode(force=parsed.force_ui)

    if parsed.mode == MODE_HELP:
        print_help()
        return EXIT_OK

    if parsed.mode == MODE_VERSION:
        console.info(f"LinXia 版本 {__version__}")
        return EXIT_OK

    if parsed.mode == MODE_SESSIONS:
        show_sessions()
        return EXIT_OK

    if parsed.mode == MODE_LOGIN:
        return run_login_mode(parsed)

    if parsed.mode == MODE_RESET:
        return run_reset_mode(parsed)

    if parsed.mode == MODE_WEBSITE:
        return run_website_mode(parsed)

    return run_sniff_mode(parsed)


if __name__ == "__main__":
    sys.exit(main())
