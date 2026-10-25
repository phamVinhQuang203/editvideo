# Video Studio — Tài liệu kỹ thuật (cho Claude / dev)

Tool **làm video review tự động, KHÔNG đăng bài**. Đầu vào là các video (link TikTok/Douyin/FB
hoặc file), tool **tự phân tích → cắt phân đoạn → xáo trộn ghép thành video mới → viết lời + đọc
voice tiếng Việt**. Web dashboard tại **http://127.0.0.1:7995**.

> File này để bạn (Claude ở máy mới) hiểu toàn bộ hệ thống và phát triển tiếp. Đọc hết trước khi sửa.

---

## 1. Cài đặt nhanh (máy mới)

```
setup.cmd              REM tao venv + cai goi core + tao config.json + kiem tra
python check_env.py    REM kiem tra lai; --fix de tu cai goi pip thieu
start-studio.cmd       REM bat dashboard -> http://127.0.0.1:7995
```

**Cần có sẵn trên máy:**
- **Python 3.10+** (đang chuẩn theo 3.13).
- **ffmpeg + ffprobe** trong PATH (`winget install Gyan.FFmpeg`). BẮT BUỘC.
- **codex CLI** (bộ não phân tích + viết lời — xem mục 6). BẮT BUỘC.
- **API key TTS** (Vivibe hoặc VoiceHay) điền vào `config.json`. BẮT BUỘC.
- yt-dlp: chỉ cần nếu tải video Facebook (`pip install yt-dlp`). TikTok/Douyin dùng tikwm, không cần.
- (Tuỳ chọn nặng) torch+demucs cho nhánh Remake tách nhạc; opencv+easyocr cho blur chữ Trung.

`config.json` (copy từ `config.example.json`):
```json
{ "codex_cmd": "codex", "codex_model": "gpt-5.6-sol",
  "tts_provider": "vivibe",
  "vivibe_api_key": "...", "voicehay_api_key": "...",
  "output_dir": "" }
```
- **`output_dir`**: để trống = lưu video ra `output/` trong gói. Muốn lưu chỗ khác thì điền đường
  dẫn tuyệt đối (vd `"D:/VideoRa"`) rồi khởi động lại tool. `engine.OUT_DIR` đọc giá trị này (hoặc
  env `VS_OUTPUT_DIR`). Trên dashboard tab **Cài đặt** có nút "📂 Mở thư mục video".

---

## 2. Cấu trúc thư mục

```
video-studio/
  app/
    server.py                 Flask dashboard (port 7995) + hàng đợi 1 job/lúc
    engine.py                 LÕI CHUNG: config, đường dẫn, codex(), whisper, tts, download, render, ffmpeg helper
    review.py                 NHÁNH 2 — Ghép Review (nhiều video 1 sản phẩm -> video mới). CHỦ LỰC.
    remake.py                 NHÁNH 1 — Làm lại 1 video (link/file -> nhiều biến thể)
    render_segmented_vivibe.py  (không dùng — bản render voice theo timestamp, để dự phòng)
  vendor/                     Script GỐC gói kèm (đừng sửa trừ khi cần):
    render_autovoice.py       Render voice (TTS Vivibe) + trộn nhạc + blur -> xuất mp4
    detect_blur.py            OCR chữ Trung -> vùng cần blur (cần opencv+easyocr)
    autovoice_web.py          learned_voice_directive (chỉ nhánh Remake dùng)
    fb_download.py            Tải video Facebook (yt-dlp) — fallback
  config.json                 Cấu hình máy (codex path + API key). KHÔNG commit key đi xa.
  requirements.txt            Gói core
  requirements-optional.txt   Gói nặng tuỳ chọn
  check_env.py                Kiểm tra môi trường (--fix tự cài pip)
  setup.cmd / start-studio.cmd
  input/ output/ jobs/ work/ logs/ analysis_cache/   (tự tạo lúc chạy)
```

**Đường dẫn PORTABLE:** `engine.py` tính `ROOT = thư mục cha của app/`, `VENDOR = ROOT/vendor`.
KHÔNG còn đường dẫn cứng. Chỉ `config.json` chứa giá trị theo máy (codex path, key).

---

## 3. Luồng hoạt động (nhánh Review — quan trọng nhất)

Người dùng dán N link cùng 1 sản phẩm → `server.py` tạo job → worker gọi `review.run_review`:

1. **Phân tích từng nguồn** (`analyze_source`) — CÓ CACHE (`analysis_cache/<ident>/`, ident = md5 URL
   hoặc hash file). Lần sau nguồn cũ = tức thì.
   - Tải video (`engine.download` → tikwm cho TikTok/Douyin, fb_download cho FB).
   - Chuẩn hoá 1080x1920 30fps (`norm.mp4`).
   - **Tách shot** (`scene_cuts`) bằng ffmpeg scene-detect.
   - **Montage đánh số shot** (`shot_montage`) — ảnh lưới có số để codex nhìn.
   - **Nghe lời** (`engine.transcribe`, whisper, tự nhận ngôn ngữ) → lời gốc từng shot.
   - **Gán vai từng shot** (`classify_shots`, codex vision đọc montage) → mỗi shot có
     `vai` (hook/problem/comparison/demo/feature/result/cta/filler) + `diem` + `mo_ta` + `loi` (lời gốc).
   - Gộp tất cả shot mọi nguồn thành `pool` (đây là "KHO PHÂN ĐOẠN").

