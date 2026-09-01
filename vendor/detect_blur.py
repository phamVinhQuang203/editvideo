#!/usr/bin/env python3
import argparse
import io
import json
import re
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


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


def extract_frame(video_path: str, frame_path: Path, second: float) -> bool:
    proc = subprocess.run(
        [
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
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
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


def merge_timed_regions(regions: list[dict], max_gap: float = 1.15) -> list[dict]:
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
    return merged


def ocr_frame_image(reader, image_path: Path, image, second: float, duration: float, y_offset: int = 0) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    if image is None:
        return [], errors
    height, width = image.shape[:2]
    frame_boxes: list[dict] = []
    try:
        detections = reader.readtext(str(image_path), detail=1, paragraph=False)
    except Exception as exc:
        errors.append(str(exc))
        return [], errors
    for points, text, confidence in detections:
        # AUTOPILOT: chữ hook TQ cách điệu OCR ra confidence thấp -> gốc lọc bỏ (<0.42),
        # KHÔNG blur. Nhưng hạ ngưỡng hết cỡ thì bắt cả RÁC (1-3 ký tự đọc nhầm conf~0).
        # Cân bằng: blur khi (NHIỀU ký tự Trung thật = text that) HOẶC (confidence đủ cao).
        _nzh = len(re.findall(r"[一-鿿]", str(text or "")))
        _conf = float(confidence or 0)
        if not ((_nzh >= 4) or _conf >= 0.42):     # ít chữ + conf thấp = rác -> bỏ
            continue
        if not is_chinese_ocr_text(text, _conf):
            continue
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
        pad = max(8, min(18, int((height + y_offset) * 0.012)))
        x1 = max(0, int(min(xs) - pad))
        y1 = max(0, int(min(ys) + y_offset - pad))
        x2 = min(width, int(max(xs) + pad))
        y2 = int(max(ys) + y_offset + pad)
        if x2 <= x1 or y2 <= y1:
            continue
        area_ratio = ((x2 - x1) * (y2 - y1)) / max(1, width * max(height + y_offset, 1))
        if area_ratio > 0.22:
            continue
        frame_boxes.append({
            "x": x1,
            "y": y1,
            "w": x2 - x1,
            "h": y2 - y1,
            "start": max(0.0, second - 0.45),
            "end": min(duration, second + 0.9),
            "confidence": round(float(confidence or 0), 3),
            "text": str(text or "")[:80],
        })
    return merge_same_frame_boxes(frame_boxes), errors


def detect(video_path: str, work_dir: Path) -> dict:
    import cv2
    import easyocr

    duration = probe_video_duration(video_path)
    frame_dir = work_dir / "ocr-frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    if duration <= 0:
        return {"ok": False, "reason": "duration_probe_failed", "regions": []}

    sample_count = max(4, min(22, int(duration / 1.25) + 1))
    step = duration / sample_count
    times = [min(duration - 0.1, 0.25 + index * step) for index in range(sample_count)]
    # AUTOPILOT: thẻ HOOK mở đầu (chữ TO giữa hình) thường CHỈ hiện ~0.0-0.3s rồi mất.
    # Mẫu cũ bắt đầu ở 0.25s -> sót hook. Thêm mẫu DÀY ngay giây đầu để luôn bắt được.
    times = sorted(set([0.0, 0.1, 0.2, 0.35, 0.55, 0.8] + times))
    try:
        import torch
        use_gpu = torch.cuda.is_available()
        reader = easyocr.Reader(["ch_sim", "en"], gpu=use_gpu, verbose=False)
    except Exception:
        use_gpu = False
        reader = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
    # Keep this bounded. EasyOCR is still memory-heavy; a dense full-video
    # subtitle pass can freeze the workstation. Most Douyin subtitle overlays
    # appear from the opening seconds, so scan that region tightly and leave the
    # rest to the sparse full-frame pass.
    subtitle_step = 0.75
    subtitle_window = min(duration, 6.5)
    subtitle_times = [min(duration - 0.1, 0.2 + index * subtitle_step) for index in range(max(1, int(subtitle_window / subtitle_step) + 1))]
    all_times = sorted(set(round(value, 2) for value in [*times, *subtitle_times]))
    regions: list[dict] = []
    errors: list[str] = []
    for index, second in enumerate(all_times):
        frame_path = frame_dir / f"ocr-{index:03d}.jpg"
        if not extract_frame(video_path, frame_path, second):
            continue
        image = cv2.imread(str(frame_path))
        if image is None:
            continue
        height, width = image.shape[:2]
        if second in subtitle_times or any(abs(second - t) < 0.01 for t in subtitle_times):
            y_start = int(height * 0.50)
            y_end = int(height * 0.90)
            crop = image[y_start:y_end, :]
            crop_path = frame_dir / f"ocr-{index:03d}-subtitle.jpg"
            cv2.imwrite(str(crop_path), crop)
            boxes, crop_errors = ocr_frame_image(reader, crop_path, crop, second, duration, y_offset=y_start)
            regions.extend(boxes)
            errors.extend(crop_errors)
        if any(abs(second - t) < 0.01 for t in times):
            boxes, frame_errors = ocr_frame_image(reader, frame_path, image, second, duration, y_offset=0)
            regions.extend(boxes)
            errors.extend(frame_errors)

    merged = merge_timed_regions(regions)
    if len(merged) > 32:
        merged = sorted(merged, key=lambda item: (float(item.get("confidence", 0)), item["w"] * item["h"]), reverse=True)[:32]
    merged = sorted(merged, key=lambda item: (item["start"], item["y"], item["x"]))
    return {
        "ok": True,
        "detector": "easyocr_ch_sim_external",
        "video": video_path,
        "duration": round(duration, 3),
        "sampleCount": sample_count,
        "subtitleSampleCount": len(subtitle_times),
        "regionCount": len(merged),
        "errors": errors[:5],
        "regions": merged,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--manifest")
    args = parser.parse_args()
    result = detect(args.video, Path(args.work_dir))
    if args.manifest:
        Path(args.manifest).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
