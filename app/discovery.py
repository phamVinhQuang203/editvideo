# -*- coding: utf-8 -*-
"""Local browser-backed discovery for public trending video links."""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from urllib.parse import urlsplit, urlunsplit


PLATFORMS = {
    "douyin": {"name": "Douyin", "login": "https://www.douyin.com/",
               "trending": "https://www.douyin.com/hot"},
    "tiktok": {"name": "TikTok", "login": "https://www.tiktok.com/login",
               "trending": "https://www.tiktok.com/explore"},
    "bilibili": {"name": "Bilibili", "login": "https://passport.bilibili.com/login",
                 "trending": "https://www.bilibili.com/v/popular/all"},
}


def validate_platform(platform):
    value = str(platform or "").strip().lower()
    if value not in PLATFORMS:
        raise ValueError("Nền tảng không hợp lệ")
    return value


def safe_owner(owner):
    value = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(owner or "user"))[:48]
    return value or "user"


def canonical_url(url, platform=""):
    value = str(url or "").strip()
    if value.startswith("//"):
        value = "https:" + value
    if not value.startswith(("http://", "https://")):
        return ""
    parsed = urlsplit(value)
    host = parsed.netloc.lower().split(":")[0]
    path = parsed.path.rstrip("/")
    platform = str(platform or "").lower()
    if platform == "douyin" or "douyin.com" in host:
        match = re.search(r"/video/(\d{15,})", path)
        return f"https://www.douyin.com/video/{match.group(1)}" if match else ""
    if platform == "tiktok" or "tiktok.com" in host:
        match = re.search(r"(/@[^/]+/video/\d{10,})", path)
        return "https://www.tiktok.com" + match.group(1) if match else ""
    if platform == "bilibili" or "bilibili.com" in host:
        match = re.search(r"/video/(BV[a-zA-Z0-9]+|av\d+)", path, re.I)
        return "https://www.bilibili.com/video/" + match.group(1) if match else ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def parse_count(value):
    text = str(value or "").strip().lower().replace(",", "").replace(" ", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(亿|万|k|m|w)?", text)
    if not match:
        return 0
    number = float(match.group(1))
    multiplier = {"k": 1_000, "w": 10_000, "万": 10_000,
                  "m": 1_000_000, "亿": 100_000_000}.get(match.group(2), 1)
    return int(number * multiplier)


def viral_score(views=0, likes=0, comments=0, shares=0):
    views, likes = max(0, int(views or 0)), max(0, int(likes or 0))
    comments, shares = max(0, int(comments or 0)), max(0, int(shares or 0))
    engagement = (likes + comments * 2 + shares * 3) / max(views, 1)
    return round(math.log10(views + 1) * 18 + min(engagement, 1.0) * 45
                 + math.log10(shares + 1) * 5, 2)


def normalize_and_rank(items, platform, topic="", limit=30):
    platform = validate_platform(platform)
    topic = str(topic or "").strip().casefold()
    unique = {}
    for raw in items or []:
        url = canonical_url(raw.get("url"), platform)
        title = " ".join(str(raw.get("title") or "Video không có tiêu đề").split())[:300]
        author = " ".join(str(raw.get("author") or "").split())[:120]
        if not url or (topic and topic not in (title + " " + author).casefold()):
            continue
        views = parse_count(raw.get("views")); likes = parse_count(raw.get("likes"))
        comments = parse_count(raw.get("comments")); shares = parse_count(raw.get("shares"))
        item = {"id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
                "platform": platform, "url": url, "title": title, "author": author,
                "thumbnail": str(raw.get("thumbnail") or "")[:2000],
                "views": views, "likes": likes, "comments": comments, "shares": shares,
                "score": viral_score(views, likes, comments, shares)}
        current = unique.get(url)
        if not current or item["score"] > current["score"]:
            unique[url] = item
    return sorted(unique.values(), key=lambda item: (-item["score"], -item["views"], item["url"]))[:max(1, min(int(limit or 30), 100))]


def profile_dir(root, owner, platform):
    platform = validate_platform(platform)
    return os.path.join(root, ".browser_profiles", safe_owner(owner), platform)


def _helper(root):
    return os.path.join(root, "vendor", "discovery_browser.py")


def _state_file(profile):
    return os.path.join(profile, ".discovery-browser.json")


def _read_state(profile):
    try:
        with open(_state_file(profile), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}


def _process_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def account_status(root, owner, platform):
    platform = validate_platform(platform)
    profile = profile_dir(root, owner, platform)
    state = _read_state(profile)
    active = _process_alive(state.get("pid"))
    cookies = os.path.join(profile, "Default", "Network", "Cookies")
    if not os.path.isfile(cookies):
        cookies = os.path.join(profile, "Default", "Cookies")
    saved = os.path.isfile(cookies) and os.path.getsize(cookies) > 0
    stamp = state.get("last_login")
    if not stamp and saved:
        stamp = datetime.fromtimestamp(os.path.getmtime(cookies)).isoformat(timespec="seconds")
    return {"platform": platform, "name": PLATFORMS[platform]["name"],
            "saved": saved, "active": active, "last_login": stamp or ""}


def list_accounts(root, owner):
    return [account_status(root, owner, platform) for platform in PLATFORMS]


def reset_account(root, owner, platform):
    platform = validate_platform(platform)
    status = account_status(root, owner, platform)
    if status["active"]:
        raise RuntimeError("Cửa sổ đăng nhập vẫn đang mở. Hãy đóng cửa sổ đó trước khi xóa phiên")
    profile = profile_dir(root, owner, platform)
    if os.path.isdir(profile):
        shutil.rmtree(profile)
    return {"platform": platform, "removed": True}


def launch_login(root, owner, platform):
    platform = validate_platform(platform)
    profile = profile_dir(root, owner, platform)
    os.makedirs(profile, exist_ok=True)
    if account_status(root, owner, platform)["active"]:
        raise RuntimeError("Cửa sổ đăng nhập nền tảng này đang mở")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    error_path = os.path.join(profile, "login-browser.log")
    with open(error_path, "w", encoding="utf-8") as error_log:
        process = subprocess.Popen([sys.executable, _helper(root), "login", platform, profile],
                                   cwd=root, creationflags=flags, close_fds=True,
                                   stdin=subprocess.DEVNULL, stdout=error_log, stderr=error_log)
    time.sleep(1.2)
    if process.poll() is not None:
        try:
            with open(error_path, encoding="utf-8", errors="replace") as handle:
                detail = handle.read().strip()
        except OSError:
            detail = ""
        hint = "Phiên trình duyệt đã hỏng. Hãy xóa phiên của nền tảng này rồi đăng nhập lại"
        if detail:
            hint += ": " + detail[-350:]
        raise RuntimeError(hint)
    return {"platform": platform, "profile_saved": os.path.isdir(profile), "active": True}


def discover(root, owner, platform, topic="", limit=30, scan_date=None, timeout=150):
    platform = validate_platform(platform)
    requested = str(scan_date or date.today().isoformat())
    if requested != date.today().isoformat():
        raise ValueError("Các nền tảng chỉ cung cấp bảng thịnh hành hiện tại; hãy chọn ngày hôm nay")
    profile = profile_dir(root, owner, platform)
    os.makedirs(profile, exist_ok=True)
    result = subprocess.run([sys.executable, _helper(root), "discover", platform, profile],
                            cwd=root, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "Không đọc được trang thịnh hành").strip()
        raise RuntimeError(message[-500:])
    payload = json.loads(result.stdout or "{}")
    ranked = normalize_and_rank(payload.get("items"), platform, topic, limit)
    return {"platform": platform, "platform_name": PLATFORMS[platform]["name"],
            "scanned_at": datetime.now().isoformat(timespec="seconds"),
            "logged_profile": os.path.isdir(profile), "items": ranked,
            "warning": str(payload.get("warning") or "")[:300]}
