# -*- coding: utf-8 -*-
"""VIDEO STUDIO — web dashboard (port 7995). KHÔNG đăng bài, chỉ LÀM video.
Nhánh 1 Remake (link/file -> biến thể) + Nhánh 2 Review (một hoặc nhiều video -> ghép review).
1 worker chạy TUẦN TỰ (1 job/lúc) -> máy không nóng."""
import os, io, sys, json, time, uuid, shutil, threading, queue, secrets, re, subprocess

# Chỉ cho phép một Video Studio chạy trên Windows. Nhiều tiến trình cùng cổng
# có thể nhận job luân phiên với môi trường/PATH khác nhau và gây lỗi khó đoán.
_INSTANCE_MUTEX = None
if os.name == "nt":
    import ctypes
    _INSTANCE_MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\VideoStudio_Port_7995")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("Video Studio đã chạy tại http://127.0.0.1:7995", file=sys.stderr)
        raise SystemExit(0)

# python.exe: ghi ra console như bình thường. pythonw.exe: không có console,
# nên bỏ output để chạy nền hoàn toàn và không tạo file log runtime.
if sys.stdout is None:
    _null_stream = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = _null_stream
    sys.stderr = _null_stream
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E
import discovery, remake, review, soundtrack, translate
from flask import Flask, request, jsonify, send_from_directory, Response, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024   # 4GB upload
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

USERS_FILE = os.path.join(E.ROOT, "users.json")
SECRET_FILE = os.path.join(E.ROOT, ".auth_secret")

def _load_or_create_secret():
    try:
        value = open(SECRET_FILE, encoding="utf-8").read().strip()
        if value: return value
    except Exception: pass
    value = secrets.token_hex(32)
    with open(SECRET_FILE, "w", encoding="utf-8") as f: f.write(value)
    return value

app.secret_key = _load_or_create_secret()

def load_users():
    try:
        data = json.load(open(USERS_FILE, encoding="utf-8"))
        if isinstance(data, dict): return data
    except Exception: pass
    data = {"admin": {"password_hash": generate_password_hash("admin123"),
                      "role": "admin", "must_change_password": True}}
    save_users(data)
    return data

def save_users(data):
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)

def current_user():
    username = session.get("username")
    user = load_users().get(username) if username else None
    if not user: return None
    return {"username": username, "role": user.get("role", "user"),
            "must_change_password": bool(user.get("must_change_password"))}

def is_admin():
    user = current_user()
    return bool(user and user["role"] == "admin")

def can_access_job(job):
    user = current_user()
    if not user or not job: return False
    return user["role"] == "admin" or job.get("owner") == user["username"]

def forbidden():
    return jsonify({"ok": False, "err": "Bạn không có quyền truy cập"}), 403

_CREDITS_CACHE = {"at": 0.0, "data": None}
_CREDITS_LOCK = threading.Lock()
_FB_OVERVIEW_CACHE = {}
_FB_OVERVIEW_LOCK = threading.Lock()

def _provider_credits():
    """Read TTS balances without sending provider keys to the browser."""
    import httpx
    result = {}
    genmax_key = str(E.CONFIG.get("genmax_api_key") or os.environ.get("GENMAX_API_KEY") or "").strip()
    if not genmax_key:
        result["genmax"] = {"ok": False, "configured": False, "error": "Chưa cấu hình API key"}
    else:
        try:
            response = httpx.get("https://api.genmax.io/v1/auth/me",
                                 headers={"xi-api-key": genmax_key}, timeout=12)
            response.raise_for_status()
            data = response.json()
            result["genmax"] = {"ok": True, "configured": True,
                                "credits": data.get("credit_balance"), "unit": "credits"}
        except Exception:
            result["genmax"] = {"ok": False, "configured": True, "error": "Không đọc được số dư"}

    vivibe_key = str(E.CONFIG.get("vivibe_api_key") or os.environ.get("VIVIBE_API_KEY") or "").strip()
    if not vivibe_key:
        result["vivibe"] = {"ok": False, "configured": False, "error": "Chưa cấu hình API key"}
    else:
        try:
            response = httpx.post("https://api.lucylab.io/json-rpc",
                                  headers={"Authorization": "Bearer " + vivibe_key,
                                           "Content-Type": "application/json"},
                                  json={"jsonrpc": "2.0", "method": "getUserInfo",
                                        "params": {}, "id": 1}, timeout=12)
            response.raise_for_status()
            user = (response.json().get("result") or {}).get("user") or {}
            if "creditsRemaining" not in user:
                raise ValueError("missing creditsRemaining")
            result["vivibe"] = {"ok": True, "configured": True,
                                "credits": user.get("creditsRemaining"), "unit": "credits",
                                "plan": user.get("subscriptionTier") or "",
                                "reset_at": user.get("creditsResetAt") or ""}
        except Exception:
            result["vivibe"] = {"ok": False, "configured": True, "error": "Không đọc được số dư"}
    return result

@app.before_request
def require_login():
    if request.endpoint == "login": return None
    if not current_user():
        if request.path.startswith("/api/") or request.path.startswith("/out/"):
            return jsonify({"ok": False, "err": "Vui lòng đăng nhập"}), 401
        return redirect("/login")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form
        username = str(data.get("username", "")).strip().lower()
        password = str(data.get("password", ""))
        user = load_users().get(username)
        if not user or not check_password_hash(user.get("password_hash", ""), password):
            return jsonify({"ok": False, "err": "Sai tên đăng nhập hoặc mật khẩu"}), 401
        session.clear(); session["username"] = username
        return jsonify({"ok": True})
    if current_user(): return redirect("/")
    return Response(LOGIN_PAGE, mimetype="text/html")

@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})

@app.get("/api/me")
def api_me():
    return jsonify(current_user())

@app.post("/api/change-password")
def api_change_password():
    data = request.get_json(silent=True) or {}
    old, new = str(data.get("old_password", "")), str(data.get("new_password", ""))
    if len(new) < 8: return jsonify({"ok": False, "err": "Mật khẩu mới cần ít nhất 8 ký tự"}), 400
    users = load_users(); username = session["username"]; user = users.get(username, {})
    if not check_password_hash(user.get("password_hash", ""), old):
        return jsonify({"ok": False, "err": "Mật khẩu hiện tại không đúng"}), 400
    user["password_hash"] = generate_password_hash(new); user["must_change_password"] = False
    users[username] = user; save_users(users)
    return jsonify({"ok": True})

@app.route("/api/users", methods=["GET", "POST"])
def api_users():
    if not is_admin(): return forbidden()
    users = load_users()
    if request.method == "GET":
        return jsonify({"users": [{"username": name, "role": u.get("role", "user")}
                                   for name, u in sorted(users.items())]})
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", "")); role = str(data.get("role", "user"))
    if not re.fullmatch(r"[a-z0-9_.-]{3,32}", username):
        return jsonify({"ok": False, "err": "Tên đăng nhập: 3-32 ký tự a-z, số, _, . hoặc -"}), 400
    if len(password) < 8: return jsonify({"ok": False, "err": "Mật khẩu cần ít nhất 8 ký tự"}), 400
    if role not in ("admin", "user"): role = "user"
    if username in users: return jsonify({"ok": False, "err": "Tài khoản đã tồn tại"}), 409
    users[username] = {"password_hash": generate_password_hash(password), "role": role}
    save_users(users); return jsonify({"ok": True})

@app.route("/api/users/<username>", methods=["PATCH", "DELETE"])
def api_user(username):
    if not is_admin(): return forbidden()
    username = username.lower(); users = load_users()
    if username not in users: return jsonify({"ok": False, "err": "Không tìm thấy tài khoản"}), 404
    data = request.get_json(silent=True) or {}
    if request.method == "DELETE":
        if username == session["username"]: return jsonify({"ok": False, "err": "Không thể xoá tài khoản đang đăng nhập"}), 400
        del users[username]
    else:
        password = str(data.get("password", ""))
        if len(password) < 8: return jsonify({"ok": False, "err": "Mật khẩu cần ít nhất 8 ký tự"}), 400
        users[username]["password_hash"] = generate_password_hash(password)
        users[username]["must_change_password"] = False
    save_users(users); return jsonify({"ok": True})

SETTINGS_FILE = os.path.join(E.ROOT, "settings.json")
DEFAULTS = {"niche": "co-khi", "voice": "adam3", "hook": "auto", "flip": "auto",
            "style": "auto", "n_variants": 3, "review_voice": "adam3",
            "review_hook": "auto", "review_style": "auto", "review_n": 3,
            "review_fw": "auto"}
def load_settings():
    s = dict(DEFAULTS)
    try: s.update(json.load(open(SETTINGS_FILE, encoding="utf-8")))
    except Exception: pass
    return s
