# -*- coding: utf-8 -*-
"""VIDEO STUDIO — LÕI CHUNG (engine).
Tái dùng bộ máy autovoice (render_autovoice.py + codex vision + demucs + blur + Vivibe),
KHÔNG đăng bài. Dùng cho 2 nhánh:
  - remake.py : 1 video -> nhiều biến thể (chọn hook/giọng/lật thủ công)
  - review.py : nhiều video 1 sản phẩm -> cắt hook+sales, ghép video review mới + voice theo cảnh

Độc lập với autopilot: chỉ GỌI các script ngoài (renderer, codex, ffmpeg, demucs, detect_blur),
không import pipeline.py (tránh side-effect wrap stdout).
"""
import sys, os, re, json, time, random, subprocess, glob, shutil

# ---- ĐƯỜNG DẪN PORTABLE: mọi thứ tính TƯƠNG ĐỐI theo thư mục gói + config.json ----
# app/ nằm trong <ROOT>; vendor/ chứa các script gốc (render_autovoice, detect_blur, ...).
APP_DIR   = os.path.dirname(os.path.abspath(__file__))
ROOT      = os.path.dirname(APP_DIR)                                    # <...>/video-studio
VENDOR    = os.path.join(ROOT, "vendor")

def _prepare_windows_tools():
    """Bổ sung các vị trí cài phổ biến khi PATH của shortcut/cửa sổ cũ chưa cập nhật."""
    if os.name != "nt":
        return
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    paths = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs"),
        os.path.join(roaming, "npm"),
    ]
    paths += glob.glob(os.path.join(
        local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "ffmpeg-*", "bin"))
    current = os.environ.get("PATH", "").split(os.pathsep)
    for p in reversed([p for p in paths if p and os.path.isdir(p)]):
        if p.lower() not in {x.lower() for x in current}:
            current.insert(0, p)
    os.environ["PATH"] = os.pathsep.join(current)

_prepare_windows_tools()

def _load_config():
    """Đọc config.json (đường dẫn codex, provider TTS, API key, thư mục output)."""
    p = os.path.join(ROOT, "config.json")
    if os.path.exists(p):
        try: return json.load(open(p, encoding="utf-8"))
        except Exception: pass
    return {}
CONFIG    = _load_config()

VIDEO_ENCODER = (os.environ.get("VS_VIDEO_ENCODER") or CONFIG.get("video_encoder") or "qsv").strip().lower()
if VIDEO_ENCODER not in ("qsv", "cpu"):
    VIDEO_ENCODER = "qsv"
os.environ["VS_VIDEO_ENCODER"] = VIDEO_ENCODER

def video_encode_args(quality=20):
    """Tham số encode H.264; QSV dùng Intel Iris Xe, CPU là fallback tương thích."""
    if VIDEO_ENCODER == "qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", str(quality + 2)]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(quality)]

# THƯ MỤC ĐẦU RA VIDEO: config.json "output_dir" (đường dẫn tuỳ ý) hoặc mặc định output/ trong gói.
OUT_DIR   = (os.environ.get("VS_OUTPUT_DIR") or CONFIG.get("output_dir") or "").strip() or os.path.join(ROOT, "output")
IN_DIR    = os.path.join(ROOT, "input")
JOB_DIR   = os.path.join(ROOT, "jobs")
WORK_DIR  = os.path.join(ROOT, "work")
LOG_DIR   = os.path.join(ROOT, "logs")
for _d in (IN_DIR, OUT_DIR, JOB_DIR, WORK_DIR, LOG_DIR):
    try: os.makedirs(_d, exist_ok=True)
    except Exception: pass

VPY       = sys.executable                                              # python trong venv
# codex CLI (bộ não phân tích/viết lời). Ưu tiên npm shim chạy được; tránh alias
# WindowsApps của app Codex vì subprocess có thể nhìn thấy nhưng bị WinError 5.
def _resolve_codex():
    configured = (os.environ.get("CODEX_CMD") or CONFIG.get("codex_cmd") or "").strip()
    if configured and configured.lower() not in ("codex", "codex.exe", "codex.cmd"):
        return configured
    npm_shim = os.path.join(os.environ.get("APPDATA", ""), "npm", "codex.cmd")
    if os.path.isfile(npm_shim):
        return npm_shim
    return shutil.which("codex.cmd") or shutil.which("codex") or configured or "codex"

CDX       = _resolve_codex()
RENDERER  = os.path.join(VENDOR, "render_autovoice.py")                 # render voice + mix (autovoice)
BLUR_DET  = os.path.join(VENDOR, "detect_blur.py")                      # blur chữ Trung (tuỳ chọn)
AVW       = os.path.join(VENDOR, "autovoice_web.py")                    # learned_voice_directive
sys.path.insert(0, VENDOR)                                              # fb_download.download

# TTS provider + key: config.json -> env (render_autovoice đọc từ env)
os.environ.setdefault("AUTOVOICE_TTS_PROVIDER", CONFIG.get("tts_provider", "vivibe"))
if CONFIG.get("vivibe_api_key"):
    os.environ.setdefault("VIVIBE_API_KEY", CONFIG["vivibe_api_key"])
