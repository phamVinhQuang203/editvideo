# -*- coding: utf-8 -*-
"""Translate spoken video content and optionally burn Vietnamese subtitles."""
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
import wave
from pathlib import Path

import engine as E
from hook_skill import TIKTOK_HOOK_3S_SKILL


LANGUAGES = {
    "vi": "tiếng Việt",
    "en": "tiếng Anh",
    "zh": "tiếng Trung giản thể",
    "ja": "tiếng Nhật",
    "ko": "tiếng Hàn",
    "th": "tiếng Thái",
    "id": "tiếng Indonesia",
}


def _srt_time(seconds):
    millis = max(0, int(round(float(seconds) * 1000)))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(segments, path):
    lines = []
    for index, segment in enumerate(segments, 1):
        text = " ".join(str(segment.get("translated") or segment.get("text") or "").split())
        if not text:
            continue
        lines.extend([str(index), f"{_srt_time(segment['start'])} --> {_srt_time(segment['end'])}", text, ""])
    with open(path, "w", encoding="utf-8-sig", newline="\n") as handle:
        handle.write("\n".join(lines))
    return path


def translate_segments(segments, target="vi", batch_size=35):
    if batch_size < 1:
        raise ValueError("batch_size phải lớn hơn 0")
    target_name = LANGUAGES.get(target, target)
    translated = []
    for start in range(0, len(segments), batch_size):
        batch = segments[start:start + batch_size]
        payload = [{"id": start + i, "text": item.get("text", "")} for i, item in enumerate(batch)]
        by_id = {}
        pending = payload
        chunk_size = batch_size
        last_error = ""
        for attempt in range(3):
            for offset in range(0, len(pending), chunk_size):
                chunk = pending[offset:offset + chunk_size]
                prompt = f"""Dịch phụ đề phim sang {target_name} tự nhiên, đúng ngữ cảnh và ngắn gọn để dễ đọc.
Giữ nguyên tên riêng, con số và sắc thái lời thoại. Không giải thích, không thêm nội dung.
Trả về DUY NHẤT JSON dạng {{"items":[{{"id":0,"text":"..."}}]}} với đủ mọi id.
Giữ đúng id đầu vào, không đánh số lại từ 0.
Dữ liệu: {json.dumps(chunk, ensure_ascii=False)}"""
                try:
                    result = E.codex_json(prompt, effort="medium", timeout=600, strict=True)
                except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
                    last_error = str(exc)
                    E.log(f"Dịch lô {start // batch_size + 1}, lần {attempt + 1}: {last_error}")
                    # A failed process is not a reason to fan out more requests.
                    break
                values = result.get("items") if isinstance(result, dict) else result
                if not isinstance(values, list):
                    last_error = "JSON thiếu danh sách items hợp lệ"
                    break
                expected = {item["id"] for item in chunk}
                for item in values:
                    if not isinstance(item, dict):
                        continue
                    item_id = item.get("id")
                    if isinstance(item_id, str) and item_id.isdecimal():
                        item_id = int(item_id)
                    text = item.get("text")
                    if type(item_id) is int and item_id in expected and isinstance(text, str) and text.strip():
                        by_id[item_id] = text.strip()
                last_error = "AI trả thiếu id hoặc bản dịch rỗng/sai định dạng"
            pending = [item for item in payload if item["id"] not in by_id]
            if not pending:
                break
            E.log(f"Dịch lô {start // batch_size + 1}, lần {attempt + 1}/3: còn thiếu {len(pending)} đoạn; {last_error}")
            chunk_size = max(1, chunk_size // 2)
        if pending:
            ids = ", ".join(str(item["id"]) for item in pending)
            raise RuntimeError(f"Dịch lô {start // batch_size + 1} thất bại sau 3 lần; thiếu {len(pending)} đoạn (id: {ids}). Nguyên nhân: {last_error}")
        for i, item in enumerate(batch):
            copy = dict(item)
            copy["translated"] = by_id[start + i]
            translated.append(copy)
    return translated


def translated_or_original_segments(segments, target="vi"):
    """Skip the translation model when the user requests original-language subtitles."""
    if str(target or "").lower() != "none":
        return translate_segments(segments, target)
    output = []
    for segment in segments:
        item = dict(segment)
        item["translated"] = str(item.get("text") or "").strip()
        output.append(item)
    return output


def retime_translation_subtitles(segments, start_delay=0.22, cue_gap=0.14):
    """Delay Whisper cues slightly and guarantee a visible pause between adjacent lines."""
    ordered = sorted((dict(item) for item in segments), key=lambda item: float(item.get("start", 0)))
    output = []
    for index, item in enumerate(ordered):
        original_start = max(0.0, float(item.get("start", 0)))
        original_end = max(original_start + 0.25, float(item.get("end", original_start + 0.25)))
        start = min(original_end - 0.2, original_start + float(start_delay))
        end = original_end - 0.08
        if index + 1 < len(ordered):
            next_start = max(0.0, float(ordered[index + 1].get("start", original_end)))
            end = min(end, next_start + float(start_delay) - float(cue_gap))
        # Do not keep short text on screen throughout a very long Whisper segment.
        words = len(str(item.get("translated") or item.get("text") or "").split())
        readable_duration = max(0.8, min(6.0, 0.45 + words / 2.7))
        end = min(end, start + readable_duration)
        item["start"] = max(0.0, start)
        item["end"] = max(item["start"] + 0.2, end)
        output.append(item)
    return output


def _ass_color(value, default):
    value = str(value or default).strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        value = default
    red, green, blue = value[1:3], value[3:5], value[5:7]
    return f"&H00{blue}{green}{red}".upper()


def _subtitle_filter(path, subtitle_style=None):
    escaped = os.path.abspath(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    subtitle_style = subtitle_style or {}
    try:
        font_size = max(12, min(48, int(subtitle_style.get("font_size", 18))))
    except (TypeError, ValueError):
        font_size = 18
    font_color = _ass_color(subtitle_style.get("font_color"), "#FFFFFF")
    outline_color = _ass_color(subtitle_style.get("outline_color"), "#101010")
    style = (f"FontName=Arial,FontSize={font_size},PrimaryColour={font_color},"
             f"OutlineColour={outline_color},BorderStyle=1,Outline=2,Shadow=1,"
             "Alignment=2,MarginV=38")
    return f"subtitles='{escaped}':force_style='{style}'"


def add_movie_header(video_path, movie_title, workdir):
    """Render an optional, top-centered movie title over the complete video."""
    title = " ".join(str(movie_title or "").split())[:160]
    if not title:
        return video_path
    os.makedirs(workdir, exist_ok=True)
    header_ass = os.path.join(workdir, "movie_header.ass")
    ass_title = title.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
    ass = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Header,Arial,56,&H00FFFFFF,&H00FFFFFF,&H00101010,&H78000000,-1,0,0,0,100,100,0,0,3,2,0,8,40,40,6,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
Dialogue: 0,0:00:00.00,23:59:59.00,Header,,0,0,0,,""" + ass_title + "\n"
    with open(header_ass, "w", encoding="utf-8-sig", newline="\n") as handle:
        handle.write(ass)
    escaped = os.path.abspath(header_ass).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    video_filter = f"subtitles='{escaped}'"
    rendered = os.path.join(workdir, "movie_header_rendered.mp4")
    commands = (
        ["ffmpeg", "-y", "-i", video_path, "-vf", video_filter,
         *E.video_encode_args(), "-c:a", "copy", "-movflags", "+faststart", rendered],
        ["ffmpeg", "-y", "-i", video_path, "-vf", video_filter,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", rendered],
    )
    for command in commands:
        result = E.ff(command)
        if result.returncode == 0 and os.path.exists(rendered) and os.path.getsize(rendered) > 10_000:
            os.replace(rendered, video_path)
            return video_path
    raise RuntimeError("Không chèn được header tên phim vào video")


def _replace_subtitle_filter(subtitle, position, subtitle_style=None, region=None):
    """Che dải sub cứng rồi đặt phụ đề dịch lên trên dải đã làm mờ."""
    if region:
        x, y, width, height = region
        crop = f"iw*{width / 100:.4f}:ih*{height / 100:.4f}:iw*{x / 100:.4f}:ih*{y / 100:.4f}"
        overlay = f"W*{x / 100:.4f}:H*{y / 100:.4f}"
    else:
        y_crop = "0" if position == "top" else "ih*0.76"
        crop = f"iw:ih*0.24:0:{y_crop}"
        overlay = "0:0" if position == "top" else "0:H-h"
    return (f"[0:v]split=2[base][subarea];"
            f"[subarea]crop={crop},boxblur=20:3[blurred];"
            f"[base][blurred]overlay={overlay}[clean];"
            f"[clean]{_subtitle_filter(subtitle, subtitle_style)}[v]")


def clean_original_subtitles(source, destination, position, region=None):
    helper = os.path.join(E.VENDOR, "remove_subtitles_local.py")
    command = [sys.executable, helper, source, destination, "--position", position]
    if region:
        command.extend(["--region", *(str(value) for value in region)])
    result = E.ff(command)
    return (result.returncode == 0 and os.path.exists(destination)
            and os.path.getsize(destination) > 10_000)


def blur_original_subtitles(source, destination, position, region=None):
    if region:
        x, y, width, height = region
        crop = f"iw*{width / 100:.4f}:ih*{height / 100:.4f}:iw*{x / 100:.4f}:ih*{y / 100:.4f}"
        overlay = f"W*{x / 100:.4f}:H*{y / 100:.4f}"
    else:
        y_crop = "0" if position == "top" else "ih*0.76"
        crop = f"iw:ih*0.24:0:{y_crop}"
        overlay = "0:0" if position == "top" else "0:H-h"
    video_filter = (f"[0:v]split=2[base][subarea];"
                    f"[subarea]crop={crop},boxblur=20:3[blurred];"
                    f"[base][blurred]overlay={overlay}[v]")
    result = E.ff(["ffmpeg", "-y", "-i", source, "-filter_complex", video_filter,
                   "-map", "[v]", "-map", "0:a?", *E.video_encode_args(),
                   "-c:a", "copy", "-movflags", "+faststart", destination])
    return (result.returncode == 0 and os.path.exists(destination)
            and os.path.getsize(destination) > 10_000)


def voice_for_provider(choice, provider, index=0):
    choice = str(choice or "auto")
    provider = str(provider or "vivibe").lower()
    if provider == "genmax":
        if choice in E.GENMAX_VOICES:
            return E.GENMAX_VOICES[choice]
        return (os.environ.get("GENMAX_VOICE_ID") or E.CONFIG.get("genmax_voice_id")
                or E.GENMAX_VOICES["adam3"][0], E.GENMAX_VOICES["adam3"][1])
    if choice in E.GENMAX_VOICES:
        choice = "auto"
    if choice in E.VOICE_BY_LABEL:
        return E.VOICE_BY_LABEL[choice], choice
    voices = E.VOICES_NU if choice == "nu" else E.VOICES_NAM
    return voices[index % len(voices)]


def burn_subtitles(source, subtitle, destination, original_subtitles="smart_bottom", workdir=None,
                   subtitle_style=None, subtitle_region=None):
    smart = original_subtitles in ("smart_bottom", "smart_top", "smart_custom")
    position = "top" if original_subtitles.endswith("top") else "bottom"
    region = subtitle_region if original_subtitles == "smart_custom" else None
    render_source = source
    if smart:
        clean_path = os.path.join(workdir or os.path.dirname(destination), "source_without_subtitles.mp4")
        if clean_original_subtitles(source, clean_path, position, region):
            render_source = clean_path
            original_subtitles = "keep"
        else:
            # Neu OpenCV/codec loi, job van hoan thanh bang cach che mo cu.
            original_subtitles = "custom" if region else position
    remove_original = original_subtitles in ("bottom", "top", "custom")
    video_filter = (_replace_subtitle_filter(subtitle, original_subtitles, subtitle_style, region)
                    if remove_original else _subtitle_filter(subtitle, subtitle_style))
    filter_args = (["-filter_complex", video_filter, "-map", "[v]", "-map", "0:a?"]
                   if remove_original else ["-vf", video_filter])
    result = E.ff(["ffmpeg", "-y", "-i", render_source, *filter_args,
                   *E.video_encode_args(), "-c:a", "copy", "-movflags", "+faststart", destination])
    if result.returncode == 0 and os.path.exists(destination) and os.path.getsize(destination) > 10_000:
        return True
    # QSV may be unavailable and some source audio codecs cannot be copied into MP4.
    result = E.ff(["ffmpeg", "-y", "-i", render_source, *filter_args,
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                   "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", destination])
    return result.returncode == 0 and os.path.exists(destination) and os.path.getsize(destination) > 10_000


def _prepare_source(item, workdir):
    if item.get("type") == "link":
        source, metadata = E.download(item["url"], workdir)
        return source, metadata.get("title") or "Video dịch"
    source = item.get("path")
    return source, item.get("name") or os.path.basename(source or "") or "Video dịch"


def summary_target_seconds(duration, requested="auto"):
    """Default to one sixth of the movie: 60 minutes becomes about 10 minutes."""
    if requested and str(requested) != "auto":
        try:
            return min(duration, max(60.0, float(requested) * 60.0))
        except (TypeError, ValueError):
            pass
    if duration <= 180.0:
        return max(10.0, duration * 0.5)
    return min(duration, max(180.0, min(1200.0, duration / 6.0)))


def summarize_transcript_chapters(segments, duration, chapter_seconds=300.0):
    """Read the movie transcript into a chronological outline without selecting footage yet."""
    summaries = []
    chapter_count = max(1, int((duration + chapter_seconds - 0.001) // chapter_seconds))
    for chapter in range(chapter_count):
        start, end = chapter * chapter_seconds, min(duration, (chapter + 1) * chapter_seconds)
        items = [item for item in segments
                 if float(item.get("end", 0)) > start and float(item.get("start", 0)) < end]
        if not items:
            continue
        payload = [{"start": round(float(item.get("start", 0)), 1),
                    "end": round(float(item.get("end", 0)), 1),
                    "text": item.get("translated") or item.get("text", "")}
                   for item in items]
        prompt = f"""Đọc lời thoại chương {chapter + 1} của phim, từ {start:.0f}s đến {end:.0f}s.
Tóm tắt 3-6 câu theo đúng trình tự: nhân vật, hành động, nguyên nhân, xung đột, phát hiện và kết quả.
Không chọn cảnh ở bước này và không bịa ngoài dữ liệu. Trả DUY NHẤT JSON: {{"summary":"..."}}.
Transcript: {json.dumps(payload, ensure_ascii=False)}"""
        result = E.codex_json(prompt, effort="medium", timeout=600) or {}
        summary = str(result.get("summary") or "").strip() if isinstance(result, dict) else ""
        if not summary:
            summary = " ".join(str(item.get("translated") or item.get("text") or "")
                               for item in items[:20]).strip()
        if summary:
            summaries.append(summary)
    return summaries


def summary_scene_scaffold(duration, target_seconds):
    """Create fallback slots/count only; final timestamps are selected after TXT is written."""
    if duration <= 0:
        return []
    count = max(2, min(40, int(round(target_seconds / 15.0)) or 2))
    count = min(count, max(1, int(duration // 2)))
    target_seconds = min(duration, max(2.0 * count, target_seconds))
    shot_length = target_seconds / count
    if count == 1:
        return [[0.0, duration]]
    gap = max(0.0, (duration - target_seconds) / (count - 1))
    ranges, cursor = [], 0.0
    for _ in range(count):
        right = min(duration, cursor + shot_length)
        ranges.append([cursor, right])
        cursor = right + gap
    ranges[-1][1] = duration
    ranges[-1][0] = max(ranges[-2][1], duration - shot_length)
    return ranges


def _short_video_ranges(duration, target_seconds):
    """Spread short summary shots across the story and always retain its ending."""
    if duration <= 0:
        return []
    shot_count = 5 if duration <= 60 else 6 if duration <= 120 else 8
    shot_count = max(1, min(shot_count, int(duration // 2) or 1))
    target_seconds = min(duration, max(2.0 * shot_count, target_seconds))
    shot_length = target_seconds / shot_count
    if shot_count == 1:
        return [[0.0, duration]]
    gap = max(0.0, (duration - target_seconds) / (shot_count - 1))
    ranges = []
    cursor = 0.0
    for index in range(shot_count):
        left = cursor
        right = min(duration, left + shot_length)
        ranges.append([left, right])
        cursor = right + gap
    ranges[-1][1] = duration
    ranges[-1][0] = max(ranges[-2][1], duration - shot_length)
    return ranges


def _fallback_range(start, end, target):
    length = min(max(2.0, target), max(2.0, end - start))
    middle = (start + end) / 2.0
    return [max(start, middle - length / 2.0), min(end, middle + length / 2.0)]


def analyze_visual_chapters(source, segments, duration, workdir, chapter_seconds=300.0):
    """Build a timestamped visual scene catalogue for matching the finished TXT to footage."""
    catalogue = []
    chapter_count = max(1, int((duration + chapter_seconds - 0.001) // chapter_seconds))
    for chapter in range(chapter_count):
        start = chapter * chapter_seconds
        end = min(duration, (chapter + 1) * chapter_seconds)
        if end <= start:
            continue
        frame_count = max(4, min(12, int((end - start) // 18) or 4))
        times = [start + (index + 0.5) * (end - start) / frame_count
                 for index in range(frame_count)]
        frame_pattern = os.path.join(workdir, f"visual_{chapter:03d}_%02d.jpg")
        frames = []
        for index, second in enumerate(times):
            frame = frame_pattern % index
            result = E.ff(["ffmpeg", "-y", "-ss", f"{second:.3f}", "-i", source,
                           "-vf", ("scale=360:-2,drawtext=text='" + f"{second:.0f}s" +
                                   "':x=8:y=8:fontsize=30:fontcolor=yellow:"
                                   "box=1:boxcolor=black@0.65:boxborderw=5"),
                           "-frames:v", "1", frame])
            if result.returncode == 0 and os.path.exists(frame):
                frames.append(frame)
        if not frames:
            continue
        montage = os.path.join(workdir, f"visual_{chapter:03d}.jpg")
        E.ff(["ffmpeg", "-y", "-framerate", "1", "-i", frame_pattern,
              "-vf", "tile=4x3:padding=5:color=black", "-frames:v", "1", montage])
        if not os.path.exists(montage):
            continue
        dialogue = [{"start": round(float(item.get("start", 0)), 1),
                     "end": round(float(item.get("end", 0)), 1),
                     "text": str(item.get("translated") or item.get("text") or "")[:300]}
                    for item in segments
                    if float(item.get("end", 0)) > start and float(item.get("start", 0)) < end]
        prompt = f"""Phân tích montage của phim từ {start:.1f}s đến {end:.1f}s. Mỗi ô đã ghi timestamp.
Tạo danh mục các cảnh/sự kiện nhìn thấy được để biên tập viên ghép hình cho bản tóm tắt.
Ưu tiên hành động, nhân vật, địa điểm, thay đổi quan trọng và cả cảnh im lặng không có thoại.
Không suy đoán chi tiết không thấy trong hình hoặc lời thoại. Mỗi cảnh dài 3-40 giây.
Trả DUY NHẤT JSON: {{"scenes":[{{"start":0.0,"end":8.0,"visual":"mô tả hình ảnh",
"event":"sự kiện/công dụng trong mạch truyện"}}]}}
Lời thoại có mốc để đối chiếu: {json.dumps(dialogue, ensure_ascii=False)}"""
        result = E.codex_json(prompt, image=montage, effort="medium", timeout=600) or {}
        for item in result.get("scenes", []) if isinstance(result, dict) else []:
            try:
                left = max(start, float(item.get("start")))
                right = min(end, float(item.get("end")))
            except (TypeError, ValueError):
                continue
            visual = " ".join(str(item.get("visual") or "").split())
            event = " ".join(str(item.get("event") or "").split())
            if right - left >= 2 and (visual or event):
                catalogue.append({"start": round(left, 2), "end": round(right, 2),
                                  "visual": visual[:500], "event": event[:500]})
    return sorted(catalogue, key=lambda item: (item["start"], item["end"]))


def select_summary_ranges(segments, duration, target_seconds):
    """Use transcript chapters to select chronological, story-bearing source ranges."""
    chapter_size = 300.0
    ranges, chapter_summaries = [], []
    chapter_count = max(1, int((duration + chapter_size - 0.001) // chapter_size))
    for chapter in range(chapter_count):
        start, end = chapter * chapter_size, min(duration, (chapter + 1) * chapter_size)
        items = [s for s in segments if float(s.get("end", 0)) > start and float(s.get("start", 0)) < end]
        if not items:
            continue
        share = target_seconds * (end - start) / max(1.0, duration)
        payload = [{"start": round(float(s["start"]), 1), "end": round(float(s["end"]), 1),
                    "text": s.get("translated") or s.get("text", "")} for s in items]
        prompt = f"""Bạn là biên tập viên tóm tắt phim. Đây là lời thoại chương {chapter + 1}, từ {start:.0f}s đến {end:.0f}s.
Chọn 2-5 khoảng video nguồn quan trọng, theo đúng thứ tự, tổng gần {share:.0f} giây. Mỗi khoảng 5-40 giây.
Ưu tiên tình tiết làm thay đổi câu chuyện, giới thiệu nhân vật, xung đột, nút thắt và kết quả; bỏ cảnh lặp hoặc lời không quan trọng.
Viết thêm tóm tắt chương 2-4 câu bằng tiếng Việt. Không bịa ngoài transcript.
Trả DUY NHẤT JSON: {{"summary":"...","ranges":[{{"start":0.0,"end":10.0}}]}}
Transcript: {json.dumps(payload, ensure_ascii=False)}"""
        result = E.codex_json(prompt, effort="medium", timeout=600) or {}
        summary = str(result.get("summary") or "").strip() if isinstance(result, dict) else ""
        if not summary:
            summary = " ".join(str(item.get("translated") or item.get("text") or "") for item in items[:12]).strip()
        if summary:
            chapter_summaries.append(summary)
        chosen = []
        for value in (result.get("ranges") or []) if isinstance(result, dict) else []:
            try:
                left = max(start, float(value["start"])); right = min(end, float(value["end"]))
            except (KeyError, TypeError, ValueError):
                continue
            if right - left >= 2.0:
                chosen.append([left, right])
        if not chosen:
            chosen = [_fallback_range(start, end, share)]
        ranges.extend(chosen)

    ranges.sort(key=lambda value: value[0])
    # Remove overlap and keep the result close to the requested duration.
    merged = []
    for left, right in ranges:
        if merged and left <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    total = sum(right - left for left, right in merged)
    if merged and total < target_seconds * 0.88:
        factor = target_seconds / max(1.0, total)
        expanded = []
        for left, right in merged:
            middle = (left + right) / 2.0
            length = (right - left) * factor
            new_left, new_right = max(0.0, middle - length / 2.0), min(duration, middle + length / 2.0)
            expanded.append([new_left, new_right])
        merged = []
        for left, right in expanded:
            if merged and left <= merged[-1][1] + 0.5:
                merged[-1][1] = max(merged[-1][1], right)
            else:
                merged.append([left, right])
        total = sum(right - left for left, right in merged)
    if total > target_seconds * 1.12:
        scale = target_seconds / total
        merged = [[left, left + (right - left) * scale] for left, right in merged]
    return merged, chapter_summaries


def write_story_summary(chapter_summaries, path, target_seconds=600, use_hook_skill=False,
                        visual_catalogue=None):
    source = "\n".join(f"Chương {i + 1}: {text}" for i, text in enumerate(chapter_summaries))
    target_words = max(80, int(target_seconds * 2.4))
    hook_rules = (f"""\n{TIKTOK_HOOK_3S_SKILL}
Áp dụng cho bản Reup: âm thầm tạo và chấm 5 ứng viên, rồi mở bản thuyết minh bằng ứng viên
tốt nhất. Câu hook đầu tối đa 12 từ, đọc được trong khoảng 3 giây; ngay sau hook phải kể
tiếp đúng mạch nội dung và trả payoff, không biến video thành quảng cáo.""" if use_hook_skill else "")
    visual_source = json.dumps(visual_catalogue or [], ensure_ascii=False)
    prompt = f"""Từ dữ liệu đọc phim dưới đây, viết lời THUYẾT MINH tóm tắt phim bằng tiếng Việt, khoảng {target_words} từ.
Kể mạch lạc theo diễn biến từ mở đầu, xung đột, cao trào đến kết thúc. Câu văn tự nhiên để đọc thành tiếng,
có câu chuyển cảnh giữa các sự kiện, không dùng tiêu đề hay gạch đầu dòng. Kết hợp lời thoại và sự kiện nhìn thấy,
nhưng không thêm chi tiết không có trong dữ liệu. Đây là TXT chuẩn; bước dựng hình sau sẽ phục vụ đúng TXT này.
Chỉ in lời thuyết minh thuần, không lời dẫn của AI.{hook_rules}
TÓM TẮT THEO CHƯƠNG:\n{source}
DANH MỤC CẢNH NHÌN THẤY CÓ TIMESTAMP:\n{visual_source}"""
    text = E.codex(prompt, effort="medium", timeout=600).strip()
    if not text:
        text = source
    with open(path, "w", encoding="utf-8-sig") as handle:
        handle.write(text)
    return text


def reup_caption(story_text, title=""):
    """Create one central, factual sentence for the rendered Reup video."""
    source = " ".join(str(story_text or "").split())[:6000]
    prompt = f"""Viết đúng MỘT CÂU caption tiếng Việt nêu nội dung trọng tâm nhất của video Reup.
Caption phải cụ thể, dễ hiểu, trung thực với bản tóm tắt; không bịa, không hashtag, không CTA,
không ghi nhãn "Caption:" và không xuống dòng. Tối đa 180 ký tự.
Tên video: {title}
Bản tóm tắt: {source}"""
    value = " ".join(E.codex(prompt, effort="low", timeout=300).strip().split())
    value = re.sub(r"^(?:caption\s*:|[-–—]\s*)", "", value, flags=re.I).strip(' "“”')
    value = re.sub(r"\s*#\S+", "", value).strip()
    ending = re.search(r"[.!?](?=\s|$)", value)
    if ending:
        value = value[:ending.end()]
    if len(value) > 180:
        value = value[:177].rsplit(" ", 1)[0].rstrip(" ,;:-") + "..."
    if not value:
        fallback = source or str(title or "Video Reup").strip()
        value = fallback[:180].strip()
    return value


def _transcript_windows(segments, window_seconds=12.0):
    """Condense timestamped dialogue for matching a finished summary to source scenes."""
    windows = []
    for segment in segments:
        start = float(segment.get("start", 0)); end = float(segment.get("end", start))
        value = str(segment.get("translated") or segment.get("text") or "").strip()
        if not value:
            continue
        bucket = int(start // window_seconds)
        if windows and windows[-1]["bucket"] == bucket:
            windows[-1]["end"] = max(windows[-1]["end"], end)
            windows[-1]["text"] = (windows[-1]["text"] + " " + value)[:900]
        else:
            windows.append({"bucket": bucket, "start": round(start, 1),
                            "end": round(end, 1), "text": value[:900]})
    for item in windows:
        item.pop("bucket", None)
    return windows


def _split_story_text(story_text, count):
    """Deterministically preserve all TXT words when AI alignment is unavailable."""
    words = str(story_text or "").split()
    if count <= 0:
        return []
    if not words:
        return ["Câu chuyện tiếp tục với một diễn biến quan trọng."] * count
    chunks = []
    for index in range(count):
        left = round(index * len(words) / count)
        right = round((index + 1) * len(words) / count)
        chunks.append(" ".join(words[left:right]).strip() or words[min(left, len(words) - 1)])
    return chunks


def align_story_to_shots(story_text, segments, duration, target_seconds, fallback_ranges,
                         use_hook_skill=False, visual_catalogue=None):
    """Split the finished TXT first, then select matching timestamped source scenes."""
    shot_count = len(fallback_ranges)
    hook_rules = (f"""\n{TIKTOK_HOOK_3S_SKILL}
Riêng item 0 phải chọn cảnh có visual stopper mạnh nhất nhưng vẫn đúng phần mở đầu TXT;
cảnh sau phải chứa payoff cho câu hook. Không được đảo sai trình tự câu chuyện."""
                  if use_hook_skill else "")
    prompt = f"""Bạn là biên tập viên dựng video. BẢN TÓM TẮT TXT dưới đây là nội dung chuẩn và là xương sống.
Chia ý nghĩa của TXT thành đúng {shot_count} đoạn thuyết minh liên tục, sau đó chọn cho mỗi đoạn cảnh phù hợp nhất từ danh mục hình ảnh và transcript có mốc thời gian.

Quy tắc bắt buộc:
- Khi ghép text theo id phải bao quát toàn bộ TXT, không tạo câu chuyện mới.
- Cảnh phải đúng nội dung text, theo thứ tự thời gian và không chồng lấn.
- Ưu tiên DANH MỤC CẢNH NHÌN THẤY; transcript chỉ hỗ trợ xác nhận sự kiện và nhân vật.
- Có thể chọn cảnh im lặng nếu hình ảnh minh họa đúng TXT.
- Tổng cảnh gần {target_seconds:.1f} giây; mỗi cảnh 5-40 giây.
- Item đầu là mở đầu; item cuối dùng cảnh kết quả/kết thúc gần cuối nguồn {duration:.1f}s.
- Có đúng các id từ 0 đến {shot_count - 1}.{hook_rules}
Trả DUY NHẤT JSON: {{"items":[{{"id":0,"text":"...","start":0.0,"end":10.0}}]}}

BẢN TÓM TẮT TXT:
{story_text}

TRANSCRIPT CÓ MỐC:
{json.dumps(_transcript_windows(segments), ensure_ascii=False)}

DANH MỤC CẢNH NHÌN THẤY CÓ MỐC:
{json.dumps(visual_catalogue or [], ensure_ascii=False)}"""
    result = E.codex_json(prompt, effort="high", timeout=900) or {}
    aligned = {}
    for item in result.get("items", []) if isinstance(result, dict) else []:
        try:
            index = int(item.get("id")); start = float(item.get("start")); end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        value = str(item.get("text") or "").strip()
        if 0 <= index < shot_count and value and 0 <= start < end <= duration and end - start >= 2:
            aligned[index] = (start, end, value)
    if len(aligned) != shot_count:
        return fallback_ranges, _split_story_text(story_text, shot_count)

    ordered = [aligned[index] for index in range(shot_count)]
    ranges, narrations, previous_end = [], [], 0.0
    for start, end, value in ordered:
        start = max(previous_end, start)
        if end - start < 2:
            return fallback_ranges, _split_story_text(story_text, shot_count)
        ranges.append([start, end]); narrations.append(value); previous_end = end
    return ranges, narrations


def remap_summary_segments(segments, ranges):
    result, offset = [], 0.0
    for left, right in ranges:
        for segment in segments:
            seg_start, seg_end = float(segment["start"]), float(segment["end"])
            if seg_end <= left or seg_start >= right:
                continue
            copy = dict(segment)
            copy["start"] = offset + max(seg_start, left) - left
            copy["end"] = offset + min(seg_end, right) - left
            if copy["end"] - copy["start"] >= 0.15:
                result.append(copy)
        offset += right - left
    return result


def build_summary_narrations(segments, ranges, chapter_summaries):
    """Viết một mạch tóm tắt toàn phim rồi chia nó vào đúng các shot."""
    payload = []
    for index, (left, right) in enumerate(ranges):
        dialogue = [str(item.get("translated") or item.get("text") or "").strip()
                    for item in segments
                    if float(item.get("end", 0)) > left and float(item.get("start", 0)) < right]
        duration = max(2.0, right - left)
        payload.append({"id": index, "start": round(left, 2), "end": round(right, 2),
                        "max_words": max(12, int(duration * 2.6)),
                        "shot_content": " ".join(filter(None, dialogue))[:1600]})

    narrations = [""] * len(payload)
    outline = "\n".join(f"Chương {index + 1}: {text}"
                         for index, text in enumerate(chapter_summaries) if str(text).strip())
    prompt = f"""Bạn là biên tập viên dựng video TÓM TẮT TOÀN BỘ PHIM bằng tiếng Việt.

ĐỀ CƯƠNG TOÀN PHIM:
{outline}

Các shot dưới đây đã được chọn theo đúng thứ tự thời gian. Hãy viết MỘT MẠCH THUYẾT MINH LIÊN TỤC
từ mở đầu, phát triển, xung đột, cao trào đến kết thúc, rồi chia mạch kể đó thành đúng {len(payload)} đoạn.
Mỗi đoạn gắn với một id để phát trên shot tương ứng.

Yêu cầu bắt buộc:
- Đây là tóm tắt toàn phim, KHÔNG phải danh sách tóm tắt từng shot độc lập.
- Các đoạn phải nối ý tự nhiên; dùng câu chuyển tiếp khi thời gian hoặc bối cảnh thay đổi.
- Tình tiết kể ở mỗi id phải phù hợp shot_content của chính id; không đọc trước tình tiết của shot sau.
- Có thể dùng đề cương để giữ tên nhân vật, quan hệ nhân quả và mạch truyện, nhưng không bịa ngoài dữ liệu.
- Mỗi id xuất hiện đúng một lần, đúng thứ tự và không vượt max_words để giọng vừa hình.
- Không tiêu đề, không gạch đầu dòng trong text.

Trả DUY NHẤT JSON: {{"items":[{{"id":0,"text":"..."}}]}}
SHOTS: {json.dumps(payload, ensure_ascii=False)}"""
    result = E.codex_json(prompt, effort="high", timeout=900) or {}
    for item in result.get("items", []) if isinstance(result, dict) else []:
        try:
            index = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(narrations):
            narrations[index] = str(item.get("text") or "").strip()

    for item in payload:
        index = item["id"]
        if narrations[index]:
            continue
        words = item["shot_content"].split()
        narrations[index] = " ".join(words[:item["max_words"]]).strip()
        if not narrations[index]:
            narrations[index] = "Câu chuyện tiếp tục với một diễn biến quan trọng."
    return narrations


def _synced_shot_duration(shot_duration, raw_voice_duration=None, mixed_audio_duration=None,
                          tail_pause=0.25):
    """Match picture to speech, allowing only renderer delay + a short natural tail."""
    if raw_voice_duration is not None and float(raw_voice_duration) > 0:
        return max(0.8, float(raw_voice_duration) + 0.3 + float(tail_pause))
    if mixed_audio_duration is not None and float(mixed_audio_duration) > 0:
        return max(0.8, float(mixed_audio_duration) + 0.25)
    return max(0.8, float(shot_duration))


GENMAX_SCENE_EMOTIONS = {
    "excited", "laughing", "giggles", "proud", "surprised", "realization",
    "gasping", "questioning", "conversational", "storytelling", "thoughtful",
    "speaking fast", "yelling", "shouting", "announcer style", "scoff", "sighs",
    "clears throat", "whisper", "ASMR style",
}


def _same_spoken_words(left, right):
    words = lambda value: re.findall(r"\w+", str(value or "").casefold(), flags=re.UNICODE)
    return words(left) == words(right)


def apply_scene_emotions(narrations, provider, enabled=True, supports_tags=True):
    """Add scene-aware emotion and punctuation without changing spoken words."""
    values = [str(value or "").strip() for value in narrations]
    if not enabled or str(provider or "").lower() != "genmax" or not values:
        return values
    payload = [{"id": index, "text": value} for index, value in enumerate(values)]
    prompt = f"""Chọn cảm xúc giọng đọc GenMax phù hợp cho từng shot của video kể chuyện Reup.
Mỗi item dùng 1-2 tag trong danh sách: {sorted(GENMAX_SCENE_EMOTIONS)}.
Hook đầu ưu tiên có lực/tò mò; diễn biến kể tự nhiên; cao trào tăng cảm xúc; đoạn cuối ấm và đọng lại.
Chỉ được chỉnh dấu phẩy, chấm, hỏi, cảm thán và dấu ba chấm trong text để tạo nhịp nghỉ tự nhiên. Tuyệt đối không thêm, bớt hoặc đổi từ.
Không lạm dụng yelling/shouting và không dùng long pause.
Trả DUY NHẤT JSON: {{"items":[{{"id":0,"tags":["storytelling"],"text":"Nội dung, có nhịp nghỉ."}}]}}
Các shot: {json.dumps(payload, ensure_ascii=False)}"""
    result = E.codex_json(prompt, effort="low", timeout=300) or {}
    selected = {}
    for item in result.get("items", []) if isinstance(result, dict) else []:
        try:
            index = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        tags = [str(tag).strip() for tag in (item.get("tags") or [])]
        punctuated = str(item.get("text") or values[index] if 0 <= index < len(values) else "").strip()
        selected[index] = {
            "tags": [tag for tag in tags if tag in GENMAX_SCENE_EMOTIONS][:2],
            "text": punctuated,
        }
    output = []
    for index, value in enumerate(values):
        choice = selected.get(index) or {}
        tags = choice.get("tags") or (["speaking fast", "questioning"] if index == 0
                                      else ["thoughtful"] if index == len(values) - 1
                                      else ["storytelling"])
        punctuated = choice.get("text") or value
        if not _same_spoken_words(value, punctuated):
            punctuated = value
        prefix = "".join(f"[{tag}]" for tag in tags) + " " if supports_tags else ""
        output.append(prefix + punctuated)
    return output


def _summary_tail_pause(narration, index, total):
    """Leave a deliberate beat at scene boundaries instead of joining every line evenly."""
    text = str(narration or "").strip().lower()
    if index == total - 1:
        return 0.7
    if any(tag in text for tag in ("[thoughtful]", "[realization]", "[sighs]", "[whisper]")):
        return 0.5
    if text.rstrip().endswith(("?", "!", "…")):
        return 0.45
    return 0.3


def _summary_subtitle_segments(narration, shot_duration, target_duration,
                               raw_voice_duration=None, tail_pause=0.3):
    """Build short subtitle cues aligned with the renderer's 300 ms voice delay."""
    text = re.sub(r"\[[^\]]+\]", "", str(narration or "")).strip()
    if not text:
        return []
    chunks = []
    for sentence in re.split(r"(?<=[.!?…])\s+|\s*[,;:]\s+", text):
        words = sentence.split()
        while words:
            chunks.append(" ".join(words[:12]))
            words = words[12:]
    if not chunks:
        chunks = [text]
    voice_start = 0.3
    if raw_voice_duration is not None and float(raw_voice_duration) > 0:
        # The renderer speeds long speech up to fit the shot budget.
        voice_duration = min(float(raw_voice_duration), max(0.5, float(shot_duration) - 0.55))
    else:
        voice_duration = max(0.35, float(target_duration) - voice_start - float(tail_pause))
    voice_end = min(float(target_duration) - max(0.12, float(tail_pause)),
                    voice_start + voice_duration)
    usable = max(0.35, voice_end - voice_start)
    weights = [max(1, len(chunk.split())) for chunk in chunks]
    total_weight = sum(weights)
    cues = []
    cursor = voice_start
    for index, (chunk, weight) in enumerate(zip(chunks, weights)):
        share = usable * weight / total_weight
        end = voice_end if index == len(chunks) - 1 else min(voice_end, cursor + share)
        cues.append({"start": cursor, "end": max(cursor + 0.25, end - 0.06),
                     "translated": chunk})
        cursor = end
    return cues


def create_synced_summary_video(source, ranges, narrations, destination, workdir,
                                voice_id, job, save, subtitle_style=None):
    """Tạo tiếng riêng cho từng shot rồi mới nối, tránh lời chạy sang cảnh khác."""
    rendered_clips = []
    for index, ((start, end), narration) in enumerate(zip(ranges, narrations)):
        clip = os.path.join(workdir, f"summary_sync_source_{index:03d}.mp4")
        voiced = os.path.join(workdir, f"summary_sync_voice_{index:03d}.mp4")
        script = os.path.join(workdir, f"summary_sync_{index:03d}.txt")
        with open(script, "w", encoding="utf-8-sig") as handle:
            handle.write(narration)
        base_args = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", source,
                     "-t", f"{end-start:.3f}", "-map", "0:v:0", "-map", "0:a?"]
        result = E.ff([*base_args, *E.video_encode_args(), "-c:a", "aac", "-ar", "44100",
                       "-ac", "2", "-avoid_negative_ts", "make_zero", clip])
        if result.returncode != 0:
            result = E.ff([*base_args, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                           "-c:a", "aac", "-ar", "44100", "-ac", "2",
                           "-avoid_negative_ts", "make_zero", clip])
        if result.returncode != 0 or not os.path.exists(clip):
            return False, f"Không cắt được shot {index + 1}"
        E.log(f"[{job['id']}] Đồng bộ giọng với shot {index + 1}/{len(ranges)}...", job); save(job)
        rendered = E.render(clip, script, voiced, voice_id,
                            {"original": 0.08, "bed": None, "bedvol": 0.07}, workdir=workdir)
        if not rendered.get("ok"):
            return False, rendered.get("err") or f"Không tạo được tiếng cho shot {index + 1}"
        # Reuse the mixed narration but make picture duration follow the actual voice.
        # This avoids long silent tails and also prevents long narration being cut.
        synced = os.path.join(workdir, f"summary_sync_fixed_{index:03d}.mp4")
        shot_duration = max(0.1, end - start)
        raw_voice_duration = rendered.get("voice_dur")
        mixed_audio_duration = None if raw_voice_duration else E.probe(voiced)
        tail_pause = _summary_tail_pause(narration, index, len(narrations))
        target_duration = _synced_shot_duration(
            shot_duration, raw_voice_duration, mixed_audio_duration, tail_pause)
        subtitle_path = os.path.join(workdir, f"summary_sync_{index:03d}.srt")
        subtitle_segments = _summary_subtitle_segments(
            narration, shot_duration, target_duration, raw_voice_duration, tail_pause)
        write_srt(subtitle_segments, subtitle_path)
        video_filters = [_subtitle_filter(subtitle_path, subtitle_style)]
        if target_duration > shot_duration + 0.05:
            stretch = target_duration / shot_duration
            video_filters.insert(0, f"setpts={stretch:.6f}*PTS")
        video_args = ["-vf", ",".join(video_filters), *E.video_encode_args()]
        fixed = E.ff(["ffmpeg", "-y", "-i", clip, "-i", voiced,
                      "-map", "0:v:0", "-map", "1:a:0", *video_args,
                      "-c:a", "aac", "-af", f"apad=pad_dur={tail_pause:.2f}",
                      "-t", f"{target_duration:.3f}", "-movflags", "+faststart", synced])
        if fixed.returncode != 0 or not os.path.exists(synced) or os.path.getsize(synced) <= 10_000:
            return False, f"Không đồng bộ được thời lượng hình và giọng cho shot {index + 1}"
        rendered_clips.append(synced)

    concat_file = os.path.join(workdir, "summary_sync_concat.txt")
    with open(concat_file, "w", encoding="utf-8") as handle:
        for clip in rendered_clips:
            handle.write("file '" + clip.replace("'", "'\\''") + "'\n")
    result = E.ff(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file,
                   "-c", "copy", "-movflags", "+faststart", destination])
    return (result.returncode == 0 and os.path.exists(destination)
            and os.path.getsize(destination) > 10_000), "Không nối được các shot đã thuyết minh"


