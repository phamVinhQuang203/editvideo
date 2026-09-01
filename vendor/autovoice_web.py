#!/usr/bin/env python3
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import autovoice_tool as core

try:
    import video_form_brain
except Exception:
    video_form_brain = None


ROOT = Path(__file__).resolve().parents[1]
CONFIG = core.load_config()
app = FastAPI(title="AutoVoice")
templates = Jinja2Templates(directory=str(ROOT / "templates"))


def codex_subprocess_env() -> dict:
    env = os.environ.copy()
    runtime_home = Path(env.get("AUTOVOICE_CODEX_HOME") or (ROOT / ".codex-runtime"))
    runtime_home.mkdir(parents=True, exist_ok=True)
    (runtime_home / ".tmp").mkdir(parents=True, exist_ok=True)

    source_home = Path(env.get("CODEX_SOURCE_HOME") or Path.home() / ".codex")
    for name in ("auth.json", "config.toml"):
        src = source_home / name
        dst = runtime_home / name
        if src.exists():
            try:
                if (not dst.exists()) or src.stat().st_mtime > dst.stat().st_mtime or src.stat().st_size != dst.stat().st_size:
                    shutil.copy2(src, dst)
            except OSError:
                pass

    env["CODEX_HOME"] = str(runtime_home)
    return env


def autovoice_ai_mode() -> str:
    return str(os.environ.get("AUTOVOICE_AI_MODE") or CONFIG.get("ai_mode") or "smart").strip().lower()


def should_use_codex_for(kind: str) -> bool:
    mode = autovoice_ai_mode()
    if mode in {"0", "false", "off", "offline", "local", "rule"}:
        return False
    if mode in {"1", "true", "on", "codex", "online", "ai"}:
        return True
    if mode == "smart":
        return kind in {"product", "autovoice"}
    return kind in {part.strip() for part in mode.split(",")}