if CONFIG.get("voicehay_api_key"):
    os.environ.setdefault("VOICEHAY_API_KEY", CONFIG["voicehay_api_key"])
if CONFIG.get("vivibe_speed"):
    os.environ.setdefault("VIVIBE_SPEED", str(CONFIG["vivibe_speed"]))
if CONFIG.get("genmax_api_key"):
    os.environ.setdefault("GENMAX_API_KEY", CONFIG["genmax_api_key"])
if CONFIG.get("genmax_voice_id"):
    os.environ.setdefault("GENMAX_VOICE_ID", CONFIG["genmax_voice_id"])
if CONFIG.get("genmax_model"):
    os.environ.setdefault("GENMAX_MODEL", CONFIG["genmax_model"])
if CONFIG.get("genmax_speed"):
    os.environ.setdefault("GENMAX_SPEED", str(CONFIG["genmax_speed"]))
os.environ["PYTHONIOENCODING"] = "utf-8"

# ---- GIỌNG VIVIBE (id + nhãn) ----
VOICES_NAM = [("24oEtXGic7NhDjXzmDbDvt","nam1"),("hKHXND5SU1srscKrYiUHFZ","nam3"),
              ("iZfLW5F2GNy72XZ3gQc4xE","nam4"),("6ucdzNGUe4AMtKVTu3w5Lm","nam5"),
              ("2VbzfSGG7i95RpgWZnLZe8","nam6")]
VOICES_NU  = [("8VXsCLxU7Pn55ADXQc6sAf", "vbee_chinh"),
              ("8pnEtqZhpNr2wJnCrmVjq5", "vbee_ly"),
              ("shAfRJNufJUhQSgJUL8NST", "vbee_ha"),
              ("wZtg5gNqQB8YTqHmE64KM2", "nu2")]
GENMAX_VOICES = {
    "adam3": ("pNInz6obpgDQGcFmaJgB", "Adam - Dominant, Firm (GenMax)"),
    "genmax_duyen": ("DVQIYWzpAqd5qcoIlirg", "Duyen (GenMax)"),
    "genmax_anhthu": ("FRYq6nepIbR0lbu7Lus4", "Anh Thu (GenMax)"),
    "genmax_mai": ("d5HVupAWCwe4e6GvMCAL", "Mai (GenMax)"),
    "genmax_thanhngoc": ("Na15FlRRkMEDtEW4nVVP", "Thanh Ngoc (GenMax)"),
}
SPECIAL_VOICES = dict(GENMAX_VOICES)
VOICE_BY_LABEL = {lb: vid for vid, lb in (VOICES_NAM + VOICES_NU)}
VOICE_BY_LABEL.update({label: voice_id for label, (voice_id, _display) in SPECIAL_VOICES.items()})
VOICE_BY_LABEL["nu1"] = "8VXsCLxU7Pn55ADXQc6sAf"
ALL_VOICES = VOICES_NAM + VOICES_NU

GENMAX_EMOTION_GUIDE = """EMOTION GENMAX ADAM (chọn đúng ngữ cảnh, tổng 2-5 tag/video):
- Vui/hài: [excited], [laughing], [giggles], [proud].
- Bất ngờ/chuyển ý: [surprised], [realization], [gasping], [questioning].
- Kể chuyện tự nhiên: [conversational], [storytelling], [thoughtful].
- Nhấn hook/CTA: [speaking fast], [yelling], [shouting], [announcer style].
- Chọc cười nhẹ: [scoff], [sighs], [clears throat].
- Chỉ dùng khi format thật sự cần: [whisper], [ASMR style].
Không dùng [long pause]; hạn chế tag buồn/mệt như [exhausted], [sniffles], [groan].
Đặt tag ngay trước câu cần biểu cảm, không đọc tên tag thành lời, không xếp nhiều tag liên tiếp."""

def voice_from_choice(choice, niche="co-khi", i=0):
    """choice: 'auto' | 'nam' | 'nu' | nhãn cụ thể (nam1/nu2...). Trả (voice_id, label)."""
    provider = (os.environ.get("AUTOVOICE_TTS_PROVIDER") or CONFIG.get("tts_provider") or "vivibe").lower()
    if provider == "genmax":
        if choice in GENMAX_VOICES:
            return GENMAX_VOICES[choice]
        if choice and re.fullmatch(r"[A-Za-z0-9_-]{16,}", choice):
            return choice, choice
        return (os.environ.get("GENMAX_VOICE_ID") or CONFIG.get("genmax_voice_id") or SPECIAL_VOICES["adam3"][0],
                SPECIAL_VOICES["adam3"][1])
    if choice in GENMAX_VOICES:
        choice = "auto"
    if choice and choice in SPECIAL_VOICES:
        return SPECIAL_VOICES[choice]
    if choice and choice in VOICE_BY_LABEL:
        return VOICE_BY_LABEL[choice], choice
    # Cho phép API/UI truyền thẳng Vivibe voice ID mà không bị rơi về giọng auto.
    if choice and re.fullmatch(r"[A-Za-z0-9_-]{16,}", choice):
        return choice, choice
    if choice == "nu":
        return VOICES_NU[i % len(VOICES_NU)]
    if choice == "nam":
        return VOICES_NAM[i % len(VOICES_NAM)]
    # Auto: nhóm gia dụng/làm đẹp/thời trang nghiêng nữ, các nhóm còn lại nghiêng nam.
    if niche in ("gia-dung", "thoi-trang", "my-pham") and random.random() < 0.75:
        return VOICES_NU[i % len(VOICES_NU)]
    return VOICES_NAM[i % len(VOICES_NAM)]

