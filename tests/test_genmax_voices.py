import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))

import render_autovoice as renderer


class GenMaxVoiceTests(unittest.TestCase):
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
