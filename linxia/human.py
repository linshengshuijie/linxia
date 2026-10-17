"""拟人化浏览模块"""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Optional

from playwright.async_api import Page

# 播放器"播放按钮"的常见选择器。只有在视频仍处于暂停状态时才尝试点击。
PLAY_BUTTON_SELECTORS = (
    ".bpx-player-ctrl-play",
    ".xgplayer-play",
    ".vjs-big-play-button",
    ".jw-icon-playback",
    ".play-btn",
    "button[aria-label*='播放']",
    "div[aria-label*='播放']",
)

# 播放器报错后"重试"按钮的常见文案
RETRY_TEXT_KEYWORDS = ("重试", "重新加载", "retry", "reload")

# 找并点击"重试"按钮的脚本。
RETRY_SCRIPT = """
([keywords]) => {
    const videos = Array.from(document.querySelectorAll('video'));
    if (videos.length === 0) return 'no-video';
    const stuck = videos.every(video => {
        const duration = video.duration;
        const notLoaded = !duration || isNaN(duration) || video.readyState < 2;
        return notLoaded;
    });
    if (!stuck) return 'playing';

    const candidates = [];
    document.querySelectorAll('button, div, span, a').forEach(el => {
        const text = String(el.innerText || '').trim();
        const cls = String(el.className || '').toLowerCase();
        if (!text && cls.indexOf('retry') < 0) return;
        if (text.length > 16) return;
        const lowered = text.toLowerCase();
        const byText = keywords.some(keyword => lowered.indexOf(String(keyword).toLowerCase()) >= 0);
        if (!byText && cls.indexOf('retry') < 0) return;
        const rect = el.getBoundingClientRect();
        if (rect.width < 10 || rect.height < 8) return;
        if (rect.width > 520 || rect.height > 160) return;
        candidates.push(el);
    });
    if (candidates.length === 0) return 'no-button';
    try {
        candidates[0].click();
        return 'clicked:' + String(candidates[0].innerText || candidates[0].className || 'retry').trim().slice(0, 16);
    } catch (err) {
        return 'error';
    }
}
"""


# 判断"这个页面是不是还显示着登录入口"用的文案。
LOGIN_ENTRY_TEXTS = (
    "登录",
    "立即登录",
    "马上登录",
    "扫码登录",
    "请登录",
    "登录/注册",
    "登录即可享受",
)

DETECT_LOGIN_SCRIPT = """
(texts) => {
    const hits = [];
    document.querySelectorAll('button, a, div, span').forEach(el => {
        const text = String(el.innerText || '').trim();
        if (!text || text.length > 12) return;
        if (texts.indexOf(text) < 0) return;
        const rect = el.getBoundingClientRect();
        if (rect.width < 20 || rect.height < 10) return;
        if (hits.indexOf(text) < 0) hits.push(text);
    });
    return hits;
}
"""


async def detect_login_entries(page: Page) -> List[str]:
    """找出页面上可见的"登录"入口文案。"""
    try:
        result = await page.evaluate(DETECT_LOGIN_SCRIPT, list(LOGIN_ENTRY_TEXTS))
    except Exception:
        return []
    if not isinstance(result, list):
        return []
    return [str(item) for item in result]


async def probe_player_retry(page: Page) -> str:
    """看一眼播放器当前状态，并在需要时点"重试"。返回状态字符串"""
    try:
        result = await page.evaluate(RETRY_SCRIPT, [list(RETRY_TEXT_KEYWORDS)])
    except Exception:
        return "error"
    return str(result or "error")


async def recover_player(page: Page) -> Optional[str]:
    """救一下卡住的播放器，返回点中了哪个按钮的说明（没救则返回 None）。"""
    status = await probe_player_retry(page)
    if status.startswith("clicked:"):
        return status[len("clicked:"):]
    return None


