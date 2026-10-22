#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic experiment contracts; no service lifecycle is simulated."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/release"))
from experiments.model import (percentiles, validate_plan, summarize, validate_observation,
                               canonical_sha256, build_prompt, resource_trend, verify_raw_observation)
from experiments.plans import example_plan
from experiments.router import IORouter
from experiments.workload import prepare, execute


def plan():
    return {"schema_version": "atlas-request-experiment-v1", "name": "unit",
            "duration_seconds": 10, "max_run_seconds": 20, "max_concurrency": 2,
            "request_timeout_seconds": 4, "maximum_requests": 10,
            "schedule_mode": "SEQUENTIAL_GROUPS",
            "recovery_threshold_seconds": 30,
            "thresholds": {"minimum_successful_requests": 2, "maximum_failures": 0,
                           "ttft_p95_seconds": 5, "e2e_p95_seconds": 10},
            "threshold_scope": "EXPERIMENT_ONLY_NOT_BUSINESS_SLO",
            "schedule": [{"at_seconds": 0, "case": "short", "repeats": 2,
                          "concurrency": 2}]}


def observation():
    return {"request_id": "r00001", "case": "short", "status": "PASS",
            "termination": "COMPLETED", "expected_termination": "COMPLETED",
            "scheduled_seconds": 0, "dispatch_seconds": .1, "start_seconds": .2,
            "headers_seconds": .3, "first_content_seconds": .5, "end_seconds": 1.2,
            "content_event_seconds": [.5, .7, 1.0], "usage": {"prompt": 30,
            "completion": 3, "total": 33, "cached": 0}, "server_queue_seconds": None}


