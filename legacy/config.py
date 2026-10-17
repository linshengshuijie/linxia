import os
import shutil


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 存放下载完成视频的目录
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
# 存放已保存登录态凭证的目录
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")

# 确保文件夹存在
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)


LOCAL_FFMPEG_PATH = ""

def get_ffmpeg_bin() -> str:
    """定位可用的 FFmpeg 可执行文件。"""
    system_bin = shutil.which("ffmpeg")
    if system_bin:
        return system_bin
    if os.path.exists(LOCAL_FFMPEG_PATH):
        return LOCAL_FFMPEG_PATH
    raise FileNotFoundError("未找到可用的 FFmpeg！请检查系统 PATH 或在 config.py 中配置正确路径。")


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 快捷别名
COMMON_ALIASES = {
    "bilibili": "https://passport.bilibili.com/login",
    "douyin": "https://www.douyin.com",
    "youtube": "https://accounts.google.com/signin",
    "weibo": "https://weibo.com/login.php",
    "tencent": "https://v.qq.com",
    "iqiyi": "https://www.iqiyi.com"
}
