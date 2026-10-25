# -*- coding: utf-8 -*-
"""Replace a video's original audio with audio downloaded from another URL."""
import os
import traceback

import engine as E


def prepare_video(source, destination, speed=1.0, flip="none"):
    speed = float(speed)
    filters = []
    if flip == "hflip":
        filters.append("hflip")
    if abs(speed - 1.0) > 0.001:
        filters.append(f"setpts=PTS/{speed:.6f}")
    if not filters:
        return source
    command = ["ffmpeg", "-y", "-i", source, "-vf", ",".join(filters), "-an",
               *E.video_encode_args(), "-movflags", "+faststart", destination]
    result = E.ff(command)
    if result.returncode != 0:
        command = ["ffmpeg", "-y", "-i", source, "-vf", ",".join(filters), "-an",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                   "-movflags", "+faststart", destination]
        result = E.ff(command)
    if result.returncode != 0 or not os.path.exists(destination) or os.path.getsize(destination) <= 10_000:
        raise RuntimeError("Không áp dụng được tốc độ/lật hình")
    return destination


def run_soundtrack(job, save):
    opts = job.get("opts") or {}
    video_url = str(opts.get("video_url") or "").strip()
    audio_url = str(opts.get("audio_url") or "").strip()
    video_speed = float(opts.get("video_speed") or 1.0)
    movie_flip = str(opts.get("movie_flip") or "none")
    outdir = os.path.join(E.OUT_DIR, job["id"])
    workdir = os.path.join(E.WORK_DIR, job["id"], "soundtrack")
    video_dir = os.path.join(workdir, "video")
    audio_dir = os.path.join(workdir, "audio")
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(video_dir, exist_ok=True)
    os.makedirs(audio_dir, exist_ok=True)
    job["outputs"] = []

    E.log(f"[{job['id']}] Đang tải video nguồn...", job); save(job)
    video_path, video_meta = E.download(video_url, video_dir)
    if not video_path or not os.path.exists(video_path):
        raise RuntimeError("Không tải được link video")
    if abs(video_speed - 1.0) > 0.001 or movie_flip != "none":
        E.log(f"[{job['id']}] Đang áp dụng tốc độ {video_speed:g}x và lật hình...", job); save(job)
        video_path = prepare_video(
            video_path, os.path.join(workdir, "video_da_xu_ly.mp4"), video_speed, movie_flip)
    E.log(f"[{job['id']}] Đang tải nguồn âm thanh...", job); save(job)
    audio_path, audio_meta = E.download(audio_url, audio_dir)
    if not audio_path or not os.path.exists(audio_path):
        raise RuntimeError("Không tải được link âm thanh")
    if not E._load_ra().has_audio(audio_path):
        raise RuntimeError("Link âm thanh không có track audio")

    output_name = "video_ghep_am_thanh.mp4"
    output_path = os.path.join(outdir, output_name)
    E.log(f"[{job['id']}] Đang thay âm thanh và đồng bộ thời lượng...", job); save(job)
    command = ["ffmpeg", "-y", "-i", video_path, "-i", audio_path,
               "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
               "-b:a", "192k", "-shortest", "-movflags", "+faststart", output_path]
    result = E.ff(command)
    if result.returncode != 0:
        command[command.index("-c:v") + 1:command.index("-c:v") + 2] = [
            "libx264", "-preset", "veryfast", "-crf", "20"]
        result = E.ff(command)
    if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) <= 10_000:
        raise RuntimeError("FFmpeg không ghép được âm thanh vào video")

    thumb = "video_ghep_am_thanh.jpg"
    E.ff(["ffmpeg", "-y", "-i", output_path, "-vf", "scale=360:-2", "-frames:v", "1",
          os.path.join(outdir, thumb)])
    title = video_meta.get("title") or "Video ghép âm thanh"
    job["outputs"].append({
        "type": "video", "file": output_name, "thumb": thumb, "san_pham": title,
        "dur": round(E.probe(output_path), 1), "kich_ban": "Thay âm thanh từ link khác",
        "source_video": video_url, "source_audio": audio_url,
        "audio_title": audio_meta.get("title") or "Âm thanh nguồn", "speed": f"{video_speed:g}x",
        "flip": E.XFORM_LABEL.get(movie_flip, movie_flip),
    })
    job["status"] = "done"
    E.log(f"[{job['id']}] Hoàn tất ghép âm thanh", job); save(job)


def run_safe(job, save):
    try:
        run_soundtrack(job, save)
    except Exception as exc:
        job["status"] = "error"
        job["error"] = f"{type(exc).__name__}: {exc}"
        job.setdefault("log", []).append(traceback.format_exc()[-1200:])
        save(job)
