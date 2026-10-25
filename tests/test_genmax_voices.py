import os
import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))

import render_autovoice as renderer


class GenMaxVoiceTests(unittest.TestCase):
    def test_minimax_voice_uses_minimax_payload_and_keeps_natural_punctuation(self):
        class Response:
            def __init__(self, data):
                self.data = data
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self):
                return json.dumps(self.data).encode()

        requests = []
        def open_request(request, timeout):
            requests.append(request)
            if "/v1/text-to-speech/" in request.full_url:
                return Response({"id": "minimax-task"})
            return Response({"status": "completed", "result": {"audio_url": "https://audio.test/minimax.mp3"}})

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "voice.mp3"
            with mock.patch.dict(os.environ, {"GENMAX_API_KEY": "test-key"}), \
                 mock.patch.object(renderer, "urlopen_with_ssl_fallback", side_effect=open_request), \
                 mock.patch.object(renderer, "download_url", side_effect=lambda _url, dst: Path(dst).write_bytes(b"a" * 2048)):
                meta = renderer.run_genmax_tts_with_retry(
                    "[storytelling] Khoan đã... món này, tiện thật!",
                    "Vietnamese_Serene_Man", output, attempts=1)

        payload = json.loads(requests[0].data)
        self.assertEqual("minimax", payload["provider"])
        self.assertEqual("speech-2.6-hd", payload["model_id"])
        self.assertEqual({"speed": 1.05, "pitch": 0}, payload["voice_settings"])
        self.assertEqual("Khoan đã... món này, tiện thật!", payload["text"])
        self.assertEqual("minimax", meta["upstreamProvider"])

    def test_capcut_voice_uses_capcut_payload(self):
        class Response:
            def __init__(self, data):
                self.data = data
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self):
                return json.dumps(self.data).encode()

        requests = []
        def open_request(request, timeout):
            requests.append(request)
            if request.full_url.endswith("/v1/text-to-speech/7491608109043945985"):
                return Response({"id": "capcut-task"})
            return Response({"status": "completed", "result": {"audio_url": "https://audio.test/a.mp3"}})

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "voice.mp3"
            def download(_url, destination):
                Path(destination).write_bytes(b"a" * 2048)
            with mock.patch.dict(os.environ, {"GENMAX_API_KEY": "test-key"}), \
                 mock.patch.object(renderer, "urlopen_with_ssl_fallback", side_effect=open_request), \
                 mock.patch.object(renderer, "download_url", side_effect=download):
                meta = renderer.run_genmax_tts_with_retry(
                    "Xin chào", "7491608109043945985", output, attempts=1)

        payload = json.loads(requests[0].data)
        self.assertEqual("capcut", payload["provider"])
        self.assertEqual("capcut", payload["model_id"])
        self.assertEqual({"speed": 1.05, "pitch": 0}, payload["voice_settings"])
        self.assertEqual("capcut", meta["upstreamProvider"])

    def test_selected_voice_id_is_forwarded_to_genmax(self):
        selected = "DVQIYWzpAqd5qcoIlirg"
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "script.txt"
            output = Path(folder) / "video.mp4"
            script.write_text("Xin chào", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "AUTOVOICE_TTS_PROVIDER": "genmax",
                "GENMAX_VOICE_ID": "pNInz6obpgDQGcFmaJgB",
            }), mock.patch.object(renderer, "run_genmax_tts_with_retry",
                                  return_value={"taskId": "test"}) as call:
                _audio, meta = renderer.generate_tts(script, output, selected, "+0%")

        self.assertEqual(call.call_args.args[1], selected)
        self.assertEqual(meta["voiceId"], selected)

    def test_invalid_choice_falls_back_to_configured_voice(self):
        fallback = "pNInz6obpgDQGcFmaJgB"
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "script.txt"
            output = Path(folder) / "video.mp4"
            script.write_text("Xin chào", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "AUTOVOICE_TTS_PROVIDER": "genmax",
                "GENMAX_VOICE_ID": fallback,
            }), mock.patch.object(renderer, "run_genmax_tts_with_retry",
                                  return_value={"taskId": "test"}) as call:
                renderer.generate_tts(script, output, "auto", "+0%")

        self.assertEqual(call.call_args.args[1], fallback)


if __name__ == "__main__":
    unittest.main()
