import os
import sys
import unittest

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vendor"))
import remove_subtitles_local as remover


class LocalSubtitleRemovalTests(unittest.TestCase):
    def test_bottom_mask_ignores_top_text(self):
        frame = np.zeros((200, 320, 3), dtype=np.uint8)
        cv2.putText(frame, "TOP", (100, 35), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        cv2.putText(frame, "SUBTITLE", (70, 180), cv2.FONT_HERSHEY_SIMPLEX, .8, (255, 255, 255), 2)
        mask = remover.text_mask(frame, "bottom")
        self.assertEqual(0, int(mask[:100].max()))
        self.assertGreater(int(mask[145:].max()), 0)

    def test_top_and_bottom_bands_do_not_overlap(self):
        self.assertEqual((0, 240), remover.subtitle_band(1000, "top"))
        self.assertEqual((720, 1000), remover.subtitle_band(1000, "bottom"))


if __name__ == "__main__":
    unittest.main()