def save_settings(d):
    s = load_settings(); s.update({k: d[k] for k in d if k in DEFAULTS})
    try: s["n_variants"] = int(s["n_variants"]); s["review_n"] = int(s["review_n"])
    except Exception: pass
    json.dump(s, open(SETTINGS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return s

def save_vivibe_key(key):
    key = str(key or "").strip()
    if not key:
        return False
    p = os.path.join(E.ROOT, "config.json")
    try: cfg = json.load(open(p, encoding="utf-8"))
    except Exception: cfg = {}
    cfg["vivibe_api_key"] = key
    json.dump(cfg, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    E.CONFIG.update(cfg)
    os.environ["VIVIBE_API_KEY"] = key
    return True

def save_genmax_key(key):
    key = str(key or "").strip()
    if not key: return False
    p = os.path.join(E.ROOT, "config.json")
    try: cfg = json.load(open(p, encoding="utf-8"))
    except Exception: cfg = {}
    cfg["genmax_api_key"] = key
    json.dump(cfg, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    E.CONFIG.update(cfg)
    os.environ["GENMAX_API_KEY"] = key
    return True

def save_douyin_cookie(cookie):
    cookie = str(cookie or "").strip()
    if not cookie: return False
    p = os.path.join(E.ROOT, "config.json")
    try: cfg = json.load(open(p, encoding="utf-8"))
    except Exception: cfg = {}
    cfg["douyin_cookie"] = cookie
    json.dump(cfg, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    E.CONFIG.update(cfg)
    os.environ["DOUYIN_COOKIE"] = cookie
    return True

def save_bilibili_cookie(cookie):
    cookie = str(cookie or "").strip()
    if not cookie or "\r" in cookie or "\n" in cookie: return False
    p = os.path.join(E.ROOT, "config.json")
    try: cfg = json.load(open(p, encoding="utf-8"))
    except Exception: cfg = {}
    cfg["bilibili_cookie"] = cookie
    json.dump(cfg, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    E.CONFIG.update(cfg)
    os.environ["BILIBILI_COOKIE"] = cookie
    return True

def facebook_pages():
    pages = [p for p in (E.CONFIG.get("facebook_pages") or [])
             if isinstance(p, dict) and p.get("id") and p.get("access_token")]
    if not pages and E.CONFIG.get("facebook_page_id") and E.CONFIG.get("facebook_page_access_token"):
        pages = [{"id": str(E.CONFIG["facebook_page_id"]), "name": "Facebook Page",
                  "access_token": str(E.CONFIG["facebook_page_access_token"])}]
    return pages

def save_facebook_config(page_id, access_token, page_name=""):
    page_id, access_token = str(page_id or "").strip(), str(access_token or "").strip()
    page_name = str(page_name or "").strip()
    if not page_id:
        return False
    p = os.path.join(E.ROOT, "config.json")
    try: cfg = json.load(open(p, encoding="utf-8"))
    except Exception: cfg = {}
    pages = [item for item in (cfg.get("facebook_pages") or []) if isinstance(item, dict)]
    if not pages and cfg.get("facebook_page_id") and cfg.get("facebook_page_access_token"):
        pages.append({"id": str(cfg["facebook_page_id"]), "name": "Facebook Page",
                      "access_token": str(cfg["facebook_page_access_token"])})
    current = next((item for item in pages if str(item.get("id")) == page_id), None)
    if current:
        if access_token: current["access_token"] = access_token
        if page_name: current["name"] = page_name
    elif access_token:
        pages.append({"id": page_id, "name": page_name or page_id, "access_token": access_token})
    else:
        return False
    cfg["facebook_pages"] = pages
    cfg.pop("facebook_page_id", None); cfg.pop("facebook_page_access_token", None)
    json.dump(cfg, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    E.CONFIG.update(cfg)
    return True

def delete_facebook_page(page_id):
    p = os.path.join(E.ROOT, "config.json")
    try: cfg = json.load(open(p, encoding="utf-8"))
    except Exception: cfg = {}
    cfg["facebook_pages"] = [item for item in (cfg.get("facebook_pages") or [])
                             if str(item.get("id")) != str(page_id)]
    if str(cfg.get("facebook_page_id") or "") == str(page_id):
        cfg.pop("facebook_page_id", None); cfg.pop("facebook_page_access_token", None)
    json.dump(cfg, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    E.CONFIG.update(cfg)

JOBQ = queue.Queue()
JOB_STATE_LOCK = threading.Lock()

def worker():
    while True:
        jid = JOBQ.get()
        job = E.load_job(jid)
        if not job:
            JOBQ.task_done(); continue
        try:
            job["status"] = "running"; job["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            E.save_job(job)
            provider = str((job.get("opts") or {}).get("tts_provider") or
                           E.CONFIG.get("tts_provider") or "vivibe").lower()
            os.environ["AUTOVOICE_TTS_PROVIDER"] = provider if provider in ("genmax", "vivibe") else "vivibe"
            if job["kind"] == "remake":
                remake.run_safe(job, E.save_job)
            elif job["kind"] == "review":
                review.run_safe(job, E.save_job)
            elif job["kind"] in ("translate", "reup"):
                translate.run_safe(job, E.save_job)
            elif job["kind"] == "movie_studio":
                translate.run_movie_studio(job, E.save_job)
            elif job["kind"] == "soundtrack":
                soundtrack.run_safe(job, E.save_job)
            else:
                raise ValueError(f"Loại job không hợp lệ: {job['kind']}")
        except Exception as e:
            job["status"] = "error"; job["error"] = str(e); E.save_job(job)
        JOBQ.task_done()

threading.Thread(target=worker, daemon=True).start()

def _save_uploads(jid, files):
    d = os.path.join(E.IN_DIR, jid); os.makedirs(d, exist_ok=True)
    out = []
    for f in files:
        if not f or not f.filename: continue
        name = os.path.basename(f.filename).replace("'", "")
        p = os.path.join(d, name); f.save(p)
        out.append({"type": "file", "path": p, "name": name})
    return out

def _make_job(kind, inputs, opts):
    jid = time.strftime("%y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
    job = {"id": jid, "kind": kind, "inputs": inputs, "opts": opts,
           "owner": session["username"],
           "status": "queued", "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "log": [], "outputs": []}
    E.save_job(job); JOBQ.put(jid)
    return jid

def _collect():
    links = [x.strip() for x in (request.form.get("links","")).splitlines() if x.strip()]
    inputs = [{"type": "link", "url": u} for u in links]
    jid_tmp = time.strftime("%y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
    inputs += _save_uploads(jid_tmp, request.files.getlist("files"))
    opts = {
        "niche": request.form.get("niche","co-khi"),
        "hook":  request.form.get("hook","auto"),
        "voice": request.form.get("voice","adam3"),
        "tts_provider": request.form.get("tts_provider", E.CONFIG.get("tts_provider", "vivibe")).lower(),
        "flip":  request.form.get("flip","auto"),
        "style": request.form.get("style","auto"),
        "n_variants": int(request.form.get("n_variants","3") or 3),
        "product": request.form.get("product","").strip(),
        "product_link": request.form.get("product_link", "").strip()[:1000],
        # nhánh Review: kịch bản + facts sản phẩm (nhập tay)
        "framework": request.form.get("framework","auto"),
        "specs": request.form.get("specs","").strip(),
        "price": request.form.get("price","").strip(),
        "cta":   request.form.get("cta","").strip(),
        "blur":  request.form.get("blur",""),      # tương thích các job cũ
        "original_subtitles": request.form.get("original_subtitles", "keep"),
        "exclude_people": request.form.get("exclude_people", ""),
        "hook_variants": int(request.form.get("hook_variants","1") or 1),  # số phiên bản hook/video
    }
    return inputs, opts

@app.post("/api/remake")
def api_remake():
    inputs, opts = _collect()
    opts["remove_original_audio"] = request.form.get("remove_original_audio") == "1"
    if not inputs: return jsonify({"ok": False, "err": "Chưa có link/file nào"}), 400
    if opts.get("product_link") and not re.match(r"^https?://", opts["product_link"], re.I):
        return jsonify({"ok": False, "err": "Link affiliate Shopee không hợp lệ"}), 400
    provider = opts.get("tts_provider")
    if provider not in ("genmax", "vivibe"): return jsonify({"ok": False, "err": "Dịch vụ giọng đọc không hợp lệ"}), 400
    if provider == "genmax" and not (os.environ.get("GENMAX_API_KEY") or E.CONFIG.get("genmax_api_key")):
        return jsonify({"ok": False, "err": "Chưa cấu hình GenMax API key"}), 400
    if provider == "vivibe" and not (os.environ.get("VIVIBE_API_KEY") or E.CONFIG.get("vivibe_api_key")):
        return jsonify({"ok": False, "err": "Chưa cấu hình Vivibe API key"}), 400
    return jsonify({"ok": True, "id": _make_job("remake", inputs, opts)})

@app.post("/api/review")
def api_review():
    inputs, opts = _collect()
    opts["remove_original_audio"] = request.form.get("remove_original_audio") == "1"
    if not inputs:
        return jsonify({"ok": False, "err": "Chưa có link/file nào để review"}), 400
    if opts.get("product_link") and not re.match(r"^https?://", opts["product_link"], re.I):
        return jsonify({"ok": False, "err": "Link affiliate Shopee không hợp lệ"}), 400
    provider = opts.get("tts_provider")
    if provider not in ("genmax", "vivibe"): return jsonify({"ok": False, "err": "Dịch vụ giọng đọc không hợp lệ"}), 400
    if provider == "genmax" and not (os.environ.get("GENMAX_API_KEY") or E.CONFIG.get("genmax_api_key")):
        return jsonify({"ok": False, "err": "Chưa cấu hình GenMax API key"}), 400
    if provider == "vivibe" and not (os.environ.get("VIVIBE_API_KEY") or E.CONFIG.get("vivibe_api_key")):
        return jsonify({"ok": False, "err": "Chưa cấu hình Vivibe API key"}), 400
    return jsonify({"ok": True, "id": _make_job("review", inputs, opts)})

@app.post("/api/translate")
def api_translate():
    inputs, _ = _collect()
    if not inputs:
        return jsonify({"ok": False, "err": "Chưa có link/file phim cần dịch"}), 400
    source_language = str(request.form.get("source_language") or "").strip().lower()
    target_language = str(request.form.get("target_language") or "vi").strip().lower()
    task_mode = str(request.form.get("task_mode") or "summary").strip().lower()
    if task_mode not in ("translate", "summary"):
        return jsonify({"ok": False, "err": "Chế độ xử lý không hợp lệ"}), 400
    if task_mode == "summary":
        target_language = "vi"
    requested_output = str(request.form.get("translation_output") or "").strip().lower()
    output_mode = ((requested_output or ("original" if target_language == "none" else "both"))
                   if task_mode == "translate" else "summary")
    original_subtitles = str(request.form.get("original_subtitles") or "smart_bottom").strip().lower()
    video_speed = str(request.form.get("video_speed") or "1").strip()
    movie_flip = str(request.form.get("movie_flip") or "none").strip().lower()
    movie_title = " ".join(str(request.form.get("movie_title") or "").split())[:160]
    subtitle_font_size = str(request.form.get("subtitle_font_size") or "18").strip()
    subtitle_font_color = str(request.form.get("subtitle_font_color") or "#ffffff").strip().upper()
    subtitle_outline_color = str(request.form.get("subtitle_outline_color") or "#101010").strip().upper()
    if source_language not in ("", *translate.LANGUAGES.keys()):
        return jsonify({"ok": False, "err": "Ngôn ngữ nguồn không hợp lệ"}), 400
    if target_language not in ("none", *translate.LANGUAGES.keys()):
        return jsonify({"ok": False, "err": "Ngôn ngữ đích không hợp lệ"}), 400
    if output_mode not in ("original", "srt", "burn", "both", "dub", "dub_srt", "summary", "all"):
        return jsonify({"ok": False, "err": "Kiểu đầu ra không hợp lệ"}), 400
    if output_mode == "original" and target_language != "none":
        return jsonify({"ok": False, "err": "Giữ nguyên video gốc chỉ dùng khi chọn Không dịch"}), 400
    wants_dub = output_mode in ("dub", "dub_srt", "all")
    if wants_dub and target_language != "vi":
        return jsonify({"ok": False, "err": "Lồng tiếng hiện chỉ hỗ trợ tiếng Việt"}), 400
    if original_subtitles not in ("keep", "bottom", "top", "smart_bottom", "smart_top", "smart_custom"):
        return jsonify({"ok": False, "err": "Vị trí phụ đề gốc không hợp lệ"}), 400
    subtitle_region = None
    if original_subtitles == "smart_custom":
        try:
            subtitle_region = [round(float(request.form.get(key)), 2)
                               for key in ("subtitle_region_x", "subtitle_region_y",
                                           "subtitle_region_width", "subtitle_region_height")]
        except (TypeError, ValueError):
            return jsonify({"ok": False, "err": "Vùng phụ đề tùy chỉnh không hợp lệ"}), 400
        x, y, width, height = subtitle_region
        if min(x, y) < 0 or min(width, height) <= 0 or x + width > 100 or y + height > 100:
            return jsonify({"ok": False, "err": "Vùng phụ đề phải nằm trong khung hình 0–100%"}), 400
    if video_speed not in ("0.75", "1", "1.25", "1.5", "2"):
        return jsonify({"ok": False, "err": "Tốc độ video không hợp lệ"}), 400
    if movie_flip not in ("none", "hflip"):
        return jsonify({"ok": False, "err": "Kiểu lật hình không hợp lệ"}), 400
    if not subtitle_font_size.isdigit() or not 12 <= int(subtitle_font_size) <= 48:
        return jsonify({"ok": False, "err": "Cỡ chữ phụ đề phải từ 12 đến 48"}), 400
    if not re.fullmatch(r"#[0-9A-F]{6}", subtitle_font_color):
        return jsonify({"ok": False, "err": "Màu chữ phụ đề không hợp lệ"}), 400
    if not re.fullmatch(r"#[0-9A-F]{6}", subtitle_outline_color):
        return jsonify({"ok": False, "err": "Màu viền phụ đề không hợp lệ"}), 400
    summary_minutes = str(request.form.get("summary_minutes") or "auto").strip().lower()
    if summary_minutes not in ("auto", "5", "10", "15", "20"):
        return jsonify({"ok": False, "err": "Thời lượng tóm tắt không hợp lệ"}), 400
    provider = str(request.form.get("tts_provider") or E.CONFIG.get("tts_provider") or "vivibe").lower()
    voice = str(request.form.get("voice") or "auto").strip()
    if task_mode == "summary" or wants_dub:
        if provider not in ("genmax", "vivibe"):
            return jsonify({"ok": False, "err": "Dịch vụ giọng đọc không hợp lệ"}), 400
        if provider == "genmax" and not (os.environ.get("GENMAX_API_KEY") or E.CONFIG.get("genmax_api_key")):
            return jsonify({"ok": False, "err": "Chưa cấu hình GenMax API key"}), 400
        if provider == "vivibe" and not (os.environ.get("VIVIBE_API_KEY") or E.CONFIG.get("vivibe_api_key")):
            return jsonify({"ok": False, "err": "Chưa cấu hình Vivibe API key"}), 400
    opts = {"source_language": source_language, "target_language": target_language,
            "task_mode": task_mode, "output_mode": output_mode, "make_summary": task_mode == "summary",
            "summary_minutes": summary_minutes, "tts_provider": provider, "voice": voice,
            "original_subtitles": original_subtitles, "subtitle_region": subtitle_region,
            "video_speed": float(video_speed),
            "movie_flip": movie_flip, "movie_title": movie_title,
            "subtitle_font_size": int(subtitle_font_size),
            "subtitle_font_color": subtitle_font_color,
            "subtitle_outline_color": subtitle_outline_color}
    return jsonify({"ok": True, "id": _make_job("translate", inputs, opts)})


@app.post("/api/movie-studio")
def api_movie_studio_create():
    inputs, _ = _collect()
    if not inputs:
        return jsonify({"ok": False, "err": "Chưa có link/file phim cần biên tập"}), 400
    source_language = str(request.form.get("source_language") or "").strip().lower()
    target_language = str(request.form.get("target_language") or "vi").strip().lower()
    task_mode = str(request.form.get("task_mode") or "translate").strip().lower()
    if task_mode not in ("translate", "summary"):
        return jsonify({"ok": False, "err": "Chế độ xử lý không hợp lệ"}), 400
    if source_language not in ("", *translate.LANGUAGES.keys()):
        return jsonify({"ok": False, "err": "Ngôn ngữ nguồn không hợp lệ"}), 400
    if target_language not in ("none", *translate.LANGUAGES.keys()):
        return jsonify({"ok": False, "err": "Ngôn ngữ đích không hợp lệ"}), 400
    if task_mode == "summary":
        target_language = "vi"
    output_mode = (str(request.form.get("translation_output") or "both").strip().lower()
                   if task_mode == "translate" else "summary")
    if output_mode not in ("srt", "burn", "both", "dub", "dub_srt", "summary", "all"):
        return jsonify({"ok": False, "err": "Kiểu đầu ra không hợp lệ"}), 400
    original_subtitles = str(request.form.get("original_subtitles") or "smart_bottom").strip().lower()
    if original_subtitles not in ("keep", "bottom", "top", "smart_bottom", "smart_top", "smart_custom"):
        return jsonify({"ok": False, "err": "Vị trí phụ đề gốc không hợp lệ"}), 400
    subtitle_region = None
    subtitle_frame_ratio = str(request.form.get("subtitle_frame_ratio") or "16:9").strip()
    if subtitle_frame_ratio not in ("9:16", "16:9", "1:1", "4:3", "3:4"):
        return jsonify({"ok": False, "err": "Tỷ lệ khung hình mẫu không hợp lệ"}), 400
    if original_subtitles == "smart_custom":
        try:
            subtitle_region = [round(float(request.form.get(key)), 2)
                               for key in ("subtitle_region_x", "subtitle_region_y",
                                           "subtitle_region_width", "subtitle_region_height")]
        except (TypeError, ValueError):
            return jsonify({"ok": False, "err": "Vùng phụ đề tùy chỉnh không hợp lệ"}), 400
        x, y, width, height = subtitle_region
        if min(x, y) < 0 or min(width, height) <= 0 or x + width > 100 or y + height > 100:
            return jsonify({"ok": False, "err": "Vùng phụ đề phải nằm trong khung hình 0–100%"}), 400
    video_speed = str(request.form.get("video_speed") or "1").strip()
    movie_flip = str(request.form.get("movie_flip") or "none").strip().lower()
    if video_speed not in ("0.75", "1", "1.25", "1.5", "2") or movie_flip not in ("none", "hflip"):
        return jsonify({"ok": False, "err": "Tùy chỉnh video không hợp lệ"}), 400
    summary_minutes = str(request.form.get("summary_minutes") or "auto").strip().lower()
    if summary_minutes not in ("auto", "5", "10", "15", "20"):
        return jsonify({"ok": False, "err": "Thời lượng tóm tắt không hợp lệ"}), 400
    provider = str(request.form.get("tts_provider") or E.CONFIG.get("tts_provider") or "vivibe").lower()
    wants_dub = task_mode == "summary" or output_mode in ("dub", "dub_srt", "all")
    if wants_dub and provider not in ("genmax", "vivibe"):
        return jsonify({"ok": False, "err": "Dịch vụ giọng đọc không hợp lệ"}), 400
    if wants_dub and provider == "genmax" and not (os.environ.get("GENMAX_API_KEY") or E.CONFIG.get("genmax_api_key")):
        return jsonify({"ok": False, "err": "Chưa cấu hình GenMax API key"}), 400
    if wants_dub and provider == "vivibe" and not (os.environ.get("VIVIBE_API_KEY") or E.CONFIG.get("vivibe_api_key")):
        return jsonify({"ok": False, "err": "Chưa cấu hình Vivibe API key"}), 400
    font_size = str(request.form.get("subtitle_font_size") or "18").strip()
    font_color = str(request.form.get("subtitle_font_color") or "#ffffff").strip().upper()
    outline_color = str(request.form.get("subtitle_outline_color") or "#101010").strip().upper()
    if not font_size.isdigit() or not 12 <= int(font_size) <= 48:
        return jsonify({"ok": False, "err": "Cỡ chữ phụ đề phải từ 12 đến 48"}), 400
    if not re.fullmatch(r"#[0-9A-F]{6}", font_color) or not re.fullmatch(r"#[0-9A-F]{6}", outline_color):
        return jsonify({"ok": False, "err": "Màu phụ đề không hợp lệ"}), 400
    opts = {
        "phase": "prepare", "source_language": source_language,
        "target_language": target_language, "task_mode": task_mode,
        "output_mode": output_mode, "make_summary": task_mode == "summary",
        "summary_minutes": summary_minutes, "tts_provider": provider,
        "voice": str(request.form.get("voice") or "auto").strip(),
        "original_subtitles": original_subtitles, "subtitle_region": subtitle_region,
        "subtitle_frame_ratio": subtitle_frame_ratio,
        "video_speed": float(video_speed),
        "movie_flip": movie_flip,
        "movie_title": " ".join(str(request.form.get("movie_title") or "").split())[:160],
        "subtitle_font_size": int(font_size), "subtitle_font_color": font_color,
        "subtitle_outline_color": outline_color,
    }
    jid = _make_job("movie_studio", inputs, opts)
    return jsonify({"ok": True, "id": jid})


def _movie_studio_job(jid):
    job = E.load_job(jid)
    if not job:
        return None, (jsonify({"ok": False, "err": "Không tìm thấy dự án"}), 404)
    if not can_access_job(job):
        return None, forbidden()
    if job.get("kind") != "movie_studio":
        return None, (jsonify({"ok": False, "err": "Job không phải dự án Studio phim"}), 400)
    return job, None


def _public_movie_drafts(job):
    drafts = []
    for item in job.get("drafts") or []:
        public = {key: value for key, value in item.items() if key != "source_path"}
        public["preview_url"] = f"/api/movie-studio/{job['id']}/source/{int(item.get('index', 0))}"
        drafts.append(public)
    return drafts


@app.get("/api/movie-studio/<jid>")
def api_movie_studio_get(jid):
    job, error = _movie_studio_job(jid)
    if error: return error
    opts = job.get("opts") or {}
    return jsonify({"ok": True, "id": jid, "status": job.get("status"),
                    "phase": job.get("phase"), "draft_version": job.get("draft_version", 0),
                    "drafts": _public_movie_drafts(job), "error": job.get("error", ""),
                    "original_subtitles": opts.get("original_subtitles", "smart_bottom"),
                    "subtitle_region": opts.get("subtitle_region")})


@app.patch("/api/movie-studio/<jid>")
def api_movie_studio_update(jid):
    with JOB_STATE_LOCK:
        job, error = _movie_studio_job(jid)
        if error: return error
        if job.get("status") != "awaiting_review":
            return jsonify({"ok": False, "err": "Dự án không ở trạng thái chờ duyệt"}), 409
        data = request.get_json(silent=True) or {}
        original_subtitles = str(data.get("original_subtitles") or
                                 (job.get("opts") or {}).get("original_subtitles") or
                                 "smart_bottom").strip().lower()
        valid_subtitle_modes = {"keep", "bottom", "top", "smart_bottom", "smart_top", "smart_custom"}
        if original_subtitles not in valid_subtitle_modes:
            return jsonify({"ok": False, "err": "Kiểu xử lý phụ đề gốc không hợp lệ"}), 400
        subtitle_region = data.get("subtitle_region")
        if original_subtitles == "smart_custom":
            try:
                subtitle_region = [round(float(value), 2) for value in subtitle_region]
                if len(subtitle_region) != 4:
                    raise ValueError
            except (TypeError, ValueError):
                return jsonify({"ok": False, "err": "Hãy khoanh vùng phụ đề trên video"}), 400
            x, y, width, height = subtitle_region
            if min(x, y) < 0 or min(width, height) <= 0 or x + width > 100 or y + height > 100:
                return jsonify({"ok": False, "err": "Vùng phụ đề phải nằm trong khung hình 0–100%"}), 400
        else:
            subtitle_region = None
        submitted = data.get("drafts")
        if not isinstance(submitted, list):
            return jsonify({"ok": False, "err": "Dữ liệu bản nháp không hợp lệ"}), 400
        current = {int(item.get("index", i)): item for i, item in enumerate(job.get("drafts") or [])}
        updated = []
        for position, payload in enumerate(submitted):
            if not isinstance(payload, dict):
                return jsonify({"ok": False, "err": "Nguồn phim không hợp lệ"}), 400
            index = int(payload.get("index", position))
            original = current.get(index)
            if not original:
                return jsonify({"ok": False, "err": "Không tìm thấy nguồn phim trong dự án"}), 400
            duration = float(original.get("duration") or 0)
            segments = payload.get("segments")
            if not isinstance(segments, list) or len(segments) > 10000:
                return jsonify({"ok": False, "err": "Danh sách phụ đề không hợp lệ"}), 400
            clean, previous_end = [], 0.0
            for segment_index, segment in enumerate(segments):
                try:
                    start, end = round(float(segment.get("start")), 3), round(float(segment.get("end")), 3)
                except (AttributeError, TypeError, ValueError):
                    return jsonify({"ok": False, "err": f"Timestamp đoạn {segment_index + 1} không hợp lệ"}), 400
                text = str(segment.get("text") or "").strip()
                translated = str(segment.get("translated") or "").strip()
                speaker = " ".join(str(segment.get("speaker") or "Nhân vật 1").split())[:80]
                voice = str(segment.get("voice") or "").strip()
                if start < 0 or end <= start or (duration > 0 and end > duration + 0.05):
                    return jsonify({"ok": False, "err": f"Thời gian đoạn {segment_index + 1} nằm ngoài video"}), 400
                if start < previous_end - 0.001:
                    return jsonify({"ok": False, "err": f"Đoạn {segment_index + 1} chồng thời gian với đoạn trước"}), 400
                if len(text) > 5000 or len(translated) > 5000:
                    return jsonify({"ok": False, "err": f"Nội dung đoạn {segment_index + 1} quá dài"}), 400
                valid_voices = {"", "auto", "nam", "nu", *E.VOICE_BY_LABEL.keys(), *E.GENMAX_VOICES.keys()}
                if voice not in valid_voices:
                    return jsonify({"ok": False, "err": f"Giọng đọc đoạn {segment_index + 1} không hợp lệ"}), 400
                if (voice in E.GENMAX_VOICES and
                        not (os.environ.get("GENMAX_API_KEY") or E.CONFIG.get("genmax_api_key"))):
                    return jsonify({"ok": False, "err": "Chưa cấu hình GenMax API key để dùng giọng GenMax"}), 400
                clean.append({"id": segment_index, "start": start, "end": end,
                              "text": text, "translated": translated,
                              "speaker": speaker, "voice": voice})
                previous_end = end
            copy = dict(original); copy["segments"] = clean
            updated.append(copy)
        if set(current) != {int(item["index"]) for item in updated}:
            return jsonify({"ok": False, "err": "Phải lưu đủ tất cả nguồn phim trong dự án"}), 400
        job["drafts"] = sorted(updated, key=lambda item: int(item["index"]))
        job.setdefault("opts", {})["original_subtitles"] = original_subtitles
        job["opts"]["subtitle_region"] = subtitle_region
        job["draft_version"] = int(job.get("draft_version") or 0) + 1
        job["draft_saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        E.save_job(job)
    return jsonify({"ok": True, "draft_version": job["draft_version"]})


@app.post("/api/movie-studio/<jid>/confirm")
def api_movie_studio_confirm(jid):
    with JOB_STATE_LOCK:
        job, error = _movie_studio_job(jid)
        if error: return error
        if job.get("status") != "awaiting_review":
            return jsonify({"ok": False, "err": "Dự án đã được xác nhận hoặc chưa sẵn sàng"}), 409
        job["phase"] = "render"
        job["opts"]["phase"] = "render"
        job["status"] = "queued"
        job["confirmed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        E.save_job(job)
        JOBQ.put(jid)
    return jsonify({"ok": True, "id": jid})


@app.get("/api/movie-studio/<jid>/source/<int:source_index>")
def api_movie_studio_source(jid, source_index):
    job, error = _movie_studio_job(jid)
    if error: return error
    draft = next((item for item in job.get("drafts") or []
                  if int(item.get("index", -1)) == source_index), None)
    if not draft:
        return jsonify({"ok": False, "err": "Không tìm thấy nguồn phim"}), 404
    source = os.path.realpath(str(draft.get("source_path") or ""))
    work_root = os.path.realpath(os.path.join(E.WORK_DIR, jid))
    try:
        inside = os.path.commonpath([source, work_root]) == work_root
    except ValueError:
        inside = False
    if not inside or not os.path.isfile(source):
        return jsonify({"ok": False, "err": "Nguồn xem trước không hợp lệ"}), 404
    return send_from_directory(os.path.dirname(source), os.path.basename(source), conditional=True)

@app.post("/api/soundtrack")
def api_soundtrack():
    video_url = str(request.form.get("video_url") or "").strip()
    audio_url = str(request.form.get("audio_url") or "").strip()
    video_speed = str(request.form.get("video_speed") or "1").strip()
    movie_flip = str(request.form.get("movie_flip") or "none").strip().lower()
    if not re.match(r"^https?://", video_url, re.I):
        return jsonify({"ok": False, "err": "Link video không hợp lệ"}), 400
    if not re.match(r"^https?://", audio_url, re.I):
        return jsonify({"ok": False, "err": "Link âm thanh không hợp lệ"}), 400
    if video_speed not in ("0.75", "1", "1.25", "1.5", "2"):
        return jsonify({"ok": False, "err": "Tốc độ video không hợp lệ"}), 400
    if movie_flip not in ("none", "hflip"):
        return jsonify({"ok": False, "err": "Kiểu lật hình không hợp lệ"}), 400
    inputs = [{"type": "link", "url": video_url}]
    opts = {"video_url": video_url, "audio_url": audio_url, "video_speed": float(video_speed),
            "movie_flip": movie_flip, "n_variants": 1}
    return jsonify({"ok": True, "id": _make_job("soundtrack", inputs, opts)})

@app.post("/api/reup")
def api_reup():
    inputs, _ = _collect()
    if not inputs:
        return jsonify({"ok": False, "err": "Chưa có link/file video cần Reup"}), 400
    source_language = str(request.form.get("source_language") or "").strip().lower()
    if source_language not in ("", *translate.LANGUAGES.keys()):
        return jsonify({"ok": False, "err": "Ngôn ngữ nguồn không hợp lệ"}), 400
    summary_minutes = str(request.form.get("summary_minutes") or "auto").strip().lower()
    if summary_minutes not in ("auto", "1", "3", "5", "10"):
        return jsonify({"ok": False, "err": "Thời lượng Reup không hợp lệ"}), 400
    original_subtitles = str(request.form.get("original_subtitles") or "smart_bottom").strip().lower()
    if original_subtitles not in ("keep", "bottom", "top", "smart_bottom", "smart_top"):
        return jsonify({"ok": False, "err": "Xử lý phụ đề gốc không hợp lệ"}), 400
    provider = str(request.form.get("tts_provider") or E.CONFIG.get("tts_provider") or "vivibe").lower()
    if provider not in ("genmax", "vivibe"):
        return jsonify({"ok": False, "err": "Dịch vụ giọng đọc không hợp lệ"}), 400
    if provider == "genmax" and not (os.environ.get("GENMAX_API_KEY") or E.CONFIG.get("genmax_api_key")):
        return jsonify({"ok": False, "err": "Chưa cấu hình GenMax API key"}), 400
    if provider == "vivibe" and not (os.environ.get("VIVIBE_API_KEY") or E.CONFIG.get("vivibe_api_key")):
        return jsonify({"ok": False, "err": "Chưa cấu hình Vivibe API key"}), 400
    opts = {"source_language": source_language, "target_language": "vi",
            "task_mode": "summary", "output_mode": "summary", "make_summary": True,
            "summary_minutes": summary_minutes, "tts_provider": provider,
            "voice": str(request.form.get("voice") or "auto").strip(),
            "original_subtitles": original_subtitles, "reup_hook_skill": True}
    return jsonify({"ok": True, "id": _make_job("reup", inputs, opts)})

@app.post("/api/discovery/login")
def api_discovery_login():
    data = request.get_json(silent=True) or {}
    try:
        result = discovery.launch_login(E.ROOT, session["username"], data.get("platform"))
        return jsonify({"ok": True, **result})
    except (ValueError, RuntimeError, OSError) as exc:
        return jsonify({"ok": False, "err": str(exc)}), 400

@app.post("/api/discovery/search")
def api_discovery_search():
    data = request.get_json(silent=True) or {}
    try:
        result = discovery.discover(
            E.ROOT, session["username"], data.get("platform"),
            topic=data.get("topic", ""), limit=data.get("limit", 30),
            scan_date=data.get("date"))
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return jsonify({"ok": False, "err": str(exc)}), 400
    except (RuntimeError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return jsonify({"ok": False, "err": str(exc)}), 502

@app.get("/api/discovery/accounts")
def api_discovery_accounts():
    return jsonify({"ok": True, "accounts": discovery.list_accounts(E.ROOT, session["username"])})

@app.delete("/api/discovery/account/<platform>")
def api_discovery_account_delete(platform):
    try:
        result = discovery.reset_account(E.ROOT, session["username"], platform)
        return jsonify({"ok": True, **result})
    except (ValueError, RuntimeError, OSError) as exc:
        return jsonify({"ok": False, "err": str(exc)}), 400

def _slim(j):
    o = j.get("opts", {}); nin = len(j.get("inputs", []))
    nv = int(o.get("n_variants", 1) or 1)
    if j["kind"] == "review":
        exp = nv * max(1, int(o.get("hook_variants", 1) or 1))
    elif j["kind"] in ("translate", "reup", "movie_studio"):
        counts = {"srt": 1, "burn": 1, "dub": 1, "both": 2, "dub_srt": 2,
                  "summary": 2, "all": 3}
        exp = max(1, nin) * counts.get(o.get("output_mode"), 2)
    else:
        exp = nv * max(1, nin)
    return {"id":j["id"],"kind":j["kind"],"status":j["status"],"created_at":j.get("created_at"),
            "owner":j.get("owner", "legacy"),
            "n_out":len(j.get("outputs",[])),"n_in":nin,"expected":exp,
            "product":j.get("product",""),"error":j.get("error",""),
            "log_len":len(j.get("log",[])),"phase":j.get("phase","")}

@app.get("/api/jobs")
def api_jobs():
    js = [j for j in E.list_jobs() if can_access_job(j)][:60]
    return jsonify({"jobs": [_slim(j) for j in js], "queue": JOBQ.qsize()})

@app.get("/api/job/<jid>")
def api_job(jid):
    j = E.load_job(jid)
    if not j: return jsonify({"err":"not found"}), 404
    if not can_access_job(j): return forbidden()
    j = dict(j); j["log"] = j.get("log", [])[-80:]
    j.pop("inputs", None)
    return jsonify(j)

@app.post("/api/job/<jid>/note")
def api_job_note(jid):
    job = E.load_job(jid)
    if not job: return jsonify({"ok": False, "err": "Không tìm thấy job"}), 404
    if not can_access_job(job): return forbidden()
    data = request.get_json(silent=True) or {}
    note = str(data.get("note", "")).strip()
    if len(note) > 2000:
        return jsonify({"ok": False, "err": "Ghi chú tối đa 2.000 ký tự"}), 400
    job["note"] = note
    E.save_job(job)
    return jsonify({"ok": True, "note": note})

@app.post("/api/job/<jid>/output-note")
def api_output_note(jid):
    job = E.load_job(jid)
    if not job: return jsonify({"ok": False, "err": "Không tìm thấy job"}), 404
    if not can_access_job(job): return forbidden()
    data = request.get_json(silent=True) or {}
    filename = str(data.get("file", ""))
    note = str(data.get("note", "")).strip()
    if len(note) > 2000:
        return jsonify({"ok": False, "err": "Ghi chú tối đa 2.000 ký tự"}), 400
    output = next((o for o in job.get("outputs", []) if o.get("file") == filename), None)
    if not output: return jsonify({"ok": False, "err": "Không tìm thấy video"}), 404
    output["note"] = note
    if "posted" in data:
        output["posted"] = bool(data.get("posted"))
    E.save_job(job)
    return jsonify({"ok": True, "note": note, "posted": bool(output.get("posted"))})

@app.get("/api/overview")
def api_overview():
    js = [j for j in E.list_jobs() if can_access_job(j)]
    total_out = sum(len(j.get("outputs",[])) for j in js)
    running = sum(1 for j in js if j["status"]=="running")
    done = sum(1 for j in js if j["status"]=="done")
    err = sum(1 for j in js if j["status"]=="error")
    try: free = shutil.disk_usage("E:/").free/1e9
    except Exception: free = 0
    outsz = 0
    for root,_,fs in os.walk(E.OUT_DIR):
        for f in fs:
            try: outsz += os.path.getsize(os.path.join(root,f))
            except Exception: pass
    return jsonify({"total_out":total_out,"jobs":len(js),"running":running,"done":done,
                    "error":err,"queue":JOBQ.qsize(),"disk_free_gb":round(free),
                    "out_size_gb":round(outsz/1e9,2),"out_dir":E.OUT_DIR,
                     "recent":[_slim(j) for j in js[:6]]})

@app.get("/api/facebook/overview")
def api_facebook_overview():
    if not is_admin(): return forbidden()
    page_id = str(request.args.get("page_id") or "").strip()
    page = next((p for p in facebook_pages() if str(p["id"]) == page_id), None)
    if not page: return jsonify({"ok": False, "err": "Khong tim thay Facebook Page"}), 404
    try: days = max(1, min(90, int(request.args.get("days") or 28)))
    except (TypeError, ValueError): days = 28
    refresh = request.args.get("refresh") == "1"
    cache_key = (page_id, days)
    with _FB_OVERVIEW_LOCK:
        cached = _FB_OVERVIEW_CACHE.get(cache_key)
        if cached and not refresh and time.time() - cached["at"] < 300:
            return jsonify(cached["data"])

    import datetime, httpx
    until = datetime.date.today() + datetime.timedelta(days=1)
    since = until - datetime.timedelta(days=days)
    metrics = "page_media_view,page_post_engagements,page_daily_follows,page_daily_unfollows"
    try:
        response = httpx.get(f"https://graph.facebook.com/v26.0/{page_id}/insights",
                             params={"metric": metrics, "period": "day",
                                     "since": since.isoformat(), "until": until.isoformat(),
                                     "access_token": page["access_token"]}, timeout=25)
        response.raise_for_status()
        totals = {"views": 0, "interactions": 0, "follows": 0, "unfollows": 0}
        series_raw = {"views": [], "interactions": [], "follows": [], "unfollows": []}
        names = {"page_media_view": "views", "page_post_engagements": "interactions",
                 "page_daily_follows": "follows", "page_daily_unfollows": "unfollows"}
        found = set()
        for metric in response.json().get("data", []):
            key = names.get(metric.get("name"))
            if not key: continue
            found.add(key)
            for item in metric.get("values", []):
                value = item.get("value")
                numeric = value if isinstance(value, (int, float)) else 0
                series_raw[key].append(numeric)
                totals[key] += numeric
        dates = [(since + datetime.timedelta(days=i)).isoformat() for i in range(days)]
        def points(key):
            values = series_raw[key]
            return [{"date": date, "value": values[i] if i < len(values) else 0}
                    for i, date in enumerate(dates)]
        follow_values, unfollow_values = series_raw["follows"], series_raw["unfollows"]
        net_series = [{"date": date,
                       "value": (follow_values[i] if i < len(follow_values) else 0) -
                                (unfollow_values[i] if i < len(unfollow_values) else 0)}
                      for i, date in enumerate(dates)]
        data = {"ok": True, "page_id": page_id, "page_name": page.get("name") or page_id,
                "days": days, "since": since.isoformat(), "until": (until - datetime.timedelta(days=1)).isoformat(),
                "views": totals["views"] if "views" in found else None,
                "interactions": totals["interactions"] if "interactions" in found else None,
                "net_followers": (totals["follows"] - totals["unfollows"])
                                 if "follows" in found and "unfollows" in found else None,
                "series": {"views": points("views"), "interactions": points("interactions"),
                           "net_followers": net_series}}
        with _FB_OVERVIEW_LOCK: _FB_OVERVIEW_CACHE[cache_key] = {"at": time.time(), "data": data}
        return jsonify(data)
    except httpx.HTTPStatusError as exc:
        try: message = exc.response.json().get("error", {}).get("message")
        except Exception: message = None
        return jsonify({"ok": False, "err": message or "Khong doc duoc Facebook Insights"}), 400
    except Exception:
        return jsonify({"ok": False, "err": "Khong ket noi duoc Facebook"}), 502

@app.get("/api/credits")
def api_credits():
    if not is_admin(): return forbidden()
    refresh = request.args.get("refresh") == "1"
    with _CREDITS_LOCK:
        now = time.time()
        if refresh or _CREDITS_CACHE["data"] is None or now - _CREDITS_CACHE["at"] > 60:
            _CREDITS_CACHE["data"] = _provider_credits()
            _CREDITS_CACHE["at"] = now
        return jsonify({"providers": _CREDITS_CACHE["data"],
                        "updated_at": int(_CREDITS_CACHE["at"])})

@app.post("/api/open_output")
def api_open_output():
    """Mở thư mục output trong Explorer (chạy trên máy của người dùng)."""
    if not is_admin(): return forbidden()
    try:
        os.makedirs(E.OUT_DIR, exist_ok=True)
        os.startfile(E.OUT_DIR)      # Windows: mở Explorer
        return jsonify({"ok": True, "path": E.OUT_DIR})
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)[:150], "path": E.OUT_DIR})

@app.get("/api/settings")
def api_get_settings():
    if not is_admin(): return forbidden()
    s = load_settings()
    s["tts_provider"] = str(E.CONFIG.get("tts_provider", "vivibe")).lower()
    s["vivibe_key_set"] = bool(os.environ.get("VIVIBE_API_KEY") or E.CONFIG.get("vivibe_api_key"))
    s["genmax_key_set"] = bool(os.environ.get("GENMAX_API_KEY") or E.CONFIG.get("genmax_api_key"))
    s["douyin_cookie_set"] = bool(os.environ.get("DOUYIN_COOKIE") or E.CONFIG.get("douyin_cookie"))
    s["bilibili_cookie_set"] = bool(os.environ.get("BILIBILI_COOKIE") or E.CONFIG.get("bilibili_cookie"))
    s["facebook_pages"] = [{"id": p["id"], "name": p.get("name") or p["id"]}
                           for p in facebook_pages()]
    s["facebook_key_set"] = bool(s["facebook_pages"])
    return jsonify(s)

@app.post("/api/settings")
def api_set_settings():
    if not is_admin(): return forbidden()
    data = request.get_json(force=True) or {}
    saved_key = save_vivibe_key(data.pop("vivibe_api_key", ""))
    saved_genmax_key = save_genmax_key(data.pop("genmax_api_key", ""))
    saved_douyin_cookie = save_douyin_cookie(data.pop("douyin_cookie", ""))
    saved_bilibili_cookie = save_bilibili_cookie(data.pop("bilibili_cookie", ""))
    if saved_key or saved_genmax_key:
        with _CREDITS_LOCK:
            _CREDITS_CACHE.update({"at": 0.0, "data": None})
    fb_requested = "facebook_page_id" in data or "facebook_page_access_token" in data
    fb_configured = save_facebook_config(data.pop("facebook_page_id", ""),
                                         data.pop("facebook_page_access_token", ""),
                                         data.pop("facebook_page_name", "")) if fb_requested else bool(facebook_pages())
    result = save_settings(data)
    result["vivibe_key_set"] = saved_key or bool(os.environ.get("VIVIBE_API_KEY"))
    result["genmax_key_set"] = saved_genmax_key or bool(os.environ.get("GENMAX_API_KEY"))
    result["douyin_cookie_set"] = saved_douyin_cookie or bool(os.environ.get("DOUYIN_COOKIE") or E.CONFIG.get("douyin_cookie"))
    result["bilibili_cookie_set"] = saved_bilibili_cookie or bool(os.environ.get("BILIBILI_COOKIE") or E.CONFIG.get("bilibili_cookie"))
    result["facebook_key_set"] = fb_configured
    return jsonify(result)

@app.post("/api/facebook/test")
def api_facebook_test():
    if not is_admin(): return forbidden()
    data = request.get_json(silent=True) or {}
    page_id = str(data.get("page_id") or "").strip()
    page = next((p for p in facebook_pages() if str(p["id"]) == page_id), None)
    if not page:
        return jsonify({"ok": False, "err": "Không tìm thấy cấu hình Page"}), 400
    token = page["access_token"]
    try:
        import httpx
        response = httpx.get(f"https://graph.facebook.com/{page_id}",
                             params={"fields": "id,name", "access_token": token}, timeout=20)
        data = response.json()
        if response.is_error or data.get("error"):
            raise ValueError((data.get("error") or {}).get("message") or "Facebook từ chối kết nối")
        return jsonify({"ok": True, "page_id": data.get("id"), "page_name": data.get("name")})
    except Exception as exc:
        return jsonify({"ok": False, "err": str(exc)[:180]}), 400

@app.delete("/api/facebook/pages/<page_id>")
def api_facebook_delete_page(page_id):
    if not is_admin(): return forbidden()
    delete_facebook_page(page_id)
    return jsonify({"ok": True})

@app.get("/api/facebook/posts")
def api_facebook_posts():
    if not is_admin(): return forbidden()
    page_id = str(request.args.get("page_id") or "").strip()
    page = next((p for p in facebook_pages() if str(p["id"]) == page_id), None)
    if not page:
        return jsonify({"ok": False, "err": "Không tìm thấy cấu hình Page"}), 400
    params = {"access_token": page["access_token"], "limit": 20,
              "fields": "id,message,created_time,permalink_url,full_picture,status_type,shares,attachments.limit(1){media_type,target{id}}"}
    after = str(request.args.get("after") or "").strip()
    if after: params["after"] = after
    try:
        import httpx
        response = httpx.get(f"https://graph.facebook.com/{page_id}/posts",
                             params=params, timeout=30)
        result = response.json()
        if response.is_error or result.get("error"):
            raise ValueError((result.get("error") or {}).get("message") or "Không đọc được bài viết")
        posts = []
        for item in result.get("data", []):
            post = {k: item.get(k) for k in ("id", "message", "created_time", "permalink_url",
                                              "full_picture", "status_type")}
            post["marketing"] = {
                "shares": int((item.get("shares") or {}).get("count") or 0),
            }
            attachment = ((item.get("attachments") or {}).get("data") or [{}])[0]
            if attachment.get("media_type") in ("video", "video_inline"):
                post["_video_id"] = str((attachment.get("target") or {}).get("id") or "")
            posts.append(post)

        # Batch keeps the post list usable even when read_insights is not granted.
        if posts:
            metrics = ("post_media_view,post_clicks,post_video_views,post_video_view_time,"
                       "post_video_avg_time_watched,post_video_views_60s_excludes_shorter,"
                       "post_reactions_by_type_total,post_activity_by_action_type")
            batch = [{"method": "GET", "relative_url": f"{p['id']}/insights?metric={metrics}"}
                     for p in posts]
            try:
                insights_response = httpx.post("https://graph.facebook.com/",
                                               data={"access_token": page["access_token"],
                                                     "batch": json.dumps(batch)}, timeout=45)
                batch_result = insights_response.json()
                if isinstance(batch_result, list):
                    key_map = {"post_media_view": "views", "post_clicks": "clicks",
                               "post_video_views": "views_3s",
                               "post_video_view_time": "watch_time_ms",
                               "post_video_avg_time_watched": "avg_watch_time_ms",
                               "post_video_views_60s_excludes_shorter": "views_1m"}
                    for post, entry in zip(posts, batch_result):
                        if int(entry.get("code") or 0) != 200: continue
                        body = json.loads(entry.get("body") or "{}")
                        for metric in body.get("data", []):
                            if metric.get("period") not in (None, "lifetime"): continue
                            values = metric.get("values") or []
                            value = values[-1].get("value", 0) if values else 0
                            name = metric.get("name")
                            if isinstance(value, (int, float)) and name in key_map:
                                post["marketing"][key_map[name]] = int(value)
                            elif isinstance(value, dict) and name == "post_reactions_by_type_total":
                                post["marketing"]["reactions"] = sum(int(v or 0) for v in value.values())
                            elif isinstance(value, dict) and name == "post_activity_by_action_type":
                                for source, target in (("comment", "comments"), ("share", "shares")):
                                    if source in value:
                                        post["marketing"][target] = int(value[source] or 0)
                                post["marketing"]["interactions"] = sum(
                                    int(v or 0) for v in value.values() if isinstance(v, (int, float)))
            except Exception:
                pass

            videos = [p for p in posts if p.get("_video_id")]
            if videos:
                video_metrics = ("blue_reels_play_count,fb_reels_total_plays,fb_reels_replay_count,"
                                 "post_video_view_time,post_video_avg_time_watched,post_video_followers")
                video_batch = [{"method": "GET",
                                "relative_url": f"{p['_video_id']}/video_insights?metric={video_metrics}"}
                               for p in videos]
                try:
                    video_response = httpx.post("https://graph.facebook.com/",
                                                data={"access_token": page["access_token"],
                                                      "batch": json.dumps(video_batch)}, timeout=45)
                    video_result = video_response.json()
                    video_map = {"blue_reels_play_count": "initial_plays",
                                 "fb_reels_total_plays": "views",
                                 "fb_reels_replay_count": "replays",
                                 "post_video_view_time": "watch_time_ms",
                                 "post_video_avg_time_watched": "avg_watch_time_ms",
                                 "post_video_followers": "net_followers"}
                    if isinstance(video_result, list):
                        for post, entry in zip(videos, video_result):
                            if int(entry.get("code") or 0) != 200: continue
                            body = json.loads(entry.get("body") or "{}")
                            for metric in body.get("data", []):
                                if metric.get("period") not in (None, "lifetime"): continue
                                values = metric.get("values") or []
                                value = values[-1].get("value", 0) if values else 0
                                if isinstance(value, (int, float)) and metric.get("name") in video_map:
                                    post["marketing"][video_map[metric["name"]]] = int(value)
                except Exception:
                    pass

                # Meta exposes post_impressions_unique in the default video insights
                # response for some Reel objects, but rejects it when requested by name.
                # Fetching the default edge lets those videos populate the viewers column.
                default_batch = [{"method": "GET",
                                  "relative_url": f"{p['_video_id']}/video_insights?limit=100"}
                                 for p in videos]
                try:
                    default_response = httpx.post("https://graph.facebook.com/",
                                                  data={"access_token": page["access_token"],
                                                        "batch": json.dumps(default_batch)}, timeout=45)
                    default_result = default_response.json()
                    if isinstance(default_result, list):
                        for post, entry in zip(videos, default_result):
                            if int(entry.get("code") or 0) != 200: continue
                            body = json.loads(entry.get("body") or "{}")
                            for metric in body.get("data", []):
                                if metric.get("name") != "post_impressions_unique": continue
                                values = metric.get("values") or []
                                value = values[-1].get("value", 0) if values else 0
                                if isinstance(value, (int, float)):
                                    post["marketing"]["viewers"] = int(value)
                except Exception:
                    pass
        for post in posts:
            post.pop("_video_id", None)
        cursor = ((result.get("paging") or {}).get("cursors") or {}).get("after")
        return jsonify({"ok": True, "posts": posts, "after": cursor,
                        "has_more": bool((result.get("paging") or {}).get("next"))})
    except Exception as exc:
        return jsonify({"ok": False, "err": str(exc)[:220]}), 400

@app.delete("/api/facebook/posts/<path:post_id>")
def api_facebook_delete_post(post_id):
    if not is_admin(): return forbidden()
    page_id = str(request.args.get("page_id") or "").strip()
    page = next((p for p in facebook_pages() if str(p["id"]) == page_id), None)
    if not page:
        return jsonify({"ok": False, "err": "Không tìm thấy cấu hình Page"}), 400
    try:
        import httpx
        response = httpx.delete(f"https://graph.facebook.com/{post_id}",
                                params={"access_token": page["access_token"]}, timeout=30)
        result = response.json()
        if response.is_error or result.get("error") or result.get("success") is False:
            raise ValueError((result.get("error") or {}).get("message") or "Facebook không cho xoá bài")
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "err": str(exc)[:220]}), 400

@app.post("/api/facebook/publish")
def api_facebook_publish():
    if not is_admin(): return forbidden()
    data = request.get_json(silent=True) or {}
    jid, filename = str(data.get("job_id") or ""), os.path.basename(str(data.get("file") or ""))
    job = E.load_job(jid)
    if not job: return jsonify({"ok": False, "err": "Không tìm thấy job"}), 404
    output = next((item for item in job.get("outputs", []) if item.get("file") == filename), None)
    if not output: return jsonify({"ok": False, "err": "Không tìm thấy video"}), 404
    page_id = str(data.get("page_id") or "").strip()
    page = next((p for p in facebook_pages() if str(p["id"]) == page_id), None)
    if not page:
        return jsonify({"ok": False, "err": "Hãy chọn Facebook Page đã cấu hình"}), 400
    posts = output.setdefault("facebook_posts", [])
    if any(str(post.get("page_id")) == page_id for post in posts):
        return jsonify({"ok": False, "err": "Video đã được đăng lên Page này"}), 409
    token = page["access_token"]
    video_path = os.path.abspath(os.path.join(E.OUT_DIR, jid, filename))
    expected_dir = os.path.abspath(os.path.join(E.OUT_DIR, jid))
    if os.path.commonpath([video_path, expected_dir]) != expected_dir or not os.path.isfile(video_path):
        return jsonify({"ok": False, "err": "File video không tồn tại"}), 404
    try:
        import httpx
        with open(video_path, "rb") as source:
            response = httpx.post(f"https://graph-video.facebook.com/{page_id}/videos",
                                  data={"access_token": token, "description": output.get("fb_title") or ""},
                                  files={"source": (filename, source, "video/mp4")},
                                  timeout=httpx.Timeout(900, connect=30))
        result = response.json()
        if response.is_error or result.get("error") or not result.get("id"):
            raise ValueError((result.get("error") or {}).get("message") or "Facebook không nhận video")
        post = {"page_id": page_id, "page_name": page.get("name") or page_id,
                "post_id": str(result["id"]), "posted_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        posts.append(post)
        output["posted"] = True
        E.save_job(job)
        return jsonify({"ok": True, "post_id": post["post_id"], "page_name": post["page_name"],
                        "url": f"https://www.facebook.com/{post['post_id']}"})
    except Exception as exc:
        return jsonify({"ok": False, "err": str(exc)[:220]}), 400

@app.get("/api/log")
def api_log():
    if not is_admin(): return forbidden()
    p = os.path.join(E.LOG_DIR, "studio.log")
    try:
        lines = open(p, encoding="utf-8").read().splitlines()[-300:]
    except Exception:
        lines = []
    return jsonify({"lines": lines})

@app.post("/api/job/<jid>/delete")
def api_del_job(jid):
    job = E.load_job(jid)
    if not job: return jsonify({"ok": False, "err": "Không tìm thấy job"}), 404
    if not can_access_job(job): return forbidden()
    for p in (E.job_path(jid), os.path.join(E.OUT_DIR,jid), os.path.join(E.IN_DIR,jid),
              os.path.join(E.WORK_DIR,jid)):
        try:
            if os.path.isdir(p): shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p): os.remove(p)
        except Exception: pass
    return jsonify({"ok": True})

@app.get("/out/<jid>/<path:fn>")
def out_file(jid, fn):
    job = E.load_job(jid)
    if not can_access_job(job): return forbidden()
    return send_from_directory(os.path.join(E.OUT_DIR, jid), fn)

@app.get("/")
def index():
    r = Response(PAGE, mimetype="text/html")
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"  # luôn nạp bản mới (tránh cache trang cũ trên máy khác)
    r.headers["Pragma"] = "no-cache"
    return r


LOGIN_PAGE = r"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Đăng nhập · Video Studio</title><link rel="icon" type="image/png" href="/static/website-icon.png">
<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:#f3f6f8;color:#17222d;font:14px "Segoe UI",system-ui,sans-serif}.box{width:min(400px,100%);background:#fff;border:1px solid #dce3e8;border-radius:8px;padding:32px;box-shadow:0 18px 50px rgba(30,55,72,.12)}.logo{width:48px;height:48px;border-radius:8px;overflow:hidden}.logo img{width:100%;height:100%;display:block;object-fit:contain}h1{font-size:22px;margin:18px 0 5px;letter-spacing:0}.d{color:#667783;margin:0 0 22px}label{display:block;color:#536571;font-size:12px;margin:13px 0 6px;font-weight:600}input{width:100%;padding:11px 12px;border-radius:6px;border:1px solid #cbd6dc;background:#fff;color:#17222d;outline:none;font:inherit;transition:.15s}input:hover{border-color:#9fb0ba}input:focus{border-color:#087f7a;box-shadow:0 0 0 3px rgba(8,127,122,.12)}button{width:100%;margin-top:18px;padding:12px;border:0;border-radius:6px;background:#087f7a;color:#fff;font-weight:700;cursor:pointer;transition:.15s}button:hover{background:#066b67}button:focus-visible{outline:3px solid rgba(8,127,122,.22);outline-offset:2px}.err{min-height:20px;color:#c33e49;margin-top:10px;font-size:12px}</style></head>
<body><form class="box" id="login"><div class="logo"><img src="/static/website-icon.png" alt="Video Studio"></div><h1>Đăng nhập Video Studio</h1><p class="d">Nhập tài khoản để tiếp tục.</p><label>Tên đăng nhập</label><input name="username" autocomplete="username" autofocus required><label>Mật khẩu</label><input name="password" type="password" autocomplete="current-password" required><div class="err" id="err"></div><button>Đăng nhập</button></form><script>document.getElementById('login').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});const j=await r.json();if(j.ok)location.href='/';else document.getElementById('err').textContent=j.err||'Đăng nhập thất bại';b.disabled=false};</script></body></html>"""


PAGE = r"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Video Studio</title><link rel="icon" type="image/png" href="/static/website-icon.png">
<style>
:root{
 --bg:#0c0f16; --side:#0f131c; --side2:#141924; --card:#161b26; --card2:#1b212e;
 --line:#242b39; --line2:#2e3646; --fg:#eaf0f8; --mut:#8a95a7; --mut2:#657084;
 --acc:#5b8cff; --acc2:#8b6cff; --ok:#37d39a; --warn:#f5b544; --err:#ff6b6b;
 --grad:linear-gradient(135deg,#8b6cff,#5b8cff);
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.55 "Segoe UI",system-ui,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:#28303f;border-radius:8px}
::-webkit-scrollbar-track{background:transparent}
a{color:var(--acc);text-decoration:none}
.app{display:grid;grid-template-columns:236px 1fr;height:100vh}

/* SIDEBAR */
.side{background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;padding:14px 12px}
.brand{display:flex;gap:11px;align-items:center;padding:8px 8px 16px}
.logo{width:38px;height:38px;border-radius:11px;background:var(--grad);display:grid;place-items:center;
 box-shadow:0 6px 18px -6px #5b8cff88;flex:none}
.logo svg{width:20px;height:20px}
.logo img{width:100%;height:100%;display:block;object-fit:contain;border-radius:8px}
.brand .t{font-weight:700;font-size:15px;line-height:1.1}
.brand .s{color:var(--mut2);font-size:11px;margin-top:2px}
nav{display:flex;flex-direction:column;gap:3px;margin-top:6px}
.nav{display:flex;gap:11px;align-items:center;padding:9px 11px;border-radius:9px;color:var(--mut);
 cursor:pointer;font-weight:500;font-size:13.5px;border:1px solid transparent;transition:.12s}
.nav svg{width:18px;height:18px;flex:none;opacity:.85}
.nav:hover{background:var(--side2);color:var(--fg)}
.nav.on{background:linear-gradient(90deg,#5b8cff1f,#8b6cff10);color:#fff;border-color:#5b8cff33}
.nav.on svg{opacity:1}
.nav .badge{margin-left:auto;background:#5b8cff2a;color:#a9c2ff;font-size:11px;padding:1px 7px;border-radius:20px;font-weight:600}
.side .foot{margin-top:auto;padding:10px 8px;color:var(--mut2);font-size:11px;border-top:1px solid var(--line)}
.account{display:flex;align-items:center;gap:8px;margin-bottom:8px}.account b{color:var(--fg);font-size:12px}.linkbtn{margin-left:auto;border:0;background:none;color:#9fbaff;cursor:pointer;font-size:11px;padding:2px}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:6px;background:var(--ok)}

/* MAIN */
.main{overflow:auto}
.top{position:sticky;top:0;z-index:5;background:#0c0f16e8;backdrop-filter:blur(8px);
 border-bottom:1px solid var(--line);padding:15px 26px;display:flex;align-items:center;gap:12px}
.top h1{margin:0;font-size:18px;font-weight:650}
.top .hint{color:var(--mut2);font-size:12.5px;margin-left:2px}
.content{padding:22px 26px 60px;max-width:1120px}

/* cards */
.grid{display:grid;gap:16px}
.cols2{grid-template-columns:1fr 1fr}.cols3{grid-template-columns:repeat(3,1fr)}.cols4{grid-template-columns:repeat(4,1fr)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}
.card h2{margin:0 0 3px;font-size:15px}.card .d{color:var(--mut);font-size:12.5px;margin:0 0 14px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;position:relative;overflow:hidden}
.stat .k{color:var(--mut);font-size:12px;font-weight:500}
.stat .v{font-size:30px;font-weight:750;letter-spacing:-.5px;margin-top:6px}
.stat .bar{height:4px;border-radius:4px;background:var(--grad);margin-top:12px;opacity:.9}
.stat.mini .v{font-size:22px}
.credits-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:18px 0 8px}.credits-head h2{font-size:14px;margin:0}.credit-refresh{border:0;background:transparent;color:var(--acc2);font-size:12px;font-weight:650;cursor:pointer;padding:5px}.credit-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.credit-card{display:flex;align-items:center;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:13px 15px;box-shadow:0 8px 24px rgba(31,55,70,.05)}.credit-provider{font-weight:700;font-size:13px}.credit-meta{color:var(--mut2);font-size:11px;margin-top:3px}.credit-value{margin-left:auto;text-align:right;font-size:22px;font-weight:750;white-space:nowrap}.credit-unit{display:block;color:var(--mut);font-size:10px;font-weight:500}.credit-dot{width:9px;height:9px;border-radius:50%;background:var(--ok);flex:none}.credit-dot.err{background:var(--err)}
.fb-overview-head{display:flex;align-items:end;justify-content:space-between;gap:12px;margin:20px 0 8px}.fb-overview-head h2{font-size:14px;margin:0}.fb-overview-controls{display:flex;align-items:center;gap:8px}.fb-overview-controls select{min-width:190px;padding:7px 30px 7px 9px;font-size:12px}.fb-overview-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.fb-overview-card{appearance:none;text-align:left;color:var(--fg);font:inherit;background:#fff;border:1px solid var(--line);border-radius:8px;padding:15px;box-shadow:0 8px 24px rgba(31,55,70,.05);cursor:pointer;transition:.15s}.fb-overview-card:hover{border-color:#9eb9b6;background:#fbfdfd}.fb-overview-card.on{border-color:var(--acc);box-shadow:0 0 0 3px var(--focus)}.fb-overview-card .k{font-size:12px;font-weight:650;color:#405966}.fb-overview-card .v{font-size:26px;font-weight:750;margin:7px 0}.fb-overview-card .d{font-size:10px;line-height:1.45;margin:0;color:var(--mut2)}.fb-overview-meta{color:var(--mut2);font-size:10px;margin-top:7px}.fb-chart{margin-top:12px;background:#fff;border:1px solid var(--line);border-radius:8px;padding:14px 14px 10px}.fb-chart-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:8px}.fb-chart-head b{font-size:13px}.fb-chart-head span{font-size:10px;color:var(--mut2)}.fb-chart-canvas{display:block;width:100%;height:260px;touch-action:pan-y}.fb-chart-tooltip{position:fixed;display:none;z-index:40;pointer-events:none;background:#17242d;color:#fff;border-radius:5px;padding:6px 8px;font-size:10px;box-shadow:0 6px 18px rgba(0,0,0,.18)}

/* form */
label{display:block;font-size:12px;color:var(--mut);margin:12px 0 5px;font-weight:500}
textarea,input[type=text],select{width:100%;background:#0d121b;border:1px solid var(--line2);color:var(--fg);
 border-radius:9px;padding:9px 11px;font:13px inherit;outline:none;transition:.12s}
textarea:focus,input:focus,select:focus{border-color:var(--acc);background:#0e1420}
textarea{min-height:78px;resize:vertical}
select{appearance:none;background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='none' stroke='%238a95a7' stroke-width='2'><path d='M2 4l4 4 4-4'/></svg>");
 background-repeat:no-repeat;background-position:right 11px center;padding-right:30px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:11px}.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:11px}.row4{display:grid;grid-template-columns:repeat(4,1fr);gap:11px}
.drop{border:1.5px dashed var(--line2);border-radius:10px;padding:14px;text-align:center;color:var(--mut);
 font-size:12.5px;cursor:pointer;transition:.12s;background:#0d121b}
.drop:hover{border-color:var(--acc);color:var(--fg)}
.drop.has{border-color:var(--ok);color:var(--ok)}
input[type=file]{display:none}
.btn{background:var(--grad);color:#fff;border:0;border-radius:10px;padding:11px 20px;font-weight:650;
 cursor:pointer;font-size:14px;margin-top:16px;box-shadow:0 8px 20px -8px #5b8cff88}
.btn:disabled{opacity:.55;cursor:default;box-shadow:none}
.btn.ghost{background:#1a2130;border:1px solid var(--line2);box-shadow:none;color:var(--fg);padding:7px 13px;font-size:12.5px;margin:0}
.btn.sm{padding:6px 12px;font-size:12px;margin:0}

/* job cards */
.job{border:1px solid var(--line);border-radius:12px;padding:14px;background:var(--card2);margin-bottom:12px}
.job .h{display:flex;align-items:center;gap:10px}
.kind{font-weight:650;font-size:13.5px}
.badge2{font-size:11px;padding:2px 10px;border-radius:20px;font-weight:600}
.b-queued{background:#2a3450;color:#a9c0ff}.b-running{background:#3a2f12;color:var(--warn)}
.b-done{background:#123a2c;color:var(--ok)}.b-error{background:#3a1616;color:var(--err)}
.meta{color:var(--mut2);font-size:11.5px}
.log{font:11px ui-monospace,Consolas,monospace;color:#7fdca8;background:#0a0e14;border:1px solid var(--line);
 border-radius:8px;padding:9px 11px;margin-top:10px;max-height:150px;overflow:auto;white-space:pre-wrap}
.outs{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:11px;margin-top:11px}
.out{border:1px solid var(--line);border-radius:9px;overflow:hidden;background:#0a0e14}
.out video{width:100%;aspect-ratio:9/16;object-fit:cover;display:block;background:#000}
.out .m{padding:7px 9px;font-size:11px;color:var(--mut)}
.tag{display:inline-block;background:#212a3a;color:#9fb3d4;border-radius:5px;padding:1px 7px;margin:2px 3px 0 0;font-size:10px}
.dl{display:inline-flex;gap:4px;align-items:center;margin-top:5px;font-size:11px}
.empty{color:var(--mut2);font-size:13px;padding:20px;text-align:center;border:1px dashed var(--line);border-radius:12px}
.spin{width:13px;height:13px;border:2px solid #ffffff33;border-top-color:#fff;border-radius:50%;display:inline-block;animation:sp .7s linear infinite;vertical-align:-2px}
@keyframes sp{to{transform:rotate(360deg)}}
.view{display:none}.view.on{display:block}
.toast{position:fixed;right:22px;bottom:22px;background:#16324a;border:1px solid #2f6ea8;color:#dcefff;
 padding:12px 16px;border-radius:10px;font-size:13px;opacity:0;transform:translateY(8px);transition:.2s;z-index:50;box-shadow:0 10px 30px -10px #000}
.toast.show{opacity:1;transform:none}
.seg{display:inline-flex;background:#141924;border:1px solid var(--line);border-radius:9px;padding:3px;gap:2px}
.seg button{background:transparent;border:0;color:var(--mut);padding:6px 13px;border-radius:7px;cursor:pointer;font-size:12.5px;font-weight:500}
.seg button.on{background:#2a3550;color:#fff}
.results-toolbar{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}.results-toolbar .post-filter{margin-left:auto}
.kv{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--line);font-size:13px}
.kv:last-child{border:0}.kv span{color:var(--mut)}
.fbox{border:1px solid var(--line2);border-radius:10px;padding:10px 12px 14px;margin-top:12px;background:#0f141d}
.fbox-t{font-size:12px;color:#a9c2ff;font-weight:600;margin-bottom:2px}
/* stepper + section (gift-friendly, trực quan) */
.stepper{display:flex;gap:8px;margin:2px 0 18px}
.stp{flex:1;display:flex;gap:10px;align-items:center;background:#0f141d;border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.stp .n{width:26px;height:26px;border-radius:50%;background:var(--grad);color:#fff;display:grid;place-items:center;font-weight:700;font-size:13px;flex:none}
.stp .tt{font-size:12.5px;font-weight:600}.stp .ds{font-size:11px;color:var(--mut2)}
.sec{margin-top:18px}
.sec-h{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:650;color:#cfe0ff;margin-bottom:8px}
.sec-h .b{width:22px;height:22px;border-radius:6px;background:#22304a;color:#a9c2ff;display:grid;place-items:center;font-size:12px;font-weight:700}
.hero-h{font-size:20px;font-weight:700;margin:0 0 2px}
.hero-d{color:var(--mut);font-size:13px;margin:0 0 16px}
.pbar{height:6px;border-radius:6px;background:#1a2130;overflow:hidden;margin:9px 0 4px}
.pbar > i{display:block;height:100%;background:var(--grad);border-radius:6px;transition:width .4s}
.big-btn{font-size:15px;padding:13px 22px;width:100%;justify-content:center;display:flex;align-items:center;gap:8px}

/* Light workspace theme */
:root{--bg:#f3f6f8;--side:#fff;--side2:#edf5f4;--card:#fff;--card2:#fff;--line:#dce4e8;--line2:#c9d5db;--fg:#17242d;--mut:#60727d;--mut2:#7e8d96;--acc:#087f7a;--acc2:#1166a5;--ok:#14805e;--warn:#a76508;--err:#c33e49;--soft:#e7f3f1;--focus:rgba(8,127,122,.14);--grad:#087f7a}
button,input,textarea,select{font:inherit}button:focus-visible,a:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
::-webkit-scrollbar-thumb{background:#bdc9cf;border:2px solid var(--bg)}a{color:var(--acc2)}a:hover{text-decoration:underline}
.side{box-shadow:2px 0 12px rgba(35,55,67,.03);z-index:10}.logo{border-radius:8px;background:var(--acc);box-shadow:none}.nav{border-radius:6px;padding:10px 11px}.nav:hover{background:#f4f7f8;color:var(--fg)}.nav.on{background:var(--soft);color:#066b67;border-color:#cce4e1;font-weight:650}.nav .badge{background:#d9ebe8;color:#066b67}.linkbtn{color:var(--acc2)}
.top{background:rgba(243,246,248,.94);backdrop-filter:blur(10px);padding:17px 28px;align-items:baseline}.content{padding:24px 28px 64px;max-width:1180px;margin:0 auto}
.card,.stat,.job{border-radius:8px;box-shadow:0 8px 24px rgba(31,55,70,.07)}.card{padding:20px}.stat .v{letter-spacing:0}.stat .bar,.pbar>i{background:var(--acc)}
textarea,input[type=text],input[type=password],select{background:#fff;border-color:var(--line2);color:var(--fg);border-radius:6px;padding:10px 11px}textarea:hover,input:hover,select:hover{border-color:#9eafb8}textarea:focus,input:focus,select:focus{border-color:var(--acc);background:#fff;box-shadow:0 0 0 3px var(--focus)}
.drop{border-color:#9db1bb;border-radius:6px;padding:17px;background:#f8fafb}.drop:hover{border-color:var(--acc);color:#066b67;background:var(--soft)}
.btn{background:var(--acc);border:1px solid var(--acc);border-radius:6px;box-shadow:none;transition:.15s}.btn:hover{background:#066b67;border-color:#066b67;text-decoration:none}.btn.ghost{background:#fff;border-color:var(--line2);color:var(--fg)}.btn.ghost:hover{background:#f3f7f8;border-color:#9eafb8}
.b-queued{background:#e6eef8;color:#315f94}.b-running{background:#fff0d9;color:var(--warn)}.b-done{background:#def2e9;color:var(--ok)}.b-error{background:#fbe4e6;color:var(--err)}
.log{color:#d9f8e8;background:#17242d;border-color:#243640}.out{border-radius:6px;background:#fff}.tag{background:#edf2f4;color:#536a78}.toast{background:#17333a;border-color:#29515a;color:#efffff;border-radius:6px;box-shadow:0 10px 30px rgba(22,45,55,.2)}
.seg{background:#e8eef1;border-color:#d4dee3;border-radius:7px}.seg button{border-radius:5px}.seg button.on{background:#fff;color:var(--fg);box-shadow:0 1px 4px rgba(36,55,66,.12);font-weight:650}
.fbox{border-color:var(--line);border-radius:6px;background:#f7f9fa;padding:12px 14px 16px}.fbox-t{color:#335e72}.stp{background:#f7f9fa;border-radius:6px}.stp .n{background:var(--acc)}.sec-h{color:#254957}.sec-h .b{border-radius:5px;background:#dcefeb;color:#066b67}.pbar{background:#e3eaed}
.fb-post{margin-top:8px;padding:9px;background:#f7f9fa;border:1px solid var(--line);border-radius:5px}.fb-post-label{display:flex;align-items:center;justify-content:space-between;gap:8px;color:#335e72;font-size:10px;font-weight:700;text-transform:uppercase}.fb-post-text{margin-top:5px;color:var(--fg);font-size:11px;line-height:1.45;white-space:pre-wrap}.copy-title{border:0;background:transparent;color:var(--acc2);font-size:10px;font-weight:650;cursor:pointer;padding:2px 0}.copy-title:hover{text-decoration:underline}
.fb-publish{display:flex;align-items:center;gap:7px;margin-top:8px}.fb-publish-btn{border:1px solid #1877f2;background:#1877f2;color:#fff;border-radius:5px;padding:6px 9px;font-size:11px;font-weight:650;cursor:pointer}.fb-publish-btn:hover{background:#1265d6}.fb-publish-btn:disabled{opacity:.55;cursor:default}.fb-published{color:var(--ok);font-size:11px;font-weight:650}.fb-published a{color:var(--ok)}
.fb-page-select{min-width:0;flex:1;padding:6px 26px 6px 8px;font-size:11px}.fb-post-list{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}.fb-post-chip{background:#e8f3ed;color:var(--ok);border-radius:4px;padding:3px 6px;font-size:10px;font-weight:650}.fb-page-row{display:flex;align-items:center;gap:8px;padding:8px 0;border-top:1px solid var(--line);font-size:12px}.fb-page-row span{min-width:0;flex:1}.fb-page-row small{display:block;color:var(--mut2)}
.page-posts-toolbar{display:flex;align-items:end;gap:9px;margin-bottom:14px}.page-posts-toolbar>div{min-width:220px}.page-posts-toolbar label{margin-top:0}.page-post-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.page-post{display:grid;grid-template-columns:112px minmax(0,1fr);background:#fff;border:1px solid var(--line);border-radius:8px;overflow:hidden}.page-post-media{width:112px;height:112px;object-fit:cover;background:#edf2f4}.page-post-media.empty{border:0;border-radius:0;padding:0;display:grid;place-items:center}.page-post-body{padding:11px;min-width:0}.page-post-message{font-size:12px;line-height:1.45;white-space:pre-wrap;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}.page-post-meta{color:var(--mut2);font-size:10px;margin-top:6px}.page-post-actions{display:flex;gap:7px;margin-top:8px}
.studio-segments{margin-top:12px;overflow-x:auto}.studio-segment{display:grid;grid-template-columns:120px 170px 72px 72px minmax(180px,1fr) minmax(180px,1fr);gap:8px;align-items:start;min-width:1050px;padding:9px;border:1px solid var(--line);border-radius:7px;margin-top:7px;background:#fff}.studio-segment:hover{border-color:#9fc9c5}.studio-segment input,.studio-segment select{padding:7px}.studio-segment textarea{min-height:58px;padding:7px}.studio-segment-head{display:grid;grid-template-columns:120px 170px 72px 72px minmax(180px,1fr) minmax(180px,1fr);gap:8px;min-width:1050px;color:var(--mut);font-size:11px;font-weight:650;padding:0 9px}
.studio-video-wrap{position:relative;width:100%;background:#111;border-radius:8px;overflow:hidden}.studio-video-wrap video{display:block;width:100%;max-height:430px;background:#111}.studio-region-layer{position:absolute;inset:0;pointer-events:none}.studio-region-layer.picking{pointer-events:auto;cursor:crosshair;background:rgba(0,0,0,.08)}.studio-region-box{position:absolute;border:2px solid #ff334f;background:rgba(255,51,79,.16);box-shadow:0 0 0 9999px rgba(0,0,0,.12);pointer-events:none}.studio-region-box:after{content:"Vùng sub cũ";position:absolute;left:4px;top:4px;padding:2px 5px;border-radius:3px;background:#ff334f;color:#fff;font-size:10px;font-weight:700}.studio-region-actions{display:flex;align-items:end;gap:8px;margin-top:10px}.studio-region-actions>div{min-width:210px;flex:1}.studio-region-values{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-top:8px}.studio-region-values input{padding:7px}
.studio-template-layout{display:grid;grid-template-columns:180px minmax(220px,1fr);gap:14px;align-items:start;margin-top:10px}.studio-template-frame{position:relative;width:520px;max-width:100%;aspect-ratio:16/9;margin:auto;background:linear-gradient(145deg,#27333b,#10161a);border:1px solid #71818b;border-radius:8px;overflow:hidden;touch-action:none;cursor:crosshair}.studio-template-frame:before{content:"KHUNG HÌNH MẪU";position:absolute;inset:0;display:grid;place-items:center;color:rgba(255,255,255,.28);font-size:13px;font-weight:750;letter-spacing:.16em}.studio-template-box{position:absolute;border:2px solid #ff334f;background:rgba(255,51,79,.2);box-shadow:0 0 0 9999px rgba(0,0,0,.15);pointer-events:none}.studio-template-box:after{content:"SUB CŨ";position:absolute;left:4px;top:4px;background:#ff334f;color:#fff;border-radius:3px;padding:2px 5px;font-size:9px;font-weight:750}
.post-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:5px;margin-top:8px}.post-metric{background:#f3f7f8;border-radius:4px;padding:5px 6px;min-width:0}.post-metric b{display:block;font-size:12px}.post-metric span{display:block;color:var(--mut2);font-size:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.post-engagement{color:var(--acc);font-size:10px;font-weight:650;margin-top:6px}
.content-table-wrap{overflow:auto;border:1px solid var(--line);border-radius:8px;background:#fff;max-height:68vh}.content-table{width:100%;min-width:1540px;border-collapse:separate;border-spacing:0;font-size:11px}.content-table th{position:sticky;top:0;z-index:2;background:#f3f6f8;color:#3d5360;font-weight:700;text-align:left;padding:10px 9px;border-bottom:1px solid var(--line);white-space:normal;line-height:1.2}.content-table td{padding:9px;border-bottom:1px solid #e8edef;vertical-align:middle;color:var(--fg)}.content-table tbody tr:hover td{background:#f8fbfb}.content-table th:first-child,.content-table td:first-child{position:sticky;left:0;z-index:1;background:#fff;min-width:300px}.content-table th:first-child{z-index:3;background:#f3f6f8}.content-preview{display:grid;grid-template-columns:60px minmax(0,1fr);gap:9px;align-items:center}.content-thumb{width:60px;height:60px;border-radius:6px;object-fit:cover;background:#edf2f4}.content-title{max-width:220px;font-size:11px;font-weight:650;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.content-date{color:var(--mut2);font-size:9px;margin-top:3px}.status-ok{display:inline-flex;align-items:center;gap:5px;border:1px solid #b9d9cb;border-radius:5px;padding:4px 7px;white-space:nowrap}.status-ok:before{content:"";width:6px;height:6px;border-radius:50%;background:var(--ok)}.metric-cell{font-variant-numeric:tabular-nums;white-space:nowrap}.table-actions{display:flex;gap:5px;white-space:nowrap}
.movie-modes{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}.movie-mode{display:flex;align-items:flex-start;gap:10px;margin:0;padding:13px;border:1px solid var(--line2);border-radius:7px;background:#fff;cursor:pointer}.movie-mode:has(input:checked){border-color:var(--acc);background:var(--soft);box-shadow:0 0 0 2px var(--focus)}.movie-mode input{width:auto;margin:3px 0 0;accent-color:var(--acc)}.movie-mode b{display:block}.movie-mode span{display:block;color:var(--mut);font-size:11px;font-weight:400;margin-top:2px}
.video-note{margin-top:8px;padding:9px;background:#f7f9fa;border:1px solid var(--line);border-radius:5px}.video-note-head{display:flex;align-items:center;justify-content:space-between;gap:8px;color:#335e72;font-size:10px;font-weight:700;text-transform:uppercase}.video-note-actions{display:flex;align-items:center;gap:9px}.video-note-input{min-height:42px;max-height:100px;margin-top:5px;padding:7px 8px;font-size:11px;line-height:1.4}.save-video-note,.copy-video-note{border:0;background:transparent;color:var(--acc2);font-size:10px;font-weight:650;cursor:pointer;padding:2px 0}.save-video-note:hover,.copy-video-note:hover{text-decoration:underline}.save-video-note:disabled{opacity:.5;cursor:default}.posted-check{display:flex;align-items:center;gap:6px;margin:7px 0 0;color:#335e72;font-size:11px;font-weight:650;cursor:pointer}.posted-check input{width:15px;height:15px;margin:0;accent-color:var(--acc);cursor:pointer}.posted-check.saving{opacity:.55}
.discover-actions{display:flex;gap:8px;align-items:end;flex-wrap:wrap}.discover-actions .btn{margin:0}.discover-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(245px,1fr));gap:12px;margin-top:15px}.discover-card{border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fff}.discover-card img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:#edf2f4}.discover-card .dc-body{padding:11px}.discover-card h3{font-size:13px;line-height:1.35;margin:0 0 6px;height:36px;overflow:hidden}.discover-meta{font-size:10px;color:var(--mut);display:flex;gap:7px;flex-wrap:wrap}.discover-score{color:var(--acc);font-weight:750}.discover-card .dc-foot{display:flex;align-items:center;gap:7px;margin-top:10px}.discover-card .dc-foot input{width:16px;height:16px}.discover-empty{padding:24px;text-align:center;color:var(--mut);border:1px dashed var(--line2);border-radius:8px;margin-top:14px}
.outs{grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:10px}.out{display:grid;grid-template-columns:160px minmax(0,1fr);align-items:start}.out video{width:160px;aspect-ratio:9/16}.out .m{padding:11px 12px;min-width:0}.out-title{display:flex;justify-content:space-between;gap:8px;align-items:flex-start;margin-bottom:7px;color:var(--fg);font-size:12px;font-weight:650}.out-duration{color:var(--mut2);font-weight:500;white-space:nowrap}.out-tags{margin-bottom:6px}.dl{margin-top:2px;font-weight:650}
@media(max-width:900px){.cols4{grid-template-columns:repeat(2,1fr)}.row3{grid-template-columns:1fr 1fr}.studio-template-layout{grid-template-columns:1fr}.stepper{display:grid;grid-template-columns:1fr}.content{padding:20px 20px 96px}.top{padding:15px 20px}}
@media(max-width:680px){body{font-size:13px}.app{display:block;height:auto;min-height:100vh}.side{position:fixed;left:0;right:0;bottom:0;height:68px;border:0;border-top:1px solid var(--line);padding:5px 6px;background:rgba(255,255,255,.97);backdrop-filter:blur(12px)}.brand,.side .foot{display:none}nav{height:100%;margin:0;display:flex;flex-direction:row;justify-content:flex-start;gap:2px;overflow-x:auto}.nav{position:relative;min-width:64px;flex:0 0 64px;flex-direction:column;justify-content:center;gap:2px;padding:4px 2px;border:0;font-size:10px;line-height:1.1;text-align:center}.nav svg{width:20px;height:20px}.nav.on{border:0}.nav .badge{position:absolute;top:2px;right:15%;margin:0;padding:0 5px}.main{overflow:visible}.top{padding:13px 16px;align-items:center}.top h1{font-size:17px}.top .hint{display:none}.content{padding:16px 12px 94px}.cols2,.cols3,.cols4,.row2,.row3,.row4{grid-template-columns:1fr}.grid{gap:12px}.card{padding:16px}.stepper{gap:6px;margin-bottom:14px}.stp{padding:8px 10px}.stp .ds{display:none}.job .h{align-items:flex-start;flex-wrap:wrap}.job .h .meta{width:100%;margin-left:0!important}.outs{grid-template-columns:1fr}.out{grid-template-columns:120px minmax(0,1fr)}.out video{width:120px}.out .m{padding:9px}.hero-h{font-size:18px}.seg{width:100%}.seg button{flex:1;padding:7px 6px}.results-toolbar .post-filter{margin-left:0}.results-toolbar .meta{order:3;width:100%}.toast{left:12px;right:12px;bottom:82px;text-align:center}}
@media(max-width:680px){.page-posts-toolbar{align-items:stretch;flex-direction:column}.page-posts-toolbar>div{min-width:0;width:100%}.page-post-grid{grid-template-columns:1fr}.page-post{grid-template-columns:96px minmax(0,1fr)}.page-post-media{width:96px;height:110px}.post-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.content-table-wrap{max-height:none}.content-table th:first-child,.content-table td:first-child{min-width:250px}.content-title{max-width:160px}}
@media(max-width:680px){.fb-overview-head{align-items:stretch;flex-direction:column}.fb-overview-controls{width:100%;flex-wrap:wrap}.fb-overview-controls select{flex:1;min-width:120px}.fb-overview-controls #overviewPage{flex-basis:100%}.fb-overview-controls .btn{margin-left:auto}.fb-overview-grid{grid-template-columns:1fr}.fb-chart{padding:12px 8px 8px}.fb-chart-canvas{height:220px}}
@media(max-width:460px){.credit-grid{grid-template-columns:1fr}.credit-card{padding:11px 13px}.credit-value{font-size:20px}}
@media(max-width:390px){.out{grid-template-columns:105px minmax(0,1fr)}.out video{width:105px}.nav{font-size:9.5px}.card{padding:14px}}
</style></head><body>
<div class="app">
 <aside class="side">
  <div class="brand">
   <div class="logo"><img src="/static/website-icon.png" alt="Video Studio"></div>
   <div><div class="t">Video Studio</div><div class="s">Làm video · không đăng</div></div>
  </div>
  <nav id="nav"></nav>
  <div class="foot"><div class="account"><div><b id="who">...</b><br><span id="role"></span></div><button class="linkbtn" id="logout">Đăng xuất</button></div><span class="dot"></span>Online · 1 job/lúc<br><span id="qinfo" style="color:var(--mut)"></span></div>
 </aside>

 <main class="main">
  <div class="top"><h1 id="ptitle">Tổng quan</h1><span class="hint" id="phint"></span></div>
  <div class="content">

   <!-- TỔNG QUAN -->
   <section class="view" id="v-home">
    <div id="facebookOverview" style="display:none">
     <div class="fb-overview-head"><div><h2 id="facebookOverviewTitle">Facebook · 28 ngày gần nhất</h2><div class="fb-overview-meta" id="facebookOverviewRange"></div></div><div class="fb-overview-controls"><select id="overviewPage" aria-label="Facebook Page"></select><select id="overviewDays" aria-label="Khoảng thời gian"><option value="7">7 ngày</option><option value="28" selected>28 ngày</option><option value="90">90 ngày</option></select><button class="btn ghost" id="refreshFacebookOverview" type="button">Làm mới</button></div></div>
     <div class="fb-overview-grid" id="facebookOverviewCards"></div>
     <div class="fb-chart" id="facebookChart" style="display:none"><div class="fb-chart-head"><b id="facebookChartTitle"></b><span>Theo ngày</span></div><canvas class="fb-chart-canvas" id="facebookChartCanvas"></canvas></div>
     <div class="fb-chart-tooltip" id="facebookChartTooltip"></div>
    </div>
    <div class="grid cols4" id="stats" style="margin-top:18px"></div>
    <div id="creditsArea" style="display:none">
     <div class="credits-head"><h2>Credits giọng đọc</h2><button class="credit-refresh" id="refreshCredits" type="button">Làm mới</button></div>
     <div class="credit-grid" id="credits"></div>
    </div>
    <div class="card" style="margin-top:16px"><h2>Job gần đây</h2><p class="d">6 việc mới nhất.</p><div id="recent"></div></div>
   </section>

   <!-- KHÁM PHÁ VIDEO -->
   <section class="view" id="v-discovery">
    <div class="card">
     <h2 class="hero-h">🔥 Khám phá video thịnh hành</h2>
     <p class="hero-d">Đọc bảng thịnh hành hiện tại bằng phiên đăng nhập lưu riêng trên máy này. Tool chỉ lấy metadata và link; không tự tải video hàng loạt.</p>
     <div class="row3">
      <div><label>Nền tảng</label><select id="discoverPlatform"><option value="douyin">Douyin</option><option value="bilibili">Bilibili</option><option value="tiktok">TikTok</option></select></div>
      <div><label>Chủ đề trong tiêu đề/tác giả</label><input id="discoverTopic" type="text" placeholder="Để trống để xem tất cả"></div>
      <div><label>Ngày</label><input id="discoverDate" type="date"></div>
     </div>
     <div class="discover-actions" style="margin-top:12px">
      <button class="btn ghost" id="discoverLogin" type="button">Đăng nhập nền tảng</button>
      <button class="btn" id="discoverSearch" type="button">Quét video thịnh hành</button>
      <select id="discoverLimit" style="width:auto"><option value="20">20 video</option><option value="30" selected>30 video</option><option value="50">50 video</option></select>
      <button class="btn ghost" id="discoverReview" type="button">Đưa video đã chọn sang Review</button>
      <button class="btn ghost" id="discoverRemake" type="button">Đưa video đã chọn sang Remake</button>
     </div>
     <p class="d" id="discoverStatus" style="margin:12px 0 0">Chọn nền tảng rồi đăng nhập. Sau khi đăng nhập xong, hãy đóng cửa sổ trình duyệt đó trước khi quét.</p>
     <div style="margin-top:18px"><h3>Tài khoản đã đăng nhập</h3><p class="d">Các phiên này chỉ được lưu trên máy của bạn.</p><div class="discover-accounts" id="discoverAccounts"></div></div>
     <div class="discover-grid" id="discoverResults"></div>
    </div>
   </section>

   <!-- GHÉP ÂM THANH -->
   <section class="view" id="v-soundtrack">
    <div class="card">
     <h2 class="hero-h">🎵 Ghép âm thanh từ video khác</h2>
     <p class="hero-d">Giữ hình ảnh của link video thứ nhất và thay toàn bộ âm thanh bằng audio lấy từ link thứ hai.</p>
     <form id="f5">
      <div class="sec"><div class="sec-h"><span class="b">1</span> Link video</div>
       <input type="text" name="video_url" placeholder="Facebook / TikTok / Douyin / Bilibili / YouTube..." required>
       <p class="d" style="margin-top:6px">Hình ảnh và thời lượng được lấy từ link này.</p>
      </div>
      <div class="sec"><div class="sec-h"><span class="b">2</span> Link âm thanh</div>
       <input type="text" name="audio_url" placeholder="Dán link video chứa âm thanh muốn sử dụng..." required>
       <p class="d" style="margin-top:6px">Âm thanh cũ sẽ bị thay. Video mới được cắt theo nguồn có thời lượng ngắn hơn.</p>
      </div>
      <div class="sec"><div class="sec-h"><span class="b">3</span> Tùy chỉnh video</div>
       <div class="row2">
        <div><label>Tốc độ video</label><select name="video_speed"><option value="0.75">0.75x · Chậm</option><option value="1" selected>1x · Bình thường</option><option value="1.25">1.25x · Nhanh nhẹ</option><option value="1.5">1.5x · Nhanh</option><option value="2">2x · Rất nhanh</option></select></div>
        <div><label>Lật hình</label><select name="movie_flip"><option value="none" selected>Giữ hình gốc</option><option value="hflip">Lật gương ngang</option></select></div>
       </div>
      </div>
      <div class="sec"><div class="sec-h"><span class="b">4</span> Tạo video</div>
       <button class="btn big-btn" type="submit">🎵 Ghép âm thanh vào video</button>
      </div>
     </form>
    </div>
   </section>

   <!-- REMAKE -->
   <section class="view" id="v-remake">
    <div class="card">
     <h2>Remake — làm mới 1 video</h2>
     <p class="d">Dán link (Douyin/TikTok/FB, mỗi dòng 1) hoặc kéo/chọn file. Ra nhiều biến thể khác lời + giọng + lật hình → FB thấy khác.</p>
     <form id="f1">
      <label>Link video</label>
      <textarea name="links" placeholder="https://www.douyin.com/video/...
https://www.tiktok.com/@.../video/..."></textarea>
      <label>hoặc file video</label>
      <label class="drop" id="d1">📁 Kéo thả hoặc bấm chọn file (nhiều file)<input type="file" name="files" multiple accept="video/*"></label>
       <div class="row3">
        <div><label>Niche</label><select name="niche" data-k="niche">%NICHE%</select></div>
        <div><label>Số biến thể / video</label><select name="n_variants" data-k="n_variants">%NUM%</select></div>
        <div><label>Giọng đọc</label><select name="voice" data-k="voice">%VOICE%</select></div>
       </div>
       <div class="row2"><div><label>Dịch vụ giọng đọc</label><select name="tts_provider" data-provider><option value="vivibe">Vivibe</option><option value="genmax">GenMax</option></select></div><div></div></div>
       <label>Link affiliate Shopee</label><input type="text" name="product_link" placeholder="https://s.shopee.vn/...">
      <div class="row3">
       <div><label>Kiểu hook</label><select name="hook" data-k="hook">%HOOK%</select></div>
       <div><label>Lật hình</label><select name="flip" data-k="flip">%FLIP%</select></div>
       <div><label>Kiểu nói (style)</label><select name="style" data-k="style">%STYLE%</select></div>
      </div>
      <label style="display:flex;align-items:center;gap:8px;margin:12px 0">
       <input type="checkbox" name="remove_original_audio" value="1" style="width:auto">
       Loại bỏ toàn bộ âm thanh gốc (video đầu ra chỉ giữ giọng đọc mới)
      </label>
      <button class="btn" type="submit">▶ Làm video</button>
     </form>
    </div>
   </section>

   <!-- REUP VIDEO -->
   <section class="view" id="v-reup">
    <div class="card">
     <h2 class="hero-h">🚀 Reup video · Tóm tắt + Hook 3 giây</h2>
     <p class="hero-d">Dán link hoặc tải video lên. Tool tự nghe và tóm tắt đúng nội dung, chọn các cảnh quan trọng, tạo hook TikTok 3 giây rồi lồng giọng tiếng Việt.</p>
     <form id="f4">
      <div class="sec"><div class="sec-h"><span class="b">1</span> Nguồn video</div>
       <textarea name="links" placeholder="Dán link YouTube / TikTok / Douyin / Bilibili / Google Drive / Facebook, mỗi dòng 1 link..."></textarea>
       <label class="drop" id="d4" style="margin-top:8px">📁 hoặc kéo thả / bấm chọn file video<input type="file" name="files" multiple accept="video/*"></label>
      </div>
      <div class="sec"><div class="sec-h"><span class="b">2</span> Tóm tắt và giọng đọc</div>
       <div class="row2">
        <div><label>Ngôn ngữ gốc</label><select name="source_language"><option value="">Tự nhận diện</option><option value="zh">Tiếng Trung</option><option value="en">Tiếng Anh</option><option value="ja">Tiếng Nhật</option><option value="ko">Tiếng Hàn</option><option value="th">Tiếng Thái</option><option value="id">Tiếng Indonesia</option><option value="vi">Tiếng Việt</option></select></div>
        <div><label>Độ dài video Reup</label><select name="summary_minutes"><option value="auto" selected>Tự động theo độ dài nguồn</option><option value="1">Khoảng 1 phút</option><option value="3">Khoảng 3 phút</option><option value="5">Khoảng 5 phút</option><option value="10">Khoảng 10 phút</option></select></div>
       </div>
       <div class="row2" style="margin-top:10px"><div><label>Dịch vụ giọng đọc</label><select name="tts_provider" data-provider><option value="vivibe">Vivibe</option><option value="genmax">GenMax</option></select></div><div><label>Giọng tiếng Việt</label><select name="voice">%VOICE%</select></div></div>
       <div style="margin-top:10px"><label>Xử lý phụ đề/chữ gốc</label><select name="original_subtitles"><option value="smart_bottom" selected>Xóa thông minh local — phía dưới</option><option value="smart_top">Xóa thông minh local — phía trên</option><option value="bottom">Làm mờ phía dưới</option><option value="top">Làm mờ phía trên</option><option value="keep">Giữ chữ gốc</option></select></div>
       <div class="fbox" style="margin-top:12px"><div class="fbox-t">⚡ Skill tự động</div><p class="d">Tạo 5 hook, chấm Visual · Curiosity · Gap · Clarity · Payoff, chọn hook tốt nhất và ghép cảnh mở đầu khớp nội dung.</p></div>
      </div>
      <div class="sec"><div class="sec-h"><span class="b">3</span> Tạo video Reup</div><button class="btn big-btn" type="submit">✨ Tóm tắt và tạo video Reup</button></div>
     </form>
    </div>
   </section>

   <!-- DỊCH PHIM -->
   <section class="view" id="v-translate">
    <div class="card">
     <h2 class="hero-h">🌐 Dịch phim · tạo phụ đề tiếng Việt</h2>
     <p class="hero-d">Dán link hoặc tải file video lên. Tool tự nghe lời thoại, dịch theo ngữ cảnh và xuất phụ đề SRT hoặc video đã gắn phụ đề.</p>
     <form id="f3">
      <div class="movie-modes">
       <label class="movie-mode"><input type="radio" name="task_mode" value="translate"><div><b>Dịch phim</b><span>Tạo phụ đề hoặc gắn phụ đề vào toàn bộ phim</span></div></label>
       <label class="movie-mode"><input type="radio" name="task_mode" value="summary" checked><div><b>Tóm tắt phim</b><span>Rút gọn phim và thuyết minh bằng giọng đã chọn</span></div></label>
      </div>
      <div class="sec">
       <div class="sec-h"><span class="b">1</span> Nguồn phim/video</div>
       <div style="margin-bottom:10px"><label>Tên phim <span class="meta">(không bắt buộc)</span></label><input name="movie_title" maxlength="160" placeholder="Ví dụ: Tây Du Ký · Tập 01"><p class="d" style="margin-top:6px">Nếu nhập, tên phim sẽ hiện thành header căn giữa phía trên video mới. Bỏ trống để không thêm header.</p></div>
       <textarea name="links" placeholder="Dán link YouTube / Bilibili / TikTok / Douyin / Google Drive / Facebook, mỗi dòng 1 link..."></textarea>
       <label class="drop" id="d3" style="margin-top:8px">📁 hoặc kéo thả / bấm chọn file video<input type="file" name="files" multiple accept="video/*"></label>
      </div>
      <div class="sec">
       <div class="sec-h"><span class="b">2</span> Ngôn ngữ và đầu ra</div>
       <div class="row2">
        <div><label>Ngôn ngữ gốc</label><select name="source_language"><option value="">Tự nhận diện</option><option value="zh">Tiếng Trung</option><option value="en">Tiếng Anh</option><option value="ja">Tiếng Nhật</option><option value="ko">Tiếng Hàn</option><option value="th">Tiếng Thái</option><option value="id">Tiếng Indonesia</option><option value="vi">Tiếng Việt</option></select></div>
        <div><label>Dịch sang</label><select name="target_language" id="movieTargetLanguage"><option value="vi" selected>Tiếng Việt</option><option value="en">Tiếng Anh</option><option value="zh">Tiếng Trung</option><option value="none">Không dịch · giữ nguyên lời gốc</option></select></div>
       </div>
       <div class="row2" style="margin-top:10px">
        <div><label>Tốc độ video</label><select name="video_speed"><option value="0.75">0.75x · Chậm</option><option value="1" selected>1x · Bình thường</option><option value="1.25">1.25x · Nhanh nhẹ</option><option value="1.5">1.5x · Nhanh</option><option value="2">2x · Rất nhanh</option></select></div>
        <div><label>Lật hình</label><select name="movie_flip"><option value="none" selected>Giữ hình gốc</option><option value="hflip">Lật gương ngang</option></select></div>
       </div>
       <div id="translateOptions"><label>Kết quả dịch</label><select name="translation_output" id="movieTranslationOutput"><option value="original" id="movieOriginalOutput">Giữ nguyên video gốc</option><option value="both" selected>SRT + toàn bộ phim có phụ đề</option><option value="dub">Chỉ phim lồng tiếng Việt</option><option value="dub_srt">SRT + phim lồng tiếng Việt</option><option value="all">SRT + phim phụ đề + phim lồng tiếng Việt</option><option value="srt">Chỉ file SRT</option><option value="burn">Chỉ toàn bộ phim có phụ đề</option></select></div>
       <div id="summaryOptions">
        <label>Độ dài video tóm tắt</label><select name="summary_minutes"><option value="auto" selected>Tự động · khoảng 1/6 phim (60 phút → 10 phút)</option><option value="5">Khoảng 5 phút</option><option value="10">Khoảng 10 phút</option><option value="15">Khoảng 15 phút</option><option value="20">Khoảng 20 phút</option></select>
       </div>
       <div id="movieVoiceOptions" class="row2" style="margin-top:10px"><div><label>Dịch vụ giọng đọc</label><select name="tts_provider" data-provider><option value="vivibe">Vivibe</option><option value="genmax">GenMax</option></select></div><div><label>Giọng tiếng Việt</label><select name="voice">%VOICE%</select></div></div>
       <div style="margin-top:10px"><label>Xử lý phụ đề gốc</label><select name="original_subtitles" id="movieOriginalSubtitles"><option value="smart_bottom" selected>Xóa thông minh local — phía dưới</option><option value="smart_top">Xóa thông minh local — phía trên</option><option value="bottom">Làm mờ phía dưới (nhanh, chỉ Dịch phim)</option><option value="top">Làm mờ phía trên (nhanh, chỉ Dịch phim)</option><option value="keep">Giữ sub gốc</option></select><p class="d" style="margin-top:6px">Áp dụng cho cả Dịch phim và Tóm tắt phim. Tool nhận diện nét chữ rồi tái tạo nền bằng OpenCV; nếu lỗi sẽ tự làm mờ vùng sub.</p></div>
       <div class="row3" style="margin-top:10px">
        <div><label>Cỡ chữ sub</label><input type="number" name="subtitle_font_size" min="12" max="48" value="18"></div>
        <div><label>Màu chữ sub</label><input type="color" name="subtitle_font_color" value="#ffffff" style="height:39px;padding:3px"></div>
        <div><label>Màu viền sub</label><input type="color" name="subtitle_outline_color" value="#101010" style="height:39px;padding:3px"></div>
       </div>
       <p class="d" style="margin-top:6px">Áp dụng cho sub dịch và lời thuyết minh trên video tóm tắt.</p>
      </div>
      <div class="sec"><div class="sec-h"><span class="b">3</span> Bắt đầu dịch</div>
       <button class="btn big-btn" type="submit">✨ Dịch phim — xem ở tab Kết quả</button></div>
     </form>
    </div>
   </section>

   <!-- STUDIO PHIM -->
   <section class="view" id="v-studio">
    <div class="card">
     <h2 class="hero-h">🎞️ Studio phim · duyệt trước khi render</h2>
     <p class="hero-d">Tạo bản nháp lời thoại và bản dịch trước. Bạn có thể sửa nội dung, chỉnh timestamp, xem lại từng đoạn rồi mới xác nhận render video.</p>
     <form id="f6">
      <div class="sec"><div class="sec-h"><span class="b">1</span> Nguồn phim</div>
       <label>Tên phim <span class="meta">(không bắt buộc)</span></label><input name="movie_title" maxlength="160" placeholder="Ví dụ: Người Phụ Nữ Bí Ẩn · Tập 6">
       <textarea name="links" placeholder="Dán link YouTube / Bilibili / TikTok / Douyin / Google Drive / Facebook, mỗi dòng 1 link..."></textarea>
       <label class="drop" id="d6" style="margin-top:8px">📁 hoặc kéo thả / bấm chọn file video<input type="file" name="files" multiple accept="video/*"></label>
      </div>
      <div class="sec"><div class="sec-h"><span class="b">2</span> Chế độ và đầu ra</div>
       <div class="movie-modes">
        <label class="movie-mode"><input type="radio" name="task_mode" value="translate" checked><div><b>Dịch phim</b><span>Duyệt phụ đề trước khi xuất SRT/video</span></div></label>
        <label class="movie-mode"><input type="radio" name="task_mode" value="summary"><div><b>Tóm tắt phim</b><span>Duyệt lời dịch trước khi dựng tóm tắt</span></div></label>
       </div>
       <div class="row2"><div><label>Ngôn ngữ gốc</label><select name="source_language"><option value="">Tự nhận diện</option><option value="zh">Tiếng Trung</option><option value="en">Tiếng Anh</option><option value="ja">Tiếng Nhật</option><option value="ko">Tiếng Hàn</option><option value="th">Tiếng Thái</option><option value="id">Tiếng Indonesia</option><option value="vi">Tiếng Việt</option></select></div><div><label>Dịch sang</label><select name="target_language"><option value="vi" selected>Tiếng Việt</option><option value="en">Tiếng Anh</option><option value="zh">Tiếng Trung</option><option value="none">Giữ nguyên lời gốc</option></select></div></div>
       <div class="row2" style="margin-top:10px"><div><label>Kết quả dịch</label><select name="translation_output"><option value="both" selected>SRT + phim có phụ đề</option><option value="srt">Chỉ file SRT</option><option value="burn">Chỉ phim có phụ đề</option><option value="dub">Phim lồng tiếng Việt</option><option value="dub_srt">SRT + phim lồng tiếng</option><option value="all">Tất cả đầu ra</option></select></div><div><label>Độ dài tóm tắt</label><select name="summary_minutes"><option value="auto">Tự động</option><option value="5">Khoảng 5 phút</option><option value="10">Khoảng 10 phút</option><option value="15">Khoảng 15 phút</option><option value="20">Khoảng 20 phút</option></select></div></div>
       <div class="row2" style="margin-top:10px"><div><label>Dịch vụ giọng đọc</label><select name="tts_provider" data-provider><option value="vivibe">Vivibe</option><option value="genmax">GenMax</option></select></div><div><label>Giọng tiếng Việt</label><select name="voice">%VOICE%</select></div></div>
       <div class="row2" style="margin-top:10px"><div><label>Tốc độ video</label><select name="video_speed"><option value="1" selected>1x · Bình thường</option><option value="0.75">0.75x</option><option value="1.25">1.25x</option><option value="1.5">1.5x</option><option value="2">2x</option></select></div><div><label>Lật hình</label><select name="movie_flip"><option value="none" selected>Giữ hình gốc</option><option value="hflip">Lật gương ngang</option></select></div></div>
       <div style="margin-top:10px"><label>Xử lý phụ đề gốc</label><select name="original_subtitles" id="studioOriginalSubtitles"><option value="smart_bottom" selected>Xóa thông minh local — phía dưới</option><option value="smart_top">Xóa thông minh local — phía trên</option><option value="smart_custom">Xóa thông minh — vùng tùy chỉnh</option><option value="bottom">Làm mờ phía dưới</option><option value="top">Làm mờ phía trên</option><option value="keep">Giữ sub gốc</option></select></div>
       <div id="studioSubtitleRegion" style="display:none"><div class="studio-template-layout"><div><label>Tỷ lệ khung hình mẫu</label><select name="subtitle_frame_ratio" id="studioFrameRatio"><option value="9:16">9:16 · Video dọc</option><option value="16:9" selected>16:9 · Video ngang</option><option value="1:1">1:1 · Vuông</option><option value="4:3">4:3 · Ngang cổ điển</option><option value="3:4">3:4 · Dọc</option></select><p class="d" style="margin-top:7px">Kéo chuột trực tiếp trên khung mẫu để khoanh vùng sub cũ.</p></div><div id="studioTemplateFrame" class="studio-template-frame"><div id="studioTemplateBox" class="studio-template-box"></div></div></div><div class="row4" style="margin-top:10px"><div><label>X (%)</label><input type="number" name="subtitle_region_x" min="0" max="99" step="0.5" value="5"></div><div><label>Y (%)</label><input type="number" name="subtitle_region_y" min="0" max="99" step="0.5" value="70"></div><div><label>Rộng (%)</label><input type="number" name="subtitle_region_width" min="1" max="100" step="0.5" value="90"></div><div><label>Cao (%)</label><input type="number" name="subtitle_region_height" min="1" max="100" step="0.5" value="25"></div></div></div>
       <div class="row3" style="margin-top:10px"><div><label>Cỡ chữ sub</label><input type="number" name="subtitle_font_size" min="12" max="48" value="18"></div><div><label>Màu chữ</label><input type="color" name="subtitle_font_color" value="#ffffff" style="height:39px;padding:3px"></div><div><label>Màu viền</label><input type="color" name="subtitle_outline_color" value="#101010" style="height:39px;padding:3px"></div></div>
      </div>
      <div class="sec"><div class="sec-h"><span class="b">3</span> Tạo bản nháp</div><button class="btn big-btn" type="submit">✨ Phân tích và tạo bản nháp để duyệt</button></div>
     </form>
     <div id="studioProject" style="display:none;margin-top:18px">
      <div class="sec-h"><span class="b">4</span> Duyệt nội dung và timestamp</div>
      <div id="studioState" class="fbox"></div>
      <div id="studioEditor" style="display:none">
       <div class="row2" style="margin:12px 0"><div><label>Nguồn đang sửa</label><select id="studioSource"></select></div><div><label>Xem trước</label><div class="d">Bấm một dòng bên dưới để phát đúng đoạn.</div></div></div>
       <div class="studio-region-actions"><div><label>Xử lý sub cũ khi render</label><select id="studioReviewSubtitleMode"><option value="smart_bottom">Xóa thông minh — phía dưới</option><option value="smart_top">Xóa thông minh — phía trên</option><option value="smart_custom">Xóa thông minh — chọn trực tiếp trên video</option><option value="bottom">Làm mờ phía dưới</option><option value="top">Làm mờ phía trên</option><option value="keep">Giữ sub gốc</option></select></div><button class="btn ghost" id="studioPickRegion" type="button">▣ Khoanh vùng trên video</button></div>
       <div id="studioReviewRegionValues" class="studio-region-values" style="display:none"><div><label>X (%)</label><input id="studioRegionX" type="number" min="0" max="99" step="0.1"></div><div><label>Y (%)</label><input id="studioRegionY" type="number" min="0" max="99" step="0.1"></div><div><label>Rộng (%)</label><input id="studioRegionW" type="number" min="0.1" max="100" step="0.1"></div><div><label>Cao (%)</label><input id="studioRegionH" type="number" min="0.1" max="100" step="0.1"></div></div>
       <div class="d" id="studioRegionHelp" style="display:none;margin:7px 0">Bấm “Khoanh vùng”, sau đó kéo chuột từ góc trên trái đến góc dưới phải của vùng chứa sub cũ.</div>
       <div class="studio-video-wrap"><video id="studioVideo" controls preload="metadata"></video><div id="studioRegionLayer" class="studio-region-layer"><div id="studioRegionBox" class="studio-region-box" style="display:none"></div></div></div>
       <div class="d" style="margin-top:10px">Mỗi line có giọng riêng. Đổi giọng ở line nào chỉ áp dụng cho đúng line đó.</div>
       <div id="studioSegments" class="studio-segments"></div>
       <div style="display:flex;gap:8px;margin-top:12px"><button class="btn ghost" id="studioSave" type="button">💾 Lưu bản nháp</button><button class="btn" id="studioConfirm" type="button">✓ Xác nhận và render</button></div>
      </div>
     </div>
    </div>
   </section>

   <!-- REVIEW -->
   <section class="view on" id="v-review">
    <div class="card">
     <h2 class="hero-h">🎬 Tạo video review</h2>
     <p class="hero-d">Dán 1 link TikTok/Douyin hoặc nhiều video cùng sản phẩm → tool tự nghe, phân tích thông tin trong video, cắt ghép và viết lời review mới. Chỉ 3 bước.</p>
     <div class="stepper">
      <div class="stp"><div class="n">1</div><div><div class="tt">Dán nguồn</div><div class="ds">1 link TikTok/Douyin hoặc nhiều video</div></div></div>
      <div class="stp"><div class="n">2</div><div><div class="tt">Tuỳ chọn</div><div class="ds">số bản, hook, facts</div></div></div>
      <div class="stp"><div class="n">3</div><div><div class="tt">Nhận video</div><div class="ds">xem & tải ở Kết quả</div></div></div>
     </div>
     <form id="f2">
      <div class="sec">
       <div class="sec-h"><span class="b">1</span> Nguồn video (tối thiểu 1 link hoặc file)</div>
       <div class="meta" style="display:flex;gap:7px;align-items:center;margin-bottom:6px"><span>Hỗ trợ:</span><span class="tag">TikTok</span><span class="tag">Douyin</span><span class="tag">Google Drive</span><span class="tag">Facebook</span></div>
       <textarea name="links" placeholder="Dán link TikTok / Douyin / Google Drive / Facebook, mỗi dòng 1 link..."></textarea>
       <label class="drop" id="d2" style="margin-top:8px">📁 hoặc kéo thả / bấm chọn nhiều file video<input type="file" name="files" multiple accept="video/*"></label>
      </div>
      <div class="sec">
       <div class="sec-h"><span class="b">2</span> Sản phẩm & tuỳ chọn</div>
        <label>Tên sản phẩm (bỏ trống = tool tự đoán)</label>
        <input type="text" name="product" placeholder="vd: viên thả bồn cầu, máy phát điện mini...">
        <label>Link affiliate Shopee</label>
        <input type="text" name="product_link" placeholder="https://s.shopee.vn/...">
       <div class="row3" style="margin-top:10px">
        <div><label>Kịch bản</label><select name="framework" data-k="review_fw">%FRAMEWORK%</select></div>
        <div><label>Số bản</label><select name="n_variants" data-k="review_n">%NUM%</select></div>
        <div><label>Hook / bản</label><select name="hook_variants" data-k="review_hv"><option value="1">1 hook</option><option value="2">2 hook</option><option value="3">3 hook</option><option value="4">4 hook</option></select></div>
       </div>
       <div class="row3" style="margin-top:10px">
        <div><label>Niche</label><select name="niche" data-k="niche">%NICHE%</select></div>
        <div><label>Giọng đọc</label><select name="voice" data-k="review_voice">%VOICE%</select></div>
        <div><label>Dịch vụ giọng đọc</label><select name="tts_provider" data-provider><option value="vivibe">Vivibe</option><option value="genmax">GenMax</option></select></div>
       </div>
       <div class="fbox">
        <div class="fbox-t">📋 Facts sản phẩm (nhồi vào lời — để trống nếu không cần)</div>
        <label>Thông số chốt</label>
        <input type="text" name="specs" placeholder="vd: 400 lần xả · 3 mùi hương · diệt khuẩn 99,9%">
        <div class="row2">
         <div><label>Giá / ưu đãi</label><input type="text" name="price" placeholder="vd: 99k/3 lọ"></div>
         <div><label>CTA</label><input type="text" name="cta" placeholder="vd: giỏ hàng góc trái"></div>
        </div>
       </div>
      </div>
      <div style="margin-top:14px"><label>Xử lý phụ đề/chữ gốc</label><select name="original_subtitles"><option value="smart_bottom" selected>Xóa thông minh local — phía dưới</option><option value="smart_top">Xóa thông minh local — phía trên</option><option value="bottom">Làm mờ phía dưới (nhanh)</option><option value="top">Làm mờ phía trên (nhanh)</option><option value="keep">Giữ chữ gốc</option></select><p class="d" style="margin-top:6px">Dùng OpenCV tái tạo nền giống tab Dịch phim; nếu xóa lỗi sẽ tự làm mờ vùng sub.</p></div>
      <label style="display:flex;align-items:center;gap:8px;margin-top:10px;cursor:pointer;font-size:12px">
       <input type="checkbox" name="exclude_people" value="1" style="width:auto"> Loại cảnh có người (vẫn giữ cảnh chỉ có bàn tay)</label>
      <label style="display:flex;align-items:center;gap:8px;margin-top:10px;cursor:pointer;font-size:12px">
       <input type="checkbox" name="remove_original_audio" value="1" style="width:auto"> Loại bỏ toàn bộ âm thanh gốc (video đầu ra chỉ giữ giọng đọc mới)</label>
      <div class="sec"><div class="sec-h"><span class="b">3</span> Tạo video</div>
       <button class="btn big-btn" type="submit">✨ Tạo video — xem kết quả ở tab Kết quả</button></div>
     </form>
    </div>
   </section>

   <!-- KẾT QUẢ -->
   <section class="view" id="v-jobs">
    <div class="results-toolbar">
     <div class="seg" id="filter">
      <button class="on" data-f="all">Tất cả</button><button data-f="reup">Reup</button><button data-f="remake">Remake</button><button data-f="review">Review</button><button data-f="translate">Dịch phim</button><button data-f="movie_studio">Studio phim</button>
     </div>
     <span class="meta" id="jcount"></span>
     <div class="seg post-filter" id="postFilter">
      <button class="on" data-p="all">Tất cả</button><button data-p="posted">Đã đăng</button><button data-p="unposted">Chưa đăng</button>
     </div>
    </div>
    <div id="jobs"></div>
   </section>

   <!-- BÀI VIẾT FACEBOOK -->
   <section class="view" id="v-fbposts">
    <div class="page-posts-toolbar">
     <div><label>Facebook Page</label><select id="postsPage"></select></div>
     <button class="btn ghost" id="refreshPagePosts">Làm mới</button>
     <span class="meta" id="pagePostCount"></span>
    </div>
    <div class="content-table-wrap">
     <table class="content-table">
      <thead><tr><th>Bản xem trước</th><th>Trạng thái</th><th>Lượt xem</th><th>Người xem</th><th>Lượt tương tác</th><th>Số người theo dõi thực</th><th>Lượt hiển thị</th><th>Bình luận</th><th>Lượt phân phối</th><th>Thời gian xem</th><th>Thời gian xem trung bình</th><th>Lượt xem tối thiểu 3 giây</th><th>Lượt xem tối thiểu 1 phút</th><th>Thao tác</th></tr></thead>
      <tbody id="pagePosts"></tbody>
     </table>
    </div>
    <button class="btn ghost" id="morePagePosts" style="display:none;margin:14px auto 0">Xem thêm</button>
   </section>

   <!-- CÀI ĐẶT -->
   <section class="view" id="v-set">
    <div class="grid cols2">
     <div class="card">
      <h2>Mặc định · Remake</h2><p class="d">Giá trị điền sẵn cho form Remake.</p>
      <div class="row2">
       <div><label>Niche</label><select data-s="niche">%NICHE%</select></div>
       <div><label>Số biến thể</label><select data-s="n_variants">%NUM%</select></div>
      </div>
      <div class="row2">
       <div><label>Giọng</label><select data-s="voice">%VOICE%</select></div>
       <div><label>Lật hình</label><select data-s="flip">%FLIP%</select></div>
      </div>
      <div class="row2">
       <div><label>Hook</label><select data-s="hook">%HOOK%</select></div>
       <div><label>Style</label><select data-s="style">%STYLE%</select></div>
      </div>
     </div>
     <div class="card">
      <h2>Mặc định · Review</h2><p class="d">Giá trị điền sẵn cho form Ghép Review.</p>
      <div class="row2">
       <div><label>Kịch bản</label><select data-s="review_fw">%FRAMEWORK%</select></div>
       <div><label>Số bản review</label><select data-s="review_n">%NUM%</select></div>
      </div>
      <div class="row2">
       <div><label>Giọng</label><select data-s="review_voice">%VOICE%</select></div>
       <div></div>
      </div>
      <button class="btn" id="saveSet">💾 Lưu cài đặt</button>
     </div>
      <div class="card">
       <h2>Hệ thống</h2><p class="d">Thông tin tool.</p>
      <div class="kv"><span>Cổng</span><b>7995</b></div>
      <div class="kv"><span>Thư mục lưu video</span><b id="outdir" style="font-size:11px;word-break:break-all;text-align:right">...</b></div>
      <button class="btn sm ghost" id="openOut" style="margin-top:8px">📂 Mở thư mục video</button>
      <p class="d" style="margin:8px 0 0">Đổi nơi lưu: sửa <code>output_dir</code> trong config.json rồi khởi động lại tool.</p>
      <div class="kv" style="margin-top:8px"><span>Giọng khả dụng</span><b>5 nam · 2 nữ (Vivibe)</b></div>
      <div class="kv"><span>Worker</span><b>1 job tuần tự</b></div>
       <div class="kv"><span>Đăng bài</span><b>Facebook Page (khi đã kết nối)</b></div>
      </div>
      <div class="card">
       <h2>GenMax API</h2><p class="d">Key dùng để tạo giọng đọc GenMax. Key được lưu cục bộ trong <code>config.json</code>.</p>
       <label>GenMax API key</label>
       <input type="password" id="genmaxKey" autocomplete="off" placeholder="Dán GenMax API key">
       <div class="meta" id="genmaxKeyState" style="margin:7px 0 10px">Chưa kiểm tra</div>
       <button class="btn" id="saveGenmax">Lưu API key</button>
      </div>
      <div class="card">
       <h2>Douyin Cookie</h2><p class="d">Dùng cho link <code>jingxuan?modal_id=...</code> bị Douyin chặn 403. Cookie chỉ lưu cục bộ trong <code>config.json</code>; không chia sẻ cho người khác.</p>
       <label>Cookie đăng nhập Douyin</label>
       <input type="password" id="douyinCookie" autocomplete="off" placeholder="Dán toàn bộ Cookie header từ Douyin">
       <div class="meta" id="douyinCookieState" style="margin:7px 0 10px">Chưa kiểm tra</div>
       <button class="btn" id="saveDouyinCookie">Lưu Douyin Cookie</button>
      </div>
      <div class="card">
       <h2>Bilibili Cookie</h2><p class="d">Dùng tài khoản Bilibili đã có quyền xem với tập yêu cầu đăng nhập. Cookie chỉ lưu cục bộ trong <code>config.json</code> và không được ghi vào log.</p>
       <label>Cookie đăng nhập Bilibili</label>
       <input type="password" id="bilibiliCookie" autocomplete="off" placeholder="Dán toàn bộ Cookie header từ bilibili.tv">
       <div class="meta" id="bilibiliCookieState" style="margin:7px 0 10px">Chưa kiểm tra</div>
       <button class="btn" id="saveBilibiliCookie">Lưu Bilibili Cookie</button>
      </div>
      <div class="card">
       <h2>Vivibe API</h2><p class="d">Key dùng để tạo giọng đọc. Key được lưu cục bộ trong <code>config.json</code>.</p>
       <label>Vivibe API key</label>
       <input type="password" id="vivibeKey" autocomplete="off" placeholder="Dán key sk_live_...">
       <div class="meta" id="vivibeKeyState" style="margin:7px 0 10px">Chưa kiểm tra</div>
       <button class="btn" id="saveVivibe">💾 Lưu API key</button>
      </div>
      <div class="card">
       <h2>Facebook Page</h2><p class="d">Kết nối Page để đăng video kèm caption. Token chỉ được lưu cục bộ trong <code>config.json</code>.</p>
       <label>Tên gợi nhớ</label><input type="text" id="facebookPageName" placeholder="Ví dụ: Page gia dụng">
       <label>Page ID</label><input type="text" id="facebookPageId" placeholder="ID của Facebook Page">
       <label>Page Access Token</label><input type="password" id="facebookToken" autocomplete="off" placeholder="Dán Page Access Token">
       <div class="meta" id="facebookState" style="margin:7px 0 10px">Chưa kiểm tra</div>
       <button class="btn" id="saveFacebook">Thêm / cập nhật Page</button>
       <div id="facebookPageList" style="margin-top:14px"></div>
      </div>
    </div>
   </section>

   <!-- NHẬT KÝ -->
   <section class="view" id="v-log">
    <div class="card"><h2>Nhật ký hệ thống</h2><p class="d">300 dòng gần nhất (studio.log).</p>
     <div class="log" id="syslog" style="max-height:64vh">...</div></div>
   </section>

   <section class="view" id="v-account">
    <div class="grid cols2">
     <div class="card"><h2>Đổi mật khẩu</h2><p class="d">Mật khẩu mới cần ít nhất 8 ký tự.</p>
      <label>Mật khẩu hiện tại</label><input type="password" id="oldPass" autocomplete="current-password">
      <label>Mật khẩu mới</label><input type="password" id="newPass" autocomplete="new-password">
      <button class="btn" id="changePass">Đổi mật khẩu</button></div>
     <div class="card" id="adminUsers" style="display:none"><h2>Quản lý người dùng</h2><p class="d">Tạo tài khoản user/admin, đặt lại mật khẩu hoặc xoá tài khoản.</p>
      <div class="row2"><div><label>Tên đăng nhập</label><input id="newUsername"></div><div><label>Vai trò</label><select id="newRole"><option value="user">User</option><option value="admin">Admin</option></select></div></div>
      <label>Mật khẩu khởi tạo</label><input type="password" id="newUserPass" autocomplete="new-password">
      <button class="btn" id="addUser">Tạo tài khoản</button><div id="userList" style="margin-top:16px"></div></div>
    </div>
   </section>

  </div>
 </main>
</div>
<div class="toast" id="toast"></div>

<script>
const IC={home:'<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/>',discovery:'<path d="M12 2l2.2 6.2L20 10l-5.8 1.8L12 18l-2.2-6.2L4 10l5.8-1.8z"/>',remake:'<path d="M4 4v6h6"/><path d="M20 20v-6h-6"/><path d="M20 9a8 8 0 0 0-14-3M4 15a8 8 0 0 0 14 3"/>',translate:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/>',review:'<rect x="3" y="4" width="18" height="14" rx="2"/><path d="M10 9l4 2-4 2z"/>',jobs:'<path d="M4 6h16M4 12h16M4 18h10"/>',fbposts:'<path d="M6 3h12a3 3 0 0 1 3 3v12a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3V6a3 3 0 0 1 3-3z"/><path d="M14 8h-2a2 2 0 0 0-2 2v8M8 13h6"/>',set:'<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1l2-1.6-2-3.4-2.3 1a7 7 0 0 0-1.7-1L14.5 2h-4l-.4 2.5a7 7 0 0 0-1.7 1l-2.3-1-2 3.4 2 1.6a7 7 0 0 0 0 2l-2 1.6 2 3.4 2.3-1a7 7 0 0 0 1.7 1l.4 2.5h4l.4-2.5a7 7 0 0 0 1.7-1l2.3 1 2-3.4-2-1.6a7 7 0 0 0 .1-1z"/>',log:'<path d="M4 4h16v16H4z"/><path d="M8 8h8M8 12h8M8 16h5"/>',account:'<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>'};
IC.reup=IC.review;IC.studio='<path d="M3 5h18v14H3z"/><path d="M7 9h10M7 13h6"/>';IC.soundtrack='<path d="M9 18V5l10-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/>';
let ME=null,NAV=[],FB_PAGES=[];
const HINT={home:"",discovery:"đăng nhập local → quét → chọn nguồn",soundtrack:"video + âm thanh → video mới",remake:"1 video → nhiều biến thể",reup:"tóm tắt nội dung → hook 3 giây → render",translate:"nghe lời → dịch → phụ đề",studio:"tạo nháp → chỉnh sub → xác nhận render",review:"dán link → cắt ghép → video mới",jobs:"video đã làm + tiến trình",fbposts:"xem và quản lý bài trên Page",set:"chỉnh mặc định",log:"",account:"đổi mật khẩu và quản lý tài khoản"};
const nav=document.getElementById("nav");
function buildNav(){nav.innerHTML="";NAV=ME.role==="admin"?[["home","Tổng quan"],["discovery","Khám phá video"],["fbposts","Bài viết Facebook"],["review","Tạo video"],["soundtrack","Ghép âm thanh"],["reup","Reup video"],["translate","Dịch phim"],["studio","Studio phim"],["jobs","Kết quả"],["remake","Làm lại 1 video"],["set","Cài đặt"],["log","Nhật ký"],["account","Tài khoản"]]:[["home","Tổng quan"],["discovery","Khám phá video"],["review","Tạo video"],["soundtrack","Ghép âm thanh"],["reup","Reup video"],["translate","Dịch phim"],["studio","Studio phim"],["jobs","Kết quả"],["remake","Làm lại 1 video"],["account","Tài khoản"]];NAV.forEach(([k,t],i)=>{const d=document.createElement("div");d.className="nav"+(i==0?" on":"");d.dataset.k=k;
 d.innerHTML=`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${IC[k]}</svg><span>${t}</span>`+(k=="jobs"?'<span class="badge" id="navq" style="display:none"></span>':'');
 d.onclick=()=>go(k);nav.appendChild(d);});}
function go(k){
 document.querySelectorAll(".nav").forEach(n=>n.classList.toggle("on",n.dataset.k==k));
 document.querySelectorAll(".view").forEach(v=>v.classList.remove("on"));
 document.getElementById("v-"+k).classList.add("on");
 document.getElementById("ptitle").textContent=NAV.find(x=>x[0]==k)[1];
 document.getElementById("phint").textContent=HINT[k]||"";
 location.hash=k;
 if(k=="jobs")renderJobs(true);if(k=="studio")resumeMovieStudio();if(k=="fbposts")loadFacebookPosts(true);if(k=="log")loadLog();if(k=="home")loadHome();if(k=="discovery")loadDiscoveryAccounts();if(k=="account"&&ME.role==="admin")loadUsers();
}
window.addEventListener("hashchange",()=>{const k=location.hash.slice(1);if(k&&NAV.some(x=>x[0]==k))go(k);});

function toast(m){const t=document.getElementById("toast");t.textContent=m;t.classList.add("show");setTimeout(()=>t.classList.remove("show"),2600);}
function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
async function apiFetch(url,options){const r=await fetch(url,options),text=await r.text();let data;try{data=JSON.parse(text);}catch(_){const clean=text.replace(/<[^>]*>/g," ").replace(/\s+/g," ").trim().slice(0,180);throw new Error(`Máy chủ trả phản hồi không hợp lệ (HTTP ${r.status})${clean?' · '+clean:''}`);}if(!r.ok||data.ok===false)throw new Error(data.err||`Yêu cầu thất bại (HTTP ${r.status})`);return data;}
function metric(n){n=Number(n||0);return n>=1000000?(n/1000000).toFixed(1)+"M":n>=1000?(n/1000).toFixed(1)+"K":String(n);}
let DISCOVERY=[];
async function loadDiscoveryAccounts(){const box=document.getElementById("discoverAccounts");box.innerHTML='<span class="d">Đang kiểm tra...</span>';try{const j=await apiFetch("/api/discovery/accounts");box.innerHTML=j.accounts.map(a=>`<div class="out" style="padding:10px;margin-top:7px;display:flex;align-items:center;gap:10px"><div style="flex:1"><b>${esc(a.name)}</b><div class="d">${a.active?'Trình duyệt đang mở':a.saved?'Đã lưu phiên đăng nhập':'Chưa đăng nhập'}${a.last_login?' · '+esc(a.last_login.replace('T',' ')):''}</div></div><button class="btn sm ghost discovery-account-login" data-platform="${a.platform}">${a.saved?'Đăng nhập lại':'Đăng nhập'}</button>${a.saved?`<button class="btn sm ghost discovery-account-delete" data-platform="${a.platform}" ${a.active?'disabled':''}>Xóa phiên</button>`:''}</div>`).join("");}catch(e){box.innerHTML='<span class="d">Lỗi: '+esc(e.message)+'</span>';}}
function renderDiscovery(items){DISCOVERY=items||[];const box=document.getElementById("discoverResults");if(!DISCOVERY.length){box.innerHTML='<div class="discover-empty">Không tìm thấy video phù hợp. Hãy thử bỏ từ khóa hoặc đăng nhập lại.</div>';return;}box.innerHTML=DISCOVERY.map((v,i)=>`<article class="discover-card"><img src="${esc(v.thumbnail)}" alt="" loading="lazy" onerror="this.style.display='none'"><div class="dc-body"><h3 title="${esc(v.title)}">${esc(v.title)}</h3><div class="discover-meta"><span>${esc(v.author||v.platform)}</span><span>👁 ${metric(v.views)}</span><span>♥ ${metric(v.likes)}</span><span class="discover-score">Điểm ${Number(v.score).toFixed(1)}</span></div><div class="dc-foot"><input type="checkbox" class="discover-pick" data-i="${i}" aria-label="Chọn video"><a href="${esc(v.url)}" target="_blank" rel="noopener">Xem nguồn</a></div></div></article>`).join("");}
document.getElementById("discoverLogin").onclick=async()=>{const platform=document.getElementById("discoverPlatform").value,b=document.getElementById("discoverLogin");b.disabled=true;try{await apiFetch("/api/discovery/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({platform})});toast("Đã mở cửa sổ đăng nhập");document.getElementById("discoverStatus").textContent="Hãy đăng nhập trong cửa sổ mới, sau đó đóng cửa sổ đó rồi bấm Quét.";}catch(e){toast("⚠ "+e.message);document.getElementById("discoverStatus").textContent="Lỗi: "+e.message;}finally{b.disabled=false;}};
document.getElementById("discoverAccounts").onclick=async e=>{const login=e.target.closest(".discovery-account-login"),del=e.target.closest(".discovery-account-delete");if(login){document.getElementById("discoverPlatform").value=login.dataset.platform;document.getElementById("discoverLogin").click();setTimeout(loadDiscoveryAccounts,800);}if(del&&confirm("Xóa phiên đăng nhập đã lưu trên máy?")){del.disabled=true;try{await apiFetch(`/api/discovery/account/${del.dataset.platform}`,{method:"DELETE"});toast("Đã xóa phiên đăng nhập");await loadDiscoveryAccounts();}catch(err){toast("⚠ "+err.message);del.disabled=false;}}};
document.getElementById("discoverSearch").onclick=async()=>{const b=document.getElementById("discoverSearch"),status=document.getElementById("discoverStatus");b.disabled=true;b.innerHTML='<span class="spin"></span> Đang quét...';status.textContent="Đang mở bảng thịnh hành và đọc dữ liệu hiển thị...";try{const body={platform:document.getElementById("discoverPlatform").value,topic:document.getElementById("discoverTopic").value,date:document.getElementById("discoverDate").value,limit:Number(document.getElementById("discoverLimit").value)},j=await apiFetch("/api/discovery/search",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});renderDiscovery(j.items);status.textContent=`Đã quét ${j.items.length} video lúc ${j.scanned_at.replace('T',' ')}${j.warning?' · '+j.warning:''}`;}catch(e){status.textContent="Lỗi: "+e.message;toast("⚠ "+e.message);}finally{b.disabled=false;b.textContent="Quét video thịnh hành";}};
function moveDiscovery(target){const urls=[...document.querySelectorAll(".discover-pick:checked")].map(x=>DISCOVERY[Number(x.dataset.i)]?.url).filter(Boolean);if(!urls.length){toast("Hãy chọn ít nhất một video");return;}const form=document.getElementById(target==="review"?"f2":"f1"),area=form.querySelector('textarea[name="links"]');area.value=urls.join("\n");go(target);toast(`Đã chuyển ${urls.length} link sang ${target==="review"?"Review":"Remake"}`);}
document.getElementById("discoverReview").onclick=()=>moveDiscovery("review");
document.getElementById("discoverRemake").onclick=()=>moveDiscovery("remake");
const BC={queued:"b-queued",running:"b-running",awaiting_review:"b-queued",done:"b-done",error:"b-error"};
const BT={queued:"⏳ chờ",running:'<span class="spin"></span> đang chạy',awaiting_review:"✏️ chờ duyệt",done:"✅ xong",error:"❌ lỗi"};

/* dropzone */
function wireDrop(id){const l=document.getElementById(id);const inp=l.querySelector("input");
 inp.onchange=()=>{l.classList.toggle("has",inp.files.length>0);if(inp.files.length)l.firstChild.textContent="✓ "+inp.files.length+" file đã chọn";};
 l.ondragover=e=>{e.preventDefault();l.classList.add("has")};
 l.ondragleave=()=>l.classList.remove("has");
 l.ondrop=e=>{e.preventDefault();inp.files=e.dataTransfer.files;inp.onchange();};}
wireDrop("d1");wireDrop("d2");
wireDrop("d3");
wireDrop("d4");
wireDrop("d6");
function syncProviderVoices(providerSelect){const voice=providerSelect.closest("form")?.querySelector('select[name="voice"]');if(!voice)return;const genmax=providerSelect.value==="genmax";[...voice.options].forEach(o=>{const isGenmax=o.value==="adam3"||o.value.startsWith("genmax_");const show=o.value==="auto"||(genmax?isGenmax:!isGenmax);o.hidden=!show;o.disabled=!show;});if(voice.selectedOptions[0]?.disabled)voice.value="auto";}
document.querySelectorAll("[data-provider]").forEach(el=>{el.addEventListener("change",()=>syncProviderVoices(el));syncProviderVoices(el);});
const studioSubMode=document.getElementById("studioOriginalSubtitles"),studioSubRegion=document.getElementById("studioSubtitleRegion"),studioFrameRatio=document.getElementById("studioFrameRatio"),studioTemplateFrame=document.getElementById("studioTemplateFrame"),studioTemplateBox=document.getElementById("studioTemplateBox"),studioTemplateInputs=["subtitle_region_x","subtitle_region_y","subtitle_region_width","subtitle_region_height"].map(name=>document.querySelector(`#f6 [name="${name}"]`));
function studioTemplateValues(){return studioTemplateInputs.map(input=>Number(input.value));}
function paintStudioTemplate(){const [x,y,w,h]=studioTemplateValues();studioTemplateBox.style.left=x+"%";studioTemplateBox.style.top=y+"%";studioTemplateBox.style.width=w+"%";studioTemplateBox.style.height=h+"%";}
function syncStudioFrameRatio(){const [w,h]=studioFrameRatio.value.split(":").map(Number);studioTemplateFrame.style.aspectRatio=`${w}/${h}`;studioTemplateFrame.style.width=w<h?"230px":w===h?"360px":"520px";paintStudioTemplate();}
function syncStudioSubRegion(){studioSubRegion.style.display=studioSubMode.value==="smart_custom"?"":"none";if(studioSubMode.value==="smart_custom")syncStudioFrameRatio();}
studioSubMode.addEventListener("change",syncStudioSubRegion);studioFrameRatio.addEventListener("change",syncStudioFrameRatio);studioTemplateInputs.forEach(input=>input.addEventListener("input",paintStudioTemplate));
(()=>{let start=null;const point=e=>{const r=studioTemplateFrame.getBoundingClientRect();return [Math.max(0,Math.min(100,(e.clientX-r.left)*100/r.width)),Math.max(0,Math.min(100,(e.clientY-r.top)*100/r.height))];};const update=(a,b)=>{const values=[Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.max(.1,Math.abs(b[0]-a[0])),Math.max(.1,Math.abs(b[1]-a[1]))];studioTemplateInputs.forEach((input,i)=>input.value=values[i].toFixed(1));paintStudioTemplate();};studioTemplateFrame.onpointerdown=e=>{start=point(e);studioTemplateFrame.setPointerCapture(e.pointerId);update(start,start);};studioTemplateFrame.onpointermove=e=>{if(start)update(start,point(e));};studioTemplateFrame.onpointerup=e=>{if(!start)return;update(start,point(e));studioTemplateFrame.releasePointerCapture(e.pointerId);start=null;toast("✓ Đã khoanh vùng sub cũ trên khung mẫu");};})();
syncStudioSubRegion();

async function submit(form,url,label){
 const btn=form.querySelector(".btn");btn.disabled=true;const old=btn.textContent;btn.innerHTML='<span class="spin"></span> Đang gửi...';
 try{const r=await fetch(url,{method:"POST",body:new FormData(form)});const j=await r.json();
  if(!j.ok){toast("⚠ "+(j.err||"Lỗi"));}
  else{const ta=form.querySelector("textarea");if(ta)ta.value="";const l=form.querySelector(".drop");if(l){const fi=l.querySelector("input");if(fi)fi.value="";l.classList.remove("has");l.firstChild.textContent=(l.id=="d1")?"📁 Kéo thả hoặc bấm chọn file (nhiều file)":"📁 hoặc kéo thả / bấm chọn nhiều file video";}
   toast("✅ Đã thêm vào hàng chờ");go("jobs");}
 }catch(e){toast("⚠ "+e);}
 btn.disabled=false;btn.textContent=old;
}
let STUDIO_JOB=localStorage.getItem("movieStudioJob")||"",STUDIO_DATA=null,STUDIO_TIMER=null,STUDIO_SOURCE=0;
function studioState(text){const root=document.getElementById("studioProject"),state=document.getElementById("studioState");root.style.display="";state.textContent=text;}
async function resumeMovieStudio(){
 if(!STUDIO_JOB){try{const data=await apiFetch("/api/jobs"),job=(data.jobs||[]).find(item=>item.kind==="movie_studio"&&["queued","running","awaiting_review"].includes(item.status));if(job){STUDIO_JOB=job.id;localStorage.setItem("movieStudioJob",STUDIO_JOB);}}catch(_){}}
 if(STUDIO_JOB)loadMovieStudio(STUDIO_JOB);
}
async function loadMovieStudio(id){
 clearTimeout(STUDIO_TIMER);studioState("Đang đọc dự án "+id+"...");
 try{const data=await apiFetch(`/api/movie-studio/${encodeURIComponent(id)}`);STUDIO_DATA=data;
  if(data.status==="queued"||data.status==="running"){document.getElementById("studioEditor").style.display="none";studioState(data.status==="queued"?"Bản nháp đang chờ xử lý...":"Đang nghe, dịch và tạo bản nháp...");STUDIO_TIMER=setTimeout(()=>loadMovieStudio(id),2500);return;}
  if(data.status==="awaiting_review"){studioState(`Bản nháp #${data.draft_version} đã sẵn sàng · ${data.drafts.length} nguồn`);document.getElementById("studioEditor").style.display="";const select=document.getElementById("studioSource");select.innerHTML=data.drafts.map((d,i)=>`<option value="${i}">${i+1}. ${esc(d.title||"Video")}</option>`).join("");STUDIO_SOURCE=Math.min(STUDIO_SOURCE,Math.max(0,data.drafts.length-1));select.value=String(STUDIO_SOURCE);document.getElementById("studioReviewSubtitleMode").value=data.original_subtitles||"smart_bottom";writeStudioRegion(data.subtitle_region||[5,70,90,25]);syncStudioReviewRegion();renderStudioSource();return;}
  document.getElementById("studioEditor").style.display="none";studioState(data.status==="done"?"Dự án đã render xong. Xem video trong mục Kết quả.":"Dự án lỗi: "+(data.error||"không rõ lỗi"));
 }catch(err){studioState("Không đọc được dự án: "+err.message);}
}
function readStudioRegion(){const ids=["studioRegionX","studioRegionY","studioRegionW","studioRegionH"],values=ids.map(id=>Number(document.getElementById(id).value));return values.every(Number.isFinite)?values.map(v=>Math.round(v*100)/100):null;}
function writeStudioRegion(region){["studioRegionX","studioRegionY","studioRegionW","studioRegionH"].forEach((id,i)=>document.getElementById(id).value=Number(region[i]).toFixed(1));paintStudioRegion();}
function paintStudioRegion(){const region=readStudioRegion(),box=document.getElementById("studioRegionBox"),custom=document.getElementById("studioReviewSubtitleMode").value==="smart_custom";if(!custom||!region){box.style.display="none";return;}box.style.display="";box.style.left=region[0]+"%";box.style.top=region[1]+"%";box.style.width=region[2]+"%";box.style.height=region[3]+"%";}
function syncStudioReviewRegion(){const custom=document.getElementById("studioReviewSubtitleMode").value==="smart_custom";document.getElementById("studioReviewRegionValues").style.display=custom?"":"none";document.getElementById("studioPickRegion").style.display=custom?"":"none";document.getElementById("studioRegionHelp").style.display=custom?"":"none";if(!custom)document.getElementById("studioRegionLayer").classList.remove("picking");paintStudioRegion();}
document.getElementById("studioReviewSubtitleMode").addEventListener("change",syncStudioReviewRegion);
document.querySelectorAll("#studioReviewRegionValues input").forEach(input=>input.addEventListener("input",paintStudioRegion));
document.getElementById("studioPickRegion").onclick=()=>{const layer=document.getElementById("studioRegionLayer");layer.classList.add("picking");toast("Kéo chuột để khoanh vùng sub cũ");};
(()=>{const layer=document.getElementById("studioRegionLayer");let start=null;const point=e=>{const r=layer.getBoundingClientRect();return [Math.max(0,Math.min(100,(e.clientX-r.left)*100/r.width)),Math.max(0,Math.min(100,(e.clientY-r.top)*100/r.height))];};layer.onpointerdown=e=>{if(!layer.classList.contains("picking"))return;start=point(e);layer.setPointerCapture(e.pointerId);writeStudioRegion([start[0],start[1],.1,.1]);};layer.onpointermove=e=>{if(!start)return;const end=point(e),x=Math.min(start[0],end[0]),y=Math.min(start[1],end[1]);writeStudioRegion([x,y,Math.max(.1,Math.abs(end[0]-start[0])),Math.max(.1,Math.abs(end[1]-start[1]))]);};layer.onpointerup=e=>{if(!start)return;layer.releasePointerCapture(e.pointerId);start=null;layer.classList.remove("picking");paintStudioRegion();toast("✓ Đã chọn vùng sub cũ");};})();
function renderStudioSource(){
 const draft=STUDIO_DATA?.drafts?.[STUDIO_SOURCE];if(!draft)return;const video=document.getElementById("studioVideo");video.src=draft.preview_url;
 const baseVoice=document.querySelector('#f6 select[name="voice"]');const voiceOptions=s=>'<option value="">Dùng giọng mặc định</option>'+[...(baseVoice?.options||[])].map(o=>`<option value="${esc(o.value)}" ${o.value===(s.voice||"")?"selected":""}>${esc(o.textContent)}</option>`).join("");
 const box=document.getElementById("studioSegments");box.innerHTML='<div class="studio-segment-head"><span>Nhân vật</span><span>Giọng</span><span>Bắt đầu</span><span>Kết thúc</span><span>Lời gốc</span><span>Lời dịch / thuyết minh</span></div>'+draft.segments.map((s,i)=>`<div class="studio-segment" data-i="${i}"><input class="seg-speaker" maxlength="80" value="${esc(s.speaker||"Nhân vật 1")}" placeholder="Tên nhân vật"><select class="seg-voice">${voiceOptions(s)}</select><input class="seg-start" type="number" min="0" step="0.001" value="${Number(s.start).toFixed(3)}"><input class="seg-end" type="number" min="0" step="0.001" value="${Number(s.end).toFixed(3)}"><textarea class="seg-original">${esc(s.text)}</textarea><textarea class="seg-translated">${esc(s.translated)}</textarea></div>`).join("");
 box.onclick=e=>{if(e.target.matches("input,textarea,select,option"))return;const row=e.target.closest(".studio-segment");if(!row)return;const start=Number(row.querySelector(".seg-start").value||0),end=Number(row.querySelector(".seg-end").value||start);video.currentTime=start;video.play();const stop=()=>{if(video.currentTime>=end){video.pause();video.removeEventListener("timeupdate",stop);}};video.addEventListener("timeupdate",stop);};
}
function captureStudioSource(){const draft=STUDIO_DATA?.drafts?.[STUDIO_SOURCE];if(!draft)return;draft.segments=[...document.querySelectorAll("#studioSegments .studio-segment")].map((row,i)=>({id:i,speaker:row.querySelector(".seg-speaker").value.trim(),voice:row.querySelector(".seg-voice").value,start:Number(row.querySelector(".seg-start").value),end:Number(row.querySelector(".seg-end").value),text:row.querySelector(".seg-original").value,translated:row.querySelector(".seg-translated").value}));}
document.getElementById("studioSource").onchange=e=>{captureStudioSource();STUDIO_SOURCE=Number(e.target.value);renderStudioSource();};
async function saveStudioDraft(show=true){captureStudioSource();const mode=document.getElementById("studioReviewSubtitleMode").value,region=mode==="smart_custom"?readStudioRegion():null;const result=await apiFetch(`/api/movie-studio/${encodeURIComponent(STUDIO_JOB)}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({original_subtitles:mode,subtitle_region:region,drafts:STUDIO_DATA.drafts.map(d=>({index:d.index,segments:d.segments}))})});STUDIO_DATA.draft_version=result.draft_version;STUDIO_DATA.original_subtitles=mode;STUDIO_DATA.subtitle_region=region;if(show)toast("✓ Đã lưu bản nháp #"+result.draft_version);return result;}
document.getElementById("studioSave").onclick=async()=>{const button=document.getElementById("studioSave");button.disabled=true;try{await saveStudioDraft();}catch(err){toast("⚠ "+err.message);}finally{button.disabled=false;}};
document.getElementById("studioConfirm").onclick=async()=>{if(!confirm("Lưu bản nháp hiện tại và bắt đầu render? Sau bước này không thể sửa tiếp."))return;const button=document.getElementById("studioConfirm");button.disabled=true;try{await saveStudioDraft(false);await apiFetch(`/api/movie-studio/${encodeURIComponent(STUDIO_JOB)}/confirm`,{method:"POST"});toast("✓ Đã xác nhận, dự án được đưa vào hàng render");go("jobs");}catch(err){toast("⚠ "+err.message);button.disabled=false;}};
document.getElementById("f6").addEventListener("submit",async e=>{e.preventDefault();const form=e.target,button=form.querySelector("button[type=submit]"),old=button.textContent;button.disabled=true;button.innerHTML='<span class="spin"></span> Đang gửi...';try{const response=await fetch("/api/movie-studio",{method:"POST",body:new FormData(form)}),data=await response.json();if(!response.ok||!data.ok)throw new Error(data.err||"Không tạo được dự án");STUDIO_JOB=data.id;localStorage.setItem("movieStudioJob",STUDIO_JOB);toast("✓ Đã tạo dự án Studio phim");loadMovieStudio(STUDIO_JOB);}catch(err){toast("⚠ "+err.message);}finally{button.disabled=false;button.textContent=old;}});
document.getElementById("f1").addEventListener("submit",e=>{e.preventDefault();submit(e.target,"/api/remake");});
document.getElementById("f2").addEventListener("submit",e=>{e.preventDefault();submit(e.target,"/api/review");});
document.getElementById("f3").addEventListener("submit",e=>{e.preventDefault();submit(e.target,"/api/translate");});
document.getElementById("f4").addEventListener("submit",e=>{e.preventDefault();submit(e.target,"/api/reup");});
document.getElementById("f5").addEventListener("submit",e=>{e.preventDefault();submit(e.target,"/api/soundtrack");});
function syncMovieMode(){const mode=document.querySelector('#f3 input[name="task_mode"]:checked')?.value||"summary",summary=mode==="summary",output=document.getElementById("movieTranslationOutput"),dub=["dub","dub_srt","all"].includes(output.value),target=document.getElementById("movieTargetLanguage"),original=document.getElementById("movieOriginalOutput");document.getElementById("translateOptions").style.display=summary?"none":"";document.getElementById("summaryOptions").style.display=summary?"":"none";document.getElementById("movieVoiceOptions").style.display=(summary||dub)?"":"none";if(summary||dub){target.value="vi";target.disabled=true;}else target.disabled=false;const noTranslation=target.value==="none";original.hidden=!noTranslation;original.disabled=!noTranslation;if(!noTranslation&&output.value==="original")output.value="both";}
function syncMovieTarget(){const target=document.getElementById("movieTargetLanguage"),output=document.getElementById("movieTranslationOutput"),original=document.getElementById("movieOriginalOutput");if(target.value==="none"){original.hidden=false;original.disabled=false;output.value="original";document.getElementById("movieOriginalSubtitles").value="keep";}syncMovieMode();}
document.querySelectorAll('#f3 input[name="task_mode"]').forEach(el=>el.addEventListener("change",syncMovieMode));document.getElementById("movieTranslationOutput").addEventListener("change",syncMovieMode);document.getElementById("movieTargetLanguage").addEventListener("change",syncMovieTarget);syncMovieTarget();

/* ===== jobs: cập nhật CÓ CHỌN LỌC (chống nháy) ===== */
let curFilter="all",postFilter="all";
document.querySelectorAll("#filter button").forEach(b=>b.onclick=()=>{
 document.querySelectorAll("#filter button").forEach(x=>x.classList.remove("on"));b.classList.add("on");
 curFilter=b.dataset.f;renderJobs(true);});
document.querySelectorAll("#postFilter button").forEach(b=>b.onclick=()=>{
 document.querySelectorAll("#postFilter button").forEach(x=>x.classList.remove("on"));b.classList.add("on");
 postFilter=b.dataset.p;applyPostFilter();});
const sigCache={};              // id -> signature (status:n_out:log_len) đã vẽ
let jobsData=[];
function jobCardHTML(j){
 const exp=j.expected||0, pct=exp?Math.min(100,Math.round(j.n_out/exp*100)):(j.status=="done"?100:0);
 const showbar=(j.status=="running"||j.status=="queued")&&exp>0;
 const kindLabel=j.kind==="remake"?"🎬 Làm lại":j.kind==="translate"?"🌐 Dịch phim":j.kind==="movie_studio"?"🎞️ Studio phim":j.kind==="reup"?"🚀 Reup video":"🎬 Tạo video";
 return `<div class="h"><span class="kind">${kindLabel}</span>
   <span class="badge2 ${BC[j.status]||''}">${BT[j.status]||j.status}</span>
   <span style="margin-left:auto" class="meta">${j.n_in} nguồn → <b style="color:var(--fg)">${j.n_out}${exp?("/"+exp):""}</b> video ${j.product?("· "+j.product):""}</span></div>
   <div class="meta">${j.id} · ${j.created_at||""}${ME.role==="admin"?` · user: ${j.owner||"legacy"}`:""}</div>
   ${showbar?`<div class="pbar"><i style="width:${pct}%"></i></div>`:""}
   <div class="detail" id="det-${j.id}"></div>`;
}
async function loadDetail(j){
 const box=document.getElementById("det-"+j.id);if(!box)return;
 const r=await fetch("/api/job/"+j.id);if(!r.ok)return;const d=await r.json();
 let h="";
 if(d.error)h+=`<div class="log" style="color:#ff9a9a">${d.error}</div>`;
 if(d.outputs&&d.outputs.length){h+='<div class="outs">';
  for(const o of d.outputs){
   if(o.type==="subtitle"||o.type==="text"){
    const isSrt=o.type==="subtitle",icon=isSrt?"CC":"TXT",downloadLabel=isSrt?"Tải file SRT":"Tải bản tóm tắt";
    h+=`<div class="out" data-posted="0"><div style="width:160px;min-height:150px;display:grid;place-items:center;background:var(--card2);font-size:38px;font-weight:700">${icon}</div><div class="m"><div class="out-title"><span>${esc(o.san_pham||o.label||"Tài liệu")}</span></div><div class="out-tags"><span class="tag">${esc(o.label||"Tài liệu")}</span>${isSrt?`<span class="tag">${o.segments||0} đoạn</span>`:""}</div><a class="dl" href="/out/${j.id}/${o.file}" download>↓ ${downloadLabel}</a></div></div>`;
    continue;
   }
   const sceneCounts={};(o.canh||[]).forEach(c=>sceneCounts[c]=(sceneCounts[c]||0)+1);
   const sceneTags=Object.entries(sceneCounts).map(([c,n])=>`<span class="tag">${esc(c)}${n>1?` ×${n}`:""}</span>`).join("");
   const tags=(o.kich_ban?`<span class="tag" style="background:#dcefeb;color:#066b67">${o.kich_ban}</span>`:"")+
     sceneTags+
     (o.style?`<span class="tag">${o.style}</span>`:"")+(o.hook?`<span class="tag">${o.hook}</span>`:"")+
     (o.voice?`<span class="tag">${o.voice}</span>`:"")+(o.flip&&o.flip!="gốc"?`<span class="tag">${o.flip}</span>`:"");
   const fb=o.fb_title?`<div class="fb-post"><div class="fb-post-label"><span>Caption Facebook</span><button class="copy-title" data-title="${esc(o.fb_title)}">Sao chép</button></div><div class="fb-post-text">${esc(o.fb_title)}</div></div>`:"";
   const posts=o.facebook_posts||[];
   const legacy=o.facebook_post_id?[{page_name:"Facebook",post_id:o.facebook_post_id}]:[];
   const postList=posts.concat(legacy).map(p=>`<span class="fb-post-chip">✓ ${esc(p.page_name||"Facebook")} · <a href="https://www.facebook.com/${esc(p.post_id)}" target="_blank" rel="noopener">Xem</a></span>`).join("");
   const postedIds=new Set(posts.map(p=>String(p.page_id))),available=FB_PAGES.filter(p=>!postedIds.has(String(p.id)));
   const pageOptions=available.map(p=>`<option value="${esc(p.id)}">${esc(p.name||p.id)}</option>`).join("");
   const publish=ME.role!=="admin"?"":`${postList?`<div class="fb-post-list">${postList}</div>`:""}<div class="fb-publish"><select class="fb-page-select" ${available.length?'':'disabled'}>${pageOptions||'<option>Chưa có Page khả dụng</option>'}</select><button class="fb-publish-btn" data-job-id="${j.id}" data-file="${esc(o.file)}" ${available.length?'':'disabled'}>Đăng Facebook</button></div>`;
   const noteId=`note-${j.id}-${o.file.replace(/[^a-zA-Z0-9_-]/g,"-")}`;
   const note=`<div class="video-note"><div class="video-note-head"><span>Ghi chú</span><div class="video-note-actions"><button class="copy-video-note" data-note-id="${noteId}">Sao chép</button><button class="save-video-note" data-job-id="${j.id}" data-file="${esc(o.file)}" data-note-id="${noteId}">Lưu</button></div></div><textarea class="video-note-input" id="${noteId}" maxlength="2000" placeholder="Thêm ghi chú...">${esc(o.note||"")}</textarea><label class="posted-check"><input type="checkbox" class="posted-toggle" data-job-id="${j.id}" data-file="${esc(o.file)}" data-note-id="${noteId}" ${o.posted?'checked':''}> Đã đăng</label></div>`;
   h+=`<div class="out" data-posted="${o.posted?'1':'0'}"><video src="/out/${j.id}/${o.file}" controls preload="none" poster="/out/${j.id}/${o.thumb||''}"></video>
     <div class="m"><div class="out-title"><span>${esc(o.san_pham||"Video kết quả")}</span><span class="out-duration">${o.dur||"?"}s</span></div><div class="out-tags">${tags}</div><a class="dl" href="/out/${j.id}/${o.file}" download>↓ Tải video</a>${fb}${publish}${note}</div></div>`;
  }
  h+='</div>';}
 if(d.status=="running"||d.status=="queued")h+=`<div class="log">${(d.log||[]).join("\n")||"..."}</div>`;
 if(d.status=="done"||d.status=="error")h+=`<div style="margin-top:9px"><button class="btn sm ghost del-job" data-job-id="${j.id}">🗑 Xoá</button></div>`;
 box.innerHTML=h;
 applyPostFilter();
}
async function delJob(jobId){if(!confirm("Xoá job + video của nó?"))return;await fetch("/api/job/"+jobId+"/delete",{method:"POST"});delete sigCache[jobId];document.getElementById("job-"+jobId)?.remove();renderJobs(true);}
document.getElementById("jobs").addEventListener("click",async e=>{const btn=e.target.closest(".del-job"),copy=e.target.closest(".copy-title"),copyNote=e.target.closest(".copy-video-note"),save=e.target.closest(".save-video-note"),publish=e.target.closest(".fb-publish-btn");if(btn)delJob(btn.dataset.jobId);if(copy){try{await navigator.clipboard.writeText(copy.dataset.title);toast("✓ Đã sao chép caption Facebook");}catch(_){toast("Không thể sao chép tự động");}}if(copyNote){const note=document.getElementById(copyNote.dataset.noteId)?.value||"";if(!note.trim()){toast("Ghi chú đang trống");}else try{await navigator.clipboard.writeText(note);toast("✓ Đã sao chép ghi chú");}catch(_){toast("Không thể sao chép tự động");}}if(save){const note=document.getElementById(save.dataset.noteId)?.value||"";save.disabled=true;const r=await fetch(`/api/job/${save.dataset.jobId}/output-note`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({file:save.dataset.file,note})});const result=await r.json();save.disabled=false;toast(result.ok?"✓ Đã lưu ghi chú":"⚠ "+(result.err||"Không lưu được"));}if(publish){const pageId=publish.parentElement.querySelector(".fb-page-select").value,page=FB_PAGES.find(p=>String(p.id)===String(pageId));if(!confirm(`Đăng video này lên ${page?.name||"Page đã chọn"}?`))return;const old=publish.textContent;publish.disabled=true;publish.textContent="Đang đăng...";try{const r=await fetch("/api/facebook/publish",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({job_id:publish.dataset.jobId,file:publish.dataset.file,page_id:pageId})});const result=await r.json();if(!result.ok)throw new Error(result.err||"Đăng thất bại");toast(`✓ Đã đăng video lên ${result.page_name}`);const job=jobsData.find(x=>x.id===publish.dataset.jobId);if(job)await loadDetail(job);applyPostFilter();}catch(err){publish.disabled=false;publish.textContent=old;toast("⚠ "+err.message);}}});
document.getElementById("jobs").addEventListener("change",async e=>{const check=e.target.closest(".posted-toggle");if(!check)return;const label=check.closest(".posted-check"),note=document.getElementById(check.dataset.noteId)?.value||"";label.classList.add("saving");check.disabled=true;try{const r=await fetch(`/api/job/${check.dataset.jobId}/output-note`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({file:check.dataset.file,note,posted:check.checked})});const result=await r.json();if(!result.ok)throw new Error(result.err||"Không lưu được");check.closest(".out").dataset.posted=check.checked?"1":"0";toast(check.checked?"✓ Đã đánh dấu đã đăng":"Đã bỏ đánh dấu đã đăng");applyPostFilter();}catch(err){check.checked=!check.checked;toast("⚠ "+(err.message||"Không lưu được"));}finally{check.disabled=false;label.classList.remove("saving");}});
function applyPostFilter(){
 let visible=0;
 document.querySelectorAll("#jobs .job").forEach(job=>{let shown=0;job.querySelectorAll(".out").forEach(out=>{const match=postFilter==="all"||(postFilter==="posted"&&out.dataset.posted==="1")||(postFilter==="unposted"&&out.dataset.posted!=="1");out.style.display=match?"":"none";if(match){shown++;visible++;}});const hasOutputs=job.querySelector(".out");job.style.display=!hasOutputs||postFilter==="all"||shown?"":"none";});
 const count=document.getElementById("jcount");count.textContent=postFilter==="all"?jobsData.filter(j=>curFilter==="all"||j.kind===curFilter).length+" job":visible+" video";
}
function renderJobs(force){
 const box=document.getElementById("jobs");
 let list=jobsData.filter(j=>curFilter=="all"||j.kind==curFilter);
 document.getElementById("jcount").textContent=list.length+" job";
 if(!list.length){box.innerHTML='<div class="empty">Chưa có job nào. Vào Remake hoặc Ghép Review để bắt đầu.</div>';return;}
 if(force&&box.querySelector(".empty"))box.innerHTML="";
 const seen=new Set();
 for(const j of list){seen.add(j.id);
  const sig=j.status+":"+j.n_out+":"+j.log_len;
  let node=document.getElementById("job-"+j.id);
  const isNew=!node;
  if(!node){node=document.createElement("div");node.className="job";node.id="job-"+j.id;box.appendChild(node);}
  if(box.firstChild!==node&&list[0].id==j.id)box.insertBefore(node,box.firstChild);
  if(isNew||sigCache[j.id]!==sig){   // Node vừa tạo phải vẽ, dù signature từng được cache
   node.innerHTML=jobCardHTML(j);sigCache[j.id]=sig;loadDetail(j);
  }
 }
 // gỡ node của job đã bị lọc/xoá
 [...box.children].forEach(c=>{if(c.id&&c.id.startsWith("job-")&&!seen.has(c.id.slice(4))){delete sigCache[c.id.slice(4)];c.remove();}});
 applyPostFilter();
}

async function poll(){
 try{const r=await fetch("/api/jobs");const j=await r.json();jobsData=j.jobs;
  const nq=document.getElementById("navq");if(j.queue>0){nq.style.display="";nq.textContent=j.queue;}else nq.style.display="none";
  document.getElementById("qinfo").textContent=j.queue>0?("Hàng chờ: "+j.queue):"Rảnh";
  if(document.getElementById("v-jobs").classList.contains("on"))renderJobs(false);
  if(document.getElementById("v-home").classList.contains("on"))loadHome();
  if(document.getElementById("v-log").classList.contains("on"))loadLog();
 }catch(e){}
}
setInterval(poll,3000);

async function loadHome(){
 const r=await fetch("/api/overview");const o=await r.json();
 document.getElementById("stats").innerHTML=`
  <div class="stat"><div class="k">Tổng video đã làm</div><div class="v">${o.total_out}</div><div class="bar"></div></div>
  <div class="stat mini"><div class="k">Đang chạy</div><div class="v">${o.running}</div><div class="bar" style="opacity:.5"></div></div>
  <div class="stat mini"><div class="k">Hàng chờ</div><div class="v">${o.queue}</div><div class="bar" style="opacity:.3"></div></div>
  <div class="stat mini"><div class="k">Ổ E còn trống</div><div class="v">${o.disk_free_gb}<span style="font-size:14px;color:var(--mut)"> GB</span></div><div class="bar" style="opacity:.3"></div></div>`;
  if(ME&&ME.role==="admin"&&FB_PAGES.length&&!document.getElementById("facebookOverview").dataset.loaded){syncOverviewPages();loadFacebookOverview();}
 if(ME&&ME.role==="admin"&&!document.getElementById("credits").dataset.loaded)loadCredits();
 const rc=document.getElementById("recent");
 if(!o.recent.length){rc.innerHTML='<div class="empty">Chưa có job nào.</div>';return;}
 rc.innerHTML=o.recent.map(j=>`<div class="kv"><span>${j.kind=="remake"?"🎬":"🎞️"} ${j.product||j.id} <span class="meta">· ${j.n_out} video</span></span>
   <span class="badge2 ${BC[j.status]||''}">${BT[j.status]||j.status}</span></div>`).join("");
}
let FB_OVERVIEW_DATA=null,FB_CHART_METRIC="views",FB_CHART_POINTS=[];
function syncOverviewPages(){const select=document.getElementById("overviewPage"),current=select.value;select.innerHTML=FB_PAGES.map(p=>`<option value="${esc(p.id)}">${esc(p.name||p.id)}</option>`).join("");if(FB_PAGES.some(p=>String(p.id)===String(current)))select.value=current;document.getElementById("facebookOverview").style.display=FB_PAGES.length?"block":"none";}
async function loadFacebookOverview(refresh=false){const area=document.getElementById("facebookOverview"),cards=document.getElementById("facebookOverviewCards"),pageId=document.getElementById("overviewPage").value,days=document.getElementById("overviewDays").value||"28";if(!pageId)return;area.dataset.loaded="1";document.getElementById("facebookOverviewTitle").textContent=`Facebook · ${days} ngày gần nhất`;cards.innerHTML='<div class="empty" style="grid-column:1/-1">Đang tải chỉ số Facebook...</div>';document.getElementById("facebookChart").style.display="none";try{const r=await fetch(`/api/facebook/overview?page_id=${encodeURIComponent(pageId)}&days=${encodeURIComponent(days)}${refresh?'&refresh=1':''}`),d=await r.json();if(!d.ok)throw new Error(d.err||"Không tải được chỉ số");FB_OVERVIEW_DATA=d;const fmt=v=>v===null||v===undefined?"—":Number(v).toLocaleString("vi-VN");cards.innerHTML=`<button type="button" class="fb-overview-card" data-metric="views"><div class="k">Lượt xem</div><div class="v">${fmt(d.views)}</div><p class="d">Số lượt phát hoặc hiển thị nội dung của bạn, gồm thước phim, bài viết, tin và quảng cáo.</p></button><button type="button" class="fb-overview-card" data-metric="interactions"><div class="k">Lượt tương tác</div><div class="v">${fmt(d.interactions)}</div><p class="d">Số lượt bày tỏ cảm xúc, click, bình luận, chia sẻ và lưu trên bài viết.</p></button><button type="button" class="fb-overview-card" data-metric="net_followers"><div class="k">Số người theo dõi thực</div><div class="v">${fmt(d.net_followers)}</div><p class="d">Số người theo dõi trừ số người bỏ theo dõi trong khoảng thời gian đã chọn.</p></button>`;document.getElementById("facebookOverviewRange").textContent=`${d.page_name} · ${new Date(d.since+'T00:00:00').toLocaleDateString('vi-VN')} - ${new Date(d.until+'T00:00:00').toLocaleDateString('vi-VN')}`;selectFacebookChart(FB_CHART_METRIC);}catch(err){cards.innerHTML=`<div class="empty" style="grid-column:1/-1">${esc(err.message)}</div>`;}}
function selectFacebookChart(metric){if(!FB_OVERVIEW_DATA)return;FB_CHART_METRIC=metric;document.querySelectorAll(".fb-overview-card").forEach(el=>el.classList.toggle("on",el.dataset.metric===metric));const names={views:"Lượt xem",interactions:"Lượt tương tác",net_followers:"Số người theo dõi thực"};document.getElementById("facebookChartTitle").textContent=names[metric];document.getElementById("facebookChart").style.display="block";FB_CHART_POINTS=(FB_OVERVIEW_DATA.series&&FB_OVERVIEW_DATA.series[metric])||[];requestAnimationFrame(drawFacebookChart);}
function drawFacebookChart(){const canvas=document.getElementById("facebookChartCanvas"),rect=canvas.getBoundingClientRect(),dpr=Math.min(devicePixelRatio||1,2),ctx=canvas.getContext("2d"),w=Math.max(300,rect.width),h=rect.height;canvas.width=w*dpr;canvas.height=h*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);const p={l:45,r:15,t:16,b:30},vals=FB_CHART_POINTS.map(x=>Number(x.value)||0),lo=Math.min(0,...vals),hi=Math.max(1,...vals),span=hi-lo||1,x=i=>p.l+i*(w-p.l-p.r)/Math.max(1,vals.length-1),y=v=>p.t+(hi-v)*(h-p.t-p.b)/span;ctx.font="10px sans-serif";ctx.strokeStyle="#e4eaed";ctx.fillStyle="#71828c";ctx.lineWidth=1;for(let i=0;i<=4;i++){const yy=p.t+i*(h-p.t-p.b)/4,val=Math.round(hi-i*span/4);ctx.beginPath();ctx.moveTo(p.l,yy);ctx.lineTo(w-p.r,yy);ctx.stroke();ctx.fillText(val.toLocaleString("vi-VN"),4,yy+3);}ctx.fillStyle="#71828c";[0,Math.floor((vals.length-1)/2),vals.length-1].forEach(i=>{if(i>=0&&FB_CHART_POINTS[i])ctx.fillText(new Date(FB_CHART_POINTS[i].date+'T00:00:00').toLocaleDateString('vi-VN',{day:'2-digit',month:'2-digit'}),Math.max(p.l-10,Math.min(w-42,x(i)-14)),h-7);});ctx.strokeStyle="#087f7a";ctx.lineWidth=2;ctx.lineJoin="round";ctx.beginPath();vals.forEach((v,i)=>i?ctx.lineTo(x(i),y(v)):ctx.moveTo(x(i),y(v)));ctx.stroke();ctx.fillStyle="#087f7a";vals.forEach((v,i)=>{ctx.beginPath();ctx.arc(x(i),y(v),2.5,0,Math.PI*2);ctx.fill();});canvas._chart={x,y,w,h,p};}
document.getElementById("facebookOverviewCards").onclick=e=>{const card=e.target.closest(".fb-overview-card");if(card)selectFacebookChart(card.dataset.metric);};
document.getElementById("facebookChartCanvas").onpointermove=e=>{const canvas=e.currentTarget,chart=canvas._chart,tip=document.getElementById("facebookChartTooltip");if(!chart||!FB_CHART_POINTS.length)return;const r=canvas.getBoundingClientRect(),idx=Math.max(0,Math.min(FB_CHART_POINTS.length-1,Math.round((e.clientX-r.left-chart.p.l)/(r.width-chart.p.l-chart.p.r)*(FB_CHART_POINTS.length-1)))),point=FB_CHART_POINTS[idx];tip.textContent=`${new Date(point.date+'T00:00:00').toLocaleDateString('vi-VN')}: ${Number(point.value).toLocaleString('vi-VN')}`;tip.style.display="block";tip.style.left=Math.min(innerWidth-tip.offsetWidth-8,e.clientX+10)+"px";tip.style.top=Math.max(8,e.clientY-tip.offsetHeight-10)+"px";};
document.getElementById("facebookChartCanvas").onpointerleave=()=>document.getElementById("facebookChartTooltip").style.display="none";
window.addEventListener("resize",()=>{if(FB_OVERVIEW_DATA)requestAnimationFrame(drawFacebookChart);});
document.getElementById("overviewPage").onchange=()=>loadFacebookOverview();
document.getElementById("overviewDays").onchange=()=>loadFacebookOverview();
document.getElementById("refreshFacebookOverview").onclick=()=>loadFacebookOverview(true);
function creditCard(name,p){
 const good=p&&p.ok,value=good&&p.credits!==null&&p.credits!==undefined?Number(p.credits).toLocaleString("vi-VN"):"--";
 const meta=good?(p.plan?`Gói ${esc(p.plan)}`:"Đã kết nối"):esc((p&&p.error)||"Không có dữ liệu");
 return `<div class="credit-card"><span class="credit-dot ${good?'':'err'}"></span><div><div class="credit-provider">${name}</div><div class="credit-meta">${meta}</div></div><div class="credit-value">${value}<span class="credit-unit">credits còn lại</span></div></div>`;
}
async function loadCredits(refresh=false){
 if(!ME||ME.role!=="admin")return;
 const area=document.getElementById("creditsArea"),box=document.getElementById("credits");area.style.display="";
 if(!box.dataset.loaded)box.innerHTML='<div class="empty" style="grid-column:1/-1">Đang tải credits...</div>';
 try{const r=await fetch("/api/credits"+(refresh?"?refresh=1":""));const d=await r.json();if(!r.ok)throw new Error();box.innerHTML=creditCard("GenMax",d.providers.genmax)+creditCard("Vivibe",d.providers.vivibe);box.dataset.loaded="1";}catch(_){box.innerHTML='<div class="empty" style="grid-column:1/-1">Không tải được credits.</div>';}
}
document.getElementById("refreshCredits").onclick=()=>loadCredits(true);
let PAGE_POST_AFTER="",PAGE_POST_TOTAL=0;
function syncPostsPages(){const select=document.getElementById("postsPage");if(!select)return;const old=select.value;select.innerHTML=FB_PAGES.map(p=>`<option value="${esc(p.id)}">${esc(p.name||p.id)}</option>`).join("");if(FB_PAGES.some(p=>String(p.id)===old))select.value=old;}
function pagePostHTML(p){const m=p.marketing||{},fmt=v=>v===undefined||v===null?"—":Number(v).toLocaleString("vi-VN"),duration=v=>{if(v===undefined||v===null)return"—";const sec=Math.round(Number(v)/1000);return sec>=60?`${Math.floor(sec/60)}p ${sec%60}s`:`${sec}s`;},when=p.created_time?new Date(p.created_time).toLocaleString("vi-VN"):"",views=m.views??m.video_views,interactions=m.interactions??([m.reactions,m.comments,m.shares].every(v=>v!==undefined)?Number(m.reactions)+Number(m.comments)+Number(m.shares):undefined),image=p.full_picture?`<img class="content-thumb" src="${esc(p.full_picture)}" alt="">`:'<div class="content-thumb"></div>';return `<tr data-post-id="${esc(p.id)}"><td><div class="content-preview">${image}<div><div class="content-title" title="${esc(p.message||"")}">${esc(p.message||"Bài viết không có nội dung chữ")}</div><div class="content-date">Đã đăng · ${when}</div></div></div></td><td><span class="status-ok">Đã đăng</span></td><td class="metric-cell">${fmt(views)}</td><td class="metric-cell">${fmt(m.viewers)}</td><td class="metric-cell">${fmt(interactions)}</td><td class="metric-cell">${fmt(m.net_followers)}</td><td class="metric-cell">${fmt(m.impressions)}</td><td class="metric-cell">${fmt(m.comments)}</td><td class="metric-cell">${fmt(m.distribution)}</td><td class="metric-cell">${duration(m.watch_time_ms)}</td><td class="metric-cell">${duration(m.avg_watch_time_ms)}</td><td class="metric-cell">${fmt(m.views_3s)}</td><td class="metric-cell">${fmt(m.views_1m)}</td><td><div class="table-actions">${p.permalink_url?`<a class="btn sm ghost" href="${esc(p.permalink_url)}" target="_blank" rel="noopener">Mở</a>`:""}<button class="btn sm ghost delete-page-post" data-id="${esc(p.id)}">Xoá</button></div></td></tr>`;}
async function loadFacebookPosts(reset=false){
 if(!ME||ME.role!=="admin")return;if(!FB_PAGES.length)await loadSettings();syncPostsPages();const pageId=document.getElementById("postsPage").value,box=document.getElementById("pagePosts"),more=document.getElementById("morePagePosts");if(!pageId){box.innerHTML='<tr><td colspan="14" class="empty">Hãy thêm Facebook Page trong Cài đặt.</td></tr>';more.style.display="none";return;}if(reset){PAGE_POST_AFTER="";PAGE_POST_TOTAL=0;box.innerHTML='<tr><td colspan="14" class="empty">Đang tải bài viết...</td></tr>';}
 try{const url="/api/facebook/posts?page_id="+encodeURIComponent(pageId)+(PAGE_POST_AFTER?"&after="+encodeURIComponent(PAGE_POST_AFTER):"");const r=await fetch(url),d=await r.json();if(!d.ok)throw new Error(d.err||"Không tải được bài viết");if(reset)box.innerHTML="";box.insertAdjacentHTML("beforeend",(d.posts||[]).map(pagePostHTML).join(""));PAGE_POST_TOTAL+=(d.posts||[]).length;PAGE_POST_AFTER=d.after||"";document.getElementById("pagePostCount").textContent=PAGE_POST_TOTAL+" bài";more.style.display=d.has_more&&PAGE_POST_AFTER?"block":"none";if(!PAGE_POST_TOTAL)box.innerHTML='<tr><td colspan="14" class="empty">Page chưa có bài viết.</td></tr>';}catch(err){box.innerHTML=`<tr><td colspan="14" class="empty">${esc(err.message)}</td></tr>`;more.style.display="none";}
}
document.getElementById("postsPage").onchange=()=>loadFacebookPosts(true);
document.getElementById("refreshPagePosts").onclick=()=>loadFacebookPosts(true);
document.getElementById("morePagePosts").onclick=()=>loadFacebookPosts(false);
document.getElementById("pagePosts").onclick=async e=>{const btn=e.target.closest(".delete-page-post");if(!btn||!confirm("Xoá bài viết này khỏi Facebook Page?"))return;btn.disabled=true;const pageId=document.getElementById("postsPage").value,r=await fetch("/api/facebook/posts/"+encodeURIComponent(btn.dataset.id)+"?page_id="+encodeURIComponent(pageId),{method:"DELETE"}),d=await r.json();if(d.ok){btn.closest("tr").remove();PAGE_POST_TOTAL=Math.max(0,PAGE_POST_TOTAL-1);document.getElementById("pagePostCount").textContent=PAGE_POST_TOTAL+" bài";toast("✓ Đã xoá bài viết");}else{btn.disabled=false;toast("⚠ "+(d.err||"Không xoá được bài"));}};
async function loadLog(){const r=await fetch("/api/log");const j=await r.json();
 const el=document.getElementById("syslog");el.textContent=(j.lines||[]).join("\n")||"(trống)";}

/* settings */
async function loadSettings(){const r=await fetch("/api/settings");const s=await r.json();
 document.querySelectorAll("[data-k]").forEach(el=>{const v=s[el.dataset.k];if(v!==undefined)el.value=v;});
 document.querySelectorAll("[data-s]").forEach(el=>{const v=s[el.dataset.s];if(v!==undefined)el.value=v;});
 document.querySelectorAll("[data-provider]").forEach(el=>el.value=s.tts_provider||"vivibe");
 document.querySelectorAll("[data-provider]").forEach(syncProviderVoices);
 if(s.tts_provider==="vivibe")document.querySelectorAll('select[name="voice"],[data-s="voice"],[data-s="review_voice"]').forEach(el=>{if(el.value==="adam3")el.value="auto";});
 const ks=document.getElementById("vivibeKeyState");if(ks)ks.textContent=s.vivibe_key_set?"✅ Đã có API key":"⚠ Chưa có API key";
 const gs=document.getElementById("genmaxKeyState");if(gs)gs.textContent=s.genmax_key_set?"✅ Đã có API key":"⚠ Chưa có API key";
 const ds=document.getElementById("douyinCookieState");if(ds)ds.textContent=s.douyin_cookie_set?"✅ Đã có Douyin Cookie":"⚠ Chưa có Douyin Cookie";
 const bs=document.getElementById("bilibiliCookieState");if(bs)bs.textContent=s.bilibili_cookie_set?"✅ Đã có Bilibili Cookie":"⚠ Chưa có Bilibili Cookie";
 FB_PAGES=s.facebook_pages||[];syncPostsPages();const fs=document.getElementById("facebookState");if(fs)fs.textContent=FB_PAGES.length?`✅ Đã cấu hình ${FB_PAGES.length} Page`:"⚠ Chưa có Facebook Page";renderFacebookPages();}
function renderFacebookPages(){const box=document.getElementById("facebookPageList");if(!box)return;box.innerHTML=FB_PAGES.length?FB_PAGES.map(p=>`<div class="fb-page-row"><span><b>${esc(p.name||p.id)}</b><small>${esc(p.id)}</small></span><button class="btn sm ghost test-fb-page" data-id="${esc(p.id)}">Kiểm tra</button><button class="btn sm ghost delete-fb-page" data-id="${esc(p.id)}">Xoá</button></div>`).join(""):'<div class="meta">Chưa thêm Page nào.</div>';}
document.getElementById("saveSet").onclick=async()=>{
 const d={};document.querySelectorAll("[data-s]").forEach(el=>d[el.dataset.s]=el.value);
 await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(d)});
 toast("✅ Đã lưu cài đặt");loadSettings();};
document.getElementById("saveVivibe").onclick=async()=>{const el=document.getElementById("vivibeKey");const key=el.value.trim();
 if(!key){toast("⚠ Hãy dán Vivibe API key");return;}
 const r=await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({vivibe_api_key:key})});
 const s=await r.json();if(s.vivibe_key_set){el.value="";toast("✅ Đã lưu Vivibe API key");loadSettings();}else toast("⚠ Không lưu được API key");};
document.getElementById("saveGenmax").onclick=async()=>{const el=document.getElementById("genmaxKey"),key=el.value.trim();if(!key){toast("⚠ Hãy dán GenMax API key");return;}const r=await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({genmax_api_key:key})}),s=await r.json();if(s.genmax_key_set){el.value="";toast("✅ Đã lưu GenMax API key");loadSettings();document.getElementById("credits").dataset.loaded="";}else toast("⚠ Không lưu được API key");};
document.getElementById("saveDouyinCookie").onclick=async()=>{const el=document.getElementById("douyinCookie"),cookie=el.value.trim();if(!cookie){toast("⚠ Hãy dán Douyin Cookie");return;}const r=await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({douyin_cookie:cookie})}),s=await r.json();if(s.douyin_cookie_set){el.value="";toast("✅ Đã lưu Douyin Cookie");loadSettings();}else toast("⚠ Không lưu được Douyin Cookie");};
document.getElementById("saveBilibiliCookie").onclick=async()=>{const el=document.getElementById("bilibiliCookie"),cookie=el.value.trim();if(!cookie){toast("⚠ Hãy dán Bilibili Cookie");return;}const r=await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({bilibili_cookie:cookie})}),s=await r.json();if(s.bilibili_cookie_set){el.value="";toast("✅ Đã lưu Bilibili Cookie");loadSettings();}else toast("⚠ Không lưu được Bilibili Cookie");};
document.getElementById("saveFacebook").onclick=async()=>{const pageId=document.getElementById("facebookPageId").value.trim(),pageName=document.getElementById("facebookPageName").value.trim(),token=document.getElementById("facebookToken").value.trim();if(!pageId){toast("⚠ Hãy nhập Page ID");return;}const payload={facebook_page_id:pageId,facebook_page_name:pageName};if(token)payload.facebook_page_access_token=token;const r=await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});const s=await r.json();if(s.facebook_key_set){document.getElementById("facebookToken").value="";document.getElementById("facebookPageId").value="";document.getElementById("facebookPageName").value="";toast("✅ Đã lưu Facebook Page");await loadSettings();Object.keys(sigCache).forEach(k=>delete sigCache[k]);renderJobs(true);}else toast("⚠ Page mới cần Page Access Token");};
document.getElementById("facebookPageList").onclick=async e=>{const test=e.target.closest(".test-fb-page"),del=e.target.closest(".delete-fb-page");if(test){test.disabled=true;const r=await fetch("/api/facebook/test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({page_id:test.dataset.id})});const s=await r.json();test.disabled=false;toast(s.ok?`✅ Đã kết nối: ${s.page_name}`:`⚠ ${s.err||"Kết nối thất bại"}`);}if(del&&confirm("Xoá Page này khỏi tool?")){await fetch("/api/facebook/pages/"+encodeURIComponent(del.dataset.id),{method:"DELETE"});await loadSettings();Object.keys(sigCache).forEach(k=>delete sigCache[k]);renderJobs(true);}};
fetch("/api/overview").then(r=>r.json()).then(o=>{const e=document.getElementById("outdir");if(e)e.textContent=o.out_dir||"output/";});
document.getElementById("openOut").onclick=async()=>{const r=await fetch("/api/open_output",{method:"POST"});const j=await r.json();toast(j.ok?"📂 Đã mở thư mục":"⚠ "+(j.err||"lỗi"));};

document.getElementById("logout").onclick=async()=>{await fetch("/api/logout",{method:"POST"});location.href="/login";};
document.getElementById("changePass").onclick=async()=>{const old_password=document.getElementById("oldPass").value,new_password=document.getElementById("newPass").value;const r=await fetch("/api/change-password",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({old_password,new_password})});const j=await r.json();toast(j.ok?"✅ Đã đổi mật khẩu":"⚠ "+(j.err||"Lỗi"));if(j.ok){document.getElementById("oldPass").value="";document.getElementById("newPass").value="";ME.must_change_password=false;}};
async function loadUsers(){const r=await fetch("/api/users");if(!r.ok)return;const j=await r.json();document.getElementById("userList").innerHTML=j.users.map(u=>`<div class="kv"><span><b style="color:var(--fg)">${u.username}</b> · ${u.role}</span><span>${u.username!==ME.username?`<button class="btn sm ghost reset-user" data-u="${u.username}">Đặt lại MK</button> <button class="btn sm ghost delete-user" data-u="${u.username}">Xoá</button>`:"Đang dùng"}</span></div>`).join("");}
document.getElementById("addUser").onclick=async()=>{const username=document.getElementById("newUsername").value,password=document.getElementById("newUserPass").value,role=document.getElementById("newRole").value;const r=await fetch("/api/users",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username,password,role})});const j=await r.json();toast(j.ok?"✅ Đã tạo tài khoản":"⚠ "+(j.err||"Lỗi"));if(j.ok){document.getElementById("newUsername").value="";document.getElementById("newUserPass").value="";loadUsers();}};
document.getElementById("userList").onclick=async e=>{const reset=e.target.closest(".reset-user"),del=e.target.closest(".delete-user");if(reset){const password=prompt("Mật khẩu mới cho "+reset.dataset.u+" (ít nhất 8 ký tự):");if(!password)return;const r=await fetch("/api/users/"+encodeURIComponent(reset.dataset.u),{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({password})});const j=await r.json();toast(j.ok?"✅ Đã đặt lại mật khẩu":"⚠ "+(j.err||"Lỗi"));}if(del&&confirm("Xoá tài khoản "+del.dataset.u+"? Video cũ vẫn được giữ.")){const r=await fetch("/api/users/"+encodeURIComponent(del.dataset.u),{method:"DELETE"});const j=await r.json();toast(j.ok?"✅ Đã xoá tài khoản":"⚠ "+(j.err||"Lỗi"));if(j.ok)loadUsers();}};

async function boot(){const r=await fetch("/api/me");if(r.status===401){location.href="/login";return;}ME=await r.json();document.getElementById("who").textContent=ME.username;document.getElementById("role").textContent=ME.role.toUpperCase();document.getElementById("adminUsers").style.display=ME.role==="admin"?"":"none";const today=new Date().toISOString().slice(0,10),day=document.getElementById("discoverDate");day.value=today;day.max=today;buildNav();if(ME.role==="admin")loadSettings();poll();const h=location.hash.slice(1);go(h&&NAV.some(x=>x[0]==h)?h:(ME.must_change_password?"account":"home"));if(ME.must_change_password)toast("⚠ Hãy đổi mật khẩu mặc định ngay");}
boot();
</script></body></html>"""

# điền option cho các select (dùng chung)
def _opts(pairs, sel=None):
    return "".join(f'<option value="{v}"{" selected" if v==sel else ""}>{t}</option>' for v,t in pairs)
_NICHE=[("co-khi","Cơ khí"),("gia-dung","Gia dụng"),("nong-nghiep","Nông nghiệp"),
        ("cong-nghe","Công nghệ"),("thoi-trang","Thời trang"),("my-pham","Mỹ phẩm")]
_NUM=[(str(n),str(n)) for n in (1,2,3,4,5,6,7,8,10,12,15)]
_VOICE=[("auto","Tự động (theo niche)"),
        ("vbee_chinh","Chinh review (Vivibe)"),("vbee_ly","Ly tâm lý (Vivibe)"),("vbee_ha","Hà review (Vivibe)"),
        ("nam","Nam tự động (Vivibe)"),("nu","Nữ tự động (Vivibe)"),("nam1","nam1 (Vivibe)"),("nam3","nam3 (Vivibe)"),
        ("nam4","nam4 (Vivibe)"),("nam5","nam5 (Vivibe)"),("nam6","nam6 (Vivibe)"),("nu2","nu2 (Vivibe)"),
        ("adam3","Adam - Dominant, Firm (GenMax)"),("genmax_vu","Vũ - Northern region (GenMax)"),("genmax_ninhdon","Ninh Đôn – Deep & Warm (GenMax)"),("genmax_duyen","Duyên (GenMax)"),
        ("genmax_anhthu","Anh Thư (GenMax)"),("genmax_mai","Mai (GenMax)"),("genmax_thanhngoc","Thanh Ngọc (GenMax)"),
        ("genmax_minimax_serene_man","Serene Man (MiniMax/GenMax)"),("genmax_minimax_crisp_announcer_v2","Crisp Announcer v2 (MiniMax/GenMax)"),
        ("genmax_capcut_cute_female","Cute Female (CapCut/GenMax)"),("genmax_capcut_sweet_girl","Sweet Little Girl (CapCut/GenMax)"),
        ("genmax_capcut_female_standard","Giọng nữ phổ thông (CapCut/GenMax)"),("genmax_capcut_confident_male","Confident Male (CapCut/GenMax)"),
        ("genmax_capcut_chi_mai","Chí Mai (CapCut/GenMax)"),("genmax_capcut_trung_caha","Trung Caha (CapCut/GenMax)"),
        ("genmax_capcut_nam_tram","Nam Trầm (CapCut/GenMax)"),("genmax_capcut_ly_nam","Ly Nam (CapCut/GenMax)"),
        ("genmax_capcut_duy_bac","Duy Bắc (CapCut/GenMax)"),("genmax_capcut_ha_nu","Hà Nữ (CapCut/GenMax)"),
        ("genmax_capcut_sai_nu","Sài Nữ (CapCut/GenMax)"),("genmax_capcut_anh_dung","Anh Dũng (CapCut/GenMax)")]
_HOOK=[("auto","Tự động (não chọn)"),("pain_point","Nỗi đau"),("ket_qua_gay_to_mo","Kết quả tò mò"),
       ("cau_hoi","Câu hỏi"),("tinh_huong_that","Tình huống thật")]
_FLIP=[("auto","Tự động xoay vòng"),("none","Giữ gốc"),("hflip","Lật gương"),("zoom","Zoom 12%"),
       ("hflip_zoom","Lật + zoom")]
_STYLE=[("auto","Tự động"),("pain","Nỗi đau"),("cau_hoi","Câu hỏi"),("ket_qua","Kết quả tò mò"),
        ("tinh_huong","Tình huống"),("persona_tho","Vai thợ"),("persona_moi","Người mới dùng"),
        ("tra_loi_cmt","Trả lời comment"),("canh_bao","Cảnh báo ngược"),("mach_chi_em","Mách chị em"),
        ("ke_chuyen","Kể chuyện")]
_FW=[("auto","Tự động (mỗi bản 1 kịch bản khác)"),("review_demo","Review + Demo"),
     ("test_real","Test thực tế"),("before_after","Before – After"),("storytelling","Storytelling"),
     ("tips_hack","Tips/Hack"),("asmr","ASMR Cleaning"),("lifestyle","Lifestyle")]
PAGE = (PAGE.replace("%NICHE%",_opts(_NICHE)).replace("%NUM%",_opts(_NUM,"3"))
        .replace("%VOICE%",_opts(_VOICE)).replace("%HOOK%",_opts(_HOOK))
        .replace("%FLIP%",_opts(_FLIP)).replace("%STYLE%",_opts(_STYLE))
        .replace("%FRAMEWORK%",_opts(_FW)))

if __name__ == "__main__":
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Video Studio -> http://127.0.0.1:7995")
    app.run(host="127.0.0.1", port=7995, threaded=True)
