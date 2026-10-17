"""媒体嗅探核心模块"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .config import MIN_IMAGE_BYTES, MIN_MEDIA_BYTES, NOISE_KEYWORDS
from .platforms import Platform

# 识别用的关键词表

# 播放清单特征
MANIFEST_URL_HINTS = (".m3u8", ".mpd")
MANIFEST_TYPE_HINTS = ("mpegurl", "dash+xml")

# 音频轨特征
AUDIO_URL_HINTS = (
    "media-audio",
    "audio-only",
    "audiotrack",
    "mime_type=audio",
    "-30216",
    "-30232",
    "-30250",
    "-30280",
    "/audio/",
    "_audio.",
    "audio.mp4",
)
AUDIO_TYPE_HINTS = ("audio/",)

# 视频轨特征
VIDEO_URL_HINTS = (
    "media-video",
    "video-only",
    "videoplayback",
    "mime_type=video",
    "-30016",
    "-30032",
    "-30064",
    "-30077",
    "-30080",
    "/video/",
    ".mp4",
    ".flv",
    ".webm",
    ".mkv",
    ".mov",
)
VIDEO_TYPE_HINTS = ("video/",)

# 一眼就能确定是分片的地址特征。
SEGMENT_STRICT_HINTS = (".ts", ".cmfv", ".cmfa", ".fmp4")

# 需要继续等待音频轨的"纯视频轨"特征
VIDEO_ONLY_HINTS = ("-300", "-301", "video-only", "mime_type=video")

# 纯静态资源类型前缀，直接忽略。
STATIC_TYPE_PREFIXES = (
    "image/",
    "font/",
    "text/css",
    "text/html",
    "text/plain",
    "application/javascript",
    "application/json",
    "application/xml",
)

# 需要忽略的请求资源类型（Playwright 的 request.resource_type）
IGNORED_RESOURCE_TYPES = ("image", "font", "stylesheet", "script")

# 同一个路径前缀下出现多少个候选后，判定这些候选是分片
SEGMENT_SIBLING_THRESHOLD = 5

# 候选类型排序权重，数字越大越优先
KIND_ORDER = {
    "video": 3,
    "audio": 3,
    "manifest": 2,
    "segment": 1,
    "unknown": 0,
}


# 数据结构


@dataclass
class MediaCandidate:
    """一条被嗅探到的媒体候选资源。"""

    url: str
    kind: str
    size: int = 0
    content_type: str = ""
    resource_type: str = ""
    page_url: str = ""
    note: str = ""

    def as_dict(self) -> dict:
        """转成可写入日志的普通字典。"""
        return {
            "url": self.url,
            "kind": self.kind,
            "size": self.size,
            "content_type": self.content_type,
            "resource_type": self.resource_type,
            "page_url": self.page_url,
            "note": self.note,
        }


@dataclass
class DownloadPlan:
    """从候选里筛选出来的最终下载方案。"""

    mode: str
    referer: str = ""
    video_url: str = ""
    audio_url: str = ""
    manifest_url: str = ""
    video_size: int = 0
    audio_size: int = 0
    reason: str = ""

    def describe(self) -> str:
        """给出一句人类可读的方案说明。"""
        if self.mode == "hls":
            return "播放清单模式：交给 FFmpeg 直接拉取 m3u8 并合并"
        if self.mode == "tracks":
            return "分离轨道模式：分别下载视频轨和音频轨，再无损合流"
        return "单文件模式：直接下载完整媒体文件"


# 从页面 DOM 里读出真正的作品图片

# 在页面里执行的采集脚本（普通 <img> 元素）。
CONTENT_IMAGE_SCRIPT = """
() => {
    const items = [];
    const viewportWidth = window.innerWidth || 0;
    const viewportHeight = window.innerHeight || 0;
    document.querySelectorAll('img').forEach(img => {
        const src = img.currentSrc || img.src || '';
        if (!src.startsWith('http')) return;
        const rect = img.getBoundingClientRect();
        items.push({
            src: src,
            naturalWidth: img.naturalWidth || 0,
            naturalHeight: img.naturalHeight || 0,
            displayWidth: Math.round(rect.width),
            displayHeight: Math.round(rect.height),
            viewportWidth: viewportWidth,
            viewportHeight: viewportHeight,
        });
    });
    return items;
}
"""

# 在页面里执行的采集脚本（CSS 背景图）。
CONTENT_BACKGROUND_SCRIPT = r"""
() => {
    const items = [];
    const viewportWidth = window.innerWidth || 0;
    const viewportHeight = window.innerHeight || 0;
    const pattern = /url\(["']?(.*?)["']?\)/;
    document.querySelectorAll('div, section, article, a, span, li, figure').forEach(el => {
        const style = getComputedStyle(el);
        const background = style.backgroundImage || '';
        if (!background || background === 'none' || background.indexOf('url(') < 0) return;
        const match = background.match(pattern);
        if (!match || !match[1]) return;
        const rect = el.getBoundingClientRect();
        if (rect.width < 1 || rect.height < 1) return;
        items.push({
            src: match[1],
            naturalWidth: 0,
            naturalHeight: 0,
            displayWidth: Math.round(rect.width),
            displayHeight: Math.round(rect.height),
            viewportWidth: viewportWidth,
            viewportHeight: viewportHeight,
        });
    });
    return items;
}
"""

# 作品图片的最小原图宽度（像素）。小于这个的基本是图标、表情、头像。
MIN_NATURAL_WIDTH = 400

# 作品图片的显示面积至少要占视口的这个比例。
MIN_VIEWPORT_AREA_RATIO = 0.20


def _image_base_key(url: str) -> str:
    """取图片的基础标识，用来把同一张图的不同处理版本合并成一条。"""
    return url.split("~", 1)[0].split("?", 1)[0]


def _pick_best_variant(variants: List[dict], captured: Optional[set] = None) -> dict:
    """同一张图有多个版本时挑一个最合适的。"""
    captured = captured or set()

    def score(item: dict) -> tuple:
        downloaded = 1 if item["url"] in captured else 0
        return (downloaded, item["natural_width"] * item["natural_height"])

    best = variants[0]
    for item in variants[1:]:
        if score(item) > score(best):
            best = item
    return best


async def collect_content_images(page, captured_urls=None) -> List[dict]:
    """从页面里挑出真正的作品图片（普通 <img> 和 CSS 背景图都算）。"""
    captured = set(captured_urls or ())

    raw_items: List[dict] = []
    for script in (CONTENT_IMAGE_SCRIPT, CONTENT_BACKGROUND_SCRIPT):
        try:
            collected = await page.evaluate(script)
        except Exception:
            collected = []
        if collected:
            raw_items.extend(collected)

    groups: Dict[str, List[dict]] = {}
    for item in raw_items:
        try:
            url = str(item.get("src", ""))
            natural_width = int(item.get("naturalWidth", 0))
            natural_height = int(item.get("naturalHeight", 0))
            display_width = int(item.get("displayWidth", 0))
            display_height = int(item.get("displayHeight", 0))
            viewport_width = int(item.get("viewportWidth", 0))
            viewport_height = int(item.get("viewportHeight", 0))
        except (TypeError, ValueError):
            continue

        if not url.startswith("http"):
            continue

        # 靠下面的"显示面积占比"来把关。
        if natural_width and natural_width < MIN_NATURAL_WIDTH:
            continue

        # 显示面积占视口的比例。视口拿不到时退回绝对尺寸判断。
        if viewport_width > 0 and viewport_height > 0:
            ratio = (display_width * display_height) / float(viewport_width * viewport_height)
            displayed_large = ratio >= MIN_VIEWPORT_AREA_RATIO
        else:
            displayed_large = display_width >= 240

        entry = {
            "url": url,
            "natural_width": natural_width,
            "natural_height": natural_height,
            "displayed_large": displayed_large,
        }
        groups.setdefault(_image_base_key(url), []).append(entry)

    # 一组里只要有一个版本显示得够大，就认定这组是作品图片；
    images: List[dict] = []
    for variants in groups.values():
        if not any(item["displayed_large"] for item in variants):
            continue
        best = _pick_best_variant(variants, captured)
        images.append(
            {
                "url": best["url"],
                "natural_width": best["natural_width"],
                "natural_height": best["natural_height"],
            }
        )

    images.sort(key=lambda item: item["natural_width"] * item["natural_height"], reverse=True)
    return images


class MediaSniffer:
    """挂在上下文上的媒体嗅探器。"""

    def __init__(
        self,
        platform: Optional[Platform] = None,
        min_bytes: int = MIN_MEDIA_BYTES,
        min_image_bytes: int = MIN_IMAGE_BYTES,
        noise_keywords: tuple = NOISE_KEYWORDS,
    ) -> None:
        self.platform = platform
        self.min_bytes = min_bytes
        self.min_image_bytes = min_image_bytes
        self.noise_keywords = noise_keywords
        # 以 URL 为键做去重，同一个地址被请求多次（例如 Range 分段）只留一条
        self._candidates: Dict[str, MediaCandidate] = {}
        # 图片单独存一份，键是"去掉缩放参数后的图片标识"
        self._images: Dict[str, MediaCandidate] = {}
        # 统计每个路径前缀下的媒体数量，用于识别分片
        self._prefix_counter: Dict[str, int] = {}
        # 每个路径前缀下已经记录过哪些候选，降级时要一起改
        self._prefix_members: Dict[str, List[str]] = {}
        # 原始响应条数，仅用于日志
        self.seen_responses = 0


    def attach(self, target) -> None:
        """开始监听。"""
        target.on("response", self._on_response)


    def _on_response(self, response) -> None:
        """每收到一个网络响应就调用一次。"""
        self.seen_responses += 1
        try:
            candidate = self._analyze(response)
        except Exception:
            return
        if candidate is not None:
            self._remember(candidate)


    def _analyze(self, response) -> Optional[MediaCandidate]:
        """判定单个响应是否为媒体，是则返回候选对象。"""
        try:
            status = response.status
        except Exception:
            return None
        # 只关心成功响应。204/304 没有实体，重定向交给最终响应处理。
        if status < 200 or status >= 400:
            return None

        try:
            url = response.url or ""
        except Exception:
            return None
        if not url or not url.startswith("http"):
            return None

        url_lower = url.lower()

        # 排除埋点和上报
        if any(keyword in url_lower for keyword in self.noise_keywords):
            return None

        try:
            headers = response.headers or {}
        except Exception:
            headers = {}
        content_type = str(headers.get("content-type", "")).lower()
        size = self._estimate_size(headers)

        try:
            resource_type = response.request.resource_type or ""
        except Exception:
            resource_type = ""

        # 图片。
        if content_type.startswith("image/") or url_lower.split("?")[0].endswith(
            (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")
        ):
            if size >= self.min_image_bytes:
                self._remember_image(url, size, content_type)
            return None

        # 播放清单，优先级最高，不吃体积阈值
        if any(hint in url_lower for hint in MANIFEST_URL_HINTS) or any(
            hint in content_type for hint in MANIFEST_TYPE_HINTS
        ):
            return self._make_candidate(url, "manifest", size, content_type, resource_type, response, "命中清单特征")

        # 静态资源直接忽略
        if resource_type in IGNORED_RESOURCE_TYPES:
            return None
        if content_type.startswith(STATIC_TYPE_PREFIXES):
            return None

        # 判定音视频
        kind = self._classify_media(url_lower, content_type)
        if kind is None:
            return None

        # 体积过小的一律视为试看片段或广告
        if size and size < self.min_bytes:
            return None

        note = "命中音频轨特征" if kind == "audio" else "命中视频轨特征"

        # .ts 这类地址一定是分片，降级处理，不参与"完整文件"的比较
        if kind == "video" and any(hint in url_lower for hint in SEGMENT_STRICT_HINTS):
            kind = "segment"
            note = "地址带分片特征，判定为清单分片"

        return self._make_candidate(url, kind, size, content_type, resource_type, response, note)

    def _classify_media(self, url_lower: str, content_type: str) -> Optional[str]:
        """区分音频和视频，识别不出来返回 None。"""
        is_audio = any(hint in url_lower for hint in AUDIO_URL_HINTS) or any(
            hint in content_type for hint in AUDIO_TYPE_HINTS
        )
        if is_audio:
            return "audio"

        is_video = any(hint in url_lower for hint in VIDEO_URL_HINTS) or any(
            hint in content_type for hint in VIDEO_TYPE_HINTS
        )
        if is_video:
            # 命中了分片特征但仍然满足音视频特征时，先按音视频记
            return "video"
        return None

    @staticmethod
    def _estimate_size(headers: dict) -> int:
        """估算资源总大小。"""
        total = 0
        content_range = headers.get("content-range")
        if content_range and "/" in str(content_range):
            tail = str(content_range).rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                total = int(tail)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                total = max(total, int(content_length))
            except (TypeError, ValueError):
                pass
        return total

    def _make_candidate(
        self,
        url: str,
        kind: str,
        size: int,
        content_type: str,
        resource_type: str,
        response,
        note: str,
    ) -> MediaCandidate:
        """组装候选对象，顺便记录触发它的页面地址。"""
        page_url = ""
        try:
            page_url = response.frame.url or ""
        except Exception:
            page_url = ""
        return MediaCandidate(
            url=url,
            kind=kind,
            size=size,
            content_type=content_type,
            resource_type=resource_type,
            page_url=page_url,
            note=note,
        )


    @staticmethod
    def _image_key(url: str) -> str:
        """取图片的"同一张图"标识，用于把同一张图的不同尺寸合并成一条。"""
        return url.split("~tplv", 1)[0].split("?", 1)[0]

    def _remember_image(self, url: str, size: int, content_type: str) -> None:
        """记录一张作品图片，同一张图只保留体积最大的版本。"""
        key = self._image_key(url)
        existing = self._images.get(key)
        if existing is not None:
            if size > existing.size:
                existing.url = url
                existing.size = size
                existing.content_type = content_type
            return
        self._images[key] = MediaCandidate(
            url=url,
            kind="image",
            size=size,
            content_type=content_type,
            note="作品图片",
        )

    @staticmethod
    def _prefix_key(url: str) -> str:
        """取 URL 的"目录级"前缀，用于判断一批文件是不是同一组分片。"""
        try:
            parsed = urlparse(url)
        except ValueError:
            return url
        path = parsed.path or ""
        if "/" in path:
            path = path.rsplit("/", 1)[0]
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    def _remember(self, candidate: MediaCandidate) -> None:
        """把候选放进结果池，做去重、分片识别和降级。"""
        existing = self._candidates.get(candidate.url)
        if existing is not None:
            # 同一个地址多次出现，保留更大的体积估计值
            if candidate.size > existing.size:
                existing.size = candidate.size
            return

        prefix = self._prefix_key(candidate.url)
        self._prefix_counter[prefix] = self._prefix_counter.get(prefix, 0) + 1

        self._candidates[candidate.url] = candidate
        members = self._prefix_members.setdefault(prefix, [])
        members.append(candidate.url)

        # 分片识别：同一目录下同类文件太多，说明这是一组分片而不是完整文件。
        if self._prefix_counter[prefix] >= SEGMENT_SIBLING_THRESHOLD:
            for member_url in members:
                member = self._candidates.get(member_url)
                if member is None or member.kind == "manifest":
                    continue
                member.kind = "segment"
                member.note = "同一目录下同类文件过多，判定为清单分片"


    @staticmethod
    def _looks_like_video_only(url: str) -> bool:
        """判断这个视频候选是不是"只有画面、没有声音"的纯视频轨。"""
        lower = url.lower()
        if any(hint in lower for hint in VIDEO_ONLY_HINTS):
            return True
        path = lower.split("?", 1)[0]
        return path.endswith(".m4s")

    @property
    def total(self) -> int:
        """当前候选总数。"""
        return len(self._candidates)

    def has_media(self) -> bool:
        """是否已经收集到"可以开始下载"的媒体。"""
        kinds = {item.kind for item in self._candidates.values()}
        if "manifest" in kinds:
            return True
        if "video" in kinds and "audio" in kinds:
            return True
        for item in self._candidates.values():
            if item.kind == "video" and not self._looks_like_video_only(item.url):
                return True
        return False

    def video_only_count(self) -> int:
        """统计纯视频轨数量，日志里用来解释"为什么还在等"。"""
        return sum(
            1
            for item in self._candidates.values()
            if item.kind == "video" and self._looks_like_video_only(item.url)
        )

    def image_candidates(self) -> List[MediaCandidate]:
        """按体积从大到小返回抓到的作品图片（图文作品用）。"""
        items = list(self._images.values())
        items.sort(key=lambda item: item.size, reverse=True)
        return items

    @property
    def image_count(self) -> int:
        """抓到的图片数量，用于判断这是不是图文作品。"""
        return len(self._images)

    def snapshot(self, kind: Optional[str] = None, limit: int = 0) -> List[MediaCandidate]:
        """按体积从大到小返回候选副本，可按类型过滤。"""
        items = [item for item in self._candidates.values() if kind is None or item.kind == kind]
        items.sort(key=lambda item: (item.size, KIND_ORDER.get(item.kind, 0)), reverse=True)
        if limit > 0:
            items = items[:limit]
        return items

    def distinct_videos(self) -> List[MediaCandidate]:
        """返回去重后的视频候选，保持抓到的顺序。"""
        import urllib.parse

        seen = {}
        ordered: List[MediaCandidate] = []
        for item in self._candidates.values():
            if item.kind != "video":
                continue
            if ".ts" in item.url.lower():
                continue
            try:
                path = urllib.parse.urlparse(item.url).path
            except Exception:
                path = item.url
            if path in seen:
                continue
            seen[path] = True
            ordered.append(item)
        return ordered

    def summary_lines(self, max_lines: int = 6, include_images: bool = False) -> List[str]:
        """生成一段简短的候选摘要，用于命令行输出。"""
        items = self.snapshot(limit=max_lines)
        lines: List[str] = []
        for item in items:
            size_text = f"{item.size / 1024 / 1024:.2f}MB" if item.size else "未知大小"
            lines.append(f"{item.kind:<8} {size_text:>10}  {item.url[:70]}")

        if include_images:
            for image in self.image_candidates()[:max_lines]:
                size_text = f"{image.size / 1024 / 1024:.2f}MB" if image.size else "未知大小"
                lines.append(f"{'image':<8} {size_text:>10}  {image.url[:70]}")
        return lines


    def build_plan(self, referer: str = "") -> Optional[DownloadPlan]:
        """从候选里挑出最终下载方案。"""
        manifests = self.snapshot("manifest")
        videos = self.snapshot("video")
        audios = self.snapshot("audio")
        segments = self.snapshot("segment")

        prefer_manifest = bool(self.platform and self.platform.prefer_manifest)

        if prefer_manifest and manifests:
            return DownloadPlan(
                mode="hls",
                referer=referer,
                manifest_url=manifests[0].url,
                reason="平台策略要求优先使用播放清单",
            )

        if videos and audios:
            # 分片降级后的候选也可能落在 video 里，这里排除掉明显是分片的
            real_videos = [item for item in videos if ".ts" not in item.url.lower()]
            picked_video = real_videos[0] if real_videos else videos[0]
            return DownloadPlan(
                mode="tracks",
                referer=referer,
                video_url=picked_video.url,
                audio_url=audios[0].url,
                video_size=picked_video.size,
                audio_size=audios[0].size,
                reason="视频轨与音频轨分离，需要下载后合流",
            )

        if videos:
            picked = videos[0]
            return DownloadPlan(
                mode="single",
                referer=referer,
                video_url=picked.url,
                video_size=picked.size,
                reason="找到完整视频文件，无需合流",
            )

        if manifests:
            return DownloadPlan(
                mode="hls",
                referer=referer,
                manifest_url=manifests[0].url,
                reason="只有播放清单可用，交给 FFmpeg 拉流",
            )

        if segments:
            return DownloadPlan(
                mode="single",
                referer=referer,
                video_url=segments[0].url,
                video_size=segments[0].size,
                reason="只抓到分片，用首个分片兜底",
            )

        return None
