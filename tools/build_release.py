"""在目标系统构建包含浏览器和 FFmpeg 的便携包。"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg
import playwright


ROOT = Path(__file__).resolve().parent.parent


def run(*args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def prepare_browsers(destination: Path, cache: Path) -> None:
    """复用匹配版本的本地浏览器，否则安装到构建目录。"""
    registry = Path(playwright.__file__).parent / "driver/package/browsers.json"
    entries = json.loads(registry.read_text(encoding="utf-8"))["browsers"]
    required = [entry for entry in entries if entry["name"] in
                ("chromium", "chromium-headless-shell", "ffmpeg", "winldd")
                and (entry["name"] != "winldd" or os.name == "nt")]
    directories = [cache / (entry["name"] + "-" + entry["revision"])
                   for entry in required]
    if sys.platform == "win32" and all(path.is_dir() for path in directories):
        destination.mkdir(parents=True)
        for path in directories:
            shutil.copytree(path, destination / path.name)
    else:
        env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(destination))
        run(sys.executable, "-m", "playwright", "install", "chromium", env=env)


def copy_licenses(destination: Path) -> None:
    """收集 Python 依赖许可，并保存 FFmpeg 构建与来源信息。"""
    destination.mkdir()
    for name in ("playwright", "aiohttp", "greenlet", "pyee", "imageio-ffmpeg",
                 "aiohappyeyeballs", "aiosignal", "attrs", "frozenlist", "multidict",
                 "propcache", "yarl", "idna", "typing-extensions", "pyinstaller"):
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        for entry in distribution.files or []:
            if any(part.lower().startswith(("license", "copying", "notice")) for part in entry.parts):
                source = Path(distribution.locate_file(entry))
                if source.is_file():
                    target = destination / name / str(entry)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    for flag, filename in (("-version", "ffmpeg-version.txt"), ("-L", "ffmpeg-license.txt")):
        result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), flag], check=True,
                                capture_output=True)
        (destination / filename).write_bytes(result.stdout + result.stderr)
    (destination / "SOURCES.txt").write_text(
        "Chromium: https://chromium.googlesource.com/chromium/src/\n"
        "Playwright: https://github.com/microsoft/playwright\n"
        "FFmpeg: https://ffmpeg.org/ ; https://github.com/imageio/imageio-ffmpeg\n"
        "FFmpeg binary builds and source/build scripts: https://github.com/imageio/imageio-binaries\n"
        "FFmpeg build configuration and license are included alongside this file.\n",
        encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-cache", type=Path,
                        default=Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".cache"))) / "ms-playwright")
    args = parser.parse_args()
    version = re.search(r'__version__\s*=\s*["\']([^"\']+)',
                        (ROOT / "linxia/__init__.py").read_text(encoding="utf-8")).group(1)
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if arch is None or sys.platform not in ("win32", "linux", "darwin"):
        raise SystemExit("不支持当前构建平台")
    system = {"win32": "windows", "darwin": "macos", "linux": "linux"}[sys.platform]
    name = f"LinXia-{version}-{system}-{arch}"
    staging = ROOT / "build" / name
    if staging.exists():
        raise SystemExit(f"构建目录已存在，请先移动旧产物：{staging}")
    staging.mkdir(parents=True)
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "linxia", "--distpath", staging / "dist", "--workpath", staging / "work",
        "--specpath", staging, ROOT / "app.py", cwd=ROOT)
    bundle = staging / "dist" / "linxia"
    prepare_browsers(bundle / "browsers", args.browser_cache)
    ffmpeg_dir = bundle / "ffmpeg"
    ffmpeg_dir.mkdir()
    ffmpeg_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), ffmpeg_dir / ffmpeg_name)
    (ffmpeg_dir / ffmpeg_name).chmod(0o755)
    shutil.copy2(ROOT / "README.md", bundle / "README.md")
    shutil.copy2(ROOT / "PACKAGING.md", bundle / "PACKAGING.md")
    copy_licenses(bundle / "THIRD_PARTY_LICENSES")
    (bundle / "BUILD.json").write_text(json.dumps({
        "version": version, "system": system, "architecture": arch,
        "python": platform.python_version(),
        "dependencies": {name: importlib.metadata.version(name) for name in
                         ("playwright", "aiohttp", "pyinstaller", "imageio-ffmpeg")},
    }, indent=2), encoding="utf-8")
    bundle.rename(staging / "dist" / name)
    bundle = staging / "dist" / name
    executable_name = "linxia.exe" if os.name == "nt" else "linxia"
    with tempfile.TemporaryDirectory(prefix="LinXia release check ") as check_dir:
        env = dict(os.environ, LINXIA_DATA_DIR=check_dir)
        env.pop("LINXIA_FFMPEG", None)
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(check_dir) / "missing-cache")
        if os.name == "nt":
            env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
        for option in ("-version", "-help", "--check-runtime"):
            run(bundle / executable_name, option, cwd=check_dir, env=env, timeout=180)
    forbidden = ("sessions", "profiles", "downloads", "logs", "temp")
    if any((bundle / name).exists() for name in forbidden):
        raise SystemExit("发布包包含用户数据目录")
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    archive = Path(shutil.make_archive(str(output / name), "zip" if os.name == "nt" else "gztar",
                                      root_dir=bundle.parent, base_dir=bundle.name))
    with archive.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    Path(str(archive) + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"发布包：{archive}")


if __name__ == "__main__":
    main()
