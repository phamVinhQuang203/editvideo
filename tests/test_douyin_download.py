import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import engine


class DouyinDownloadTests(unittest.TestCase):
    def test_normalizes_jingxuan_modal_id(self):
        url = "https://www.douyin.com/jingxuan?modal_id=7671233853402418478"
        self.assertEqual("https://www.douyin.com/video/7671233853402418478",
                         engine.normalize_douyin_url(url))

    def test_cookie_is_sent_over_stdin_not_command_line(self):
        cookie = "sessionid=secret-test-value"
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(engine.CONFIG, {"douyin_cookie": cookie}), \
             patch.object(engine, "log"), \
             patch.object(engine.os.path, "isfile", return_value=True), \
             patch.object(engine.subprocess, "run") as run:
            run.return_value.stdout = ""
            run.return_value.stderr = "failed"
            engine._douyin_f2("https://www.douyin.com/video/7671233853402418478", folder)
        args, kwargs = run.call_args
        self.assertNotIn(cookie, " ".join(args[0]))
        self.assertEqual(cookie, kwargs["input"])


if __name__ == "__main__":
    unittest.main()