# ---- BIẾN ĐỔI HÌNH (lật/zoom) — copy từ pipeline autopilot ----
VISUAL_XFORMS = ["none", "hflip", "hflip_zoom", "zoom", "hflip_zoom2", "zoom2_warm"]
XFORM_LABEL = {"none":"gốc","hflip":"lật gương","zoom":"zoom 12%","hflip_zoom":"lật+zoom 10%",
               "hflip_zoom2":"lật+zoom 15%","zoom2_warm":"zoom 14% + ấm màu"}
def _xform_filter(xf):
    return {
        "hflip":       "hflip",
        "zoom":        "crop=iw*0.88:ih*0.88,scale=iw/0.88:ih/0.88",
        "hflip_zoom":  "hflip,crop=iw*0.90:ih*0.90,scale=iw/0.90:ih/0.90",
        "hflip_zoom2": "hflip,crop=iw*0.85:ih*0.85,scale=iw/0.85:ih/0.85",
        "zoom2_warm":  "crop=iw*0.86:ih*0.86,scale=iw/0.86:ih/0.86,eq=saturation=1.12:brightness=0.04",
    }.get(xf)

def faststart(path):
    """Remux đưa moov về đầu file (faststart) -> player/Telegram đọc kích thước & phát ngay."""
    fs = path + ".fs.mp4"
    subprocess.run(["ffmpeg","-y","-v","error","-i",path,"-c","copy","-movflags","+faststart",fs],
                   capture_output=True, timeout=300)
    if os.path.exists(fs) and os.path.getsize(fs) > 10000:
        os.replace(fs, path); return True
    if os.path.exists(fs): os.remove(fs)
    return False

def apply_xform(path, xf):
    filt = _xform_filter(xf)
    if not filt: return False
    tmp = path + ".xf.mp4"
    subprocess.run(["ffmpeg","-y","-i",path,"-vf",filt,*video_encode_args(),
                    "-c:a","copy","-movflags","+faststart",tmp],
                   capture_output=True, timeout=600)
    if os.path.exists(tmp) and os.path.getsize(tmp) > 10000:
        os.replace(tmp, path); return True
    if os.path.exists(tmp): os.remove(tmp)
    return False

# ---- HOOK / KIỂU NÓI (copy từ pipeline autopilot, giữ đồng bộ luật content) ----
HOOKS = ["pain_point","ket_qua_gay_to_mo","cau_hoi","tinh_huong_that"]
HOOK_LABEL = {"pain_point":"Nỗi đau/khó chịu","ket_qua_gay_to_mo":"Kết quả gây tò mò",
              "cau_hoi":"Câu hỏi","tinh_huong_that":"Tình huống thật"}