def _atempo_filter(rate):
    parts = []
    rate = max(0.05, float(rate))
    while rate > 2.0:
        parts.append("atempo=2.0"); rate /= 2.0
    while rate < 0.5:
        parts.append("atempo=0.5"); rate /= 0.5
    parts.append(f"atempo={rate:.6f}")
    return ",".join(parts)


def _synthesize_dub_line(text, provider, voice_id, destination):
    renderer = E._load_ra()
    if provider == "genmax":
        renderer.run_genmax_tts_with_retry(text, voice_id, Path(destination), attempts=3)
    else:
        renderer.run_vivibe_tts_with_retry(text, voice_id, Path(destination), 1.0, attempts=3)
    return E.probe(destination)


def create_dub_voice_timeline(segments, duration, workdir, provider, voice_id, job, save):
    """Tạo WAV tiếng Việt với từng câu nằm đúng khe thời gian của lời gốc."""
    fitted = []
    ordered = sorted(segments, key=lambda item: float(item.get("start", 0)))
    for index, segment in enumerate(ordered):
        text = str(segment.get("translated") or "").strip()
        start = max(0.0, float(segment.get("start", 0)))
        end = min(duration, max(start + 0.35, float(segment.get("end", start + 0.35))))
        if not text or end <= start:
            continue
        raw = os.path.join(workdir, f"dub_raw_{index:04d}.audio")
        line = os.path.join(workdir, f"dub_line_{index:04d}.wav")
        segment_voice = str(segment.get("voice") or "").strip()
        line_provider = "genmax" if segment_voice in E.GENMAX_VOICES else provider
        line_voice_id, line_voice_label = ((voice_id, "mặc định") if not segment_voice else
                                           voice_for_provider(segment_voice, line_provider, index))
        speaker = str(segment.get("speaker") or "").strip()
        detail = (f" · {speaker} · {line_voice_label} ({line_provider})"
                  if speaker or segment_voice else "")
        E.log(f"[{job['id']}] Tạo câu lồng tiếng {index + 1}/{len(ordered)}{detail}...", job); save(job)
        raw_duration = _synthesize_dub_line(text, line_provider, line_voice_id, raw)
        slot = max(0.35, end - start)
        tempo = max(1.0, raw_duration / slot)
        result = E.ff(["ffmpeg", "-y", "-i", raw, "-vn", "-af",
                       f"{_atempo_filter(tempo)},apad,atrim=0:{slot:.4f}",
                       "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", line])
        if result.returncode != 0 or not os.path.exists(line):
            raise RuntimeError(f"Không căn được câu lồng tiếng {index + 1}")
        fitted.append((start, end, line))
    if not fitted:
        raise RuntimeError("Không tạo được câu lồng tiếng Việt")

    timeline = os.path.join(workdir, "dub_voice_timeline.wav")
    rate, channels, sample_width = 44100, 2, 2
    current_frame = 0
    with wave.open(timeline, "wb") as output:
        output.setnchannels(channels); output.setsampwidth(sample_width); output.setframerate(rate)
        for start, end, line in fitted:
            target_frame = max(current_frame, int(round(start * rate)))
            silence_frames = target_frame - current_frame
            while silence_frames > 0:
                chunk = min(silence_frames, rate * 10)
                output.writeframesraw(b"\0" * chunk * channels * sample_width)
                current_frame += chunk; silence_frames -= chunk
            with wave.open(line, "rb") as source_line:
                frames = source_line.readframes(
                    min(source_line.getnframes(), int(round((end - start) * rate))))
            output.writeframesraw(frames)
            current_frame += len(frames) // (channels * sample_width)
        final_frame = int(round(duration * rate))
        while current_frame < final_frame:
            chunk = min(final_frame - current_frame, rate * 10)
            output.writeframesraw(b"\0" * chunk * channels * sample_width)
            current_frame += chunk
    return timeline


