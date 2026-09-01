# -*- coding: utf-8 -*-
"""FB DOWNLOAD — tải video/reel Facebook về để học content.
FB chặn video nếu không cookie đăng nhập -> lấy cookie từ GPM profile FB (như Douyin),
xuất Netscape cookie file, dùng yt-dlp system (có curl_cffi) + --impersonate.
Chạy test:  venv python fb_download.py --url "<fb link>" [--profile <gpm_id>]
Trả JSON {ok, file, title, duration, uploader, url}.
"""
import os, re, json, time, subprocess, tempfile, argparse, urllib.request, sys

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "work", "fb_data")
COOKIE_FILE = os.path.join(DATA, "fb_cookies.txt")          # Netscape format cho yt-dlp
FB_PROFILE_FILE = os.path.join(DATA, "fb_profile.json")     # {"id": "<gpm profile id>"}
YTDLP = [sys.executable, "-m", "yt_dlp"]
GPM_API = "http://127.0.0.1:19995"
IMPERSONATE = "Chrome-136"

def _gpm_start(pid):
    with urllib.request.urlopen(f"{GPM_API}/api/v3/profiles/start/{pid}", timeout=60) as r:
        d = json.loads(r.read().decode())
    data = d.get("data") or {}
    # remote_debugging_address hoặc dạng "127.0.0.1:port"
    ep = data.get("remote_debugging_address") or f"127.0.0.1:{data.get('debug_port') or data.get('remote_debugging_port')}"
    return ep

def export_cookies_from_gpm(profile_id: str | None = None) -> str:
    """Mở GPM profile FB -> lấy cookie facebook.com -> ghi Netscape file."""
    if not profile_id:
        profile_id = (json.load(open(FB_PROFILE_FILE, encoding="utf-8")).get("id")
                      if os.path.exists(FB_PROFILE_FILE) else None)
    if not profile_id:
        raise RuntimeError("Chưa chọn GPM profile FB — chạy với --profile <id> hoặc tạo data/fb_profile.json")
    from playwright.sync_api import sync_playwright
    ep = _gpm_start(profile_id)
    for _ in range(30):
        try: urllib.request.urlopen(f"http://{ep}/json/version", timeout=2); break
        except Exception: time.sleep(2)
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://{ep}")
        ctx = browser.contexts[0]
        cookies = ctx.cookies()
    fb = [c for c in cookies if "facebook.com" in c.get("domain", "")]
    # ghi Netscape
    lines = ["# Netscape HTTP Cookie File"]
    for c in fb:
        dom = c["domain"]
        lines.append("\t".join([dom, "TRUE" if dom.startswith(".") else "FALSE",
                                 c.get("path", "/"), "TRUE" if c.get("secure") else "FALSE",
                                 str(int(c.get("expires") or 0) if c.get("expires", -1) > 0 else 0),
                                 c["name"], c["value"]]))
    open(COOKIE_FILE, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    json.dump({"id": profile_id}, open(FB_PROFILE_FILE, "w", encoding="utf-8"))
    return f"{len(fb)} cookie FB -> {COOKIE_FILE}"

UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

def resolve(url: str) -> str:
    """Theo redirect share/fb.watch link -> URL chuẩn."""
    if any(k in url for k in ("fb.watch", "/share/", "l.facebook")):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA_DESKTOP})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.url
        except Exception:
            return url
    return url

def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA_DESKTOP,
        "Accept-Language": "en-US,en;q=0.9", "Cookie": "locale=en_US;"})
    with urllib.request.urlopen(req, timeout=40) as r:
        raw = r.read()
    try:
        import gzip
        if raw[:2] == b"\x1f\x8b": raw = gzip.decompress(raw)
    except Exception: pass
    return raw.decode("utf-8", "replace")

def _unescape(u: str) -> str:
    return (u.replace("\\/", "/").replace("\\u0025", "%").replace("\\u00253A", ":")
             .encode().decode("unicode_escape", "ignore")) if "\\u" in u else u.replace("\\/", "/")