STYLE_FB = """
CÔNG THỨC ĐANG ĂN KHÁCH (học từ reel Việt 60-69K view — BÁM SÁT):
1. BA GIÂY ĐẦU vào thẳng: chỉ cảnh vấn đề trên hình rồi cho sản phẩm xử lý NGAY, hoặc lời hứa gọn rồi chứng minh liền.
2. LỜI BÁM TỪNG CẢNH: mỗi công dụng = 1 câu ngắn, nói ĐÚNG LÚC cảnh đó diễn ra (lắp/mở → cho thấy → dùng thật → cất gọn).
3. NHỒI ĐỦ 3-5 công dụng, đừng tả 1 ý dài dòng.
4. MỘT CON SỐ dễ nhớ nếu hình cho thấy. Cụ thể hơn tính từ.
5. VĂN PHONG thân mật kiểu mách nhau món vừa mua, câu ngắn nhịp nhanh, không đoạn chết. Nói LỢI ÍCH THẤY ĐƯỢC.
6. CHỐT bằng cái người mua quan tâm, nhẹ nhàng không hô hào.
7. NẾU CHỈ 1 SẢN PHẨM: xoáy sâu đúng món đó, không kể lan sang đồ khác.
"""
KIEU_NOI = {
 "pain":        "MỞ BẰNG NỖI KHÓ CHỊU thật đang thấy trên hình, rồi cho sản phẩm xử lý ngay.",
 "cau_hoi":     "MỞ BẰNG CÂU HỎI trúng tình huống người xem hay gặp, rồi mới vào sản phẩm.",
 "ket_qua":     "MỞ BẰNG KẾT QUẢ sướng nhất khiến người xem tò mò, rồi mới lộ sản phẩm.",
 "tinh_huong":  "KỂ MỘT TÌNH HUỐNG đời thường cụ thể, rồi dẫn vào sản phẩm.",
 "persona_tho": "VAI THỢ LÂU NĂM: nói như thợ tư vấn, tone kỹ thuật gọn chắc, nhấn lắp đặt/chịu lực/vật liệu.",
 "persona_moi": "VAI NGƯỜI MỚI DÙNG LẦN ĐẦU: hơi bất ngờ, thật thà, kể vừa thử thấy gì.",
 "tra_loi_cmt": "FORMAT TRẢ LỜI COMMENT: mở 'Nhiều người hỏi ... có ... không', rồi trả lời bằng chi tiết thật.",
 "canh_bao":    "FORMAT CẢNH BÁO NGƯỢC: mở 'Đừng mua cái này nếu bạn...' rồi lật thành điểm mạnh.",
 "mach_chi_em": "MÁCH CHỊ EM: gọi người xem vào đúng cảnh khó chịu, khẩu ngữ đời thường nhiệt, câu ngắn cảm thán.",
 "ke_chuyen":   "KỂ CHUYỆN NHÀ MÌNH: mở bằng chuyện thật hồi chưa có sản phẩm, có mốc thời gian, cảm xúc thật.",
}
STYLE_POOL = {
 "gia-dung":    ["pain","cau_hoi","ket_qua","tinh_huong","persona_moi","tra_loi_cmt","canh_bao","mach_chi_em","ke_chuyen"],
 "co-khi":      ["pain","cau_hoi","ket_qua","tinh_huong","persona_tho","persona_moi","tra_loi_cmt","canh_bao"],
 "nong-nghiep": ["pain","cau_hoi","ket_qua","tinh_huong","persona_tho","tra_loi_cmt","canh_bao"],
 "cong-nghe":    ["pain","cau_hoi","ket_qua","tinh_huong","persona_moi","tra_loi_cmt","canh_bao"],
 "thoi-trang":   ["cau_hoi","ket_qua","tinh_huong","persona_moi","tra_loi_cmt","canh_bao","mach_chi_em","ke_chuyen"],
 "my-pham":      ["pain","cau_hoi","ket_qua","tinh_huong","persona_moi","tra_loi_cmt","canh_bao","mach_chi_em","ke_chuyen"],
}
def style_text(key):
    k = KIEU_NOI.get(key)
    return STYLE_FB + ("\n\nKIỂU NÓI BẮT BUỘC BẢN NÀY:\n" + k if k else "")

def facebook_title(product, script, hook_type="", niche="", product_link=""):
    """Tạo caption chi tiết từ tên sản phẩm và lời thoại của video, không gọi thêm Codex."""
    import unicodedata
    product = " ".join(str(product or "sản phẩm trong video").split())[:100]
    openings = {
        "pain_point": f"Ai đang gặp đúng vấn đề này thì nên xem {product} xử lý thực tế thế nào!",
        "ket_qua_gay_to_mo": f"Kết quả thực tế khi dùng {product} có đáng mong đợi không?",
        "cau_hoi": f"{product} có thực sự tiện và đáng dùng như mọi người vẫn nói?",
        "tinh_huong_that": f"Thử {product} trong tình huống thực tế mới thấy những điểm đáng chú ý.",
    }
    title = openings.get(hook_type, f"{product} có gì đáng chú ý? Cùng xem cách sản phẩm hoạt động trong video thực tế.")

    # Bỏ emotion tag của GenMax và lấy các ý thật từ chính lời thoại video.
    clean_script = re.sub(r"\[[^\]\n]{1,40}\]", " ", str(script or ""))
    clean_script = re.sub(r"\s+", " ", clean_script).strip()
    candidates = re.split(r"(?<=[.!?])\s+|\s*[;|]\s*", clean_script)
    details = []
    seen = set()
    for sentence in candidates:
        sentence = sentence.strip(" -•,.;")
        key = re.sub(r"\W+", "", sentence.lower())
        if len(sentence) < 22 or key in seen: continue
        seen.add(key)
        if len(sentence) > 155:
            sentence = sentence[:152].rsplit(" ", 1)[0] + "..."
        details.append(sentence[0].upper() + sentence[1:] if sentence else sentence)
        if len(details) == 3: break

    parts = [title]
    if details:
        paragraph = " ".join(detail.rstrip(".!?") + "." for detail in details)
        parts.append(paragraph)
    parts.append("Xem hết video để thấy rõ cách dùng và kết quả thực tế nhé. Nếu thấy phù hợp, hãy lưu lại hoặc chia sẻ cho người đang cần!")
    product_link = str(product_link or "").strip()
    if product_link:
        parts.append(f"🛒 Xem sản phẩm tại Shopee: {product_link}")

    def hashtag(value):
        value = unicodedata.normalize("NFD", value.replace("đ", "d").replace("Đ", "D"))
        value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
        words = re.findall(r"[A-Za-z0-9]+", value)
        return "".join(word[:1].upper() + word[1:] for word in words)[:36]

    niche_tags = {
        "co-khi": ["DoCoKhi", "DungCuHuuIch"],
        "gia-dung": ["DoGiaDung", "MeoHayGiaDinh"],
        "nong-nghiep": ["DoNongNghiep", "NongNghiepThongMinh"],
        "cong-nghe": ["DoCongNghe", "CongNgheTienIch"],
        "thoi-trang": ["ThoiTrang", "ReviewThoiTrang"],
        "my-pham": ["MyPham", "ReviewMyPham"],
    }
    tags = [hashtag(product), "ReviewSanPham", "ReviewThucTe", *(niche_tags.get(niche) or ["SanPhamHay"]), "ReelsVietNam"]
    tags = [f"#{tag}" for i, tag in enumerate(tags) if tag and tag not in tags[:i]]
    parts.append(" ".join(tags))
    return "\n\n".join(parts)[:1800]