def create_dubbed_video(source, segments, destination, workdir, provider, voice_id, job, save):
    """Remove the original dialogue, then mix timestamp-aligned Vietnamese speech."""
    E.log(f"[{job['id']}] Đang tách giọng nói gốc bằng Demucs...", job); save(job)
    demucs_dir = os.path.join(workdir, "dub_demucs")
    os.makedirs(demucs_dir, exist_ok=True)
    bed = E.autovoice_audio("mixed_voice_music", source, demucs_dir).get("bed")
    if not bed or not os.path.exists(bed):
        raise RuntimeError("Demucs không tách được giọng gốc; đã dừng để tránh chồng hai giọng")
    timeline = create_dub_voice_timeline(
        segments, E.probe(source), workdir, provider, voice_id, job, save)
    result = E.ff(["ffmpeg", "-y", "-i", source, "-i", bed, "-i", timeline,
                   "-filter_complex", "[1:a]volume=0.92[bed];[2:a]volume=1.35[voice];"
                   "[bed][voice]amix=inputs=2:duration=first:dropout_transition=0,"
                   "alimiter=limit=0.95[a]", "-map", "0:v:0", "-map", "[a]",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                   "-movflags", "+faststart", destination])
    return result.returncode == 0 and os.path.exists(destination) and os.path.getsize(destination) > 10_000


