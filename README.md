# LinXia

基于 Playwright 的网页媒体嗅探与下载工具。通过 Chromium 打开页面，监听播放器实际发出的网络请求，保存视频、音频和图文资源。

## 功能

- 自动识别平台，也可指定抖音、哔哩哔哩、YouTube、微博、小红书和快手。
- 支持完整媒体文件、分离音视频轨道和 HLS（m3u8）。
- 支持部分图文作品、小红书多视频轮播笔记。
- 保存并复用浏览器登录状态。
- 提供多链接处理、下载进度、详细日志和交互界面。

平台识别不代表所有链接都可下载，实际结果取决于网页提供的资源及当前登录状态。

## 环境要求

- Python 3.9 或更高版本。
- Playwright Chromium 浏览器。
- FFmpeg：用于音视频合并、HLS 下载及静图与原声合成，需加入系统 `PATH`。

## 安装

使用 Windows、macOS 或 Linux 便携发布包时，解压后直接运行其中的 `linxia.exe` 或 `linxia`，无需安装 Python。系统要求、构建方法与 GitHub Actions 操作见 [PACKAGING.md](PACKAGING.md)。下面是从源码安装的步骤。

克隆仓库并进入项目目录后，创建虚拟环境：

```bash
python -m venv .venv
```

Windows PowerShell 激活：

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS / Linux 激活：

```bash
source .venv/bin/activate
```

安装项目和浏览器：

```bash
python -m pip install -e .
python -m playwright install chromium
```

Linux 缺少浏览器系统依赖时，可使用 `python -m playwright install --with-deps chromium`。

检查安装：

```bash
linxia -version
ffmpeg -version
linxia -help
```

## 使用

### 下载媒体

```bash
linxia "https://example.com/video"
linxia "链接1" "链接2"
linxia -douyin "分享链接或完整分享文案"
linxia -bilibili "https://www.bilibili.com/video/作品编号"
linxia -o ./downloads -timeout 60 -jobs 2 "链接"
```

分享文案建议整体加引号，程序会提取其中的网址。未识别的平台按通用网页处理。

| 平台 | 参数 |
| --- | --- |
| 抖音 | `-douyin` / `-dy` |
| 哔哩哔哩 | `-bilibili` / `-bili` |
| YouTube | `-youtube` / `-yt` |
| 微博 | `-weibo` / `-wb` |
| 小红书 | `-xiaohongshu` / `-xhs` |
| 快手 | `-kuaishou` / `-ks` |

### 登录与浏览

需要登录才能访问的内容，先在弹出的浏览器中手动完成登录：

```bash
linxia -login douyin
linxia -login "https://www.xiaohongshu.com"
linxia -sessions
linxia -website douyin
```

登录状态保存在 `profiles/` 和 `sessions/`。指定平台时优先复用浏览器配置目录。凭据失效或需要更换账号时，清除后重新登录：

```bash
linxia -reset douyin
linxia -login douyin
```

`-reset` 会删除对应的本地凭据和浏览器配置目录。

### 交互界面

```bash
linxia
```

在 `LinXia >` 提示符后输入链接或参数，如 `-login douyin`、`-help`；输入 `exit` 退出。非交互终端默认显示帮助，也可使用 `linxia -ui` 显式进入界面。

### 常用选项

| 选项 | 作用 | 默认值 |
| --- | --- | --- |
| `-o` / `-output 目录` | 保存目录 | 项目目录下的 `downloads/` |
| `-timeout 秒` | 单条链接的嗅探等待时间 | 30 |
| `-jobs 数量` | 同时处理的链接数 | 2 |
| `-show` | 显示嗅探浏览器窗口 | 关闭 |
| `-verbose` | 输出平台识别、候选资源与下载方案等细节 | 关闭 |
| `-help` | 显示完整帮助 | — |
| `-version` | 显示版本 | — |

多链接嗅探可以并发，下载阶段串行执行；复用同一浏览器配置目录的任务也会串行执行。

## 输出与限制

- 视频通常保存为单个文件；分离的音视频轨道通过 FFmpeg 合并。
- 图文作品保存到同名文件夹，图片按序号命名。
- 部分快手图文或实况作品在网页端仅提供静图和原声。生成的 `实况合成.mp4` 是静态画面与原声的合成结果。
- 清晰度取决于网页播放器实际请求的媒体流。
- 不支持 DRM 解密。登录限制、验证码、地区限制及站点更新可能导致嗅探失败。
- 图集仅保存当前采集流程加载到的资源，可能不完整。

## 项目结构

```text
linxia/
  cli.py          命令行解析与分发
  tui.py          交互界面
  browser.py      浏览器与上下文管理
  human.py        页面播放与浏览动作
  sniffer.py      媒体识别与下载方案
  tasks.py        任务编排与结果汇总
  downloader.py   下载与 FFmpeg 调用
  login.py        登录与凭据管理
  sessions.py     登录态文件管理
  browse.py       复用登录态浏览
  platforms.py    平台配置与地址识别
  config.py       路径、浏览器与下载配置
  console.py      终端输出与进度条
app.py            兼容启动入口
tools/            代码检查工具
legacy/           历史脚本，不参与安装包构建
```

运行时自动创建 `downloads/`、`sessions/`、`profiles/`、`temp/` 和 `logs/`，这些目录已在 `.gitignore` 中排除。登录凭据和浏览器配置包含个人会话信息，不应提交到仓库。

## 开发检查

```bash
python tools/check_style.py
python -m compileall -q linxia app.py tools
```

仅下载你拥有权利或已获得授权的内容，并遵守目标网站的使用条款。
