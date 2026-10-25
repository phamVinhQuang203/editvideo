import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import engine


class _FakeYoutubeDL:
    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=True):
        path = self.options["outtmpl"].replace("%(ext)s", "mp4")
        with open(path, "wb") as handle:
            handle.write(b"0" * 60_000)
        return {"title": "Demo", "duration": 12, "uploader": "Tester", "webpage_url": url}


class YoutubeDownloadTests(unittest.TestCase):
    def test_recognizes_common_youtube_urls(self):
        self.assertTrue(engine.is_youtube_url("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(engine.is_youtube_url("https://youtu.be/abc"))
        self.assertTrue(engine.is_youtube_url("https://youtube.com/shorts/abc"))
        self.assertFalse(engine.is_youtube_url("https://www.douyin.com/video/123"))

    def test_download_returns_file_and_metadata(self):
        fake_module = types.SimpleNamespace(YoutubeDL=_FakeYoutubeDL)
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"yt_dlp": fake_module}):
            path, metadata = engine._youtube("https://youtu.be/demo", directory)
            self.assertTrue(os.path.exists(path))
            self.assertEqual("Demo", metadata["title"])
            self.assertEqual(12, metadata["dur"])

    def test_bilibili_china_download_returns_file_and_metadata(self):
        fake_module = types.SimpleNamespace(YoutubeDL=_FakeYoutubeDL)
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"yt_dlp": fake_module}):
            path, metadata = engine._bilibili_mainland(
                "https://www.bilibili.com/video/BV1demo", directory)
            self.assertTrue(os.path.exists(path))
            self.assertEqual("Demo", metadata["title"])
            self.assertEqual(12, metadata["dur"])


if __name__ == "__main__":
    unittest.main()
