""""用已保存的登录态开一个浏览器"模式"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from playwright.async_api import async_playwright

from . import console
from .browser import STEALTH_SCRIPT, clean_stale_profiles, remove_tree
from .human import detect_login_entries
from .config import (
    BROWSER_ARGS,
    BROWSER_LOCALE,
    BROWSER_TIMEZONE,
    DEFAULT_USER_AGENT,
    DEFAULT_VIEWPORT,
    SESSIONS_DIR,
    TEMP_DIR,
)
from .login import EnterWatcher
from .platforms import (
    all_platforms,
    get_platform,
    is_url_like,
    normalize_url,
    platform_by_url,
    platform_summary,
)
from .sessions import find_session_file, profile_dir_for, session_auth_fields


@dataclass
class BrowseOptions:
    """浏览模式的可选参数。"""

    # 最久允许开多久（秒）。不是限制用户，只是防止忘了关留下一个孤儿进程。
    max_seconds: float = 7200.0

    # 是否显示浏览器窗口。浏览模式必须可见，保留开关只是为了将来做截图用途。
    show_browser: bool = True


def list_session_files() -> List[str]:
    """列出 sessions 目录里全部可用凭据文件（绝对路径）。"""
    if not os.path.isdir(SESSIONS_DIR):
        return []
    result: List[str] = []
    for filename in sorted(os.listdir(SESSIONS_DIR)):
        if not filename.endswith("_state.json"):
            continue
        path = os.path.join(SESSIONS_DIR, filename)
        if os.path.isfile(path):
            result.append(path)
    return result


def merge_storage_states(paths: List[str]) -> Tuple[dict, List[str]]:
    """把多份登录态合并成一份 storage_state。"""
    cookies: Dict[Tuple[str, str, str], dict] = {}
    origins: Dict[str, dict] = {}
    used: List[str] = []

    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            continue
        used.append(path)

        for cookie in payload.get("cookies") or []:
            if not isinstance(cookie, dict):
                continue
            key = (
                str(cookie.get("name", "")),
                str(cookie.get("domain", "")),
                str(cookie.get("path", "/")),
            )
            cookies[key] = cookie

        for origin in payload.get("origins") or []:
            if not isinstance(origin, dict):
                continue
            name = str(origin.get("origin", ""))
            if not name:
                continue
            bucket = origins.setdefault(name, {"origin": name, "localStorage": []})
            bucket["localStorage"].extend(origin.get("localStorage") or [])

    return {"cookies": list(cookies.values()), "origins": list(origins.values())}, used


def resolve_target(target: str) -> Tuple[str, Optional[str], str]:
    """解析浏览目标，返回 (要打开的地址, 平台键, 展示名)。"""
    text = (target or "").strip()
    if not text:
        return "about:blank", "", "未指定目标（空白页）"

    platform = get_platform(text)
    if platform is not None:
        return platform.home_url, platform.key, f"{platform.name}（{platform.key}）"

    if is_url_like(text):
        url = normalize_url(text)
        platform = platform_by_url(url)
        if platform is not None:
            return url, platform.key, f"{platform.name}（{platform.key}）"
        return url, "", f"通用网页（{url}）"

    return "", "", ""


def sanitize_cookies(cookies: List[dict]) -> List[dict]:
    """清洗 cookie 数据，只保留浏览器接口认的字段。"""
    allowed_same_site = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",
    }
    cleaned: List[dict] = []

    for cookie in cookies:
        try:
            name = str(cookie.get("name", ""))
            value = str(cookie.get("value", ""))
            domain = str(cookie.get("domain", ""))
        except AttributeError:
            continue
        if not name or not domain:
            continue

        item = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": str(cookie.get("path") or "/"),
            "httpOnly": bool(cookie.get("httpOnly", False)),
            "secure": bool(cookie.get("secure", False)),
        }
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            item["expires"] = float(expires)

        same_site = cookie.get("sameSite")
        if isinstance(same_site, str):
            normalized = allowed_same_site.get(same_site.lower())
            if normalized:
                item["sameSite"] = normalized

        cleaned.append(item)
    return cleaned


def build_local_storage_seed(origins: List[dict]) -> str:
    """生成一段"给页面补 localStorage"的注入脚本。"""
    buckets: Dict[str, Dict[str, str]] = {}
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        name = str(origin.get("origin", ""))
        if not name:
            continue
        bucket = buckets.setdefault(name, {})
        for item in origin.get("localStorage") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("name", ""))
            if key:
                bucket[key] = str(item.get("value", ""))

    if not buckets:
        return ""

    payload = json.dumps(buckets, ensure_ascii=False)
    return f"""
