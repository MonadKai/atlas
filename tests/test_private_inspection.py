# SPDX-License-Identifier: AGPL-3.0-only
"""Actual temporary-filesystem checks; no simulated external lifecycle."""
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/release"))
from inspection.paths import IORouter, assess_assets, assess_cleanup, assess_mounts, container_references, observe_plan, observe_references
from inspection.resources import ResourceIORouter, observe_resources, parse_meminfo, parse_key_values, parse_metrics, resource_summary
from inspection.cuda_memory import MINIMUM_AVAILABLE, validate_request


def item(path, kind="file"):
    value = {"path": str(path), "kind": kind, "resolved_path": str(path), "symlink_policy": "reject", "stat_identity": None}
    value["file_identity"] = {"size_bytes": 5, "sha256": "sha256:" + hashlib.sha256(b"model").hexdigest()} if kind == "file" else None
    return value


class PathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = self.root / "model"
        self.model.mkdir()
        self.file = self.model / "weights"
        self.file.write_bytes(b"model")
        self.plan = {"schema_version": "atlas-recovery-path-plan-v1", "paths": [item(self.model, "directory"), item(self.file)]}
        self.router = IORouter()

    def assess(self, plan=None):
        plan = self.plan if plan is None else plan
        return assess_assets(plan, observe_plan(plan, self.router))

    def test_real_files_sizes_hash_and_parent_identity_pass(self):
        self.assertEqual(self.assess()["status"], "PASS")

    def test_missing_wrong_hash_and_identity_fail(self):
        for mutation in (lambda: self.file.unlink(), lambda: self.file.write_bytes(b"wrong")):
            self.file.write_bytes(b"model")
            mutation()
            self.assertEqual(self.assess()["status"], "FAIL")
        self.file.write_bytes(b"model")
        plan = copy.deepcopy(self.plan)
        plan["paths"][1]["stat_identity"] = {"device": 0, "inode": 0}
        self.assertEqual(self.assess(plan)["status"], "FAIL")

    def test_parent_symlink_rejected_and_explicit_fixed_target_allowed(self):
        link = self.root / "link"
        link.symlink_to(self.model, target_is_directory=True)
        linked = item(link / "weights")
        linked["resolved_path"] = str(self.file)
        plan = {**self.plan, "paths": [linked]}
        self.assertEqual(self.assess(plan)["status"], "FAIL")
        linked["symlink_policy"] = "allow-fixed"
        self.assertEqual(self.assess(plan)["status"], "PASS")
        linked["resolved_path"] = str(self.root / "wrong-target")
        self.assertEqual(self.assess(plan)["status"], "FAIL")

    def test_broken_symlink_loop_and_file_as_directory_fail(self):
        link = self.root / "broken"
        link.symlink_to(self.root / "missing")
        loop = self.root / "loop"
        loop.symlink_to(loop)
        for path in (link, loop):
            plan = {**self.plan, "paths": [item(path)]}
            self.assertEqual(self.assess(plan)["status"], "FAIL")
        self.plan["paths"][1]["kind"] = "directory"
        self.plan["paths"][1]["file_identity"] = None
        self.assertEqual(self.assess()["status"], "FAIL")

    def test_observation_is_bound_to_full_plan_and_coverage(self):
        observed = observe_plan(self.plan, self.router)
        self.plan["paths"][0]["resolved_path"] += "-moved"
        with self.assertRaises(ValueError):
            assess_assets(self.plan, observed)
        observed = observe_plan(self.plan, self.router)
        observed["paths"].pop()
        with self.assertRaises(ValueError):
            assess_assets(self.plan, observed)

    def test_candidate_parent_child_exact_and_resolved_conflicts(self):
        ref = {"path": str(self.model), "reason": "recovery-model"}
        protected = observe_references([ref], self.router)
        for path in (self.root, self.model, self.file):
            candidates = observe_references([{"path": str(path), "reason": "candidate"}], self.router)
            result = assess_cleanup(candidates, protected)
            self.assertEqual(result["status"], "REFUSED")
            self.assertTrue(result["references"])
        other = self.root / "unused"
        other.mkdir()
        candidates = observe_references([{"path": str(other), "reason": "candidate"}], self.router)
        self.assertEqual(assess_cleanup(candidates, protected)["status"], "NO_REFERENCES_OBSERVED")
        alias = self.root / "alias"
        alias.symlink_to(self.model, target_is_directory=True)
        candidates = observe_references([{"path": str(alias), "reason": "candidate"}], self.router)
        self.assertEqual(assess_cleanup(candidates, protected)["status"], "REFUSED")

    def test_lexical_conflict_survives_missing_path_and_ambiguous_resolution_refuses(self):
        broken = self.root / "broken"
        broken.symlink_to(self.root / "missing")
        candidates = observe_references([{"path": str(broken), "reason": "candidate"}], self.router)
        protected = observe_references([{"path": str(broken / "weights"), "reason": "stopped-recovery"}], self.router)
        result = assess_cleanup(candidates, protected)
        self.assertEqual(result["status"], "REFUSED")
        self.assertTrue(result["uncertain_paths"])
        self.assertTrue(result["references"])

    def test_unsafe_paths_and_duplicates_are_rejected(self):
        for path in ("relative", "/a/../b", "/a//b", "/a/", "//a", "/a\x00b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                observe_references([{"path": path, "reason": "candidate"}], self.router)
        with self.assertRaises(ValueError):
            observe_references([{"path": str(self.root), "reason": "x"}] * 2, self.router)

    def test_stopped_container_mounts_remain_recovery_references(self):
        containers = [{"Id": "a" * 64, "State": {"Status": "exited"}, "Mounts": [
            {"Type": "bind", "Source": str(self.model), "Destination": "/models"},
            {"Type": "tmpfs", "Destination": "/tmp"}]}]
        references = container_references(containers)
        self.assertEqual(len(references), 1)
        self.assertIn(":exited:", references[0]["reason"])
        protected = observe_references(references, self.router)
        candidates = observe_references([{"path": str(self.root), "reason": "candidate"}], self.router)
        self.assertEqual(assess_cleanup(candidates, protected)["status"], "REFUSED")

    def test_replaced_directory_identity_and_other_file_type_fail(self):
        plan = copy.deepcopy(self.plan)
        metadata = self.model.stat()
        plan["paths"][0]["stat_identity"] = {"device": metadata.st_dev, "inode": metadata.st_ino}
        self.assertEqual(self.assess(plan)["status"], "PASS")
        replacement = self.root / "old-model"
        self.model.rename(replacement)
        self.model.mkdir()
        self.file.write_bytes(b"model")
        self.assertEqual(self.assess(plan)["status"], "FAIL")
        pipe = self.root / "pipe"
        import os
        os.mkfifo(pipe)
        plan = {**self.plan, "paths": [item(pipe)]}
        self.assertEqual(self.assess(plan)["status"], "FAIL")

    def test_docker_failure_is_never_an_empty_container_set(self):
        class FailedRouter(IORouter):
            def docker(self, arguments): raise ValueError("daemon unavailable")
        with self.assertRaisesRegex(ValueError, "daemon unavailable"):
            FailedRouter().containers()

    def test_tmpfs_nested_under_model_is_not_filtered_or_qualified(self):
        from inspection.__main__ import normalized_mounts
        base = {"Type": "bind", "Source": str(self.model), "Destination": "/models", "RW": False}
        mask = {"Type": "tmpfs", "Destination": "/models/weights", "RW": True}
        observed = normalized_mounts({"Id": "a" * 64, "Mounts": [base, mask]})
        self.assertEqual(len(observed["mounts"]), 2)
        self.assertIsNone(observed["mounts"][1]["source"])
        expected = {"container_id": "a" * 64, "mounts": [{"type": "bind", "source": str(self.model), "destination": "/models", "read_only": True}]}
        self.assertEqual(assess_mounts(expected, observed)["status"], "FAIL")
        self.assertIn("nested_mount_masks_required_path", assess_mounts(expected, observed)["reasons"])

    def test_mount_identity_and_nested_masking_fail_closed(self):
        mount = {"type": "bind", "source": str(self.model), "destination": "/model", "read_only": True}
        expected = {"container_id": "a" * 64, "mounts": [mount]}
        actual = {"container_id": "a" * 64, "mounts": [mount]}
        self.assertEqual(assess_mounts(expected, actual)["status"], "PASS")
        actual["mounts"].append({**mount, "destination": "/model/sub"})
        self.assertEqual(assess_mounts(expected, actual)["status"], "FAIL")
        actual = {"container_id": "b" * 64, "mounts": [mount]}
        self.assertEqual(assess_mounts(expected, actual)["status"], "FAIL")


class ResourceTests(unittest.TestCase):
    def test_separate_nvml_compute_process_query_and_unavailable_status(self):
        class ReadOnlyBoundary:
            def __init__(self): self.calls = []
            def read(self, path):
                return "Cached: 1 kB\n" if path == "/proc/meminfo" else "counter 0\n"
            def optional(self, path): return {"status": "UNAVAILABLE", "reason": "fixture"}
            def command(self, argv):
                self.calls.append(argv)
                return {"status": "UNAVAILABLE", "reason": "diagnostic fixture: permission unavailable"}
        boundary = ReadOnlyBoundary()
        observed = observe_resources([], boundary, None)
        self.assertEqual(len(boundary.calls), 2)
        self.assertEqual(boundary.calls[1], ["nvidia-smi", "--query-compute-apps=pid,gpu_uuid,process_name,used_gpu_memory", "--format=csv,noheader,nounits"])
        self.assertEqual(observed["raw"]["nvidia_smi_compute_apps"]["status"], "UNAVAILABLE")
        self.assertEqual(observed["summary"]["cuda_driver_memory"]["status"], "NOT_OBSERVED")
        self.assertNotIn("cuda_driver_free_bytes", observed)

    def test_failed_read_only_command_does_not_invent_memory_values(self):
        evidence = ResourceIORouter().command(["/nonexistent-atlas-diagnostic-program"])
        self.assertEqual(evidence["status"], "UNAVAILABLE")
        self.assertEqual(evidence["reason"], "FileNotFoundError")
        self.assertNotIn("stdout", evidence)

    def test_cuda_probe_requires_ack_floor_device_and_bounded_timeout(self):
        validate_request(0, 10, MINIMUM_AVAILABLE, True)
        for arguments in ((0, 10, MINIMUM_AVAILABLE, False), (-1, 10, MINIMUM_AVAILABLE, True),
                          (0, 31, MINIMUM_AVAILABLE, True), (0, 10, MINIMUM_AVAILABLE - 1, True)):
            with self.assertRaises(ValueError): validate_request(*arguments)

    def test_allowlisted_metrics_do_not_invent_queue_or_missing_counter_zero(self):
        result = parse_metrics("atlas_requests_active 1\nsecret_label_metric 2\n")
        self.assertEqual(result["active_requests"]["value"], 1)
        self.assertEqual(result["queue_depth"]["status"], "NOT_EXPORTED")
        self.assertNotIn("secret_label_metric", result["values"])
        for text in ("atlas_requests_active NaN", "atlas_requests_active -1", "atlas_requests_active 0\natlas_requests_active 0"):
            with self.assertRaises(ValueError): parse_metrics(text)

    def test_linux_cached_and_memavailable_are_distinct(self):
        parsed = parse_meminfo("MemTotal: 100 kB\nMemFree: 2 kB\nMemAvailable: 30 kB\nCached: 7 kB\nSwapTotal: 5 kB\nSwapFree: 1 kB\n")
        self.assertEqual(parsed["Cached"], 7168)
        self.assertEqual(parsed["MemAvailable"], 30720)
        self.assertEqual(parsed["MemFree"], 2048)

    def test_duplicate_or_invalid_kernel_observation_rejected(self):
        for text in ("Cached: 1 kB\nCached: 2 kB\n", "Cached: -1 kB\n", "Cached: 2 MB\n"):
            with self.assertRaises(ValueError):
                parse_meminfo(text)
        with self.assertRaises(ValueError):
            parse_key_values("oom 1\noom 2\n")

    def test_summary_never_equates_nvml_linux_or_atlas_accounting(self):
        raw = {"linux_meminfo": {"MemFree": 10, "MemAvailable": 30, "Cached": 5}, "containers": [], "nvidia_smi": {"status": "NOT_AVAILABLE"}}
        summary = resource_summary(raw)
        self.assertEqual(summary["linux"]["Cached"], 5)
        self.assertEqual(summary["cuda_driver_memory"]["status"], "NOT_OBSERVED")
        self.assertEqual(summary["atlas_allocator"]["status"], "NOT_OBSERVED")


if __name__ == "__main__":
    unittest.main()