async def wait_and_recover_player(page: Page, budget: float = 4.0) -> Optional[str]:
    """在拟人化动作开始之前，先花几百毫秒看看播放器是不是报了错，报错就点重试。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(budget, 0.0)

    while loop.time() < deadline:
        status = await probe_player_retry(page)
        if status.startswith("clicked:"):
            return status[len("clicked:"):]
        if status in ("playing", "no-video"):
            return None
        await asyncio.sleep(0.4)
    return None


def _random_delay(low: float, high: float) -> float:
    """返回区间内的随机等待秒数。"""
    return random.uniform(low, high)


async def _safe_sleep(seconds: float) -> None:
    """安全等待，负数按 0 处理。"""
    await asyncio.sleep(max(seconds, 0.0))


async def warm_up_media(page: Page) -> int:
    """让页面里的 video 元素开始播放。"""
    script = """
    () => {
        const videos = Array.from(document.querySelectorAll('video'));
        for (const v of videos) {
            try {
                v.muted = true;
                v.volume = 0;
                const result = v.play();
                if (result && typeof result.catch === 'function') {
                    result.catch(() => {});
                }
            } catch (err) {
                // 单个元素失败不影响其他元素
            }
        }
        return videos.length;
    }
    """
    try:
        return int(await page.evaluate(script) or 0)
    except Exception:
        return 0


async def _video_box(page: Page):
    """取第一个 video 元素的位置，取不到返回 None。"""
    try:
        element = await page.query_selector("video")
        if element is None:
            return None
        return await element.bounding_box()
    except Exception:
        return None


async def move_mouse_naturally(page: Page, target_x: float, target_y: float) -> None:
    """把鼠标分几步移到目标位置，模拟真人移动轨迹。"""
    try:
        viewport = page.viewport_size or {"width": 1440, "height": 900}
        start_x = random.uniform(0.2, 0.8) * viewport["width"]
        start_y = random.uniform(0.3, 0.7) * viewport["height"]
        await page.mouse.move(start_x, start_y)
        await _safe_sleep(_random_delay(0.1, 0.3))
        await page.mouse.move(target_x, target_y, steps=random.randint(8, 18))
    except Exception:
        pass


async def light_scroll(page: Page, rounds: int = 2) -> None:
    """轻微滚动页面。"""
    for _ in range(max(rounds, 0)):
        try:
            delta = random.randint(180, 420)
            await page.mouse.wheel(0, delta)
            await _safe_sleep(_random_delay(0.4, 0.9))
            await page.mouse.wheel(0, -delta // 2)
            await _safe_sleep(_random_delay(0.3, 0.7))
        except Exception:
            return


async def try_click_play(page: Page) -> None:
    """如果视频仍然暂停着，尝试点一下页面上常见的播放按钮。"""
    paused = True
    try:
        paused = bool(
            await page.evaluate(
                """
                () => {
                    const v = document.querySelector('video');
                    return v ? v.paused : true;
                }
                """
            )
        )
    except Exception:
        return
    if not paused:
        return

    for selector in PLAY_BUTTON_SELECTORS:
        try:
            element = await page.query_selector(selector)
            if element is None:
                continue
            if not await element.is_visible():
                continue
            await element.click(timeout=2000)
            await _safe_sleep(_random_delay(0.4, 0.9))
            return
        except Exception:
            continue

    # 没有找到播放按钮，退一步直接在元素上派发一次点击
    try:
        element = await page.query_selector("video")
        if element is not None:
            await element.click(timeout=2000, position={"x": 10, "y": 10})
    except Exception:
        pass


async def simulate_viewing(
    page: Page,
    has_media: Optional[Callable[[], bool]] = None,
    budget: float = 8.0,
    seed_note: str = "",
    carousel: bool = False,
) -> int:
    """执行一整轮拟人化浏览，返回"翻了几次页"（没翻就是 0）。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(budget, 0.0)
    advanced = 0

    def time_left() -> float:
        return deadline - loop.time()

    def finished(stop_on_media: bool = True) -> bool:
        if time_left() <= 0:
            return True
        # 轮播模式下不能"一抓到媒体就收工"：第一页的媒体通常秒到
        if stop_on_media and has_media is not None and has_media():
            return True
        return False

    # 轮播平台要一路走到底，其它平台抓到流就可以收工
    def may_stop() -> bool:
        return finished(stop_on_media=not carousel)

    try:
        # 给页面留出渲染和播放器初始化的时间
        await wait_and_recover_player(page, budget=min(4.0, max(time_left(), 0.0)))
        await _safe_sleep(min(_random_delay(0.4, 0.8), max(time_left(), 0)))
        if may_stop():
            return

        # 把鼠标移到视频区域，制造"正在看这个视频"的信号
        box = await _video_box(page)
        if box:
            target_x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
            target_y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
            await move_mouse_naturally(page, target_x, target_y)
            await _safe_sleep(min(_random_delay(0.3, 0.7), max(time_left(), 0)))
        if may_stop():
            return

        # 静音并触发播放，这是拿到媒体流最关键的一步
        await warm_up_media(page)
        await _safe_sleep(min(_random_delay(0.8, 1.5), max(time_left(), 0)))
        if may_stop():
            return

        # 轻微滚动，等懒加载和清晰度切换
        await light_scroll(page, rounds=2)
        if may_stop():
            return

        # 如果还没抓到流，再补一次播放动作
        await try_click_play(page)
        await warm_up_media(page)

        # 轮播笔记逐张翻页。
        if carousel:
            advanced = await advance_carousel(page, has_media=has_media, budget=max(time_left(), 0.0))
            # 翻完给最后一页留点时间把流拉起来
            await _safe_sleep(min(_random_delay(2.0, 3.0), max(time_left(), 0)))
            return advanced

        # 安静停留，等播放器把分片请求发完
        while not finished():
            await _safe_sleep(min(0.5, max(time_left(), 0)))
            if random.random() < 0.25:
                await warm_up_media(page)
    except Exception:
        # 页面被关闭、跳转等情况都会走到这里，直接结束模拟即可
        return advanced
    return advanced


