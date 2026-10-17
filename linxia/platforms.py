"""平台注册表"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

# 从一段文字里捞网址用的正则。
URL_PATTERN = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")

# 裸域名（不带协议头）的校验规则：至少两段，各段由字母数字和短横线组成
HOST_PATTERN = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?)+$"
)

# IPv4 直连地址
IPV4_PATTERN = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


@dataclass(frozen=True)
class Platform:
    """单个平台的静态描述信息。"""

    key: str
    name: str
    aliases: Tuple[str, ...]
    domains: Tuple[str, ...]
    login_url: str
    home_url: str
    referer: str
    session_keys: Tuple[str, ...] = ()
    title_suffixes: Tuple[str, ...] = ()
    prefer_manifest: bool = False
    carousel: bool = False
    auth_cookies: Tuple[str, ...] = ()
    notes: str = ""


# 平台清单

_PLATFORM_LIST: Tuple[Platform, ...] = (
    Platform(
        key="douyin",
        name="抖音",
        aliases=("douyin", "dy", "tiktokcn"),
        domains=("douyin.com", "iesdouyin.com", "douyinvod.com"),
        login_url="https://www.douyin.com/",
        home_url="https://www.douyin.com/",
        referer="https://www.douyin.com/",
        session_keys=("douyin", "douyin.com", "www.douyin.com"),
        title_suffixes=(" - 抖音", "- 抖音", " - 抖音短视频"),
        # passport_csrf_token 之类的 cookie 匿名访客也会被写入
        auth_cookies=("sessionid", "sessionid_ss", "sid_tt"),
        notes="短视频，多数视频音画已合流；部分作品需要登录才能看到高清流。",
    ),
    Platform(
        key="bilibili",
        name="哔哩哔哩",
        aliases=("bilibili", "bili", "b站", "bzhan"),
        domains=("bilibili.com", "b23.tv", "bilivideo.com", "bilivideo.cn"),
        login_url="https://passport.bilibili.com/login",
        home_url="https://www.bilibili.com/",
        referer="https://www.bilibili.com/",
        session_keys=("bilibili", "bilibili.com", "www.bilibili.com"),
        title_suffixes=("_哔哩哔哩_bilibili", "哔哩哔哩_bilibili"),
        auth_cookies=("SESSDATA", "DedeUserID"),
        notes="音视频分离（DASH），会自动下载两条轨道再用 FFmpeg 无损合流。",
    ),
    Platform(
        key="youtube",
        name="YouTube",
        aliases=("youtube", "yt", "油管"),
        domains=("youtube.com", "youtu.be", "googlevideo.com"),
        login_url="https://accounts.google.com/signin",
        home_url="https://www.youtube.com/",
        referer="https://www.youtube.com/",
        session_keys=("youtube", "youtube.com", "www.youtube.com"),
        title_suffixes=(" - YouTube",),
        auth_cookies=("SID", "SAPISID", "__Secure-1PSID", "LOGIN_INFO"),
        notes="未登录也可以嗅探，但部分视频需要登录后才能播放。",
    ),
    Platform(
        key="weibo",
        name="微博",
        aliases=("weibo", "wb"),
        domains=("weibo.com", "weibo.cn", "sina.com.cn"),
        login_url="https://weibo.com/login.php",
        home_url="https://weibo.com/",
        referer="https://weibo.com/",
        session_keys=("weibo", "weibo.com"),
        auth_cookies=("SUB", "SUBP", "SSOLoginState"),
        notes="视频多为直链 mp4。",
    ),
    Platform(
        key="xiaohongshu",
        name="小红书",
        aliases=("xiaohongshu", "xhs", "redbook", "小红书"),
        domains=("xiaohongshu.com", "xhscdn.com", "xhslink.com", "xhslink.cn"),
        login_url="https://www.xiaohongshu.com/",
        home_url="https://www.xiaohongshu.com/",
        referer="https://www.xiaohongshu.com/",
        session_keys=("xiaohongshu", "xiaohongshu.com", "www.xiaohongshu.com"),
        # a1 和 webId 是匿名访客也会有的设备标识，不能作为登录依据，只用 web_session
        auth_cookies=("web_session",),
        # 小红书笔记是 swiper 轮播，多视频/多图作品要点右箭头才会加载下一份媒体。
        carousel=True,
        notes="图文笔记没有视频流，此时会明确提示未嗅探到媒体；多图/多视频会逐张翻页采集。",
    ),
    Platform(
        key="kuaishou",
        name="快手",
        aliases=("kuaishou", "ks"),
        domains=("kuaishou.com", "gifshow.com", "kwaicdn.com", "chenzhongtech.com"),
        # 直接打开登录页，省得用户在首页自己找登录按钮
        login_url="https://passport.kuaishou.com/pc/account/login",
        home_url="https://www.kuaishou.com/",
        referer="https://www.kuaishou.com/",
        session_keys=("kuaishou", "kuaishou.com", "www.kuaishou.com"),
        # 快手登录后的凭据字段，新旧两套命名都列上
        auth_cookies=(
            "kuaishou.server.web_st",
            "kuaishou.server.web_ph",
            "kuaishou.web.cp.api_st",
            "kuaishou.web.cp.api_ph",
            "userId",
        ),
        notes="短视频，多为直链 mp4。",
    ),
)

# key -> Platform 的查表结构
PLATFORMS: Dict[str, Platform] = {item.key: item for item in _PLATFORM_LIST}

# 简写 -> key 的查表结构，命令行解析和 URL 识别都会用到
ALIAS_TO_KEY: Dict[str, str] = {}
for _item in _PLATFORM_LIST:
    for _alias in _item.aliases:
        ALIAS_TO_KEY[_alias.lower()] = _item.key
    ALIAS_TO_KEY[_item.key.lower()] = _item.key


# 对外函数


def all_platforms() -> List[Platform]:
    """按登记顺序返回全部平台，供帮助信息使用。"""
    return list(_PLATFORM_LIST)


def get_platform(key_or_alias: str) -> Optional[Platform]:
    """按平台标识或简写取平台对象。"""
    if not key_or_alias:
        return None
    key = ALIAS_TO_KEY.get(key_or_alias.strip().lower())
    if key:
        return PLATFORMS.get(key)
    return None


def platform_by_url(url: str) -> Optional[Platform]:
    """根据 URL 的域名反查平台。"""
    host = extract_host(url)
    if not host:
        return None
    for platform in _PLATFORM_LIST:
        for domain in platform.domains:
            if host == domain or host.endswith("." + domain):
                return platform
    return None


def resolve_platform(target: str, forced: Optional[str] = None) -> Optional[Platform]:
    """统一解析目标对应的平台。"""
    if forced:
        platform = get_platform(forced)
        if platform:
            return platform
    platform = get_platform(target)
    if platform:
        return platform
    return platform_by_url(target)


def extract_host(url: str) -> str:
    """从 URL 里取出小写域名（去掉端口），非法 URL 返回空字符串。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        netloc = urlparse(raw).netloc.lower()
    except ValueError:
        return ""
    return netloc.split(":")[0].split("@")[-1]


