"""验证发布包中的浏览器和 FFmpeg 可独立运行。"""

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

from .config import get_ffmpeg_bin


async def check_browser() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("<title>LinXia runtime check</title>")
            if await page.title() != "LinXia runtime check":
                raise RuntimeError("浏览器页面检查失败")
        finally:
            await browser.close()
        with tempfile.TemporaryDirectory(prefix="linxia-check-") as profile:
            context = await playwright.chromium.launch_persistent_context(profile, headless=True)
            await context.close()


def main() -> int:
    """验证浏览器和音视频合成，失败时返回非零退出码。"""
    asyncio.run(check_browser())
    ffmpeg = get_ffmpeg_bin()
    with tempfile.TemporaryDirectory(prefix="linxia-media-") as directory:
        output = Path(directory) / "check.mp4"
        subprocess.run([
            ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            "color=c=black:s=64x64:r=1", "-f", "lavfi", "-i",
            "anullsrc=r=44100:cl=stereo", "-t", "1", "-c:v", "libx264",
            "-c:a", "aac", "-y", str(output),
        ], check=True, timeout=60)
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("FFmpeg 合成检查失败")
    print(json.dumps({"browser": "ok", "ffmpeg": "ok"}))
    return 0
