import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import discovery


class DiscoveryTests(unittest.TestCase):
    def test_parse_platform_counts(self):
        self.assertEqual(123_000, discovery.parse_count("12.3万"))
        self.assertEqual(2_500_000, discovery.parse_count("2.5M views"))

    def test_canonical_urls_remove_tracking(self):
        self.assertEqual("https://www.douyin.com/video/7658905260395629858",
                         discovery.canonical_url("https://www.douyin.com/video/7658905260395629858?foo=1", "douyin"))
        self.assertEqual("https://www.bilibili.com/video/BV1abc123",
                         discovery.canonical_url("https://www.bilibili.com/video/BV1abc123?p=2", "bilibili"))

    def test_rank_deduplicates_and_filters_topic(self):
        items = [
            {"url": "https://www.douyin.com/video/7658905260395629858", "title": "Mẹo nhà bếp", "views": "1万", "likes": "100"},
            {"url": "https://www.douyin.com/video/7658905260395629858?x=1", "title": "Mẹo nhà bếp", "views": "2万", "likes": "200"},
            {"url": "https://www.douyin.com/video/7658905260395629999", "title": "Thời trang", "views": "9万"},
        ]
        result = discovery.normalize_and_rank(items, "douyin", "nhà bếp", 10)
        self.assertEqual(1, len(result))
        self.assertEqual(20_000, result[0]["views"])
        self.assertGreater(result[0]["score"], 0)

    def test_past_day_is_rejected_instead_of_claiming_historical_results(self):
        with self.assertRaisesRegex(ValueError, "ngày hôm nay"):
            discovery.discover(".", "admin", "douyin", scan_date="2020-01-01")

    def test_owner_cannot_escape_profile_directory(self):
        path = discovery.profile_dir("C:/studio", "../../admin", "tiktok")
        self.assertNotIn("..", os.path.relpath(path, "C:/studio").split(os.sep))

    def test_accounts_report_saved_cookie_and_reset_profile(self):
        with tempfile.TemporaryDirectory() as root:
            profile = discovery.profile_dir(root, "admin", "tiktok")
            cookie = os.path.join(profile, "Default", "Network", "Cookies")
            os.makedirs(os.path.dirname(cookie))
            with open(cookie, "wb") as handle:
                handle.write(b"cookie")
            status = discovery.account_status(root, "admin", "tiktok")
            self.assertTrue(status["saved"])
            self.assertFalse(status["active"])
            discovery.reset_account(root, "admin", "tiktok")
            self.assertFalse(os.path.exists(profile))

    def test_reset_refuses_active_login(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(discovery, "_process_alive", return_value=True):
            profile = discovery.profile_dir(root, "admin", "douyin")
            os.makedirs(profile)
            with open(os.path.join(profile, ".discovery-browser.json"), "w", encoding="utf-8") as handle:
                handle.write('{"pid": 123}')
            with self.assertRaisesRegex(RuntimeError, "vẫn đang mở"):
                discovery.reset_account(root, "admin", "douyin")

    def test_login_reports_browser_that_exits_before_popup(self):
        process = mock.Mock()
        process.poll.return_value = 21
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(discovery.subprocess, "Popen", return_value=process), \
                mock.patch.object(discovery.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "xóa phiên"):
                discovery.launch_login(root, "admin", "tiktok")


if __name__ == "__main__":
    unittest.main()
