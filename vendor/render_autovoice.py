#!/usr/bin/env python3
import argparse
import json
import math
import os
import random
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


VOICE_MAP = {
    "male": "vi-VN-NamMinhNeural",
    "female": "vi-VN-HoaiMyNeural",
}

VIVIBE_RPC_URL = "https://api.lucylab.io/json-rpc"
GENMAX_API_URL = "https://api.genmax.io"

def video_encode_args(quality: int = 20) -> list[str]:
    if os.environ.get("VS_VIDEO_ENCODER", "cpu").strip().lower() == "qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", str(quality + 2)]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(quality)]

def normalize_vietnamese_tts(text: str) -> str:
    """Chuẩn hoá cách đọc keyword và giảm khoảng nghỉ máy móc trước khi gọi TTS."""
    text = str(text or "")
    # Keyword sản phẩm/giá thường bị engine đọc dính hoặc đọc theo tiếng Anh.
    text = re.sub(r"(?i)\b3\s*in\s*1\b", "ba in một", text)
    text = re.sub(r"(?i)\bTIAN\b", "Ti An", text)
    text = re.sub(r"(?i)\b(\d[\d.,]*)\s*K\b", r"\1 nghìn", text)
    # Các video tham chiếu dùng emotion tags của ElevenLabs. Vivibe không có
    # cùng cú pháp, nên đổi các tag có âm thanh thành tiếng đệm tự nhiên và
    # bỏ tag chỉ dẫn để tránh engine đọc nguyên tiếng Anh.
    tag_sounds = {
        "laughing": "Ha ha ha! ", "giggles": "Hì hì! ",
        "clears throat": "Ừm, ", "sighs": "Haiz, ",
        "coughs": "Khụ khụ, ", "questioning": "Ủa? ",
        "amazed": "Trời ơi! ", "surprised": "Ủa! ",
    }
    for tag, spoken in tag_sounds.items():
        text = re.sub(rf"(?i)\[{re.escape(tag)}\]", spoken, text)
    text = re.sub(
        r"(?i)\[(?:excited|speaking fast|speaking slowly|whispering|shouting|"
        r"angry|annoyed|confused|screams|sniffles|groan|exhausted|stuttering)\]",
        "", text,
    )
    # Vivibe nghỉ khá lâu ở xuống dòng/dấu chấm. Giữ câu hỏi, cảm thán; nối các
    # câu kể bằng dấu phẩy để giọng liền mạch và nghỉ rất ngắn.
    text = re.sub(r"[\r\n]+", ", ", text)
    text = re.sub(r"\.{2,}", ", ", text)
    text = re.sub(r"[;:]+", ",", text)
    text = re.sub(r"\.\s+(?=\S)", ", ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"(?:,\s*){2,}", ", ", text)
    return re.sub(r"\s+", " ", text).strip(" ,")

def normalize_genmax_tts(text: str) -> str:
    """Chuẩn hoá phát âm nhưng giữ emotion tags cho Eleven v3 của GenMax."""
    text = str(text or "")
    text = re.sub(r"(?i)\b3\s*in\s*1\b", "ba in một", text)
    text = re.sub(r"(?i)\bTIAN\b", "Ti An", text)
    text = re.sub(r"(?i)\b(\d[\d.,]*)\s*K\b", r"\1 nghìn", text)
    text = re.sub(r"[\r\n]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def urlopen_with_ssl_fallback(request, timeout):
    """Retry only certificate-chain failures seen on older Windows installs."""
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc) and "Basic Constraints" not in str(exc):
            raise
        return urllib.request.urlopen(request, timeout=timeout,
                                      context=ssl._create_unverified_context())

VIVIBE_MALE_KEYWORDS = [
    "cơ khí", "co khi", "kỹ thuật", "ky thuat", "dụng cụ", "dung cu",
    "dao", "khoan", "khóa", "khoa", "máy", "điện", "dien", "phun",
    "bơm", "bom", "cưa", "racing",
]

VIVIBE_FEMALE_KEYWORDS = [
    "decor", "trang trí", "trang tri", "gia dụng", "giadung", "nhà bếp",
    "nha bep", "góc bếp", "goc bep", "goc-", "bep-", "lavabo", "phòng tắm",
    "phong tam", "nha-tam", "ban công", "ban cong", "ban-cong", "phơi",
    "phoi", "phoi-", "kệ", "ke-", "móc", "moc", "moc-", "tủ", "tu-",
    "chén", "chen", "chen-", "nồi", "noi", "noi-", "thảm", "tham",
    "tham-",
]



def run(cmd, timeout=None):
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Command timed out:\n" + " ".join(map(str, cmd))) from exc
    if proc.returncode != 0:
        raise RuntimeError("Command failed:\n" + " ".join(map(str, cmd)) + "\n" + proc.stderr)
    return proc.stdout


def run_tts_with_retry(cmd, raw_voice: Path, attempts: int = 3):
    last_error = None
    for index in range(1, attempts + 1):
        try:
            raw_voice.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            run(cmd, timeout=90)
            if raw_voice.exists() and raw_voice.stat().st_size > 1024:
                return
            last_error = RuntimeError(f"TTS produced an empty audio file: {raw_voice}")
        except Exception as exc:
            last_error = exc
        if index < attempts:
            time.sleep(2 * index)
    raise RuntimeError(f"TTS failed after {attempts} attempts: {last_error}")


def load_env_file(file_path: Path):
    try:
        for line in file_path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            runtime_prefixes = ("AUTOVOICE_", "VIVIBE_", "LOCAL_F5_", "TTS_")
            if key and (key not in os.environ or key.startswith(runtime_prefixes)):
                os.environ[key] = value
    except Exception:
        pass


def load_runtime_env():
    candidates = [
        Path.cwd() / ".env",
        Path("E:/CODEX/Auto FB/.env"),
        Path("E:/CODEX/autovoice-tool/.env"),
    ]
    for candidate in candidates:
        load_env_file(candidate)


def vivibe_voice_pool():
    raw = os.environ.get("AUTOVOICE_VIVIBE_VOICE_IDS") or os.environ.get("VIVIBE_VOICE_IDS") or ""
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def vivibe_group_pool(group: str):
    key = f"AUTOVOICE_VIVIBE_{group.upper()}_VOICE_IDS"
    raw = os.environ.get(key) or os.environ.get(f"VIVIBE_{group.upper()}_VOICE_IDS") or ""
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def select_vivibe_voice(requested_voice: str, script: Path, output: Path) -> str:
    requested = str(requested_voice or "").strip()
    pool = vivibe_voice_pool()
    if requested and requested not in VOICE_MAP and requested.lower() not in {"auto", "random", "none"}:
        return requested
    if not pool:
        raise RuntimeError("Vivibe TTS is enabled but AUTOVOICE_VIVIBE_VOICE_IDS is empty.")
    seed = f"{script}|{output}|{time.time_ns()}|{os.getpid()}"
    rng = random.Random(seed)
    male_pool = vivibe_group_pool("male")
    female_pool = vivibe_group_pool("female")
    requested_key = requested.lower()
    if requested_key == "male" and male_pool:
        return rng.choice(male_pool)
    if requested_key == "female" and female_pool:
        return rng.choice(female_pool)
    text = " ".join([
        str(script),
        str(output),
        script.read_text(encoding="utf-8", errors="replace") if script.exists() else "",
    ]).lower()
    if male_pool and any(token in text for token in VIVIBE_MALE_KEYWORDS):
        pool = male_pool
    elif female_pool and any(token in text for token in VIVIBE_FEMALE_KEYWORDS):
        pool = female_pool
    return rng.choice(pool)


def local_f5_profile_root() -> Path:
    return Path(os.environ.get("AUTOVOICE_LOCAL_F5_PROFILE_DIR") or "E:/CODEX/Auto FB/_style-learning/vivibe-local-f5")


def local_f5_voice_pool():
    raw = os.environ.get("AUTOVOICE_LOCAL_F5_PROFILE_IDS") or os.environ.get("LOCAL_F5_PROFILE_IDS") or ""
    if raw.strip():
        return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]
    root = local_f5_profile_root()
    if not root.exists():
        return []
    return sorted([item.name for item in root.iterdir() if item.is_dir()])


