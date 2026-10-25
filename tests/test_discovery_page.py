import os
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "app" / "server.py"


class DiscoveryPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SERVER.read_text(encoding="utf-8")
        page = cls.source.split('PAGE = r"""', 1)[1].split('</script></body></html>"""', 1)[0]
        cls.html = page + "</script></body></html>"

    def test_discovery_controls_and_routes_are_wired(self):
        for value in ("v-discovery", "discoverLogin", "discoverSearch", "discoverAccounts",
                      "/api/discovery/login", "/api/discovery/search",
                      "/api/discovery/accounts", "/api/discovery/account/<platform>"):
            self.assertIn(value, self.source)

    def test_reup_page_and_job_filter_are_wired(self):
        for value in ('id="v-reup"', 'id="f4"', 'id="d4"', '/api/reup',
                      '["reup","Reup video"]', 'data-f="reup"',
                      'j.kind==="reup"?"🚀 Reup video"'):
            self.assertIn(value, self.source)

    def test_movie_no_translation_option_keeps_original_subtitles_by_default(self):
        for value in ('value="none">Không dịch · giữ nguyên lời gốc',
                      'value="original" id="movieOriginalOutput">Giữ nguyên video gốc',
                      'id="movieOriginalSubtitles"',
                      'function syncMovieTarget()',
                      'output.value="original"',
                      'movieOriginalSubtitles").value="keep"'):
            self.assertIn(value, self.source)

    def test_movie_source_field_lists_youtube_and_bilibili(self):
        self.assertIn(
            'placeholder="Dán link YouTube / Bilibili / TikTok / Douyin / Google Drive / Facebook, mỗi dòng 1 link..."',
            self.source,
        )

    def test_movie_studio_left_menu_editor_and_routes_are_wired(self):
        for value in ('["studio","Studio phim"]', 'id="v-studio"', 'id="f6"', 'id="d6"',
                      'id="studioSegments"', 'id="studioSave"', 'id="studioConfirm"',
                      'seg-speaker', 'seg-voice', 'Dùng giọng mặc định',
                      'id="studioOriginalSubtitles"', 'id="studioSubtitleRegion"', 'smart_custom',
                      'id="studioFrameRatio"', 'id="studioTemplateFrame"', 'id="studioTemplateBox"',
                      'function syncStudioFrameRatio()', 'studioTemplateFrame.onpointerdown',
                      'id="studioPickRegion"', 'id="studioRegionLayer"',
                      'function paintStudioRegion()', 'subtitle_region:region',
                      '/api/movie-studio', 'awaiting_review', 'run_movie_studio'):
            self.assertIn(value, self.source)
        self.assertIn("Mỗi line có giọng riêng", self.source)
        self.assertNotIn('box.onchange=e=>{if(!e.target.matches(".seg-voice"))', self.source)

    def test_bilibili_cookie_settings_are_wired(self):
        for value in ("bilibiliCookie", "bilibiliCookieState", "saveBilibiliCookie",
                      "bilibili_cookie", "bilibili_cookie_set"):
            self.assertIn(value, self.source)

    def test_page_ids_are_unique(self):
        ids = re.findall(r'\bid="([^"]+)"', self.html)
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        self.assertEqual([], duplicates)

    def test_dashboard_javascript_parses(self):
        script = self.html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
        result = subprocess.run(["node", "--check", "-"], input=script,
                                text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