def prepare_translation_source(source, destination, speed=1.0, flip="none"):
    """Apply the selected movie speed and mirror before transcription/rendering."""
    speed = float(speed)
    video_filters = []
    if flip == "hflip":
        video_filters.append("hflip")
    if abs(speed - 1.0) > 0.001:
        video_filters.append(f"setpts=PTS/{speed:.6f}")
    if not video_filters:
        return source
    command = ["ffmpeg", "-y", "-i", source, "-vf", ",".join(video_filters),
               "-map", "0:v:0", "-map", "0:a?", *E.video_encode_args()]
    if E._load_ra().has_audio(Path(source)):
        command += ["-af", _atempo_filter(speed), "-c:a", "aac", "-b:a", "192k"]
    command += ["-movflags", "+faststart", destination]
    result = E.ff(command)
    if result.returncode != 0:
        command = ["ffmpeg", "-y", "-i", source, "-vf", ",".join(video_filters),
                   "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "20"]
        if E._load_ra().has_audio(Path(source)):
            command += ["-af", _atempo_filter(speed), "-c:a", "aac", "-b:a", "192k"]
        command += ["-movflags", "+faststart", destination]
        result = E.ff(command)
    if result.returncode != 0 or not os.path.exists(destination) or os.path.getsize(destination) <= 10_000:
        raise RuntimeError("Không áp dụng được tốc độ/lật hình cho phim nguồn")
    return destination


def create_summary_video(source, ranges, destination, workdir):
    clips = []
    for index, (start, end) in enumerate(ranges):
        clip = os.path.join(workdir, f"summary_clip_{index:03d}.mp4")
        base_args = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", source, "-t", f"{end-start:.3f}",
                     "-map", "0:v:0", "-map", "0:a?"]
        result = E.ff([*base_args, *E.video_encode_args(), "-c:a", "aac", "-ar", "44100", "-ac", "2",
                       "-avoid_negative_ts", "make_zero", clip])
        if result.returncode != 0:
            result = E.ff([*base_args, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                           "-c:a", "aac", "-ar", "44100", "-ac", "2",
                           "-avoid_negative_ts", "make_zero", clip])
        if result.returncode == 0 and os.path.exists(clip):
            clips.append(clip)
    if not clips:
        return False
    concat_file = os.path.join(workdir, "summary_concat.txt")
    with open(concat_file, "w", encoding="utf-8") as handle:
        for clip in clips:
            handle.write("file '" + clip.replace("'", "'\\''") + "'\n")
    result = E.ff(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file,
                   "-c", "copy", "-movflags", "+faststart", destination])
    return result.returncode == 0 and os.path.exists(destination) and os.path.getsize(destination) > 10_000


