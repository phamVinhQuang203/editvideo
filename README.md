# 🎬 Video Studio

Tự động **làm video review** từ các video có sẵn: dán link → tool phân tích, cắt ghép thành video
mới + tự viết lời và đọc voice tiếng Việt. **Không đăng bài**, chỉ tạo video để bạn tải về dùng.

## Cài đặt (máy mới, làm 1 lần)

1. Bấm đúp **`setup.cmd`** → tự động cài **Python + ffmpeg + Node + codex CLI** (qua winget/npm),
   tạo môi trường ảo, cài thư viện, tạo `config.json`, rồi kiểm tra.
   - Nếu đây là lần đầu vừa cài Python/ffmpeg: **đóng cửa sổ, mở lại `setup.cmd`** để hoàn tất
     (cho hệ thống nhận đường dẫn mới).
2. **Đăng nhập codex** 1 lần: mở cửa sổ lệnh mới, gõ `codex login`.
3. Mở **`config.json`**, điền:
   - `vivibe_api_key` (hoặc `voicehay_api_key`): key dịch vụ đọc giọng.
   - `codex_cmd`: để `"codex"` là được (nếu đã cài qua setup).
4. (Không bắt buộc) chạy `check_env.cmd` / `venv\Scripts\python check_env.py` để xác nhận đủ.

> Máy đã có sẵn Python/ffmpeg thì `setup.cmd` tự bỏ qua bước cài, chỉ dựng môi trường.
> Máy không có `winget` (Windows cũ): cài tay Python 3.10+, ffmpeg, Node rồi chạy lại `setup.cmd`.

## Dùng hằng ngày

- Mở terminal tại thư mục project rồi chạy **`start-studio.cmd`** → mở trình duyệt
  **http://127.0.0.1:7995**.
- Giữ terminal mở trong lúc sử dụng; nhấn `Ctrl+C` hoặc đóng terminal để dừng server.
- Vào **Tạo video**: dán link (nhiều video cùng 1 sản phẩm) → chọn số bản / hook → bấm **Tạo video**.
- Xem và tải kết quả ở tab **Kết quả**.

## Cần gì

| Thứ | Bắt buộc? | Ghi chú |
|---|---|---|
| Python 3.10+ | ✅ | |
| ffmpeg / ffprobe | ✅ | cắt ghép video |
| codex CLI | ✅ | phân tích + viết lời |
| API key TTS (Vivibe/VoiceHay) | ✅ | đọc giọng |
| yt-dlp | ❌ | chỉ cần tải video Facebook |
| torch + demucs | ❌ | chỉ cho nhánh Remake tách nhạc |
| opencv + easyocr | ❌ | chỉ khi bật che chữ Trung |

Chi tiết kỹ thuật + phát triển tiếp: xem **`CLAUDE.md`**.
