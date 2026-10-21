#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Shutdown observer parsing tests only; no external lifecycle or GPU claims."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/release"))
from observe_shutdown import content_event, classify_stream, atomic_ready


def chunk(delta, reason=None):
    return "data: " + json.dumps({"id": "chat-1", "model": "m", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}) + "\n\n"


class ShutdownOracle(unittest.TestCase):
    def test_role_only_is_not_ready(self):
        for raw in [chunk({"role": "assistant"}), chunk({"content": ""}), ": ping\n\n"]:
            self.assertIsNone(content_event(raw, "m"))

    def test_real_content_is_ready_with_response_identity(self):
        self.assertEqual(content_event(chunk({"content": "1"}), "m"),
                         {"response_id": "chat-1", "model": "m", "content": "1"})

    def test_wrong_role_cannot_trigger_coordinator(self):
        with self.assertRaises(ValueError):
            content_event(chunk({"role": "user", "content": "1"}), "m")

    def test_ready_publication_is_atomic_and_never_overwrites_previous_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready.json"
            atomic_ready(ready, {"response_id": "original"})
            with self.assertRaises(FileExistsError):
                atomic_ready(ready, {"response_id": "retry"})
            self.assertEqual(json.loads(ready.read_text()), {"response_id": "original"})
            self.assertEqual(list(Path(directory).iterdir()), [ready])

    def test_full_stream_requires_finish_and_done(self):
        raw = chunk({"role": "assistant"}) + chunk({"content": "1 2"}) + chunk({}, "stop") + "data: [DONE]\n\n"
        result = classify_stream(raw, "m")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["outcome"], "COMPLETED")

    def test_eof_without_done_never_passes(self):
        raw = chunk({"content": "1 2"}) + chunk({}, "stop")
        result = classify_stream(raw, "m")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["outcome"], "INCOMPLETE_OR_INVALID")

    def test_explicit_usage_request_cannot_silently_omit_accounting(self):
        raw = chunk({"content": "1"}) + chunk({}, "stop") + "data: [DONE]\n\n"
        self.assertEqual(classify_stream(raw, "m", require_usage=True)["status"], "FAIL")

    def test_terminal_error_separate_from_completed(self):
        raw = chunk({"content": "1"}) + 'data: {"error":"server shutting down"}\n\n'
        result = classify_stream(raw, "m")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["outcome"], "TERMINAL_ERROR")

    def test_wrong_alias_malformed_or_post_done_event_fails(self):
        for raw in [chunk({"content": "1"}).replace('"model": "m"', '"model": "other"'),
                    "data: {bad}\n\n", chunk({"content": "1"}) + "data: [DONE]\n\n"]:
            with self.subTest(raw=raw):
                self.assertEqual(classify_stream(raw, "m")["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