2. **Ráp M biến thể** — mỗi biến thể 1 **KỊCH BẢN** (`FRAMEWORKS`, 7 loại) khác nhau:
   - `build_plan_fw` chọn cảnh theo `flow` của kịch bản, **ÉP TRỘN ≥2-3 nguồn khác nhau** +
     **CHỐNG TRÙNG cảnh** giữa các biến thể (`used_global`).
   - Cắt clip (`cut_clip`) + nối (`concat_clips`) → `assembled.mp4` (video mới ghép).

3. **Viết lời + đọc voice** cho video ghép (coi như 1 video hoàn chỉnh):
   - `review_script_fw`: viết 1 lời LIỀN MẠCH theo cấu trúc kịch bản + **HỌC lời gốc** các cảnh đã chọn
     (chống lời "1 màu") + nhồi FACTS (thông số/giá/CTA nhập tay). GENERIC cho mọi sản phẩm.
   - **Phiên bản hook**: `hook_variants` (1-4) — cùng video ghép, đổi kiểu hook mở đầu + giọng → nhiều bản.
   - `engine.render` gọi `vendor/render_autovoice.py` để TTS + trộn + xuất mp4 (voice LIỀN MẠCH, 1 giọng).
   - `engine.faststart` đưa moov về đầu (player/Telegram hiện đúng khung dọc).

**Tổng video/job = n_variants (kịch bản/tổ hợp cảnh) × hook_variants.** Vd 7×3 = 21.

Nhánh **Remake** (`remake.py`): 1 video → N biến thể (đổi style/hook/giọng/lật hình). Đơn giản hơn.

---

## 4. Các điểm chốt (đừng phá)

- **Voice LIỀN MẠCH**: viết 1 lời cho cả video rồi render 1 giọng. ĐÃ THỬ tách câu từng cảnh → nghe
  đứt quãng, user chê. Giữ liền mạch.
- **Chống "1 màu"**: prompt GENERIC (không hardcode sản phẩm) + học `loi` (lời gốc) + effort=medium.
  Nếu đổi cấu trúc `labeled` (thêm/bớt field) phải **XOÁ `analysis_cache/`** để job cũ phân tích lại.
- **Trộn nhiều nguồn + chống trùng**: mỗi video mới phải lấy cảnh từ nhiều nguồn khác nhau.
- **Blur chữ Trung TẮT mặc định** cho Review (nguồn Việt không có chữ Trung; OCR chậm ~4ph/bản).
  Bật qua ô "Che chữ Trung" khi nguồn Douyin.
- **1 job/lúc** (hàng đợi tuần tự trong server.py) để máy không quá tải.

---

## 5. TTS provider (Vivibe / VoiceHay)

Hiện `render_autovoice.py` dùng **Vivibe** (RPC `api.lucylab.io/json-rpc`, key `sk_live_...`,
env `VIVIBE_API_KEY`). Giọng khai báo trong `engine.py` (`VOICES_NAM/VOICES_NU`, id + nhãn nam1..nu2).

**VoiceHay** (`api.voicehay.com`, key `vv_live_...`, REST) là bản nâng cấp (2.2M ký tự/tháng, 3 job
song song, 108 giọng, dùng `slug`). Để chuyển sang VoiceHay: viết hàm `voicehay_tts(slug,text)->mp3`
(POST /v1/speech → poll /v1/speech/{id} → GET /v1/speech/{id}/audio) và thay đường render voice.
Đây là việc CHƯA làm — xem memory/user để biết quyết định.

---

## 6. Codex CLI (bộ não phân tích + viết lời)

`engine.codex()` gọi CLI: `codex exec -c model="<codex_model>" -c model_reasoning_effort="..."
--skip-git-repo-check --image=<contact> -`. Đây là công cụ dòng lệnh riêng (Node), KHÔNG phải pip.

Trên máy mới cần cài codex CLI và cấu hình auth + model. Ghi đường dẫn vào `config.json.codex_cmd`
(hoặc để "codex" nếu đã trong PATH), model vào `config.json.codex_model`. Nếu model `gpt-5.6-sol`
không có, đổi sang model vision mà tài khoản codex hỗ trợ.

---

## 7. Phát triển tiếp (ý tưởng đang mở)

- Chuyển TTS sang VoiceHay (nhanh 3x + quota gấp đôi).
- Mỗi phiên bản hook đổi luôn tổ hợp cảnh (hiện dùng chung video ghép, chỉ khác voice/hook).
- Bán tự động: cho user xem trước các đoạn cắt rồi chọn.
- Lấy nguồn theo từ khoá (search TikTok) thay vì dán link tay.
- Nhạc nền: giữ 1 đoạn tiếng gốc cho tự nhiên.

## 8. Gỡ lỗi nhanh

- Import lỗi path: kiểm `engine.ROOT/VENDOR` đúng chưa (`python check_env.py`).
- Render voice fail "credit": hết credit TTS → nạp hoặc đổi key trong config.json.
- Video "vỡ" trên Telegram: gửi kèm width=1080/height=1920/duration/supports_streaming/thumbnail
  (file thật đã faststart, chỉ là Telegram cần metadata).
- Job kẹt: xem `logs/studio.log` và `jobs/<id>.json` (có mảng log từng bước).

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **VideoStudio** (298 symbols, 1291 relationships, 25 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "main"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "main" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- Explore with `query({search_query: "concept"})` for process-grouped flows.
- Use `context({name: "symbolName"})` for callers, callees, and flows.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/VideoStudio/context` | Codebase overview, check index freshness |
| `gitnexus://repo/VideoStudio/clusters` | All functional areas |
| `gitnexus://repo/VideoStudio/processes` | All execution flows |
| `gitnexus://repo/VideoStudio/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
