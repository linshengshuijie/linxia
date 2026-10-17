"""下载与合流模块"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import aiohttp

from . import console
from .config import CHUNK_SIZE, DOWNLOAD_RECORD_FILE, get_ffmpeg_bin

# 文件名里不允许出现的字符（Windows 限制最严，按它来）
ILLEGAL_FILENAME_CHARS = r'[\\/:*?"<>|\r\n\t]'

# 文件名最大长度，留出扩展名和序号的位置
MAX_FILENAME_LENGTH = 120


# 请求头处理


@dataclass
class DownloadHeaders:
    """下载媒体流时要带的一整套伪装请求头。"""

    referer: str = ""
    user_agent: str = ""
    cookie: str = ""

    def as_http_dict(self) -> dict:
        """转成 aiohttp 能用的字典。空值不发送。"""
        headers = {
            "Referer": self.referer,
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return {key: value for key, value in headers.items() if value}

    def as_ffmpeg_args(self) -> List[str]:
        """转成 FFmpeg 的 -headers 参数。"""
        lines = []
        if self.referer:
            lines.append(f"Referer: {self.referer}")
        if self.cookie:
            lines.append(f"Cookie: {self.cookie}")
        args: List[str] = []
        if lines:
            args += ["-headers", "\r\n".join(lines) + "\r\n"]
        if self.user_agent:
            args += ["-user_agent", self.user_agent]
        return args


def build_cookie_header(cookies: Sequence[dict], url: str) -> str:
    """筛选适用于目标地址的 cookie 并构建请求头。"""
    host = urlparse(url).netloc.split(":")[0].lower()
    if not host:
        return ""

    pairs: List[str] = []
    for cookie in cookies or []:
        try:
            domain = str(cookie.get("domain", "")).lstrip(".").lower()
            name = str(cookie.get("name", ""))
            value = str(cookie.get("value", ""))
        except AttributeError:
            continue
        if not name:
            continue
        if domain and not (host == domain or host.endswith("." + domain)):
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


# 文件名与路径


def sanitize_filename(name: str) -> str:
    """清理标题里的非法字符，得到一个安全的文件名主体。"""
    cleaned = re.sub(ILLEGAL_FILENAME_CHARS, "_", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if len(cleaned) > MAX_FILENAME_LENGTH:
        cleaned = cleaned[:MAX_FILENAME_LENGTH].strip()
    return cleaned or "未命名视频"


def unique_output_path(directory: str, stem: str, extension: str = ".mp4") -> str:
    """生成不冲突的输出路径。"""
    os.makedirs(directory, exist_ok=True)
    candidate = os.path.join(directory, f"{stem}{extension}")
    index = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}({index}){extension}")
        index += 1
    return candidate


def temp_output_path(destination: str) -> str:
    """生成"带正确扩展名"的临时输出路径。"""
    directory = os.path.dirname(destination)
    base, extension = os.path.splitext(os.path.basename(destination))
    return os.path.join(directory, f"{base}.part{extension}")


def write_download_record(record: dict) -> None:
    """把一条下载记录追加到 logs/downloads.jsonl。"""
    try:
        os.makedirs(os.path.dirname(DOWNLOAD_RECORD_FILE), exist_ok=True)
        payload = dict(record)
        payload["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(DOWNLOAD_RECORD_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


# 普通文件下载


async def download_file(
    url: str,
    destination: str,
    headers: DownloadHeaders,
    label: str = "下载",
    max_attempts: int = 2,
    fallback_referer: str = "",
) -> int:
    """分块下载单个媒体文件。"""
    part_path = destination + ".part"
    last_error: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        bar = console.ProgressBar(label=label)
        try:
            request_headers = headers.as_http_dict()
            if attempt > 1 and fallback_referer:
                request_headers["Referer"] = fallback_referer
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=request_headers, allow_redirects=True) as response:
                    if response.status not in (200, 206):
                        raise RuntimeError(f"服务端返回 HTTP {response.status}")
                    total = int(response.headers.get("content-length") or 0)
                    if not total:
                        # 没有 content-length 时，试着从 content-range 里拿总长度
                        content_range = response.headers.get("content-range") or ""
                        if "/" in content_range:
                            tail = content_range.rsplit("/", 1)[-1].strip()
                            if tail.isdigit():
                                total = int(tail)
                    bar.set_total(total)

                    written = 0
                    with open(part_path, "wb") as handle:
                        async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            written += len(chunk)
                            bar.update(len(chunk))
                    bar.close(note=f"共 {console.format_bytes(written)}")

            if os.path.exists(destination):
                os.remove(destination)
            os.rename(part_path, destination)
            return written

        except Exception as exc:
            last_error = exc
            bar.close(note="本次尝试失败")
            # 清理半截文件，避免下次拼接出错误内容
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass
            if attempt < max_attempts:
                console.warn(f"{label} 第 {attempt} 次尝试失败：{exc}，稍后重试")
                await asyncio.sleep(1.0)

    raise RuntimeError(f"下载失败：{last_error}")


# FFmpeg 调用底座


def _run_ffmpeg(cmd: Sequence[str], on_progress: Callable[[str], None]) -> Tuple[int, str]:
    """在一个独立线程里运行 FFmpeg，并把进度行回调出去。"""
    process = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    stderr_lines: List[str] = []

    def drain_stderr() -> None:
        try:
            for line in process.stderr:  # type: ignore[union-attr]
                stderr_lines.append(line)
        except Exception:
            pass

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    try:
        for line in process.stdout:  # type: ignore[union-attr]
            on_progress(line.strip())
    except Exception:
        pass

    return_code = process.wait()
    stderr_thread.join(timeout=3)
    return return_code, "".join(stderr_lines).strip()


def _parse_ffmpeg_progress(line: str) -> int:
    """解析 FFmpeg 的 -progress 输出。"""
    if line.startswith("total_size="):
        value = line.split("=", 1)[1].strip()
        if value.isdigit():
            return int(value)
    return 0


# HLS（m3u8）下载


async def download_hls(
    url: str,
    destination: str,
    headers: DownloadHeaders,
    label: str = "拉流",
) -> int:
    """用 FFmpeg 下载 HLS 流并直接合并成一个 mp4。"""
    ffmpeg_bin = get_ffmpeg_bin()
    part_path = temp_output_path(destination)

    cmd: List[str] = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
    cmd += headers.as_ffmpeg_args()
    cmd += ["-i", url, "-c", "copy", "-movflags", "+faststart", part_path]

    bar = console.ProgressBar(label=label)
    state = {"last": 0}

    def on_progress(line: str) -> None:
        current = _parse_ffmpeg_progress(line)
        if current > state["last"]:
            bar.update(current - state["last"])
            state["last"] = current

    console.detail("使用 FFmpeg 拉取清单并合并分片，通常需要等待十几秒")
    return_code, stderr_text = await asyncio.to_thread(_run_ffmpeg, cmd, on_progress)

    if return_code != 0:
        bar.close(note="失败")
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except OSError:
                pass
        raise RuntimeError(f"FFmpeg 拉流失败：{stderr_text[-400:] or '未知错误'}")

    bar.close(note=f"共 {console.format_bytes(state['last'])}")
    if os.path.exists(destination):
        os.remove(destination)
    os.rename(part_path, destination)
    return os.path.getsize(destination)


# 图文作品的图片下载


def guess_image_extension(content_type: str, url: str) -> str:
    """推测图片扩展名，优先看响应类型，其次看地址。"""
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
        "image/heic": ".heic",
    }
    base_type = (content_type or "").split(";")[0].strip().lower()
    if base_type in mapping:
        return mapping[base_type]

    lowered = (url or "").lower().split("?")[0]
    for extension in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".heic"):
        if lowered.endswith(extension):
            return ".jpg" if extension == ".jpeg" else extension
    return ".jpg"


def unique_directory(directory: str) -> str:
    """生成不冲突的目录路径（不实际创建）。"""
    candidate = directory
    index = 1
    while os.path.exists(candidate):
        candidate = f"{directory}({index})"
        index += 1
    return candidate


async def download_images_via_browser(
    context,
    images: Sequence,
    directory: str,
    label: str = "图片",
) -> List[str]:
    """通过浏览器上下文自身的请求接口下载图片。"""
    os.makedirs(directory, exist_ok=True)
    saved: List[str] = []
    total = len(images)
    bar = console.CountBar(label=label, total=max(total, 1))

    for index, image in enumerate(images, start=1):
        url = getattr(image, "url", "")
        content_type = getattr(image, "content_type", "")
        extension = guess_image_extension(content_type, url)
        destination = os.path.join(directory, f"{len(saved) + 1:02d}{extension}")

        written = 0
        try:
            response = await context.request.get(url, timeout=60_000)
            if not response.ok:
                raise RuntimeError(f"服务端返回 HTTP {response.status}")
            body = await response.body()
            with open(destination, "wb") as handle:
                handle.write(body)
            written = len(body)
            saved.append(destination)
        except Exception as exc:
            console.warn(f"第 {index} 张图片下载失败: {exc}")

        bar.step(written)

    # 只有确实有失败时才在进度条后面补一句说明，全成功就不重复报了
    failed = total - len(saved)
    bar.close(note=f"失败 {failed} 张" if failed else "")
    return saved


# 音视频合流


async def mux_tracks(
    video_path: str,
    audio_path: str,
    destination: str,
    label: str = "合流",
) -> int:
    """把分离的视频轨和音频轨合成一个 mp4。"""
    ffmpeg_bin = get_ffmpeg_bin()
    part_path = temp_output_path(destination)

    cmd: List[str] = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
    cmd += ["-i", video_path, "-i", audio_path, "-c", "copy", "-movflags", "+faststart", part_path]

    bar = console.ProgressBar(label=label)
    state = {"last": 0}

    def on_progress(line: str) -> None:
        current = _parse_ffmpeg_progress(line)
        if current > state["last"]:
            bar.update(current - state["last"])
            state["last"] = current

    return_code, stderr_text = await asyncio.to_thread(_run_ffmpeg, cmd, on_progress)

    if return_code != 0:
        bar.close(note="失败")
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except OSError:
                pass
        detail = stderr_text[-400:] or "未知错误"
        raise RuntimeError(
            f"FFmpeg 合流失败：{detail}\n"
            f"    两条轨道已保留，可手工合并：\n"
            f"    视频轨 {video_path}\n"
            f"    音频轨 {audio_path}"
        )

    bar.close(note=f"输出 {console.format_bytes(state['last'])}")

    # 清理中间产物
    for temp_file in (video_path, audio_path):
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass

    if os.path.exists(destination):
        os.remove(destination)
    os.rename(part_path, destination)
    return os.path.getsize(destination)