(() => {{
    const buckets = {payload};
    const bucket = buckets[location.origin];
    if (!bucket) return;
    try {{
        for (const key of Object.keys(bucket)) {{
            localStorage.setItem(key, bucket[key]);
        }}
    }} catch (err) {{
        // 站点可能禁用 localStorage，忽略即可
    }}
}})();
"""


def _remove_tree(path: str, attempts: int = 6) -> bool:
    """删除临时浏览器数据目录（实现在 browser.py 里，和登录模式共用）。"""
    return remove_tree(path, attempts)


async def run_website(target: str = "", options: Optional[BrowseOptions] = None) -> bool:
    """用已保存的登录态打开一个临时浏览器，让用户自己浏览。"""
    options = options or BrowseOptions()

    url, platform_key, display = resolve_target(target)
    if not url and not platform_key:
        console.error(f'"{target}" 既不是内置平台名，也不是一个网址')
        console.info(f"内置平台有：{platform_summary()}")
        console.info('可以写：linxia -website douyin 或 linxia -website "https://www.zhihu.com"')
        return False

    platform = get_platform(platform_key) if platform_key else None

    # 优先用"浏览器配置目录"：那是真·普通浏览器行为，cookie、localStorage
    profile_dir = profile_dir_for(platform, url) if platform is not None else ""
    use_profile = bool(profile_dir) and os.path.isdir(profile_dir) and bool(
        os.listdir(profile_dir)
    )

    if use_profile:
        return await _run_with_profile(profile_dir, display, url, options)

    # 没有配置目录：用临时浏览器 + 合并注入所有凭据
    return await _run_with_injected_cookies(target, url, platform, platform_key, display, options)


async def _run_with_profile(profile_dir: str, display: str, url: str, options: "BrowseOptions") -> bool:
    """用已有的浏览器配置目录打开浏览器。"""
    console.rule()
    console.stage("浏览器浏览（复用已登录的配置目录）")
    console.detail(f"目标: {display}")
    console.detail(f"入口地址: {url}")
    console.detail(f"浏览器配置目录: {profile_dir}")
    console.detail("这就是普通浏览器的做法：配置目录一直复用，登录态自然延续")
    console.rule()

    opened = False
    try:
        async with async_playwright() as playwright:
            try:
                context = await playwright.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=not options.show_browser,
                    args=list(BROWSER_ARGS),
                    user_agent=DEFAULT_USER_AGENT,
                    viewport=dict(DEFAULT_VIEWPORT),
                    locale=BROWSER_LOCALE,
                    timezone_id=BROWSER_TIMEZONE,
                )
            except Exception as exc:
                console.error(f"打开浏览器失败: {str(exc).splitlines()[0][:120]}")
                console.info("常见原因：上一次的浏览器窗口还没关掉，配置目录被占用")
                return False

            await context.add_init_script(STEALTH_SCRIPT)
            page = context.pages[0] if context.pages else await context.new_page()
            if url and url != "about:blank":
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                except Exception as exc:
                    console.warn(f"页面加载提示: {str(exc).splitlines()[0][:100]}")

                await _report_login_state(page, url, in_profile=True)

            console.info("浏览器已打开，可以随便逛")
            console.info("看完回到本窗口按回车结束（直接关掉浏览器窗口也可以）")
            console.blank()

            opened = True
            await _wait_until_done(context, options.max_seconds)
            await context.close()
    finally:
        # 配置目录必须保留，不然下次又变成未登录了
        console.detail(f"浏览器配置目录已保留: {profile_dir}")

    console.ok("浏览结束")
    return opened


async def _run_with_injected_cookies(
    target: str,
    url: str,
    platform,
    platform_key: str,
    display: str,
    options: "BrowseOptions",
) -> bool:
    """没有配置目录时的退路：临时浏览器 + 注入 cookie。"""
    # 决定注入哪些凭据
    if platform_key:
        session_file = find_session_file(platform, url)
        session_files = [session_file] if session_file else []
    else:
        session_files = list_session_files()

    storage_state, used_files = merge_storage_states(session_files)

    console.rule()
    console.stage("临时浏览器浏览（注入已有凭据）")
    console.detail(f"目标: {display}")
    console.detail(f"入口地址: {url}")
    if used_files:
        for path in used_files:

            name = os.path.basename(path).replace("_state.json", "")
            item_platform = None
            for item in all_platforms():
                if name in item.session_keys or name == item.key:
                    item_platform = item
                    break
            hits = session_auth_fields(path, item_platform.auth_cookies if item_platform else ())
            state_text = f"登录字段 {', '.join(hits[:3])}" if hits else "未发现登录字段"
            console.detail(f"注入凭据: {os.path.basename(path)}（{state_text}）")
    else:
        console.warn("没有找到任何已保存的凭据，这个浏览器会是未登录状态")
        console.detail("先执行 linxia -login douyin 这样的命令完成一次登录")
    console.rule()

    temp_profile = tempfile.mkdtemp(prefix="linxia-browse-", dir=TEMP_DIR)
    opened = False


    stale = clean_stale_profiles("linxia-browse-")
    if stale:
        console.detail(f"已清理 {stale} 个上次中断残留的临时浏览器目录")

    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=temp_profile,
                headless=not options.show_browser,
                args=list(BROWSER_ARGS),
                user_agent=DEFAULT_USER_AGENT,
                viewport=dict(DEFAULT_VIEWPORT),
                locale=BROWSER_LOCALE,
                timezone_id=BROWSER_TIMEZONE,
            )
            await context.add_init_script(STEALTH_SCRIPT)

            # 持久化上下文不能像普通上下文那样直接传 storage_state
            cookies = sanitize_cookies(storage_state.get("cookies") or [])
            if cookies:
                try:
                    await context.add_cookies(cookies)
                    console.detail(f"已注入 {len(cookies)} 条 cookie")
                except Exception as exc:
                    console.warn(f"注入 cookie 失败: {str(exc).splitlines()[0][:100]}")

            seed_script = build_local_storage_seed(storage_state.get("origins") or [])
            if seed_script:
                await context.add_init_script(seed_script)
                console.detail(f"已准备 {len(storage_state.get('origins') or [])} 个站点的本地存储")

            page = context.pages[0] if context.pages else await context.new_page()
            if url and url != "about:blank":
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                except Exception as exc:
                    console.warn(f"页面加载提示: {str(exc).splitlines()[0][:100]}")

                await _report_login_state(page, url, platform_key=platform_key)

            console.info("浏览器已打开，登录态已注入，可以随便逛")
            console.info("看完回到本窗口按回车结束（直接关掉浏览器窗口也可以）")
            console.info("想以后打开就是登录状态，用 linxia -login 平台名 登录一次（会保留配置目录）")
            console.blank()

            opened = True
            await _wait_until_done(context, options.max_seconds)
            await context.close()
    finally:
        removed = _remove_tree(temp_profile)
        if removed:
            console.detail("临时浏览器数据已删除")
        else:
            console.warn(f"临时浏览器数据未能完全删除，请手动删除目录: {temp_profile}")

    console.ok("浏览结束")
    return opened


async def _report_login_state(
    page,
    url: str,
    in_profile: bool = False,
    platform_key: str = "",
) -> None:
    """打开页面后，如实报一句"站点认不认这份登录"。"""
    import urllib.parse

    try:
        path = urllib.parse.urlparse(url).path
    except Exception:
        path = "/"

    if path not in ("", "/"):
        # 不是首页，不做结论
        return

    await asyncio.sleep(3.0)
    entries = await detect_login_entries(page)
    if not entries:
        console.detail("确认过页面：已经是登录状态")
        return

    shown = "、".join(entries)
    if in_profile:
        console.warn(f"页面仍显示登录入口（{shown}），这份登录状态可能已失效")
        console.detail("处理办法：先 linxia -reset 平台名，再重新 linxia -login 平台名")
    else:
        console.warn(f"页面仍显示登录入口（{shown}），站点没把这份凭据当登录")
        console.detail("抖音、快手这类平台会把登录态和设备指纹绑定，光注入 cookie 不够")
        if platform_key:
            console.detail(f"处理办法：linxia -login {platform_key} 登录一次（会保留配置目录）")
        else:
            console.detail("处理办法：先 linxia -login 平台名 登录一次，之后再用 -website")


async def _wait_until_done(context, max_seconds: float) -> None:
    """等用户看完。"""
    loop = asyncio.get_event_loop()
    watcher = EnterWatcher()
    if not watcher.start():
        console.detail("当前输入不是交互式终端，只能用关闭浏览器窗口来结束")

    deadline = loop.time() + max(max_seconds, 30.0)
    try:
        while loop.time() < deadline:
            if watcher.pressed:
                return
            try:
                if not context.pages:
                    # 页面全关了，等同于用户关掉了浏览器
                    return
            except Exception:
                return
            await asyncio.sleep(0.5)
        console.warn("已到达最长浏览时限，自动结束")
    finally:
        watcher.stop()