def make_summary_outputs(source, translated, title, outdir, workdir, base, requested_minutes,
                         voice_choice, job, save, use_hook_skill=False, movie_title="",
                         subtitle_style=None):
    duration = E.probe(source)
    target = summary_target_seconds(duration, requested_minutes)
    E.log(f"[{job['id']}] Đọc lời thoại và lập dàn ý phim...", job); save(job)
    chapters = summarize_transcript_chapters(translated, duration)
    E.log(f"[{job['id']}] Phân tích hình ảnh và lập danh mục cảnh có timestamp...", job); save(job)
    visual_catalogue = analyze_visual_chapters(source, translated, duration, workdir)
    if not chapters and not visual_catalogue:
        raise RuntimeError("Không đọc được lời thoại hoặc hình ảnh để tóm tắt phim")
    if visual_catalogue:
        E.log(f"[{job['id']}] Đã nhận diện {len(visual_catalogue)} cảnh hình ảnh để dựng theo TXT", job)
    else:
        E.log(f"[{job['id']}] Không tạo được danh mục hình ảnh; dùng transcript có timestamp", job)
    save(job)
    text_name = base + "_tom_tat.txt"
    script_path = os.path.join(outdir, text_name)
    story_text = write_story_summary(
        chapters, script_path, target, use_hook_skill=use_hook_skill,
        visual_catalogue=visual_catalogue)
    E.log(f"[{job['id']}] Đối chiếu bản TXT với cảnh nguồn...", job); save(job)
    ranges = summary_scene_scaffold(duration, target)
    ranges, narrations = align_story_to_shots(
        story_text, translated, duration, target, ranges, use_hook_skill=use_hook_skill,
        visual_catalogue=visual_catalogue)
    scene_map_path = os.path.join(workdir, base + "_scene_map.json")
    with open(scene_map_path, "w", encoding="utf-8") as handle:
        json.dump({"story_text": story_text,
                   "scenes": [{"id": index, "start": value[0], "end": value[1],
                               "narration": narrations[index]}
                              for index, value in enumerate(ranges)]},
                  handle, ensure_ascii=False, indent=2)
    video_name = base + "_tom_tat.mp4"
    video_path = os.path.join(outdir, video_name)
    provider = str((job.get("opts") or {}).get("tts_provider") or "vivibe")
    voice_id, voice_label = voice_for_provider(voice_choice, provider, 0)
    if provider.lower() == "genmax":
        supports_tags = not str(voice_id).isdigit()
        E.log(f"[{job['id']}] Tạo nhịp nghỉ và cảm xúc giọng đọc theo từng shot...", job); save(job)
        narrations = apply_scene_emotions(
            narrations, provider, enabled=True, supports_tags=supports_tags)
    E.log(f"[{job['id']}] Tạo giọng {voice_label} khớp theo {len(ranges)} shot...", job); save(job)
    rendered, render_error = create_synced_summary_video(
        source, ranges, narrations, video_path, workdir, voice_id, job, save, subtitle_style)
    if not rendered:
        if render_error == "vivibe_credit":
            raise RuntimeError("Vivibe hết credit — hãy nạp thêm hoặc chọn GenMax")
        raise RuntimeError("Không tạo được giọng thuyết minh: " + str(render_error or "lỗi TTS"))
    add_movie_header(video_path, movie_title, workdir)
    thumb = base + "_tom_tat.jpg"
    E.ff(["ffmpeg", "-y", "-i", video_path, "-vf", "scale=360:-2", "-frames:v", "1", os.path.join(outdir, thumb)])
    video_label = "Video Reup · Tóm tắt + Hook 3 giây" if use_hook_skill else "Video tóm tắt phim"
    caption = reup_caption(story_text, title) if use_hook_skill else ""
    return [
        {"type": "text", "file": text_name, "san_pham": title, "label": "Bản tóm tắt nội dung"},
        {"type": "video", "file": video_name, "thumb": thumb, "san_pham": title,
         "dur": round(E.probe(video_path), 1), "kich_ban": video_label,
         "hook": "Hook TikTok 3 giây" if use_hook_skill else "", "voice": voice_label,
         "fb_title": caption},
    ]


