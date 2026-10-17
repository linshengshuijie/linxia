"""浏览器登录模块"""

from __future__ import annotations

import json

import asyncio
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import async_playwright

from . import console
from .browser import STEALTH_SCRIPT
from .human import detect_login_entries
from .config import (
    BROWSER_ARGS,
    BROWSER_LOCALE,
    BROWSER_TIMEZONE,
    DEFAULT_USER_AGENT,
    DEFAULT_VIEWPORT,
)
from .platforms import (
    extract_domain_key,
    get_platform,
    is_url_like,
    normalize_url,
    platform_by_url,
    platform_summary,
)
from .sessions import (
    delete_profile,
    find_session_file,
    delete_sessions,
    profile_dir_for,
    profile_exists,
    session_path_for,
)

# 默认登录等待时间（秒）。扫码登录通常一两分钟内能完成，给足十分钟。
DEFAULT_LOGIN_TIMEOUT_S = 600.0

# 检测登录凭据的轮询间隔（秒）
AUTH_POLL_INTERVAL_S = 1.5

# 记录匿名基线前先等多久（秒）。
AUTH_BASELINE_DELAY_S = 2.0

# 检测到登录后，导出凭据前再等多久（秒）。
AUTH_SETTLE_S = 2.0

# 匿名访客本来就会写上、跟登录无关的 cookie 名字。
ANONYMOUS_COOKIE_NAMES = {
    # 通用统计
    "_ga", "_gid", "_gl", "_fbp", "_clck", "_clsk",
    # 字节系（抖音、快手、西瓜共用一批匿名标识）
    "did", "didv", "ttwid", "tt_webid", "tt_webid_v2", "install_id",
    "odin_tt", "uid_tt", "tt_scid", "s_v_web_id", "passport_csrf_token",
    "passport_csrf_token_default", "clientid", "buvid3", "buvid4", "buvid3rd",
    # 抖音网页版在页面加载时就会写的一批"设备指纹"cookie。
    "hevc_supported", "device_web_cpu_core", "device_web_memory_size",
    "architecture", "is_support_rtm_web_ts", "dy_swidth", "dy_sheight",
    "strategyabtestkey", "enter_pc_once", "is_dbsc", "biz_trace_id",
    "x-web-secsdk-uid", "uifid", "uifid_temp", "__security_mc_1_s_sdk_crypt_sdk",
    "kpf", "kpn", "df", "kwpsec_sid", "kwpsec_cid", "kwssectoken", "clientid_ks",
    "kwfv1", "kwpsecproductname", "kwscode",
    # 小红书 / 微博 / B 站
    "a1", "webid", "webid_ts", "fid", "buvid", "b_nut", "b_lsid",
    # 界面偏好
    "volume", "language", "theme", "is_staff",
    "ktrace-context", "dy_swidth",
}


def _looks_like_token(value: str) -> bool:
    """粗略判断一个 cookie 值像不像"登录令牌"。"""
    text = value or ""
    if len(text) < 16:
        return False
    has_digit = any(character.isdigit() for character in text)
    has_alpha = any(character.isalpha() for character in text)
    return has_digit and has_alpha


# 登录 cookie 名字里常见的词根。
AUTH_NAME_HINTS = (
    "session",
    "token",
    "auth",
    "login",
    "passport",
    "sid",
    "uid",
    "user",
    "ticket",
    "jwt",
)


def _looks_like_auth_name(name: str) -> bool:
    """cookie 名字里是否带登录相关词根。"""
    lowered = (name or "").lower()
    return any(hint in lowered for hint in AUTH_NAME_HINTS)


def _generic_login_evidence(changed_pairs: List[tuple]) -> List[str]:
    """给"没有登记关键字段的平台"用的通用判断：从变化的 cookie 里挑出像登录凭据的。"""
    evidence: List[str] = []
    for name, value in changed_pairs:
        if _looks_like_anonymous_noise(name):
            continue
        if not _looks_like_auth_name(name):
            continue
        if not _looks_like_token(value):
            continue
        evidence.append(name)
    return evidence


@dataclass
class LoginOptions:
    """登录流程的可选参数。"""

    timeout: float = DEFAULT_LOGIN_TIMEOUT_S
    show_browser: bool = True


