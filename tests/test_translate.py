import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import translate


class TranslateTests(unittest.TestCase):
    def test_subtitle_filter_uses_custom_size_and_colors(self):
        value = translate._subtitle_filter("sub.srt", {
            "font_size": 30,
            "font_color": "#12ABEF",
            "outline_color": "#102030",
        })
        self.assertIn("FontSize=30", value)
        self.assertIn("PrimaryColour=&H00EFAB12", value)
        self.assertIn("OutlineColour=&H00302010", value)

    def test_movie_header_blank_skips_ffmpeg(self):
        with patch.object(translate.E, "ff") as ff:
            self.assertEqual("video.mp4", translate.add_movie_header("video.mp4", "   ", "work"))
        ff.assert_not_called()

    def test_movie_header_is_top_centered_and_replaces_video(self):
        with tempfile.TemporaryDirectory() as directory:
            video = os.path.join(directory, "video.mp4")
            with open(video, "wb") as handle:
                handle.write(b"old")

            def fake_ff(command):
                with open(command[-1], "wb") as handle:
                    handle.write(b"x" * 10_001)
                return SimpleNamespace(returncode=0)

            with patch.object(translate.E, "ff", side_effect=fake_ff) as ff, \
                 patch.object(translate.E, "video_encode_args", return_value=["-c:v", "libx264"]):
                translate.add_movie_header(video, "  Tây Du Ký   Tập 01  ", directory)
            command = ff.call_args.args[0]
            subtitle_filter = command[command.index("-vf") + 1]
            self.assertIn("movie_header.ass", subtitle_filter)
            with open(os.path.join(directory, "movie_header.ass"), encoding="utf-8-sig") as handle:
                header = handle.read()
            self.assertIn("Tây Du Ký Tập 01", header)
            self.assertIn("BorderStyle,Outline,Shadow,Alignment", header)
            self.assertIn(",3,2,0,8,40,40,6,1", header)
            self.assertGreater(os.path.getsize(video), 10_000)

    def test_synced_shot_duration_removes_long_silent_tail(self):
        self.assertAlmostEqual(6.55, translate._synced_shot_duration(8.0, 6.0), places=2)

    def test_synced_shot_duration_extends_picture_instead_of_cutting_voice(self):
        self.assertAlmostEqual(10.55, translate._synced_shot_duration(8.0, 10.0), places=2)

    def test_scene_emotions_are_added_without_rewriting_narration(self):
        with patch.object(translate.E, "codex_json", return_value={"items": [
                {"id": 0, "tags": ["surprised", "speaking fast"]},
                {"id": 1, "tags": ["thoughtful"]},
        ]}):
            result = translate.apply_scene_emotions(
                ["Một bí mật xuất hiện.", "Nhân vật hiểu ra mọi chuyện."], "genmax")
        self.assertEqual("[surprised][speaking fast] Một bí mật xuất hiện.", result[0])
        self.assertEqual("[thoughtful] Nhân vật hiểu ra mọi chuyện.", result[1])

    def test_scene_emotions_do_not_add_tags_for_vivibe(self):
        source = ["Giữ nguyên câu này."]
        with patch.object(translate.E, "codex_json") as codex_json:
            self.assertEqual(source, translate.apply_scene_emotions(source, "vivibe"))
        codex_json.assert_not_called()

    def test_capcut_summary_keeps_emotion_tags_out_but_uses_ai_punctuation(self):
        with patch.object(translate.E, "codex_json", return_value={"items": [{
                "id": 0, "tags": ["thoughtful"],
                "text": "Nhân vật dừng lại... rồi nhìn về phía cánh cửa!",
        }]}):
            result = translate.apply_scene_emotions(
                ["Nhân vật dừng lại rồi nhìn về phía cánh cửa."],
                "genmax", supports_tags=False)
        self.assertEqual("Nhân vật dừng lại... rồi nhìn về phía cánh cửa!", result[0])
        self.assertNotIn("[thoughtful]", result[0])

    def test_scene_delivery_rejects_ai_word_rewrite(self):
        original = "Nhân vật mở cánh cửa."
        with patch.object(translate.E, "codex_json", return_value={"items": [{
                "id": 0, "tags": ["surprised"], "text": "Kẻ lạ mở cánh cửa!",
        }]}):
            result = translate.apply_scene_emotions([original], "genmax")
        self.assertEqual("[surprised] " + original, result[0])

    def test_summary_tail_pause_is_longer_for_reflection_and_ending(self):
        self.assertEqual(0.5, translate._summary_tail_pause("[thoughtful] Sự thật lộ ra.", 1, 3))
        self.assertEqual(0.7, translate._summary_tail_pause("Mọi chuyện khép lại.", 2, 3))

    def test_summary_subtitles_start_with_voice_and_stop_before_tail_pause(self):
        cues = translate._summary_subtitle_segments(
            "[thoughtful] Bí mật xuất hiện, nhân vật lập tức chạy đi tìm sự thật.",
            shot_duration=8.0, target_duration=7.0, raw_voice_duration=6.0,
            tail_pause=0.5)
        self.assertEqual(0.3, cues[0]["start"])
        self.assertLessEqual(cues[-1]["end"], 6.5)
        self.assertGreaterEqual(7.0 - cues[-1]["end"], 0.5)
        self.assertGreater(len(cues), 1)
        self.assertNotIn("[thoughtful]", " ".join(cue["translated"] for cue in cues))

    def test_summary_subtitles_account_for_renderer_speedup(self):
        cues = translate._summary_subtitle_segments(
            "Một câu thuyết minh dài.", shot_duration=4.0, target_duration=4.0,
            raw_voice_duration=9.0, tail_pause=0.3)
        self.assertLessEqual(cues[-1]["end"], 3.7)

    def test_unknown_emotion_tags_are_replaced_by_safe_fallback(self):
        with patch.object(translate.E, "codex_json", return_value={
                "items": [{"id": 0, "tags": ["long pause", "fake-tag"]}]}):
            result = translate.apply_scene_emotions(["Câu mở đầu."], "genmax")
        self.assertEqual("[speaking fast][questioning] Câu mở đầu.", result[0])

    def test_reup_story_uses_three_second_hook_skill(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(translate.E, "codex", return_value="Hook rồi kể đúng nội dung") as codex:
            result = translate.write_story_summary(
                ["Nhân vật tìm thấy manh mối."], os.path.join(directory, "reup.txt"), 60,
                use_hook_skill=True)
        prompt = codex.call_args.args[0]
        self.assertEqual("Hook rồi kể đúng nội dung", result)
        self.assertIn("QUY TẮC HOOK TIKTOK 3 GIÂY", prompt)
        self.assertIn("tối đa 12 từ", prompt)
        self.assertIn("không biến video thành quảng cáo", prompt)

    def test_reup_caption_keeps_only_one_central_sentence(self):
        with patch.object(translate.E, "codex",
                          return_value="Caption: Nhân vật phát hiện bí mật quyết định số phận cả gia đình. Câu thừa. #viral"):
            caption = translate.reup_caption("Bản tóm tắt đầy đủ", "Tên video")
        self.assertEqual("Nhân vật phát hiện bí mật quyết định số phận cả gia đình.", caption)
        self.assertNotIn("#", caption)

    def test_reup_video_output_contains_caption(self):
        job = {"id": "reup-caption", "opts": {"tts_provider": "genmax"}, "log": []}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(translate.E, "probe", return_value=60), \
             patch.object(translate, "summarize_transcript_chapters", return_value=["Tóm tắt"]), \
             patch.object(translate, "analyze_visual_chapters", return_value=[
                 {"start": 0, "end": 30, "visual": "Cảnh mở đầu", "event": "Mở đầu"}]), \
             patch.object(translate, "summary_scene_scaffold", return_value=[[0, 30]]), \
             patch.object(translate, "write_story_summary", return_value="Nội dung chính"), \
             patch.object(translate, "align_story_to_shots", return_value=([[0, 30]], ["Nội dung chính"])), \
             patch.object(translate, "voice_for_provider", return_value=("voice-id", "Giọng đọc")), \
             patch.object(translate, "create_synced_summary_video", return_value=(True, "")), \
             patch.object(translate, "apply_scene_emotions", return_value=["Nội dung chính"]), \
             patch.object(translate, "reup_caption", return_value="Một câu trọng tâm.") as caption_mock, \
             patch.object(translate.E, "ff"), patch.object(translate.E, "log"):
            outputs = translate.make_summary_outputs(
                "source.mp4", [], "Tên video", directory, directory, "video_reup_1", "auto",
                "genmax_ninhdon", job, lambda _job: None, use_hook_skill=True)
        self.assertEqual("Một câu trọng tâm.", outputs[1]["fb_title"])
        caption_mock.assert_called_once_with("Nội dung chính", "Tên video")

    @patch.object(translate.E, "codex_json", return_value={})
    def test_reup_alignment_requests_visual_stopper_and_payoff(self, codex_json):
        translate.align_story_to_shots(
            "Hook và câu chuyện", [], 30, 15, [[0, 6], [24, 30]], use_hook_skill=True)
        prompt = codex_json.call_args.args[0]
        self.assertIn("visual stopper", prompt)
        self.assertIn("payoff", prompt)
        self.assertIn("Không được đảo sai trình tự", prompt)

    def test_srt_time(self):
        self.assertEqual("00:01:02,345", translate._srt_time(62.345))

    def test_auto_summary_is_one_sixth(self):
        self.assertEqual(600.0, translate.summary_target_seconds(3600, "auto"))

    def test_short_summary_uses_half_duration(self):
        self.assertEqual(60.0, translate.summary_target_seconds(120, "auto"))

    def test_short_video_keeps_story_ranges_selected_from_content(self):
        segments = [{"start": 0, "end": 120, "text": "Nội dung"}]
        with patch.object(translate.E, "codex_json", return_value={
                "summary": "Mạch nội dung", "ranges": [
                    {"start": 0, "end": 10}, {"start": 50, "end": 60},
                    {"start": 110, "end": 120}]}):
            ranges, _ = translate.select_summary_ranges(segments, 120, 60)
        self.assertEqual(3, len(ranges))
        self.assertEqual([[0.0, 15.0], [45.0, 65.0], [105.0, 120]], ranges)
        self.assertEqual(120, ranges[-1][1])

    def test_transcript_windows_preserve_times_and_translation(self):
        segments = [
            {"start": 1, "end": 3, "text": "A", "translated": "Mở đầu"},
            {"start": 5, "end": 8, "text": "B", "translated": "Diễn biến"},
            {"start": 14, "end": 16, "text": "C", "translated": "Kết quả"},
        ]
        windows = translate._transcript_windows(segments, 12)
        self.assertEqual(2, len(windows))
        self.assertEqual("Mở đầu Diễn biến", windows[0]["text"])
        self.assertEqual(16.0, windows[1]["end"])

    @patch.object(translate.E, "codex_json")
    def test_story_txt_is_split_before_matching_shots(self, codex_json):
        codex_json.return_value = {"items": [
            {"id": 0, "text": "Mở đầu từ TXT", "start": 1, "end": 9},
            {"id": 1, "text": "Kết thúc từ TXT", "start": 51, "end": 60},
        ]}
        fallback = [[0, 10], [50, 60]]
        ranges, narrations = translate.align_story_to_shots(
            "Mở đầu. Kết thúc.", [{"start": 0, "end": 60, "translated": "Nội dung"}],
            60, 20, fallback)
        self.assertEqual([[1.0, 9.0], [51.0, 60.0]], ranges)
        self.assertEqual(["Mở đầu từ TXT", "Kết thúc từ TXT"], narrations)
        self.assertIn("BẢN TÓM TẮT TXT", codex_json.call_args.args[0])

    def test_story_writer_uses_visual_catalogue_before_scene_matching(self):
        visual = [{"start": 12, "end": 20, "visual": "Nhân vật mở két",
                   "event": "Phát hiện chứng cứ"}]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(translate.E, "codex", return_value="Bản TXT hoàn chỉnh") as codex:
            result = translate.write_story_summary(
                ["Nhân vật tìm chứng cứ"], os.path.join(directory, "story.txt"), 60,
                visual_catalogue=visual)
        self.assertEqual("Bản TXT hoàn chỉnh", result)
        prompt = codex.call_args.args[0]
        self.assertIn("DANH MỤC CẢNH NHÌN THẤY", prompt)
        self.assertIn("Nhân vật mở két", prompt)

    @patch.object(translate.E, "codex_json")
    def test_scene_matching_prefers_visual_catalogue_and_keeps_ai_timestamps(self, codex_json):
        codex_json.return_value = {"items": [
            {"id": 0, "text": "Nhân vật phát hiện chứng cứ.", "start": 12, "end": 20},
            {"id": 1, "text": "Kẻ xấu bị bắt.", "start": 52, "end": 60},
        ]}
        visual = [{"start": 12, "end": 20, "visual": "Mở két", "event": "Thấy chứng cứ"},
                  {"start": 52, "end": 60, "visual": "Cảnh sát còng tay", "event": "Bắt giữ"}]
        ranges, narrations = translate.align_story_to_shots(
            "Phát hiện chứng cứ rồi kẻ xấu bị bắt.", [], 60, 20,
            [[0, 10], [40, 50]], visual_catalogue=visual)
        self.assertEqual([[12.0, 20.0], [52.0, 60.0]], ranges)
        self.assertEqual(["Nhân vật phát hiện chứng cứ.", "Kẻ xấu bị bắt."], narrations)
        prompt = codex_json.call_args.args[0]
        self.assertIn("Ưu tiên DANH MỤC CẢNH NHÌN THẤY", prompt)
        self.assertIn("Cảnh sát còng tay", prompt)

    def test_summary_pipeline_writes_txt_before_matching_scenes_and_rendering_voice(self):
        calls = []
        job = {"id": "summary-order", "opts": {"tts_provider": "vivibe"}, "log": []}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(translate.E, "probe", return_value=60), \
                patch.object(translate, "summarize_transcript_chapters", return_value=["Dàn ý"]), \
                patch.object(translate, "analyze_visual_chapters", return_value=[
                    {"start": 5, "end": 12, "visual": "Cảnh A", "event": "Sự kiện A"}]), \
                patch.object(translate, "summary_scene_scaffold", return_value=[[0, 10]]), \
                patch.object(translate, "write_story_summary",
                             side_effect=lambda *a, **k: calls.append("txt") or "TXT chuẩn"), \
                patch.object(translate, "align_story_to_shots",
                             side_effect=lambda *a, **k: calls.append("scenes") or ([[5, 12]], ["TXT chuẩn"])), \
                patch.object(translate, "voice_for_provider", return_value=("voice", "Giọng")), \
                patch.object(translate, "create_synced_summary_video",
                             side_effect=lambda *a, **k: calls.append("voice") or (True, "")), \
                patch.object(translate.E, "ff"), patch.object(translate.E, "log"):
            translate.make_summary_outputs(
                "source.mp4", [{"start": 0, "end": 3, "translated": "Lời"}], "Phim",
                directory, directory, "phim", "auto", "auto", job, lambda _job: None)
        self.assertEqual(["txt", "scenes", "voice"], calls)

    @patch.object(translate.E, "codex_json", return_value={})
    def test_story_alignment_fallback_preserves_all_txt_words(self, _codex_json):
        story = "một hai ba bốn năm sáu"
        ranges, narrations = translate.align_story_to_shots(
            story, [], 60, 20, [[0, 10], [50, 60]])
        self.assertEqual(story, " ".join(narrations))
        self.assertEqual([[0, 10], [50, 60]], ranges)

    def test_remap_summary_segments(self):
        segments = [{"start": 10, "end": 12, "text": "A"}, {"start": 30, "end": 33, "text": "B"}]
        result = translate.remap_summary_segments(segments, [[9, 15], [29, 35]])
        self.assertEqual(1.0, result[0]["start"])
        self.assertEqual(7.0, result[1]["start"])

    @patch.object(translate.E, "codex_json")
    def test_summary_narration_stays_with_its_shot(self, codex_json):
        codex_json.return_value = {"items": [
            {"id": 0, "text": "Nhân vật bước vào căn phòng."},
            {"id": 1, "text": "Người bạn phát hiện bí mật."},
        ]}
        segments = [
            {"start": 1, "end": 4, "translated": "Tôi vừa tới căn phòng."},
            {"start": 21, "end": 24, "translated": "Đây chính là bí mật."},
        ]

        result = translate.build_summary_narrations(
            segments, [[0, 10], [20, 30]], ["Nhân vật tới căn phòng rồi phát hiện một bí mật."])

        self.assertEqual("Nhân vật bước vào căn phòng.", result[0])
        self.assertEqual("Người bạn phát hiện bí mật.", result[1])
        shots = codex_json.call_args.args[0]
        self.assertIn("TÓM TẮT TOÀN BỘ PHIM", shots)
        self.assertIn("Nhân vật tới căn phòng rồi phát hiện một bí mật.", shots)
        self.assertIn("Tôi vừa tới căn phòng.", shots)
        self.assertIn("Đây chính là bí mật.", shots)

    def test_write_srt_uses_translation_and_utf8(self):
        segments = [{"start": 0.5, "end": 2.25, "text": "Hello", "translated": "Xin chào"}]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "sub.srt")
            translate.write_srt(segments, path)
            with open(path, encoding="utf-8-sig") as handle:
                text = handle.read()
        self.assertIn("00:00:00,500 --> 00:00:02,250", text)
        self.assertIn("Xin chào", text)

    def test_subtitle_filter_escapes_windows_drive(self):
        value = translate._subtitle_filter(r"C:\video studio\sub.srt")
        self.assertIn(r"C\:/video studio/sub.srt", value)

    def test_replace_original_bottom_subtitle_filter(self):
        value = translate._replace_subtitle_filter(r"C:\video studio\sub.srt", "bottom")
        self.assertIn("crop=iw:ih*0.24:0:ih*0.76", value)
        self.assertIn("boxblur=20:3", value)
        self.assertIn("overlay=0:H-h", value)
        self.assertIn("subtitles=", value)

    def test_replace_original_top_subtitle_filter(self):
        value = translate._replace_subtitle_filter("sub.srt", "top")
        self.assertIn("crop=iw:ih*0.24:0:0", value)
        self.assertIn("overlay=0:0", value)

    def test_custom_subtitle_region_is_used_for_blur_filter(self):
        value = translate._replace_subtitle_filter(
            "sub.srt", "custom", region=[5, 70, 90, 25])
        self.assertIn("crop=iw*0.9000:ih*0.2500:iw*0.0500:ih*0.7000", value)
        self.assertIn("overlay=W*0.0500:H*0.7000", value)

    def test_smart_custom_removal_passes_region_to_local_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "out.mp4")
            with patch.object(translate, "clean_original_subtitles", return_value=False) as clean, \
                 patch.object(translate.E, "ff") as ff:
                ff.return_value.returncode = 1
                translate.burn_subtitles(
                    "source.mp4", "sub.srt", destination, "smart_custom", directory,
                    subtitle_region=[5, 70, 90, 25])
            clean.assert_called_once_with(
                "source.mp4", os.path.join(directory, "source_without_subtitles.mp4"),
                "bottom", [5, 70, 90, 25])
            command = ff.call_args_list[0].args[0]
            self.assertIn("iw*0.9000", command[command.index("-filter_complex") + 1])

    def test_smart_removal_falls_back_to_blur(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "out.mp4")
            with patch.object(translate, "clean_original_subtitles", return_value=False), \
                 patch.object(translate.E, "ff") as ff:
                ff.return_value.returncode = 1
                translate.burn_subtitles("source.mp4", "sub.srt", destination,
                                         "smart_bottom", directory)
            command = ff.call_args_list[0].args[0]
            self.assertIn("-filter_complex", command)
            self.assertIn("boxblur=20:3", command[command.index("-filter_complex") + 1])

    def test_summary_voice_uses_selected_provider(self):
        voice_id, _ = translate.voice_for_provider("adam3", "genmax")
        self.assertEqual(translate.E.GENMAX_VOICES["adam3"][0], voice_id)
        vivibe_id, _ = translate.voice_for_provider("adam3", "vivibe")
        self.assertNotEqual(translate.E.GENMAX_VOICES["adam3"][0], vivibe_id)

    def test_vu_northern_genmax_voice_is_available(self):
        voice_id, label = translate.voice_for_provider("genmax_vu", "genmax")
        self.assertEqual("kyWPawcLE727zSyf3yY6", voice_id)
        self.assertEqual("Vũ - Northern region (GenMax)", label)

    def test_minimax_vietnamese_voices_are_available(self):
        serene_id, serene_label = translate.voice_for_provider(
            "genmax_minimax_serene_man", "genmax")
        crisp_id, crisp_label = translate.voice_for_provider(
            "genmax_minimax_crisp_announcer_v2", "genmax")
        self.assertEqual("Vietnamese_Serene_Man", serene_id)
        self.assertIn("Serene Man", serene_label)
        self.assertEqual("Vietnamese_crisp_announcer_v2", crisp_id)
        self.assertIn("Crisp Announcer", crisp_label)

    def test_ninh_don_genmax_voice_is_available(self):
        voice_id, label = translate.voice_for_provider("genmax_ninhdon", "genmax")
        self.assertEqual("aN7cv9yXNrfIR87bDmyD", voice_id)
        self.assertEqual("Ninh Đôn – Deep & Warm (GenMax)", label)

    def test_capcut_genmax_voice_is_available(self):
        voice_id, label = translate.voice_for_provider("genmax_capcut_duy_bac", "genmax")
        self.assertEqual("7491608109043945985", voice_id)
        self.assertEqual("Duy Bắc (CapCut/GenMax)", label)

    @patch.object(translate.E, "codex_json")
    def test_translate_segments_preserves_timestamps(self, codex_json):
        codex_json.return_value = {"items": [{"id": "0", "text": "Xin chào"}]}
        source = [{"start": 1.0, "end": 2.0, "text": "Hello"}]
        result = translate.translate_segments(source)
        self.assertEqual("Xin chào", result[0]["translated"])
        self.assertEqual(1.0, result[0]["start"])

    def test_no_translation_keeps_original_text_without_calling_ai(self):
        source = [{"start": 1.0, "end": 2.0, "text": "Original dialogue"}]
        with patch.object(translate, "translate_segments") as translate_mock:
            result = translate.translated_or_original_segments(source, "none")
        translate_mock.assert_not_called()
        self.assertEqual("Original dialogue", result[0]["translated"])
        self.assertEqual(1.0, result[0]["start"])
        self.assertNotIn("translated", source[0])

    def test_translation_subtitles_are_delayed_and_have_gaps(self):
        source = [
            {"start": 1.0, "end": 3.0, "translated": "Câu thứ nhất."},
            {"start": 3.0, "end": 5.0, "translated": "Câu thứ hai."},
        ]
        result = translate.retime_translation_subtitles(source)
        self.assertAlmostEqual(1.22, result[0]["start"], places=2)
        self.assertGreaterEqual(result[1]["start"] - result[0]["end"], 0.14)
        self.assertEqual(1.0, source[0]["start"])

    def test_translation_subtitles_do_not_fill_long_silent_tail(self):
        result = translate.retime_translation_subtitles([
            {"start": 2.0, "end": 20.0, "translated": "Một câu rất ngắn."},
        ])
        self.assertLessEqual(result[0]["end"] - result[0]["start"], 6.0)

    def test_atempo_filter_supports_long_dialogue_speedup(self):
        self.assertEqual("atempo=2.0,atempo=2.0,atempo=1.250000",
                         translate._atempo_filter(5.0))

    def test_dub_job_adds_vietnamese_voice_video(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            with open(source, "wb"):
                pass
            job = {"id": "dub1", "inputs": [{"type": "file", "path": source, "name": "Phim"}],
                   "opts": {"target_language": "vi", "output_mode": "dub", "tts_provider": "vivibe"},
                   "log": [], "outputs": []}
            with patch.object(translate.E, "OUT_DIR", os.path.join(directory, "out")), \
                 patch.object(translate.E, "WORK_DIR", os.path.join(directory, "work")), \
                 patch.object(translate.E, "transcribe", return_value=("Hello", [{"start": 0, "end": 1, "text": "Hello"}])), \
                 patch.object(translate, "translate_segments", return_value=[{"start": 0, "end": 1, "text": "Hello", "translated": "Xin chào"}]), \
                 patch.object(translate, "voice_for_provider", return_value=("voice-id", "Giọng Việt")), \
                 patch.object(translate, "create_dubbed_video", return_value=(True, "")) as dub_mock, \
                 patch.object(translate.E, "probe", return_value=1.0), \
                 patch.object(translate.E, "ff"), patch.object(translate.E, "log"):
                translate.run_translate(job, lambda _job: None)
            self.assertEqual("done", job["status"])
            self.assertEqual("Phim lồng tiếng Việt", job["outputs"][0]["kich_ban"])
            dub_mock.assert_called_once()

    def test_translation_source_applies_speed_and_horizontal_flip(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            destination = os.path.join(directory, "prepared.mp4")
            with open(source, "wb") as handle:
                handle.write(b"source")

            def fake_ff(command):
                with open(destination, "wb") as handle:
                    handle.write(b"x" * 10_001)
                return type("Result", (), {"returncode": 0})()

            renderer = type("Renderer", (), {"has_audio": lambda _self, _path: True})()
            with patch.object(translate.E, "ff", side_effect=fake_ff) as ff, \
                 patch.object(translate.E, "video_encode_args", return_value=["-c:v", "libx264"]), \
                 patch.object(translate.E, "_load_ra", return_value=renderer):
                result = translate.prepare_translation_source(source, destination, 1.5, "hflip")
            self.assertEqual(destination, result)
            command = ff.call_args.args[0]
            self.assertEqual("hflip,setpts=PTS/1.500000", command[command.index("-vf") + 1])
            self.assertEqual("atempo=1.500000", command[command.index("-af") + 1])

    def test_silent_video_skips_subtitles_and_dubbing(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "silent.mp4")
            with open(source, "wb") as handle:
                handle.write(b"silent video")
            job = {"id": "silent1", "inputs": [{"type": "file", "path": source, "name": "Phim câm"}],
                   "opts": {"target_language": "vi", "output_mode": "all"}, "log": [], "outputs": []}
            with patch.object(translate.E, "OUT_DIR", os.path.join(directory, "out")), \
                 patch.object(translate.E, "WORK_DIR", os.path.join(directory, "work")), \
                 patch.object(translate.E, "probe", return_value=5.0), \
                 patch.object(translate.E, "ff"), patch.object(translate.E, "log"), \
                 patch.object(translate.E, "transcribe", return_value=("", [])) as transcribe, \
                 patch.object(translate, "translate_segments") as translate_mock, \
                 patch.object(translate, "create_dubbed_video") as dub_mock:
                translate.run_translate(job, lambda _job: None)
            self.assertEqual("done", job["status"])
            self.assertEqual("Video không có lời thoại", job["outputs"][0]["kich_ban"])
            self.assertNotIn("subtitle", job["outputs"][0])
            transcribe.assert_called_once()
            translate_mock.assert_not_called()
            dub_mock.assert_not_called()

    def test_original_output_copies_video_without_transcription_or_transform(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            with open(source, "wb") as handle:
                handle.write(b"original video")
            job = {"id": "original1", "inputs": [{"type": "file", "path": source, "name": "Phim gốc"}],
                   "opts": {"target_language": "none", "output_mode": "original",
                            "original_subtitles": "keep", "video_speed": 2, "movie_flip": "hflip"},
                   "log": [], "outputs": []}
            with patch.object(translate.E, "OUT_DIR", os.path.join(directory, "out")), \
                 patch.object(translate.E, "WORK_DIR", os.path.join(directory, "work")), \
                 patch.object(translate, "_prepare_source", return_value=(source, "Phim gốc")), \
                 patch.object(translate.E, "probe", return_value=10.0), \
                 patch.object(translate.E, "ff"), patch.object(translate.E, "log"), \
                 patch.object(translate.E, "transcribe") as transcribe, \
                 patch.object(translate, "prepare_translation_source") as transform:
                translate.run_translate(job, lambda _job: None)
            self.assertEqual("done", job["status"])
            self.assertEqual("Giữ nguyên video gốc", job["outputs"][0]["kich_ban"])
            with open(os.path.join(directory, "out", "original1",
                                   job["outputs"][0]["file"]), "rb") as result:
                self.assertEqual(b"original video", result.read())
            transcribe.assert_not_called()
            transform.assert_not_called()

    def test_srt_only_job_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            with open(source, "wb"):
                pass
            job = {"id": "job1", "inputs": [{"type": "file", "path": source, "name": "Phim"}],
                   "opts": {"target_language": "vi", "output_mode": "srt"}, "log": [], "outputs": []}
            with patch.object(translate.E, "OUT_DIR", os.path.join(directory, "out")), \
                 patch.object(translate.E, "WORK_DIR", os.path.join(directory, "work")), \
                 patch.object(translate.E, "transcribe", return_value=("Hello", [{"start": 0, "end": 1, "text": "Hello"}])), \
                 patch.object(translate, "translate_segments", return_value=[{"start": 0, "end": 1, "text": "Hello", "translated": "Xin chào"}]), \
                 patch.object(translate.E, "log"):
                translate.run_translate(job, lambda _job: None)
            self.assertEqual("done", job["status"])
            self.assertEqual("subtitle", job["outputs"][0]["type"])
            self.assertTrue(os.path.exists(os.path.join(directory, "out", "job1", "phim_dich_1.srt")))

    def test_summary_job_adds_text_and_video_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            with open(source, "wb"):
                pass
            summary_outputs = [{"type": "text", "file": "summary.txt"}, {"type": "video", "file": "summary.mp4"}]
            job = {"id": "job2", "inputs": [{"type": "file", "path": source, "name": "Phim"}],
                   "opts": {"target_language": "vi", "output_mode": "summary", "make_summary": True,
                            "original_subtitles": "smart_bottom"},
                   "log": [], "outputs": []}
            with patch.object(translate.E, "OUT_DIR", os.path.join(directory, "out")), \
                 patch.object(translate.E, "WORK_DIR", os.path.join(directory, "work")), \
                 patch.object(translate.E, "transcribe", return_value=("Hello", [{"start": 0, "end": 1, "text": "Hello"}])), \
                 patch.object(translate, "translate_segments", return_value=[{"start": 0, "end": 1, "text": "Hello", "translated": "Xin chào"}]), \
                 patch.object(translate, "clean_original_subtitles", return_value=True), \
                 patch.object(translate, "make_summary_outputs", return_value=summary_outputs) as summary_mock, \
                 patch.object(translate.E, "log"):
                translate.run_translate(job, lambda _job: None)
            self.assertEqual("done", job["status"])
            self.assertEqual(["text", "video"], [item["type"] for item in job["outputs"]])
            self.assertEqual("auto", summary_mock.call_args.args[7])
            self.assertTrue(summary_mock.call_args.args[0].endswith("summary_source_without_subtitles.mp4"))

    def test_reup_job_enables_hook_skill_for_summary_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            with open(source, "wb"):
                pass
            job = {"id": "reup1", "inputs": [{"type": "file", "path": source, "name": "Video"}],
                   "opts": {"target_language": "vi", "output_mode": "summary", "make_summary": True,
                            "original_subtitles": "keep", "reup_hook_skill": True},
                   "log": [], "outputs": []}
            with patch.object(translate.E, "OUT_DIR", os.path.join(directory, "out")), \
                 patch.object(translate.E, "WORK_DIR", os.path.join(directory, "work")), \
                 patch.object(translate.E, "transcribe", return_value=("Nội dung", [{"start": 0, "end": 2, "text": "Nội dung"}])), \
                 patch.object(translate, "translate_segments", return_value=[{"start": 0, "end": 2, "text": "Nội dung", "translated": "Nội dung"}]), \
                 patch.object(translate, "make_summary_outputs", return_value=[{"type": "video", "file": "reup.mp4"}]) as summary_mock, \
                 patch.object(translate.E, "log"):
                translate.run_translate(job, lambda _job: None)
            self.assertEqual("done", job["status"])
            self.assertTrue(summary_mock.call_args.kwargs["use_hook_skill"])

    def test_movie_studio_prepare_stops_for_review_with_editable_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            with open(source, "wb") as handle:
                handle.write(b"video")
            job = {"id": "studio1", "inputs": [{"type": "file", "path": source, "name": "Phim"}],
                   "opts": {"target_language": "vi", "video_speed": 1, "movie_flip": "none"},
                   "log": [], "outputs": []}
            saves = []
            with patch.object(translate.E, "WORK_DIR", os.path.join(directory, "work")), \
                 patch.object(translate.E, "probe", return_value=20), \
                 patch.object(translate.E, "transcribe", return_value=("Hello", [
                     {"start": 1, "end": 3, "text": "Hello"}])), \
                 patch.object(translate, "translate_segments", return_value=[
                     {"start": 1, "end": 3, "text": "Hello", "translated": "Xin chào"}]), \
                 patch.object(translate.E, "log"):
                translate.prepare_movie_studio(job, lambda value: saves.append(value.get("status")))
            self.assertEqual("awaiting_review", job["status"])
            self.assertEqual("review", job["phase"])
            self.assertEqual("Nhân vật 1", job["drafts"][0]["segments"][0]["speaker"])
            self.assertEqual("", job["drafts"][0]["segments"][0]["voice"])
            self.assertEqual("Xin chào", job["drafts"][0]["segments"][0]["translated"])
            self.assertTrue(job["drafts"][0]["source_path"].startswith(os.path.join(directory, "work")))
            self.assertEqual([], job["outputs"])

    def test_dub_timeline_uses_voice_selected_for_each_character(self):
        with tempfile.TemporaryDirectory() as directory:
            selected_voices = []
            selected_providers = []

            def synthesize(_text, provider, voice_id, _destination):
                selected_providers.append(provider)
                selected_voices.append(voice_id)
                return 0.25

            def make_line(command):
                destination = command[-1]
                with translate.wave.open(destination, "wb") as output:
                    output.setnchannels(2)
                    output.setsampwidth(2)
                    output.setframerate(44100)
                    output.writeframes(b"\0" * 4410 * 4)
                return SimpleNamespace(returncode=0)

            segments = [
                {"start": 0, "end": 0.5, "translated": "Câu nam",
                 "speaker": "Nam chính", "voice": "adam3"},
                {"start": 0.6, "end": 1.1, "translated": "Câu nữ",
                 "speaker": "Nữ chính", "voice": "genmax_duyen"},
                {"start": 1.2, "end": 1.7, "translated": "Câu mặc định",
                 "speaker": "Người kể", "voice": ""},
            ]
            with patch.object(translate, "_synthesize_dub_line", side_effect=synthesize), \
                 patch.object(translate.E, "ff", side_effect=make_line), \
                 patch.object(translate.E, "log"):
                timeline = translate.create_dub_voice_timeline(
                    segments, 2, directory, "genmax", "default-voice",
                    {"id": "voice1"}, lambda _job: None)

            self.assertEqual([
                translate.E.GENMAX_VOICES["adam3"][0],
                translate.E.GENMAX_VOICES["genmax_duyen"][0],
                "default-voice",
            ], selected_voices)
            self.assertEqual(["genmax", "genmax", "genmax"], selected_providers)
            self.assertTrue(os.path.exists(timeline))

    def test_character_can_use_genmax_when_project_default_is_vivibe(self):
        with tempfile.TemporaryDirectory() as directory:
            providers = []

            def synthesize(_text, provider, _voice_id, _destination):
                providers.append(provider)
                return 0.2

            def make_line(command):
                with translate.wave.open(command[-1], "wb") as output:
                    output.setnchannels(2); output.setsampwidth(2); output.setframerate(44100)
                    output.writeframes(b"\0" * 4410 * 4)
                return SimpleNamespace(returncode=0)

            segments = [{"start": 0, "end": 0.5, "translated": "Xin chào",
                         "speaker": "Nhân vật", "voice": "genmax_mai"}]
            with patch.object(translate, "_synthesize_dub_line", side_effect=synthesize), \
                 patch.object(translate.E, "ff", side_effect=make_line), \
                 patch.object(translate.E, "log"):
                translate.create_dub_voice_timeline(
                    segments, 1, directory, "vivibe", "vivibe-default",
                    {"id": "voice2"}, lambda _job: None)

            self.assertEqual(["genmax"], providers)

    def test_movie_studio_render_uses_reviewed_text_without_retranscribing(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "reviewed.mp4")
            with open(source, "wb"):
                pass
            job = {"id": "studio2", "kind": "movie_studio", "phase": "render",
                   "inputs": [{"type": "file", "path": source, "name": "Phim"}],
                   "opts": {"target_language": "vi", "output_mode": "srt",
                            "video_speed": 1, "movie_flip": "none"},
                   "drafts": [{"index": 0, "title": "Phim", "source_path": source,
                               "duration": 20, "segments": [{"id": 0, "start": 2, "end": 4,
                               "text": "Hello", "translated": "Nội dung đã sửa"}]}],
                   "log": [], "outputs": []}
            with patch.object(translate.E, "OUT_DIR", os.path.join(directory, "out")), \
                 patch.object(translate.E, "WORK_DIR", os.path.join(directory, "work")), \
                 patch.object(translate.E, "transcribe") as transcribe, \
                 patch.object(translate, "translate_segments") as translate_mock, \
                 patch.object(translate.E, "log"):
                translate.run_movie_studio(job, lambda _job: None)
            self.assertEqual("done", job["status"])
            transcribe.assert_not_called()
            translate_mock.assert_not_called()
            srt_path = os.path.join(directory, "out", "studio2", "phim_dich_1.srt")
            with open(srt_path, encoding="utf-8-sig") as handle:
                srt = handle.read()
            self.assertIn("Nội dung đã sửa", srt)
            self.assertIn("00:00:02,220", srt)


if __name__ == "__main__":
    unittest.main()