def prepare_movie_studio(job, save):
    """Prepare editable transcript/translation drafts without rendering outputs."""
    opts = job.get("opts") or {}
    source_language = opts.get("source_language") or None
    target_language = opts.get("target_language") or "vi"
    video_speed = float(opts.get("video_speed") or 1.0)
    movie_flip = opts.get("movie_flip") or "none"
    drafts = []
    for index, item in enumerate(job.get("inputs") or []):
        workdir = os.path.join(E.WORK_DIR, job["id"], f"translate_{index}")
        os.makedirs(workdir, exist_ok=True)
        E.log(f"[{job['id']}] Chuẩn bị bản nháp {index + 1}/{len(job['inputs'])}...", job); save(job)
        source, title = _prepare_source(item, workdir)
        if not source or not os.path.exists(source):
            E.log(f"[{job['id']}] Không tải/đọc được nguồn {index + 1}", job); save(job)
            continue
        try:
            source_is_local = (os.path.commonpath([os.path.realpath(source), os.path.realpath(workdir)]) ==
                               os.path.realpath(workdir))
        except ValueError:
            source_is_local = False
        if not source_is_local:
            extension = os.path.splitext(source)[1] or ".mp4"
            local_source = os.path.join(workdir, "studio_source" + extension)
            shutil.copy2(source, local_source)
            source = local_source
        if abs(video_speed - 1.0) > 0.001 or movie_flip != "none":
            source = prepare_translation_source(
                source, os.path.join(workdir, "translation_source.mp4"), video_speed, movie_flip)
        duration = float(E.probe(source) or 0)
        E.log(f"[{job['id']}] Đang nhận diện lời thoại cho bản nháp...", job); save(job)
        _, segments = E.transcribe(source, workdir, want_segments=True, language=source_language)
        segments = [s for s in segments if str(s.get("text") or "").strip()]
        translated = translated_or_original_segments(segments, target_language) if segments else []
        clean_segments = []
        for segment_index, segment in enumerate(translated):
            clean_segments.append({
                "id": segment_index,
                "start": round(float(segment.get("start") or 0), 3),
                "end": round(float(segment.get("end") or 0), 3),
                "text": str(segment.get("text") or "").strip(),
                "translated": str(segment.get("translated") or segment.get("text") or "").strip(),
                "speaker": str(segment.get("speaker") or "Nhân vật 1").strip()[:80],
                "voice": str(segment.get("voice") or "").strip(),
            })
        drafts.append({"index": index, "title": title, "source_path": source,
                       "duration": round(duration, 3), "segments": clean_segments})
        job["drafts"] = drafts
        save(job)
    if not drafts:
        raise RuntimeError("Không chuẩn bị được nguồn phim nào để biên tập")
    job["draft_version"] = 1
    job["status"] = "awaiting_review"
    job["phase"] = "review"
    E.log(f"[{job['id']}] Bản nháp đã sẵn sàng để kiểm tra và chỉnh sửa", job)
    save(job)


