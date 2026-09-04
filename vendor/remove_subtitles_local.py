# -*- coding: utf-8 -*-
"""Xoa subtitle cung bang mask tuong phan + OpenCV Telea inpainting.

Thiet ke de chay CPU, bo nho thap: xu ly tung frame, chi trong dai tren/duoi.
"""
import argparse
import os
import subprocess
import sys

import cv2
import numpy as np


def subtitle_band(height, position):
    if position == "top":
        return 0, max(1, int(height * 0.24))
    return int(height * 0.72), height


def text_mask(frame, position):
    """Tao mask hep quanh net chu sang/mau co tuong phan cao."""
    height, width = frame.shape[:2]
    y0, y1 = subtitle_band(height, position)
    band = frame[y0:y1]
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    local_max = cv2.dilate(gray, np.ones((5, 5), np.uint8))
    local_min = cv2.erode(gray, np.ones((5, 5), np.uint8))
    contrast = cv2.subtract(local_max, local_min)
    bright = (gray >= 175) & (contrast >= 24)
    colored = (hsv[:, :, 1] >= 95) & (hsv[:, :, 2] >= 145) & (contrast >= 20)
    seed = ((bright | colored).astype(np.uint8) * 255)

    # Subtitle thuong nam o trung tam; tranh xoa icon/UI sat hai mep.
    edge = max(2, int(width * 0.035))
    seed[:, :edge] = 0
    seed[:, width - edge:] = 0
    count, labels, stats, _ = cv2.connectedComponentsWithStats(seed, 8)
    kept = np.zeros_like(seed)
    max_height = max(5, int(height * 0.09))
    for label in range(1, count):
        x, y, w, h, area = stats[label]
        if 3 <= area <= width * max_height * 0.25 and 2 <= h <= max_height and w <= width * 0.35:
            kept[labels == label] = 255
    kept = cv2.dilate(kept, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 5)), iterations=2)
    mask = np.zeros((height, width), np.uint8)
    mask[y0:y1] = kept
    return mask


def clean_video(source, destination, position):
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        raise RuntimeError("cannot open input video")
    fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    silent = destination + ".silent.mp4"
    writer = cv2.VideoWriter(silent, cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        capture.release()
        raise RuntimeError("cannot create temporary video")
    frames = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            mask = text_mask(frame, position)
            cleaned = cv2.inpaint(frame, mask, 3, cv2.INPAINT_TELEA) if np.any(mask) else frame
            writer.write(cleaned)
            frames += 1
    finally:
        capture.release()
        writer.release()
    if frames == 0 or not os.path.exists(silent):
        raise RuntimeError("input video has no frames")
    command = ["ffmpeg", "-y", "-i", silent, "-i", source, "-map", "0:v:0", "-map", "1:a?",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy",
               "-shortest", "-movflags", "+faststart", destination]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        command[command.index("copy")] = "aac"
        result = subprocess.run(command, capture_output=True)
    try:
        os.remove(silent)
    except OSError:
        pass
    if result.returncode != 0 or not os.path.exists(destination) or os.path.getsize(destination) < 10_000:
        raise RuntimeError("ffmpeg could not restore audio")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("--position", choices=("top", "bottom"), default="bottom")
    args = parser.parse_args()
    clean_video(args.source, args.destination, args.position)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"REMOVE_SUBTITLES_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