def path_in(base: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def job_dirs():
    work = Path(CONFIG["work_dir"])
    work.mkdir(parents=True, exist_ok=True)
    return sorted([p for p in work.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)


def job_info(job_dir: Path):
    source = job_dir / "source-path.txt"
    script = job_dir / "voice-script.txt"
    transcript = job_dir / "transcript-zh.txt"
    meta = job_dir / "metadata.json"
    output = Path(CONFIG["output_dir"]) / f"{job_dir.name}-vi-voice.mp4"
    error = job_dir / "last-error.txt"
    duration = ""
    if meta.exists():
        try:
            duration = f"{core.duration_from_probe(json.loads(meta.read_text(encoding='utf-8'))):.2f}s"
        except Exception:
            duration = ""
    return {
        "id": job_dir.name,
        "source": source.read_text(encoding="utf-8") if source.exists() else "",
        "has_contact": (job_dir / "contact-sheet.jpg").exists(),
        "script": script.read_text(encoding="utf-8") if script.exists() else "",
        "has_script": script.exists() and bool(script.read_text(encoding="utf-8").strip()),
        "transcript": transcript.read_text(encoding="utf-8") if transcript.exists() else "",
        "has_output": output.exists(),
        "output_name": output.name,
        "duration": duration,
        "error": error.read_text(encoding="utf-8", errors="replace") if error.exists() else "",
    }


def get_job(job_id: str) -> Path:
    job = Path(CONFIG["work_dir"]) / job_id
    if not path_in(Path(CONFIG["work_dir"]), job) or not job.exists():
        raise HTTPException(404, "Job not found")
    return job


def text_like_score(raw: bytes, width: int, height: int) -> float:
    if len(raw) < width * height:
        return 0.0
    pixels = raw[: width * height]
    bright = sum(1 for value in pixels if value >= 205)
    bright_density = bright / max(1, width * height)
    edge = 0
    for y in range(height):
        row = y * width
        for x in range(1, width):
            if abs(pixels[row + x] - pixels[row + x - 1]) >= 45:
                edge += 1
    edge_density = edge / max(1, height * (width - 1))
    row_hits = 0
    for y in range(height):
        row = pixels[y * width : (y + 1) * width]
        if sum(1 for value in row if value >= 205) / width >= 0.035:
            row_hits += 1
    row_density = row_hits / max(1, height)
    if bright_density < 0.015 or bright_density > 0.42:
        return 0.0
    return (bright_density * 2.0) + edge_density + row_density


def has_chinese_text(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", str(value or "")))


def is_chinese_ocr_text(value: str, confidence: float) -> bool:
    text = str(value or "")
    if has_chinese_text(text):
        return True
    compact = re.sub(r"\s+", "", text)
    return float(confidence or 0) >= 0.75 and bool(re.fullmatch(r"[?？]{2,}", compact))


def probe_video_duration(video_path: str) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-hide_banner",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nk=1:nw=1",
            video_path,
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        return max(0.0, float(proc.stdout.strip()))
    except Exception:
        return 0.0


def extract_ocr_frame(video_path: str, frame_path: Path, second: float) -> bool:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, second):.3f}",
        "-i",
        video_path,
        "-frames:v",
        "1",
        str(frame_path),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.returncode == 0 and frame_path.exists() and frame_path.stat().st_size > 0


def merge_same_frame_boxes(boxes: list[dict], x_gap: int = 18, y_gap: int = 12) -> list[dict]:
    merged: list[dict] = []
    for box in sorted(boxes, key=lambda item: (item["y"], item["x"])):
        target = None
        for current in merged:
            same_line = abs((box["y"] + box["h"] / 2) - (current["y"] + current["h"] / 2)) <= max(y_gap, current["h"] * 0.55)
            close_x = box["x"] <= current["x"] + current["w"] + x_gap and current["x"] <= box["x"] + box["w"] + x_gap
            if same_line and close_x:
                target = current
                break
        if target is None:
            merged.append(dict(box))
            continue
        x1 = min(target["x"], box["x"])
        y1 = min(target["y"], box["y"])
        x2 = max(target["x"] + target["w"], box["x"] + box["w"])
        y2 = max(target["y"] + target["h"], box["y"] + box["h"])
        target.update({
            "x": x1,
            "y": y1,
            "w": x2 - x1,
            "h": y2 - y1,
            "confidence": max(float(target.get("confidence", 0)), float(box.get("confidence", 0))),
            "text": f"{target.get('text', '')} {box.get('text', '')}".strip(),
        })
    return merged


def overlap_ratio(a: dict, b: dict) -> float:
    ax2 = a["x"] + a["w"]
    ay2 = a["y"] + a["h"]
    bx2 = b["x"] + b["w"]
    by2 = b["y"] + b["h"]
    ix = max(0, min(ax2, bx2) - max(a["x"], b["x"]))
    iy = max(0, min(ay2, by2) - max(a["y"], b["y"]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    return inter / max(1, min(a["w"] * a["h"], b["w"] * b["h"]))


def merge_timed_blur_regions(regions: list[dict], max_gap: float = 1.15) -> list[dict]:
    merged: list[dict] = []
    for region in sorted(regions, key=lambda item: (item["start"], item["y"], item["x"])):
        target = None
        for current in merged:
            if region["start"] - current["end"] > max_gap:
                continue
            same_place = overlap_ratio(region, current) >= 0.18
            close_center = (
                abs((region["x"] + region["w"] / 2) - (current["x"] + current["w"] / 2)) <= max(60, current["w"] * 0.75)
                and abs((region["y"] + region["h"] / 2) - (current["y"] + current["h"] / 2)) <= max(34, current["h"] * 0.9)
            )
            if same_place or close_center:
                target = current
                break
        if target is None:
            merged.append(dict(region))
            continue
        x1 = min(target["x"], region["x"])
        y1 = min(target["y"], region["y"])
        x2 = max(target["x"] + target["w"], region["x"] + region["w"])
        y2 = max(target["y"] + target["h"], region["y"] + region["h"])
        target.update({
            "start": min(target["start"], region["start"]),
            "end": max(target["end"], region["end"]),
            "x": x1,
            "y": y1,
            "w": x2 - x1,
            "h": y2 - y1,
            "confidence": max(float(target.get("confidence", 0)), float(region.get("confidence", 0))),
            "text": f"{target.get('text', '')} {region.get('text', '')}".strip()[:160],
        })
        evidence = []
        for source in (target, region):
            for value in source.get("frameEvidenceTimes") or []:
                try:
                    evidence.append(round(float(value), 2))
                except (TypeError, ValueError):
                    continue
            if source.get("local") is not None:
                try:
                    evidence.append(round((float(source.get("start", 0)) + float(source.get("end", 0))) / 2, 2))
                except (TypeError, ValueError):
                    pass
        if evidence:
            target["frameEvidenceTimes"] = sorted(set(evidence))[:48]
    return merged


def validate_subtitle_blur_regions(regions: list[dict], width: int, height: int, duration: float) -> tuple[list[dict], list[dict]]:
    accepted: list[dict] = []
    rejected: list[dict] = []
    for raw in regions:
        region = dict(raw)
        x = max(0, int(region.get("x", 0)))
        y = max(0, int(region.get("y", 0)))
        w = max(1, int(region.get("w", 1)))
        h = max(1, int(region.get("h", 1)))
        x2 = min(width, x + w)
        y2 = min(height, y + h)
        w = max(1, x2 - x)
        h = max(1, y2 - y)
        start = max(0.0, float(region.get("start", 0.0)))
        end = min(duration, max(start, float(region.get("end", duration))))
        region.update({"x": x, "y": y, "w": w, "h": h, "start": start, "end": end})

        center_x = x + w / 2
        center_y = y + h / 2
        width_ratio = w / max(1, width)
        height_ratio = h / max(1, height)
        area_ratio = (w * h) / max(1, width * height)
        duration_ratio = (end - start) / max(0.1, duration)
        label = str(region.get("text", ""))
        reasons: list[str] = []

        is_bottom_subtitle_zone = (
            height * 0.60 <= center_y <= height * 0.95
            and abs(center_x - width / 2) <= width * 0.40
        )
        is_edge_overlay_zone = (
            y <= height * 0.18
            or y + h >= height * 0.86
            or x <= width * 0.14
            or x + w >= width * 0.86
        )

        if "stable_repeated_text_overlay" in label:
            if not (is_bottom_subtitle_zone or is_edge_overlay_zone):
                reasons.append("stable_text_not_subtitle_or_edge_overlay")
            if width_ratio > 0.94:
                reasons.append("stable_text_too_wide")
            if height_ratio > 0.18:
                reasons.append("stable_text_too_tall")
            if area_ratio > 0.075:
                reasons.append("stable_text_too_much_frame")
            if (end - start) < 1.0:
                reasons.append("stable_text_too_short_time")
            if reasons:
                region["rejectReasons"] = reasons
                rejected.append(region)
                continue
            accepted.append(region)
            continue

        if "stable_fixed_overlay" in label:
            if not is_edge_overlay_zone:
                reasons.append("fixed_overlay_not_edge")
            if width_ratio > 0.94:
                reasons.append("fixed_overlay_too_wide")
            if height_ratio > 0.18:
                reasons.append("fixed_overlay_too_tall")
            if area_ratio > 0.075:
                reasons.append("fixed_overlay_too_much_frame")
            if (end - start) < 1.0:
                reasons.append("fixed_overlay_too_short_time")
            if reasons:
                region["rejectReasons"] = reasons
                rejected.append(region)
                continue
            accepted.append(region)
            continue

        max_center_y = height * (0.985 if "bottom_fineprint" in label else 0.94)
        if center_y < height * 0.60 or center_y > max_center_y:
            reasons.append("not_subtitle_band")
        if abs(center_x - width / 2) > width * 0.40:
            reasons.append("not_centered_like_subtitle")
        if width_ratio < 0.12:
            reasons.append("too_narrow")
        if width_ratio > 0.92:
            reasons.append("too_wide")
        if height_ratio < 0.016:
            reasons.append("too_short")
        if height_ratio > 0.13:
            reasons.append("too_tall")
        if area_ratio > 0.09:
            reasons.append("too_much_frame")
        if (end - start) < 0.35:
            reasons.append("too_short_time")
        if "color_overlay_subtitle" in label and duration_ratio > 0.72:
            reasons.append("color_region_too_long")
        if "color_overlay_subtitle" in label:
            if x <= width * 0.04 or x + w >= width * 0.96:
                reasons.append("color_region_touches_edge")
            if center_y < height * 0.70:
                reasons.append("color_region_too_high")
            if width_ratio > 0.90:
                reasons.append("color_region_too_wide")
        if "bottom_fineprint" in label:
            if center_y < height * 0.88:
                reasons.append("fineprint_not_bottom_edge")
            if height_ratio > 0.070:
                reasons.append("fineprint_too_tall")

        if reasons:
            region["rejectReasons"] = reasons
            rejected.append(region)
            continue
        accepted.append(region)
    return accepted, rejected


def extend_stable_subtitle_blur_regions(regions: list[dict], width: int, height: int, duration: float) -> list[dict]:
    if duration <= 0 or not regions:
        return regions
    normalized: list[dict] = []
    subtitle_regions: list[dict] = []
    for raw in regions:
        region = dict(raw)
        center_y = float(region.get("y", 0)) + float(region.get("h", 0)) / 2
        width_ratio = float(region.get("w", 0)) / max(1, width)
        height_ratio = float(region.get("h", 0)) / max(1, height)
        label = str(region.get("text", ""))
        max_center_y = height * (0.985 if "bottom_fineprint" in label else 0.94)
        looks_like_bottom_subtitle = (
            height * 0.60 <= center_y <= max_center_y
            and 0.12 <= width_ratio <= 0.92
            and 0.016 <= height_ratio <= 0.13
        )
        if not looks_like_bottom_subtitle:
            normalized.append(region)
            continue
        subtitle_regions.append(region)

    # Douyin captions often keep the same baseline while the sentence length changes.
    # Build one full-duration mask from the largest/union subtitle box so short OCR
    # samples do not leave Chinese characters visible on either side.
    clusters: list[list[dict]] = []
    for region in sorted(subtitle_regions, key=lambda item: float(item.get("y", 0)) + float(item.get("h", 0)) / 2):
        center_y = float(region.get("y", 0)) + float(region.get("h", 0)) / 2
        target = None
        for cluster in clusters:
            ref = cluster[0]
            ref_center_y = float(ref.get("y", 0)) + float(ref.get("h", 0)) / 2
            if abs(center_y - ref_center_y) <= max(28, height * 0.045):
                target = cluster
                break
        if target is None:
            clusters.append([region])
        else:
            target.append(region)

    for cluster in clusters:
        evidence_times: list[float] = []
        for item in cluster:
            for value in item.get("frameEvidenceTimes") or []:
                try:
                    evidence_times.append(round(float(value), 2))
                except (TypeError, ValueError):
                    continue
            if item.get("local") is not None:
                try:
                    evidence_times.append(round((float(item.get("start", 0)) + float(item.get("end", 0))) / 2, 2))
                except (TypeError, ValueError):
                    pass
        evidence_times = sorted(set(evidence_times))
        cluster_label = " ".join(str(item.get("text", "")) for item in cluster)
        min_evidence = 4 if "bottom_fineprint" in cluster_label else 3
        evidence_span = (max(evidence_times) - min(evidence_times)) if len(evidence_times) >= 2 else 0.0
        if len(evidence_times) < min_evidence:
            continue
        if "bottom_fineprint" in cluster_label and evidence_span < max(2.0, duration * 0.22):
            continue
        x1 = min(int(item.get("x", 0)) for item in cluster)
        y1 = min(int(item.get("y", 0)) for item in cluster)
        x2 = max(int(item.get("x", 0)) + int(item.get("w", 1)) for item in cluster)
        y2 = max(int(item.get("y", 0)) + int(item.get("h", 1)) for item in cluster)
        center_x = (x1 + x2) / 2
        max_w = int(width * float(CONFIG.get("subtitle_blur_stable_max_width_ratio", 0.90)))
        # Keep the mask tied to observed subtitle text. Older logic forced a
        # 55-62% minimum width, which made short subtitles blur too much scene.
        observed_w = max(1, x2 - x1)
        target_w = min(max_w, observed_w)
        x1 = int(round(center_x - target_w / 2))
        x2 = x1 + target_w
        # Incoming subtitle boxes already include detector padding. Extend only
        # adds a tiny safety margin while taking the widest same-baseline text.
        pad_x = max(5, int(width * 0.008))
        pad_y = max(2, int(height * 0.003))
        x = max(0, x1 - pad_x)
        y = max(0, y1 - pad_y)
        x2 = min(width, x2 + pad_x)
        y2 = min(height, y2 + pad_y)
        sample_heights = [float(item.get("h", 0)) for item in cluster if float(item.get("h", 0) or 0) > 0]
        median_h = median_number(sample_heights)
        core_heights = [float(item.get("textCoreH", 0)) for item in cluster if float(item.get("textCoreH", 0) or 0) > 0]
        median_core_h = median_number(core_heights)
        observed_h = max(1, y2 - y)
        # Keep subtitle height tied to the detected text itself. The rule is:
        # use the widest observed subtitle baseline for width, but only a small
        # vertical padding around the actual text, never a fixed frame-percent
        # height that can blur too much of the product scene.
        target_h = observed_h
        if median_core_h > 0:
            expected_h = int(round(median_core_h + max(6, int(height * 0.006)) * 2))
            target_h = max(1, min(observed_h, expected_h))
        elif median_h > 0:
            expected_h = int(round(median_h + pad_y * 2))
            if observed_h > max(expected_h + pad_y * 2, expected_h * 1.55):
                target_h = max(1, min(observed_h, expected_h))
        if observed_h > target_h:
            centers = [
                float(item.get("y", 0)) + float(item.get("h", 0)) / 2
                for item in cluster
                if float(item.get("h", 0) or 0) > 0
            ]
            center_y = median_number(centers) if centers else (y + y2) / 2
            y = max(0, int(round(center_y - target_h / 2)))
            y2 = min(height, y + target_h)
        best = max(cluster, key=lambda item: int(item.get("w", 0)) * int(item.get("h", 0)))
        best.update({
            "x": x,
            "y": y,
            "w": max(1, x2 - x),
            "h": max(1, y2 - y),
            "start": 0.0,
            "end": duration,
            "text": f"{str(best.get('text', '')).split()[0] if best.get('text') else 'subtitle'} stable_subtitle_full_duration",
            "frameEvidenceTimes": evidence_times[:48],
        })
        normalized.append(best)
    return merge_timed_blur_regions(normalized, max_gap=duration + 1.0)


def stable_repeated_text_blur_regions(
    regions: list[dict],
    width: int,
    height: int,
    duration: float,
    sample_count: int = 0,
) -> list[dict]:
    if duration <= 0 or width <= 0 or height <= 0 or not regions:
        return []
    clusters: list[list[dict]] = []
    for raw in regions:
        try:
            region = dict(raw)
            x = int(region.get("x", 0))
            y = int(region.get("y", 0))
            w = int(region.get("w", 0))
            h = int(region.get("h", 0))
            start = float(region.get("start", 0.0))
            end = float(region.get("end", start))
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        width_ratio = w / max(1, width)
        height_ratio = h / max(1, height)
        area_ratio = (w * h) / max(1, width * height)
        if width_ratio < 0.035 or height_ratio < 0.010:
            continue
        if width_ratio > 0.94 or height_ratio > 0.18 or area_ratio > 0.075:
            continue
        cx = x + w / 2
        cy = y + h / 2
        target = None
        for cluster in clusters:
            rcx = median_number([float(item["x"]) + float(item["w"]) / 2 for item in cluster])
            rcy = median_number([float(item["y"]) + float(item["h"]) / 2 for item in cluster])
            if abs(cx - rcx) <= max(42, width * 0.055) and abs(cy - rcy) <= max(30, height * 0.045):
                target = cluster
                break
        if target is None:
            clusters.append([region])
        else:
            target.append(region)

    stable: list[dict] = []
    min_support = max(3, min(7, int(max(1, sample_count) * 0.28)))
    for cluster in clusters:
        times = sorted({
            round((float(item.get("start", 0.0)) + float(item.get("end", 0.0))) / 2, 1)
            for item in cluster
        })
        if len(times) < min_support:
            continue
        x1 = min(int(item.get("x", 0)) for item in cluster)
        y1 = min(int(item.get("y", 0)) for item in cluster)
        x2 = max(int(item.get("x", 0)) + int(item.get("w", 1)) for item in cluster)
        y2 = max(int(item.get("y", 0)) + int(item.get("h", 1)) for item in cluster)
        raw_w = max(1, x2 - x1)
        raw_h = max(1, y2 - y1)
        if raw_w / max(1, width) > 0.94 or raw_h / max(1, height) > 0.18:
            continue
        pad_x = max(10, int(width * 0.018))
        pad_y = max(5, int(height * 0.008))
        span = max(times) - min(times) if times else 0.0
        start = max(0.0, min(times) - 0.75)
        end = min(duration, max(times) + 1.25)
        if len(times) >= max(min_support + 1, int(max(1, sample_count) * 0.45)) or span >= duration * 0.42:
            start, end = 0.0, duration
        stable.append({
            "x": max(0, x1 - pad_x),
            "y": max(0, y1 - pad_y),
            "w": max(1, min(width - max(0, x1 - pad_x), raw_w + pad_x * 2)),
            "h": max(1, min(height - max(0, y1 - pad_y), raw_h + pad_y * 2)),
            "start": start,
            "end": end,
            "confidence": round(max(float(item.get("confidence", 0)) for item in cluster), 3),
            "text": "stable_repeated_text_overlay",
            "support": len(times),
            "frameEvidenceTimes": times[:24],
        })
    stable.sort(key=lambda item: (item["support"], item["w"] * item["h"]), reverse=True)
    for item in stable:
        item.pop("support", None)
    return stable[:3]


def subtitle_policy_summary(accepted: list[dict], rejected: list[dict], duration: float) -> dict:
    subtitle_accepted = [
        item for item in accepted
        if "stable_fixed_overlay" not in str(item.get("text", ""))
        and "stable_repeated_text_overlay" not in str(item.get("text", ""))
    ]
    positions: list[tuple[int, int]] = []
    for region in subtitle_accepted:
        positions.append((round((region["x"] + region["w"] / 2) / 48), round((region["y"] + region["h"] / 2) / 48)))
    distinct_positions = len(set(positions))
    covered = sum(max(0.0, float(region.get("end", 0)) - float(region.get("start", 0))) for region in subtitle_accepted)
    coverage_ratio = covered / max(0.1, duration)
    non_subtitle_rejected = []
    for item in rejected:
        reasons = set(item.get("rejectReasons", []))
        if not reasons.intersection({"not_subtitle_band", "not_centered_like_subtitle", "too_wide", "too_tall", "too_much_frame"}):
            continue
        if "stable_fixed_overlay" not in str(item.get("text", "")):
            continue
        # Fixed overlays are useful blur hints, not a reason to reject a video by
        # themselves. This avoids rejecting clean product videos because a small
        # logo/control/label was detected outside the subtitle band.
        continue
    reject_reasons: list[str] = []
    max_regions = int(CONFIG.get("subtitle_blur_max_regions", 3))
    max_positions = int(CONFIG.get("subtitle_blur_max_positions", 2))
    max_rejected = int(CONFIG.get("subtitle_blur_max_non_subtitle_rejected", 1))
    max_coverage = float(CONFIG.get("subtitle_blur_max_coverage_ratio", 1.15))
    if len(subtitle_accepted) > max_regions:
        reject_reasons.append("too_many_subtitle_regions")
    all_stable_full_duration = bool(subtitle_accepted) and all(
        (
            "stable_subtitle_full_duration" in str(item.get("text", ""))
        )
        and abs((float(item.get("end", 0)) - float(item.get("start", 0))) - duration) <= 0.75
        for item in subtitle_accepted
    )
    if distinct_positions > max_positions and not all_stable_full_duration:
        reject_reasons.append("subtitle_moves_too_many_positions")
    if len(non_subtitle_rejected) > max_rejected:
        reject_reasons.append("too_many_non_subtitle_text_regions")
    stable_full_duration_subtitle = all_stable_full_duration and len(subtitle_accepted) <= max_regions
    if coverage_ratio > max_coverage and not stable_full_duration_subtitle:
        reject_reasons.append("subtitle_timeline_too_dense")
    return {
        "rejectRecommended": bool(reject_reasons),
        "rejectReasons": reject_reasons,
        "acceptedRegionCount": len(accepted),
        "acceptedSubtitleRegionCount": len(subtitle_accepted),
        "rejectedRegionCount": len(rejected),
        "nonSubtitleRejectedCount": len(non_subtitle_rejected),
        "distinctAcceptedPositions": distinct_positions,
        "coverageRatio": round(coverage_ratio, 3),
    }


def enforce_text_overlay_policy(job: Path | None, regions: list[dict]):
    if not job:
        return
    manifest = job / "chinese-text-blur-regions.json"
    if not manifest.exists():
        return
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return
    policy = data.get("subtitlePolicy") or {}
    if policy.get("rejectRecommended"):
        reasons = ", ".join(policy.get("rejectReasons") or ["text_overlay_policy_reject"])
        raise HTTPException(422, f"Reject video: Chinese text/subtitle too complex ({reasons})")


def external_ocr_python_candidates() -> list[str]:
    candidates = [
        os.environ.get("AUTOVOICE_OCR_PYTHON", ""),
        r"C:\Users\Admin\AppData\Local\Programs\Python\Python313\python.exe",
        shutil.which("python") or "",
        sys.executable,
    ]
    out: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in out and Path(candidate).exists():
            out.append(candidate)
    return out


def detect_chinese_text_blur_regions_external(video_path: str, job: Path | None = None, reason: str = "") -> list[dict]:
    manifest = (job / "chinese-text-blur-regions.json") if job else None
    helper = Path(__file__).resolve().parent / "detect_chinese_blur_regions.py"
    work_dir = job or (ROOT / "temp" / "ocr-blur")
    work_dir.mkdir(parents=True, exist_ok=True)
    for python_exe in external_ocr_python_candidates():
        if Path(python_exe).resolve() == Path(sys.executable).resolve() and reason:
            continue
        cmd = [
            python_exe,
            str(helper),
            "--video",
            video_path,
            "--work-dir",
            str(work_dir),
        ]
        if manifest:
            cmd += ["--manifest", str(manifest)]
        proc = subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=240,
        )
        if proc.returncode != 0:
            continue
        try:
            data = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            continue
        regions = data.get("regions") or []
        if not isinstance(regions, list):
            return []
        duration = probe_video_duration(video_path)
        width, height = probe_video_size(video_path)
        if duration <= 0 or width <= 0 or height <= 0:
            return []
        sample_count = int(data.get("sampleCount") or data.get("subtitleSampleCount") or 0)
        stable_regions = stable_repeated_text_blur_regions(regions, width, height, duration, sample_count)
        stable_regions, stable_rejected = validate_subtitle_blur_regions(stable_regions, width, height, duration)
        accepted, rejected = validate_subtitle_blur_regions(regions, width, height, duration)
        accepted = extend_stable_subtitle_blur_regions(accepted, width, height, duration)
        if stable_regions:
            accepted = [
                *stable_regions,
                *[item for item in accepted if not any(overlap_ratio(item, stable) >= 0.35 for stable in stable_regions)],
            ]
            rejected.extend(stable_rejected)
        if manifest:
            policy = subtitle_policy_summary(accepted, rejected, duration)
            manifest.write_text(json.dumps({
                "ok": True,
                "detector": "external_ocr_validated",
                "sourceDetector": data.get("detector") or "external_ocr",
                "reason": reason,
                "video": video_path,
                "duration": round(duration, 3),
                "width": width,
                "height": height,
                "regionCount": len(accepted),
                "subtitlePolicy": policy,
                "rejectRecommended": policy["rejectRecommended"],
                "rejectReasons": policy["rejectReasons"],
                "subtitlePreflight": {
                    "hasChineseSubtitle": bool(accepted),
                    "acceptedRegionCount": len(accepted),
                    "rejectedRegionCount": len(rejected),
                    "rules": "repeated text across sampled frames; stable same-position text may blur at any screen position",
                },
                "regions": accepted,
                "rejectedRegions": rejected,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        return accepted
    if manifest:
        manifest.write_text(json.dumps({
            "ok": False,
            "reason": reason or "external_ocr_failed",
            "regions": [],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return []


def probe_video_size(video_path: str) -> tuple[int, int]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            video_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    match = re.search(r"(\d+)x(\d+)", proc.stdout or "")
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def fixed_region(width: int, height: int, x: float, y: float, w: float, h: float, start: float, end: float, score: float, label: str) -> dict:
    return {
        "x": max(0, int(width * x)),
        "y": max(0, int(height * y)),
        "w": max(1, int(width * w)),
        "h": max(1, int(height * h)),
        "start": max(0.0, float(start)),
        "end": max(0.0, float(end)),
        "confidence": round(score, 3),
        "text": label,
    }


def text_region_hits(video_path: str, crop: str, times: tuple[str, ...], out_width: int, out_height: int, threshold: float) -> tuple[int, list[float]]:
    hits = 0
    scores: list[float] = []
    for second in times:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            second,
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-vf",
            f"{crop},scale={out_width}:{out_height},format=gray",
            "-f",
            "rawvideo",
            "-",
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0 or not proc.stdout:
            scores.append(0.0)
            continue
        score = text_like_score(proc.stdout, out_width, out_height)
        scores.append(score)
        if score >= threshold:
            hits += 1
    return hits, scores


def extract_gray_crop(video_path: str, crop: str, second: str, out_width: int, out_height: int) -> bytes:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        second,
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-vf",
        f"{crop},scale={out_width}:{out_height},format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.stdout if proc.returncode == 0 else b""


def extract_rgb_frame(video_path: str, second: str, width: int, height: int) -> bytes:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        second,
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:{height},format=rgb24",
        "-f",
        "rawvideo",
        "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.stdout if proc.returncode == 0 else b""


def overlay_text_box_from_rgb(raw: bytes, width: int, height: int) -> tuple[int, int, int, int] | None:
    try:
        import numpy as np
    except Exception:
        return None
    if len(raw) < width * height * 3:
        return None
    frame = np.frombuffer(raw[: width * height * 3], dtype=np.uint8).reshape((height, width, 3))
    r = frame[:, :, 0].astype(np.int16)
    g = frame[:, :, 1].astype(np.int16)
    b = frame[:, :, 2].astype(np.int16)
    high = np.maximum(np.maximum(r, g), b)
    low = np.minimum(np.minimum(r, g), b)
    white = (r >= 165) & (g >= 165) & (b >= 165) & ((high - low) <= 115)
    yellow = (r >= 160) & (g >= 115) & (b <= 150) & ((r - b) >= 35) & ((g - b) >= 15)
    dark = high <= 95
    near_dark = dark.copy()
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            if dx == 0 and dy == 0:
                continue
            near_dark |= np.roll(np.roll(dark, dy, axis=0), dx, axis=1)
    mask = (white | yellow) & near_dark
    mask[: int(height * 0.44), :] = False
    mask[int(height * 0.92):, :] = False
    if int(mask.sum()) < 24:
        return None

    ys, xs = np.where(mask)
    points = set(zip(xs.tolist(), ys.tolist()))
    components: list[dict] = []
    while points:
        stack = [points.pop()]
        min_x = max_x = stack[0][0]
        min_y = max_y = stack[0][1]
        count = 0
        while stack:
            x, y = stack.pop()
            count += 1
            if x < min_x: min_x = x
            if x > max_x: max_x = x
            if y < min_y: min_y = y
            if y > max_y: max_y = y
            for nx in (x - 1, x, x + 1):
                for ny in (y - 1, y, y + 1):
                    if nx == x and ny == y:
                        continue
                    point = (nx, ny)
                    if point in points:
                        points.remove(point)
                        stack.append(point)
        comp_w = max_x - min_x + 1
        comp_h = max_y - min_y + 1
        if count < 6 or comp_w < 3 or comp_h < 3:
            continue
        if comp_w > width * 0.22 or comp_h > height * 0.08:
            continue
        components.append({"x": min_x, "y": min_y, "w": comp_w, "h": comp_h, "confidence": count, "text": "overlay_component"})
    if not components:
        return None
    line_boxes = merge_same_frame_boxes(components, x_gap=max(42, int(width * 0.085)), y_gap=max(12, int(height * 0.014)))
    candidates = []
    for box in line_boxes:
        x1, y1, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
        if w < width * 0.16 or w > width * 0.88:
            continue
        if h < height * 0.012 or h > height * 0.09:
            continue
        center_x = x1 + w / 2
        center_y = y1 + h / 2
        if abs(center_x - width / 2) > width * 0.34:
            continue
        if center_y < height * 0.52 or center_y > height * 0.90:
            continue
        white_count = int(white[y1:y1 + h, x1:x1 + w].sum())
        yellow_count = int(yellow[y1:y1 + h, x1:x1 + w].sum())
        if white_count < 16 and not (white_count >= 8 and yellow_count >= 8):
            continue
        y_bonus = 1.0 + max(0.0, (center_y / height - 0.58)) * 2.2
        center_bonus = 1.0 - min(0.55, abs(center_x - width / 2) / max(1, width * 0.62))
        score = (white_count + yellow_count * 0.8) * min(1.0, w / max(1, width * 0.38)) * y_bonus * center_bonus
        candidates.append((score, x1, y1, w, h))
    if not candidates:
        return None
    _, x, y, w, h = max(candidates, key=lambda item: item[0])
    return int(x), int(y), int(w), int(h)


def overlay_text_boxes_from_rgb(raw: bytes, width: int, height: int) -> list[dict]:
    try:
        import numpy as np
    except Exception:
        return []
    if len(raw) < width * height * 3:
        return []
    frame = np.frombuffer(raw[: width * height * 3], dtype=np.uint8).reshape((height, width, 3))
    r = frame[:, :, 0].astype(np.int16)
    g = frame[:, :, 1].astype(np.int16)
    b = frame[:, :, 2].astype(np.int16)
    high = np.maximum(np.maximum(r, g), b)
    low = np.minimum(np.minimum(r, g), b)
    white = (r >= 168) & (g >= 168) & (b >= 168) & ((high - low) <= 125)
    yellow = (r >= 160) & (g >= 112) & (b <= 155) & ((r - b) >= 30)
    dark = high <= 98
    near_dark = dark.copy()
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            if dx == 0 and dy == 0:
                continue
            near_dark |= np.roll(np.roll(dark, dy, axis=0), dx, axis=1)
    mask = (white | yellow) & near_dark
    if int(mask.sum()) < 24:
        return []

    ys, xs = np.where(mask)
    points = set(zip(xs.tolist(), ys.tolist()))
    components: list[dict] = []
    while points:
        stack = [points.pop()]
        min_x = max_x = stack[0][0]
        min_y = max_y = stack[0][1]
        count = 0
        while stack:
            x, y = stack.pop()
            count += 1
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            for nx in (x - 1, x, x + 1):
                for ny in (y - 1, y, y + 1):
                    point = (nx, ny)
                    if point in points:
                        points.remove(point)
                        stack.append(point)
        comp_w = max_x - min_x + 1
        comp_h = max_y - min_y + 1
        if count < 6 or comp_w < 3 or comp_h < 3:
            continue
        if comp_w > width * 0.52 or comp_h > height * 0.24:
            continue
        components.append({"x": min_x, "y": min_y, "w": comp_w, "h": comp_h, "confidence": count, "text": "overlay_component"})
    if not components:
        return []

    line_boxes = merge_same_frame_boxes(components, x_gap=max(28, int(width * 0.055)), y_gap=max(10, int(height * 0.012)))
    boxes: list[dict] = []
    for box in line_boxes:
        x, y, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
        center_x = x + w / 2
        center_y = y + h / 2
        near_edge = (
            y <= height * 0.18
            or y + h >= height * 0.84
            or x <= width * 0.16
            or x + w >= width * 0.84
        )
        if not near_edge:
            continue
        if w < max(28, width * 0.045) or h < max(8, height * 0.010):
            continue
        if w > width * 0.92 or h > height * 0.18:
            continue
        horizontal = w >= h * 1.5
        vertical = h >= w * 1.25
        if not horizontal and not vertical:
            continue
        if horizontal and h > max(72, height * 0.075):
            continue
        if vertical and w > max(110, width * 0.16):
            continue
        edge_score = 0.0
        edge_score += max(0.0, 1.0 - y / max(1, height * 0.18))
        edge_score += max(0.0, ((y + h) - height * 0.84) / max(1, height * 0.16))
        edge_score += max(0.0, 1.0 - x / max(1, width * 0.16))
        edge_score += max(0.0, ((x + w) - width * 0.84) / max(1, width * 0.16))
        boxes.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "confidence": round(float(box.get("confidence", 0)) + edge_score * 20, 3),
            "text": "fixed_overlay_text",
        })
    return boxes


def detect_fixed_overlay_text_regions(video_path: str, width: int, height: int, duration: float) -> tuple[list[dict], list[dict]]:
    if duration <= 0:
        return [], []
    if str(os.environ.get("AUTOVOICE_ENABLE_FIXED_OVERLAY_BLUR") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return [], []
    times: list[float] = []
    sample_count = max(6, min(18, int(duration / 2.2) + 1))
    for index in range(sample_count):
        second = min(duration - 0.15, 0.45 + index * (duration / sample_count))
        if second >= 0.1:
            times.append(round(second, 2))
    checks: list[dict] = []
    samples: list[dict] = []
    for second in sorted(set(times)):
        raw = extract_rgb_frame(video_path, f"{second:.2f}", width, height)
        boxes = overlay_text_boxes_from_rgb(raw, width, height) if raw else []
        checks.append({
            "label": "fixed_overlay_text",
            "time": f"{second:.2f}",
            "boxes": [[box["x"], box["y"], box["w"], box["h"]] for box in boxes[:8]],
            "hits": len(boxes),
        })
        for box in boxes:
            item = dict(box)
            item["time"] = second
            samples.append(item)
    if not samples:
        return [], checks

    clusters: list[list[dict]] = []
    for box in sorted(samples, key=lambda item: (item["y"] + item["h"] / 2, item["x"] + item["w"] / 2)):
        cx = box["x"] + box["w"] / 2
        cy = box["y"] + box["h"] / 2
        target = None
        for cluster in clusters:
            rcx = median_number([item["x"] + item["w"] / 2 for item in cluster])
            rcy = median_number([item["y"] + item["h"] / 2 for item in cluster])
            if abs(cx - rcx) <= max(34, width * 0.045) and abs(cy - rcy) <= max(28, height * 0.032):
                target = cluster
                break
        if target is None:
            clusters.append([box])
        else:
            target.append(box)

    regions: list[dict] = []
    min_support = 3 if len(times) >= 8 else 2
    for cluster in clusters:
        unique_times = sorted({round(float(item["time"]), 1) for item in cluster})
        if len(unique_times) < min_support:
            continue
        x1 = min(int(item["x"]) for item in cluster)
        y1 = min(int(item["y"]) for item in cluster)
        x2 = max(int(item["x"] + item["w"]) for item in cluster)
        y2 = max(int(item["y"] + item["h"]) for item in cluster)
        raw_w = max(1, x2 - x1)
        raw_h = max(1, y2 - y1)
        pad_x = max(8, int(width * 0.012))
        pad_y = max(5, int(height * 0.006))
        horizontal = raw_w >= raw_h * 1.5
        max_h = max(84, int(height * 0.085)) if horizontal else max(170, int(height * 0.18))
        max_w = max(160, int(width * 0.22)) if not horizontal else int(width * 0.94)
        if raw_h + pad_y * 2 > max_h or raw_w + pad_x * 2 > max_w:
            continue
        start = max(0.0, min(unique_times) - 0.7)
        end = min(duration, max(unique_times) + 1.25)
        if len(unique_times) >= max(4, int(len(times) * 0.42)):
            start, end = 0.0, duration
        regions.append({
            "x": max(0, x1 - pad_x),
            "y": max(0, y1 - pad_y),
            "w": max(1, min(width - max(0, x1 - pad_x), raw_w + pad_x * 2)),
            "h": max(1, min(height - max(0, y1 - pad_y), raw_h + pad_y * 2)),
            "start": start,
            "end": end,
            "confidence": round(max(float(item.get("confidence", 0)) for item in cluster), 3),
            "text": "fixed_overlay_text stable_fixed_overlay",
        })
    regions.sort(key=lambda item: (item["y"], item["x"]))
    return regions[:3], checks


def detect_color_overlay_subtitle_regions(video_path: str, width: int, height: int, duration: float, trim_start: float, job: Path | None = None) -> tuple[list[dict], list[dict]]:
    times: list[float] = []
    start = max(0.4, min(duration - 0.4, trim_start + 0.4 if trim_start > 0 else 0.6))
    t = start
    while t < max(start + 0.1, duration - 0.4):
        times.append(round(t, 2))
        t += 1.0
    if trim_start > 0:
        times.extend(round(trim_start + offset, 2) for offset in (1.0, 2.0, 3.0, 4.5, 6.0, 8.0, 10.0))
    times = sorted(set(time for time in times if 0.1 <= time <= duration - 0.1))
    boxes: list[dict] = []
    checks: list[dict] = []
    for second in times:
        raw = extract_rgb_frame(video_path, f"{second:.2f}", width, height)
        local = overlay_text_box_from_rgb(raw, width, height) if raw else None
        checks.append({
            "label": "color_overlay_subtitle",
            "time": f"{second:.2f}",
            "box": list(local) if local else None,
            "accepted": bool(local),
        })
        if not local:
            continue
        x, y, w, h = local
        boxes.append({
            "x": max(0, x - max(8, int(width * 0.012))),
            "y": max(0, y - max(8, int(height * 0.006))),
            "w": min(width, w + max(16, int(width * 0.024))),
            "h": min(height, h + max(28, int(height * 0.028))),
            "start": max(0.0, second - 0.45),
            "end": min(duration, second + 1.10),
            "confidence": 1.1,
            "text": "color_overlay_subtitle",
            "local": [x, y, w, h],
        })
    groups: list[list[dict]] = []
    for box in boxes:
        cx = box["x"] + box["w"] / 2
        cy = box["y"] + box["h"] / 2
        target = None
        for group in groups:
            gcx = median_number([item["x"] + item["w"] / 2 for item in group])
            gcy = median_number([item["y"] + item["h"] / 2 for item in group])
            if abs(cy - gcy) <= max(70, height * 0.055) and abs(cx - gcx) <= max(210, width * 0.30):
                target = group
                break
        if target is None:
            groups.append([box])
        else:
            target.append(box)

    stable: list[dict] = []
    min_color_support = int(CONFIG.get("subtitle_blur_min_color_frame_support", 3))
    for group in groups:
        if len(group) < min_color_support:
            continue
        ordered_group = sorted(group, key=lambda item: float(item.get("start", 0)))
        timed_groups: list[list[dict]] = []
        for box in ordered_group:
            if not timed_groups or float(box.get("start", 0)) - float(timed_groups[-1][-1].get("end", 0)) > 1.8:
                timed_groups.append([box])
            else:
                timed_groups[-1].append(box)
        for timed_group in timed_groups:
            if len(timed_group) < min_color_support:
                continue
            # Color subtitle fallback should only cover the observed subtitle
            # plus a small margin; wide padding was catching clean product area.
            pad_x = max(18, int(width * 0.035))
            pad_y = max(8, int(height * 0.008))
            x1 = min(item["x"] for item in timed_group) - pad_x
            y1 = min(item["y"] for item in timed_group)
            x2 = max(item["x"] + item["w"] for item in timed_group) + pad_x
            y2 = max(item["y"] + item["h"] for item in timed_group) + pad_y
            region = {
                "x": max(0, x1),
                "y": max(0, y1),
                "w": max(1, min(width - max(0, x1), x2 - x1)),
                "h": max(1, min(height - max(0, y1), y2 - y1)),
                "start": max(0.0, min(float(item.get("start", 0)) for item in timed_group) - 0.25),
                "end": min(duration, max(float(item.get("end", 0)) for item in timed_group) + 0.35),
                "confidence": max(float(item.get("confidence", 0)) for item in timed_group),
                "text": "color_overlay_subtitle",
                "support": len(timed_group),
                "frameEvidenceTimes": [round(float(item.get("start", 0)) + 0.45, 2) for item in timed_group],
            }
            center_y = region["y"] + region["h"] / 2
            center_x = region["x"] + region["w"] / 2
            if center_y < height * 0.64 or center_y > height * 0.93:
                continue
            if abs(center_x - width / 2) > width * 0.38:
                continue
            if region["h"] > max(150, int(height * 0.115)):
                continue
            if region["w"] < width * 0.18:
                continue
            stable.append(region)
    stable.sort(key=lambda item: (item["y"] + item["h"] / 2, item.get("support", 0), item["w"]), reverse=True)
    for item in stable:
        item.pop("support", None)
    return stable[:1], checks


def dynamic_text_box(raw: bytes, width: int, height: int) -> tuple[int, int, int, int] | None:
    if len(raw) < width * height:
        return None
    pixels = raw[: width * height]
    row_hits: list[tuple[int, float]] = []
    for y in range(height):
        row = y * width
        bright = 0
        edges = 0
        for x in range(1, width):
            value = pixels[row + x]
            if value >= 205:
                bright += 1
            if abs(value - pixels[row + x - 1]) >= 45:
                edges += 1
        bright_density = bright / max(1, width)
        edge_density = edges / max(1, width - 1)
        if bright_density >= 0.025 and edge_density >= 0.045:
            row_hits.append((y, bright_density + edge_density))
    if not row_hits:
        return None
    clusters: list[list[tuple[int, float]]] = []
    for item in row_hits:
        if not clusters or item[0] - clusters[-1][-1][0] > 2:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    clusters = [cluster for cluster in clusters if len(cluster) >= 2]
    if not clusters:
        return None
    best = max(clusters, key=lambda cluster: sum(score for _, score in cluster) * min(1.0, len(cluster) / 8))
    y1 = max(0, min(y for y, _ in best) - 3)
    y2 = min(height - 1, max(y for y, _ in best) + 3)

    col_hits: list[int] = []
    for x in range(width):
        bright = 0
        edges = 0
        for y in range(max(1, y1), y2 + 1):
            idx = y * width + x
            value = pixels[idx]
            if value >= 205:
                bright += 1
            if abs(value - pixels[idx - width]) >= 45:
                edges += 1
        band_h = max(1, y2 - y1 + 1)
        if bright / band_h >= 0.025 or edges / band_h >= 0.055:
            col_hits.append(x)
    if not col_hits:
        return None
    x1 = max(0, min(col_hits) - 5)
    x2 = min(width - 1, max(col_hits) + 5)
    if x2 - x1 < width * 0.08 or y2 - y1 < height * 0.10:
        return None
    return x1, y1, x2 - x1 + 1, y2 - y1 + 1


def median_number(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2


def dynamic_subtitle_reject_reason(
    local: tuple[int, int, int, int],
    out_width: int,
    out_height: int,
    min_y_ratio: float = 0.18,
    max_height_ratio: float = 0.32,
    reject_edge_touch: bool = False,
) -> str:
    lx, ly, lw, lh = local
    width_ratio = lw / max(1, out_width)
    height_ratio = lh / max(1, out_height)
    touches_left = lx <= max(4, int(out_width * 0.025))
    touches_right = lx + lw >= out_width - max(4, int(out_width * 0.025))
    if height_ratio > max_height_ratio:
        return "box_too_tall"
    if ly < out_height * min_y_ratio:
        return "too_high_for_bottom_subtitle"
    if reject_edge_touch and width_ratio >= 0.94 and touches_left and touches_right:
        return "touches_crop_edges"
    if width_ratio >= 0.98 and (touches_left or touches_right) and height_ratio > 0.31:
        return "touches_crop_edges"
    if width_ratio < 0.10 or lh < out_height * 0.11:
        return "box_too_small"
    return ""


def stable_dynamic_subtitle_boxes(boxes: list[dict], out_height: int) -> list[dict]:
    if len(boxes) < 3:
        return []
    y_centers = [float(box["local"][1]) + float(box["local"][3]) / 2 for box in boxes]
    heights = [float(box["local"][3]) for box in boxes]
    med_y = median_number(y_centers)
    med_h = median_number(heights)
    stable = [
        box for box, y_center, box_h in zip(boxes, y_centers, heights)
        if abs(y_center - med_y) <= max(8.0, out_height * 0.10)
        and abs(box_h - med_h) <= max(7.0, out_height * 0.08)
    ]
    if len(stable) < 3:
        return []
    y_values = [float(box["local"][1]) for box in stable]
    h_values = [float(box["local"][3]) for box in stable]
    if max(y_values) - min(y_values) > max(12.0, out_height * 0.13):
        return []
    if max(h_values) - min(h_values) > max(9.0, out_height * 0.10):
        return []
    if median_number(h_values) > out_height * 0.30:
        return []
    return stable


def detect_dynamic_blur_boxes(
    video_path: str,
    width: int,
    height: int,
    crop_rect: tuple[float, float, float, float],
    times: tuple[str, ...],
    out_width: int,
    out_height: int,
    label: str,
    duration: float,
    min_y_ratio: float = 0.18,
    max_height_ratio: float = 0.32,
    reject_edge_touch: bool = False,
) -> tuple[list[dict], list[dict]]:
    crop_x, crop_y, crop_w, crop_h = crop_rect
    crop_filter = f"crop=w=iw*{crop_w}:h=ih*{crop_h}:x=iw*{crop_x}:y=ih*{crop_y}"
    boxes: list[dict] = []
    checks: list[dict] = []
    for second in times:
        raw = extract_gray_crop(video_path, crop_filter, second, out_width, out_height)
        score = text_like_score(raw, out_width, out_height) if raw else 0.0
        local = dynamic_text_box(raw, out_width, out_height) if score >= 0.22 else None
        reject_reason = dynamic_subtitle_reject_reason(
            local,
            out_width,
            out_height,
            min_y_ratio,
            max_height_ratio,
            reject_edge_touch,
        ) if local else ""
        checks.append({
            "label": label,
            "time": second,
            "score": round(score, 4),
            "box": list(local) if local else None,
            "accepted": bool(local and not reject_reason),
            "rejectReason": reject_reason,
        })
        if not local:
            continue
        if reject_reason:
            continue
        lx, ly, lw, lh = local
        pad_x = max(6, int(width * 0.012))
        pad_y = max(4, int(height * 0.006))
        x = int(width * crop_x + (lx / out_width) * width * crop_w) - pad_x
        y = int(height * crop_y + (ly / out_height) * height * crop_h) - pad_y
        w = int((lw / out_width) * width * crop_w) + pad_x * 2
        h = int((lh / out_height) * height * crop_h) + pad_y * 2
        core_h = max(1, int((lh / out_height) * height * crop_h))
        t = float(second)
        boxes.append({
            "x": max(0, x),
            "y": max(0, y),
            "w": max(1, min(width - max(0, x), w)),
            "h": max(1, min(height - max(0, y), h)),
            "start": max(0.0, t - 0.35),
            "end": min(duration, t + 1.05),
            "confidence": round(score, 3),
            "text": label,
            "local": [lx, ly, lw, lh],
            "textCoreH": core_h,
            "frameEvidenceTimes": [round(t, 2)],
        })
    stable_boxes = stable_dynamic_subtitle_boxes(boxes, out_height)
    if stable_boxes:
        padded: list[dict] = []
        for box in stable_boxes:
            x1 = int(box["x"]) - max(4, int(width * 0.006))
            y1 = int(box["y"]) - max(6, int(height * 0.006))
            x2 = int(box["x"] + box["w"]) + max(4, int(width * 0.006))
            y2 = int(box["y"] + box["h"]) + max(8, int(height * 0.010))
            if y2 - y1 > max(170, int(height * 0.13)):
                continue
            padded.append({
                "x": max(0, x1),
                "y": max(0, y1),
                "w": max(1, min(width - max(0, x1), x2 - x1)),
                "h": max(1, min(height - max(0, y1), y2 - y1)),
                "start": max(0.0, float(box.get("start", 0))),
                "end": min(duration, float(box.get("end", duration))),
                "confidence": round(float(box.get("confidence", 0)), 3),
                "text": label,
                "textCoreH": int(box.get("textCoreH", 0) or 0),
                "frameEvidenceTimes": [round((float(box.get("start", 0)) + float(box.get("end", 0))) / 2, 2)],
            })
        return merge_timed_blur_regions(padded, max_gap=1.05), checks
    return [], checks


def precise_subtitle_regions_from_checks(results: list[dict], width: int, height: int, duration: float) -> list[dict]:
    if str(os.environ.get("AUTOVOICE_ENABLE_PRECISE_SUBTITLE_FALLBACK") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return []
    candidates: list[dict] = []
    dynamic_crop = (0.10, 0.68, 0.80, 0.17)
    dynamic_out_width = 260
    dynamic_out_height = 96
    for item in results:
        label = str(item.get("label", ""))
        raw_box = item.get("box")
        if not raw_box or len(raw_box) != 4:
            continue
        try:
            bx, by, bw, bh = [float(value) for value in raw_box]
            score = float(item.get("score", 0) or 0)
            second = float(item.get("time", 0) or 0)
        except (TypeError, ValueError):
            continue
        if label == "dynamic_bottom_subtitle":
            if not item.get("accepted"):
                continue
            crop_x, crop_y, crop_w, crop_h = dynamic_crop
            x = int(width * crop_x + (bx / dynamic_out_width) * width * crop_w)
            y = int(height * crop_y + (by / dynamic_out_height) * height * crop_h)
            w = int((bw / dynamic_out_width) * width * crop_w)
            h = int((bh / dynamic_out_height) * height * crop_h)
            confidence = max(0.85, score)
        elif label == "color_overlay_subtitle":
            if not item.get("accepted"):
                continue
            x, y, w, h = int(bx), int(by), int(bw), int(bh)
            confidence = 1.05
        else:
            continue

        center_y = y + h / 2
        if center_y < height * 0.58 or center_y > height * 0.95:
            continue
        if h <= 0 or w <= 0:
            continue
        candidates.append({
            "x": max(0, x),
            "y": max(0, y),
            "w": max(1, min(width - max(0, x), w)),
            "h": max(1, min(height - max(0, y), h)),
            "time": second,
            "confidence": confidence,
            "text": f"{label}_precise",
        })

    if not candidates:
        return []

    clusters: list[list[dict]] = []
    for candidate in sorted(candidates, key=lambda item: item["y"] + item["h"] / 2):
        center_y = candidate["y"] + candidate["h"] / 2
        target = None
        for cluster in clusters:
            ref_y = median_number([item["y"] + item["h"] / 2 for item in cluster])
            if abs(center_y - ref_y) <= max(34, height * 0.035):
                target = cluster
                break
        if target is None:
            clusters.append([candidate])
        else:
            target.append(candidate)

    regions: list[dict] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        x1 = min(int(item["x"]) for item in cluster)
        y1 = min(int(item["y"]) for item in cluster)
        x2 = max(int(item["x"] + item["w"]) for item in cluster)
        y2 = max(int(item["y"] + item["h"]) for item in cluster)
        raw_h = max(1, y2 - y1)
        pad_y = max(8, int(height * 0.008))
        target_h = min(max(raw_h + pad_y * 2, int(height * 0.045)), max(88, int(height * 0.075)))
        center_y = (y1 + y2) / 2
        y = int(round(center_y - target_h / 2))

        raw_w = max(1, x2 - x1)
        pad_x = max(14, int(width * 0.024))
        if raw_w >= width * 0.18:
            target_w = min(int(width * 0.88), raw_w + pad_x * 2)
            center_x = width / 2
        else:
            target_w = min(width, raw_w + pad_x * 2)
            center_x = (x1 + x2) / 2
        x = int(round(center_x - target_w / 2))

        regions.append({
            "x": max(0, x),
            "y": max(0, y),
            "w": max(1, min(width - max(0, x), target_w)),
            "h": max(1, min(height - max(0, y), target_h)),
            "start": 0.0,
            "end": duration,
            "confidence": round(max(float(item.get("confidence", 0)) for item in cluster), 3),
            "text": "fallback_precise_subtitle stable_subtitle_full_duration",
        })

    regions.sort(key=lambda item: (item["y"], -item["w"]))
    return regions[:1]


def detect_lightweight_text_blur_regions(video_path: str, job: Path | None = None) -> list[dict]:
    manifest = (job / "chinese-text-blur-regions.json") if job else None
    duration = probe_video_duration(video_path)
    width, height = probe_video_size(video_path)
    if duration <= 0 or width <= 0 or height <= 0:
        return []

    checks = [
        {
            "label": "top_text_band",
            "crop": "crop=w=iw*0.84:h=ih*0.09:x=iw*0.08:y=ih*0.09",
            "times": ("0.5", "1.8", "3.2"),
            "size": (192, 64),
            "threshold": 0.26,
            "min_hits": 2,
            "region": None,
        },
        {
            "label": "large_intro_center_text",
            "crop": "crop=w=iw*0.88:h=ih*0.34:x=iw*0.06:y=ih*0.18",
            "times": ("0.05", "0.45", "0.85", "1.8", "2.8"),
            "size": (240, 144),
            "threshold": 0.18,
            "min_hits": 1,
            "region": None,
        },
    ]

    regions: list[dict] = []
    results = []
    reject_reasons: list[str] = []
    trim_recommended = detect_intro_title_trim(video_path)
    dynamic_times = []
    dynamic_t = 0.5
    while dynamic_t <= max(0.5, duration - 0.4):
        dynamic_times.append(f"{dynamic_t:.1f}")
        dynamic_t += 1.0
    if trim_recommended > 0:
        dynamic_times.extend(f"{trim_recommended + offset:.1f}" for offset in (0.5, 1.3, 2.1, 2.9, 3.7, 5.0, 6.2, 7.4))
    for check in checks:
        out_width, out_height = check["size"]
        hits, scores = text_region_hits(
            video_path,
            str(check["crop"]),
            check["times"],
            int(out_width),
            int(out_height),
            float(check["threshold"]),
        )
        results.append({
            "label": check["label"],
            "hits": hits,
            "scores": [round(score, 4) for score in scores],
            "threshold": check["threshold"],
            "action": "flag_only" if check["region"] is None else "blur",
        })
        if check["region"] is not None and hits >= int(check["min_hits"]):
            x, y, w, h, start, end = check["region"]
            regions.append(fixed_region(width, height, x, y, w, h, start, end, max(scores or [0.0]), str(check["label"])))

    dynamic_regions, dynamic_checks = detect_dynamic_blur_boxes(
        video_path,
        width,
        height,
        (0.10, 0.68, 0.80, 0.17),
        tuple(dict.fromkeys(dynamic_times)),
        260,
        96,
        "dynamic_bottom_subtitle",
        duration,
        reject_edge_touch=True,
    )
    regions.extend(dynamic_regions)
    results.extend(dynamic_checks)
    fineprint_regions, fineprint_checks = detect_dynamic_blur_boxes(
        video_path,
        width,
        height,
        (0.05, 0.86, 0.90, 0.12),
        tuple(dict.fromkeys(dynamic_times)),
        300,
        80,
        "dynamic_bottom_fineprint",
        duration,
        min_y_ratio=0.0,
        max_height_ratio=0.42,
    )
    regions.extend(fineprint_regions)
    results.extend(fineprint_checks)
    fixed_overlay_regions, fixed_overlay_checks = detect_fixed_overlay_text_regions(video_path, width, height, duration)
    regions.extend(fixed_overlay_regions)
    results.extend(fixed_overlay_checks)
    if not dynamic_regions:
        color_regions, color_checks = detect_color_overlay_subtitle_regions(video_path, width, height, duration, trim_recommended, job)
        regions.extend(color_regions)
        results.extend(color_checks)
    accepted_regions, rejected_regions = validate_subtitle_blur_regions(regions, width, height, duration)
    accepted_regions = extend_stable_subtitle_blur_regions(accepted_regions, width, height, duration)
    if not accepted_regions:
        accepted_regions = precise_subtitle_regions_from_checks(results, width, height, duration)

    if manifest:
        policy = subtitle_policy_summary(accepted_regions, rejected_regions, duration)
        manifest.write_text(json.dumps({
            "ok": True,
            "detector": "ffmpeg_text_like_fixed_bands",
            "video": video_path,
            "duration": round(duration, 3),
            "width": width,
            "height": height,
            "regionCount": len(accepted_regions),
            "trimRecommended": round(trim_recommended, 3),
            "subtitlePolicy": policy,
            "rejectRecommended": bool(reject_reasons) or policy["rejectRecommended"],
            "rejectReasons": reject_reasons + policy["rejectReasons"],
            "subtitlePreflight": {
                "hasChineseSubtitle": bool(accepted_regions),
                "acceptedRegionCount": len(accepted_regions),
                "rejectedRegionCount": len(rejected_regions),
                "rules": "frame-evidence only: blur same-position text repeated across sampled frames; subtitle/footer regions are kept tight",
            },
            "frameEvidenceRules": {
                "fixedOverlayBlurEnabled": str(os.environ.get("AUTOVOICE_ENABLE_FIXED_OVERLAY_BLUR") or "").strip().lower() in {"1", "true", "yes", "on"},
                "preciseFallbackEnabled": str(os.environ.get("AUTOVOICE_ENABLE_PRECISE_SUBTITLE_FALLBACK") or "").strip().lower() in {"1", "true", "yes", "on"},
                "minColorFrameSupport": int(CONFIG.get("subtitle_blur_min_color_frame_support", 3)),
                "dynamicSubtitleRequiresStableFrames": True,
            },
            "checks": results,
            "regions": accepted_regions,
            "rejectedRegions": rejected_regions,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return accepted_regions


def detect_chinese_text_blur_regions(video_path: str, job: Path | None = None) -> list[dict]:
    manifest = (job / "chinese-text-blur-regions.json") if job else None
    if str(os.environ.get("AUTOVOICE_ENABLE_OCR_BLUR") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return detect_lightweight_text_blur_regions(video_path, job)
    try:
        import cv2
        import easyocr
    except Exception as exc:
        regions = detect_chinese_text_blur_regions_external(video_path, job, reason=f"ocr_unavailable: {exc}")
        if regions:
            return regions
        return detect_lightweight_text_blur_regions(video_path, job)

    duration = probe_video_duration(video_path)
    if duration <= 0:
        return []
    sample_count = max(4, min(22, int(duration / 1.25) + 1))
    step = duration / sample_count
    times = [min(duration - 0.1, 0.25 + index * step) for index in range(sample_count)]
    frame_dir = (job / "ocr-frames") if job else (ROOT / ".ocr-frames")
    frame_dir.mkdir(parents=True, exist_ok=True)
    regions: list[dict] = []
    errors: list[str] = []

    try:
        reader = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
    except Exception as exc:
        regions = detect_chinese_text_blur_regions_external(video_path, job, reason=f"ocr_reader_failed: {exc}")
        if regions:
            return regions
        return detect_lightweight_text_blur_regions(video_path, job)

    for index, second in enumerate(times):
        frame_path = frame_dir / f"ocr-{index:03d}.jpg"
        if not extract_ocr_frame(video_path, frame_path, second):
            continue
        image = cv2.imread(str(frame_path))
        if image is None:
            continue
        height, width = image.shape[:2]
        frame_boxes: list[dict] = []
        try:
            detections = reader.readtext(str(frame_path), detail=1, paragraph=False)
        except Exception as exc:
            errors.append(str(exc))
            continue
        for points, text, confidence in detections:
            if float(confidence or 0) < 0.42 or not is_chinese_ocr_text(text, float(confidence or 0)):
                continue
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
            pad = max(8, min(18, int(height * 0.012)))
            x1 = max(0, int(min(xs) - pad))
            y1 = max(0, int(min(ys) - pad))
            x2 = min(width, int(max(xs) + pad))
            y2 = min(height, int(max(ys) + pad))
            if x2 <= x1 or y2 <= y1:
                continue
            area_ratio = ((x2 - x1) * (y2 - y1)) / max(1, width * height)
            if area_ratio > 0.22:
                continue
            frame_boxes.append({
                "x": x1,
                "y": y1,
                "w": x2 - x1,
                "h": y2 - y1,
                "start": max(0.0, second - 0.45),
                "end": min(duration, second + 0.85),
                "confidence": round(float(confidence or 0), 3),
                "text": str(text or "")[:80],
            })
        regions.extend(merge_same_frame_boxes(frame_boxes))

    width, height = probe_video_size(video_path)
    stable_regions = stable_repeated_text_blur_regions(regions, width, height, duration, sample_count)
    stable_regions, stable_rejected = validate_subtitle_blur_regions(stable_regions, width, height, duration)
    merged = merge_timed_blur_regions(regions)
    merged, rejected = validate_subtitle_blur_regions(merged, width, height, duration)
    merged = extend_stable_subtitle_blur_regions(merged, width, height, duration)
    if stable_regions:
        merged = [
            *stable_regions,
            *[item for item in merged if not any(overlap_ratio(item, stable) >= 0.35 for stable in stable_regions)],
        ]
        rejected.extend(stable_rejected)
    merged = sorted(merged, key=lambda item: (item["start"], item["y"], item["x"]))
    if len(merged) > 32:
        merged = sorted(merged, key=lambda item: (float(item.get("confidence", 0)), item["w"] * item["h"]), reverse=True)[:32]
        merged = sorted(merged, key=lambda item: (item["start"], item["y"], item["x"]))
    if manifest:
        policy = subtitle_policy_summary(merged, rejected, duration)
        manifest.write_text(json.dumps({
            "ok": True,
            "detector": "easyocr_ch_sim",
            "video": video_path,
            "duration": round(duration, 3),
            "width": width,
            "height": height,
            "sampleCount": sample_count,
            "regionCount": len(merged),
            "subtitlePolicy": policy,
            "rejectRecommended": policy["rejectRecommended"],
            "rejectReasons": policy["rejectReasons"],
            "subtitlePreflight": {
                "hasChineseSubtitle": bool(merged),
                "acceptedRegionCount": len(merged),
                "rejectedRegionCount": len(rejected),
                "rules": "repeated text across sampled frames; stable same-position Chinese text may blur at any screen position",
            },
            "errors": errors[:5],
            "regions": merged,
            "rejectedRegions": rejected,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    if not merged:
        return detect_lightweight_text_blur_regions(video_path, job)
    return merged


def should_blur_top_text(video_path: str) -> bool:
    return bool(detect_chinese_text_blur_regions(video_path))


def blur_region_arg(region: dict) -> str:
    return ":".join([
        str(int(region["x"])),
        str(int(region["y"])),
        str(int(region["w"])),
        str(int(region["h"])),
        f"{float(region['start']):.3f}",
        f"{float(region['end']):.3f}",
    ])


def timed_blur_filter_complex(regions: list[dict]) -> str:
    current = "0:v"
    filters = []
    for index, region in enumerate(regions):
        x = int(region["x"])
        y = int(region["y"])
        w = int(region["w"])
        h = int(region["h"])
        start = float(region["start"])
        end = float(region["end"])
        radius = max(2, min(10, int(max(2, min(w, h)) / 4)))
        out_label = "v" if index == len(regions) - 1 else f"vb{index}"
        filters.append(
            f"[{current}]split=2[vbase{index}][vblur{index}];"
            f"[vblur{index}]crop=w={w}:h={h}:x={x}:y={y},boxblur={radius}:1[vpatch{index}];"
            f"[vbase{index}][vpatch{index}]overlay=x={x}:y={y}:enable='between(t,{start:.3f},{end:.3f})'[{out_label}]"
        )
        current = out_label
    return ";".join(filters)


def intro_title_score(raw: bytes, width: int, height: int) -> float:
    if len(raw) < width * height * 3:
        return 0.0
    colorful = 0
    strong_edges = 0
    last_bright = None
    pixels = width * height
    for idx in range(0, pixels * 3, 3):
        r = raw[idx]
        g = raw[idx + 1]
        b = raw[idx + 2]
        high = max(r, g, b)
        low = min(r, g, b)
        is_yellow = r >= 160 and g >= 120 and b <= 120 and high - low >= 70
        is_red = r >= 160 and g <= 120 and b <= 120 and high - low >= 70
        if is_yellow or is_red:
            colorful += 1
        bright = (r + g + b) // 3
        if last_bright is not None and abs(bright - last_bright) >= 70:
            strong_edges += 1
        last_bright = bright
    return (colorful / max(1, pixels)) + (strong_edges / max(1, pixels)) * 0.25


def detect_intro_title_trim(video_path: str) -> float:
    width = 240
    height = 120
    hit_seconds: list[float] = []
    for second in ("0.05", "0.45", "0.85", "1.25", "1.75", "2.35", "3.00", "3.65"):
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            second,
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-vf",
            "crop=w=iw*0.82:h=ih*0.22:x=iw*0.09:y=ih*0.26,scale=240:120,format=rgb24",
            "-f",
            "rawvideo",
            "-",
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0 or not proc.stdout:
            continue
        if intro_title_score(proc.stdout, width, height) >= 0.045:
            hit_seconds.append(float(second))
    if not hit_seconds:
        return 0.0
    return min(4.25, max(1.15, max(hit_seconds) + 0.55))


def read_audio_mode(job: Path) -> dict:
    mode_path = job / "audio-mode.json"
    if not mode_path.exists():
        return {}
    try:
        return json.loads(mode_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def write_audio_mode(job: Path, data: dict) -> None:
    (job / "audio-mode.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare_vocal_separated_bed(job: Path, source_video: str, trim_start: float) -> Path | None:
    if not CONFIG.get("vocal_separation_enabled", False):
        return None
    if shutil.which("ffmpeg") is None:
        return None
    bed_dir = job / "separated-bed"
    audio_path = bed_dir / "source-for-separation.wav"
    bed_path = bed_dir / "htdemucs" / audio_path.stem / "no_vocals.wav"
    if bed_path.exists() and bed_path.stat().st_size > 0:
        return bed_path

    bed_dir.mkdir(parents=True, exist_ok=True)
    extract_cmd = ["ffmpeg", "-hide_banner", "-y"]
    if trim_start > 0:
        extract_cmd += ["-ss", f"{trim_start:.3f}"]
    extract_cmd += [
        "-i",
        source_video,
        "-vn",
        "-ac",
        "2",
        "-ar",
        "44100",
        str(audio_path),
    ]
    extract = subprocess.run(extract_cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if extract.returncode != 0 or not audio_path.exists():
        (job / "vocal-separation-log.txt").write_text(extract.stdout + "\n\n" + extract.stderr, encoding="utf-8")
        return None

    cmd = [
        sys.executable,
        "-m",
        "demucs",
        "--two-stems=vocals",
        "-n",
        str(CONFIG.get("demucs_model", "htdemucs")),
        "--out",
        str(bed_dir),
        str(audio_path),
    ]
    print("AUTOVOICE_PROGRESS audio: tÃ¡ch vocal, giá»¯ nháº¡c/tiáº¿ng sáº£n pháº©m lÃ m ná»n", flush=True)
    proc = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (job / "vocal-separation-log.txt").write_text(proc.stdout + "\n\n" + proc.stderr, encoding="utf-8")
    if proc.returncode != 0 or not bed_path.exists() or bed_path.stat().st_size == 0:
        return None
    return bed_path


def audio_mean_volume_db(path: Path | str) -> float | None:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            os.devnull,
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", proc.stdout + "\n" + proc.stderr)
    return float(match.group(1)) if match else None


def is_audible_bed(path: Path | str) -> bool:
    mean_volume = audio_mean_volume_db(path)
    return mean_volume is not None and mean_volume >= float(CONFIG.get("separated_bed_min_mean_db", -45.0))


def dynamic_separated_bed_volume(path: Path | str) -> tuple[float, float | None]:
    """Pick a bed level that stays behind Vietnamese voice instead of using one fixed gain."""
    mean_volume = audio_mean_volume_db(path)
    fallback = float(CONFIG.get("separated_bed_volume", 0.35))
    if mean_volume is None:
        return fallback, None
    target_db = float(CONFIG.get("separated_bed_target_mean_db", -21.0))
    min_volume = float(CONFIG.get("separated_bed_min_volume", 0.25))
    max_volume = float(CONFIG.get("separated_bed_max_volume", 0.45))
    volume = 10 ** ((target_db - mean_volume) / 20)
    volume = max(min_volume, min(max_volume, volume))
    return round(volume, 3), mean_volume


def should_try_vocal_separation(job: Path, audio_mode: dict) -> bool:
    mode = str(audio_mode.get("audio_mode") or "").strip().lower()
    if audio_mode.get("needs_vocal_separation") or mode == "chinese_voice":
        return True
    transcript = (job / "transcript-zh.txt").read_text(encoding="utf-8", errors="replace") if (job / "transcript-zh.txt").exists() else ""
    return has_meaningful_chinese_voice_transcript(transcript)


def run_renderer(job: Path, original_volume: float, voice: str, rate: str, voice_volume: float, blur_text: bool = False):
    source_path = job / "source-path.txt"
    script_path = job / "voice-script.txt"
    if not source_path.exists() or not script_path.exists() or not script_path.read_text(encoding="utf-8").strip():
        raise HTTPException(400, "Missing source video or voice script")
    out = autovoice_output_path(job)
    out.parent.mkdir(parents=True, exist_ok=True)
    renderer = Path(CONFIG["skill_dir"]) / "scripts" / "render_autovoice.py"
    print("AUTOVOICE_PROGRESS render: táº¡o voice vÃ  ghÃ©p vÃ o video", flush=True)
    source_video = source_path.read_text(encoding="utf-8").strip()
    trim_start = detect_intro_title_trim(source_video)
    if trim_start > 0:
        print(f"AUTOVOICE_PROGRESS trim: cut {trim_start:.2f}s opening Chinese title", flush=True)
    audio_mode = read_audio_mode(job)
    separated_bed = None
    renderer_original_volume = original_volume
    if should_try_vocal_separation(job, audio_mode):
        separated_bed = prepare_vocal_separated_bed(job, source_video, trim_start)
        if separated_bed and is_audible_bed(separated_bed):
            separated_bed_volume, separated_bed_mean = dynamic_separated_bed_volume(separated_bed)
            renderer_original_volume = 0
            audio_mode["needs_vocal_separation"] = True
            audio_mode["vocal_separation_status"] = "done"
            audio_mode["separated_bed"] = str(separated_bed)
            audio_mode["render_original_volume"] = 0
            audio_mode["separated_bed_volume"] = separated_bed_volume
            audio_mode["separated_bed_mean_volume_db"] = separated_bed_mean
            write_audio_mode(job, audio_mode)
            print(f"AUTOVOICE_PROGRESS audio: Ä‘Ã£ bá» voice gá»‘c, giá»¯ ná»n no-vocals volume={separated_bed_volume}", flush=True)
        else:
            separated_bed = None
            renderer_original_volume = 0
            audio_mode["vocal_separation_status"] = "no_audible_bed_mute_original"
            audio_mode["render_original_volume"] = 0
            write_audio_mode(job, audio_mode)
            print("AUTOVOICE_PROGRESS audio: khÃ´ng cÃ³ ná»n no-vocals Ä‘á»§ nghe, táº¯t audio gá»‘c Ä‘á»ƒ khÃ´ng lá»t voice Trung", flush=True)
    blur_regions: list[dict] = []
    if blur_text:
        print("AUTOVOICE_PROGRESS blur: kiểm tra chữ bằng ffmpeg nhẹ", flush=True)
        blur_regions = detect_chinese_text_blur_regions(source_video, job)
        enforce_text_overlay_policy(job, blur_regions)
        print(
            f"AUTOVOICE_PROGRESS blur: {'thấy ' + str(len(blur_regions)) + ' vùng chữ TQ, blur theo thời điểm' if blur_regions else 'không thấy chữ TQ, bỏ qua blur'}",
            flush=True,
        )
    cmd = [
            sys.executable,
            str(renderer),
            "--video",
            source_video,
            "--script",
            str(script_path),
            "--output",
            str(out),
            "--voice",
            voice,
            "--rate",
            rate,
            "--original-volume",
            str(renderer_original_volume),
            "--delay-ms",
            str(CONFIG.get("default_delay_ms", 180)),
            "--voice-volume",
            str(voice_volume),
    ]
    if separated_bed:
        cmd += [
            "--music",
            str(separated_bed),
            "--music-volume",
            str(audio_mode.get("separated_bed_volume", CONFIG.get("separated_bed_volume", 0.35))),
        ]
    if trim_start > 0:
        cmd += ["--trim-start", f"{trim_start:.3f}"]
    for region in blur_regions:
        cmd += ["--blur-region", blur_region_arg(region)]
    proc = subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    (job / "render-log.txt").write_text(proc.stdout + "\n\n" + proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        raise HTTPException(500, proc.stderr[-2000:])
    return out


def autovoice_output_path(job: Path) -> Path:
    output_dir = Path(CONFIG["output_dir"])
    title = ""
    content_path = job / "content-fb.txt"
    if content_path.exists():
        title = content_path.read_text(encoding="utf-8", errors="replace")
    if not title and (job / "audio-mode.json").exists():
        try:
            data = json.loads((job / "audio-mode.json").read_text(encoding="utf-8", errors="replace"))
            title = str(data.get("product_summary") or "")
        except Exception:
            title = ""
    stem = product_file_stem(title) or job.name
    target = output_dir / f"{stem}-vi-voice.mp4"
    if not target.exists():
        return target
    for index in range(2, 1000):
        candidate = output_dir / f"{stem}-{index}-vi-voice.mp4"
        if not candidate.exists():
            return candidate
    return output_dir / f"{stem}-{int(time.time())}-vi-voice.mp4"


def product_file_stem(text: str) -> str:
    value = re.sub(r"#\S+", " ", str(text or ""))
    value = re.split(r"(?<=[.!?])\s+", value)[0]
    value = unicodedata.normalize("NFD", value)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = value.lower().replace("Ä‘", "d")
    words = re.findall(r"[a-z0-9]+", value)
    stop = {
        "mot", "mon", "do", "nho", "gon", "dung", "hang", "ngay", "rat", "tien",
        "nay", "cho", "hon", "moi", "ngay", "ho", "tro", "de", "va", "la", "cai"
    }
    picked = [word for word in words if word not in stop][:8]
    return "-".join(picked)[:80].strip("-")


def fold_vietnamese_text(text: str) -> str:
    value = repair_mojibake_text(str(text or ""))
    value = unicodedata.normalize("NFD", value)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = value.replace("Ä‘", "d").replace("Ä", "D")
    return " ".join(value.lower().split())


def read_audio_volume(job: Path, default: float = 1.0) -> float:
    mode = job / "audio-mode.json"
    if not mode.exists():
        return default
    try:
        data = json.loads(mode.read_text(encoding="utf-8"))
        return float(data.get("original_volume", default))
    except Exception:
        return default


def choose_voice_for_job(job: Path, tone: str = "", voice: str = "auto") -> str:
    requested = str(voice or "auto").strip()
    tts_provider = str(os.environ.get("AUTOVOICE_TTS_PROVIDER") or os.environ.get("TTS_PROVIDER") or "").strip().lower()
    if tts_provider in {"vivibe", "local_f5"} and requested.lower() in {"auto", "random", "none", "tá»± Ä‘á»™ng", "tu dong"}:
        return "auto"
    if requested and requested.lower() not in ["auto", "tá»± Ä‘á»™ng", "tu dong"]:
        return requested
    configured = str(CONFIG.get("default_voice", "male") or "male")
    if configured.lower() in ["male", "female"]:
        return configured
    if not configured.strip():
        return "male"
    text = " ".join([
        tone or "",
        (job / "source-path.txt").read_text(encoding="utf-8", errors="replace") if (job / "source-path.txt").exists() else "",
        (job / "analysis-brief.md").read_text(encoding="utf-8", errors="replace") if (job / "analysis-brief.md").exists() else "",
        (job / "audio-mode.json").read_text(encoding="utf-8", errors="replace") if (job / "audio-mode.json").exists() else "",
        (job / "voice-script.txt").read_text(encoding="utf-8", errors="replace") if (job / "voice-script.txt").exists() else "",
    ]).lower()
    if any(token in text for token in ["giá»ng nam", "nam ", "anh em", "Ä‘i xe", "ngoÃ i trá»i", "kÃ­nh", "dá»¥ng cá»¥", "sá»­a", "dao", "cáº¯t cá»"]):
        return "male"
    if any(token in text for token in ["giá»ng ná»¯", "voice female", "female voice"]):
        return "female"
    return configured or "male"


def sanitize_review_script_text(text: str) -> str:
    cleaned = repair_mojibake_text(str(text or ""))
    replacements = [
        (r"\s*(?:nha|ha|nè|nhé)\s*([.!?,;:]|$)", r"\1"),
        (r"\s+luôn\s+nha\s*([.!?,;:]|$)", r"\1"),
        (r"\s+(?:đó|đấy)\s+nha\s*([.!?,;:]|$)", r"\1"),
        (r"\brất\s+đáng\s+mua\b", "dùng khá hợp"),
        (r"\bcái\s+này\s+hay\b", "điểm này đáng chú ý"),
        (r"\bquá\s+là\s+tiện\b", "gọn hơn trong lúc dùng"),
        (r"\bsiêu\s+tiện\s+lợi\b", "tiện trong việc dùng hằng ngày"),
        (r"\bnhỏ\s+mà\s+có\s+võ\b", "kích thước gọn nhưng làm được đúng việc"),
        (r"\bai\s+cũng\s+nên\s+có\b", "hợp với những góc dùng thường xuyên"),
        (r"\bchân\s+ái\b", "món dùng hợp thói quen"),
    ]
    for pattern, replacement in replacements:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"([,.!?;:]){2,}", r"\1", cleaned)
    return cleaned.strip()


def validate_vietnamese_script(job: Path):
    script_path = job / "voice-script.txt"
    text = script_path.read_text(encoding="utf-8", errors="replace").strip() if script_path.exists() else ""
    cleaned = sanitize_review_script_text(text)
    if cleaned != text:
        script_path.write_text(cleaned, encoding="utf-8")
        text = cleaned
    if not text:
        raise HTTPException(500, "voice-script.txt is empty.")
    if looks_broken_vietnamese_text(text):
        raise HTTPException(500, "Generated script has broken Vietnamese encoding. Stop before render.")
    thai_chars = sum(1 for ch in text if "\u0e00" <= ch <= "\u0e7f")
    if thai_chars:
        raise HTTPException(500, "Generated script contains Thai characters. Stop before render.")
    mark_count = vietnamese_mark_count(text)
    word_count = len([word for word in text.replace("\n", " ").split(" ") if word.strip()])
    if word_count >= 12 and mark_count < 8:
        raise HTTPException(500, "Generated script has too few Vietnamese diacritics. Stop before render.")


def vietnamese_mark_count(value: str) -> int:
    text = str(value or "")
    decomposed = unicodedata.normalize("NFD", text)
    combining_marks = sum(1 for ch in decomposed if unicodedata.category(ch) == "Mn")
    vietnamese_letters = sum(1 for ch in text.lower() if ch in "ăâđêôơư")
    return combining_marks + vietnamese_letters


def normalize_audio_mode_for_product_sound(job: Path):
    mode_path = job / "audio-mode.json"
    if not mode_path.exists():
        return
    try:
        data = json.loads(mode_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return
    context = " ".join([
        (job / "analysis-brief.md").read_text(encoding="utf-8", errors="replace") if (job / "analysis-brief.md").exists() else "",
        (job / "voice-script.txt").read_text(encoding="utf-8", errors="replace") if (job / "voice-script.txt").exists() else "",
        str(data.get("product_summary") or ""),
    ]).lower()
    folded_context = fold_vietnamese_text(context)
    product_sound_tokens = [
        "cat", "thai", "chat", "keo", "got", "xay", "ep", "motor", "nuoc", "rua",
        "xit", "bom", "click", "bam", "do nuoc", "rot", "phun", "va cham",
        "kim loai", "lo xo", "scrape", "cut", "slice", "water", "pour"
    ]
    has_product_sound = any(
        (token in folded_context if " " in token else re.search(rf"\b{re.escape(token)}\b", folded_context))
        for token in product_sound_tokens
    )
    audio_mode = str(data.get("audio_mode") or "").strip().lower()
    has_mixed_bed = has_product_sound or audio_mode in {"product_sound", "music_only"}
    transcript_text = (job / "transcript-zh.txt").read_text(encoding="utf-8", errors="replace") if (job / "transcript-zh.txt").exists() else ""
    has_chinese_voice = has_meaningful_chinese_voice_transcript(transcript_text)
    if has_chinese_voice:
        if has_mixed_bed:
            data["audio_mode"] = "product_sound"
            data["original_volume"] = float(CONFIG.get("original_volume_when_mixed_voice_product_sound", 0))
            data["needs_vocal_separation"] = True
            data["audio_note"] = "Chinese source voice is mixed with music/product sound: target is to remove only the source voice stem and keep the music/product bed. Vocal separation is required; if it is unavailable or no clean audible no-vocals bed is produced, mute original audio to avoid Chinese/source voice leaking under Vietnamese voice."
            data["target_audio_rule"] = "separate_source_voice_keep_music_product_bed"
        else:
            data["audio_mode"] = "chinese_voice"
            data["original_volume"] = float(CONFIG.get("original_volume_when_chinese_voice", 0))
            data["audio_note"] = "Chinese source voice detected without useful music/product bed: mute original track and use source meaning for Vietnamese script."
        mode_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    if audio_mode == "product_sound" and not has_product_sound:
        data["audio_mode"] = "music_only"
        data["original_volume"] = float(CONFIG.get("original_volume_when_music_only", 0.3))
        data["audio_note"] = "No Chinese/source voice detected, but no strong product-operation sound was confirmed. Treat original audio as music/SFX bed at 30% instead of product sound at 70%."
        mode_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    if has_product_sound and float(data.get("original_volume") or 0) <= 0:
        data["audio_mode"] = "product_sound"
        data["original_volume"] = float(CONFIG.get("original_volume_when_product_sound", 0.7))
        data["audio_note"] = "No Chinese source voice detected: keep product-operation sound as proof at a clear bed under Vietnamese voice."
        mode_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def has_meaningful_chinese_voice_transcript(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    cleaned = re.sub(r"language\s+\w+\s+\d+(?:\.\d+)?", " ", value, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[[0-9:.]+-[0-9:.]+\]", " ", cleaned)
    cleaned = " ".join(cleaned.split())
    cjk_count = sum(1 for ch in cleaned if "\u4e00" <= ch <= "\u9fff")
    if cjk_count < 6:
        return False
    credit_markers = ["å­—å¹•", "å‰ªè¾‘", "å…³æ³¨", "ç‚¹èµž", "è¯„è®º", "è½¬å‘", "by", "douyin", "æŠ–éŸ³"]
    if len(cleaned) <= 24 and any(marker in cleaned.lower() for marker in credit_markers):
        return False
    return True


def stable_fallback_choice(options: list[str], key: str) -> str:
    if not options:
        return ""
    digest = hashlib.sha1(str(key or "").encode("utf-8", errors="replace")).hexdigest()
    return options[int(digest[:8], 16) % len(options)]


def fallback_script_for_job(job: Path, tone: str = "", reason: str = ""):
    allow_generic_fallback = str(os.environ.get("AUTOVOICE_ALLOW_GENERIC_FALLBACK") or CONFIG.get("allow_generic_fallback", "0")).strip().lower()
    if allow_generic_fallback not in {"1", "true", "yes", "on"}:
        raise HTTPException(
            500,
            "Product-specific voice script was not generated. Stop render instead of using a generic fallback script."
        )
    source = (job / "source-path.txt").read_text(encoding="utf-8", errors="replace") if (job / "source-path.txt").exists() else ""
    transcript = (job / "transcript-zh.txt").read_text(encoding="utf-8", errors="replace") if (job / "transcript-zh.txt").exists() else ""
    brief = (job / "analysis-brief.md").read_text(encoding="utf-8", errors="replace") if (job / "analysis-brief.md").exists() else ""
    meta = json.loads((job / "metadata.json").read_text(encoding="utf-8")) if (job / "metadata.json").exists() else {}
    duration = core.duration_from_probe(meta) if meta else 0
    context = f"{source}\n{brief}\n{transcript}"
    fallback_key = f"{job.name}|{source}|{transcript[:240]}|{brief[:240]}"
    folded = fold_vietnamese_text(context)

    if any(token in context for token in ["绞肉", "料理机", "搅拌", "xay", "băm", "bam"]):
        summary = "Máy xay/băm nhỏ gọn, hỗ trợ chuẩn bị thịt, rau củ và gia vị nhanh hơn."
        script = stable_fallback_choice([
            "Căn bếp nhỏ mà có chiếc máy xay kiểu này thì chuẩn bị đồ ăn nhàn hơn hẳn. Thịt, rau củ hay gia vị cho vào là xử lý rất nhanh, không phải băm tay lâu. Cối gọn, dễ rửa và cất cũng không tốn chỗ, hợp với những bữa cơm cần làm nhanh mỗi ngày.",
            "Nhìn thao tác xay là thấy món này hợp với nhà hay nấu ăn. Nguyên liệu cho vào cối, bấm một nhịp là nhỏ đều hơn nhiều so với băm tay. Phần thân gọn nên dùng xong rửa và cất cũng nhẹ việc.",
            "Món này giải quyết đúng đoạn mất thời gian nhất khi sơ chế. Thịt, rau củ hoặc gia vị cho vào là xử lý nhanh, tay không phải cầm dao băm lâu. Với bữa cơm thường ngày, kiểu máy nhỏ gọn như vậy khá thực tế.",
        ], fallback_key)
        audio_mode = "product_sound"
        original_volume = float(CONFIG.get("original_volume_when_product_sound", 0.7))
    elif any(token in context for token in ["收纳", "置物", "架", "柜", "调料", "storage", "shelf", "kệ", "ke ", "gọn", "gon"]):
        summary = "Kệ/đồ sắp xếp giúp góc nhà gọn hơn và dễ lấy đồ khi dùng."
        script = stable_fallback_choice([
            "Góc nhà hay góc bếp chỉ cần thêm một món sắp xếp đúng chỗ là nhìn gọn hơn hẳn. Đồ nhỏ được gom lại, lúc cần cũng dễ lấy hơn, không phải lục tung cả khu vực. Kiểu thiết kế này hợp để dùng hằng ngày vì gọn, thực tế và tiết kiệm diện tích.",
            "Những món đồ nhỏ mà để rải rác thì góc nào cũng nhanh rối. Kiểu kệ này gom chúng về một chỗ, nhìn mặt bàn thoáng hơn và lúc dùng cũng dễ lấy. Điểm đáng chú ý là nó tận dụng được khoảng trống thay vì chiếm thêm diện tích.",
            "Mình thích kiểu sắp xếp như thế này vì nó làm góc dùng thường ngày bớt lộn xộn rất nhanh. Đồ được chia tầng rõ hơn, nhìn thấy món cần lấy ngay, không phải dọn đi dọn lại nhiều lần.",
            "Với mấy góc nhỏ trong bếp hoặc trong nhà, chỉ cần thêm một kệ đúng kích thước là khác hẳn. Chai lọ, hộp nhỏ hay đồ dùng thường xuyên có chỗ riêng, nhìn gọn mà vẫn tiện tay khi cần.",
        ], fallback_key)
        audio_mode = "product_sound"
        original_volume = float(CONFIG.get("original_volume_when_product_sound", 0.7))
    elif any(token in context for token in ["厨房", "燃气灶", "锅", "削皮", "切菜", "bep", "nau", "dao", "chao", "noi"]):
        summary = "Dụng cụ/bếp gia đình hỗ trợ thao tác nấu nướng gọn và nhanh hơn."
        script = stable_fallback_choice([
            "Nấu nướng mà có đúng món hỗ trợ thì thao tác nhẹ hơn nhiều. Thiết kế gọn, dùng vào việc hằng ngày trong bếp rất thực tế, từ chuẩn bị nguyên liệu đến dọn góc nấu đều đỡ mất thời gian. Nhà nào hay vào bếp sẽ thấy mấy chi tiết nhỏ như vậy khá đáng dùng.",
            "Trong bếp, mấy món nhỏ như thế này thường giúp tiết kiệm thời gian rõ nhất. Cầm lên dùng nhanh, thao tác gọn hơn và sau đó dọn cũng đỡ lỉnh kỉnh. Nó không cầu kỳ, nhưng đúng kiểu dùng được mỗi ngày.",
            "Nhìn cách thao tác là thấy món này sinh ra cho căn bếp bận rộn. Việc nhỏ được xử lý nhanh hơn, tay đỡ vướng và khu vực nấu cũng gọn hơn sau khi dùng. Những chi tiết như vậy làm bữa cơm nhẹ việc hơn.",
        ], fallback_key)
        audio_mode = "product_sound"
        original_volume = float(CONFIG.get("original_volume_when_product_sound", 0.7))
    elif any(token in context for token in ["枕", "棉", "床", "凉席", "家纺", "pillow", "cotton", "goi", "giuong", "chan", "nem"]):
        summary = "Đồ dùng phòng ngủ/gia đình giúp không gian gọn, sạch và dễ chịu hơn."
        script = stable_fallback_choice([
            "Phòng ngủ muốn nhìn sạch và dễ chịu thì mấy món nhỏ như thế này lại rất hữu ích. Chất liệu và kiểu dáng hướng đến dùng hằng ngày, dễ sắp xếp, dễ thay và không làm không gian bị rối. Nhà nào thích gọn gàng sẽ thấy khá tiện.",
            "Không gian nghỉ ngơi chỉ cần đổi vài món đúng tông là cảm giác đã dễ chịu hơn. Kiểu đồ dùng này nhìn gọn, dùng hằng ngày ổn và không làm phòng bị nặng mắt. Ai thích phòng sạch, nhẹ nhàng sẽ hợp.",
            "Điểm hay của món này là nó làm phòng nhìn chỉn chu hơn mà không cần bày biện nhiều. Dùng xong dễ gấp, dễ cất, phù hợp với những căn phòng cần giữ sự thoáng và sạch.",
        ], fallback_key)
        audio_mode = "music_only"
        original_volume = float(CONFIG.get("original_volume_when_music_only", 0.3))
    elif any(token in context for token in ["发电", "电", "灯", "充电", "solar", "generator", "đèn", "den", "sạc", "sac"]):
        summary = "Đồ điện/đèn tiện ích hỗ trợ sinh hoạt hằng ngày gọn hơn."
        script = stable_fallback_choice([
            "Mấy món đồ điện nhỏ như thế này dùng trong nhà khá tiện. Thiết kế gọn, thao tác đơn giản, khi cần chiếu sáng, sạc hoặc hỗ trợ sinh hoạt thì lấy ra dùng rất nhanh. Điểm hay là không chiếm nhiều chỗ nhưng vẫn giải quyết được nhu cầu hằng ngày.",
            "Đồ điện nhỏ mà dùng đúng lúc thì rất hữu ích. Món này nhìn gọn, thao tác không rườm rà, cần sáng hơn, mát hơn hoặc hỗ trợ sinh hoạt là có thể dùng ngay. Để trong nhà cũng không tốn nhiều diện tích.",
            "Với những góc hay cần thêm ánh sáng hoặc tiện ích nhanh, kiểu thiết kế nhỏ gọn này khá hợp. Không phải lắp đặt phức tạp, cầm lên dùng được liền và khi xong thì cất đi rất nhẹ.",
        ], fallback_key)
        audio_mode = "music_only"
        original_volume = float(CONFIG.get("original_volume_when_music_only", 0.3))
    elif any(token in context for token in ["养鸡", "农村", "农", "farm", "chicken", "nong", "nuoi"]):
        summary = "Dụng cụ/trang bị nông nghiệp hỗ trợ chăm sóc và sắp xếp khu nuôi trồng gọn hơn."
        script = stable_fallback_choice([
            "Làm vườn hay chăn nuôi mà có đúng dụng cụ thì công việc nhẹ hơn nhiều. Thiết kế trong video tập trung vào việc sắp xếp và vận hành gọn hơn, giảm thao tác lặp lại mỗi ngày. Với khu nuôi trồng nhỏ, mấy món hỗ trợ như vậy khá thực tế.",
            "Những việc ngoài vườn thường lặp lại mỗi ngày, nên có một món hỗ trợ đúng chỗ sẽ đỡ công hơn. Cách dùng trong video khá thẳng, tập trung vào sự gọn và dễ thao tác.",
            "Món này hợp với kiểu làm vườn, chăn nuôi hoặc khu sinh hoạt nhỏ cần sắp xếp lại cho tiện. Không phải đồ cầu kỳ, nhưng giúp thao tác đều đặn hằng ngày nhẹ hơn.",
        ], fallback_key)
        audio_mode = "chinese_voice"
        original_volume = 0
    elif any(token in context for token in ["眼镜", "glasses", "kính", "kinh", "骑行"]):
        summary = "Kính đi đường/chống gió giúp che bụi và giữ mắt dễ chịu hơn."
        script = stable_fallback_choice([
            "Đi ngoài đường mà gió bụi tạt vào mắt thì một chiếc kính gọn như thế này khá đáng dùng. Form ôm mặt, nhìn vẫn gọn và dễ phối khi đi xe hằng ngày. Đi phố, đi làm hay chạy xe ngoài trời đều tiện hơn.",
            "Chiếc kính này hợp với người hay đi xe hoặc ra ngoài nhiều. Dáng ôm vừa mặt, che gió bụi tốt hơn kính thường mà nhìn vẫn gọn. Dùng khi đi làm, đi phố hay chạy xe đều ổn.",
            "Mình để ý phần form kính khá gọn, không bị cồng kềnh khi đeo ngoài trời. Nó giúp mắt dễ chịu hơn lúc gặp gió, bụi hoặc nắng nhẹ, nhất là khi phải di chuyển nhiều.",
        ], fallback_key)
        audio_mode = "product_sound"
        original_volume = float(CONFIG.get("original_volume_when_product_sound", 0.7))
    else:
        summary = "Sản phẩm tiện ích trong video hỗ trợ sinh hoạt hằng ngày gọn hơn."
        script = stable_fallback_choice([
            "Đây là một món đồ nhỏ nhưng dùng trong sinh hoạt hằng ngày khá tiện. Thiết kế gọn, thao tác đơn giản, phù hợp để đặt trong nhà và dùng khi cần. Nếu đang muốn bớt mất thời gian cho mấy việc lặp lại mỗi ngày thì món này đáng để tham khảo.",
            "Món này không quá phức tạp, nhưng nhìn cách dùng thì thấy giải quyết được một việc nhỏ rất rõ. Cầm lên thao tác nhanh, dùng xong cất gọn, hợp với những ai thích đồ tiện mà không chiếm chỗ.",
            "Có những món nhìn đơn giản nhưng đặt đúng chỗ lại dùng thường xuyên. Thiết kế trong video hướng đến thao tác nhanh, dễ hiểu và phục vụ việc sinh hoạt mỗi ngày.",
            "Điểm mình chú ý là món này làm một việc rất cụ thể, không cần giải thích nhiều vẫn hiểu cách dùng. Kiểu đồ nhỏ như vậy hợp để đặt trong nhà, lúc cần lấy ra xử lý nhanh.",
        ], fallback_key)
        audio_mode = "music_only"
        original_volume = float(CONFIG.get("original_volume_when_music_only", 0.3))

    if duration and duration < 13:
        script = script.split(". ")[0] + ". " + script.split(". ")[1] if ". " in script else script
    (job / "voice-script.txt").write_text(script.strip(), encoding="utf-8")
    (job / "audio-mode.json").write_text(json.dumps({
        "audio_mode": audio_mode,
        "original_volume": original_volume,
        "product_summary": summary,
        "fallback": True,
        "fallback_reason": reason[-800:] if reason else "",
        "tone": tone,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (job / "fallback-script.txt").write_text(script.strip(), encoding="utf-8")
    return {
        "product_summary": summary,
        "audio_mode": audio_mode,
        "original_volume": original_volume,
        "script": script,
    }


def interaction_output_path(job: Path) -> Path:
    output_dir = Path(CONFIG["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = product_file_stem(job.name) or job.name
    target = output_dir / f"{stem}-interaction-clean.mp4"
    if not target.exists():
        return target
    for index in range(2, 1000):
        candidate = output_dir / f"{stem}-{index}-interaction-clean.mp4"
        if not candidate.exists():
            return candidate
    return output_dir / f"{stem}-{int(time.time())}-interaction-clean.mp4"


def product_original_audio_output_path(job: Path) -> Path:
    output_dir = Path(CONFIG["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    title = ""
    if (job / "content-fb.txt").exists():
        title = (job / "content-fb.txt").read_text(encoding="utf-8", errors="replace")
    if not title and (job / "audio-mode.json").exists():
        try:
            title = str(json.loads((job / "audio-mode.json").read_text(encoding="utf-8", errors="replace")).get("product_summary") or "")
        except Exception:
            title = ""
    stem = product_file_stem(title) or job.name
    target = output_dir / f"{stem}-product-sound.mp4"
    if not target.exists():
        return target
    for index in range(2, 1000):
        candidate = output_dir / f"{stem}-{index}-product-sound.mp4"
        if not candidate.exists():
            return candidate
    return output_dir / f"{stem}-{int(time.time())}-product-sound.mp4"


def deterministic_ratio(job: Path, salt: str = "") -> float:
    source = (job / "source-path.txt").read_text(encoding="utf-8", errors="replace") if (job / "source-path.txt").exists() else ""
    key = f"{job.name}|{source}|{salt}"
    digest = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:12]
    return int(digest, 16) / float(0xFFFFFFFFFFFF)


def should_preserve_clean_product_audio(job: Path) -> bool:
    if str(os.environ.get("AUTOVOICE_FORCE_VOICEOVER") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    audio_mode = read_audio_mode(job)
    mode = str(audio_mode.get("audio_mode") or "").strip().lower()
    transcript = (job / "transcript-zh.txt").read_text(encoding="utf-8", errors="replace") if (job / "transcript-zh.txt").exists() else ""
    if has_meaningful_chinese_voice_transcript(transcript):
        return False
    if audio_mode.get("needs_vocal_separation") or mode == "chinese_voice":
        return False
    if mode not in {"product_sound", "music_only"}:
        return False
    preserve_ratio = float(CONFIG.get("product_sound_preserve_original_ratio", 0.4))
    return deterministic_ratio(job, "preserve_product_audio") < preserve_ratio


def render_product_original_audio(job: Path, blur_text: bool = False) -> Path:
    source_path = job / "source-path.txt"
    if not source_path.exists():
        raise HTTPException(400, "Missing source video")
    source_video = source_path.read_text(encoding="utf-8").strip()
    if not source_video:
        raise HTTPException(400, "Missing source video")
    output = product_original_audio_output_path(job)
    trim_start = detect_intro_title_trim(source_video)
    if trim_start > 0:
        print(f"AUTOVOICE_PROGRESS trim: cut {trim_start:.2f}s opening Chinese title", flush=True)
    blur_regions: list[dict] = []
    if blur_text:
        print("AUTOVOICE_PROGRESS blur: kiem tra subtitle/text overlay", flush=True)
        blur_regions = detect_chinese_text_blur_regions(source_video, job)
        enforce_text_overlay_policy(job, blur_regions)
    cmd = ["ffmpeg", "-hide_banner", "-y"]
    if trim_start > 0:
        cmd += ["-ss", f"{trim_start:.3f}"]
    cmd += ["-i", source_video]
    if blur_regions:
        cmd += [
            "-filter_complex",
            timed_blur_filter_complex(blur_regions),
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "copy",
        ]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a?", "-c", "copy"]
    cmd += ["-movflags", "+faststart", str(output)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    (job / "product-original-audio-render-log.txt").write_text((proc.stdout or "") + "\n" + (proc.stderr or ""), encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "ffmpeg product original audio render failed")[-2000:])
    audio_mode = read_audio_mode(job)
    audio_mode.update({
        "voiceGenerated": False,
        "voiceDecision": "preserve_clean_product_audio",
        "voiceDecisionRatio": round(deterministic_ratio(job, "preserve_product_audio"), 4),
        "voiceProbability": float(CONFIG.get("product_sound_voiceover_ratio", 0.6)),
        "originalAudioPreserved": True,
        "blurChecked": bool(blur_text),
        "blurApplied": bool(blur_regions),
        "blurRegionCount": len(blur_regions),
        "trimStart": round(trim_start, 3),
    })
    write_audio_mode(job, audio_mode)
    return output


def fallback_interaction_caption(job: Path, reason: str = "") -> str:
    context = " ".join([
        (job / "analysis-brief.md").read_text(encoding="utf-8", errors="replace") if (job / "analysis-brief.md").exists() else "",
        (job / "source-path.txt").read_text(encoding="utf-8", errors="replace") if (job / "source-path.txt").exists() else "",
        reason or "",
    ])
    folded = fold_vietnamese_text(context)
    if any(token in folded for token in ["bep", "nau", "mon an", "thuc an", "food", "cook", "kitchen"]):
        text = "Cảnh trong bếp nhìn đơn giản mà dễ kéo người xem dừng lại."
    elif any(token in folded for token in ["nha", "don", "sap xep", "storage", "clean"]):
        text = "Một góc sinh hoạt nhỏ, nhìn gọn hơn là thấy dễ chịu."
    elif any(token in folded for token in ["tool", "cat", "bam", "may", "dung cu"]):
        text = "Pha thao tác này nhìn gọn tay, xem khá đã mắt."
    else:
        text = "Một đoạn đời thường ngắn, càng xem càng muốn xem tiếp."
    return format_short_fb_caption(text, f"{context} interaction")


def generate_interaction_caption_with_codex(job: Path) -> str:
    contact = job / "contact-sheet.jpg"
    content_path = job / "content-fb.txt"
    if not contact.exists():
        caption = fallback_interaction_caption(job, "missing contact sheet")
        content_path.write_text(caption, encoding="utf-8")
        return caption

    meta = json.loads((job / "metadata.json").read_text(encoding="utf-8")) if (job / "metadata.json").exists() else {}
    duration = core.duration_from_probe(meta) if meta else 0
    brief = (job / "analysis-brief.md").read_text(encoding="utf-8", errors="replace") if (job / "analysis-brief.md").exists() else ""
    prompt = f"""
Analyze the attached contact sheet for an interaction/repost video.

This is NOT an AutoVoice product voice job. Do not create voice script, subtitle, scene list, or sales copy.

Task:
- Look at the visible action/topic in the frames.
- Write only one Facebook caption to this file: {content_path}
- Caption language: Vietnamese with full diacritics.
- Style: short, natural, curiosity/interaction oriented, suitable for a Facebook Reel.
- If the video is about household, kitchen, tool, cleaning, food, or daily-life utility, mention the visible action/use naturally like the household product captions do, but do not hard-sell.
- If it is just funny/odd/satisfying/life content, make the caption about the moment viewers would react to.
- Format: one compact sentence under 85 characters + exactly 3 relevant hashtags.
- Avoid Chinese, Thai, English, mojibake, medical claims, child claims, violence, pests/poison/killing, regulated goods, gambling/adult wording, exaggerated guarantees, and feng-shui/wealth promises.

Context:
- Job folder: {job}
- Duration: {duration:.2f}s
- Source path: {(job / 'source-path.txt').read_text(encoding='utf-8', errors='replace') if (job / 'source-path.txt').exists() else ''}
- Brief:
{brief}
"""
    codex_exe = shutil.which("codex")
    if not codex_exe:
        caption = fallback_interaction_caption(job, "codex cli not found")
        content_path.write_text(caption, encoding="utf-8")
        return caption
    if codex_exe.lower().endswith(".ps1"):
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            codex_exe,
        ]
    else:
        cmd = [codex_exe]
    cmd += [
        "exec",
        "--skip-git-repo-check",
        "--full-auto",
        f"--cd={job}",
        f"--image={contact}",
        "-",
    ]
    print("AUTOVOICE_PROGRESS interaction: tao caption tu video", flush=True)
    proc = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=codex_subprocess_env(),
    )
    (job / "interaction-caption-log.txt").write_text(proc.stdout + "\n\n" + proc.stderr, encoding="utf-8")
    raw = content_path.read_text(encoding="utf-8", errors="replace").strip() if content_path.exists() else ""
    if proc.returncode != 0 or not raw or looks_broken_vietnamese_text(raw):
        raw = fallback_interaction_caption(job, proc.stderr or proc.stdout)
    caption = format_short_fb_caption(raw, f"{brief} interaction")
    content_path.write_text(caption, encoding="utf-8")
    return caption


def interaction_job(job: Path):
    error = job / "last-error.txt"
    if error.exists():
        error.unlink()
    try:
        source_path = job / "source-path.txt"
        if not source_path.exists():
            raise HTTPException(400, "Missing source video")
        source_video = source_path.read_text(encoding="utf-8").strip()
        if not source_video:
            raise HTTPException(400, "Missing source video")

        output = interaction_output_path(job)
        blur_regions = detect_chinese_text_blur_regions(source_video, job)
        enforce_text_overlay_policy(job, blur_regions)
        apply_blur = bool(blur_regions)
        write_audio_mode(job, {
            "audio_mode": "interaction_original_audio",
            "mode": "interaction",
            "voiceGenerated": False,
            "subtitleGenerated": False,
            "blurChecked": True,
            "blurApplied": bool(apply_blur),
            "blurRegionCount": len(blur_regions),
            "originalAudioPreserved": True,
        })
        (job / "voice-script.txt").write_text("", encoding="utf-8")
        if should_use_codex_for("interaction"):
            generate_interaction_caption_with_codex(job)
        else:
            content_path = job / "content-fb.txt"
            content_path.write_text(fallback_interaction_caption(job, "offline interaction caption"), encoding="utf-8")

        if apply_blur:
            print(f"AUTOVOICE_PROGRESS interaction: blur {len(blur_regions)} Chinese text regions", flush=True)
            filter_complex = timed_blur_filter_complex(blur_regions)
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-y",
                "-i",
                source_video,
                "-filter_complex",
                filter_complex,
                "-map",
                "[v]",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            (job / "interaction-render-log.txt").write_text((proc.stdout or "") + "\n" + (proc.stderr or ""), encoding="utf-8")
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or "ffmpeg interaction render failed")[-2000:])
        else:
            print("AUTOVOICE_PROGRESS interaction: no text blur needed", flush=True)
            shutil.copy2(source_video, output)

        (job / "last-success.txt").write_text(str(output), encoding="utf-8")
        return output
    except Exception as exc:
        error.write_text(str(exc), encoding="utf-8")
        raise


def read_price_promo_request(job: Path) -> dict:
    path = job / "price-promo-request.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def enforce_price_promo_script(job: Path):
    request = read_price_promo_request(job)
    if not request.get("enabled"):
        return
    price = str(request.get("price") or "").strip()
    if not price:
        return
    script_path = job / "voice-script.txt"
    script = script_path.read_text(encoding="utf-8", errors="replace").strip() if script_path.exists() else ""
    compact_script = re.sub(r"\s+", "", script.lower())
    compact_price = re.sub(r"\s+", "", price.lower())
    if compact_price and compact_price in compact_script:
        return
    audio_mode = read_audio_mode(job)
    summary = str(audio_mode.get("product_summary") or "").strip()
    product = re.split(r"[,.\|。#]", summary)[0].strip()
    if not product or re.search(r"[\u3400-\u9fff]", product):
        product = "Món này"
    prefix = f"{product} đang xả kho {price} một cái."
    script_path.write_text(" ".join([prefix, script]).strip(), encoding="utf-8")


def auto_job(job: Path, tone: str = "", blur_text: bool = False, voice: str = "auto"):
    error = job / "last-error.txt"
    if error.exists():
        error.unlink()
    try:
        if should_use_codex_for("product"):
            print("AUTOVOICE_PROGRESS codex: phÃ¢n tÃ­ch video vÃ  viáº¿t voice", flush=True)
            generate_script_with_codex(job, tone)
        else:
            print("AUTOVOICE_PROGRESS offline: tao script bang rule/local cache", flush=True)
            fallback_script_for_job(job, tone, "AUTOVOICE_AI_MODE disables product Codex")
            normalize_audio_mode_for_product_sound(job)
        validate_vietnamese_script(job)
        enforce_price_promo_script(job)
        if should_preserve_clean_product_audio(job) and "PRICE_PROMO_MODE" not in str(tone or ""):
            print("AUTOVOICE_PROGRESS audio: clean product sound selected, preserve original audio without voice", flush=True)
            output = render_product_original_audio(job, blur_text=blur_text)
            content_fb = ensure_content_fb(job)
            if content_fb:
                (output.with_suffix(".content.txt")).write_text(content_fb.strip(), encoding="utf-8")
            (job / "last-success.txt").write_text(str(output), encoding="utf-8")
            return output
        original_volume = read_audio_volume(job, CONFIG.get("original_volume_when_product_sound", 1.0))
        output = run_renderer(
            job,
            original_volume=original_volume,
            voice=choose_voice_for_job(job, tone, voice),
            rate=CONFIG.get("default_rate", "+30%"),
            voice_volume=CONFIG.get("voice_volume", 1.35),
            blur_text=blur_text,
        )
        content_fb = ensure_content_fb(job)
        if content_fb:
            (output.with_suffix(".content.txt")).write_text(content_fb.strip(), encoding="utf-8")
        (job / "last-success.txt").write_text(str(output), encoding="utf-8")
        return output
    except Exception as exc:
        msg = str(exc)
        error.write_text(msg, encoding="utf-8")
        raise


def load_rules():
    skill = Path(CONFIG["skill_dir"])
    parts = ["""
Core AutoVoice rules:
- Write natural Vietnamese product-review voice, close and practical, not hard-sell.
- Use the learned style files below as the main creative voice memory. Do not make scripts mechanical.
- Do not copy fixed phrases across every video. Choose the hook, role, rhythm, and proof order from the footage.
- Write like a real user reviewing after trying the product: slightly personal, practical, and specific to the visible action.
- Avoid repeated AI-sounding filler such as "rất đáng mua nha", "cái này hay ha", "quá là tiện", "siêu tiện lợi", "nhỏ mà có võ". Do not reuse the same closing line across jobs.
- Do not use casual filler particles at sentence endings: "nha", "ha", "nè", "nhé", "luôn nha", "đó nha", "đấy nha". The voice should sound like a real product user explaining what they tried, not a template seller.
- Use varied sentence length and spoken punctuation. Add natural micro-pauses with commas or short sentences, but do not add stage directions, SSML, emoji, or bracketed emotion labels.
- Learn writing behavior from samples, not exact wording. Convert sample phrases into intent, voice role, rhythm, transition logic, and proof order.
- The script must feel like a fresh content creator line for this footage, not a copied template from prior videos.
- Use source Chinese only for meaning. Rewrite naturally; never translate line by line.
- Ignore opening Chinese title overlays and transcript noise/credits such as "å­—å¹• by...".
- Audio: Chinese/source speech only -> original_volume 0. Chinese/source speech mixed with music/product sound -> needs_vocal_separation true, remove source voice stem and keep no-vocals bed. No source speech with confirmed real product-operation sound -> keep original bed around 0.7. No source speech with music/SFX/ambience only, or product sound is not clearly confirmed -> keep original bed around 0.3.
- Scene-beat voice rule: write to the actual order of visible scenes. Do not paste a generic product paragraph over the whole video. Each spoken beat must match the action currently shown: problem/old habit -> product reveal -> setup/use -> proof/result -> light close.
- Hook discipline: if the footage shows a problem, old habit, messy state, awkward action, smell/cleaning issue, or before-state, open from that pain point. Do not open with the final proof/result when problem footage exists.
- Transition rule: after the hook, bridge naturally into the product as the fix. Avoid abrupt jumps from "problem" straight to feature listing; use one short reason that explains why the product appears now.
- Proof scenes need room: when the video shows water, handling, clicking, flushing, sliding, cutting, spraying, or other real product-operation sound, use fewer words or a short pause so the sound remains believable.
- Script density: "vua thoai" is the target. Do not make the voice too short/cut off, and do not make it so long that TTS has to rush. Keep enough words to connect hook -> solution -> demo -> proof, but let the footage carry obvious actions.
- Never output meta/editor notes inside the script, such as "doan nay nen giu tieng..." or instructions about what the editor should do.
- Facebook-safe caption: one short benefit sentence under 85 chars + exactly 3 hashtags. Avoid regulated goods, violent words, medical/child claims, sensitive-attribute hooks, exaggerated guarantees, and feng-shui/wealth promise words.
- Output must use clean UTF-8 Vietnamese with full diacritics. Never output mojibake, romanized Vietnamese, Thai, Chinese, or mixed-language voiceover.
""".strip()]
    for rel in [
        "references/style-generalization-rules.md",
        "references/learned-competitor-voice-style.md",
        "references/learned-fb-home-kitchen-style.md",
        "references/voice-selection-matrix.md",
        "references/video-form-voice-brain.md",
    ]:
        p = skill / rel
        if p.exists():
            parts.append(f"--- {rel} ---\n{p.read_text(encoding='utf-8', errors='replace').strip()}")
    return "\n\n".join(parts)


def learned_voice_directive(duration: float, tone: str = "", allow_particle: bool | None = None) -> str:
    # Random THEO TỪNG VIDEO: chỉ ~1/3 video được dùng 1 từ đệm thân mật
    # (nè/nha/ha) đặt tự nhiên; còn lại không dùng. Để cả trang KHÔNG nghe
    # giống nhau — không phải video nào cũng "nha/ha" (yêu cầu 2026-07: random
    # chứ không 100%, chính là cái làm giọng nghe không tự nhiên trước đây).
    if allow_particle is None:
        allow_particle = random.random() < 0.33
    if allow_particle:
        particle_rule = ("- Video NÀY: được dùng TỐI ĐA 1 từ đệm thân mật (nè / nha / ha) đặt ở 1 chỗ hợp lý, "
                         "tự nhiên. TUYỆT ĐỐI không lặp, không kết mỗi câu bằng nó.")
    else:
        particle_rule = ("- Video NÀY: KHÔNG dùng từ đệm nè / nha / ha / nhé. Giữ ấm bằng quan sát cụ thể + "
                         "nhịp câu, không bằng từ đệm.")
    return f"""
Bạn viết LỜI THOẠI (voiceover) tiếng Việt cho video sản phẩm — nghe như MỘT NGƯỜI THẬT vừa dùng qua đang kể cho bạn bè xem, KHÔNG phải quảng cáo đọc như máy, KHÔNG phải bản dịch cứng.

GIỌNG MỤC TIÊU (bắt chước TINH THẦN, tuyệt đối không copy nguyên chữ):
- Ngôi thứ nhất, gần gũi: "Cái này mình thấy...", "Không ngờ lại có...", "Được cái là...".
- Câu ngắn xen câu vừa, ngắt nghỉ tự nhiên bằng dấu phẩy. Đừng đọc như liệt kê tính năng.
- Ấm và thật nhờ CHI TIẾT CỤ THỂ nhìn thấy (thao tác tay, vị trí, trước/sau, âm thanh, kích thước, chất liệu, nơi dùng) — không nhờ lời khen suông.

VÍ DỤ NHỊP TỰ NHIÊN (chỉ để CẢM nhận văn phong, không được lặp lại y nguyên):
- "Không ngờ cái nồi nhỏ này lại làm được cả nướng lẫn chiên. Gập gọn lại là cất vừa hộc bếp, nhà chật cỡ nào cũng ổn."
- "Từ hồi có cái máy xay cầm tay, đi đâu mình cũng làm được ly sinh tố. Xay xong tháo cốc ra rửa, gọn."
- "Chỗ bồn rửa hay đọng nước bẩn, giờ gắn cái này lên là nước tự thoát, nhìn khô ráo hẳn."

ĐA DẠNG HOÁ (để cả trang không nghe 1 màu — đây là điểm QUAN TRỌNG nhất):
- Mỗi video tự chọn 1 KIỂU MỞ khác nhau, xoay vòng: từ vấn đề, từ kết quả, từ tình huống, từ chi tiết lạ, từ câu hỏi, từ người dùng cụ thể, từ sự bất ngờ. KHÔNG lặp kiểu mở của video trước.
- Đổi cả sắc thái người kể: lúc như người mới dùng thấy sướng, lúc như người kỹ tính soi kỹ, lúc như người bận rộn thấy tiện. Miễn là thật.
{particle_rule}

BÁM ĐÚNG VIDEO & SẢN PHẨM (bắt buộc — giữ tinh thần thật của video):
- Chỉ nói cái NHÌN THẤY trong khung hình hoặc CÓ trong voice gốc. KHÔNG bịa tính năng, chất liệu, thông số, giá, kết quả.
- Nếu có voice gốc (thường tiếng Trung): lấy Ý + trình tự + cảm xúc từ đó rồi viết lại tự nhiên bằng tiếng Việt (không dịch từng câu cứng). Dịch sát mà cho mượt cũng đã ổn.
- Beat-matching: nói đúng cảnh đang chiếu — cảnh setup nói setup, cảnh chứng minh nói kết quả. Lúc có tiếng thao tác thật (xả nước, cắt, click, xịt...) thì nói ÍT lại / chừa khoảng lặng để tiếng đó tự chứng minh.

Độ dài theo {duration:.2f}s: 10-12s = 2-4 câu gọn; 15-18s ≈ 45-70 từ; 19-24s ≈ 60-85 từ; 25-35s ≈ 75-110 từ. Thiếu chỗ thì cắt bớt chữ, ĐỪNG ép đọc nhanh.

TRÁNH:
- Câu sáo rỗng lặp lại: "rất đáng mua", "quá tiện lợi", "nhỏ mà có võ", "ai cũng nên có", "chân ái".
- Từ thổi phồng: "siêu phẩm", "thần thánh", "đỉnh cao" (trừ khi được yêu cầu).
- Ghi chú sản xuất/biên tập nằm trong lời thoại.

An toàn FB/VN: tránh vũ khí/bạo lực, thuốc/chữa bệnh/giảm cân, trẻ em, cờ bạc, người lớn, cam kết phóng đại, và từ phong thuỷ/tài lộc (tài lộc, phong thuỷ, may mắn, hút tiền, hút lộc, thần tài, phát tài, đổi vận...). Đồ xịt gọi "đầu xịt / vòi xịt / bình xịt", KHÔNG gọi "súng xịt".

Tone yêu cầu hiện tại: {tone or 'người dùng thật review nhẹ nhàng, tự nhiên'} — hoà tone này vào văn phong trên, không bỏ cái nào.
"""


def video_form_directive(job: Path, duration: float, tone: str = "", transcript: str = "") -> str:
    if video_form_brain is None:
        return ""
    try:
        return video_form_brain.build_voice_directive(job, duration=duration, tone=tone, transcript=transcript)
    except Exception as exc:
        return f"Video form brain unavailable for this job: {exc}"


def ensure_content_fb(job: Path) -> str:
    content_path = job / "content-fb.txt"
    existing = repair_mojibake_text(content_path.read_text(encoding="utf-8", errors="replace")).strip() if content_path.exists() else ""
    if existing:
        content = format_short_fb_caption(existing, existing)
        if content != existing:
            content_path.write_text(content, encoding="utf-8")
        return content
    script = (job / "voice-script.txt").read_text(encoding="utf-8", errors="replace").strip() if (job / "voice-script.txt").exists() else ""
    audio_mode = {}
    if (job / "audio-mode.json").exists():
        try:
            audio_mode = json.loads((job / "audio-mode.json").read_text(encoding="utf-8", errors="replace"))
        except Exception:
            audio_mode = {}
    summary = str(audio_mode.get("product_summary") or "").strip()
    text = " ".join((summary or script).split())
    if not text:
        content = "Má»™t mÃ³n Ä‘á»“ nhá» nhÆ°ng dÃ¹ng háº±ng ngÃ y ráº¥t tiá»‡n. Ai thÃ­ch gá»n vÃ  nhÃ n hÆ¡n thÃ¬ nÃªn thá»­."
    elif any(word in text.lower() for word in ["ná»“i", "chÃ¡o", "cÆ¡m", "báº¿p", "háº¥p", "giá»¯ áº¥m", "háº¹n giá»"]):
        content = "SÃ¡ng dáº­y cÃ³ sáºµn bá»¯a nÃ³ng, khÃ´ng cáº§n canh báº¿p. Chiáº¿c ná»“i nhá» gá»n nÃ y quÃ¡ há»£p cho ngÆ°á»i báº­n rá»™n."
    elif any(word in text.lower() for word in ["dao", "cáº¯t", "khoan", "váº·n", "dá»¥ng cá»¥", "tool"]):
        content = "LÃ m viá»‡c nhanh hÆ¡n háº³n khi cÃ³ Ä‘Ãºng mÃ³n Ä‘á»“ há»— trá»£. Nhá» gá»n mÃ  cÃ´ng nÄƒng ráº¥t Ä‘Ã¡ng tiá»n."
    else:
        first = text.split(".")[0].strip()
        content = first if 25 <= len(first) <= 150 else text[:150].rsplit(" ", 1)[0]
        content = content.rstrip(".,; ") + "."
    content = format_short_fb_caption(content, text)
    content_path.write_text(content.strip(), encoding="utf-8")
    return content.strip()


def is_interaction_context(context: str) -> bool:
    value = normalize_policy_text(context)
    return "interaction" in value or "tuong tac" in value


def fallback_interaction_policy_caption(context: str = "") -> str:
    value = normalize_policy_text(context)
    if re.search(r"\b(be|tre|em be|mam non|ngu trua|lop hoc|hoc sinh|baby|kids|children|kindergarten)\b", value):
        return "Một cảnh sinh hoạt nhẹ nhàng, nhìn rất tự nhiên"
    if re.search(r"\b(thu cung|pet|cho|meo|cat|dog|cun)\b", value):
        return "Biểu cảm nhỏ này nhìn qua là muốn xem lại"
    if re.search(r"\b(diy|thu cong|handmade|craft)\b", value):
        return "Đoạn làm thủ công này nhìn chậm rãi mà khá cuốn"
    if re.search(r"\b(bep|nau|mon an|food|cook|kitchen)\b", value):
        return "Cảnh trong bếp nhìn đơn giản mà dễ kéo người xem dừng lại"
    if re.search(r"\b(nha|don|sap xep|storage|clean)\b", value):
        return "Một góc sinh hoạt nhỏ, nhìn gọn hơn là thấy dễ chịu"
    return "Một đoạn đời thường ngắn, càng xem càng muốn xem tiếp"


def looks_like_product_fallback_caption(text: str) -> bool:
    value = normalize_policy_text(text)
    return len(value) < 12 or any(token in value for token in [
        "goc bep",
        "g?c b?p",
        "nha kho",
        "nh? kho",
        "mon do nho",
        "dung cu bep",
        "san pham nay",
        "tien hon",
        "gon gang",
        "g?n g?ng",
        "de ve sinh",
        "d? v? sinh",
    ])


def looks_like_interaction_caption(text: str) -> bool:
    value = normalize_policy_text(text)
    return bool(re.search(r"\b(khoanh khac|cuon|xem la vui|xemlavui|cuoi nghieng|cuoinghieng|xemlagi|reelsvui|viral|dang yeu|giaitri|bat cuoi|hai huoc|doi thuong ngan)\b", value))


def fallback_product_policy_caption(context: str = "") -> str:
    value = normalize_policy_text(context)
    if re.search(r"\b(bep|noi|chao|dao|ep|coc|ly|binh|kitchen|cook|fruit|juice|chen|bat|dua|thia|mon an)\b", value):
        return "Món đồ bếp nhỏ gọn, dùng hằng ngày khá tiện"
    if re.search(r"\b(camp|outdoor|da ngoai|cam trai|bep ga|gas|leu|san vuon|ngoai troi)\b", value):
        return "Món tiện ích này hợp cho những lúc cần sắp xếp gọn ngoài trời"
    if re.search(r"\b(xe|oto|o to|car|motor|bike|di xe|kinh|non|mu)\b", value):
        return "Phụ kiện nhỏ gọn, dùng khi đi lại khá tiện"
    if re.search(r"\b(ghe|ban|giuong|rem|tu|ke|gia|treo|storage|sap xep|don|clean)\b", value):
        return "Món đồ nhỏ giúp góc nhà gọn gàng và dễ dùng hơn"
    return "Món đồ nhỏ gọn, dùng trong sinh hoạt hằng ngày khá tiện"


def format_short_fb_caption(content: str, context: str = "") -> str:
    text = " ".join(repair_mojibake_text(str(content or "")).split())
    text = re.sub(r"#\S+", "", text).strip()
    interaction = is_interaction_context(context)
    if not text or looks_broken_vietnamese_text(text):
        text = fallback_interaction_policy_caption(context) if interaction else "Món đồ nhỏ mà dùng rất tiện."
    if not text or looks_broken_vietnamese_text(text):
        policy_context = normalize_policy_text(f"{context} {content}")
        text = "Khoảnh khắc này nhìn đơn giản mà khá cuốn." if "interaction" in policy_context else "Món đồ nhỏ mà dùng rất tiện."
    text = sanitize_facebook_policy_text(text, f"{context} {content}")
    if interaction and looks_like_product_fallback_caption(text):
        text = fallback_interaction_policy_caption(f"{context} {content}")
    if not interaction and looks_like_interaction_caption(text):
        text = fallback_product_policy_caption(f"{context} {content}")
    text = re.split(r"(?<=[.!?])\s+", text)[0].strip()
    if len(text) > 85:
        text = text[:85].rsplit(" ", 1)[0].strip()
    text = text.rstrip(".,;:!?") + "."
    hashtags = choose_caption_hashtags(f"{context} {content}", interaction=interaction)
    return f"{text} {' '.join(hashtags)}".strip()

def normalize_policy_text(value: str) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return " ".join(text.lower().split())


def repair_mojibake_text(value: str) -> str:
    text = str(value or "")
    if not re.search(r"[ÃƒÃ‚]|Ã¡Âº|Ã¡Â»|ï¿½", text):
        return text
    try:
        repaired = text.encode("latin1", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return text
    bad_score = len(re.findall(r"[ÃƒÃ‚]|Ã¡Âº|Ã¡Â»|ï¿½", text))
    repaired_score = len(re.findall(r"[ÃƒÃ‚]|Ã¡Âº|Ã¡Â»|ï¿½", repaired))
    return repaired if repaired and repaired_score < bad_score else text


def looks_broken_vietnamese_text(value: str) -> bool:
    text = str(value or "")
    if not text.strip():
        return False
    if re.search(r"[ÃƒÃ‚]|Ã¡Âº|Ã¡Â»|ï¿½", text):
        return True
    words = re.findall(r"[^\W\d_?]+|\?+", text, flags=re.UNICODE)
    if not words:
        return False
    question_words = [word for word in words if "?" in word and re.search(r"[A-Za-z]", word)]
    if len(question_words) >= 2:
        return True
    mark_count = vietnamese_mark_count(text)
    plain_vietnamese_signals = re.search(r"\b(mon|dung|hang|ngay|tien|gon|bep|nha|dung cu|ho tro)\b", normalize_policy_text(text))
    return bool(plain_vietnamese_signals and len(words) >= 8 and mark_count < 3)


def sanitize_facebook_policy_text(content: str, context: str = "") -> str:
    source = normalize_policy_text(f"{content} {context}")
    interaction = is_interaction_context(context)
    if re.search(r"\b(bay chuot|bat chuot|diet chuot|chuot|con trung|gian|muoi|thuoc doc|chat doc|doc hai)\b", source):
        return fallback_interaction_policy_caption(context) if interaction else "Giữ góc bếp và nhà kho gọn gàng, dễ vệ sinh hơn"
    if re.search(r"\b(tai loc|phong thuy|may man|hut tien|hut loc|chieu tai|chieu loc|doi van|cai van|than tai|tai van|van may|gia chu|lam an phat dat|phat tai|phat loc|tien vao|giu tien|cau tai|cau loc)\b", source):
        return fallback_interaction_policy_caption(context) if interaction else "Trang trí không gian gọn đẹp, nhìn hài hòa hơn mỗi ngày"
    if re.search(r"\b(dao|kiem|sung|vu khi|chem|dam|cat dut|sat thuong|xuong|mau)\b", source):
        return fallback_interaction_policy_caption(context) if interaction else "Dụng cụ bếp nhỏ gọn, hỗ trợ sơ chế nhanh hơn mỗi ngày"
    if re.search(r"\b(chua|tri|khoi benh|giam can|dot mo|benh|dau nhuc|huyet ap|tieu duong|ung thu|thuoc|vitamin|dinh duong|cho be|tre em|em be)\b", source):
        return fallback_interaction_policy_caption(context) if interaction else "Món đồ nhỏ gọn, dùng trong sinh hoạt hằng ngày rất tiện"
    if re.search(r"\b(ruou|bia|thuoc la|vape|casino|ca cuoc|co bac|ma tuy|18\+|sex|nguoi lon)\b", source):
        return fallback_interaction_policy_caption(context) if interaction else "Một món đồ tiện ích, hợp cho sinh hoạt hằng ngày"
    if re.search(r"\b(bay chuot|bat chuot|diet chuot|chuot|con trung|gian|muoi|thuoc doc|chat doc|doc hai)\b", source):
        return fallback_interaction_policy_caption(context) if interaction else "Giữ góc bếp và nhà kho gọn gàng, dễ vệ sinh hơn"
    if re.search(r"\b(tai loc|phong thuy|may man|hut tien|hut loc|chieu tai|chieu loc|doi van|cai van|than tai|tai van|van may|gia chu|lam an phat dat|phat tai|phat loc|tien vao|giu tien|cau tai|cau loc)\b", source):
        return "Trang trÃ­ khÃ´ng gian gá»n Ä‘áº¹p, nhÃ¬n hÃ i hÃ²a hÆ¡n má»—i ngÃ y"
    if re.search(r"\b(dao|kiem|sung|vu khi|chem|dam|cat dut|sat thuong|xuong|mau)\b", source):
        return fallback_interaction_policy_caption(context) if interaction else "Dụng cụ bếp nhỏ gọn, hỗ trợ sơ chế nhanh hơn mỗi ngày"
    if re.search(r"\b(chua|tri|khoi benh|giam can|dot mo|benh|dau nhuc|huyet ap|tieu duong|ung thu|thuoc|vitamin|dinh duong|cho be|tre em|em be)\b", source):
        return fallback_interaction_policy_caption(context) if interaction else "Món đồ nhỏ gọn, dùng trong sinh hoạt hằng ngày rất tiện"
    if re.search(r"\b(ruou|bia|thuoc la|vape|casino|ca cuoc|co bac|ma tuy|18\+|sex|nguoi lon)\b", source):
        return "Má»™t mÃ³n Ä‘á»“ tiá»‡n Ã­ch, há»£p cho sinh hoáº¡t háº±ng ngÃ y"

    text = " ".join(str(content or "").split())
    replacements = [
        (r"\bsiÃªu pháº©m\b", "mÃ³n Ä‘á»“"),
        (r"\btháº§n thÃ¡nh\b", "tiá»‡n"),
        (r"\bÄ‘á»‰nh cao\b", "gá»n gÃ ng"),
        (r"\bcam káº¿t\b", "dá»…"),
        (r"\bÄ‘áº£m báº£o\b", "há»— trá»£"),
        (r"\bdao\b", "dá»¥ng cá»¥ báº¿p"),
        (r"\bxÆ°Æ¡ng\b", "nguyÃªn liá»‡u cá»©ng"),
    ]
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return text.strip()


def choose_caption_hashtags(context: str, interaction: bool = False) -> list[str]:
    raw = str(context or "")
    value = fold_vietnamese_text(raw)
    interaction_context = bool(interaction) and bool(
        re.search(r"\b(interaction|tuong tac|khoanh khac|cuon|funny|odd|satisfying|thu cong|diy|handmade|craft|xinh|cun|cho|meo|pet|thu cung|dang yeu|bat cuoi|hai huoc)\b", value)
        or re.search(r"\u624b\u5de5|\u624b\u4f5c|\u6298\u7eb8|\u513f\u7ae5\u624b\u5de5|\u624b\u5de5diy|\u624b\u5de5\u6298\u7eb8|\u6247\u5f62\u624b\u5de5", raw, re.IGNORECASE)
    )
    product_context = (not interaction_context) and bool(re.search(r"\b(product|san pham|gia dung|tien ich|review|mua hang|ecom|bep|noi|chao|dao|ep|coc|ly|binh|kitchen|cook|fruit|juice|chen|bat|dua|thia|mon an|camp|outdoor|da ngoai|cam trai|bep ga|gas|leu|leo nui|san vuon|ngoai troi|xe|oto|o to|car|motor|bike|di xe|kinh|non|mu|may|dung cu|sua|nguon|den|sac|goi|vo boc|cotton)\b", value))
    if interaction_context:
        pools = [
            ["#xuhuong", "#doisong", "#giaitri"],
            ["#xemlagi", "#khoanhkhac", "#videohay"],
            ["#cuoinghieng", "#xuhuong", "#xemlavui"],
            ["#khoanhkhacvui", "#doisong", "#giaitri"],
            ["#reelsvui", "#xemlagi", "#viral"],
            ["#dangyeu", "#xuhuong", "#doisong"],
        ]
    elif re.search(r"\b(bep|noi|chao|dao|ep|coc|ly|binh|kitchen|cook|fruit|juice|chen|bat|dua|thia|mon an)\b", value):
        pools = [
            ["#giadung", "#nhabep", "#tienich"],
            ["#meovatnhabep", "#bepnha", "#giadung"],
            ["#gocbep", "#tienichbep", "#doisong"],
            ["#nhabepxinh", "#meonho", "#tienich"],
        ]
    elif re.search(r"\b(camp|outdoor|da ngoai|cam trai|bep ga|gas|leu|leo nui|san vuon|ngoai troi)\b", value):
        pools = [
            ["#dangoai", "#camtrai", "#tienich"],
            ["#ngoaitroi", "#doisong", "#meohay"],
            ["#sanvuon", "#tienich", "#giadung"],
        ]
    elif re.search(r"\b(xe|oto|o to|car|motor|bike|di xe|kinh|non|mu)\b", value):
        pools = [
            ["#xehay", "#tienich", "#doisong"],
            ["#diduong", "#meohay", "#tienich"],
            ["#phukien", "#doisong", "#xuhuong"],
        ]
    elif product_context:
        pools = [
            ["#giadung", "#tienich", "#doisong"],
            ["#meohay", "#doisong", "#tienich"],
            ["#xuhuong", "#giadung", "#meonho"],
        ]
    else:
        pools = [
            ["#giadung", "#tienich", "#doisong"],
            ["#meohay", "#doisong", "#tienich"],
            ["#xuhuong", "#giadung", "#meonho"],
        ]
    seed = sum((index + 1) * ord(ch) for index, ch in enumerate(value or raw)) + int(time.time() // 17)
    return pools[seed % len(pools)]


def generate_script_with_openai(job: Path, tone: str):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(400, "OPENAI_API_KEY is not set. Set it, restart server, then generate script.")
    try:
        from openai import OpenAI
    except Exception as exc:
        raise HTTPException(500, f"OpenAI SDK unavailable: {exc}") from exc

    contact = job / "contact-sheet.jpg"
    if not contact.exists():
        raise HTTPException(400, "Missing contact sheet. Prepare the job first.")
    transcript = (job / "transcript-zh.txt").read_text(encoding="utf-8", errors="replace") if (job / "transcript-zh.txt").exists() else ""
    meta = json.loads((job / "metadata.json").read_text(encoding="utf-8")) if (job / "metadata.json").exists() else {}
    duration = core.duration_from_probe(meta) if meta else 0
    form_directive = video_form_directive(job, duration, tone, transcript)
    b64 = base64.b64encode(contact.read_bytes()).decode("ascii")

    prompt = f"""
You are AutoVoice, a Vietnamese product-video voiceover writer.

Rules:
{load_rules()}

{learned_voice_directive(duration, tone)}

{form_directive}

Job:
- Duration: {duration:.2f}s
- Tone request: {tone or 'natural Vietnamese sales review'}
- Chinese transcript if any:
{transcript}

Task:
1. Analyze the contact sheet and transcript.
2. Decide whether source audio likely has Chinese review voice, product operation sound, or mostly music.
3. Write one Vietnamese voiceover script matching the video duration.
4. Return only JSON with keys:
   product_summary, audio_mode, original_volume, script, content_fb

audio_mode must be one of: chinese_voice, product_sound, music_only.
Do not invent product functions, materials, claims, or usage scenes that are not visible in the frames or present in a real source voice. If the product cannot be identified well enough, keep the script conservative and describe only the visible action/mechanism.
If meaningful Chinese/source speech exists, use it only for meaning. If it is the only useful audio, set original_volume to 0. Ignore transcript noise/credits such as "å­—å¹• by..." and treat that as no source voice. If meaningful Chinese speech is mixed with product sound or music, classify it as product_sound, set needs_vocal_separation true, target removing only the source voice stem while keeping the music/product bed, and set original_volume to 0 unless Demucs later produces a clean audible no-vocals bed. If there is no meaningful Chinese/source speech and the source has confirmed real product-operation sound, set audio_mode product_sound and keep original_volume around 0.7. If product-operation sound is not clearly confirmed, or the source is only background music/SFX/ambience, set audio_mode music_only and keep original_volume around 0.3.
The script must be natural spoken Vietnamese, not literal translation. Ignore opening Chinese title overlays and do not translate on-screen banner text.
The script must be scene-beat matched: speak about the visible action at that moment, not a generic paragraph. If the video shows problem footage first, open from the problem/old habit, then bridge into the product as the solution, then demo, then proof/result. If there is product-operation sound, leave short gaps or fewer words during proof moments so the sound remains audible and real.
Avoid scripts that are too sparse or too dense. The voice should feel like a real user guiding the viewer through the footage. Do not output editor/meta notes, timing notes, or comments about what sound should be kept.
content_fb is a very short Vietnamese Facebook caption for posting this finished video: 1 compact sentence under 85 characters, then exactly 3 relevant hashtags. No long explanation.
content_fb must be Facebook-safe and Vietnam-safe: avoid pests/poison/killing, weapons/violence, medical or child-use claims, regulated goods, gambling/adult wording, exaggerated guarantees, and feng-shui/wealth words like tÃ i lá»™c, phong thá»§y, may máº¯n, hÃºt tiá»n, hÃºt lá»™c, chiÃªu tÃ i, Ä‘á»•i váº­n, tháº§n tÃ i, phÃ¡t tÃ i, phÃ¡t lá»™c.
"""
    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=CONFIG.get("script_model", "gpt-4.1-mini"),
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"},
                ],
            }
        ],
    )
    text = response.output_text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {
            "product_summary": "",
            "audio_mode": "product_sound",
            "original_volume": float(CONFIG.get("original_volume_when_product_sound", 0.7)),
            "script": text
        }
    (job / "ai-script-response.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (job / "voice-script.txt").write_text(data.get("script", "").strip(), encoding="utf-8")
    (job / "content-fb.txt").write_text(format_short_fb_caption(data.get("content_fb") or "", data.get("product_summary") or ""), encoding="utf-8")
    (job / "audio-mode.json").write_text(json.dumps({
        "audio_mode": data.get("audio_mode", "product_sound"),
        "original_volume": data.get("original_volume", float(CONFIG.get("original_volume_when_product_sound", 0.7))),
        "product_summary": data.get("product_summary", ""),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    validate_vietnamese_script(job)
    normalize_audio_mode_for_product_sound(job)
    ensure_content_fb(job)
    return data


def generate_script_with_codex(job: Path, tone: str):
    contact = job / "contact-sheet.jpg"
    if not contact.exists():
        raise HTTPException(400, "Missing contact sheet. Prepare the job first.")
    transcript = (job / "transcript-zh.txt").read_text(encoding="utf-8", errors="replace") if (job / "transcript-zh.txt").exists() else ""
    meta = json.loads((job / "metadata.json").read_text(encoding="utf-8")) if (job / "metadata.json").exists() else {}
    duration = core.duration_from_probe(meta) if meta else 0
    skill = Path(CONFIG["skill_dir"])
    form_directive = video_form_directive(job, duration, tone, transcript)
    prompt = f"""
Use $autovoice at {skill / 'SKILL.md'}.

Create the Vietnamese voice script for this AutoVoice job.

Style priority:
{learned_voice_directive(duration, tone)}

{form_directive}

Inputs:
- Job folder: {job}
- Contact sheet image is attached.
- Source path: {(job / 'source-path.txt').read_text(encoding='utf-8', errors='replace') if (job / 'source-path.txt').exists() else ''}
- Duration: {duration:.2f}s
- Tone request: {tone or 'Vietnamese product review voice, natural sales-demo style'}
- Chinese transcript if useful:
{transcript}

Requirements:
1. Analyze the visible product functions and source transcript.
2. Before writing, choose internally: product group, hook style, sentence rhythm, and voice character. Use that choice to make the script flexible and attractive. Do not output the plan.
3. If the transcript is real Chinese product review, extract the product meaning then rewrite naturally in Vietnamese. Do not translate line by line.
4. If transcript is noisy or not a review, write from the visible product functions.
5. Do not invent product functions, materials, claims, or usage scenes. Only describe what is visible in the contact sheet/frames or what a real source voice says. If the product is unclear, write conservatively from the visible action/mechanism instead of guessing.
6. Write by scene beats, not as one generic sales paragraph:
   - If problem/old habit/before-state footage exists, open from that pain point.
   - Bridge naturally into the product as the fix before explaining features.
   - Match each sentence to the visible action at that point: setup while setup is visible, proof while proof is visible, result while result is visible.
   - During water, flushing, spraying, sliding, clicking, cutting, or handling sounds, use fewer words or a short pause so the product sound stays real.
   - The final script should be "vua thoai": enough to connect hook -> solution -> demo -> proof, but not so dense that the voice rushes.
   - Never include editor/meta notes inside the spoken script.
7. Decide source audio mode:
   - chinese_voice: original_volume 0 when meaningful Chinese/source speech is the only useful audio.
   - mixed source voice + product sound/music: classify as product_sound, target removing only the source voice stem while keeping music/product bed, set needs_vocal_separation true, and set original_volume to 0 unless Demucs later produces a clean audible no-vocals bed.
   - product_sound: original_volume around 0.7 only when there is no meaningful Chinese/source speech and the audio has confirmed real product-operation sound.
   - music_only: original_volume around 0.3 when there is no meaningful Chinese/source speech and the audio is music/SFX/ambience only or product-operation sound is not clearly confirmed.
8. Write the final script to {job / 'voice-script.txt'}.
9. Write a short Facebook caption to {job / 'content-fb.txt'}: 1 compact Vietnamese sentence under 85 characters, then exactly 3 relevant hashtags. Do not make it long.
   Caption/title must be Facebook-safe and Vietnam-safe: avoid pests/poison/killing, weapons/violence, medical or child-use claims, regulated goods, gambling/adult wording, exaggerated guarantees, and feng-shui/wealth words like tÃ i lá»™c, phong thá»§y, may máº¯n, hÃºt tiá»n, hÃºt lá»™c, chiÃªu tÃ i, Ä‘á»•i váº­n, tháº§n tÃ i, phÃ¡t tÃ i, phÃ¡t lá»™c.
10. Write {job / 'audio-mode.json'} with JSON keys: audio_mode, original_volume, product_summary.
11. Keep the script matched to {duration:.2f}s. Do not render video.
12. The script MUST be Vietnamese with full Vietnamese diacritics. Do not write romanized Vietnamese, Thai, Chinese, English, or mixed-language voiceover.
13. If the source transcript is Chinese, translate meaning into natural Vietnamese; never output Thai.
14. Ignore big Chinese title overlays at the start of the video. Do not translate on-screen banner text into the voice.
15. Skip or soften unsupported health, medical, nutrition, and child claims from the source voice. Do not say "cho bÃ© uá»‘ng" or similar child-use claims unless the user explicitly asks.
16. The final voice must feel flexible and close like the learned Facebook examples. Do not just list features mechanically.
"""
    log = job / "codex-generate-log.txt"
    codex_exe = shutil.which("codex")
    if not codex_exe:
        fallback_script_for_job(job, tone, "Codex CLI not found in PATH")
        validate_vietnamese_script(job)
        normalize_audio_mode_for_product_sound(job)
        ensure_content_fb(job)
        return
    if codex_exe.lower().endswith(".ps1"):
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            codex_exe,
        ]
    else:
        cmd = [codex_exe]
    cmd += [
        "exec",
        # Nâng reasoning cho RIÊNG lệnh viết voice (config global đang để
        # "low" — bóp sáng tạo, là 1 lý do voice nhạt/"1 màu"). "medium" cân
        # bằng creativity vs tốc độ khi xử lý nhiều video (2026-07).
        "-c", 'model_reasoning_effort="medium"',
        "--skip-git-repo-check",
        "--full-auto",
        f"--cd={job}",
        f"--add-dir={skill}",
        f"--image={contact}",
        "-",
    ]
    proc = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    log.write_text(proc.stdout + "\n\n" + proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        codex_error = (proc.stderr[-3000:] or proc.stdout[-3000:] or "Codex CLI failed").strip()
        if re.search(r"usage limit|try again at|purchase more credits|rate limit|too many requests|quota", codex_error, re.IGNORECASE):
            raise HTTPException(429, f"Codex CLI quota/rate limit while generating product-specific voice script: {codex_error}")
        fallback_script_for_job(job, tone, proc.stderr[-3000:] or proc.stdout[-3000:])
        validate_vietnamese_script(job)
        normalize_audio_mode_for_product_sound(job)
        ensure_content_fb(job)
        return
    if not (job / "voice-script.txt").read_text(encoding="utf-8", errors="replace").strip():
        fallback_script_for_job(job, tone, "Codex finished but voice-script.txt is still empty. See codex-generate-log.txt.")
        validate_vietnamese_script(job)
        normalize_audio_mode_for_product_sound(job)
        ensure_content_fb(job)
        return
    validate_vietnamese_script(job)
    normalize_audio_mode_for_product_sound(job)
    ensure_content_fb(job)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "jobs": [job_info(j) for j in job_dirs()],
        "input_dir": CONFIG["input_dir"],
        "output_dir": CONFIG["output_dir"],
        "has_key": bool(os.environ.get("OPENAI_API_KEY")),
    })


@app.post("/upload")
async def upload(video: UploadFile = File(...)):
    input_dir = Path(CONFIG["input_dir"])
    input_dir.mkdir(parents=True, exist_ok=True)
    dest = input_dir / Path(video.filename).name
    with dest.open("wb") as f:
        shutil.copyfileobj(video.file, f)
    core.prepare()
    return RedirectResponse("/", status_code=303)


@app.post("/prepare")
def prepare_all():
    core.prepare()
    return RedirectResponse("/", status_code=303)


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    job = get_job(job_id)
    info = job_info(job)
    audio_mode = {}
    if (job / "audio-mode.json").exists():
        audio_mode = json.loads((job / "audio-mode.json").read_text(encoding="utf-8"))
    return templates.TemplateResponse(request, "job.html", {
        "job": info,
        "audio_mode": audio_mode,
        "has_key": bool(os.environ.get("OPENAI_API_KEY")),
    })


@app.post("/job/{job_id}/generate")
def generate(job_id: str, tone: str = Form("")):
    generate_script_with_openai(get_job(job_id), tone)
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/job/{job_id}/generate-codex")
def generate_codex(job_id: str, tone: str = Form("")):
    job = get_job(job_id)
    try:
        generate_script_with_codex(job, tone)
        err = job / "last-error.txt"
        if err.exists():
            err.unlink()
    except Exception as exc:
        (job / "last-error.txt").write_text(str(exc), encoding="utf-8")
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.api_route("/job/{job_id}/auto", methods=["GET", "POST"])
def run_auto(job_id: str):
    job = get_job(job_id)
    try:
        auto_job(job, "giá»ng review bÃ¡n hÃ ng tiáº¿ng Viá»‡t tá»± nhiÃªn, bÃ¡m cÃ´ng nÄƒng trong video")
    except Exception:
        pass
    return RedirectResponse("/", status_code=303)


@app.post("/job/{job_id}/script")
def save_script(job_id: str, script: str = Form(...), original_volume: float = Form(1.0)):
    job = get_job(job_id)
    (job / "voice-script.txt").write_text(script.strip(), encoding="utf-8")
    (job / "audio-mode.json").write_text(json.dumps({
        "audio_mode": "manual",
        "original_volume": original_volume,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/job/{job_id}/render")
def render_job(
    job_id: str,
    original_volume: float = Form(1.0),
    voice: str = Form("male"),
    rate: str = Form("+30%"),
    voice_volume: float = Form(1.35),
    blur_text: bool = Form(False),
):
    job = get_job(job_id)
    original_volume = read_audio_volume(job, original_volume)
    run_renderer(job, original_volume, voice, rate, voice_volume, blur_text=blur_text)
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.get("/job/{job_id}/contact-sheet.jpg")
def contact(job_id: str):
    path = get_job(job_id) / "contact-sheet.jpg"
    if not path.exists():
        raise HTTPException(404, "No contact sheet")
    return FileResponse(path)


@app.get("/output/{name}")
def output_file(name: str):
    path = Path(CONFIG["output_dir"]) / name
    if not path_in(Path(CONFIG["output_dir"]), path) or not path.exists():
        raise HTTPException(404, "Output not found")
    return FileResponse(path)

