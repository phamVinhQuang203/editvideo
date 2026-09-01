# -*- coding: utf-8 -*-
"""Phân tích một lô link TikTok/Douyin: metadata viral, video tương đồng và khả năng reup.

Kết luận "gốc/reup" chỉ là điểm tin cậy dựa trên thời gian đăng, chất lượng và độ giống hình ảnh;
không được trình bày như xác minh bản quyền tuyệt đối.
"""
import hashlib
import json
import math
import os
import re
import subprocess
import time
from urllib.parse import urlparse

import engine as E

CACHE_DIR = os.path.join(E.ROOT, "viral_cache")
MAX_LINKS = 12
SIMILARITY_THRESHOLD = 0.80
SUPPORTED_HOSTS = ("tiktok.com", "douyin.com")
os.makedirs(CACHE_DIR, exist_ok=True)


def normalize_url(value):
    value = str(value or "").strip()
    if not value or not re.match(r"^https?://", value, re.I):
        return ""
    try:
        host = (urlparse(value).hostname or "").lower()
    except Exception:
        return ""
    if not any(host == allowed or host.endswith("." + allowed) for allowed in SUPPORTED_HOSTS):
        return ""
    return value


def _json_read(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _json_write(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _tikwm_metadata(url):
    """Metadata bổ sung, tách khỏi engine.download để không đổi contract pipeline Review/Remake."""
    import httpx
    try:
        response = httpx.get("https://www.tikwm.com/api/", params={"url": url, "hd": 1},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=45)
        data = (response.json().get("data") or {}) if response.status_code == 200 else {}
        author = data.get("author") or {}
        return {
            "title": data.get("title") or "",
            "uploader": author.get("unique_id") or author.get("nickname") or "",
            "author_name": author.get("nickname") or "",
            "create_time": int(data.get("create_time") or 0),
            "views": int(data.get("play_count") or 0),
            "likes": int(data.get("digg_count") or 0),
            "comments": int(data.get("comment_count") or 0),
            "shares": int(data.get("share_count") or 0),
        }
    except Exception:
        return {}


def viral_score(meta, now=None):
    views = max(0, int(meta.get("views") or 0))
    likes = max(0, int(meta.get("likes") or 0))
    comments = max(0, int(meta.get("comments") or 0))
    shares = max(0, int(meta.get("shares") or 0))
    engagement = (likes + comments * 2 + shares * 3) / max(views, 1)
    created = int(meta.get("create_time") or 0)
    now = int(now or time.time())
    age_days = max(0.0, (now - created) / 86400) if created else 30.0
    freshness = 1.0 / (1.0 + age_days / 14.0)
    return round((math.log10(views + 10) * 16.0) + min(engagement, 1.0) * 55.0 + freshness * 20.0, 1)


def _frame_hash(raw, mirrored=False):
    """dHash 256-bit từ frame gray 17x16; mirrored bắt bản reup lật ngang."""
    if len(raw) != 17 * 16:
        return None
    rows = [list(raw[y * 17:(y + 1) * 17]) for y in range(16)]
    if mirrored:
        rows = [list(reversed(row)) for row in rows]
    value = 0
    for row in rows:
        for x in range(16):
            value = (value << 1) | int(row[x] > row[x + 1])
    return f"{value:064x}"


def video_signature(path, frame_count=9):
    duration = E.probe(path)
    width, height = E.dims(path)
    if duration <= 0:
        return {"duration": 0, "width": width, "height": height, "frames": [], "mirrored": []}
    frames, mirrored = [], []
    for index in range(frame_count):
        second = max(0.05, duration * (index + 1) / (frame_count + 1))
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-ss", f"{second:.3f}", "-i", path,
            "-vf", "scale=17:16,format=gray", "-frames:v", "1", "-f", "rawvideo", "pipe:1"
        ], capture_output=True, timeout=90)
        normal_hash = _frame_hash(result.stdout)
        mirror_hash = _frame_hash(result.stdout, mirrored=True)
        if normal_hash:
            frames.append(normal_hash)
            mirrored.append(mirror_hash)
    return {"duration": round(duration, 3), "width": width, "height": height,
            "frames": frames, "mirrored": mirrored}


def _hash_similarity(left, right):
    return 1.0 - ((int(left, 16) ^ int(right, 16)).bit_count() / 256.0)


def signature_similarity(left, right):
    a, b = left.get("frames") or [], right.get("frames") or []
    b_mirror = right.get("mirrored") or []
    if not a or not b:
        return 0.0

    def coverage(source, targets):
        return sum(max(_hash_similarity(item, target) for target in targets) for item in source) / len(source)

    normal = (coverage(a, b) + coverage(b, a)) / 2.0
    flipped = (coverage(a, b_mirror) + coverage(b_mirror, a)) / 2.0 if b_mirror else 0.0
    visual = max(normal, flipped)
    da, db = float(left.get("duration") or 0), float(right.get("duration") or 0)
    duration_match = min(da, db) / max(da, db) if da > 0 and db > 0 else 0.0
    return round(visual * 0.88 + duration_match * 0.12, 4)


