import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "app"))
import viral


def signature(value, duration=20):
    return {"frames": [value] * 4, "mirrored": [value] * 4,
            "duration": duration, "width": 1080, "height": 1920}


class ViralAnalysisTests(unittest.TestCase):
    def test_only_tiktok_and_douyin_urls_are_accepted(self):
        self.assertTrue(viral.normalize_url("https://www.tiktok.com/@demo/video/123"))
        self.assertTrue(viral.normalize_url("https://www.douyin.com/video/123"))
        self.assertEqual("", viral.normalize_url("https://example.com/video/123"))
        self.assertEqual("", viral.normalize_url("not-a-url"))

    def test_signature_similarity_detects_same_and_different_content(self):
        black = "0" * 64
        white = "f" * 64
        self.assertEqual(1.0, viral.signature_similarity(signature(black), signature(black)))
        self.assertLess(viral.signature_similarity(signature(black), signature(white)), 0.3)

    def test_group_leader_is_earliest_similar_video(self):
        items = [
            {"id": "late", "create_time": 200, "signature": signature("1" * 64)},
            {"id": "early", "create_time": 100, "signature": signature("1" * 64)},
        ]
        groups = viral.classify_items(items)
        self.assertEqual("early", groups[0]["leader_id"])
        self.assertEqual("Khả năng là bản gốc", items[1]["origin_label"])
        self.assertEqual("Có dấu hiệu reup", items[0]["origin_label"])

    def test_recent_engaging_video_scores_above_old_inactive_video(self):
        now = int(time.time())
        recent = {"views": 100_000, "likes": 12_000, "comments": 500, "shares": 800,
                  "create_time": now - 86400}
        old = {"views": 100_000, "likes": 500, "comments": 10, "shares": 5,
               "create_time": now - 180 * 86400}
        self.assertGreater(viral.viral_score(recent, now), viral.viral_score(old, now))


if __name__ == "__main__":
    unittest.main()