class ExperimentContracts(unittest.TestCase):
    def test_explicit_plan_and_no_implicit_slo(self):
        self.assertEqual(validate_plan(plan())["name"], "unit")
        for key in ["duration_seconds", "max_concurrency", "thresholds"]:
            p = plan(); del p[key]
            with self.assertRaises(ValueError):
                validate_plan(p)
        p = plan(); p["threshold_scope"] = "PRODUCTION"
        with self.assertRaises(ValueError):
            validate_plan(p)

    def test_bounded_schedule_and_finite_thresholds(self):
        for mutation in [lambda p: p.update(duration_seconds=0),
                         lambda p: p.update(max_run_seconds=9),
                         lambda p: p.update(max_concurrency=True),
                         lambda p: p["schedule"][0].update(at_seconds=11),
                         lambda p: p["schedule"][0].update(repeats=11),
                         lambda p: p["thresholds"].update(ttft_p95_seconds=float("nan"))]:
            p = plan(); mutation(p)
            with self.subTest(p=p), self.assertRaises(ValueError):
                validate_plan(p)

    def test_nearest_rank_percentiles_with_sample_limitation(self):
        self.assertEqual(percentiles([1, 2, 3, 4])["p95"], 4)
        self.assertEqual(percentiles([])["p99"], None)
        self.assertEqual(percentiles([1])["sample_count"], 1)
        with self.assertRaises(ValueError):
            percentiles([float("inf")])

    def test_metrics_distinguish_client_queue_from_unknown_server_queue(self):
        row = validate_observation(observation())
        self.assertAlmostEqual(row["client_queue_seconds"], .2)
        self.assertAlmostEqual(row["ttft_seconds"], .3)
        self.assertAlmostEqual(row["e2e_seconds"], 1)
        self.assertIsNone(row["server_queue_seconds"])
        self.assertIsNone(row["token_intervals_seconds"])
        self.assertEqual(len(row["content_chunk_intervals_seconds"]), 2)

    def test_order_and_actual_usage_are_mandatory(self):
        for mutation in [lambda r: r.update(first_content_seconds=2),
                         lambda r: r["usage"].update(completion=0),
                         lambda r: r["usage"].update(total=32),
                         lambda r: r.update(content_event_seconds=[.5, .4])]:
            row = observation(); mutation(row)
            with self.assertRaises(ValueError):
                validate_observation(row)

    def test_missing_failed_requests_cannot_be_dropped_from_denominator(self):
        result = summarize(plan(), [observation()], elapsed=10)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["planned_requests"], 2)
        self.assertEqual(result["missing_requests"], 1)

    def test_timeout_is_separate_from_successful_completed_work(self):
        cancelled = observation(); cancelled.update(status="PASS", termination="CALLER_CANCELLED",
            expected_termination="CALLER_CANCELLED", usage=None, request_id="r00002")
        result = summarize(plan(), [observation(), cancelled], elapsed=10)
        self.assertEqual(result["completed_requests"], 1)
        self.assertEqual(result["status"], "FAIL")

    def test_threshold_failure_and_immutable_input_hash(self):
        p = plan(); rows = [observation(), observation()]; rows[1]["request_id"] = "r00002"
        self.assertEqual(summarize(p, rows, 10)["status"], "PASS")
        rows[0]["end_seconds"] = 14
        self.assertEqual(summarize(p, rows, 10)["status"], "FAIL")
        p2 = copy.deepcopy(p); p2["thresholds"]["e2e_p95_seconds"] = 11
        self.assertNotEqual(canonical_sha256(p), canonical_sha256(p2))

    def test_same_denominator_cannot_substitute_another_frozen_case_or_termination(self):
        p = plan(); a, b = observation(), observation(); b["request_id"] = "r00002"
        b["case"] = "context32"
        self.assertEqual(summarize(p, [a, b], 10)["status"], "FAIL")
        b["case"] = "short"; b.update(termination="CALLER_CANCELLED", expected_termination="CALLER_CANCELLED")
        self.assertEqual(summarize(p, [a, b], 10)["status"], "FAIL")

    def test_a_pre_fault_short_completion_cannot_count_as_recovery(self):
        p = plan(); p["schedule"] = [
            {"at_seconds": 0, "case": "cancel", "repeats": 1, "concurrency": 1},
            {"at_seconds": 0, "case": "short", "repeats": 1, "concurrency": 1}]
        p["thresholds"]["minimum_successful_requests"] = 1
        fault = observation(); fault.update(case="cancel", termination="CALLER_CANCELLED",
            expected_termination="CALLER_CANCELLED", usage=None, end_seconds=3)
        recovery = observation(); recovery["request_id"] = "r00002"
        result = summarize(p, [fault, recovery], 10)
        self.assertEqual(result["status"], "FAIL")

    def test_resource_counter_values_cannot_be_reused_across_container_identity(self):
        p = example_plan("soak")
        rows = [{"elapsed_seconds": t, "rss_bytes": 1000000, "cgroup_current_bytes": 2000000,
                 "restart_count": 0, "oom_events": 0, "active_requests": 0,
                 "container_id": "a" * 64, "container_started_at": "2026-10-22T01:00:00Z"}
                 for t in range(0, 7201, 30)]
        rows[-1]["container_id"] = "b" * 64
        with self.assertRaises(ValueError):
            resource_trend(rows, p)

    def test_prompt_marker_locations_and_suffix_isolation(self):
        fixture = {"filler_template": "Record {index}: neutral payload.\n",
                   "markers": [173, 286, 491], "cache_codes": {"ALPHA": 731, "BETA": 492}}
        prompt = build_prompt("context", fixture, 1000)
        self.assertTrue(prompt.index("173") < prompt.index("286") < prompt.index("491"))
        a = build_prompt("cache_alpha", fixture, 1000)
        b = build_prompt("cache_beta", fixture, 1000)
        self.assertEqual(a.rsplit("for key", 1)[0], b.rsplit("for key", 1)[0])
        self.assertNotEqual(a, b)

    def test_both_frozen_example_plans_are_valid_and_bounds_are_exact(self):
        for kind, expected in [("performance", 130), ("soak", 600)]:
            p = validate_plan(example_plan(kind))
            count = sum(g["repeats"] * (2 if "companion_case" in g else 1) for g in p["schedule"])
            self.assertEqual(count, expected)
            self.assertEqual(p["scenario_thresholds"]["mixed_short"]["ttft_p95_seconds"], 5)

    def test_resource_trend_has_fixed_windows_and_does_not_infer_a_leak(self):
        p = example_plan("soak")
        rows = [{"elapsed_seconds": t, "rss_bytes": 1000000 + t,
                 "cgroup_current_bytes": 2000000 + t, "restart_count": 0,
                 "oom_events": 0, "active_requests": 0,
                 "container_id": "a" * 64, "container_started_at": "2026-10-22T01:00:00Z"} for t in range(0, 7201, 30)]
        self.assertEqual(resource_trend(rows, p)["status"], "PASS")
        rows[-1]["oom_events"] = 1
        self.assertEqual(resource_trend(rows, p)["status"], "FAIL")
        with self.assertRaises(ValueError):
            resource_trend(rows[:-1], p)

    def test_resource_growth_target_failure_is_not_reclassified_as_oom(self):
        p = example_plan("soak")
        rows = [{"elapsed_seconds": t, "rss_bytes": t * 2000000,
                 "cgroup_current_bytes": t * 2000000, "restart_count": 0,
                 "oom_events": 0, "active_requests": 0,
                 "container_id": "a" * 64, "container_started_at": "2026-10-22T01:00:00Z"} for t in range(0, 7201, 30)]
        result = resource_trend(rows, p)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(all("growth" in reason for reason in result["failure_reasons"]))

    def test_prepare_freezes_unique_measured_long_variants_before_execution(self):
        p = example_plan("performance")
        p["schedule"] = [{"at_seconds": 0, "case": "context16", "repeats": 2, "concurrency": 1}]
        fixture = json.loads((Path(__file__).resolve().parents[1] / "deploy/release/experiments/fixtures.json").read_text())

        class CountBoundary:
            def count(self, payload):
                return len(payload["messages"][0]["content"]) // 4

        workload = prepare(p, fixture, "m", CountBoundary())
        variants = workload["cases"]["context16"]["variants"]
        self.assertEqual(len(variants), 2)
        self.assertNotEqual(variants[0]["prefix_nonce"], variants[1]["prefix_nonce"])
        for variant in variants:
            self.assertTrue(16336 <= variant["counted_prompt_tokens"] <= 16352)
            self.assertLessEqual(variant["counted_prompt_tokens"] + variant["payload"]["max_tokens"], 32768)
        changed = copy.deepcopy(p); changed["name"] = "changed-after-materialization"
        class NoRequests:
            model = "m"
        with self.assertRaises(ValueError):
            execute(changed, workload, NoRequests())

    def test_companion_cannot_pace_past_the_frozen_run_budget(self):
        p = example_plan("performance")
        mixed = next(group for group in p["schedule"] if "companion_case" in group)
        mixed["companion_delay_seconds"] = 1e20
        with self.assertRaises(ValueError):
            validate_plan(p)
        with tempfile.TemporaryDirectory() as directory:
            router = IORouter("http://127.0.0.1:1", directory, "m", 2, time.monotonic())
            router.run_deadline = time.monotonic() - 1
            before = time.monotonic()
            router.pace(1e20)
            self.assertLess(time.monotonic() - before, .1)

    def test_nonfinite_resource_thresholds_and_duplicate_json_input_rejected(self):
        p = example_plan("soak"); p["resource_trend"]["median_growth_limit_bytes"] = float("nan")
        with self.assertRaises(ValueError):
            validate_plan(p)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for value in ('{"threshold": NaN}', '{"a": 1,"a": 2}'):
                path.write_text(value)
                with self.assertRaises(ValueError):
                    IORouter.read_json(path)


