# -*- coding: utf-8 -*-
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "app"))

import remake


class RemakeDiversityTests(unittest.TestCase):
    def test_remove_original_audio_mutes_source_and_skips_separation(self):
        with patch.object(remake.E, "autovoice_audio") as autovoice:
            audio = remake.prepare_audio(
                {"loai_audio": "mixed_voice_music"}, "source.mp4", "work", True)
        autovoice.assert_not_called()
        self.assertEqual({"original": 0.0, "bed": None, "bedvol": 0.0}, audio)

    def test_original_audio_behavior_is_preserved_when_checkbox_is_off(self):
        expected = {"original": 0.22, "bed": None, "bedvol": 0.07}
        with patch.object(remake.E, "autovoice_audio", return_value=expected) as autovoice:
            audio = remake.prepare_audio(
                {"loai_audio": "product_sound"}, "source.mp4", "work", False)
        autovoice.assert_called_once_with("product_sound", "source.mp4", "work")
        self.assertIs(expected, audio)

    def test_creative_notes_assign_different_story_structures(self):
        first = remake.creative_note(0, 3)
        second = remake.creative_note(1, 3, ["Mở đầu cũ rồi nói về sản phẩm."])
        self.assertNotEqual(first, second)
        self.assertIn("bản 1/3", first)
        self.assertIn("bản 2/3", second)
        self.assertIn("cấm viết lại/paraphrase", second)
        self.assertIn("Mở đầu cũ", second)

    def test_script_similarity_detects_rephrased_shared_structure(self):
        original = "Chiếc máy này cắt tấm gỗ rất nhanh và đường cắt cực kỳ gọn."
        close = "Chiếc máy này cắt tấm gỗ rất nhanh, đường cắt cực kỳ gọn luôn."
        different = "Lúc đầu mình tưởng lắp đặt khó, nhưng hai thao tác là dùng được ngay."
        self.assertGreater(remake.script_similarity(original, close), 0.45)
        self.assertLess(remake.script_similarity(original, different), 0.15)


if __name__ == "__main__":
    unittest.main()