class EnterWatcher:
    """后台监听"用户是否按了回车"。"""

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._pressed_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.active = False


    def _loop_windows(self) -> None:
        """Windows：轮询控制台按键，只认真正的回车。"""
        import msvcrt

        # 清掉启动之前残留在缓冲区里的按键
        try:
            while msvcrt.kbhit():
                msvcrt.getwch()
        except Exception:
            return

        # 只把真正的回车当成确认
        while not self._stop_event.is_set():
            try:
                if msvcrt.kbhit():
                    key = msvcrt.getwch()
                    if key == "\r":
                        self._pressed_event.set()
                        return
            except Exception:
                return
            time.sleep(0.05)

    def _loop_posix(self) -> None:
        """类 Unix：用 select 判断有没有整行可以读。"""
        import select

        while not self._stop_event.is_set():
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.1)
                if ready:
                    sys.stdin.readline()
                    self._pressed_event.set()
                    return
            except Exception:
                return


    def start(self) -> bool:
        """启动监听。返回 False 表示当前不是交互式终端，无需监听回车。"""
        try:
            if not sys.stdin.isatty():
                return False
        except Exception:
            return False

        target = self._loop_windows if os.name == "nt" else self._loop_posix
        self._thread = threading.Thread(target=target, name="linxia-enter", daemon=True)
        self._thread.start()
        self.active = True
        return True

    @property
    def pressed(self) -> bool:
        """用户是否已经按过回车。"""
        return self._pressed_event.is_set()

    def stop(self) -> None:
        """停止监听并等线程退出，保证它不会再占用标准输入。"""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self.active = False