def _cluster(items):
    parent = list(range(len(items)))

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a, b):
        ra, rb = root(a), root(b)
        if ra != rb:
            parent[rb] = ra

    similarities = {}
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            score = signature_similarity(items[i]["signature"], items[j]["signature"])
            similarities[(i, j)] = score
            if score >= SIMILARITY_THRESHOLD:
                union(i, j)
    groups = {}
    for index in range(len(items)):
        groups.setdefault(root(index), []).append(index)
    return list(groups.values()), similarities


def _origin_rank(item):
    created = int(item.get("create_time") or 0)
    signature = item.get("signature") or {}
    pixels = int(signature.get("width") or 0) * int(signature.get("height") or 0)
    return (0 if created else 1, created or 2**62, -pixels, -float(signature.get("duration") or 0))


def classify_items(items):
    groups, similarities = _cluster(items)
    result_groups = []
    for number, indexes in enumerate(groups, 1):
        ordered = sorted(indexes, key=lambda idx: _origin_rank(items[idx]))
        leader = ordered[0]
        for idx in indexes:
            item = items[idx]
            item["group"] = number
            if len(indexes) == 1:
                item.update(origin_label="Không đủ dữ liệu", origin_confidence=35,
                            origin_reasons=["Chưa tìm thấy video tương đồng trong lô phân tích"])
            elif idx == leader:
                item.update(origin_label="Khả năng là bản gốc", origin_confidence=78,
                            origin_reasons=["Đăng sớm nhất hoặc có chất lượng nguồn tốt nhất trong nhóm",
                                            "Các video còn lại có hình ảnh tương đồng cao"])
            else:
                pair = (min(idx, leader), max(idx, leader))
                similarity = similarities.get(pair, 0.0)
                item.update(origin_label="Có dấu hiệu reup", origin_confidence=round(similarity * 100),
                            origin_reasons=[f"Giống video đứng đầu nhóm {similarity * 100:.0f}%",
                                            "Đăng muộn hơn hoặc chất lượng nguồn thấp hơn"])
        result_groups.append({"id": number, "count": len(indexes), "leader_id": items[leader]["id"]})
    return result_groups


def analyze_links(values):
    urls, errors = [], []
    for value in values:
        url = normalize_url(value)
        if not url:
            errors.append({"url": str(value or "").strip(), "error": "Chỉ hỗ trợ link TikTok hoặc Douyin hợp lệ"})
        elif url not in urls:
            urls.append(url)
    if len(urls) > MAX_LINKS:
        errors.append({"url": "", "error": f"Chỉ phân tích tối đa {MAX_LINKS} link mỗi lần"})
        urls = urls[:MAX_LINKS]

    items = []
    for url in urls:
        ident = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        directory = os.path.join(CACHE_DIR, ident)
        os.makedirs(directory, exist_ok=True)
        source = os.path.join(directory, "source.mp4")
        meta_path = os.path.join(directory, "meta.json")
        signature_path = os.path.join(directory, "signature.json")
        meta = _json_read(meta_path, {}) or {}
        if not os.path.exists(source) or os.path.getsize(source) < 50000:
            downloaded, download_meta = E.download(url, directory)
            if not downloaded:
                errors.append({"url": url, "error": "Không tải được video"})
                continue
            if os.path.abspath(downloaded) != os.path.abspath(source):
                os.replace(downloaded, source)
            meta.update(download_meta or {})
        live_meta = _tikwm_metadata(url)
        if live_meta:
            meta.update({key: value for key, value in live_meta.items() if value not in (None, "")})
        meta["url"] = url
        _json_write(meta_path, meta)

        signature = _json_read(signature_path)
        if not signature:
            signature = video_signature(source)
            _json_write(signature_path, signature)
        thumb = os.path.join(directory, "thumb.jpg")
        if not os.path.exists(thumb):
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", source, "-vf", "scale=360:-2",
                            "-frames:v", "1", thumb], capture_output=True, timeout=90)
        item = {"id": ident, "url": url, "title": meta.get("title") or "Video không có caption",
                "uploader": meta.get("uploader") or "Không rõ", "create_time": int(meta.get("create_time") or 0),
                "views": int(meta.get("views") or 0), "likes": int(meta.get("likes") or 0),
                "comments": int(meta.get("comments") or 0), "shares": int(meta.get("shares") or 0),
                "viral_score": viral_score(meta), "signature": signature,
                "thumb": f"/api/viral/thumb/{ident}" if os.path.exists(thumb) else ""}
        items.append(item)
    groups = classify_items(items) if items else []
    for item in items:
        item.pop("signature", None)
    items.sort(key=lambda item: (-item["viral_score"], item["group"]))
    return {"items": items, "groups": groups, "errors": errors}


def thumbnail_path(ident):
    if not re.fullmatch(r"[0-9a-f]{16}", str(ident or "")):
        return ""
    path = os.path.join(CACHE_DIR, ident, "thumb.jpg")
    return path if os.path.isfile(path) else ""