class RequestTransport(unittest.TestCase):
    """Real localhost HTTP framing checks, not service deployment/lifecycle mocks."""
    def test_usage_before_finish_done_raw_timings_and_exact_oracle(self):
        usage = {"prompt_tokens": 30, "completion_tokens": 2, "total_tokens": 32,
                 "prompt_tokens_details": {"cached_tokens": 0}}
        events = [
            {"id": "x", "model": "m", "object": "chat.completion.chunk", "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "15"}, "finish_reason": None}]},
            {"id": "x", "model": "m", "object": "chat.completion.chunk", "choices": [], "usage": usage},
            {"id": "x", "model": "m", "object": "chat.completion.chunk", "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}]}]
        raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(raw.encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                router = IORouter(f"http://127.0.0.1:{server.server_port}", directory, "m", 2, time.monotonic())
                case = {"case": "short", "payload": {"stream": True}, "answer": "15",
                        "expected_termination": "COMPLETED", "counted_prompt_tokens": 30}
                row = router.execute("r1", case, 0, 0)
                self.assertEqual(row["status"], "PASS", row)
                self.assertEqual(row["usage"]["completion"], 2)
                self.assertIsNone(row["token_intervals_seconds"])
                self.assertEqual((Path(directory) / "r1/response.sse-or-json").read_text(), raw)
                request = (Path(directory) / "r1/request.json").read_bytes()
                verify_raw_observation(row, case, "m", request, raw.encode())
                with self.assertRaises(ValueError):
                    verify_raw_observation(row, case, "m", b'{"stream": false}', raw.encode())
                with self.assertRaises(ValueError):
                    verify_raw_observation(row, {**case, "answer": "WRONG"}, "m", request, raw.encode())
                case["answer"] = "WRONG"
                self.assertEqual(router.execute("r2", case, 0, 0)["status"], "FAIL")
                case["expected_termination"] = "CALLER_CANCELLED"
                self.assertEqual(router.execute("r3", case, 0, 0)["termination"], "CALLER_CANCELLED")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
