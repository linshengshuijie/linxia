"""登录态文件管理模块"""

from __future__ import annotations

import json
import os
import shutil
import time
from typing import Dict, List, Optional

from .config import PROFILES_DIR, SESSIONS_DIR
from .platforms import (
    Platform,
    all_platforms,
    extract_domain_key,
    normalize_url,
    session_file_candidates,
)

# 凭据文件后缀
SESSION_SUFFIX = "_state.json"


def session_path_for(platform: Optional[Platform], url: str) -> str:
    """返回保存凭据时应该写入的完整路径。"""
    existing = find_session_file(platform, url)
    if existing:
        return existing

    if platform is not None:
        name = platform.key
    else:
        name = extract_domain_key(normalize_url(url))
    return os.path.join(SESSIONS_DIR, f"{name}{SESSION_SUFFIX}")


def find_session_file(platform: Optional[Platform], url: str) -> Optional[str]:
    """查找该目标当前可用的凭据文件。"""
    for name in session_file_candidates(platform, url):
        candidate = os.path.join(SESSIONS_DIR, f"{name}{SESSION_SUFFIX}")
        if os.path.exists(candidate):
            return candidate
    return None


# 浏览器配置目录（像普通浏览器那样保持登录）


def profile_name_for(platform: Optional[Platform], url: str = "") -> str:
    """该目标对应的配置目录名：能识别平台时用平台键，否则用主域名。"""
    if platform is not None:
        return platform.key
    if url:
        return extract_domain_key(normalize_url(url))
    return "generic"


def profile_dir_for(platform: Optional[Platform], url: str = "") -> str:
    """返回该目标对应的浏览器配置目录路径（不创建）。"""
    return os.path.join(PROFILES_DIR, profile_name_for(platform, url))


def profile_exists(platform: Optional[Platform], url: str = "") -> bool:
    """该平台是否已经有配置目录（即是否登录过）。"""
    path = profile_dir_for(platform, url)
    return os.path.isdir(path) and bool(os.listdir(path))


def profile_size(path: str) -> int:
    """统计配置目录占用多少字节，用于 -sessions 显示。"""
    total = 0
    if not os.path.isdir(path):
        return 0
    for current, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(current, name))
            except OSError:
                continue
    return total


def list_profiles() -> List[dict]:
    """列出全部配置目录，供 -sessions 展示。"""
    results: List[dict] = []
    if not os.path.isdir(PROFILES_DIR):
        return results
    for name in sorted(os.listdir(PROFILES_DIR)):
        path = os.path.join(PROFILES_DIR, name)
        if not os.path.isdir(path):
            continue
        try:
            modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
        except OSError:
            modified = "未知"
        results.append(
            {
                "name": name,
                "path": path,
                "size": profile_size(path),
                "modified": modified,
            }
        )
    return results


def delete_profile(platform: Optional[Platform], url: str = "") -> bool:
    """删除该目标的浏览器配置目录。"""
    path = profile_dir_for(platform, url)
    if not os.path.isdir(path):
        return False
    try:
        shutil.rmtree(path, ignore_errors=False)
        return True
    except Exception:
        return False


def read_session_cookies(path: str) -> List[dict]:
    """读取凭据文件里的 cookie 列表。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return []
    cookies = payload.get("cookies", []) or []
    return [item for item in cookies if isinstance(item, dict)]


def session_auth_fields(path: str, auth_cookies) -> List[str]:
    """检查凭据文件里有哪些关键登录字段，并判断它是否真的处于登录状态。"""
    wanted = {str(name).lower() for name in (auth_cookies or ()) if name}
    if not wanted:
        return []

    hits: List[str] = []
    for cookie in read_session_cookies(path):
        name = str(cookie.get("name", ""))
        value = str(cookie.get("value", ""))
        # 同一个 cookie 名可能在多个域名或路径下各存一份，这里只列一次
        if name.lower() in wanted and value and name not in hits:
            hits.append(name)
    return hits


def platform_for_session_name(name: str):
    """根据凭据文件名反查它属于哪个平台。"""
    for platform in all_platforms():
        keys = {platform.key}
        keys.update(platform.session_keys or ())
        if name in keys:
            return platform
    return None


def delete_sessions(platform, url: str) -> List[str]:
    """删除某个目标对应的全部凭据文件，返回实际删掉的文件路径。"""
    deleted: List[str] = []
    for name in session_file_candidates(platform, url):
        candidate = os.path.join(SESSIONS_DIR, f"{name}{SESSION_SUFFIX}")
        if not os.path.isfile(candidate):
            continue
        try:
            os.remove(candidate)
            deleted.append(candidate)
        except OSError:
            # 文件被占用时跳过，让上层如实报告删除失败
            continue
    return deleted


def list_sessions() -> List[Dict[str, object]]:
    """列出本地全部凭据文件。"""
    results: List[Dict[str, object]] = []
    if not os.path.isdir(SESSIONS_DIR):
        return results

    for filename in sorted(os.listdir(SESSIONS_DIR)):
        if not filename.endswith(SESSION_SUFFIX):
            continue
        full_path = os.path.join(SESSIONS_DIR, filename)
        if not os.path.isfile(full_path):
            continue

        cookie_count = -1
        origin_count = -1
        try:
            with open(full_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            cookie_count = len(payload.get("cookies", []) or [])
            origin_count = len(payload.get("origins", []) or [])
        except Exception:
            cookie_count = -1

        name = filename[: -len(SESSION_SUFFIX)]
        platform = platform_for_session_name(name)
        if cookie_count == -1:
            login_state = "broken"
            auth_hits: List[str] = []
        elif platform is None:
            login_state = "unknown"
            auth_hits = []
        else:
            auth_hits = session_auth_fields(full_path, platform.auth_cookies)
            login_state = "ok" if auth_hits else "empty"

        try:
            stat = os.stat(full_path)
            size = stat.st_size
            modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
        except OSError:
            size = 0
            modified = "未知"

        results.append(
            {
                "name": name,
                "path": full_path,
                "size": size,
                "modified": modified,
                "cookies": cookie_count,
                "origins": origin_count,
                "platform": platform.key if platform else "",
                "platform_name": platform.name if platform else "",
                "login_state": login_state,
                "auth_fields": auth_hits,
            }
        )
    return results
