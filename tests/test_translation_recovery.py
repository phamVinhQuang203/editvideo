import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import translate
import engine


class TranslationRecoveryTests(unittest.TestCase):
    def source(self, count):
        return [{"start": i, "end": i + 1, "text": f"Source {i}"} for i in range(count)]

    def test_99_segments_recovers_empty_batch_and_keeps_global_ids(self):
        requests = []
        def respond(prompt, **kwargs):
            payload = json.loads(prompt.split("Dữ liệu: ")[1])
            requests.append([item["id"] for item in payload])
            if len(requests) == 1:
                return None
            return {"items": [{"id": item["id"], "text": f"Dịch {item['id']}"} for item in reversed(payload)]}
        source = self.source(99)
        with patch.object(engine, "codex_json", side_effect=respond), patch.object(engine, "log"):
            result = translate.translate_segments(source)
        self.assertEqual(99, len(result))
        self.assertEqual(list(range(17)), requests[1])
        for i, item in enumerate(result):
            self.assertEqual(f"Dịch {i}", item["translated"])
            self.assertEqual(i, item["start"])
            self.assertNotIn("translated", source[i])

    def test_retries_only_missing_and_rejects_bad_text(self):
        responses = [{"items": [{"id": 0, "text": "OK"}, {"id": 1, "text": {}},
                                 {"id": 99, "text": "wrong"}]},
                     {"items": [{"id": "1", "text": "Fixed"}]}]
        with patch.object(engine, "codex_json", side_effect=responses) as call, patch.object(engine, "log"):
            result = translate.translate_segments(self.source(2))
        payload = json.loads(call.call_args.args[0].split("Dữ liệu: ")[1])
        self.assertEqual([1], [item["id"] for item in payload])
        self.assertEqual(["OK", "Fixed"], [item["translated"] for item in result])

    def test_persistent_failure_is_bounded_and_preserves_cause(self):
        for error in (RuntimeError("Codex exit 1: unavailable"),
                      subprocess.TimeoutExpired("codex", 600)):
            with self.subTest(error=error), patch.object(engine, "codex_json", side_effect=error) as call, patch.object(engine, "log"):
                with self.assertRaisesRegex(RuntimeError, "sau 3 lần.*Nguyên nhân"):
                    translate.translate_segments(self.source(99))
                self.assertEqual(3, call.call_count)

    def test_bad_shapes_fail_cleanly(self):
        for value in (None, {}, {"items": 42}, {"items": "bad"}, {"items": [None, {"id": True, "text": "bad"}]}):
            with self.subTest(value=value), patch.object(engine, "codex_json", return_value=value), patch.object(engine, "log"):
                with self.assertRaisesRegex(RuntimeError, "thiếu 1 đoạn"):
                    translate.translate_segments(self.source(1))

    def test_strict_process_failure_redacts_configured_secret(self):
        response = subprocess.CompletedProcess([], 1, "", "authentication failed: private-token")
        with patch.object(engine.subprocess, "run", return_value=response), patch.dict(engine.CONFIG, {"api_key": "private-token"}):
            with self.assertRaises(RuntimeError) as raised:
                engine.codex_json("test", strict=True)
            self.assertIn("Codex exit 1", str(raised.exception))
            self.assertNotIn("private-token", str(raised.exception))
            self.assertEqual("", engine.codex("test"))

    def test_strict_json_accepts_fences_and_arrays(self):
        for raw in ('```json\n{"items": []}\n```', '[{"id":0,"text":"ok"}]'):
            with patch.object(engine, "codex", return_value=raw):
                self.assertIsNotNone(engine.codex_json("test", strict=True))

    def test_strict_json_reports_invalid_and_empty_responses(self):
        with patch.object(engine, "codex", return_value="not json"):
            with self.assertRaisesRegex(RuntimeError, "JSON không hợp lệ"):
                engine.codex_json("test", strict=True)
            self.assertIsNone(engine.codex_json("test"))
        with patch.object(engine.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            with self.assertRaisesRegex(RuntimeError, "stdout rỗng"):
                engine.codex_json("test", strict=True)


if __name__ == "__main__":
    unittest.main()
