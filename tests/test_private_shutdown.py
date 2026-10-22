#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Shutdown observer parsing tests only; no external lifecycle or GPU claims."""
import json
import contextlib
import io
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/release"))
from observe_shutdown import content_event, classify_stream, atomic_ready, load_prompt, default_prompt, main


def chunk(delta, reason=None):
    return "data: " + json.dumps({"id": "chat-1", "model": "m", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}) + "\n\n"


class PromptIdentity(unittest.TestCase):
    def test_explicit_regular_utf8_prompt_records_exact_bytes_and_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompt.txt"
            raw = "请写一篇具体而不重复的语音服务文章。\n".encode()
            path.write_bytes(raw)
            value = load_prompt(path)
            self.assertEqual(value["text"], raw.decode())
            self.assertEqual(value["identity"]["size_bytes"], len(raw))
            self.assertEqual(value["identity"]["sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())

    def test_empty_invalid_utf8_oversize_nonregular_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "prompt.txt"
            for raw in (b"", b" \n", b"\xff", b"x" * 16385, b"before\x00after"):
                path.write_bytes(raw)
                with self.subTest(raw=raw[:20]), self.assertRaises(ValueError): load_prompt(path)
            path.write_text("valid")
            link = root / "link"
            link.symlink_to(path)
            parent = root / "parent"
            parent.symlink_to(root, target_is_directory=True)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            for candidate in (link, parent / "prompt.txt", fifo, root, root / "missing"):
                with self.subTest(path=candidate), self.assertRaises(ValueError): load_prompt(candidate)

    def test_explicit_prompt_does_not_relax_the_existing_512_token_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.txt"
            prompt.write_text("A varied prose fixture")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
                main(["--base-url", "http://127.0.0.1:1", "--model", "m", "--ready-file", str(root / "ready"),
                      "--output", str(root / "output"), "--prompt-file", str(prompt), "--max-tokens", "513"])
            self.assertEqual(failure.exception.code, 2)
            self.assertFalse((root / "output").exists())

    def test_legacy_default_is_preserved_and_identified(self):
        value = default_prompt()
        self.assertIn("Print the integers from 1 through 180", value["text"])
        self.assertEqual(value["identity"]["source"], "legacy-default")
        self.assertEqual(value["identity"]["sha256"], "sha256:" + hashlib.sha256(value["text"].encode()).hexdigest())


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
