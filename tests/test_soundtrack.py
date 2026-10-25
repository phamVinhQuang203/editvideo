import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import soundtrack


class SoundtrackTests(unittest.TestCase):
    def test_replaces_original_audio_and_stops_at_shorter_source(self):
        with tempfile.TemporaryDirectory() as directory:
            video = os.path.join(directory, "video.mp4")
            audio = os.path.join(directory, "audio.mp4")
            for path in (video, audio):
                with open(path, "wb") as handle:
                    handle.write(b"source")
            job = {"id": "sound1", "opts": {"video_url": "https://video.example/1",
                   "audio_url": "https://audio.example/2"}, "log": [], "outputs": []}
            downloads = [(video, {"title": "Video chính"}), (audio, {"title": "Nhạc nguồn"})]

            def fake_ff(command):
                if command[-1].endswith(".mp4"):
                    os.makedirs(os.path.dirname(command[-1]), exist_ok=True)
                    with open(command[-1], "wb") as handle:
                        handle.write(b"x" * 10_001)
                return type("Result", (), {"returncode": 0})()

            renderer = type("Renderer", (), {"has_audio": lambda _self, _path: True})()
            with patch.object(soundtrack.E, "OUT_DIR", os.path.join(directory, "out")), \
                 patch.object(soundtrack.E, "WORK_DIR", os.path.join(directory, "work")), \
                 patch.object(soundtrack.E, "download", side_effect=downloads) as download, \
                 patch.object(soundtrack.E, "_load_ra", return_value=renderer), \
                 patch.object(soundtrack.E, "ff", side_effect=fake_ff) as ff, \
                 patch.object(soundtrack.E, "probe", return_value=12.0), \
                 patch.object(soundtrack.E, "log"):
                soundtrack.run_soundtrack(job, lambda _job: None)

            self.assertEqual("done", job["status"])
            self.assertEqual(2, download.call_count)
            self.assertNotIn("-stream_loop", ff.call_args_list[0].args[0])
            self.assertIn("-shortest", ff.call_args_list[0].args[0])
            mux_command = ff.call_args_list[0].args[0]
            map_positions = [index for index, value in enumerate(mux_command) if value == "-map"]
            self.assertEqual("1:a:0", mux_command[map_positions[1] + 1])
            self.assertEqual("Thay âm thanh từ link khác", job["outputs"][0]["kich_ban"])

    def test_page_contains_two_separate_link_inputs(self):
        source = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
        self.assertIn('id="v-soundtrack"', source)
        self.assertIn('name="video_url"', source)
        self.assertIn('name="audio_url"', source)
        self.assertIn('name="video_speed"', source)
        self.assertIn('name="movie_flip"', source)
        self.assertIn('submit(e.target,"/api/soundtrack")', source)

    def test_prepare_video_applies_speed_and_flip(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            destination = os.path.join(directory, "prepared.mp4")
            with open(source, "wb") as handle:
                handle.write(b"source")

            def fake_ff(command):
                with open(destination, "wb") as handle:
                    handle.write(b"x" * 10_001)
                return type("Result", (), {"returncode": 0})()

            with patch.object(soundtrack.E, "ff", side_effect=fake_ff) as ff, \
                 patch.object(soundtrack.E, "video_encode_args", return_value=["-c:v", "libx264"]):
                result = soundtrack.prepare_video(source, destination, 1.5, "hflip")
            self.assertEqual(destination, result)
            command = ff.call_args.args[0]
            self.assertEqual("hflip,setpts=PTS/1.500000", command[command.index("-vf") + 1])
            self.assertIn("-an", command)


if __name__ == "__main__":
    unittest.main()