def extract_no_cookie(url: str) -> dict:
    """KHÔNG cookie: lấy HTML công khai -> bắt playable_url (hd/sd). Cách các web downloader dùng.
    Trả {hd, sd, title} — CDN link sống vài giờ."""
    url = resolve(url)
    tries = [url]
    # bản mobile/mbasic thường lộ link trực tiếp dễ hơn
    tries.append(re.sub(r"://(www\.|web\.)?facebook", "://www.facebook", url) + ("&" if "?" in url else "?") + "_rdr")
    html = ""
    for u in tries:
        try:
            html = _fetch(u)
            if len(html) > 5000: break
        except Exception: continue
    def grab(*keys):
        for k in keys:
            m = re.search(k + r'"\s*:\s*"(https:[^"]+)"', html)
            if m: return _unescape(m.group(1))
        return ""
    hd = grab("playable_url_quality_hd", "browser_native_hd_url", "hd_src\\\\?")
    sd = grab("playable_url", "browser_native_sd_url", "sd_src\\\\?")
    tm = re.search(r'<title[^>]*>([^<]+)</title>', html)
    return {"hd": hd, "sd": sd, "title": (tm.group(1).strip() if tm else "")[:120],
            "html_len": len(html)}

def _ytdlp(url, out_tpl, extra):
    cmd = YTDLP + ["--no-warnings", "--no-playlist", "-f", "mp4/best",
           "--impersonate", IMPERSONATE, "-o", out_tpl] + extra + [url]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)

def _dl_direct(direct_url: str, dst: str) -> bool:
    try:
        req = urllib.request.Request(direct_url, headers={"User-Agent": UA_DESKTOP})
        with urllib.request.urlopen(req, timeout=180) as r, open(dst, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk: break
                f.write(chunk)
        return os.path.getsize(dst) > 10000
    except Exception:
        return False

def download(url: str, workdir: str | None = None, use_cookies: bool = False) -> dict:
    """Tải video FB. ĐƯỜNG CHÍNH: yt-dlp + --impersonate, KHÔNG cookie — chạy tốt với
    video/reel CÔNG KHAI (đã kiểm chứng 2026-07-21). Cookie chỉ bật khi video riêng tư."""
    url = resolve(url)
    workdir = workdir or tempfile.mkdtemp(prefix="fb_")
    os.makedirs(workdir, exist_ok=True)
    out_tpl = os.path.join(workdir, "fbvid.%(ext)s")

    def run(extra):
        for x in os.listdir(workdir):
            if x.startswith("fbvid."):
                try: os.remove(os.path.join(workdir, x))
                except OSError: pass
        d = _ytdlp(url, out_tpl, extra)
        f = next((os.path.join(workdir, x) for x in os.listdir(workdir) if x.startswith("fbvid.")), None)
        return d, f

    d, f = run([]); used = "no_cookie"
    if not f and use_cookies:                       # video riêng tư -> cookie GPM
        try:
            if not os.path.exists(COOKIE_FILE): export_cookies_from_gpm()
            d, f = run(["--cookies", COOKIE_FILE]); used = "cookie"
            if not f:
                export_cookies_from_gpm(); d, f = run(["--cookies", COOKIE_FILE]); used = "cookie_refreshed"
        except Exception as e:
            return {"ok": False, "err": f"cookie path fail: {str(e)[:120]}"}
    if not f:
        return {"ok": False, "err": "yt-dlp không tải được (thử use_cookies nếu video riêng tư)",
                "ytdlp_err": (d.stderr or "")[-300:]}
    ex = ["--cookies", COOKIE_FILE] if used != "no_cookie" else []
    m = _ytdlp(url, os.devnull, ex + ["--print",
        "%(title)s|||%(duration)s|||%(uploader)s|||%(webpage_url)s", "--no-download"])
    p = ((m.stdout or "").strip().splitlines() or [""])[0].split("|||")
    return {"ok": True, "file": f, "method": used, "title": p[0],
            "duration": p[1] if len(p) > 1 else "", "uploader": p[2] if len(p) > 2 else "",
            "url": p[3] if len(p) > 3 else url}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--profile", help="GPM profile FB id -> chỉ export cookie rồi thoát")
    a = ap.parse_args()
    if a.profile:
        print(export_cookies_from_gpm(a.profile))
    elif a.url:
        print(json.dumps(download(a.url), ensure_ascii=False, indent=1))
    else:
        ap.print_help()
