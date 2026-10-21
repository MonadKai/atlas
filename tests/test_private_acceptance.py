#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure acceptance-oracle checks; never starts or mocks a service lifecycle."""
import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/release"))
from acceptance_cases import (validate_completion, validate_context, validate_cache,
                              validate_sse, validate_error, metric, native_log_evidence, validate_tool,
                              validate_completed_work)
from acceptance_suites import fit_prompt, chat_payload
from acceptance_http import Recorder


def response(text="15", prompt=30, cached=0):
    return {"id": "chatcmpl-fixture", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": 2,
                      "total_tokens": prompt + 2,
                      "prompt_tokens_details": {"cached_tokens": cached}}}


def sse(text="15"):
    events = [
        {"id": "x", "model": "m", "object": "chat.completion.chunk",
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                      "finish_reason": None}]},
        {"id": "x", "model": "m", "object": "chat.completion.chunk",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    return "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"


class CompletionOracle(unittest.TestCase):
    def test_exact_arithmetic_and_schema(self):
        validate_completion(response(), "m", "15")

    def test_json_markers_accept_formatting_but_not_wrong_value(self):
        validate_completion(response("[173, 286,491]"), "m", [173, 286, 491])
        with self.assertRaises(ValueError):
            validate_completion(response("[173, 287,491]"), "m", [173, 286, 491])

    def test_nonempty_is_not_an_oracle(self):
        with self.assertRaises(ValueError):
            validate_completion(response("wrong"), "m", "15")

    def test_wrong_model_usage_boolean_and_cached_overflow_rejected(self):
        for mutate in [lambda r: r.update(model="other"),
                       lambda r: r["usage"].update(prompt_tokens=True),
                       lambda r: r["usage"].update(total_tokens=1),
                       lambda r: r["usage"]["prompt_tokens_details"].update(cached_tokens=31)]:
            r = response()
            mutate(r)
            with self.subTest(r=r), self.assertRaises(ValueError):
                validate_completion(r, "m", "15")

    def test_multiple_choices_and_missing_finish_rejected(self):
        for r in [response(), response()]:
            if len(r["choices"]) == 1:
                r["choices"].append(copy.deepcopy(r["choices"][0]))
            with self.assertRaises(ValueError):
                validate_completion(r, "m", "15")
        r = response()
        r["choices"][0]["finish_reason"] = None
        with self.assertRaises(ValueError):
            validate_completion(r, "m", "15")


class BoundaryAndCache(unittest.TestCase):
    def test_count_search_uses_measured_count_and_rejects_unreachable_target(self):
        text, count = fit_prompt(lambda s: len(s) // 2, lambda n: "x" * n, 1000, 97, 1)
        self.assertTrue(96 <= count <= 97)
        self.assertEqual(count, len(text) // 2)
        with self.assertRaises(ValueError):
            fit_prompt(lambda s: 20, lambda n: "x" * n, 100, 97, 1)

    def test_real_count_and_full_budget(self):
        validate_context(response(prompt=32736), 32736, 32768, 32, 16)

    def test_estimate_wrong_or_overflow_fails(self):
        for count, prompt, limit in [(32730, 32736, 32768), (32736, 32736, 32737),
                                     (29048, 29048, 32768)]:
            with self.subTest(count=count), self.assertRaises(ValueError):
                validate_context(response(prompt=prompt), count, limit, 32, 16)

    def test_cache_three_suffixes_exact_and_reuse(self):
        rows = [response("731", 9000), response("492", 9000, 8000),
                response("731", 9000, 8000)]
        validate_cache(rows, "m", ["731", "492", "731"], 0, 16000)

    def test_warm_hit_without_isolation_or_counter_fails(self):
        rows = [response("731", 9000), response("731", 9000, 8000),
                response("731", 9000, 8000)]
        with self.assertRaises(ValueError):
            validate_cache(rows, "m", ["731", "492", "731"], 0, 16000)
        rows[1] = response("492", 9000, 8000)
        with self.assertRaises(ValueError):
            validate_cache(rows, "m", ["731", "492", "731"], 2, 2)


class StreamOracle(unittest.TestCase):
    def test_valid_stream(self):
        self.assertEqual(validate_sse(sse(), "m", "15")["content"], "15")

    def test_atlas_usage_only_chunk_precedes_finish(self):
        usage = {"id": "x", "model": "m", "object": "chat.completion.chunk",
                 "choices": [], "usage": response()["usage"]}
        encoded = "data: " + json.dumps(usage) + "\n\n"
        raw = sse().replace('data: {"id": "x", "model": "m", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {},',
                            encoded + 'data: {"id": "x", "model": "m", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {},')
        self.assertEqual(validate_sse(raw, "m", "15")["usage"]["prompt"], 30)

    def test_duplicate_usage_and_content_after_usage_rejected(self):
        usage = {"id": "x", "model": "m", "object": "chat.completion.chunk",
                 "choices": [], "usage": response()["usage"]}
        encoded = "data: " + json.dumps(usage) + "\n\n"
        for raw in [encoded + encoded + sse(), encoded + sse()]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_sse(raw, "m", "15")

    def test_no_done_no_finish_or_wrong_oracle_fails(self):
        variants = [sse().replace("data: [DONE]\n\n", ""),
                    sse().replace('"finish_reason": "stop"', '"finish_reason": null'),
                    sse("wrong"), sse().replace('"model": "m"', '"model": "other"', 1),
                    sse() + "data: {}\n\n", "data: [DONE]\n\n"]
        for raw in variants:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_sse(raw, "m", "15")

    def test_error_frame_and_unterminated_event_rejected(self):
        for raw in ['data: {"error":{"message":"bad"}}\n\n' + sse(),
                    sse().rstrip(), sse().replace('"id": "x"', '"id": "y"', 1)]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_sse(raw, "m", "15")


class OtherEvidence(unittest.TestCase):
    def test_blocking_requests_use_completed_generation_counter_not_stream_only_counter(self):
        # Captured real baseline: decoded stayed659 while four blocking replies
        # completed36tokens, generation910->946 and requests28->32.
        result = validate_completed_work(36, 910, 946, 4, 28, 32)
        self.assertEqual(result["generation_tokens_delta"], 36)

    def test_incomplete_completed_work_or_counter_reset_rejected(self):
        for values in [(36, 910, 945, 4, 28, 32), (36, 910, 946, 4, 28, 31),
                       (36, 910, 1, 4, 28, 32)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_completed_work(*values)

    def test_automatic_tool_requires_exact_function_arguments_and_finish(self):
        body = response()
        choice = body["choices"][0]
        choice["finish_reason"] = "tool_calls"
        choice["message"] = {"role": "assistant", "content": None,
                             "tool_calls": [{"id": "call-1", "type": "function",
                                             "function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}]}
        validate_tool(body, "m", "get_weather", {"city": "北京"})
        for bad in ["other_function", "get_weather"]:
            altered = copy.deepcopy(body)
            function = altered["choices"][0]["message"]["tool_calls"][0]["function"]
            function["name"] = bad
            if bad == "get_weather":
                function["arguments"] = '{"city":"東京"}'
            with self.assertRaises(ValueError):
                validate_tool(altered, "m", "get_weather", {"city": "北京"})

    def test_url_rejects_credentials_query_or_unexpected_path(self):
        for url in ["http://name:secret@example.test", "file:///tmp/local", "http://example.test/v1?key=x",
                    "http://example.test/v2"]:
            with self.assertRaises(ValueError):
                Recorder(url, "/tmp/not-created", 1)

    def test_fixed_sampling_does_not_enable_experimental_paths(self):
        payload = chat_payload("m", "prompt")
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("mtp_gate", payload)

    def test_structured_error(self):
        validate_error(400, {"error": {"message": "Prompt too long: 32768", "type": "invalid_request_error"}}, "Prompt too long")
        for status, body in [(200, {"error": {"message": "bad"}}),
                             (400, {"error": "bad"}), (400, {"error": {"message": "else"}})]:
            with self.assertRaises(ValueError):
                validate_error(status, body, "Prompt too long")

    def test_metric_exact_and_no_duplicate(self):
        self.assertEqual(metric("# HELP x x\nx 2\nx_other 4\n", "x"), 2)
        for text in ["x nan\n", "x 1\nx 2\n", "x_other 1\n"]:
            with self.assertRaises(ValueError):
                metric(text, "x")

    def test_fresh_exporter_unregistered_work_counters_have_explicit_zero_baseline(self):
        # Real first cold-exporter shape: no chat handler has registered these
        # lazy_static counters yet; static prefix/kernel metrics already exist.
        fresh = ("# HELP atlas_http_bytes_out_total Total HTTP response body bytes\n"
                 "# TYPE atlas_http_bytes_out_total counter\natlas_http_bytes_out_total 269\n"
                 "atlas_prefix_cache_hit_tokens_total 0\natlas_kernel_lookups_unresolved 0\n")
        before_tokens = metric(fresh, "atlas_generation_tokens_total", missing_zero=True)
        before_requests = metric(fresh, "atlas_requests_total", missing_zero=True)
        self.assertEqual(metric(fresh, "atlas_decoded_tokens_total", missing_zero=True), 0)
        self.assertEqual(validate_completed_work(36, before_tokens, 36, 4, before_requests, 4)
                         ["generation_tokens_delta"], 36)
        # After actual requests the completed counters must really be present.
        for name in ["atlas_generation_tokens_total", "atlas_requests_total"]:
            with self.assertRaises(ValueError):
                metric(fresh, name)

    def test_optional_zero_is_whitelisted_and_never_hides_invalid_or_duplicate(self):
        for text, name in [("", "atlas_requests_active"), ("", "unknown_counter"),
                           ("atlas_generation_tokens_total nan\n", "atlas_generation_tokens_total"),
                           ("atlas_generation_tokens_total\n", "atlas_generation_tokens_total"),
                           ('atlas_generation_tokens_total{unexpected="label"} 2\n', "atlas_generation_tokens_total"),
                           ("atlas_generation_tokens_total 1\natlas_generation_tokens_total 2\n",
                            "atlas_generation_tokens_total")]:
            with self.subTest(text=text, name=name), self.assertRaises(ValueError):
                metric(text, name, missing_zero=True)

    def test_cache_missing_zero_applies_only_to_explicit_initial_lookup(self):
        self.assertEqual(metric("", "atlas_prefix_cache_hit_tokens_total", missing_zero=True), 0)
        with self.assertRaises(ValueError):
            metric("", "atlas_prefix_cache_hit_tokens_total")

    def test_native_logs_never_convert_visibility_to_execution(self):
        result = native_log_evidence("GPU visible; serving; speculative decoding enabled")
        self.assertEqual(result["gpu_kernel_execution"], "NOT_RUN")
        self.assertEqual(result["ssm_internal_restore"], "NOT_RUN")


class HttpEvidenceBoundary(unittest.TestCase):
    """Real localhost transport to check recorder bytes, not a service lifecycle mock."""
    def test_http400_and_unparseable_body_preserve_raw_evidence(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"bad"}}' if self.path == "/error" else b'not-json')

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                client = Recorder(f"http://127.0.0.1:{server.server_port}", directory, 2)
                status, body, _ = client.request("/error", {"hello": "fixture"})
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["message"], "bad")
                with self.assertRaises(json.JSONDecodeError):
                    client.request("/broken", {"hello": "fixture"})
                self.assertEqual((Path(directory) / "002.response").read_bytes(), b"not-json")
                self.assertTrue((Path(directory) / "002.receipt.json").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_standalone_entrypoints_do_not_mutate_bundle_with_bytecode(self):
        source = Path(__file__).resolve().parents[1] / "deploy/release"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ["acceptance.py", "acceptance_cases.py", "acceptance_http.py",
                         "acceptance_suites.py", "observe_shutdown.py"]:
                shutil.copyfile(source / name, root / name)
            for name in ["acceptance.py", "observe_shutdown.py"]:
                result = subprocess.run([sys.executable, "-E", "-s", str(root / name), "--help"],
                                        cwd=root, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
