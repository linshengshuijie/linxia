# 便携包使用与构建

## 使用发布包

解压完整发布包，不要只移动可执行文件。

- Windows：双击 `linxia.exe`，或在终端执行 `.\linxia.exe -help`。
- macOS / Linux：在解压目录的终端执行 `./linxia` 或 `./linxia -help`。
- 可选：把可执行文件所在目录加入系统 PATH，即可在任意目录使用 `linxia`。

发布包包含 Python 运行依赖、Chromium 和 FFmpeg，无需另外安装 Python。运行数据默认保存到程序所在目录，请将程序解压到可写位置。通过 `LINXIA_DATA_DIR` 可指定独立的数据目录；通过 `LINXIA_FFMPEG` 可覆盖 FFmpeg 路径。每位用户需登录自己的账号。

Windows 目标为 x64。macOS 提供 Intel x64 和 Apple 芯片 arm64 两种包，当前包未签名或公证，首次打开可能需要在系统设置中允许。Linux 当前构建目标为 Ubuntu 22.04 x64，不保证兼容所有发行版；Chromium 仍依赖系统共享库。登录和可见浏览器模式需要图形桌面。

可运行 `linxia --check-runtime`（Windows 使用 `.\linxia.exe --check-runtime`）检查 Chromium 启动和 FFmpeg 音视频合成。

## 本地构建

必须在目标操作系统及对应架构的 Python 环境中构建，不能在 Windows 上交叉生成 macOS 或 Linux 可执行文件。

```bash
python -m pip install -e . -r requirements-build.txt
python tools/build_release.py
```

构建脚本只收集应用及其依赖，不收集项目中的登录凭据、下载或日志。Windows 优先复用匹配版本的本地 Playwright 浏览器，其他情况下载当前依赖对应的 Chromium。第一次构建需要联网，并需预留数 GB 磁盘空间。

Linux 构建或运行前可能需要安装 Chromium 系统依赖。在已安装 Playwright 的环境执行：

```bash
python -m playwright install-deps chromium
```

脚本会验证版本、帮助、浏览器启动及 FFmpeg 合成，成功后在 `dist/` 中生成压缩包和 SHA-256 校验文件。构建中间产物在 `build/`；重复构建时先移走对应的旧构建目录。包内 `BUILD.json` 记录依赖版本，`THIRD_PARTY_LICENSES/` 保存第三方许可与来源信息，浏览器自身的许可文件保留在浏览器目录中。

## GitHub 构建

上传源码及 `.github/workflows/build-release.yml` 后，在 GitHub 的 Actions 中运行 **Build portable packages**。也可推送 `v` 开头的标签触发构建。

工作流分别在 Windows、Ubuntu、macOS Intel 和 macOS Apple 芯片环境构建并检查。完成后从该次运行的 Artifacts 下载对应文件；上传至 GitHub Releases 时使用其中的实际 ZIP / tar.gz 及校验文件。工作流不会自动发布 Release。

自动检查验证依赖能运行，不代表所有网站的下载均经过验证。正式发布前仍应在干净的目标系统测试交互登录和实际媒体下载。
