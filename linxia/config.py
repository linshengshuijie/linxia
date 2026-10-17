"""全局配置模块"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# 目录配置

# 便携包将用户数据保存在程序旁，也可显式指定可写目录。
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
BASE_DIR = os.path.abspath(os.path.expanduser(os.environ.get("LINXIA_DATA_DIR", str(APP_DIR))))
if getattr(sys, "frozen", False):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(APP_DIR / "browsers")

# 下载完成的视频存放处
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

# 登录态（storage_state JSON）存放处
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")

# 浏览器配置目录存放处。
PROFILES_DIR = os.path.join(BASE_DIR, "profiles")

# 临时目录。临时浏览器的用户数据目录会建在这里，用完立即删除。
TEMP_DIR = os.path.join(BASE_DIR, "temp")

# 运行记录目录，按行追加 JSON，用于事后排查"这条视频是怎么抓到的"
LOG_DIR = os.path.join(BASE_DIR, "logs")

# 启动即确保目录存在，避免运行中途因为目录缺失报错
for _directory in (DOWNLOAD_DIR, SESSIONS_DIR, PROFILES_DIR, TEMP_DIR, LOG_DIR):
    os.makedirs(_directory, exist_ok=True)

# 下载记录文件（JSON Lines 格式，一行一条记录）
DOWNLOAD_RECORD_FILE = os.path.join(LOG_DIR, "downloads.jsonl")

# 浏览器配置

# 伪装成普通 Chrome。注意版本号要和实际 Chromium 内核接近，否则反而更可疑。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# 常用桌面分辨率
DEFAULT_VIEWPORT = {"width": 1440, "height": 900}

# 手机设备模拟参数。
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)
MOBILE_VIEWPORT = {"width": 390, "height": 844}
MOBILE_DEVICE_SCALE_FACTOR = 3.0

# 浏览器启动参数
BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--autoplay-policy=no-user-gesture-required",
]

# 浏览器语言，跟随中文环境，减少"可疑境外访问"的判定概率
BROWSER_LOCALE = "zh-CN"
BROWSER_TIMEZONE = "Asia/Shanghai"

# 超时与并发

# 打开页面的最长等待时间（毫秒）
PAGE_LOAD_TIMEOUT_MS = 45_000

# 单个链接的嗅探总预算（秒）。页面打开后最多等这么久来收集媒体流。
SNIFF_TIMEOUT_S = 30.0

# 嗅探过程中的轮询间隔（秒）
SNIFF_POLL_INTERVAL_S = 0.5

# 默认同时处理多少个链接（每个链接一个上下文）
DEFAULT_JOBS = 2

# 同一个链接内部，媒体流下载的并发数
DOWNLOAD_CONCURRENCY = 2

# 媒体识别阈值

# 小于该体积的媒体响应视为试看片段、封面视频或广告，不作为正式资源。
MIN_MEDIA_BYTES = 120 * 1024

# 小于该体积的图片响应视为界面图标、表情包、头像占位图，不算作品内容。
MIN_IMAGE_BYTES = 150 * 1024

# 明显的统计上报、埋点、日志类地址，直接忽略，避免误判成视频
NOISE_KEYWORDS = (
    "/log",
    "/report",
    "report/",
    "stats",
    "beacon",
    "analytics",
    "collect",
    "monitor",
    "tracing",
    "sentry",
    "telemetry",
)

# 下载时的分块大小（1MB）
CHUNK_SIZE = 1024 * 1024

# FFmpeg 定位

# 可通过环境变量覆盖 FFmpeg 路径。
LOCAL_FFMPEG_PATH = os.environ.get("LINXIA_FFMPEG", "")


def get_ffmpeg_bin() -> str:
    """按优先级寻找可用的 FFmpeg 可执行文件"""
    bundled = APP_DIR / "ffmpeg" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    for candidate in (LOCAL_FFMPEG_PATH, str(bundled)):
        if candidate and os.path.isfile(candidate):
            return candidate
    system_binary = shutil.which("ffmpeg")
    if system_binary:
        return system_binary
    raise FileNotFoundError(
        "没有找到可用的 FFmpeg。请把它加入系统 PATH，"
        "或者设置 LINXIA_FFMPEG 指向可执行文件。"
    )