def run_movie_studio(job, save):
    """Run one phase of the review-first movie workflow."""
    phase = str(job.get("phase") or (job.get("opts") or {}).get("phase") or "prepare")
    if phase == "prepare":
        prepare_movie_studio(job, save)
        return
    if phase != "render":
        raise ValueError("Giai đoạn Studio phim không hợp lệ")
    run_translate(job, save, reviewed_drafts=job.get("drafts") or [])


def run_translate(job, save, reviewed_drafts=None):
    opts = job.get("opts") or {}
    source_language = opts.get("source_language") or None
    target_language = opts.get("target_language") or "vi"
    target_name = "Nguyên bản" if target_language == "none" else LANGUAGES.get(target_language, target_language)
    output_mode = opts.get("output_mode") or "both"
    make_summary = bool(opts.get("make_summary"))
    summary_minutes = opts.get("summary_minutes") or "auto"
    original_subtitles = opts.get("original_subtitles") or "smart_bottom"
    subtitle_region = opts.get("subtitle_region")
    video_speed = float(opts.get("video_speed") or 1.0)
    movie_flip = opts.get("movie_flip") or "none"
    movie_title = " ".join(str(opts.get("movie_title") or "").split())[:160]
    subtitle_style = {
        "font_size": opts.get("subtitle_font_size", 18),
        "font_color": opts.get("subtitle_font_color", "#FFFFFF"),
        "outline_color": opts.get("subtitle_outline_color", "#101010"),
    }
    use_hook_skill = bool(opts.get("reup_hook_skill"))
    outdir = os.path.join(E.OUT_DIR, job["id"])
    os.makedirs(outdir, exist_ok=True)
    job["outputs"] = []
    made = 0

    reviewed_by_index = {int(item.get("index", i)): item
                         for i, item in enumerate(reviewed_drafts or [])}
    for index, item in enumerate(job.get("inputs") or []):
        workdir = os.path.join(E.WORK_DIR, job["id"], f"translate_{index}")
        os.makedirs(workdir, exist_ok=True)
        reviewed = reviewed_by_index.get(index)
        task_label = "Reup video" if use_hook_skill else ("Render bản đã duyệt" if reviewed else "Dịch video")
        E.log(f"[{job['id']}] {task_label} {index + 1}/{len(job['inputs'])}: chuẩn bị nguồn...", job); save(job)
        if reviewed:
            source = str(reviewed.get("source_path") or "")
            title = str(reviewed.get("title") or item.get("name") or f"Video {index + 1}")
        else:
            source, title = _prepare_source(item, workdir)
        if not source or not os.path.exists(source):
            E.log(f"[{job['id']}] Không tải/đọc được nguồn {index + 1}", job); save(job); continue

        if output_mode == "original":
            video_name = f"phim_goc_{index + 1}.mp4"
            video_path = os.path.join(outdir, video_name)
            shutil.copy2(source, video_path)
            add_movie_header(video_path, movie_title, workdir)
            thumb = f"phim_goc_{index + 1}.jpg"
            E.ff(["ffmpeg", "-y", "-i", video_path, "-vf", "scale=360:-2", "-frames:v", "1",
                  os.path.join(outdir, thumb)])
            job["outputs"].append({"type": "video", "file": video_name, "thumb": thumb,
                                   "san_pham": title, "dur": round(E.probe(video_path), 1),
                                   "kich_ban": "Giữ nguyên video gốc", "speed": "1x",
                                   "flip": E.XFORM_LABEL.get("none", "none")})
            E.log(f"[{job['id']}] Giữ nguyên video gốc, không dịch và giữ sub gốc", job); save(job)
            made += 1
            continue

        if not reviewed and (abs(video_speed - 1.0) > 0.001 or movie_flip != "none"):
            actions = []
            if abs(video_speed - 1.0) > 0.001:
                actions.append(f"tốc độ {video_speed:g}x")
            if movie_flip == "hflip":
                actions.append("lật gương")
            E.log(f"[{job['id']}] Đang áp dụng {' và '.join(actions)}...", job); save(job)
            source = prepare_translation_source(
                source, os.path.join(workdir, "translation_source.mp4"), video_speed, movie_flip)

        if reviewed:
            translated = [dict(segment) for segment in (reviewed.get("segments") or [])]
            segments = translated
            E.log(f"[{job['id']}] Dùng {len(translated)} đoạn đã được duyệt", job); save(job)
        else:
            E.log(f"[{job['id']}] Đang nhận diện lời thoại...", job); save(job)
            _, segments = E.transcribe(source, workdir, want_segments=True, language=source_language)
            segments = [s for s in segments if str(s.get("text") or "").strip()]
        if not segments:
            base = f"video_reup_{index + 1}" if use_hook_skill else f"phim_dich_{index + 1}"
            video_name = base + "_khong_loi_thoai.mp4"
            video_path = os.path.join(outdir, video_name)
            shutil.copy2(source, video_path)
            add_movie_header(video_path, movie_title, workdir)
            thumb = base + "_khong_loi_thoai.jpg"
            E.ff(["ffmpeg", "-y", "-i", video_path, "-vf", "scale=360:-2", "-frames:v", "1",
                  os.path.join(outdir, thumb)])
            E.log(f"[{job['id']}] Nguồn {index + 1} không có lời thoại; bỏ qua phụ đề và lồng tiếng", job); save(job)
            job["outputs"].append({"type": "video", "file": video_name, "thumb": thumb,
                                   "san_pham": title, "dur": round(E.probe(video_path), 1),
                                   "kich_ban": "Video không có lời thoại", "speed": f"{video_speed:g}x",
                                   "flip": E.XFORM_LABEL.get(movie_flip, movie_flip)})
            made += 1
            continue

        if not reviewed:
            if target_language == "none":
                E.log(f"[{job['id']}] Không dịch: giữ nguyên {len(segments)} đoạn lời gốc...", job); save(job)
            else:
                E.log(f"[{job['id']}] Đang dịch {len(segments)} đoạn sang {LANGUAGES.get(target_language, target_language)}...", job); save(job)
            translated = translated_or_original_segments(segments, target_language)
        base = f"video_reup_{index + 1}" if use_hook_skill else f"phim_dich_{index + 1}"
        subtitle_segments = retime_translation_subtitles(translated)
        srt_name = base + ".srt"
        srt_path = write_srt(subtitle_segments, os.path.join(outdir, srt_name))
        if output_mode in ("srt", "both", "dub_srt", "all"):
            job["outputs"].append({"type": "subtitle", "file": srt_name, "san_pham": title,
                                   "label": f"Phụ đề {target_name}", "segments": len(translated)})
            made += 1

        if output_mode in ("burn", "both", "all"):
            video_name = base + ".mp4"
            video_path = os.path.join(outdir, video_name)
            action = "xóa sub gốc và gắn sub dịch" if original_subtitles != "keep" else "gắn phụ đề dịch"
            E.log(f"[{job['id']}] Đang {action}...", job); save(job)
            if burn_subtitles(source, srt_path, video_path, original_subtitles, workdir,
                              subtitle_style, subtitle_region):
                add_movie_header(video_path, movie_title, workdir)
                thumb = base + ".jpg"
                E.ff(["ffmpeg", "-y", "-i", video_path, "-vf", "scale=360:-2", "-frames:v", "1", os.path.join(outdir, thumb)])
                job["outputs"].append({"type": "video", "file": video_name, "thumb": thumb,
                                       "san_pham": title, "dur": round(E.probe(video_path), 1),
                                       "kich_ban": f"Phụ đề {target_name}", "subtitle": srt_name,
                                       "speed": f"{video_speed:g}x", "flip": E.XFORM_LABEL.get(movie_flip, movie_flip)})
                made += 1
            else:
                E.log(f"[{job['id']}] FFmpeg không gắn được phụ đề; file SRT vẫn được giữ", job); save(job)

        if output_mode in ("dub", "dub_srt", "all"):
            visual_source = source
            if original_subtitles in ("smart_bottom", "smart_top", "smart_custom"):
                position = "top" if original_subtitles.endswith("top") else "bottom"
                region = subtitle_region if original_subtitles == "smart_custom" else None
                clean_path = os.path.join(workdir, "dub_source_without_subtitles.mp4")
                E.log(f"[{job['id']}] Đang xóa phụ đề gốc trước khi lồng tiếng...", job); save(job)
                if clean_original_subtitles(source, clean_path, position, region):
                    visual_source = clean_path
                else:
                    fallback_path = os.path.join(workdir, "dub_source_blurred.mp4")
                    if blur_original_subtitles(source, fallback_path, position, region):
                        visual_source = fallback_path
            dub_name = base + "_long_tieng_vi.mp4"
            dub_path = os.path.join(outdir, dub_name)
            provider = str(opts.get("tts_provider") or "vivibe")
            voice_id, voice_label = voice_for_provider(opts.get("voice", "auto"), provider, index)
            E.log(f"[{job['id']}] Đang lồng tiếng Việt bằng giọng {voice_label}...", job); save(job)
            rendered = create_dubbed_video(
                visual_source, translated, dub_path, workdir, provider, voice_id, job, save)
            if not rendered:
                raise RuntimeError("Không ghép được giọng Việt vào phim")
            add_movie_header(dub_path, movie_title, workdir)
            thumb = base + "_long_tieng_vi.jpg"
            E.ff(["ffmpeg", "-y", "-i", dub_path, "-vf", "scale=360:-2", "-frames:v", "1",
                  os.path.join(outdir, thumb)])
            job["outputs"].append({"type": "video", "file": dub_name, "thumb": thumb,
                                   "san_pham": title, "dur": round(E.probe(dub_path), 1),
                                   "kich_ban": "Phim lồng tiếng Việt", "voice": voice_label,
                                   "subtitle": srt_name, "speed": f"{video_speed:g}x",
                                   "flip": E.XFORM_LABEL.get(movie_flip, movie_flip)})
            made += 1

        if make_summary:
            summary_source = source
            if original_subtitles in ("smart_bottom", "smart_top", "smart_custom"):
                position = "top" if original_subtitles.endswith("top") else "bottom"
                region = subtitle_region if original_subtitles == "smart_custom" else None
                clean_path = os.path.join(workdir, "summary_source_without_subtitles.mp4")
                E.log(f"[{job['id']}] Đang xóa phụ đề gốc bằng xử lý local...", job); save(job)
                if clean_original_subtitles(source, clean_path, position, region):
                    summary_source = clean_path
                else:
                    fallback_path = os.path.join(workdir, "summary_source_blurred.mp4")
                    E.log(f"[{job['id']}] Xóa thông minh lỗi, chuyển sang làm mờ vùng sub...", job); save(job)
                    if blur_original_subtitles(source, fallback_path, position, region):
                        summary_source = fallback_path
            summary_outputs = make_summary_outputs(summary_source, translated, title, outdir, workdir, base,
                                                   summary_minutes, opts.get("voice", "auto"), job, save,
                                                   use_hook_skill=use_hook_skill, movie_title=movie_title,
                                                   subtitle_style=subtitle_style)
            job["outputs"].extend(summary_outputs)
            made += len(summary_outputs)

    if not made:
        job["status"] = "error"
        job["error"] = "Không tạo được kết quả. Hãy kiểm tra nguồn, cấu hình giọng đọc và thử lại."
    else:
        job["status"] = "done"
        action = "Reup video" if use_hook_skill else "dịch phim"
        E.log(f"[{job['id']}] Hoàn tất {action}: {made} tệp kết quả", job)
    save(job)


def run_safe(job, save):
    try:
        run_translate(job, save)
    except Exception as exc:
        job["status"] = "error"
        job["error"] = f"{type(exc).__name__}: {exc}"
        job.setdefault("log", []).append(traceback.format_exc()[-1200:])
        save(job)
