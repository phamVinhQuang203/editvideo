import os
import sys
import tempfile
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import translate


class TranslateTests(unittest.TestCase):
    def test_srt_time(self):
        self.assertEqual("00:01:02,345", translate._srt_time(62.345))

    def test_auto_summary_is_one_sixth(self):
        self.assertEqual(600.0, translate.summary_target_seconds(3600, "auto"))

    def test_remap_summary_segments(self):
        segments = [{"start": 10, "end": 12, "text": "A"}, {"start": 30, "end": 33, "text": "B"}]
        result = translate.remap_summary_segments(segments, [[9, 15], [29, 35]])
        self.assertEqual(1.0, result[0]["start"])
        self.assertEqual(7.0, result[1]["start"])

    @patch.object(translate.E, "codex_json")
    def test_summary_narration_stays_with_its_shot(self, codex_json):
        codex_json.return_value = {"items": [
            {"id": 0, "text": "Nhân vật bước vào căn phòng."},
            {"id": 1, "text": "Người bạn phát hiện bí mật."},
        ]}
        segments = [
            {"start": 1, "end": 4, "translated": "Tôi vừa tới căn phòng."},
            {"start": 21, "end": 24, "translated": "Đây chính là bí mật."},
        ]

        result = translate.build_summary_narrations(
            segments, [[0, 10], [20, 30]], ["Nhân vật tới căn phòng rồi phát hiện một bí mật."])

        self.assertEqual("Nhân vật bước vào căn phòng.", result[0])
        self.assertEqual("Người bạn phát hiện bí mật.", result[1])
        shots = codex_json.call_args.args[0]
        self.assertIn("TÓM TẮT TOÀN BỘ PHIM", shots)
        self.assertIn("Nhân vật tới căn phòng rồi phát hiện một bí mật.", shots)
        self.assertIn("Tôi vừa tới căn phòng.", shots)
        self.assertIn("Đây chính là bí mật.", shots)

    def test_write_srt_uses_translation_and_utf8(self):
        segments = [{"start": 0.5, "end": 2.25, "text": "Hello", "translated": "Xin chào"}]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "sub.srt")
            translate.write_srt(segments, path)
            with open(path, encoding="utf-8-sig") as handle:
                text = handle.read()
        self.assertIn("00:00:00,500 --> 00:00:02,250", text)
        self.assertIn("Xin chào", text)

    def test_subtitle_filter_escapes_windows_drive(self):
        value = translate._subtitle_filter(r"C:\video studio\sub.srt")
        self.assertIn(r"C\:/video studio/sub.srt", value)

    def test_replace_original_bottom_subtitle_filter(self):
        value = translate._replace_subtitle_filter(r"C:\video studio\sub.srt", "bottom")
        self.assertIn("crop=iw:ih*0.24:0:ih*0.76", value)
        self.assertIn("boxblur=20:3", value)
        self.assertIn("overlay=0:H-h", value)
        self.assertIn("subtitles=", value)

    def test_replace_original_top_subtitle_filter(self):
        value = translate._replace_subtitle_filter("sub.srt", "top")
        self.assertIn("crop=iw:ih*0.24:0:0", value)
        self.assertIn("overlay=0:0", value)

    def test_smart_removal_falls_back_to_blur(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "out.mp4")
            with patch.object(translate, "clean_original_subtitles", return_value=False), \
                 patch.object(translate.E, "ff") as ff:
                ff.return_value.returncode = 1
                translate.burn_subtitles("source.mp4", "sub.srt", destination,
                                         "smart_bottom", directory)
            command = ff.call_args_list[0].args[0]
            self.assertIn("-filter_complex", command)
            self.assertIn("boxblur=20:3", command[command.index("-filter_complex") + 1])

    def test_atempo_chain_supports_long_translated_line(self):
        chain = translate._atempo_chain(5.0)
        self.assertEqual("atempo=2.0,atempo=2.0,atempo=1.25000", chain)

    def test_dub_voice_uses_selected_provider(self):
        voice_id, _ = translate.voice_for_provider("adam3", "genmax")
        self.assertEqual(translate.E.GENMAX_VOICES["adam3"][0], voice_id)
        vivibe_id, _ = translate.voice_for_provider("adam3", "vivibe")
        self.assertNotEqual(translate.E.GENMAX_VOICES["adam3"][0], vivibe_id)

    def test_dub_timeline_preserves_original_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            def fake_ff(command):
                output = command[-1]
                with wave.open(output, "wb") as handle:
                    handle.setnchannels(2); handle.setsampwidth(2); handle.setframerate(44100)
                    handle.writeframes(b"\1\0\1\0" * 22050)
                return SimpleNamespace(returncode=0)

            segments = [{"start": 1.0, "end": 1.5, "translated": "Xin chào"}]
            job = {"id": "dub-test"}
            with patch.object(translate, "_synthesize_dub_line", return_value=0.5), \
                 patch.object(translate.E, "ff", side_effect=fake_ff), \
                 patch.object(translate.E, "log"):
                timeline = translate.create_dub_voice_timeline(
                    segments, 2.0, directory, "vivibe", "voice", job, lambda _job: None)
            with wave.open(timeline, "rb") as handle:
                self.assertEqual(88200, handle.getnframes())
                leading = handle.readframes(44100)
            self.assertEqual({0}, set(leading))

    def test_dubbing_stops_if_demucs_cannot_remove_original_voice(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(translate.E, "autovoice_audio", return_value={"bed": None}), \
             patch.object(translate.E, "log"):
            with self.assertRaisesRegex(RuntimeError, "không tách được giọng gốc"):
                translate.create_dubbed_video(
                    "source.mp4", [], os.path.join(directory, "out.mp4"), directory,
                    "vivibe", "voice", {"id": "dub-test"}, lambda _job: None)

    @patch.object(translate.E, "codex_json")
    def test_translate_segments_preserves_timestamps(self, codex_json):
        codex_json.return_value = {"items": [{"id": "0", "text": "Xin chào"}]}
        source = [{"start": 1.0, "end": 2.0, "text": "Hello"}]
        result = translate.translate_segments(source)
        self.assertEqual("Xin chào", result[0]["translated"])
        self.assertEqual(1.0, result[0]["start"])

    def test_srt_only_job_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            with open(source, "wb"):
                pass
            job = {"id": "job1", "inputs": [{"type": "file", "path": source, "name": "Phim"}],
                   "opts": {"target_language": "vi", "output_mode": "srt"}, "log": [], "outputs": []}
            with patch.object(translate.E, "OUT_DIR", os.path.join(directory, "out")), \
                 patch.object(translate.E, "WORK_DIR", os.path.join(directory, "work")), \
                 patch.object(translate.E, "transcribe", return_value=("Hello", [{"start": 0, "end": 1, "text": "Hello"}])), \
                 patch.object(translate, "translate_segments", return_value=[{"start": 0, "end": 1, "text": "Hello", "translated": "Xin chào"}]), \
                 patch.object(translate.E, "log"):
                translate.run_translate(job, lambda _job: None)
            self.assertEqual("done", job["status"])
            self.assertEqual("subtitle", job["outputs"][0]["type"])
            self.assertTrue(os.path.exists(os.path.join(directory, "out", "job1", "phim_dich_1.srt")))

    def test_summary_job_adds_text_and_video_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            with open(source, "wb"):
                pass
            summary_outputs = [{"type": "text", "file": "summary.txt"}, {"type": "video", "file": "summary.mp4"}]
            job = {"id": "job2", "inputs": [{"type": "file", "path": source, "name": "Phim"}],
                   "opts": {"target_language": "vi", "output_mode": "summary", "make_summary": True,
                            "original_subtitles": "smart_bottom"},
                   "log": [], "outputs": []}
            with patch.object(translate.E, "OUT_DIR", os.path.join(directory, "out")), \
                 patch.object(translate.E, "WORK_DIR", os.path.join(directory, "work")), \
                 patch.object(translate.E, "transcribe", return_value=("Hello", [{"start": 0, "end": 1, "text": "Hello"}])), \
                 patch.object(translate, "translate_segments", return_value=[{"start": 0, "end": 1, "text": "Hello", "translated": "Xin chào"}]), \
                 patch.object(translate, "clean_original_subtitles", return_value=True), \
                 patch.object(translate, "make_summary_outputs", return_value=summary_outputs) as summary_mock, \
                 patch.object(translate.E, "log"):
                translate.run_translate(job, lambda _job: None)
            self.assertEqual("done", job["status"])
            self.assertEqual(["text", "video"], [item["type"] for item in job["outputs"]])
            self.assertEqual("auto", summary_mock.call_args.args[7])
            self.assertTrue(summary_mock.call_args.args[0].endswith("summary_source_without_subtitles.mp4"))


if __name__ == "__main__":
    unittest.main()
