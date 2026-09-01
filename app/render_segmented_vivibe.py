#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render voice theo TỪNG CẢNH (timestamp) bằng giọng VIVIBE -> khớp cảnh với voice.
Dựa trên render_segmented_autovoice.py của autovoice (giữ nguyên logic timeline: đặt mỗi
đoạn voice tại adelay=start, ép tempo <= max hoặc giãn video cho vừa khung [start,end]).
KHÁC: TTS dùng Vivibe (run_vivibe_tts_with_retry) thay edge_tts. Blur đã BAKE sẵn ở ngoài."""
import argparse, json, subprocess, sys, os, time
from pathlib import Path

# lấy hàm TTS Vivibe từ renderer gốc (vendor/ trong gói)
import importlib.util
_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
sys.path.insert(0, _VENDOR)
_spec = importlib.util.spec_from_file_location("render_autovoice",
        os.path.join(_VENDOR, "render_autovoice.py"))
_ra = importlib.util.module_from_spec(_spec)
try: _spec.loader.exec_module(_ra)
except SystemExit: pass
run_vivibe_tts = _ra.run_vivibe_tts_with_retry
run_genmax_tts = _ra.run_genmax_tts_with_retry

def run(cmd):
    proc = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace",
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError("Command failed:\n" + " ".join(map(str, cmd)) + "\n" + proc.stderr)
    return proc.stdout

def duration(path):
    out = run(["ffprobe","-hide_banner","-v","error","-show_entries","format=duration",
               "-of","default=nk=1:nw=1", str(path)]).strip()
    return float(out)

def atempo_chain(factor):
    parts = []
    while factor > 2.0: parts.append("atempo=2.0"); factor /= 2.0
    while factor < 0.5: parts.append("atempo=0.5"); factor /= 0.5
    parts.append(f"atempo={factor:.4f}")
    return ",".join(parts)

def tts_segment(text, voice_id, raw_path, speed):
    raw_path.unlink(missing_ok=True)
    provider = (os.environ.get("AUTOVOICE_TTS_PROVIDER") or "vivibe").strip().lower()
    if provider == "genmax":
        run_genmax_tts(_ra.normalize_genmax_tts(text), voice_id, raw_path, attempts=3)
    else:
        run_vivibe_tts(text, voice_id, raw_path, speed, attempts=3)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--segments", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--voice", required=True)
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--voice-volume", type=float, default=1.4)
    p.add_argument("--max-tempo", type=float, default=1.18)
    p.add_argument("--max-video-slowdown", type=float, default=1.30)
    p.add_argument("--allow-video-slowdown", action="store_true")
    a = p.parse_args()

    video = Path(a.video); output = Path(a.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    work = output.with_suffix(""); work.mkdir(parents=True, exist_ok=True)
    segments = json.loads(Path(a.segments).read_text(encoding="utf-8"))
    video_dur = duration(video)

    prepared = []; slow_scale = 1.0
    for i, seg in enumerate(segments):
        start = max(0.0, float(seg["start"]))
        end = max(start + 0.5, float(seg.get("end", start + 4)))
        text = " ".join(str(seg["text"]).split())
        if not text: continue
        raw = work / f"seg-{i:02d}-raw.mp3"; final = work / f"seg-{i:02d}.m4a"
        tts_segment(text, a.voice, raw, a.speed)
        raw_dur = duration(raw)
        budget = max(0.5, end - start - 0.2)
        if a.allow_video_slowdown and a.max_tempo > 0:
            slow_scale = max(slow_scale, raw_dur / (budget * a.max_tempo))
        prepared.append({"index": i, "start": start, "end": end, "raw": raw,
                         "final": final, "raw_dur": raw_dur})

    if not a.allow_video_slowdown: slow_scale = 1.0
    slow_scale = max(1.0, min(float(slow_scale), max(1.0, float(a.max_video_slowdown or 1.0))))

    inputs = ["ffmpeg","-hide_banner","-y","-i",str(video)]
    filter_parts = []; mix_inputs = []
    for it in prepared:
        idx = it["index"]; start = it["start"]*slow_scale; end = it["end"]*slow_scale
        budget = max(0.5, end - start - 0.2)
        tempo = max(1.0, it["raw_dur"]/budget)
        if a.max_tempo > 0: tempo = min(tempo, a.max_tempo)
        af = f"{atempo_chain(tempo)},volume=1.75" if tempo > 1.01 else "volume=1.75"
        run(["ffmpeg","-hide_banner","-y","-i",str(it["raw"]),"-filter:a",af,"-ar","44100",str(it["final"])])
        inputs += ["-i", str(it["final"])]
        si = len(mix_inputs) + 1
        delay = int(start*1000)
        filter_parts.append(f"[{si}:a]adelay={delay}|{delay},volume={a.voice_volume}[v{idx}]")
        mix_inputs.append(f"[v{idx}]")

    current_video = "0:v"
    if slow_scale > 1.001:
        filter_parts.append(f"[0:v]setpts={slow_scale:.6f}*PTS[vslow]"); current_video = "vslow"

    if not mix_inputs:
        raise RuntimeError("Khong co doan voice nao")
    if len(mix_inputs) == 1:
        filter_parts.append(f"{mix_inputs[0]}alimiter=limit=0.95[a]")
    else:
        filter_parts.append("".join(mix_inputs) +
            f"amix=inputs={len(mix_inputs)}:duration=longest:dropout_transition=0,alimiter=limit=0.95[a]")

    mapped_video = "[vslow]" if slow_scale > 1.001 else "0:v:0"
    cmd = inputs + ["-filter_complex",";".join(filter_parts),"-map",mapped_video,"-map","[a]",
        "-c:v","libx264" if slow_scale>1.001 else "copy",
        *([] if slow_scale<=1.001 else ["-preset","veryfast","-crf","20"]),
        "-c:a","aac","-b:a","128k","-movflags","+faststart", str(output)]
    run(cmd)
    print(json.dumps({"ok": True, "output": str(output), "segments": len(prepared),
                      "slow": round(slow_scale,3)}, ensure_ascii=False))

if __name__ == "__main__":
    main()