async def run_login(target: str, options: Optional[LoginOptions] = None) -> bool:
    """执行一次临时浏览器登录。"""
    options = options or LoginOptions()

    # 解析目标，得到要打开的地址
    platform = get_platform(target)
    if platform is not None:
        # 情况一：给的是内置平台名，直接打开该平台的登录页面
        target_url = platform.login_url
        display = f"{platform.name}（{platform.key}）"
    else:
        # 情况二：不是内置平台名，那就必须是一个网址，否则没有可以打开的页面
        if not is_url_like(target):
            console.error(f'"{target}" 既不是内置平台名，也不是一个网址')
            console.info(f"内置平台有：{platform_summary()}")
            console.info('要登录别的网站，请给出完整网址，例如：linxia -login "https://www.zhihu.com"')
            return False
        target_url = normalize_url(target)
        platform = platform_by_url(target_url)
        if platform is not None:
            display = f"{platform.name}（{platform.key}）"
        else:
            display = f"通用网页（{extract_domain_key(target_url)}）"

    session_file = session_path_for(platform, target_url)
    # 浏览器配置目录：像普通浏览器一样长期保留，登录态就是靠它延续的
    profile_dir = profile_dir_for(platform, target_url)

    console.rule()
    console.stage("浏览器登录")
    console.detail(f"目标: {display}")

    # 已经有了"同一台设备同一个浏览器"的配置目录时，先看看站点认不认。
    if profile_exists(platform, target_url):
        console.detail(f"浏览器配置目录: {profile_dir}")
        console.detail("正在用这个配置目录确认站点是否仍处于登录状态 ...")
        verified = await _verify_profile(platform, target_url, profile_dir)
        if verified is True:
            console.rule()
            console.ok(f"{display} 已经有可用的登录状态，不用重复登录")
            console.detail(f"浏览器配置目录: {profile_dir}")
            if platform is not None:
                console.info(f"想换账号，先执行 linxia -reset {platform.key} 再重新登录")
            console.rule()
            return True
        console.warn("本地配置目录还在，但站点没把它当成登录（可能已失效），继续走登录流程")

    # 没有配置目录、但有旧凭据文件时，先试着把旧凭据"迁进"配置目录。
    if not profile_exists(platform, target_url) and platform is not None:
        legacy = find_session_file(platform, target_url)
        if legacy:
            console.detail(f"发现旧凭据 {os.path.basename(legacy)}，尝试迁进浏览器配置目录 ...")
            if await _bootstrap_profile(platform, target_url, legacy, profile_dir):
                console.rule()
                console.ok(f"{display} 已有登录状态，已把旧凭据迁进浏览器配置目录")
                console.detail(f"浏览器配置目录: {profile_dir}")
                console.rule()
                return True
            console.detail("迁移后站点仍未认可，继续走登录流程")

    console.detail(f"入口地址: {target_url}")
    console.detail(f"浏览器配置目录: {profile_dir}（登录后保留，像普通浏览器一样复用）")
    console.detail(f"同时导出一份凭据文件: {session_file}（供抓取时装配）")
    # 把等待预算写出来，用户就知道窗口打开后为什么会停在那里，而不是以为卡住了
    console.detail(
        f"最长等待 {int(options.timeout)} 秒，检测到登录会自动提前结束；"
        "想手动结束就回到本窗口按回车"
    )
    console.rule()

    os.makedirs(profile_dir, exist_ok=True)
    saved = False

    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=not options.show_browser,
                args=list(BROWSER_ARGS),
                user_agent=DEFAULT_USER_AGENT,
                viewport=dict(DEFAULT_VIEWPORT),
                locale=BROWSER_LOCALE,
                timezone_id=BROWSER_TIMEZONE,
            )
            await context.add_init_script(STEALTH_SCRIPT)

            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:
                console.warn(f"页面加载提示: {str(exc).splitlines()[0][:100]}")

            console.info("请在弹出的浏览器窗口中完成登录（扫码、验证码、账号密码都可以）")
            console.info("登录成功后，程序会自动识别并保存凭据")
            console.info("如果没有自动识别，请回到本窗口按回车键手动确认保存")
            console.blank()

            auth_cookies = list(platform.auth_cookies) if platform else []
            auth_wanted = {name.lower() for name in auth_cookies}
            loop = asyncio.get_event_loop()

            # 先记一份"匿名基线"：页面刚打开、用户还没登录时，浏览器里已经有哪些 cookie
            await asyncio.sleep(AUTH_BASELINE_DELAY_S)
            baseline = await _all_cookie_snapshot(context)

            # 同时等两件事：检测到 cookie 变化，或者用户自己按回车确认。
            watcher = EnterWatcher()
            if not watcher.start():
                console.detail("当前输入不是交互式终端，改为只依赖自动检测和超时")

            confirmed_by_enter = False
            timed_out = False
            changed_all: List[str] = []
            meaningful: List[str] = []
            auth_changed: List[str] = []
            started = loop.time()
            deadline = started + max(options.timeout, 1.0)
            last_hint = started

            try:
                while True:
                    changed_pairs = await _changed_cookie_pairs(context, baseline)
                    changed_all = [name for name, _ in changed_pairs]
                    if changed_all:
                        # 判定逻辑
                        auth_changed = [name for name in changed_all if name.lower() in auth_wanted]
                        meaningful = _generic_login_evidence(changed_pairs) if not auth_wanted else []
                        if auth_changed or meaningful:
                            break
                    if watcher.pressed:
                        confirmed_by_enter = True
                        break
                    now = loop.time()
                    if now >= deadline:
                        timed_out = True
                        break
                    # 每 30 秒提醒一次，用户才知道程序还在等、以及可以按回车手动结束
                    if now - last_hint >= 30:
                        console.detail(
                            f"已等待 {now - started:.0f} 秒，仍在等待登录完成；"
                            "如果已经登录但没自动识别，请按回车键保存"
                        )
                        last_hint = now
                    await asyncio.sleep(AUTH_POLL_INTERVAL_S)
            finally:
                watcher.stop()

            # 检测到登录后，让站点把完整登录态写下来再导出
            if meaningful or auth_changed:
                await asyncio.sleep(AUTH_SETTLE_S)
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30_000)
                    await asyncio.sleep(AUTH_SETTLE_S)
                except Exception:
                    pass

            # 结束之后再看一次，给用户统计信息，也用于判断能不能覆盖旧凭据
            try:
                cookies = await context.cookies()
            except Exception:
                cookies = []
            cookie_names = {str(item.get("name", "")) for item in cookies}
            if not changed_all:
                changed_pairs = await _changed_cookie_pairs(context, baseline)
                changed_all = [name for name, _ in changed_pairs]
                auth_changed = [name for name in changed_all if name.lower() in auth_wanted]
                meaningful = _generic_login_evidence(changed_pairs) if not auth_wanted else []
            changed_names = meaningful or auth_changed

            if confirmed_by_enter:
                console.info("收到回车确认")
            elif timed_out:
                console.warn("等待超时")
            else:
                shown = ", ".join(changed_names[:4])
                console.ok(f"已检测到登录凭据（变化的 cookie: {shown}）")

            # 完全没检测到变化，而目标文件里已经有凭据 -> 必须问
            strong_evidence = bool(auth_changed)
            file_exists = os.path.exists(session_file)


            if timed_out and not changed_names:
                console.warn("等待超时，没有检测到登录，凭据未保存")
                await context.close()
                console.blank()
                return False

            can_write = True
            if file_exists and not strong_evidence:
                can_write = _confirm_overwrite(session_file, weak=bool(changed_names))

            if not can_write:
                console.warn("已取消保存，原有登录态保持不变")
                await context.close()
                console.blank()
                return False

            # 导出登录态
            try:
                os.makedirs(os.path.dirname(session_file), exist_ok=True)
                await context.storage_state(path=session_file)
                saved = True
            except Exception as exc:
                console.error(f"保存凭据失败: {exc}")

            console.detail(f"共保存 {len(cookies)} 条 cookie，覆盖 {len(cookie_names)} 个字段名")
            if auth_cookies and not auth_changed:
                console.detail("注：变化的不在平台预设的关键字段里，但登录态已按当前状态保存")

            await context.close()
    finally:
        # 浏览器配置目录要保留：它就是"普通浏览器的用户数据目录"
        console.detail(f"浏览器配置目录已保留: {profile_dir}")

    if saved:
        console.rule()
        console.ok(f"登录完成，凭据已保存: {session_file}")
        console.detail(f"浏览器配置目录: {profile_dir}")
        # 用同一个配置目录再问一次站点：这才代表"下次打开浏览器时的真实状态"
        verified = await _verify_profile(platform, target_url, profile_dir)
        if verified is True:
            console.info("已用同一个配置目录验证：网页显示为已登录，登录态会被保留")
        elif verified is False:
            console.warn("已保存凭据，但用配置目录打开首页仍显示登录入口")
            console.detail("可能这次登录没有真正完成，或者站点风控把这次会话判掉了")
            console.detail("想再试一次：linxia -reset " + (platform.key if platform else "平台名") + " 后重新登录")
        else:
            console.detail("未能自动验证登录态（平台没有登记首页，或者页面加载失败）")
        console.info("下次抓取该平台时会自动装配这份凭据；浏览用 linxia -website 平台名")
        console.rule()
    else:
        console.error("登录未完成，凭据没有保存")
    return saved