# ---- HELPERS ffmpeg ----
def file_hash(path, nbytes=2*1024*1024):
    """Hash nhanh (đầu 2MB + size) để cache phân tích theo NỘI DUNG video, ổn định qua các bản sao."""
    import hashlib
    try:
        h = hashlib.md5(); h.update(str(os.path.getsize(path)).encode())
        with open(path, "rb") as f: h.update(f.read(nbytes))
        return h.hexdigest()[:16]
    except Exception:
        return None

def ff(c):     return subprocess.run(c, capture_output=True)
def probe(p):
    o = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of",
                        "default=nw=1:nk=1",p], capture_output=True, text=True).stdout.strip()
    try: return float(o)
    except: return 0.0
def dims(p):
    r = subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries",
                        "stream=width,height","-of","csv=p=0:s=x",p],
                       capture_output=True, text=True).stdout.strip()
    try: w,h = r.split("x")[:2]; return int(w), int(h)
    except: return 1080, 1920
def mean_db(p):
    r = subprocess.run(["ffmpeg","-hide_banner","-i",p,"-af","volumedetect","-f","null","-"],
                       capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+)", r.stderr)
    return float(m.group(1)) if m else None

def log(m, job=None):
    line = time.strftime("%H:%M:%S ") + m
    open(os.path.join(LOG_DIR, "studio.log"), "a", encoding="utf-8").write(line + "\n")
    if job:
        job.setdefault("log", []).append(line)
    print(line, flush=True)

# ---- CODEX (não vision) ----
CODEX_MODEL = os.environ.get("CODEX_MODEL") or CONFIG.get("codex_model", "gpt-5.6-sol")
def codex(prompt, image=None, effort="medium", timeout=300):
    cmd = [CDX, "exec", "-c", f'model="{CODEX_MODEL}"', "-c", f'model_reasoning_effort="{effort}"',
           "--skip-git-repo-check"]
    if image: cmd.append(f"--image={image}")
    cmd.append("-")
    p = subprocess.run(cmd, input=prompt, text=True, encoding="utf-8", errors="replace",
                       capture_output=True, timeout=timeout)
    out = (p.stdout or "").strip()
    # codex exec in ra vài dòng header; lấy phần cuối
    return out

def codex_json(prompt, image=None, effort="medium", timeout=300):
    raw = codex(prompt, image=image, effort=effort, timeout=timeout)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m: return None
    try: return json.loads(m.group(0))
    except Exception:
        # cắt bớt rác sau JSON
        try: return json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)))
        except Exception: return None

# ---- CONTACT SHEET (12 khung trải đều) ----
def contact_sheet(src, out, cols=4, rows=3):
    dur = probe(src) or 1.0
    fpsv = (cols*rows) / dur
    ff(["ffmpeg","-y","-i",src,"-vf",f"fps={fpsv:.4f},scale=360:-1,tile={cols}x{rows}",
        "-frames:v","1",out])
    return out if os.path.exists(out) else None

# ---- WHISPER (lazy) ----
_WH = None
def whisper():
    global _WH
    if _WH is None:
        from faster_whisper import WhisperModel
        try:
            import torch
            if torch.cuda.is_available():
                # GTX 10xx (Pascal) không có FP16 hiệu quả; float32 vẫn nhanh hơn CPU.
                _WH = WhisperModel("small", device="cuda", compute_type="float32")
                log(f"whisper: dùng GPU {torch.cuda.get_device_name(0)}")
            else:
                raise RuntimeError("CUDA unavailable")
        except Exception as e:
            log(f"whisper: fallback CPU ({str(e)[:80]})")
            _WH = WhisperModel("small", device="cpu", compute_type="int8")
    return _WH

def transcribe(src, workdir, want_segments=False, language="zh"):
    """Trả (text, segments[]). language=None -> TỰ NHẬN (nguồn TikTok Việt: để None/‘vi’;
    Douyin: ‘zh’). Video không có tiếng -> trả rỗng, không lỗi."""
    wav = os.path.join(workdir, "asr.wav")
    ff(["ffmpeg","-y","-i",src,"-vn","-ac","1","-ar","16000",wav])
    try:
        segs, _ = whisper().transcribe(wav, language=language, vad_filter=True)
    except Exception:
        return "", []
    out_txt = []; out_seg = []
    for s in segs:
        t = (s.text or "").strip()
        if t: out_txt.append(t)
        if want_segments:
            out_seg.append({"start": round(s.start,2), "end": round(s.end,2), "text": t})
    return " ".join(out_txt), out_seg

