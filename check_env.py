# -*- coding: utf-8 -*-
"""KIỂM TRA MÔI TRƯỜNG cho Video Studio.
Chạy:  python check_env.py           -> chỉ kiểm tra, báo thiếu gì
       python check_env.py --fix     -> tự cài các gói pip còn thiếu (core)
Kiểm: Python, ffmpeg/ffprobe, yt-dlp, codex CLI, config.json + TTS key, gói pip core/optional.
"""
import sys, io, os, json, shutil, subprocess, glob, importlib.util
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))

# Shortcut/cửa sổ được mở trước lúc winget/npm cài xong thường giữ PATH cũ.
if os.name == "nt":
    extra = [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs"),
             os.path.join(os.environ.get("APPDATA", ""), "npm")]
    extra += glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet",
                                    "Packages", "Gyan.FFmpeg*", "ffmpeg-*", "bin"))
    os.environ["PATH"] = os.pathsep.join([p for p in extra if os.path.isdir(p)] +
                                          [os.environ.get("PATH", "")])
FIX = "--fix" in sys.argv
OK, WARN, ERR = "  ✅", "  ⚠️ ", "  ❌"
problems = []; warns = []

def hr(t): print("\n" + "═"*54 + f"\n  {t}\n" + "═"*54)

# ---------- 1. Python ----------
hr("1. Python")
v = sys.version_info
if v >= (3, 10):
    print(OK, f"Python {v.major}.{v.minor}.{v.micro}")
else:
    print(ERR, f"Python {v.major}.{v.minor} — cần >= 3.10"); problems.append("Python >=3.10")

# ---------- 2. Binary ngoài ----------
hr("2. Công cụ ngoài (PATH)")
for name, need, hint in [("ffmpeg", True, "cài ffmpeg + thêm vào PATH (winget install Gyan.FFmpeg)"),
                         ("ffprobe", True, "đi kèm ffmpeg"),
                         ("yt-dlp", False, "chỉ cần cho tải video Facebook (pip install yt-dlp)"),
                         ("cloudflared", False, "chỉ cần khi đưa website ra Internet bằng Cloudflare Tunnel")]:
    p = shutil.which(name)
    if name == "yt-dlp" and not p and importlib.util.find_spec("yt_dlp"):
        p = f"{sys.executable} -m yt_dlp"
    if p: print(OK, f"{name}: {p}")
    elif need: print(ERR, f"{name} KHÔNG thấy — {hint}"); problems.append(name)
    else: print(WARN, f"{name} không thấy — {hint}"); warns.append(name)

# ---------- 3. config.json + codex + TTS key ----------
hr("3. Cấu hình (config.json)")
cfgp = os.path.join(ROOT, "config.json")
cfg = {}
if os.path.exists(cfgp):
    try: cfg = json.load(open(cfgp, encoding="utf-8")); print(OK, "config.json đọc được")
    except Exception as e: print(ERR, f"config.json lỗi JSON: {e}"); problems.append("config.json")
else:
    print(ERR, "THIẾU config.json — copy config.example.json thành config.json rồi điền key"); problems.append("config.json")

# codex CLI
cdx = os.environ.get("CODEX_CMD") or cfg.get("codex_cmd") or shutil.which("codex") or shutil.which("codex.cmd")
if cdx and (os.path.exists(cdx) or shutil.which(cdx)):
    print(OK, f"codex CLI: {cdx}")
else:
    print(ERR, "codex CLI KHÔNG thấy — cài rồi ghi 'codex_cmd' trong config.json (xem CLAUDE.md phần Codex)"); problems.append("codex CLI")

# TTS key
prov = cfg.get("tts_provider", "vivibe")
key = cfg.get(f"{prov}_api_key", "")
if key and not key.startswith("DIEN_"):
    print(OK, f"TTS provider = {prov}, có API key")
else:
    print(ERR, f"THIẾU key cho provider '{prov}' — điền '{prov}_api_key' trong config.json"); problems.append("TTS key")

# ---------- 4. Gói pip CORE ----------
hr("4. Gói Python CORE (bắt buộc)")
core = [("flask", "Flask"), ("httpx", "httpx"), ("faster_whisper", "faster-whisper")]
missing = []
for mod, pkg in core:
    try: __import__(mod); print(OK, f"{pkg}")
    except Exception: print(ERR, f"{pkg} CHƯA cài"); missing.append(pkg)
if missing and FIX:
    print("\n  → Đang cài:", ", ".join(missing))
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", os.path.join(ROOT, "requirements.txt")])
    print("  → Cài xong, chạy lại check_env.py để xác nhận.")
elif missing:
    problems.append("gói core (" + ",".join(missing) + ")")
    print(WARN, "Chạy lại với --fix để tự cài:  python check_env.py --fix")

# ---------- 5. Gói OPTIONAL ----------
hr("5. Gói tuỳ chọn (chỉ cần nếu dùng)")
for mod, what in [("torch", "tách nhạc nền Remake"), ("demucs", "tách nhạc nền Remake"),
                  ("cv2", "blur chữ Trung"), ("easyocr", "blur chữ Trung")]:
    try:
        loaded = __import__(mod)
        extra = ""
        if mod == "torch":
            extra = (f" · GPU {loaded.cuda.get_device_name(0)}" if loaded.cuda.is_available()
                     else " · chỉ CPU")
        print(OK, f"{mod} — {what}{extra}")
    except Exception: print(WARN, f"{mod} chưa cài — {what} (pip install -r requirements-optional.txt)")

# ---------- Tổng kết ----------
hr("KẾT LUẬN")
if not problems:
    print("  ✅ ĐỦ MÔI TRƯỜNG CHẠY. Bật tool:  start-studio.cmd  ->  http://127.0.0.1:7995")
else:
    print("  ❌ CÒN THIẾU (bắt buộc):")
    for p in problems: print("       -", p)
    print("\n  Cách xử lý nhanh:  python check_env.py --fix   (cài gói pip)")
    print("  ffmpeg/codex/key: xem CLAUDE.md hoặc README.md.")
if warns:
    print("\n  ⚠️  Không bắt buộc, thiếu cũng chạy được luồng chính:", ", ".join(warns))
print()
