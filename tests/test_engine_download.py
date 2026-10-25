import os
import json
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import engine


class EngineDownloadTests(unittest.TestCase):
    def test_bilibili_tv_video_url(self):
        url = "https://www.bilibili.tv/vi/video/2042409502?bstar_from=playlist"
        self.assertEqual(("aid", "2042409502"), engine._bilibili_tv_id(url))

    def test_bilibili_tv_episode_url(self):
        url = "https://www.bilibili.tv/en/play/1000677/12345678"
        self.assertEqual(("ep_id", "12345678"), engine._bilibili_tv_id(url))

    def test_bilibili_tv_series_url_is_not_one_episode(self):
        self.assertIsNone(engine._bilibili_tv_id("https://www.bilibili.tv/vi/play/1000677"))

    def test_bilibili_tv_falls_back_to_biliintl_api(self):
        payload = {"code": 0, "data": {"playurl": {
            "duration": 12_000,
            "video": [{"video_resource": {
                "url": "https://media.example/video.m4s", "codec_id": 7,
                "quality": 32, "bandwidth": 1000}}],
            "audio_resource": [{
                "url": "https://media.example/audio.m4s", "bandwidth": 128}],
        }}}
        api_hosts = []

        def fake_run(args, **_kwargs):
            if args[0] == "curl.exe" and "/playurl?" in args[-3]:
                api_hosts.append(args[-3])
                if "api.bilibili.tv" in args[-3]:
                    return types.SimpleNamespace(returncode=35, stderr="connection reset")
                with open(args[-1], "w", encoding="utf-8") as stream:
                    json.dump(payload, stream)
                return types.SimpleNamespace(returncode=0, stderr="")
            if args[0] == "curl.exe":
                with open(args[-1], "wb") as stream:
                    stream.write(b"0" * 60_000)
                return types.SimpleNamespace(returncode=0, stderr="")
            with open(args[-1], "wb") as stream:
                stream.write(b"0" * 60_000)
            return types.SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(engine.subprocess, "run", side_effect=fake_run):
            path, metadata = engine._bilibili_tv(
                "https://www.bilibili.tv/vi/play/2097863/13079919", directory)

        self.assertEqual(2, len(api_hosts))
        self.assertIn("api.biliintl.com", api_hosts[1])
        self.assertTrue(path.endswith("bilibili.mp4"))
        self.assertEqual(12.0, metadata["dur"])

    def test_bilibili_cookie_is_sent_over_stdin_not_command_line(self):
        cookie = "SESSDATA=secret-test-value; bili_jct=csrf-value"
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return types.SimpleNamespace(returncode=35, stderr="connection reset")

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(engine.CONFIG, {"bilibili_cookie": cookie}), \
                patch.dict(os.environ, {"BILIBILI_COOKIE": ""}), \
                patch.object(engine.subprocess, "run", side_effect=fake_run):
            engine._bilibili_tv(
                "https://www.bilibili.tv/vi/play/2097863/13080118", directory)

        self.assertTrue(calls)
        for args, kwargs in calls:
            self.assertNotIn(cookie, " ".join(args))
            self.assertIn("--config", args)
            self.assertIn(cookie, kwargs["input"])

    def test_recognizes_bilibili_china_and_short_links(self):
        self.assertTrue(engine.is_bilibili_mainland_url(
            "https://www.bilibili.com/video/BV1abc123?p=2"))
        self.assertTrue(engine.is_bilibili_mainland_url("https://b23.tv/demo"))
        self.assertFalse(engine.is_bilibili_mainland_url(
            "https://www.bilibili.tv/vi/video/2042409502"))

    @patch.object(engine, "_bilibili_tv")
    @patch.object(engine, "_youtube")
    def test_download_routes_bilibili_without_touching_youtube(self, youtube, bilibili):
        bilibili.return_value = ("bilibili.mp4", {"uploader": "Bilibili TV"})
        with tempfile.TemporaryDirectory() as directory:
            result = engine.download(
                "https://www.bilibili.tv/vi/video/2042409502", directory)
        self.assertEqual("bilibili.mp4", result[0])
        bilibili.assert_called_once()
        youtube.assert_not_called()

    @patch.object(engine, "_youtube")
    @patch.object(engine, "_bilibili_tv")
    def test_download_keeps_youtube_route(self, bilibili, youtube):
        youtube.return_value = ("youtube.mp4", {})
        with tempfile.TemporaryDirectory() as directory:
            result = engine.download("https://www.youtube.com/watch?v=abc123", directory)
        self.assertEqual("youtube.mp4", result[0])
        youtube.assert_called_once()
        bilibili.assert_not_called()

    @patch.object(engine, "_bilibili_mainland")
    @patch.object(engine, "_bilibili_tv")
    @patch.object(engine, "_youtube")
    def test_download_routes_bilibili_china_to_its_downloader(self, youtube, bilibili_tv,
                                                              bilibili_mainland):
        bilibili_mainland.return_value = ("bilibili-cn.mp4", {"uploader": "Bilibili"})
        with tempfile.TemporaryDirectory() as directory:
            result = engine.download(
                "https://www.bilibili.com/video/BV1abc123", directory)
        self.assertEqual("bilibili-cn.mp4", result[0])
        bilibili_mainland.assert_called_once()
        bilibili_tv.assert_not_called()
        youtube.assert_not_called()


if __name__ == "__main__":
    unittest.main()