# ---- BRAIN (thẩm định 1 video) — copy prompt pipeline ----
def brain(desc, zh, stats, contact, dur, niche):
    prompt = f"""Bạn là BỘ NÃO thẩm định video sản phẩm Douyin để làm content Facebook Reels tiếng Việt (niche: {niche}).
DỮ LIỆU:
- Mô tả gốc tiếng Trung: {desc or '(trống)'}
- Transcript audio: {zh or '(im lặng/chỉ nhạc)'}
- Thời lượng {dur:.1f}s. Tương tác: {stats}
- Contact-sheet đính kèm: xác nhận sản phẩm + thao tác + mức chữ TQ.
Trả về DUY NHẤT 1 JSON: {{"san_pham":"...","loai_audio":"chinese_voice|mixed_voice_music|product_sound|music_only",
"form":"problem_solution|review_demo|real_test|before_after|tutorial|comparison|price_deal|stress_test|transformation",
"hook_type":"pain_point|ket_qua_gay_to_mo|cau_hoi|tinh_huong_that","goc_content":"...","diem_tiem_nang":1-10,
"ly_do_bo":null|"...","caption_fb":"1 câu <85 ký tự + 3 hashtag"}}
Chấm điểm rải đều: 9-10 hiếm (độc/lạ chắc viral), 7-8 đa số video tốt, 6 tạm, <=5 loại (tour nhiều món/chữ TQ đầy/mơ hồ)."""
    return codex_json(prompt, image=contact)

# ---- WRITE SCRIPT (lời thoại tiếng Việt) — copy pipeline, có hook+style ----
def _learned_directive(tgt):
    try:
        src = open(AVW, encoding="utf-8-sig").read()
        m = re.search(r"def learned_voice_directive\(.*?(?=\ndef video_form_directive\()", src, re.DOTALL)
        ns = {"random": random}; exec(m.group(0), ns)
        return ns["learned_voice_directive"](tgt, "")
    except Exception:
        return ""

def write_script(br, zh, contact, dur, hook_type="", style_key="", extra_note=""):
    tgt = max(10.0, min(26.0, dur - 1.5))
    words = int(tgt * 4.3)
    ht = hook_type or br.get("hook_type") or "pain_point"
    sblock = ("\n" + style_text(style_key)) if style_key else ""
    persona_seed = sum(ord(ch) for ch in f"{br.get('san_pham','')}|{ht}|{style_key}")
    address_rule = (
        'XƯNG HÔ BẮT BUỘC: xưng "tao", gọi người xem là "mày" hoặc "chúng mày"; '
        'giữ nhất quán từ đầu đến cuối.'
        if persona_seed % 2 == 0 else
        'XƯNG HÔ BẮT BUỘC: xưng "anh", gọi người xem là "các vợ yêu" hoặc "các con vợ"; '
        'giữ nhất quán từ đầu đến cuối.'
    )
    prompt = f"""Bạn là AutoVoice — viết LỜI THOẠI tiếng Việt cho video sản phẩm Facebook Reels.

{_learned_directive(tgt)}{sblock}

BỘ NÃO đã thẩm định: Sản phẩm {br.get('san_pham','')} | Form {br.get('form','')} | Hook bắt buộc kiểu "{ht}" | Góc: {br.get('goc_content','')}
{extra_note}
{address_rule}
TONE/NGÔN TỪ: hài hước, vui vẻ, lầy nhẹ, tự nhiên như đang tám chuyện. Không tục, không công kích.
{GENMAX_EMOTION_GUIDE}
LUẬT HOOK FB: câu đầu vào thẳng theo kiểu hook trên, cấm mở "Đây là...", kết đọng tự nhiên không hô hào.
QUAN TRỌNG — KHỚP THỜI LƯỢNG: video dài {dur:.0f}s. Lời đọc kéo dài ~{tgt:.0f}s (~{words} từ, ±10) để phủ gần hết video. Contact-sheet 12 khung trải đều: viết lời theo ĐÚNG THỨ TỰ cảnh (1-4 mở, 5-8 giữa, 9-12 kết).
VOICE GỐC tiếng Trung (lấy Ý, đừng dịch cứng): {zh or '(không có)'}
YÊU CẦU: CHỈ in lời thoại thuần."""
    return codex(prompt, image=contact), ht

# ---- BLUR chữ Trung ----
def blur_regions_raw(src, workdir):
    """Trả list dict vùng chữ Trung [{x,y,w,h,start,end}]."""
    try:
        man = os.path.join(workdir, "blur.json")
        env = dict(os.environ, AUTOVOICE_ENABLE_OCR_BLUR="1")
        subprocess.run([VPY, BLUR_DET, "--video", src, "--work-dir", workdir, "--manifest", man],
                       capture_output=True, timeout=900, env=env)
        if not os.path.exists(man): return []
        return (json.load(open(man, encoding="utf-8")) or {}).get("regions") or []
    except Exception:
        return []

