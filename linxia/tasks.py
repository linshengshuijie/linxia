"""抓取任务编排模块"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page

from . import console
from .browser import BrowserManager
from .config import (
    DEFAULT_JOBS,
    DEFAULT_USER_AGENT,
    DOWNLOAD_DIR,
    PAGE_LOAD_TIMEOUT_MS,
    SNIFF_POLL_INTERVAL_S,
    SNIFF_TIMEOUT_S,
    get_ffmpeg_bin,
)
from .downloader import (
    DownloadHeaders,
    build_cookie_header,
    download_file,
    download_hls,
    download_images_via_browser,
    mux_tracks,
    sanitize_filename,
    unique_directory,
    unique_output_path,
    write_download_record,
)
from .human import recover_player, simulate_viewing
from .platforms import (
    Platform,
    is_mobile_landing_url,
    kuaishou_work_ids,
    normalize_url,
    resolve_platform,
)
from .sessions import find_session_file, profile_dir_for, profile_exists
from .sniffer import DownloadPlan, MediaCandidate, MediaSniffer, collect_content_images

# 嗅探过程中每隔多少秒汇报一次"还在等什么"
PROGRESS_REPORT_INTERVAL_S = 5.0

# 轮播平台收工前额外多等这么久，给最后一份媒体留出拉流时间
CAROUSEL_GRACE_S = 9.0

# 轮播平台给模拟动作多留的预算（逐页翻要时间）
CAROUSEL_EXTRA_BUDGET_S = 22.0

# 页面上有播放器但迟迟没起播时，额外再等这么久（只延一次）
STALLED_PLAYER_GRACE_S = 9.0

# 模拟浏览动作做完后，如果还没抓到媒体，再多等这么久
POST_SIMULATION_GRACE_S = 5.0

# 模拟真人浏览这一步最多花多少秒。超过这个时间页面该发的请求基本都发完了
MAX_SIMULATION_S = 15.0


# 配置与结果数据结构


@dataclass
class TaskOptions:
    """一次运行的全局选项，来自命令行。"""

    output_dir: str = DOWNLOAD_DIR
    sniff_timeout: float = SNIFF_TIMEOUT_S
    jobs: int = DEFAULT_JOBS
    show_browser: bool = False
    platform_key: str = ""
    verbose: bool = False


@dataclass
class TaskResult:
    """单个链接的处理结果。"""

    index: int
    url: str
    success: bool = False
    platform_name: str = ""
    title: str = ""
    output_path: str = ""
    size: int = 0
    message: str = ""
    candidates: int = 0
    mode: str = ""


class TaskReporter:
    """带任务编号的输出器。"""

    def __init__(self, index: int, total: int, quiet: bool = True) -> None:
        self.index = index
        self.total = total
        self.tag = "" if total <= 1 else f"任务 {index}/{total}"
        self.quiet = quiet

    def _prefix(self) -> str:
        return f"{self.tag}：" if self.tag else ""

    def progress_prefix(self) -> str:
        """进度条标签前缀：单任务为空，多任务用来区分。"""
        return f"任务{self.index} " if self.total > 1 else ""

    def info(self, message: str) -> None:
        console.info(f"{self._prefix()}{message}")

    def warn(self, message: str) -> None:
        console.warn(f"{self._prefix()}{message}")

    def error(self, message: str) -> None:
        console.error(f"{self._prefix()}{message}")

    def detail(self, message: str) -> None:
        """过程细节。简洁模式下直接丢弃。"""
        if self.quiet:
            return
        console.detail(f"{self._prefix()}{message}", indent=4)


# 辅助函数


async def extract_title(page: Page, platform: Optional[Platform], fallback: str = "") -> str:
    """提取视频标题。"""
    title = ""

    try:
        og = await page.evaluate(
            """
            () => {
                const node = document.querySelector('meta[property="og:title"]')
                          || document.querySelector('meta[name="og:title"]');
                return node ? (node.getAttribute('content') || '') : '';
            }
            """
        )
        title = (og or "").strip()
    except Exception:
        title = ""

    if not title:
        try:
            heading = await page.query_selector("h1")
            if heading is not None:
                title = (await heading.inner_text() or "").strip()
        except Exception:
            title = ""

    if not title:
        try:
            title = (await page.title() or "").strip()
        except Exception:
            title = ""

    if not title:
        title = fallback

    if platform:
        for suffix in platform.title_suffixes:
            if title.endswith(suffix):
                title = title[: -len(suffix)].strip()
    return title


def guess_output_extension(plan: DownloadPlan) -> str:
    """推测最终文件扩展名。"""
    if plan.mode in ("hls", "tracks"):
        return ".mp4"
    path = urlparse(plan.video_url).path.lower()
    for extension in (".flv", ".webm", ".mkv", ".mov", ".ts"):
        if path.endswith(extension):
            return extension
    return ".mp4"


async def _load_more_images(page: Page, sniffer: MediaSniffer, reporter: TaskReporter, budget: float = 8.0) -> None:
    """图文作品专用：把页面往下滚，逼出懒加载的图片。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + budget
    last_count = sniffer.image_count
    stable_rounds = 0

    reporter.detail("正在滚动页面，等待懒加载的图片出现")

    while loop.time() < deadline:
        try:
            await page.mouse.wheel(0, 700)
        except Exception:
            break
        await asyncio.sleep(0.6)


        if sniffer.image_count == last_count:
            stable_rounds += 1
            if stable_rounds >= 3:
                break
        else:
            stable_rounds = 0
            last_count = sniffer.image_count
            reporter.detail(f"已加载 {last_count} 张图片")


