import os
import re
import asyncio
import subprocess
import aiohttp
from config import DOWNLOAD_DIR, get_ffmpeg_bin

def clean_filename(name: str) -> str:
    """清理文件名中的非法字符，防止系统保存文件报错"""
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip()

async def download_stream_chunked(url: str, save_path: str, referer: str, user_agent: str):
    """携带请求头分块下载媒体并显示进度。"""
    headers = {
        "User-Agent": user_agent,
        "Referer": referer,
        "Range": "bytes=0-"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as response:
            if response.status not in (200, 206):
                raise RuntimeError(f"流媒体切片请求失败，HTTP 状态码: {response.status}")

            total_size = int(response.headers.get("content-length", 0))
            downloaded = 0

            with open(save_path, "wb") as f:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        percent = (downloaded / total_size) * 100
                        print(f"\r  写入进度: {percent:.1f}% ({downloaded // (1024*1024)}MB/{total_size // (1024*1024)}MB)", end="")
            print()

def mux_media(video_path: str, audio_path: str, output_path: str):
    """使用 FFmpeg 合并音视频并清理临时文件。"""
    ffmpeg_bin = get_ffmpeg_bin()
    print(f"\n 正在调用 FFmpeg 进行音视频无损合流...")

    cmd = [
        ffmpeg_bin, "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c", "copy",
        "-strict", "experimental",
        output_path
    ]

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg 合并失败: {res.stderr.decode('utf-8', errors='ignore')}")

    # 清理过程垃圾文件
    if os.path.exists(video_path):
        os.remove(video_path)
    if os.path.exists(audio_path):
        os.remove(audio_path)
    print(f"视频合成完毕: {output_path}")

async def save_media(title: str, video_url: str, audio_url: str, page_url: str, user_agent: str):
    """根据媒体类型选择直接下载或音视频合并。"""
    safe_title = clean_filename(title)
    temp_v = os.path.join(DOWNLOAD_DIR, f"{safe_title}_v.m4s")
    temp_a = os.path.join(DOWNLOAD_DIR, f"{safe_title}_a.m4s")
    final_output = os.path.join(DOWNLOAD_DIR, f"{safe_title}.mp4")

    print(f"启动下载管线: 《{safe_title}》")

    # 下载视频画面
    print("正在拉取视频画面流...")
    await download_stream_chunked(video_url, temp_v, referer=page_url, user_agent=user_agent)

    # 如果存在分离的伴音轨，则下载并合并；否则直接重命名
    if audio_url:
        print("正在拉取伴音音频流...")
        await download_stream_chunked(audio_url, temp_a, referer=page_url, user_agent=user_agent)
        mux_media(temp_v, temp_a, final_output)
    else:
        if os.path.exists(final_output):
            os.remove(final_output)
        os.rename(temp_v, final_output)
        print(f"直链媒体保存完成: {final_output}")