async def _bootstrap_profile(platform, url: str, session_file: str, profile_dir: str) -> bool:
    """把旧版留下的凭据文件"迁进"浏览器配置目录，返回站点是否认可。"""
    if not os.path.exists(session_file):
        return False

    try:
        with open(session_file, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return False

    cookies = payload.get("cookies") or []
    if not cookies:
        return False

    os.makedirs(profile_dir, exist_ok=True)
    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=True,
                args=list(BROWSER_ARGS),
                user_agent=DEFAULT_USER_AGENT,
                viewport=dict(DEFAULT_VIEWPORT),
                locale=BROWSER_LOCALE,
                timezone_id=BROWSER_TIMEZONE,
            )
            try:
                await context.add_init_script(STEALTH_SCRIPT)
                try:
                    await context.add_cookies(cookies)
                except Exception:
                    pass
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                except Exception:
                    return False
                await asyncio.sleep(3.0)
                entries = await detect_login_entries(page)
                if entries:
                    return False

                try:
                    await context.storage_state(path=session_file)
                except Exception:
                    pass
                return True
            finally:
                await context.close()
    except Exception:
        return False


async def _verify_profile(platform, url: str, profile_dir: str) -> Optional[bool]:
    """用某个浏览器配置目录打开平台首页，判断站点是否仍处于登录状态。"""
    if platform is None or not os.path.isdir(profile_dir):
        return None

    try:
        async with async_playwright() as playwright:
            # 刚关掉的浏览器进程可能还没把配置目录的锁释放掉
            context = None
            for attempt in range(3):
                try:
                    context = await playwright.chromium.launch_persistent_context(
                        user_data_dir=profile_dir,
                        headless=True,
                        args=list(BROWSER_ARGS),
                        user_agent=DEFAULT_USER_AGENT,
                        viewport=dict(DEFAULT_VIEWPORT),
                        locale=BROWSER_LOCALE,
                        timezone_id=BROWSER_TIMEZONE,
                    )
                    break
                except Exception:
                    if attempt == 2:
                        return None
                    await asyncio.sleep(1.5)
            if context is None:
                return None
            try:
                await context.add_init_script(STEALTH_SCRIPT)
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    await page.goto(platform.home_url, wait_until="domcontentloaded", timeout=45_000)
                except Exception:
                    return None
                await asyncio.sleep(3.0)
                entries = await detect_login_entries(page)
                return not bool(entries)
            finally:
                await context.close()
    except Exception:
        return None


async def _all_cookie_snapshot(context) -> dict:
    """记录当前上下文的全部 cookie（名字 -> 值），作为"未登录基线"。"""
    snapshot: dict = {}
    try:
        cookies = await context.cookies()
    except Exception:
        return snapshot
    for cookie in cookies:
        name = str(cookie.get("name", ""))
        value = str(cookie.get("value", ""))
        if name:
            snapshot[name] = value
    return snapshot


def _looks_like_anonymous_noise(name: str) -> bool:
    """判断这个 cookie 是不是"匿名访客本来就会写、跟登录无关"的那类。"""
    return name.lower() in ANONYMOUS_COOKIE_NAMES