def describe_wait_state(sniffer: MediaSniffer, waited: float) -> str:
    """生成一句"当前在等什么"的说明，让用户知道程序没卡死。"""
    video_only = sniffer.video_only_count()
    if video_only and not sniffer.snapshot("audio"):
        return (
            f"已等待 {waited:.0f} 秒，抓到 {video_only} 条纯视频轨，"
            "继续等音频轨出现（否则合成出来的视频没有声音）"
        )
    return f"已等待 {waited:.0f} 秒，已捕获 {sniffer.total} 条候选，继续观察页面请求"


# 对外入口


async def run_sniff_tasks(urls: List[str], options: TaskOptions) -> List[TaskResult]:
    """批量执行嗅探下载任务。"""
    total = len(urls)
    quiet = not options.verbose

    manager = BrowserManager(headless=not options.show_browser)
    await manager.start()


    task_slots = asyncio.Semaphore(max(1, options.jobs))
    # 下载锁：保证同一时刻只有一个进度条在画
    download_lock = asyncio.Semaphore(1)

    try:
        coroutines = [
            _run_single_task(index, total, url, options, manager, task_slots, download_lock)
            for index, url in enumerate(urls, start=1)
        ]
        results = await asyncio.gather(*coroutines, return_exceptions=True)
    finally:
        await manager.stop()

    # 把意外泄漏出来的异常也整理成失败结果，保证返回值结构统一
    normalized: List[TaskResult] = []
    for index, item in enumerate(results, start=1):
        if isinstance(item, TaskResult):
            normalized.append(item)
        else:
            normalized.append(
                TaskResult(
                    index=index,
                    url=urls[index - 1],
                    success=False,
                    message=f"任务内部异常: {item}",
                )
            )
    return normalized


# 单任务流程