def blur_args_from(regs):
    """dict vùng -> tham số --blur-region 6 phần cho render_autovoice.py (nhánh remake)."""
    args = []
    for r in regs:
        args += ["--blur-region", "%d:%d:%d:%d:%.3f:%.3f" % (
            int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"]), float(r["start"]), float(r["end"]))]
    return args

def blur_regions(src, workdir):
    return blur_args_from(blur_regions_raw(src, workdir))

def bake_blur(src, regs, out):
    """BAKE blur theo vùng + KHOẢNG THỜI GIAN vào video (dùng cho review ghép — segmented
    renderer không nhận time-range). enable='between(t,start,end)' cho từng vùng."""
    regs = [r for r in (regs or []) if r]
    if not regs:
        return False
    cur = "0:v"; parts = []
    for i, r in enumerate(regs):
        x, y, w, h = int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])
        s, e = float(r.get("start", 0)), float(r.get("end", 9999))
        rad = max(6, min(w, h) // 4)
        lbl = "vout" if i == len(regs) - 1 else f"v{i}"
        parts.append(
            f"[{cur}]split=2[b{i}][c{i}];"
            f"[c{i}]crop=w={w}:h={h}:x={x}:y={y},boxblur={rad}:2[p{i}];"
            f"[b{i}][p{i}]overlay=x={x}:y={y}:enable='between(t,{s:.3f},{e:.3f})'[{lbl}]")
        cur = lbl
    tmp = out + ".bk.mp4"
    r = subprocess.run(["ffmpeg","-y","-i",src,"-filter_complex",";".join(parts),
                        "-map","[vout]","-an",*video_encode_args(),
                        "-movflags","+faststart",tmp], capture_output=True, timeout=900)
    if os.path.exists(tmp) and os.path.getsize(tmp) > 10000:
        os.replace(tmp, out); return True
    if os.path.exists(tmp): os.remove(tmp)
    return False

# ---- AUDIO autovoice (tách nhạc nền demucs) ----
def autovoice_audio(la, src, workdir):
    out = {"original": 0.04, "bed": None, "bedvol": 0.07}
    if la in ("chinese_voice", "mixed_voice_music"):
        wav = os.path.join(workdir, "src.wav")
        ff(["ffmpeg","-hide_banner","-y","-i",src,"-vn","-ac","2","-ar","44100",wav])
        try:
            import torch
            demucs_device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            demucs_device = "cpu"
        log(f"demucs: dùng {demucs_device}")
        subprocess.run([VPY,"-m","demucs","--device",demucs_device,"--segment","7",
                        "--two-stems=vocals","-n","htdemucs","--out",workdir,wav],
                       capture_output=True, timeout=1800)
        bed = os.path.join(workdir, "htdemucs", "src", "no_vocals.wav")
        if os.path.exists(bed):
            mv = mean_db(bed)
            out["bed"] = bed
            out["bedvol"] = 0.10 if (mv is None or mv < -30) else 0.07
        out["original"] = 0.0
    elif la == "product_sound":
        out["original"] = 0.22   # giữ tiếng sản phẩm thật
    return out

# ---- RENDER 1 bản (voice + mix + blur) ----
def render(src, script_path, out_path, voice_id, au, blur_args=None, workdir=None):
    cmd = [VPY, RENDERER, "--video", src, "--script", script_path, "--output", out_path,
           "--voice", voice_id, "--voice-volume", "1.35", "--delay-ms", "300",
           "--original-volume", str(au.get("original", 0.04))]
    if au.get("bed"):
        cmd += ["--music", au["bed"], "--music-volume", str(au.get("bedvol", 0.07))]
    if blur_args:
        cmd += blur_args
    rr = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=1200)
    blob = (rr.stdout or "") + (rr.stderr or "")
    if not os.path.exists(out_path):
        if "credit" in blob.lower() or "insufficient" in blob.lower():
            return {"ok": False, "err": "vivibe_credit"}
        return {"ok": False, "err": (rr.stderr or "render fail")[-200:]}
    # chống đuôi trống tiếng
    mvd = re.search(r'"raw_voice_duration":\s*([\d.]+)', blob)
    if mvd and workdir:
        vend = 0.3 + float(mvd.group(1)); fdur = probe(out_path)
        if fdur > vend + 2.5:
            cut = out_path + ".cut.mp4"
            ff(["ffmpeg","-y","-i",out_path,"-t",f"{vend+1.6:.2f}",*video_encode_args(),"-c:a","aac",cut])
            if os.path.exists(cut) and probe(cut) > 6: os.replace(cut, out_path)
    faststart(out_path)   # moov về đầu -> player/Telegram đọc đúng khung dọc ngay
    return {"ok": True, "voice_dur": float(mvd.group(1)) if mvd else None}

# ---- TTS VIVIBE 1 câu (cho review khớp cảnh) ----
_RA = None
def _load_ra():
    global _RA
    if _RA is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("render_autovoice", RENDERER)
        m = importlib.util.module_from_spec(spec)
        try: spec.loader.exec_module(m)
        except SystemExit: pass
        _RA = m
    return _RA

def tts_vivibe(text, voice_id, out_path, speed=1.0):
    """TTS 1 câu bằng giọng Vivibe -> file audio. Trả duration. Ném lỗi nếu fail (vd hết credit)."""
    from pathlib import Path
    _load_ra().run_vivibe_tts_with_retry(text, voice_id, Path(out_path), speed, attempts=3)
    return probe(out_path)