# 轮播"下一张"按钮的常见选择器，按优先级排列
CAROUSEL_NEXT_SELECTORS = (
    ".arrow-controller.right",
    ".swiper-button-next",
    ".slick-next",
    "button[aria-label*='下一']",
    "div[aria-label*='下一']",
    ".chevron-right",
)

# 轮播当前页的签名：拿当前激活那一页里媒体地址当标识
_ACTIVE_SLIDE_SCRIPT = """
() => {
    const active = document.querySelector('.swiper-slide-active');
    if (!active) return '';
    const video = active.querySelector('video');
    const img = active.querySelector('img');
    const videoSrc = video ? (video.currentSrc || video.src || '') : '';
    const imgSrc = img ? (img.currentSrc || img.src || '') : '';
    const index = active.getAttribute('data-swiper-slide-index') || '';
    return [index, videoSrc.split('?')[0], imgSrc.split('?')[0]].join('|');
}
"""

_SLIDE_COUNT_SCRIPT = """
() => {
    // 排除 swiper 循环用的克隆节点，只数真实页数
    const slides = Array.from(document.querySelectorAll('.swiper-slide'))
        .filter(el => String(el.className || '').indexOf('duplicate') < 0);
    return slides.length;
}
"""


async def _active_slide_signature(page: Page) -> str:
    """当前激活页的标识，用来判断"翻页到底有没有真的翻过去"。"""
    try:
        return str(await page.evaluate(_ACTIVE_SLIDE_SCRIPT))
    except Exception:
        return ""


async def _carousel_slide_count(page: Page) -> int:
    """轮播一共有几页（不含克隆节点）；不是轮播就返回 0。"""
    try:
        count = int(await page.evaluate(_SLIDE_COUNT_SCRIPT))
    except Exception:
        return 0
    return max(count, 0)


async def _click_carousel_next(page: Page) -> bool:
    """点一下轮播的"下一张"，点到了返回 True。"""
    for selector in CAROUSEL_NEXT_SELECTORS:
        try:
            element = await page.query_selector(selector)
            if element is None:
                continue
            # 已经翻到头时按钮会带 forbidden/disabled 之类的类名，别再点
            classes = str(await element.get_attribute("class") or "")
            if "forbidden" in classes or "disabled" in classes:
                return False
            await element.click(timeout=2000)
            return True
        except Exception:
            continue
    return False


async def advance_carousel(
    page: Page,
    has_media: Optional[Callable[[], bool]] = None,
    budget: float = 20.0,
    max_steps: int = 12,
) -> int:
    """逐张翻轮播，让每一页的媒体都加载出来，返回实际翻了几次。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(budget, 0.0)
    advanced = 0

    slides = await _carousel_slide_count(page)
    if slides <= 1:
        return 0

    start_url = page.url
    limit = min(max_steps, max(slides, 1))

    while advanced < limit and loop.time() < deadline:
        before = await _active_slide_signature(page)
        if not await _click_carousel_next(page):
            break

        # 给新一页留出拉流的时间
        await _safe_sleep(_random_delay(1.6, 2.4))
        advanced += 1

        # 跳走了就别继续点，否则可能一路点到别的作品上
        try:
            if page.url != start_url:
                break
        except Exception:
            break

        after = await _active_slide_signature(page)
        if not after or after == before:
            # 翻不动了（到头了，或者这一页本来就没有独立媒体）
            break

    return advanced