def extract_domain_key(url: str) -> str:
    """从 URL 提取用于凭据命名的主域名。"""
    host = extract_host(url)
    if not host:
        return "unknown"
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def extract_url(text: str) -> str:
    """从一段文字里提取出真正的网址。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    match = URL_PATTERN.search(raw)
    if match is None:
        return ""
    return match.group(0).rstrip(".,;:!?)]}\"'")


def normalize_url(target: str) -> str:
    """把用户输入整理成合法 URL。"""
    extracted = extract_url(target)
    if extracted:
        return extracted

    raw = (target or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return "https://" + raw


def is_url_like(token: str) -> bool:
    """判断一个参数里是否含有可以真正打开的网址。"""
    if extract_url(token):
        return True

    text = (token or "").strip()
    if not text or len(text) > 2048:
        return False
    # 裸域名里不应该出现空白和转义字符
    if any(character.isspace() for character in text):
        return False

    # 去掉路径和查询串，只留主机部分
    host = text.split("//", 1)[-1].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if not host:
        return False
    # 去掉端口号，192.168.1.10:8080、localhost:8000 这类写法要能识别
    host = host.split(":", 1)[0]

    # IPv4 直连地址也允许
    if IPV4_PATTERN.match(host):
        return True

    if not HOST_PATTERN.match(host):
        return False

    # 顶级域名必须是字母，这样 "2.38" 就过不去
    tld = host.rsplit(".", 1)[-1]
    return tld.isalpha() and len(tld) >= 2


def platform_summary() -> str:
    """返回一句"内置平台有哪些"的说明，用于报错提示。"""
    return "、".join(platform.key for platform in _PLATFORM_LIST)


# "App 专用分享链接"的识别
MOBILE_LANDING_HOSTS: Dict[str, tuple] = {
    "kuaishou": ("v.m.chenzhongtech.com", "m.chenzhongtech.com", "m.kuaishou.com"),
}


def is_mobile_landing_url(url: str) -> bool:
    """地址是不是落在"只给手机端的落地页"上。"""
    host = extract_host(url or "").lower()
    if not host:
        return False
    for hosts in MOBILE_LANDING_HOSTS.values():
        for item in hosts:
            if host == item or host.endswith("." + item):
                return True
    return False


# 快手手机分享页地址里的作品 id 与"原声"标识
_KUAISHOU_PHOTO_ID = re.compile(r"/(?:fw/photo|photo)/([0-9A-Za-z_-]{6,})")
_KUAISHOU_QUERY_VALUE = re.compile(r"[?&](photoId|shareObjectId)=([0-9A-Za-z_-]+)")


def kuaishou_work_ids(url: str) -> Dict[str, str]:
    """从快手手机分享页地址里取出作品 id 和原声对象 id。"""
    text = url or ""
    result: Dict[str, str] = {}

    matched = _KUAISHOU_PHOTO_ID.search(text)
    if matched:
        result["photo_id"] = matched.group(1)

    for key, value in _KUAISHOU_QUERY_VALUE.findall(text):
        if key == "photoId" and "photo_id" not in result:
            result["photo_id"] = value
        elif key == "shareObjectId":
            result["object_id"] = value

    return result


def session_file_candidates(platform: Optional[Platform], url: str) -> List[str]:
    """列出该目标可能对应的凭据文件名（不含扩展名），按优先级排序。"""
    names: List[str] = []
    if platform:
        names.extend(platform.session_keys or (platform.key,))
    domain_key = extract_domain_key(url)
    if domain_key:
        names.append(domain_key)
    # 去重但保持顺序
    ordered: List[str] = []
    for name in names:
        if name and name not in ordered:
            ordered.append(name)
    return ordered