def select_local_f5_profile(requested_voice: str, script: Path, output: Path) -> str:
    requested = str(requested_voice or "").strip()
    pool = local_f5_voice_pool()
    if requested and requested not in VOICE_MAP and requested.lower() not in {"auto", "random", "none"}:
        return requested
    if not pool:
        raise RuntimeError("Local F5 TTS is enabled but no voice profiles were found.")
    seed = f"{script}|{output}|{time.time_ns()}|{os.getpid()}"
    rng = random.Random(seed)
    return rng.choice(pool)


def read_local_f5_profile(profile_id: str) -> dict:
    root = local_f5_profile_root()
    profile_dir = root / profile_id
    if not (profile_dir / "profile.json").exists() and root.exists():
        requested = str(profile_id or "").strip().lower()
        for candidate in root.iterdir():
            candidate_profile = candidate / "profile.json"
            if not candidate.is_dir() or not candidate_profile.exists():
                continue
            try:
                data = json.loads(candidate_profile.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            names = {
                str(data.get("id") or "").strip().lower(),
                str(data.get("slug") or "").strip().lower(),
                str(data.get("name") or "").strip().lower(),
                str(data.get("displayName") or "").strip().lower(),
                candidate.name.lower(),
            }
            if requested in names:
                profile_dir = candidate
                break
    profile_path = profile_dir / "profile.json"
    if not profile_path.exists():
        raise RuntimeError(f"Missing local F5 profile: {profile_path}")
    data = json.loads(profile_path.read_text(encoding="utf-8", errors="replace"))
    data["_profile_dir"] = str(profile_dir)
    return data


def path_from_profile(profile: dict, key: str) -> Path:
    profile_dir = Path(profile["_profile_dir"])
    value = str(profile.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"Local F5 profile missing {key}: {profile_dir}")
    path = Path(value)
    if not path.is_absolute():
        path = profile_dir / path
    return path


def run_local_f5_tts(text: str, profile_id: str, raw_voice: Path, attempts: int = 1):
    profile = read_local_f5_profile(profile_id)
    ref_audio = path_from_profile(profile, "ref_audio")
    ref_text_path = path_from_profile(profile, "ref_text")
    ref_text = ref_text_path.read_text(encoding="utf-8", errors="replace").strip()
    if not ref_audio.exists():
        raise RuntimeError(f"Missing local F5 ref audio: {ref_audio}")
    if not ref_text:
        raise RuntimeError(f"Missing local F5 ref text: {ref_text_path}")

    gpu_url = (os.environ.get("LOCAL_F5_SERVER_URL") or "").strip().rstrip("/")
    if gpu_url:
        slug = str(profile.get("slug") or profile_id).strip()
        gpu_ref_audio = str(profile.get("gpu_ref_audio") or f"C:/voice-server/voices/{slug}/reference.wav")
        output_file = str(profile.get("gpu_output_file") or f"outputs/autovoice-{profile_id}-{int(time.time() * 1000)}.wav")
        payload = json.dumps({
            "text": text,
            "ref_audio": gpu_ref_audio,
            "ref_text": ref_text,
            "speed": float(profile.get("speed") or os.environ.get("LOCAL_F5_SPEED") or 1.0),
            "nfe_step": int(profile.get("nfe_step") or os.environ.get("LOCAL_F5_NFE_STEP") or 8),
            "output_file": output_file,
            "return_file": False,
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{gpu_url}/generate",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
            method="POST",
        )
        timeout = max(60, int(float(os.environ.get("LOCAL_F5_TIMEOUT_SECONDS", "900") or 900)))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8", errors="replace"))
        if not result.get("ok") or not result.get("path"):
            raise RuntimeError(f"GPU F5 failed: {result}")
        remote_path = str(result["path"]).replace("\\", "/").replace("C:/voice-server/", "")
        download_url_remote = f"{gpu_url}/download?path={urllib.parse.quote(remote_path)}"
        download_url(download_url_remote, raw_voice)
        return {"profileId": profile_id, "engine": "F5-TTS-Vietnamese-GPU", "gpuPath": result.get("path", "")}

    f5_cli = Path(os.environ.get("LOCAL_F5_CLI") or "E:/CODEX/voice-local/.venv/Scripts/f5-tts_infer-cli.exe")
    f5_repo = Path(os.environ.get("LOCAL_F5_REPO") or "E:/CODEX/voice-local/F5-TTS-Vietnamese")
    ckpt = Path(os.environ.get("LOCAL_F5_CKPT") or "E:/CODEX/voice-local/models/F5-TTS-Vietnamese-ViVoice/model_last.pt")
    vocab = Path(os.environ.get("LOCAL_F5_VOCAB") or "E:/CODEX/voice-local/models/F5-TTS-Vietnamese-ViVoice/vocab.txt")
    if not f5_cli.exists():
        raise RuntimeError(f"Missing F5 CLI: {f5_cli}")
    if not ckpt.exists() or not vocab.exists():
        raise RuntimeError(f"Missing F5 model files: {ckpt} / {vocab}")

    out_dir = raw_voice.parent
    out_file = raw_voice.name
    speed = str(profile.get("speed") or os.environ.get("LOCAL_F5_SPEED") or "1.0")
    nfe_step = str(profile.get("nfe_step") or os.environ.get("LOCAL_F5_NFE_STEP") or "8")
    timeout = max(60, int(float(os.environ.get("LOCAL_F5_TIMEOUT_SECONDS", "600") or 600)))
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTORCH_CUDA_ALLOC_CONF": os.environ.get("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True"),
    }
    cmd = [
        str(f5_cli),
        "--model", "F5TTS_Base",
        "--ref_audio", str(ref_audio),
        "--ref_text", ref_text,
        "--gen_text", text,
        "--speed", speed,
        "--nfe_step", nfe_step,
        "--vocoder_name", "vocos",
        "--load_vocoder_from_local",
        "--vocab_file", str(vocab),
        "--ckpt_file", str(ckpt),
        "--output_dir", str(out_dir),
        "--output_file", out_file,
    ]
    last_error = None
    for index in range(1, attempts + 1):
        raw_voice.unlink(missing_ok=True)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(f5_repo),
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
            if proc.returncode == 0 and raw_voice.exists() and raw_voice.stat().st_size > 1024:
                return {"profileId": profile_id, "engine": "F5-TTS-Vietnamese"}
            last_error = RuntimeError((proc.stderr or proc.stdout or "Local F5 did not produce audio")[-3000:])
        except Exception as exc:
            last_error = exc
        if index < attempts:
            time.sleep(2 * index)
    raise RuntimeError(f"Local F5 TTS failed after {attempts} attempts: {last_error}")


def vivibe_rpc(method: str, input_data: dict, api_key: str):
    payload = json.dumps({"method": method, "input": input_data}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        VIVIBE_RPC_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 AutoVoice/1.0",
        },
        method="POST",
    )
    try:
        with urlopen_with_ssl_fallback(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Vivibe API HTTP {exc.code}: {detail}") from exc
    data = json.loads(body)
    if data.get("error"):
        raise RuntimeError(f"Vivibe API error: {data['error']}")
    return data.get("result") or {}


def download_url(url: str, target: Path):
    request = urllib.request.Request(url, headers={"User-Agent": "AutoVoice/1.0"})
    with urlopen_with_ssl_fallback(request, timeout=60) as response:
        content = response.read()
    if len(content) < 1024:
        raise RuntimeError(f"Vivibe audio download is too small: {len(content)} bytes")
    target.write_bytes(content)


def run_vivibe_tts_with_retry(text: str, voice_id: str, raw_voice: Path, speed: float, attempts: int = 3):
    api_key = os.environ.get("VIVIBE_API_KEY") or os.environ.get("LUCYLAB_API_KEY") or ""
    if not api_key:
        raise RuntimeError("Missing VIVIBE_API_KEY.")
    poll_interval = max(0.5, float(os.environ.get("VIVIBE_POLL_INTERVAL_SECONDS", "2.5") or 2.5))
    timeout_seconds = max(30.0, float(os.environ.get("VIVIBE_TIMEOUT_SECONDS", "180") or 180))
    last_error = None
    for index in range(1, attempts + 1):
        try:
            raw_voice.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            start = vivibe_rpc("ttsLongText", {"text": text, "userVoiceId": voice_id, "speed": speed}, api_key)
            export_id = start.get("projectExportId") or start.get("id") or ""
            if not export_id:
                raise RuntimeError(f"Vivibe did not return projectExportId: {start}")
            deadline = time.time() + timeout_seconds
            status = {}
            while time.time() < deadline:
                status = vivibe_rpc("getExportStatus", {"projectExportId": export_id}, api_key)
                state = str(status.get("state") or "").lower()
                if state == "completed":
                    break
                if state == "failed":
                    raise RuntimeError(f"Vivibe export failed: {status}")
                time.sleep(poll_interval)
            if str(status.get("state") or "").lower() != "completed" or not status.get("url"):
                raise RuntimeError(f"Vivibe export timed out: {status}")
            download_url(status["url"], raw_voice)
            if raw_voice.exists() and raw_voice.stat().st_size > 1024:
                return {"exportId": export_id, "url": status.get("url", ""), "srtUrl": status.get("srtUrl", "")}
            last_error = RuntimeError(f"Vivibe produced an empty audio file: {raw_voice}")
        except Exception as exc:
            last_error = exc
        if index < attempts:
            time.sleep(2 * index)
    raise RuntimeError(f"Vivibe TTS failed after {attempts} attempts: {last_error}")

def run_genmax_tts_with_retry(text: str, voice_id: str, raw_voice: Path, attempts: int = 3):
    api_key = os.environ.get("GENMAX_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing GENMAX_API_KEY.")
    voice_id = str(voice_id).strip()
    if voice_id.startswith("Vietnamese_"):
        upstream_provider = "minimax"
    else:
        upstream_provider = "capcut" if voice_id.isdigit() else "elevenlabs"
    if upstream_provider == "capcut":
        model = "capcut"
    elif upstream_provider == "minimax":
        model = os.environ.get("GENMAX_MINIMAX_MODEL", "speech-2.6-hd")
    else:
        model = os.environ.get("GENMAX_MODEL", "eleven_v3")
    speed = float(os.environ.get("GENMAX_SPEED", "1.05") or 1.05)
    if upstream_provider in {"capcut", "minimax"}:
        voice_settings = {"speed": speed, "pitch": 0}
    else:
        voice_settings = {
            "stability": 0.5, "similarity_boost": 0.82, "style": 0.55,
            "use_speaker_boost": True, "speed": speed,
        }
    # MiniMax lấy ngữ điệu từ dấu câu; các tag kiểu ElevenLabs có thể bị đọc thành lời.
    tts_text = re.sub(r"\[(?:[^\]\r\n]{1,40})\]\s*", "", text) if upstream_provider == "minimax" else text
    payload = {
        "text": tts_text, "model_id": model, "provider": upstream_provider,
        "language_code": "vi", "voice_settings": voice_settings,
    }
    headers = {
        "xi-api-key": api_key, "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "VideoStudio/1.0",
    }
    last_error = None
    for index in range(1, attempts + 1):
        try:
            raw_voice.unlink(missing_ok=True)
            request = urllib.request.Request(
                f"{GENMAX_API_URL}/v1/text-to-speech/{voice_id}",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers, method="POST",
            )
            with urlopen_with_ssl_fallback(request, timeout=60) as response:
                task = json.loads(response.read().decode("utf-8"))
            task_id = task.get("id")
            if not task_id:
                raise RuntimeError(f"GenMax did not return task id: {task}")
            deadline = time.time() + 300
            while time.time() < deadline:
                status_req = urllib.request.Request(
                    f"{GENMAX_API_URL}/v1/history/{task_id}", headers=headers)
                with urlopen_with_ssl_fallback(status_req, timeout=30) as response:
                    status = json.loads(response.read().decode("utf-8"))
                state = status.get("status")
                if state == "completed":
                    audio_url = (status.get("result") or {}).get("audio_url")
                    if not audio_url:
                        raise RuntimeError("GenMax completed without audio_url")
                    download_url(audio_url, raw_voice)
                    if raw_voice.exists() and raw_voice.stat().st_size > 1024:
                        return {"taskId": task_id, "url": audio_url, "model": model,
                                "upstreamProvider": upstream_provider}
                    raise RuntimeError("GenMax produced an empty audio file")
                if state in {"failed", "error"}:
                    raise RuntimeError(status.get("detail_error") or status.get("error") or state)
                time.sleep(1.5)
            raise TimeoutError("GenMax TTS timed out")
        except Exception as exc:
            last_error = exc
            if index < attempts:
                time.sleep(2 * index)
    raise RuntimeError(f"GenMax TTS failed after {attempts} attempts: {last_error}")


def generate_tts(script: Path, output: Path, requested_voice: str, rate: str):
    provider = (os.environ.get("AUTOVOICE_TTS_PROVIDER") or os.environ.get("TTS_PROVIDER") or "edge").strip().lower()
    vivibe_speed = float(os.environ.get("VIVIBE_SPEED", "1.0") or 1.0)
    attempts = max(1, int(os.environ.get("TTS_ATTEMPTS", "3") or 3))
    if provider == "genmax":
        raw_voice = output.with_suffix(".voice-raw.mp3")
        text = normalize_genmax_tts(script.read_text(encoding="utf-8", errors="replace"))
        if not text:
            raise RuntimeError(f"Missing voice text: {script}")
        # engine.py đã đổi lựa chọn trên giao diện thành voice ID thật. Chỉ
        # dùng cấu hình mặc định khi caller gửi auto/tên không hợp lệ.
        requested = str(requested_voice or "").strip()
        voice_id = (requested if re.fullmatch(r"[A-Za-z0-9_-]{16,}", requested)
                    else os.environ.get("GENMAX_VOICE_ID", "pNInz6obpgDQGcFmaJgB").strip())
        meta = run_genmax_tts_with_retry(text, voice_id, raw_voice, attempts=attempts)
        return raw_voice, {"provider": "genmax", "voiceId": voice_id, **meta}
    if provider in {"local_f5", "f5", "f5_local"}:
        raw_voice = output.with_suffix(".voice-raw.wav")
        text = normalize_vietnamese_tts(script.read_text(encoding="utf-8", errors="replace"))
        if not text:
            raise RuntimeError(f"Missing voice text: {script}")
        profile_id = select_local_f5_profile(requested_voice, script, output)
        try:
            meta = run_local_f5_tts(text, profile_id, raw_voice, attempts=max(1, min(attempts, 2)))
            return raw_voice, {"provider": "local_f5", "voiceId": profile_id, **meta}
        except Exception:
            fallback = (os.environ.get("AUTOVOICE_TTS_FALLBACK") or "vivibe").strip().lower()
            if fallback not in {"vivibe", "edge"}:
                raise
            print(f"AUTOVOICE_PROGRESS tts: Local F5 failed, fallback {fallback}", flush=True)
            if fallback == "edge":
                provider = "edge"
            else:
                provider = "vivibe"

    if provider == "vivibe":
        raw_voice = output.with_suffix(".voice-raw.wav")
        text = normalize_vietnamese_tts(script.read_text(encoding="utf-8", errors="replace"))
        if not text:
            raise RuntimeError(f"Missing voice text: {script}")
        voice_id = select_vivibe_voice(requested_voice, script, output)
        try:
            meta = run_vivibe_tts_with_retry(text, voice_id, raw_voice, vivibe_speed, attempts=attempts)
            return raw_voice, {"provider": "vivibe", "voiceId": voice_id, **meta}
        except Exception:
            if (os.environ.get("AUTOVOICE_TTS_FALLBACK") or "edge").strip().lower() != "edge":
                raise
            print("AUTOVOICE_PROGRESS tts: Vivibe failed, fallback Edge TTS", flush=True)

    voice_id = VOICE_MAP.get(requested_voice)
    if not voice_id:
        # requested_voice có thể là ID riêng của Vivibe; Edge không hiểu ID đó.
        voice_id = "vi-VN-NamMinhNeural"
    raw_voice = output.with_suffix(".voice-raw.mp3")
    normalized_script = output.with_suffix(".voice-normalized.txt")
    normalized_script.write_text(
        normalize_vietnamese_tts(script.read_text(encoding="utf-8", errors="replace")),
        encoding="utf-8",
    )
    run_tts_with_retry([
        sys.executable, "-m", "edge_tts",
        "--voice", voice_id,
        "--rate", rate,
        "--file", str(normalized_script),
        "--write-media", str(raw_voice),
    ], raw_voice, attempts=attempts)
    return raw_voice, {"provider": "edge", "voiceId": voice_id}




def duration(path):
    out = run([
        "ffprobe", "-hide_banner", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nk=1:nw=1",
        str(path),
    ]).strip()
    return float(out)


def has_audio(path):
    out = run([
        "ffprobe", "-hide_banner", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "json",
        str(path),
    ])
    return bool(json.loads(out).get("streams"))


def extend_video_if_short(video: Path, trim_start: float, min_duration: float = 15.0, threshold: float = 10.0):
    source_duration = max(0.5, duration(video) - trim_start)
    if source_duration >= threshold:
        return video, trim_start, source_duration, None

    extended = video.with_name(f"{video.stem}.extended-15s{video.suffix}")
    loop_count = max(1, math.ceil(min_duration / max(source_duration, 0.5)))
    cmd = ["ffmpeg", "-hide_banner", "-y"]
    if trim_start > 0:
        cmd += ["-ss", f"{trim_start:.3f}"]
    cmd += [
        "-stream_loop", str(loop_count),
        "-i", str(video),
        "-t", f"{min_duration:.3f}",
        *video_encode_args(),
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(extended),
    ]
    run(cmd)
    return extended, 0.0, min_duration, extended


def atempo_chain(factor):
    parts = []
    while factor > 2.0:
        parts.append("atempo=2.0")
        factor /= 2.0
    while factor < 0.5:
        parts.append("atempo=0.5")
        factor /= 0.5
    parts.append(f"atempo={factor:.4f}")
    return ",".join(parts)


def parse_blur_box(value):
    parts = [part.strip() for part in str(value or "").split(":")]
    if len(parts) not in {4, 6}:
        raise ValueError("Blur box must be x:y:w:h or x:y:w:h:start:end")
    if len(parts) == 4:
        parts += ["", ""]
    return parts


def main():
    load_runtime_env()
    parser = argparse.ArgumentParser(description="Generate Vietnamese TTS and mix it into a product video.")
    parser.add_argument("--video", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--voice", default="male")
    parser.add_argument("--rate", default="+30%")
    parser.add_argument("--original-volume", type=float, default=0.04)
    parser.add_argument("--music")
    parser.add_argument("--music-volume", type=float, default=0.07)
    parser.add_argument("--delay-ms", type=int, default=180)
    parser.add_argument("--voice-volume", type=float, default=1.35)
    parser.add_argument("--trim-start", type=float, default=0.0, help="Trim seconds from the beginning of the source video before rendering.")
    parser.add_argument(
        "--blur-region",
        action="append",
        default=[],
        help="Blur a video rectangle. Format x:y:w:h or x:y:w:h:start:end. Can be repeated.",
    )
    args = parser.parse_args()

    video = Path(args.video)
    script = Path(args.script)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    final_voice = output.with_suffix(".voice-final.m4a")
    raw_voice, tts_meta = generate_tts(script, output, args.voice, args.rate)

    trim_start = max(0.0, float(args.trim_start or 0.0))
    render_video, render_trim_start, video_dur, extended_video = extend_video_if_short(video, trim_start)
    voice_dur = duration(raw_voice)
    budget = max(0.5, video_dur - (args.delay_ms / 1000.0) - 0.25)
    requested_tempo = max(1.0, voice_dur / budget)
    tempo = requested_tempo
    max_tempo = 0.0
    if str(tts_meta.get("provider") or "").lower() == "local_f5":
        max_tempo = float(os.environ.get("LOCAL_F5_MAX_TEMPO", "1.18") or 1.18)
        tempo = min(tempo, max_tempo)
    audio_filter = f"{atempo_chain(tempo)},volume=1.75" if tempo > 1.01 else "volume=1.75"

    run([
        "ffmpeg", "-hide_banner", "-y",
        "-i", str(raw_voice),
        "-filter:a", audio_filter,
        "-ar", "44100",
        str(final_voice),
    ])

    inputs = ["ffmpeg", "-hide_banner", "-y"]
    if render_trim_start > 0:
        inputs += ["-ss", f"{render_trim_start:.3f}"]
    inputs += ["-i", str(render_video), "-i", str(final_voice)]
    input_count = 2
    if args.music:
        inputs += ["-i", str(Path(args.music))]
        input_count += 1

    filters = []
    mix_inputs = []
    blur_regions = [str(item or "").strip() for item in (args.blur_region or []) if str(item or "").strip()]
    if blur_regions:
        current = "0:v"
        for index, blur_region in enumerate(blur_regions):
            x, y, w, h, start, end = parse_blur_box(blur_region)
            overlay_x = x.replace("iw", "main_w").replace("ih", "main_h")
            overlay_y = y.replace("iw", "main_w").replace("ih", "main_h")
            out_label = "v" if index == len(blur_regions) - 1 else f"vb{index}"
            enable = f":enable='between(t,{float(start):.3f},{float(end):.3f})'" if start and end else ""
            filters.append(
                f"[{current}]split=2[vbase{index}][vblur{index}];"
                f"[vblur{index}]crop=w={w}:h={h}:x={x}:y={y},boxblur=14:1[vpatch{index}];"
                f"[vbase{index}][vpatch{index}]overlay=x={overlay_x}:y={overlay_y}{enable}[{out_label}]"
            )
            current = out_label
    if has_audio(render_video) and args.original_volume > 0:
        filters.append(f"[0:a]volume={args.original_volume}[a0]")
        mix_inputs.append("[a0]")
    filters.append(f"[1:a]adelay={args.delay_ms}|{args.delay_ms},volume={args.voice_volume}[a1]")
    mix_inputs.append("[a1]")
    if args.music:
        filters.append(f"[2:a]volume={args.music_volume}[a2]")
        mix_inputs.append("[a2]")

    if len(mix_inputs) == 1:
        filters.append(f"{mix_inputs[0]}alimiter=limit=0.95[a]")
    else:
        filters.append("".join(mix_inputs) + f"amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=0,alimiter=limit=0.95[a]")

    cmd = inputs + [
        "-filter_complex", ";".join(filters),
        "-map", "[v]" if blur_regions else "0:v:0",
        "-map", "[a]",
        *(video_encode_args() if blur_regions else ["-c:v", "copy"]),
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output),
    ]
    run(cmd)

    for temp in (raw_voice, final_voice, extended_video):
        if not temp:
            continue
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass

    print(json.dumps({
        "output": str(output),
        "voice": str(final_voice),
        "tts_provider": tts_meta.get("provider", ""),
        "tts_voice_id": tts_meta.get("voiceId", ""),
        "tts_export_id": tts_meta.get("exportId", ""),
        "video_duration": round(video_dur, 3),
        "extended_to_minimum": bool(extended_video),
        "raw_voice_duration": round(voice_dur, 3),
        "tempo": round(tempo, 3),
        "requested_tempo": round(requested_tempo, 3),
        "tempo_limited": bool(max_tempo and requested_tempo > max_tempo),
        "max_tempo": round(max_tempo, 3) if max_tempo else 0,
        "trim_start": round(trim_start, 3),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