# ---- DOWNLOAD (link -> file) ----
def _tikwm(url, workdir):
    """TikTok/Douyin qua tikwm (KHÔNG watermark, KHÔNG cần cookie/login). Trả (file, meta) hoặc (None,{})."""
    import httpx
    try:
        req = {"params": {"url": url, "hd": 1}, "timeout": 45,
               "headers": {"User-Agent": "Mozilla/5.0"}}
        try:
            r = httpx.get("https://www.tikwm.com/api/", **req)
        except Exception as cert_err:
            if "CERTIFICATE_VERIFY_FAILED" not in str(cert_err):
                raise
            log("tikwm: chứng chỉ máy cũ không xác minh được, thử lại SSL fallback")
            r = httpx.get("https://www.tikwm.com/api/", verify=False, **req)
        j = r.json()
        if j.get("code") != 0: return None, {}
        d = j.get("data") or {}
        play = d.get("hdplay") or d.get("play") or d.get("wmplay")
        if not play: return None, {}
        purl = play if play.startswith("http") else "https://www.tikwm.com" + play
        media_req = {"timeout": 120,
                     "headers": {"User-Agent": "Mozilla/5.0", "Referer": "https://www.tikwm.com/"}}
        try:
            vid = httpx.get(purl, **media_req).content
        except Exception as cert_err:
            if "CERTIFICATE_VERIFY_FAILED" not in str(cert_err):
                raise
            vid = httpx.get(purl, verify=False, **media_req).content
        if len(vid) < 50000: return None, {}
        dst = os.path.join(workdir, "dl.mp4"); open(dst, "wb").write(vid)
        return dst, {"title": d.get("title",""), "dur": d.get("duration",""),
                     "uploader": ((d.get("author") or {}).get("unique_id","")), "url": url}
    except Exception as e:
        log(f"tikwm loi: {str(e)[:80]}")
        return None, {}

def download(url, workdir):
    """Ưu tiên tikwm (TikTok/Douyin, không watermark). FB -> fb_download. Douyin -> thêm fallback signed."""
    os.makedirs(workdir, exist_ok=True)
    low = url.lower()
    # 0) Google Drive công khai (link /file/d/... hoặc open?id=...)
    if "drive.google.com" in low:
        try:
            import gdown
            dst = os.path.join(workdir, "drive.mp4")
            got = gdown.download(url=url, output=dst, quiet=True, fuzzy=True)
            if got and os.path.exists(dst) and os.path.getsize(dst) > 50000:
                return dst, {"title": os.path.basename(dst), "dur": "", "uploader": "Google Drive", "url": url}
        except Exception as e:
            log(f"download Google Drive lỗi: {str(e)[:120]}")
    # 1) tikwm cho tiktok/douyin
    if "tiktok.com" in low or "douyin.com" in low or "/v/" in low:
        f, meta = _tikwm(url, workdir)
        if f: return f, meta
    # 2) fb_download (yt-dlp impersonate) — tốt cho FB, thử luôn cho link khác
    try:
        import fb_download
        r = fb_download.download(url, workdir=workdir, use_cookies=False)
        if r.get("ok"):
            return r["file"], {"title": r.get("title",""), "dur": r.get("duration",""),
                               "uploader": r.get("uploader",""), "url": r.get("url", url)}
    except Exception as e:
        log(f"download fb_download loi: {str(e)[:80]}")
    # 3) tikwm lần cuối (phòng khi link rút gọn)
    f, meta = _tikwm(url, workdir)
    if f: return f, meta
    # 4) douyin_signed nếu là link douyin có aweme id
    m = re.search(r"(\d{15,})", url)
    if m and "douyin" in low:
        try:
            from scrapers import douyin_signed as ds
            d = ds.call_signed("/aweme/v1/web/aweme/detail/", {"aweme_id": m.group(1)})
            urls = ((d.get("aweme_detail") or {}).get("video", {}).get("play_addr", {}) or {}).get("url_list", [])
            import httpx
            cl = httpx.Client(timeout=90, follow_redirects=True,
                              headers={"User-Agent":"Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)",
                                       "Referer":"https://www.douyin.com/"})
            for u in urls:
                try:
                    resp = cl.get(u)
                    if len(resp.content) > 50000:
                        dst = os.path.join(workdir, "dl.mp4")
                        open(dst,"wb").write(resp.content)
                        return dst, {"title":"", "dur":"", "uploader":"", "url":url}
                except Exception: pass
        except Exception as e:
            log(f"download douyin loi: {str(e)[:80]}")
    return None, {}

# ---- JOB STATE ----
def job_path(jid): return os.path.join(JOB_DIR, jid + ".json")
def save_job(job): json.dump(job, open(job_path(job["id"]),"w",encoding="utf-8"), ensure_ascii=False, indent=1)
def load_job(jid):
    try: return json.load(open(job_path(jid), encoding="utf-8"))
    except Exception: return None
def list_jobs():
    out = []
    for f in sorted(glob.glob(os.path.join(JOB_DIR,"*.json")), key=os.path.getmtime, reverse=True):
        try: out.append(json.load(open(f, encoding="utf-8")))
        except Exception: pass
    return out