async def _run_single_task(
    index: int,
    total: int,
    url: str,
    options: TaskOptions,
    manager: BrowserManager,
    task_slots: asyncio.Semaphore,
    download_lock: asyncio.Semaphore,
) -> TaskResult:
    """单条链接的完整处理流程。"""
    reporter = TaskReporter(index, total, quiet=not options.verbose)
    target_url = normalize_url(url)
    result = TaskResult(index=index, url=target_url)

    # 等一个任务槽位，避免同时打开过多上下文
    async with task_slots:
        reporter.info(f"开始处理 {target_url}")

        # 识别平台
        platform = resolve_platform(target_url, options.platform_key or None)
        if platform is not None:
            result.platform_name = platform.name
            reporter.detail(f"平台识别: {platform.name}（{platform.key}），下载 Referer 使用 {platform.referer}")
            if options.platform_key and platform.key != options.platform_key:
                reporter.warn("命令行指定的平台与链接域名不一致，按指定平台策略执行")
        else:
            reporter.detail("平台识别: 通用网页，使用通用嗅探策略")

        # 装配登录态
        session_path = find_session_file(platform, target_url)
        profile_dir = profile_dir_for(platform, target_url) if platform is not None else ""
        use_profile = bool(profile_dir) and profile_exists(platform, target_url)

        if use_profile:
            reporter.detail(f"登录态: 使用该平台的浏览器配置目录 {profile_dir}")
        elif session_path:
            reporter.detail(f"登录态: 已装配 {os.path.basename(session_path)}")
        else:
            reporter.detail("登录态: 没有找到可用凭据，以匿名访客身份访问")
            if platform is not None:
                reporter.detail(f"提示: 想拿到完整内容，先执行 linxia -login {platform.key} 登录该平台")
            else:
                reporter.detail('提示: 想拿到完整内容，先执行 linxia -login "该网站首页地址" 登录')

        # 建立独立上下文并挂载嗅探器
        carousel = bool(platform is not None and platform.carousel)
        context: Optional[BrowserContext] = None
        page: Optional[Page] = None
        sniffer = MediaSniffer(platform=platform)
        image_outcome: Optional[TaskResult] = None
        # 配置目录同一时刻只能被一个浏览器实例占用，所以按目录串行化
        profile_lock = _profile_lock(profile_dir) if use_profile else None
        if profile_lock is not None:
            await profile_lock.acquire()
        try:
            if use_profile:
                try:
                    context = await manager.new_profile_context(profile_dir)
                except Exception as exc:
                    first_line = str(exc).splitlines()[0] if str(exc) else "未知原因"
                    reporter.warn(f"配置目录打不开，退回注入凭据方式: {first_line[:100]}")
                    use_profile = False
                    context = None
            if context is None:
                context = await manager.new_context(
                    task_name=f"task-{index}", storage_state=session_path
                )
            sniffer.attach(context)
            page = await context.new_page()
            page.set_default_timeout(PAGE_LOAD_TIMEOUT_MS)

            # 打开页面
            reporter.detail("正在打开页面并模拟真人浏览")
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            except Exception as exc:
                # 加载超时通常还能继续嗅探（页面往往已经渲染出主要内容了）
                first_line = str(exc).splitlines()[0] if str(exc) else "未知原因"
                reporter.detail(f"页面加载未完全成功，继续尝试嗅探: {first_line[:120]}")

            # 有些分享链接只给手机端页面（快手图文、实况就是），桌面打开
            try:
                landed_mobile = is_mobile_landing_url(page.url or "")
            except Exception:
                landed_mobile = False
            if landed_mobile:
                reporter.detail("这条链接只有手机端页面，改用手机设备模拟访问")
                mobile_result = await _run_mobile_task(
                    manager=manager,
                    resolved_url=page.url or target_url,
                    target_url=target_url,
                    session_path=session_path,
                    reporter=reporter,
                    result=result,
                    download_lock=download_lock,
                    options=options,
                )
                if mobile_result is not None:
                    return mobile_result
                reporter.detail("手机模式也没拿到内容，继续按桌面流程判断")


            await _sniff_until_ready(
                page,
                sniffer,
                reporter,
                options.sniff_timeout,
                carousel=carousel,
            )

            unavailable = await _describe_unavailable(page, target_url)
            if unavailable:
                result.status = "failed"
                result.message = unavailable
                reporter.detail(f"已中止: {unavailable}")
                return result

            # 一条视频都没抓到，那就看看页面上有没有作品图片（图文作品）。
            content_images: List[MediaCandidate] = []
            if not sniffer.has_media():
                if sniffer.image_count:
                    await _load_more_images(page, sniffer, reporter)
                # 采集作品图片：普通 <img> 和 CSS 背景图都会扫。
                captured = [item.url for item in sniffer.image_candidates()]
                for item in await collect_content_images(page, captured_urls=captured):
                    resolution = (
                        f"{item['natural_width']}x{item['natural_height']}"
                        if item["natural_width"]
                        else "背景图"
                    )
                    content_images.append(
                        MediaCandidate(
                            url=item["url"],
                            kind="image",
                            size=0,
                            note=resolution,
                        )
                    )

            # 提取标题
            title = await extract_title(page, platform, fallback=target_url)
            result.title = title
            result.candidates = sniffer.total
            reporter.detail(f"标题: {title}")
            if result.candidates:
                reporter.detail(f"共捕获 {result.candidates} 条媒体候选")
                for line in sniffer.summary_lines(
                    max_lines=40 if options.verbose else 4,
                    include_images=options.verbose,
                ):
                    reporter.detail(f"  {line}")
            if content_images:
                reporter.detail(f"页面上识别到 {len(content_images)} 张作品图片")

            # 生成下载方案并导出 cookie
            referer = platform.referer if platform else target_url
            plan = sniffer.build_plan(referer=referer)
            cookies = await manager.read_cookies(context)
            cookie_header = build_cookie_header(cookies, target_url)

            # 轮播作品（小红书多视频笔记）每一页都是一条独立视频
            carousel_videos = sniffer.distinct_videos() if carousel else []
            if carousel and len(carousel_videos) > 1:
                video_outcome = await _deliver_videos(
                    videos=carousel_videos,
                    title=title,
                    options=options,
                    cookie_header=cookie_header,
                    referer=referer,
                    reporter=reporter,
                    download_lock=download_lock,
                    result=result,
                    target_url=target_url,
                )
                if video_outcome is not None:
                    return video_outcome

            # 图文作品要在上下文仍然打开的时候下载图片
            if plan is None and content_images:
                image_outcome = await _deliver_images(
                    context=context,
                    images=content_images,
                    title=title,
                    options=options,
                    reporter=reporter,
                    download_lock=download_lock,
                    result=result,
                    target_url=target_url,
                )
        finally:
            # 无论成功失败都关闭上下文，释放该任务的 cookie 和缓存
            await manager.close_context(context)
            # 配置目录用完要解锁，否则同平台的下一条链接会一直等
            if profile_lock is not None:
                profile_lock.release()

        if image_outcome is not None:
            return image_outcome

        if plan is None:
            message = (
                "未嗅探到可用媒体流，也没有发现作品图片。常见原因：给的是首页或列表页"
                "（请换成具体作品链接）、需要登录才能查看，或者页面结构比较特殊"
            )
            reporter.warn(message)
            # 把观察到的响应数量打出来，方便判断是"页面没请求媒体"还是"请求了但没识别出来"
            reporter.detail(
                f"本次共观察到 {sniffer.seen_responses} 个网络响应，"
                f"识别出 {sniffer.total} 条媒体候选、{sniffer.image_count} 张候选图片"
            )
            result.message = message
            return result

        reporter.detail(f"下载方案: {plan.describe()}")
        if sniffer.image_count:
            # 实况图这类作品：视频只是"动起来的那几秒"，另外还有静图。
            reporter.detail(f"另外检测到 {sniffer.image_count} 张图片（实况静图），当前只保存视频")

        # 串行下载，保证进度条输出干净
        async with download_lock:
            try:
                size, output_path = await _deliver(
                    plan=plan,
                    title=title,
                    options=options,
                    cookie_header=cookie_header,
                    reporter=reporter,
                )
            except Exception as exc:
                message = f"下载失败: {exc}"
                reporter.error(message)
                result.message = message
                return result

        result.success = True
        result.output_path = output_path
        result.size = size
        result.mode = plan.mode
        result.message = "下载完成"
        reporter.info(f"已保存: {output_path}（{console.format_bytes(size)}）")

        write_download_record(
            {
                "url": target_url,
                "platform": result.platform_name or "通用",
                "title": title,
                "mode": plan.mode,
                "reason": plan.reason,
                "output": output_path,
                "size": size,
                "candidates": [item.as_dict() for item in sniffer.snapshot(limit=20)],
            }
        )
        return result


