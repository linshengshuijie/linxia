"""浏览器管理模块"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from typing import Dict, List, Optional

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from .config import (
    BROWSER_ARGS,
    BROWSER_LOCALE,
    BROWSER_TIMEZONE,
    DEFAULT_USER_AGENT,
    DEFAULT_VIEWPORT,
    MOBILE_DEVICE_SCALE_FACTOR,
    MOBILE_USER_AGENT,
    MOBILE_VIEWPORT,
    TEMP_DIR,
)

# 反自动化检测注入脚本

# 这段脚本在每个页面加载前执行，抹掉最容易被识别的几个自动化特征。
STEALTH_SCRIPT = """
// navigator.webdriver 是最直接的自动化标志，改成 undefined
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 无头浏览器默认没有语言和插件，补上正常值
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });

// 无头模式下 window.chrome 常常缺失，补一个最小对象
if (!window.chrome) {
    window.chrome = { runtime: {} };
}

// 通知权限查询在无头模式下会暴露异常，改为走原生逻辑
try {
    const originalQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
    window.navigator.permissions.query = (parameters) => (
        parameters && parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters)
    );
} catch (err) {
    // 某些站点会冻结 permissions 对象，忽略即可
}
"""


async def apply_stealth(context: BrowserContext) -> None:
    """给上下文注册注入脚本，让该上下文下所有页面（含 iframe）都生效。"""
    await context.add_init_script(STEALTH_SCRIPT)


# 浏览器管理器


class BrowserManager:
    """管理一个 Chromium 进程以及它下面若干个任务上下文。"""

    def __init__(
        self,
        headless: bool = True,
        user_agent: str = DEFAULT_USER_AGENT,
        viewport: Optional[Dict[str, int]] = None,
    ) -> None:
        self.headless = headless
        self.user_agent = user_agent
        self.viewport = dict(viewport or DEFAULT_VIEWPORT)
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        # 记录上下文名字方便排查，值是该名字创建的上下文数量
        self._context_counter: Dict[str, int] = {}


    async def start(self) -> None:
        """启动 Playwright 和浏览器进程。重复调用是安全的。"""
        if self._browser is not None:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=list(BROWSER_ARGS),
        )

    async def stop(self) -> None:
        """关闭浏览器并释放 Playwright。所有任务结束后必须调用。"""
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:

                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()


    async def new_context(
        self,
        task_name: str = "任务",
        storage_state: Optional[str] = None,
        viewport: Optional[Dict[str, int]] = None,
        mobile: bool = False,
    ) -> BrowserContext:
        """创建一个任务专用上下文。"""
        if self._browser is None:
            await self.start()

        state_path = None
        if storage_state and os.path.exists(storage_state):
            state_path = storage_state

        options: Dict[str, object] = {
            "user_agent": MOBILE_USER_AGENT if mobile else self.user_agent,
            "viewport": dict(viewport or (MOBILE_VIEWPORT if mobile else self.viewport)),
            "locale": BROWSER_LOCALE,
            "timezone_id": BROWSER_TIMEZONE,
            "storage_state": state_path,
            # 由站点自己决定是否忽略证书问题，正常环境下保持严格
            "ignore_https_errors": False,
        }
        if mobile:
            options["device_scale_factor"] = MOBILE_DEVICE_SCALE_FACTOR
            options["is_mobile"] = True
            options["has_touch"] = True

        context = await self._browser.new_context(**options)
        await apply_stealth(context)
        self._context_counter[task_name] = self._context_counter.get(task_name, 0) + 1
        return context

    async def close_context(self, context: Optional[BrowserContext]) -> None:
        """关闭单个任务上下文，释放它的 cookie 和缓存，不影响其他任务。"""
        if context is None:
            return
        try:
            await context.close()
        except Exception:
            pass

    async def new_profile_context(self, profile_dir: str) -> BrowserContext:
        """用某个浏览器配置目录开一个持久化上下文。"""
        if self._playwright is None:
            await self.start()

        context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=self.headless,
            args=list(BROWSER_ARGS),
            user_agent=self.user_agent,
            viewport=dict(self.viewport),
            locale=BROWSER_LOCALE,
            timezone_id=BROWSER_TIMEZONE,
        )
        await apply_stealth(context)
        return context

    async def read_cookies(self, context: BrowserContext) -> List[dict]:
        """读出上下文里的全部 cookie。"""
        try:
            return await context.cookies()
        except Exception:
            return []


# 临时浏览器数据目录的清理


def remove_tree(path: str, attempts: int = 6) -> bool:
    """删除临时浏览器数据目录。"""
    for index in range(attempts):
        if not os.path.exists(path):
            return True
        try:
            shutil.rmtree(path, ignore_errors=False)
            return True
        except Exception:
            if index == attempts - 1:
                return False
            # 递增等待，给系统一点时间释放文件句柄
            time.sleep(0.5 * (index + 1))
    return False


def clean_stale_profiles(prefix: str, max_age_hours: float = 6.0) -> int:
    """清理以前被强制中断留下的临时浏览器数据目录，返回清理数量。"""
    removed = 0
    try:
        entries = os.listdir(TEMP_DIR)
    except OSError:
        return 0

    deadline = time.time() - max_age_hours * 3600
    for name in entries:
        if not name.startswith(prefix):
            continue
        target = os.path.join(TEMP_DIR, name)
        if not os.path.isdir(target):
            continue
        try:
            if os.path.getmtime(target) > deadline:
                continue
        except OSError:
            continue
        if remove_tree(target, attempts=2):
            removed += 1
    return removed


# 等待进入登录态的辅助函数


async def wait_for_auth_cookie(
    context: BrowserContext,
    cookie_names: List[str],
    timeout: float,
    poll_interval: float = 1.5,
) -> bool:
    """轮询上下文 cookie，判断用户是否已经登录。"""
    wanted = {name.lower() for name in cookie_names if name}
    if not wanted:
        return False
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        cookies = await context.cookies()
        for cookie in cookies:
            if str(cookie.get("name", "")).lower() in wanted and cookie.get("value"):
                return True
        await asyncio.sleep(poll_interval)
    return False
