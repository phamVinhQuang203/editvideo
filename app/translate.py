# -*- coding: utf-8 -*-
"""Translate spoken video content and optionally burn Vietnamese subtitles."""
import json
import os
import shutil
import sys
import traceback
import wave
from pathlib import Path

import engine as E


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
    target_name = LANGUAGES.get(target, target)
    translated = []
    for start in range(0, len(segments), batch_size):
        batch = segments[start:start + batch_size]
        payload = [{"id": start + i, "text": item.get("text", "")} for i, item in enumerate(batch)]
        prompt = f"""Dịch phụ đề phim sang {target_name} tự nhiên, đúng ngữ cảnh và ngắn gọn để dễ đọc.
Giữ nguyên tên riêng, con số và sắc thái lời thoại. Không giải thích, không thêm nội dung.
Trả về DUY NHẤT JSON dạng {{"items":[{{"id":0,"text":"..."}}]}} với đủ mọi id.
Dữ liệu: {json.dumps(payload, ensure_ascii=False)}"""
        result = E.codex_json(prompt, effort="medium", timeout=600) or {}
        values = result.get("items") if isinstance(result, dict) else result
        by_id = {}
        for item in values or []:
            if not isinstance(item, dict):
                continue
            try:
                item_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            text = str(item.get("text") or "").strip()
            if text:
                by_id[item_id] = text
        missing = [item["id"] for item in payload if item["id"] not in by_id]
        if missing:
            raise RuntimeError(f"AI không trả đủ {len(missing)} đoạn dịch; vui lòng chạy lại")
        for i, item in enumerate(batch):
            copy = dict(item)
            copy["translated"] = by_id[start + i]
            translated.append(copy)
    return translated