# 配置目录 -> 异步锁。同一个平台的两条链接不能同时用同一个配置目录
_PROFILE_LOCKS: Dict[str, asyncio.Lock] = {}


def _profile_lock(profile_dir: str) -> asyncio.Lock:
    """取某个配置目录对应的锁（没有就新建）。"""
    lock = _PROFILE_LOCKS.get(profile_dir)
    if lock is None:
        lock = asyncio.Lock()
        _PROFILE_LOCKS[profile_dir] = lock
    return lock


CONTENT_ID_PATTERN = re.compile(
    r"/(?:short-video|video|note|photo|explore|status)/([0-9A-Za-z_-]{6,})"
)


def content_id_of(url: str) -> str:
    """从常见的内容页地址里抠出作品 id，抠不出返回空串。"""
    matched = CONTENT_ID_PATTERN.search(url or "")
    return matched.group(1) if matched else ""


async def _describe_unavailable(page: Page, requested_url: str) -> str:
    """判断"这条作品在网页端到底拿不拿得到"，拿不到就返回一句人话，拿得到返回空串。"""
    try:
        current = page.url or ""
    except Exception:
        current = ""

    expected = content_id_of(requested_url)
    actual = content_id_of(current)
    if expected and actual and expected != actual:
        return f"页面跳到了另一个作品（{actual}），目标作品在网页端不可见，已中止避免下错"

    if is_mobile_landing_url(current):
        return "这条链接是 App 专用分享，桌面打开只有手机端落地页，网页端没有作品页"

    script = """
    () => {
        const text = (document.body.innerText || '');
        return {
            scan: text.indexOf('扫码下载') >= 0,
            now: text.indexOf('立即下载') >= 0,
            store: text.indexOf('AppStore') >= 0 || text.indexOf('Android') >= 0,
            needLogin: text.indexOf('登录查看更多作品') >= 0,
            feed: text.indexOf('播放其他精彩视频') >= 0,
        };
    }
    """
    try:
        flags = await page.evaluate(script)
    except Exception:
        return ""

    if flags.get("feed") or flags.get("needLogin"):
        return "快手没有给出这条作品，页面正在播放推荐流的其他视频，已中止避免下错"
    if flags.get("scan") or (flags.get("now") and flags.get("store")):
        return "这条链接跳到了 App 下载引导页，网页端没有对应作品页，已中止避免下错宣传视频"
    return ""


async def _has_stalled_player(page: Page) -> bool:
    """页面上有 video 元素，但它还没加载出时长（readyState 还在 0/1）。"""
    script = """
    () => {
        const videos = Array.from(document.querySelectorAll('video'));
        if (videos.length === 0) return false;
        return videos.some(v => !v.duration || isNaN(v.duration) || v.readyState < 2);
    }
    """
    try:
        return bool(await page.evaluate(script))
    except Exception:
        return False


MOBILE_IMAGE_SCRIPT = """
(photoId) => {
    // 收集页面上真实渲染出来的大图：<img> 和 CSS 背景图都算。
    // 快手手机分享页把作品图既放在 <img> 里，也放在容器的 background-image 上。
    const found = [];
    const push = (url, naturalWidth, naturalHeight, visibleWidth) => {
        if (!url || url.indexOf('data:') === 0) return;
        if (naturalWidth && naturalWidth < 400) return;
        if (visibleWidth && visibleWidth < 120) return;
        found.push({
            url: url,
            naturalWidth: naturalWidth || 0,
            naturalHeight: naturalHeight || 0,
            visibleWidth: Math.round(visibleWidth || 0),
        });
    };

    document.querySelectorAll('img').forEach(img => {
        const src = img.currentSrc || img.src || '';
        const rect = img.getBoundingClientRect();
        push(src, img.naturalWidth, img.naturalHeight, rect.width);
    });

    document.querySelectorAll('*').forEach(el => {
        const style = getComputedStyle(el);
        const background = style.backgroundImage || '';
        const matched = background.match(/url\\(["']?(.*?)["']?\\)/);
        if (!matched) return;
        const rect = el.getBoundingClientRect();
        push(matched[1], 0, 0, rect.width);
    });

    return { works: found, photoId: photoId };
}
"""


# 图片地址里明显是界面素材的路径，直接排除
IMAGE_URL_NOISE = ("/uhead/", "/bs2/", "wskwai.com", "data:image")

# 图集图片的命名：<前缀>_<序号>.jpg
_ATLAS_INDEX = re.compile(r"^(.*)_(\d+)\.(?:jpg|jpeg|png|webp)$", re.IGNORECASE)


def _image_series_key(url: str):
    """把图片地址归到"同一个系列"里，返回 (系列前缀, 序号)。"""
    path = (url or "").split("?")[0].split("~")[0]
    matched = _ATLAS_INDEX.match(path)
    if matched:
        return matched.group(1), int(matched.group(2))
    return path, -1


