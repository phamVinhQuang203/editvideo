import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import review


class ReviewSubtitleTests(unittest.TestCase):
    def test_remove_original_audio_mutes_review_source(self):
        self.assertEqual(
            {"original": 0.0, "bed": None, "bedvol": 0.0},
            review.prepare_audio(True),
        )

    def test_original_audio_is_preserved_when_option_is_off(self):
        self.assertEqual(
            {"original": 0.05, "bed": None, "bedvol": 0.07},
            review.prepare_audio(False),
        )

    def test_shot_classifier_applies_three_second_hook_skill(self):
        response = {"shots": [{"i": 1, "vai": "hook", "mo_ta": "Kết quả lạ", "diem": 9}]}
        with patch.object(review.E, "codex_json", return_value=response) as codex_json:
            result = review.classify_shots("montage.jpg", [(0.0, 3.0)], ["lời gốc"], "sản phẩm", "gia dụng")

        prompt = codex_json.call_args.args[0]
        self.assertEqual("hook", result[0]["vai"])
        self.assertIn("0-1 giây", prompt)
        self.assertIn("Visual, Curiosity, Gap, Clarity và Payoff", prompt)
        self.assertIn("không bịa claim", prompt)

    def test_review_writer_selects_best_of_five_hooks_but_returns_plain_script(self):
        plan = [{"vai": "hook", "mo_ta": "Cận cảnh kết quả", "loi": "nhanh và sạch"}]
        with patch.object(review.E, "codex", return_value="Hook chiến thắng") as codex:
            result = review.review_script_fw(
                "máy vệ sinh", plan, review.FRAMEWORKS["review_demo"], {}, 20, "gia dụng",
                hook_type="pain_point",
            )

        prompt = codex.call_args.args[0]
        self.assertEqual("Hook chiến thắng", result)
        self.assertIn("âm thầm tạo 5 hook", prompt)
        self.assertIn("không in danh sách ứng viên", prompt)
        self.assertIn("payoff rõ nhất", prompt)
        self.assertIn("phải có nhấn nhá", prompt)
        self.assertIn("dấu ba chấm cho điểm chờ", prompt)
        self.assertIn('xưng "mình", gọi người xem là "bạn"', prompt)
        self.assertIn('CẤM dùng cách xưng hô "tao – mày"', prompt)

    def test_smart_mode_uses_cleaned_video(self):
        with tempfile.TemporaryDirectory() as workdir, \
                patch.object(review.T, "clean_original_subtitles", return_value=True) as clean, \
                patch.object(review.T, "blur_original_subtitles") as blur:
            result = review.prepare_subtitle_source("assembled.mp4", workdir, "smart_bottom")
            self.assertIn("without_subtitles", result)
            clean.assert_called_once()
            blur.assert_not_called()

    def test_smart_mode_falls_back_to_blurred_video(self):
        with tempfile.TemporaryDirectory() as workdir, \
                patch.object(review.T, "clean_original_subtitles", return_value=False), \
                patch.object(review.T, "blur_original_subtitles", return_value=True) as blur:
            result = review.prepare_subtitle_source("assembled.mp4", workdir, "smart_top")
            self.assertIn("subtitles_blurred", result)
            self.assertEqual("top", blur.call_args.args[2])

    def test_keep_mode_does_not_process_video(self):
        with patch.object(review.T, "clean_original_subtitles") as clean, \
                patch.object(review.T, "blur_original_subtitles") as blur:
            self.assertEqual("assembled.mp4", review.prepare_subtitle_source("assembled.mp4", ".", "keep"))
            clean.assert_not_called()
            blur.assert_not_called()


if __name__ == "__main__":
    unittest.main()