def _subtitle_filter(path):
    escaped = os.path.abspath(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    style = "FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=38"
    return f"subtitles='{escaped}':force_style='{style}'"


def _replace_subtitle_filter(subtitle, position):
    """Che dải sub cứng rồi đặt phụ đề dịch lên trên dải đã làm mờ."""
    y_crop = "0" if position == "top" else "ih*0.76"
    y_overlay = "0" if position == "top" else "H-h"
    return (f"[0:v]split=2[base][subarea];"
            f"[subarea]crop=iw:ih*0.24:0:{y_crop},boxblur=20:3[blurred];"
            f"[base][blurred]overlay=0:{y_overlay}[clean];"
            f"[clean]{_subtitle_filter(subtitle)}[v]")


def clean_original_subtitles(source, destination, position):
    helper = os.path.join(E.VENDOR, "remove_subtitles_local.py")
    result = E.ff([sys.executable, helper, source, destination, "--position", position])
    return (result.returncode == 0 and os.path.exists(destination)
            and os.path.getsize(destination) > 10_000)


def blur_original_subtitles(source, destination, position):
    y_crop = "0" if position == "top" else "ih*0.76"
    y_overlay = "0" if position == "top" else "H-h"
    video_filter = (f"[0:v]split=2[base][subarea];"
                    f"[subarea]crop=iw:ih*0.24:0:{y_crop},boxblur=20:3[blurred];"
                    f"[base][blurred]overlay=0:{y_overlay}[v]")
    result = E.ff(["ffmpeg", "-y", "-i", source, "-filter_complex", video_filter,
                   "-map", "[v]", "-map", "0:a?", *E.video_encode_args(),
                   "-c:a", "copy", "-movflags", "+faststart", destination])
    return (result.returncode == 0 and os.path.exists(destination)
            and os.path.getsize(destination) > 10_000)


def _atempo_chain(factor):
    parts = []
    factor = max(0.05, float(factor))
    while factor > 2.0:
        parts.append("atempo=2.0"); factor /= 2.0
    while factor < 0.5:
        parts.append("atempo=0.5"); factor /= 0.5
    parts.append(f"atempo={factor:.5f}")
    return ",".join(parts)


def _synthesize_dub_line(text, provider, voice_id, destination):
    renderer = E._load_ra()
    if provider == "genmax":
        renderer.run_genmax_tts_with_retry(text, voice_id, Path(destination), attempts=3)
    else:
        renderer.run_vivibe_tts_with_retry(text, voice_id, Path(destination), 1.0, attempts=3)
    return E.probe(destination)


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


def create_dub_voice_timeline(segments, duration, workdir, provider, voice_id, job, save):
    """Tạo một WAV tiếng Việt, mỗi câu nằm đúng khe thời gian của lời gốc."""
    fitted = []
    for index, segment in enumerate(sorted(segments, key=lambda item: float(item.get("start", 0)))):
        text = str(segment.get("translated") or "").strip()
        start = max(0.0, float(segment.get("start", 0)))
        end = min(duration, max(start + 0.35, float(segment.get("end", start + 0.35))))
        if not text or end <= start:
            continue
        raw = os.path.join(workdir, f"dub_raw_{index:04d}.audio")
        line = os.path.join(workdir, f"dub_line_{index:04d}.wav")
        E.log(f"[{job['id']}] Tạo câu lồng tiếng {index + 1}/{len(segments)}...", job); save(job)
        raw_duration = _synthesize_dub_line(text, provider, voice_id, raw)
        slot = max(0.35, end - start)
        tempo = max(1.0, raw_duration / slot)
        audio_filter = f"{_atempo_chain(tempo)},apad,atrim=0:{slot:.4f}"
        result = E.ff(["ffmpeg", "-y", "-i", raw, "-vn", "-af", audio_filter,
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
            with wave.open(line, "rb") as source:
                frames = source.readframes(min(source.getnframes(), int(round((end - start) * rate))))
            output.writeframesraw(frames)
            current_frame += len(frames) // (channels * sample_width)
        final_frame = int(round(duration * rate))
        while current_frame < final_frame:
            chunk = min(final_frame - current_frame, rate * 10)
            output.writeframesraw(b"\0" * chunk * channels * sample_width)
            current_frame += chunk
    return timeline


def create_dubbed_video(source, segments, destination, workdir, provider, voice_id, job, save):
    E.log(f"[{job['id']}] Đang tách và loại bỏ giọng nói gốc bằng Demucs (CPU)...", job); save(job)
    demucs_dir = os.path.join(workdir, "dub_demucs")
    os.makedirs(demucs_dir, exist_ok=True)
    audio = E.autovoice_audio("mixed_voice_music", source, demucs_dir)
    bed = audio.get("bed")
    if not bed or not os.path.exists(bed):
        raise RuntimeError("Demucs không tách được giọng gốc; đã dừng để không trộn chồng hai giọng")
    timeline = create_dub_voice_timeline(
        segments, E.probe(source), workdir, provider, voice_id, job, save)
    result = E.ff(["ffmpeg", "-y", "-i", source, "-i", bed, "-i", timeline,
                   "-filter_complex", "[1:a]volume=0.92[bed];[2:a]volume=1.35[voice];"
                   "[bed][voice]amix=inputs=2:duration=first:dropout_transition=0,"
                   "alimiter=limit=0.95[a]", "-map", "0:v:0", "-map", "[a]",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                   "-movflags", "+faststart", destination])
    return (result.returncode == 0 and os.path.exists(destination)
            and os.path.getsize(destination) > 10_000)


def burn_subtitles(source, subtitle, destination, original_subtitles="smart_bottom", workdir=None):
    smart = original_subtitles in ("smart_bottom", "smart_top")
    position = "top" if original_subtitles.endswith("top") else "bottom"
    render_source = source
    if smart:
        clean_path = os.path.join(workdir or os.path.dirname(destination), "source_without_subtitles.mp4")
        if clean_original_subtitles(source, clean_path, position):
            render_source = clean_path
            original_subtitles = "keep"
        else:
            # Neu OpenCV/codec loi, job van hoan thanh bang cach che mo cu.
            original_subtitles = position
    remove_original = original_subtitles in ("bottom", "top")
    video_filter = _replace_subtitle_filter(subtitle, original_subtitles) if remove_original else _subtitle_filter(subtitle)
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
    return min(duration, max(180.0, min(1200.0, duration / 6.0)))


def _fallback_range(start, end, target):
    length = min(max(2.0, target), max(2.0, end - start))
    middle = (start + end) / 2.0
    return [max(start, middle - length / 2.0), min(end, middle + length / 2.0)]


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


def write_story_summary(chapter_summaries, path, target_seconds=600):
    source = "\n".join(f"Chương {i + 1}: {text}" for i, text in enumerate(chapter_summaries))
    target_words = max(500, int(target_seconds * 3.2))
    prompt = f"""Từ các tóm tắt chương dưới đây, viết lời THUYẾT MINH tóm tắt phim bằng tiếng Việt, khoảng {target_words} từ.
Kể mạch lạc theo diễn biến từ mở đầu, xung đột, cao trào đến kết thúc. Câu văn tự nhiên để đọc thành tiếng,
có câu chuyển cảnh giữa các sự kiện, không dùng tiêu đề hay gạch đầu dòng. Không thêm chi tiết không có trong dữ liệu.
Chỉ in lời thuyết minh thuần, không lời dẫn của AI.\n{source}"""
    text = E.codex(prompt, effort="medium", timeout=600).strip()
    if not text:
        text = source
    with open(path, "w", encoding="utf-8-sig") as handle:
        handle.write(text)
    return text


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


def create_synced_summary_video(source, ranges, narrations, destination, workdir,
                                voice_id, job, save):
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
        rendered_clips.append(voiced)

    concat_file = os.path.join(workdir, "summary_sync_concat.txt")
    with open(concat_file, "w", encoding="utf-8") as handle:
        for clip in rendered_clips:
            handle.write("file '" + clip.replace("'", "'\\''") + "'\n")
    result = E.ff(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file,
                   "-c", "copy", "-movflags", "+faststart", destination])
    return (result.returncode == 0 and os.path.exists(destination)
            and os.path.getsize(destination) > 10_000), "Không nối được các shot đã thuyết minh"


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
                         voice_choice, job, save):
    duration = E.probe(source)
    target = summary_target_seconds(duration, requested_minutes)
    E.log(f"[{job['id']}] Chọn tình tiết cho bản tóm tắt khoảng {target/60:.1f} phút...", job); save(job)
    ranges, chapters = select_summary_ranges(translated, duration, target)
    if not ranges:
        raise RuntimeError("Không chọn được các đoạn quan trọng để tóm tắt")
    narrations = build_summary_narrations(translated, ranges, chapters)
    text_name = base + "_tom_tat.txt"
    script_path = os.path.join(outdir, text_name)
    with open(script_path, "w", encoding="utf-8-sig") as handle:
        handle.write("\n\n".join(narrations))
    video_name = base + "_tom_tat.mp4"
    video_path = os.path.join(outdir, video_name)
    provider = str((job.get("opts") or {}).get("tts_provider") or "vivibe")
    voice_id, voice_label = voice_for_provider(voice_choice, provider, 0)
    E.log(f"[{job['id']}] Tạo giọng {voice_label} khớp theo {len(ranges)} shot...", job); save(job)
    rendered, render_error = create_synced_summary_video(
        source, ranges, narrations, video_path, workdir, voice_id, job, save)
    if not rendered:
        if render_error == "vivibe_credit":
            raise RuntimeError("Vivibe hết credit — hãy nạp thêm hoặc chọn GenMax")
        raise RuntimeError("Không tạo được giọng thuyết minh: " + str(render_error or "lỗi TTS"))
    thumb = base + "_tom_tat.jpg"
    E.ff(["ffmpeg", "-y", "-i", video_path, "-vf", "scale=360:-2", "-frames:v", "1", os.path.join(outdir, thumb)])
    return [
        {"type": "text", "file": text_name, "san_pham": title, "label": "Bản tóm tắt nội dung"},
        {"type": "video", "file": video_name, "thumb": thumb, "san_pham": title,
         "dur": round(E.probe(video_path), 1), "kich_ban": "Video tóm tắt phim", "voice": voice_label},
    ]


def run_translate(job, save):
    opts = job.get("opts") or {}
    source_language = opts.get("source_language") or None
    target_language = opts.get("target_language") or "vi"
    target_name = LANGUAGES.get(target_language, target_language)
    output_mode = opts.get("output_mode") or "both"
    make_summary = bool(opts.get("make_summary"))
    summary_minutes = opts.get("summary_minutes") or "auto"
    original_subtitles = opts.get("original_subtitles") or "smart_bottom"
    outdir = os.path.join(E.OUT_DIR, job["id"])
    os.makedirs(outdir, exist_ok=True)
    job["outputs"] = []
    made = 0

    for index, item in enumerate(job.get("inputs") or []):
        workdir = os.path.join(E.WORK_DIR, job["id"], f"translate_{index}")
        os.makedirs(workdir, exist_ok=True)
        E.log(f"[{job['id']}] Dịch video {index + 1}/{len(job['inputs'])}: chuẩn bị nguồn...", job); save(job)
        source, title = _prepare_source(item, workdir)
        if not source or not os.path.exists(source):
            E.log(f"[{job['id']}] Không tải/đọc được nguồn {index + 1}", job); save(job); continue

        E.log(f"[{job['id']}] Đang nhận diện lời thoại...", job); save(job)
        _, segments = E.transcribe(source, workdir, want_segments=True, language=source_language)
        segments = [s for s in segments if str(s.get("text") or "").strip()]
        if not segments:
            E.log(f"[{job['id']}] Nguồn {index + 1} không có lời thoại nhận diện được", job); save(job); continue

        E.log(f"[{job['id']}] Đang dịch {len(segments)} đoạn sang {LANGUAGES.get(target_language, target_language)}...", job); save(job)
        translated = translate_segments(segments, target_language)
        base = f"phim_dich_{index + 1}"
        srt_name = base + ".srt"
        srt_path = write_srt(translated, os.path.join(outdir, srt_name))
        if output_mode in ("srt", "both", "all", "dub_subtitle"):
            job["outputs"].append({"type": "subtitle", "file": srt_name, "san_pham": title,
                                   "label": f"Phụ đề {target_name}", "segments": len(translated)})
            made += 1

        if output_mode in ("burn", "both", "all"):
            video_name = base + ".mp4"
            video_path = os.path.join(outdir, video_name)
            action = "xóa sub gốc và gắn sub dịch" if original_subtitles != "keep" else "gắn phụ đề dịch"
            E.log(f"[{job['id']}] Đang {action}...", job); save(job)
            if burn_subtitles(source, srt_path, video_path, original_subtitles, workdir):
                thumb = base + ".jpg"
                E.ff(["ffmpeg", "-y", "-i", video_path, "-vf", "scale=360:-2", "-frames:v", "1", os.path.join(outdir, thumb)])
                job["outputs"].append({"type": "video", "file": video_name, "thumb": thumb,
                                       "san_pham": title, "dur": round(E.probe(video_path), 1),
                                       "kich_ban": f"Phụ đề {target_name}", "subtitle": srt_name})
                made += 1
            else:
                E.log(f"[{job['id']}] FFmpeg không gắn được phụ đề; file SRT vẫn được giữ", job); save(job)

        if output_mode in ("dub", "dub_subtitle"):
            visual_source = source
            if original_subtitles in ("smart_bottom", "smart_top"):
                position = "top" if original_subtitles.endswith("top") else "bottom"
                clean_path = os.path.join(workdir, "dub_source_without_subtitles.mp4")
                E.log(f"[{job['id']}] Đang xóa phụ đề gốc trước khi lồng tiếng...", job); save(job)
                if clean_original_subtitles(source, clean_path, position):
                    visual_source = clean_path
                else:
                    fallback_path = os.path.join(workdir, "dub_source_blurred.mp4")
                    if blur_original_subtitles(source, fallback_path, position):
                        visual_source = fallback_path
            provider = str(opts.get("tts_provider") or "vivibe")
            voice_id, voice_label = voice_for_provider(opts.get("voice", "auto"), provider, index)
            raw_dub = os.path.join(workdir, "dubbed_without_subtitle.mp4")
            video_name = base + "_long_tieng.mp4"
            video_path = os.path.join(outdir, video_name)
            try:
                dubbed = create_dubbed_video(
                    visual_source, translated, raw_dub, workdir,
                    provider, voice_id, job, save)
                if dubbed and output_mode == "dub_subtitle":
                    dubbed = burn_subtitles(raw_dub, srt_path, video_path, "keep", workdir)
                elif dubbed:
                    shutil.copy2(raw_dub, video_path)
                if dubbed and os.path.exists(video_path):
                    thumb = base + "_long_tieng.jpg"
                    E.ff(["ffmpeg", "-y", "-i", video_path, "-vf", "scale=360:-2",
                          "-frames:v", "1", os.path.join(outdir, thumb)])
                    job["outputs"].append({"type": "video", "file": video_name, "thumb": thumb,
                                           "san_pham": title, "dur": round(E.probe(video_path), 1),
                                           "kich_ban": f"Lồng tiếng {target_name}", "voice": voice_label,
                                           "subtitle": srt_name if output_mode == "dub_subtitle" else ""})
                    made += 1
                else:
                    E.log(f"[{job['id']}] Không tạo được video lồng tiếng", job); save(job)
            except Exception as exc:
                E.log(f"[{job['id']}] Lồng tiếng lỗi: {str(exc)[:220]}", job); save(job)

        if make_summary:
            summary_source = source
            if original_subtitles in ("smart_bottom", "smart_top"):
                position = "top" if original_subtitles.endswith("top") else "bottom"
                clean_path = os.path.join(workdir, "summary_source_without_subtitles.mp4")
                E.log(f"[{job['id']}] Đang xóa phụ đề gốc bằng xử lý local...", job); save(job)
                if clean_original_subtitles(source, clean_path, position):
                    summary_source = clean_path
                else:
                    fallback_path = os.path.join(workdir, "summary_source_blurred.mp4")
                    E.log(f"[{job['id']}] Xóa thông minh lỗi, chuyển sang làm mờ vùng sub...", job); save(job)
                    if blur_original_subtitles(source, fallback_path, position):
                        summary_source = fallback_path
            summary_outputs = make_summary_outputs(summary_source, translated, title, outdir, workdir, base,
                                                   summary_minutes, opts.get("voice", "auto"), job, save)
            job["outputs"].extend(summary_outputs)
            made += len(summary_outputs)

    if not made:
        job["status"] = "error"
        job["error"] = "Không tạo được kết quả. Hãy kiểm tra nguồn, cấu hình giọng đọc và thử lại."
    else:
        job["status"] = "done"
        E.log(f"[{job['id']}] Hoàn tất dịch phim: {made} tệp kết quả", job)
    save(job)


def run_safe(job, save):
    try:
        run_translate(job, save)
    except Exception as exc:
        job["status"] = "error"
        job["error"] = f"{type(exc).__name__}: {exc}"
        job.setdefault("log", []).append(traceback.format_exc()[-1200:])
        save(job)