async def _collect_mobile_images(page: Page, photo_id: str) -> List[MediaCandidate]:
    """从手机分享页里挑出"这条作品自己的"图片。"""
    try:
        payload = await page.evaluate(MOBILE_IMAGE_SCRIPT, photo_id)
    except Exception:
        return []

    items = payload.get("works") if isinstance(payload, dict) else None
    if not items:
        return []

    # 先洗干净：去掉空地址、界面素材、宽高都太小的
    cleaned: List[dict] = []
    for item in items:
        url = str(item.get("url") or "")
        if not url:
            continue
        if any(noise in url for noise in IMAGE_URL_NOISE):
            continue
        natural_width = int(item.get("naturalWidth") or 0)
        natural_height = int(item.get("naturalHeight") or 0)
        visible_width = int(item.get("visibleWidth") or 0)
        # 懒加载中的图自然尺寸还是 0，但占位宽度够大，也要留着
        if natural_width < 400 and visible_width < 150:
            continue
        cleaned.append(
            {
                "url": url,
                "natural_width": natural_width,
                "natural_height": natural_height,
                "visible_width": visible_width,
            }
        )

    if not cleaned:
        return []

    chosen: List[dict] = []

    # 路线一：地址里带作品 id
    if photo_id:
        chosen = [item for item in cleaned if photo_id in item["url"]]

    # 路线二：按系列分组，取成员最多的那组（图集）
    if not chosen:
        groups: Dict[str, List[tuple]] = {}
        for item in cleaned:
            key, index = _image_series_key(item["url"])
            groups.setdefault(key, []).append((index, item))
        if groups:
            biggest = max(groups.values(), key=len)
            if len(biggest) >= 2:
                # 按序号排好序，保证存下来的顺序和图集顺序一致
                biggest.sort(key=lambda pair: pair[0])
                chosen = [item for _, item in biggest]

    # 都不成立：那就拿可见面积最大的那一张（单图作品的兜底）
    if not chosen:
        chosen = [max(cleaned, key=lambda item: item["visible_width"])]

    # 按地址去重，保留自然尺寸最大的那一份
    best: Dict[str, dict] = {}
    for item in chosen:
        key = item["url"].split("?")[0].split("~")[0]
        area = item["natural_width"] * item["natural_height"]
        previous = best.get(key)
        if previous is None or area > previous["area"]:
            best[key] = {"url": item["url"], "area": area, "item": item}

    candidates: List[MediaCandidate] = []
    for entry in best.values():
        item = entry["item"]
        width = item["natural_width"]
        height = item["natural_height"]
        note = f"{width}x{height}" if width and height else "作品图"
        candidates.append(MediaCandidate(url=entry["url"], kind="image", size=0, note=note))
    return candidates


MOBILE_TITLE_SCRIPT = """
() => {
    const content = (selector) => {
        const el = document.querySelector(selector);
        return el ? String(el.getAttribute('content') || '').trim() : '';
    };
    return {
        og: content('meta[property="og:title"]'),
        ogDesc: content('meta[property="og:description"]'),
        desc: content('meta[name="description"]'),
        title: String(document.title || '').trim(),
        body: (document.body.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 400),
    };
}
"""


async def _extract_mobile_title(
    page: Page,
    fallback: str,
    platform_name: str = "快手",
    photo_id: str = "",
) -> str:
    """手机分享页的标题提取。"""
    try:
        payload = await page.evaluate(MOBILE_TITLE_SCRIPT)
    except Exception:
        payload = {}

    if isinstance(payload, dict):
        for key in ("og", "ogDesc", "desc"):
            value = str(payload.get(key) or "").strip()
            if value and value != platform_name:
                return value

        body = str(payload.get("body") or "").strip()
        captured = _caption_from_body(body)
        if captured:
            return captured

        title = str(payload.get("title") or "").strip()
        if title and title != platform_name:
            return title

        if body and "邀请你一起玩" not in body[:20]:
            return body[:40]

    if photo_id:
        return f"{platform_name}-{photo_id}"
    return fallback


_CAPTION_PATTERNS = (
    re.compile(r"我也要拍\s*@\s*[^\s@]+\s+(.{2,80}?)\s*打开快手"),
    re.compile(r"打开看看\s+(.{2,60}?)\s+我也要拍"),
)


def _caption_from_body(body: str) -> str:
    """从手机分享页的正文里抠出文案片段。"""
    if not body:
        return ""
    for pattern in _CAPTION_PATTERNS:
        matched = pattern.search(body)
        if not matched:
            continue
        caption = matched.group(1).strip(" -|·")

        if len(caption) >= 4:
            return caption
    return ""