async def _changed_cookie_pairs(context, baseline: dict) -> List[tuple]:
    """返回相比基线"新出现或值变了"的 cookie，形式是 [(名字, 值), ...]。"""
    try:
        cookies = await context.cookies()
    except Exception:
        # 上下文已经关闭，当作没有检测到
        return []

    changed: List[tuple] = []
    for cookie in cookies:
        name = str(cookie.get("name", ""))
        value = str(cookie.get("value", ""))
        if not name or not value:
            continue
        if baseline.get(name) != value:
            changed.append((name, value))
    return changed


def _confirm_overwrite(session_file: str, weak: bool = False) -> bool:
    """覆盖已有登录态前的二次确认。"""
    if weak:
        console.warn("检测到的变化不在平台预设的登录字段里，继续保存会覆盖现有的登录态")
    else:
        console.warn("没有检测到任何登录凭据，继续保存会覆盖现有的登录态")
    console.detail(f"受影响文件: {session_file}")

    try:
        interactive = sys.stdin.isatty()
    except Exception:
        interactive = False

    if not interactive:
        console.warn("当前不是交互式终端，为安全起见不覆盖，原有登录态保持不变")
        return False

    try:
        answer = input("  确定要覆盖吗？输入 y 回车确认，直接回车取消: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.blank()
        return False
    return answer in ("y", "yes", "是", "确认", "覆盖")


def show_sessions() -> None:
    """打印本地已保存的凭据列表，对应命令 linxia -sessions。"""
    from .sessions import list_sessions

    records = list_sessions()
    console.rule()
    console.stage("本地已保存的登录凭据")
    if not records:
        console.detail("暂无。可以先执行 linxia -login douyin 这样的命令完成一次登录", indent=0)
    for item in records:
        cookies = item.get("cookies")
        cookies_text = "读取失败" if cookies == -1 else f"{cookies} 条 cookie"
        # 登录状态一栏直接告诉用户这份凭据还能不能用
        state = item.get("login_state")
        if state == "ok":
            fields = item.get("auth_fields") or []
            state_text = f"有登录字段（{fields[0]} 等）" if fields else "有登录字段"
        elif state == "empty":
            state_text = "没有登录字段，需要重新登录"
        elif state == "broken":
            state_text = "文件损坏"
        else:
            state_text = "通用网站凭据，无法自动判断"
        console.detail(
            f"{item['name']:<16} {state_text:<26} {cookies_text:<14} {item['modified']}",
            indent=0,
        )
        console.detail(f"    {item['path']}", indent=0)
    console.rule()


    from .sessions import list_profiles

    profiles = list_profiles()
    if profiles:
        console.stage("浏览器配置目录")
        for item in profiles:
            size_text = console.format_bytes(item["size"])
            console.detail(f"{item['name']:<16} {size_text:<12} {item['modified']}", indent=0)
            console.detail(f"    {item['path']}", indent=0)
        console.rule()


def reset_session(target: str) -> bool:
    """删除指定目标的登录凭据，对应命令 linxia -reset douyin。"""
    platform = get_platform(target)
    if platform is not None:
        target_url = platform.login_url
        display = f"{platform.name}（{platform.key}）"
    elif is_url_like(target):
        target_url = normalize_url(target)
        platform = platform_by_url(target_url)
        display = platform.name if platform else f"通用网页（{extract_domain_key(target_url)}）"
    else:
        console.error(f'"{target}" 既不是内置平台名，也不是一个网址')
        console.info(f"内置平台有：{platform_summary()}")
        console.info('要删除别的网站的凭据，请给出网址，例如：linxia -reset "https://www.zhihu.com"')
        return False

    console.rule()
    console.stage(f"删除登录凭据: {display}")

    # 两样东西都要删：凭据文件（抓取时装配用）和浏览器配置目录（登录态真正所在）
    deleted = delete_sessions(platform, target_url)
    profile_removed = delete_profile(platform, target_url)

    if not deleted and not profile_removed:
        console.warn(f"没有找到 {display} 的凭据或浏览器配置目录，无需删除")
        console.rule()
        return False

    for path in deleted:
        console.detail(f"已删除凭据文件: {path}")
    if profile_removed:
        console.detail(f"已删除浏览器配置目录: {profile_dir_for(platform, target_url)}")

    console.rule()
    console.ok(f"{display} 的登录状态已清除")
    if platform is not None:
        console.info(f"需要重新登录时执行：linxia -login {platform.key}")
    else:
        console.info("需要重新登录时再执行一次 linxia -login 加该网址")
    console.rule()
    return True
