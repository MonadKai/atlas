# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic input and receipt checks; no service lifecycle simulation."""

import copy
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / "deploy/release/experiments/coload.py"
SPEC = importlib.util.spec_from_file_location("atlas_coload", PATH)
coload = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coload)


def site():
    names = ("asr_client", "asr_scorer", "asr_audio", "asr_fixtures", "br_client",
             "br_audio", "ten_client")
    return {
        "schema_version": "atlas-coload-v1", "cycles": 2, "interval_seconds": 1,
        "interval_semantics": "START_TO_START",
        "max_duration_seconds": 60, "request_timeout_seconds": 10,
        "expected_websockets_version": "12.0",
        "files": {n: {"path": f"/fixed/{n}", "size_bytes": 3,
                      "sha256": "sha256:" + "a" * 64} for n in names},
        "asr": {"endpoint": "ws://127.0.0.1:18295/v1/ws", "language": "zh",
                "fixture_name": "zh-16000.wav", "chunk_delay_seconds": 0.1,
                "maximum_error_rate": 0.6, "maximum_increase": 0.15},
        "br": {"endpoint": "http://127.0.0.1:18092", "semantic_profile": "v024-non-ja",
               "suppression": "balanced", "sample_rate_hz": 16000,
               "input_frames_per_message": 320, "output_frames_per_message": 320},
        "ten": {"endpoint": "ws://127.0.0.1:10080/"},
    }


class Inputs(unittest.TestCase):
    def test_validate_is_pure_and_does_not_mutate(self):
        original = site()
        before = copy.deepcopy(original)
        self.assertEqual(coload.validate_site(original), before)
        self.assertEqual(original, before)

    def test_invalid_bounds_and_identities(self):
        changes = [("cycles", True), ("cycles", 0), ("interval_seconds", float("nan")),
                   ("max_duration_seconds", 0), ("request_timeout_seconds", 301),
                   ("expected_websockets_version", "15.0.1")]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                value_site = site()
                value_site[key] = value
                with self.assertRaises(ValueError):
                    coload.validate_site(value_site)
        for field, value in [("path", "relative"), ("size_bytes", -1),
                             ("sha256", "sha256:" + "x" * 64)]:
            value_site = site()
            value_site["files"]["asr_client"][field] = value
            with self.assertRaises(ValueError):
                coload.validate_site(value_site)

    def test_no_credentials_or_unbounded_endpoint(self):
        for url in ["ws://user:secret@localhost:80", "ws://localhost/?token=secret",
                    "file:///tmp/socket", "ws://localhost/#fragment"]:
            value = site()
            value["asr"]["endpoint"] = url
            with self.assertRaises(ValueError):
                coload.validate_site(value)

    def test_start_to_start_semantics_must_be_explicit(self):
        for semantics in (None, "AFTER_COMPLETION"):
            value = site()
            value["interval_semantics"] = semantics
            with self.assertRaises(ValueError):
                coload.validate_site(value)

    def test_receipts_bind_exact_site_and_complete_cycle(self):
        value = site()
        digest = coload.site_digest(value)
        rows = [{"cycle": i, "service": name, "site_sha256": digest, "status": "PASS"}
                for i in range(2) for name in ("asr", "br", "ten")]
        self.assertEqual(coload.evaluate_results(value, rows)["status"], "PASS")
        self.assertEqual(coload.evaluate_results(value, rows[:-1])["status"], "FAIL")
        self.assertEqual(coload.evaluate_results(value, rows + [rows[0]])["status"], "FAIL")
        rows[0]["site_sha256"] = "sha256:" + "b" * 64
        self.assertEqual(coload.evaluate_results(value, rows)["status"], "FAIL")

    def test_br_profile_and_offer_must_both_match(self):
        desired = site()["br"]
        p = {"semantic_profile": "v024-non-ja", "suppression": "balanced",
             "input": {"sample_rate_hz": 16000}, "framing": {"offers": [
                 {"input_frames_per_message": 320, "output_frames_per_message": 320}]}}
        self.assertEqual(coload.select_br_profile({"qualified_profiles": [p]}, desired)[0], p)
        p["framing"]["offers"] = []
        with self.assertRaises(ValueError):
            coload.select_br_profile({"qualified_profiles": [p]}, desired)

    def test_publisher_reference_must_bind_audio_and_cannot_relax_its_policy(self):
        value = site()
        manifest = {"files": {"zh-16000.wav": {
            "language": "zh", "sha256": value["files"]["asr_audio"]["sha256"],
            "reference": "reference", "current_error_rate": 0.1}},
            "regression_policy": {"maximum_error_rate": 0.6,
                                  "maximum_increase_over_current": 0.15}}
        self.assertEqual(coload.bind_asr_fixture(value, manifest)["reference"], "reference")
        for mutate in ("sha", "language", "threshold"):
            wrong = copy.deepcopy(manifest)
            if mutate == "sha":
                wrong["files"]["zh-16000.wav"]["sha256"] = "sha256:" + "b" * 64
            elif mutate == "language":
                wrong["files"]["zh-16000.wav"]["language"] = "en"
            else:
                wrong["regression_policy"]["maximum_error_rate"] = 0.5
            with self.assertRaises(ValueError):
                coload.bind_asr_fixture(value, wrong)


if __name__ == "__main__":
    unittest.main()