async def _run_mobile_task(
    manager: BrowserManager,
    resolved_url: str,
    target_url: str,
    session_path: Optional[str],
    reporter: TaskReporter,
    result: TaskResult,
    download_lock: asyncio.Semaphore,
    options: TaskOptions,
) -> Optional[TaskResult]:
    """用手机设备模拟打开分享页，把作品图（以及配合作品的原声）取下来。"""
    ids = kuaishou_work_ids(resolved_url)
    photo_id = ids.get("photo_id", "")
    reporter.detail(f"作品 id: {photo_id or '未知'}")

    context = await manager.new_context(
        task_name="mobile", storage_state=session_path, mobile=True
    )
    audio_urls: List[str] = []
    try:
        # 记下媒体响应，用于事后匹配"作品原声"
        def on_response(response) -> None:
            try:
                content_type = (response.headers or {}).get("content-type", "")
            except Exception:
                return
            if "audio" in content_type:
                audio_urls.append(response.url)

        context.on("response", on_response)

        page = context.pages[0] if context.pages else await context.new_page()
        page.set_default_timeout(PAGE_LOAD_TIMEOUT_MS)
        try:
            await page.goto(resolved_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        except Exception as exc:
            first_line = str(exc).splitlines()[0] if str(exc) else "未知原因"
            reporter.detail(f"手机页加载未完成，继续尝试: {first_line[:100]}")

        # 手机页面靠触摸滑动触发懒加载，先给它几下
        await asyncio.sleep(4.0)
        for _ in range(3):
            try:
                await page.touchscreen.tap(195, 420)
            except Exception:
                pass
            try:
                await page.mouse.wheel(0, 380)
            except Exception:
                pass
            await asyncio.sleep(1.6)

        if is_mobile_landing_url(page.url or "") and "captcha" in (page.url or ""):
            result.status = "failed"
            result.message = "被平台的人机验证拦住了，稍等几分钟再试"
            return result

        images = await _collect_mobile_images(page, photo_id)
        if not images:
            return None

        title = await _extract_mobile_title(
            page, target_url, platform_name="快手", photo_id=photo_id
        )
        result.title = title
        reporter.detail(f"标题: {title}")
        reporter.detail(f"页面上识别到 {len(images)} 张作品图片")

        outcome = await _deliver_images(
            context=context,
            images=images,
            title=title,
            options=options,
            reporter=reporter,
            download_lock=download_lock,
            result=result,
            target_url=target_url,
            # 手机模式下后面还可能追加原声和合成视频
            announce=False,
        )

        # 顺带把作品原声也存下来：图文/实况作品的"动"就在这段音频里。
        audio_name = _match_work_audio(audio_urls, ids.get("object_id", ""))
        audio_path = ""
        if audio_name and outcome.output_path:
            audio_path = await _save_work_audio(
                context, audio_name, outcome.output_path, outcome, reporter
            )

        # 图文/实况作品网页端只给"一张静图 + 一段原声"，没有视频文件。
        if audio_path and len(images) == 1 and outcome.output_path:
            await _compose_still_video(
                directory=outcome.output_path,
                audio_path=audio_path,
                result=outcome,
                reporter=reporter,
            )

        # 全部完成后再报一次，这里的体积才是最终值
        if outcome.output_path:
            file_count = len(os.listdir(outcome.output_path))
            reporter.info(
                f"已保存 {file_count} 个文件，输出目录: {outcome.output_path}"
                f"（{console.format_bytes(outcome.size)}）"
            )
        return outcome
    finally:
        await manager.close_context(context)


def _match_work_audio(audio_urls: List[str], object_id: str) -> str:
    """从抓到的音频地址里挑出属于这条作品的那一个。"""
    if not object_id:
        return ""
    # 原声文件名里带 shareObjectId 的 base64。取前 12 位数字去编码
    head = object_id[:12]
    marker = base64.b64encode(head.encode("utf-8")).decode("ascii").rstrip("=")
    if len(marker) < 8:
        return ""
    for url in audio_urls:
        if marker and marker in url:
            return url
    return ""


async def _save_work_audio(
    context: BrowserContext,
    audio_url: str,
    directory: str,
    result: TaskResult,
    reporter: TaskReporter,
) -> str:
    """把作品原声存到图片目录里，返回保存路径（失败返回空串）。"""
    try:
        target = unique_output_path(directory, "作品原声", ".m4a")
        response = await context.request.get(audio_url, timeout=60_000)
        if not response.ok:
            reporter.detail(f"原声下载失败: HTTP {response.status}")
            return ""
        payload = await response.body()
        if not payload:
            return ""
        with open(target, "wb") as handle:
            handle.write(payload)
        result.size += len(payload)
        result.message = f"{result.message}，含原声"
        reporter.detail(f"已保存作品原声: {os.path.basename(target)}（{console.format_bytes(len(payload))}）")
        return target
    except Exception as exc:
        reporter.detail(f"原声下载跳过: {str(exc).splitlines()[0][:80]}")
        return ""


async def _compose_still_video(
    directory: str,
    audio_path: str,
    result: TaskResult,
    reporter: TaskReporter,
) -> None:
    """把"一张静图 + 一段原声"合成为一个 mp4。"""
    ffmpeg_bin = get_ffmpeg_bin()
    if not ffmpeg_bin:
        reporter.detail("没找到 FFmpeg，跳过合成")
        return

    # 找刚下下来的第一张图当画面
    image_path = ""
    try:
        for name in sorted(os.listdir(directory)):
            lowered = name.lower()
            if lowered.endswith((".jpg", ".jpeg", ".png", ".webp")):
                image_path = os.path.join(directory, name)
                break
    except OSError:
        return

    if not image_path or not os.path.exists(audio_path):
        return

    target = unique_output_path(directory, "实况合成", ".mp4")
    command = [
        ffmpeg_bin,
        "-y",
        "-loglevel", "error",
        # 一张图循环成视频流
        "-loop", "1",
        "-i", image_path,
        "-i", audio_path,
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "128k",
        # 以音频时长为准截断，画面跟着音频走
        "-shortest",
        target,
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
    except Exception as exc:
        reporter.detail(f"合成失败: {str(exc).splitlines()[0][:80]}")
        return

    if process.returncode != 0:
        message = (stderr or b"").decode("utf-8", "replace").splitlines()
        detail = message[-1] if message else "未知原因"
        reporter.detail(f"合成失败: {detail[:100]}")
        return

    try:
        size = os.path.getsize(target)
    except OSError:
        size = 0
    result.size += size
    result.message = f"{result.message}，已合成静图视频"
    reporter.detail(f"已合成视频: {os.path.basename(target)}（{console.format_bytes(size)}）")
    reporter.detail("说明: 网页端只提供静图和原声，合成后画面是静态的")


async def _sniff_until_ready(
    page: Page,
    sniffer: MediaSniffer,
    reporter: TaskReporter,
    timeout: float,
    carousel: bool = False,
) -> None:
    """一边模拟真人浏览，一边等待媒体流出现。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(timeout, 1.0)
    started = loop.time()

    simulation = asyncio.create_task(
        simulate_viewing(
            page=page,
            has_media=sniffer.has_media,
            budget=min(
                timeout,
                MAX_SIMULATION_S + (CAROUSEL_EXTRA_BUDGET_S if carousel else 0.0),
            ),
            carousel=carousel,
        )
    )

    status = console.StatusLine(label="嗅探中") if reporter.quiet else None
    last_report = loop.time()
    grace_deadline: Optional[float] = None
    retry_attempts = 0
    next_retry_at = 3.0
    grace_extension = 0

    try:
        while True:
            now = loop.time()
            elapsed = now - started
            # 轮播平台不能"一有媒体就收工"：第一份媒体秒到
            if sniffer.has_media() and not carousel:
                break
            if now >= deadline:
                if status is not None:
                    status.close()
                # 超时本身不算错误，后面的"没抓到东西"提示会说明白，这里只记细节
                reporter.detail("嗅探时间已到，仍未拿到完整媒体流")
                break

            # 有些站点的播放器会先弹一句"浏览器版本过低 / 加载失败"（其实是误报）
            if not sniffer.has_media() and elapsed >= next_retry_at and retry_attempts < 2:
                retry_attempts += 1
                next_retry_at = elapsed + 6.0
                clicked = await recover_player(page)
                if clicked:
                    reporter.detail(f"播放器报错，已点击「{clicked}」，等待重新加载")

            # 模拟动作做完还没结果时，只再宽限几秒，不必耗满整个预算。
            if simulation.done() and grace_deadline is None:
                # 轮播平台多翻几页要时间，收工前多留一会儿给最后一份媒体拉流
                grace_deadline = now + (CAROUSEL_GRACE_S if carousel else POST_SIMULATION_GRACE_S)
                try:
                    advanced = simulation.result()
                except Exception:
                    advanced = 0
                if isinstance(advanced, int) and advanced > 0:
                    reporter.detail(f"已翻阅 {advanced} 页，逐页采集媒体")
            if grace_deadline is not None and now >= grace_deadline:
                if grace_extension < 1 and await _has_stalled_player(page):
                    grace_extension += 1
                    grace_deadline = now + STALLED_PLAYER_GRACE_S
                    reporter.detail(
                        f"页面上有播放器但还没起播，再等 {int(STALLED_PLAYER_GRACE_S)} 秒"
                    )
                    clicked = await recover_player(page)
                    if clicked:
                        reporter.detail(f"播放器报错，已点击「{clicked}」")
                    continue
                break

            if status is not None:
                # 头一秒不刷状态，避免一闪而过地显示 "0s 候选 0"
                elapsed = now - started
                if elapsed >= 1.0:
                    status.update(f"{elapsed:.0f}s  候选 {sniffer.total}")
            elif now - last_report >= PROGRESS_REPORT_INTERVAL_S:
                reporter.detail(describe_wait_state(sniffer, now - started))
                last_report = now

            await asyncio.sleep(SNIFF_POLL_INTERVAL_S)
    finally:
        if status is not None:
            status.close()
        if not simulation.done():
            simulation.cancel()
            try:
                await simulation
            except (asyncio.CancelledError, Exception):
                pass
        else:

            try:
                simulation.result()
            except Exception:
                pass


async def _deliver(
    plan: DownloadPlan,
    title: str,
    options: TaskOptions,
    cookie_header: str,
    reporter: TaskReporter,
) -> Tuple[int, str]:
    """执行下载方案，返回（文件大小, 输出路径）。"""
    stem = sanitize_filename(title)
    extension = guess_output_extension(plan)
    output_path = unique_output_path(options.output_dir, stem, extension)
    output_dir = os.path.dirname(output_path)
    base_name = os.path.splitext(os.path.basename(output_path))[0]

    headers = DownloadHeaders(
        referer=plan.referer,
        user_agent=DEFAULT_USER_AGENT,
        cookie=cookie_header,
    )

    if plan.mode == "hls":
        reporter.detail("清单地址: " + plan.manifest_url[:100])
        size = await download_hls(
            url=plan.manifest_url,
            destination=output_path,
            headers=headers,
            label=f"{reporter.progress_prefix()}拉流",
        )
        return size, output_path

    if plan.mode == "tracks":
        video_temp = os.path.join(output_dir, f"{base_name}.video.m4s")
        audio_temp = os.path.join(output_dir, f"{base_name}.audio.m4s")

        reporter.detail(f"视频轨: {console.format_bytes(plan.video_size)}  {plan.video_url[:80]}")
        reporter.detail(f"音频轨: {console.format_bytes(plan.audio_size)}  {plan.audio_url[:80]}")

        await download_file(
            url=plan.video_url,
            destination=video_temp,
            headers=headers,
            label=f"{reporter.progress_prefix()}视频轨",
        )
        await download_file(
            url=plan.audio_url,
            destination=audio_temp,
            headers=headers,
            label=f"{reporter.progress_prefix()}音频轨",
        )
        size = await mux_tracks(
            video_path=video_temp,
            audio_path=audio_temp,
            destination=output_path,
            label=f"{reporter.progress_prefix()}合流",
        )
        return size, output_path

    # single 模式
    reporter.detail("媒体地址: " + plan.video_url[:100])
    size = await download_file(
        url=plan.video_url,
        destination=output_path,
        headers=headers,
        label=f"{reporter.progress_prefix()}下载",
    )
    return size, output_path


async def _deliver_videos(
    videos: List[MediaCandidate],
    title: str,
    options: TaskOptions,
    cookie_header: str,
    referer: str,
    reporter: TaskReporter,
    download_lock: asyncio.Semaphore,
    result: TaskResult,
    target_url: str,
) -> Optional[TaskResult]:
    """把轮播里的多条视频逐个下载到一个文件夹，序号即翻页顺序。"""
    stem = sanitize_filename(title)
    directory = unique_directory(os.path.join(options.output_dir, stem))
    headers = DownloadHeaders(
        referer=referer,
        user_agent=DEFAULT_USER_AGENT,
        cookie=cookie_header,
    )

    saved = 0
    total_size = 0
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        reporter.warn(f"无法创建输出目录: {exc}")
        return None

    reporter.info(f"共 {len(videos)} 条视频，逐条下载")
    async with download_lock:
        for index, item in enumerate(videos, start=1):
            target = os.path.join(directory, f"{index:02d}.mp4")
            try:
                size = await download_file(
                    url=item.url,
                    destination=target,
                    headers=headers,
                    label=f"{reporter.progress_prefix()}视频 {index}/{len(videos)}",
                    fallback_referer=referer,
                )
            except Exception as exc:
                reporter.warn(f"第 {index} 条下载失败: {str(exc).splitlines()[0][:100]}")
                continue
            saved += 1
            total_size += size
            reporter.detail(f"第 {index} 条已保存（{console.format_bytes(size)}）")

    if saved == 0:
        return None

    result.success = True
    result.output_path = directory
    result.size = total_size
    result.mode = "videos"
    result.message = f"共 {len(videos)} 条视频，已保存 {saved} 条"
    reporter.info(f"已保存 {saved}/{len(videos)} 条，输出目录: {directory}（{console.format_bytes(total_size)}）")

    write_download_record(
        {
            "url": target_url,
            "platform": result.platform_name or "通用",
            "title": title,
            "mode": "videos",
            "reason": "轮播作品，逐页采集",
            "output": directory,
            "size": total_size,
            "file_count": saved,
            "candidates": [item.as_dict() for item in videos[:20]],
        }
    )
    return result


async def _deliver_images(
    context: BrowserContext,
    images,
    title: str,
    options: TaskOptions,
    reporter: TaskReporter,
    download_lock: asyncio.Semaphore,
    result: TaskResult,
    target_url: str,
    announce: bool = True,
) -> TaskResult:
    """图文作品的交付路径：把抓到的图片存进一个以标题命名的文件夹。"""
    stem = sanitize_filename(title)
    directory = unique_directory(os.path.join(options.output_dir, stem))

    reporter.info(f"图文作品，共 {len(images)} 张图片")
    reporter.detail(f"保存目录: {directory}")

    async with download_lock:
        saved = await download_images_via_browser(
            context=context,
            images=images,
            directory=directory,
            label=f"{reporter.progress_prefix()}图片",
        )

    if not saved:
        message = "识别为图文作品，但所有图片都下载失败了"
        reporter.error(message)
        result.message = message
        return result

    total_size = 0
    for path in saved:
        try:
            total_size += os.path.getsize(path)
        except OSError:
            pass

    result.success = True
    result.output_path = directory
    result.size = total_size
    result.mode = "images"
    result.message = f"图文作品，已保存 {len(saved)} 张图片"
    if announce:
        reporter.info(f"已保存 {len(saved)}/{len(images)} 张，输出目录: {directory}（{console.format_bytes(total_size)}）")

    write_download_record(
        {
            "url": target_url,
            "platform": result.platform_name or "通用",
            "title": title,
            "mode": "images",
            "reason": "作品没有视频流，保存图片",
            "output": directory,
            "size": total_size,
            "file_count": len(saved),
            "candidates": [item.as_dict() for item in images[:20]],
        }
    )
    return result


def summarize_results(results: List[TaskResult], quiet: bool = True) -> int:
    """打印最终汇总，返回进程退出码。"""
    success_count = sum(1 for item in results if item.success)

    if not quiet or len(results) > 1:
        console.rule()
        for item in results:
            if item.success:
                console.detail(f"{item.output_path}（{console.format_bytes(item.size)}）", indent=0)
            else:
                console.detail(f"失败 | {item.url} | {item.message}", indent=0)
        console.rule()
    else:
        # 单条链接 + 静默模式：成功时保存路径已经打印过了，不必再重复；
        item = results[0]
        if not item.success:
            console.error(f"失败: {item.message or '没有拿到可下载的内容'}")

    if len(results) > 1:
        console.info(f"成功 {success_count} 条，失败 {len(results) - success_count} 条")
    return 0 if success_count == len(results) and results else 1
