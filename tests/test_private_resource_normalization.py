# SPDX-License-Identifier: AGPL-3.0-only
"""Pure resource evidence normalization; never sends requests or controls services."""
import copy
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/release"))
from experiments.resources import normalize, parse_observations

ID = "a" * 64


def raw(index=0):
    return {"schema_version": "atlas-resource-observation-v1", "sample_index": index,
            "started_at": f"2026-10-22T02:0{index + 1}:00+00:00", "ended_at": f"2026-10-22T02:0{index + 1}:00.100000+00:00",
            "collection_seconds": .1, "raw": {
                "linux_meminfo": {"MemAvailable": 300, "MemFree": 10, "Cached": 200},
                "atlas_metrics": {"status": "OBSERVED", "values": {"atlas_requests_active": 1.0},
                    "active_requests": {"status": "OBSERVED", "value": 1.0},
                    "allowlisted_raw_lines": ["atlas_requests_active 1"]},
                "containers": [{"container_id": ID, "status": "OBSERVED", "restart_count": 0,
                    "state": {"Running": True, "Restarting": False, "OOMKilled": False,
                              "StartedAt": "2026-10-22T02:00:00.123456789Z"},
                    "process_status": {"VmRSS": 3},
                    "member_processes": [{"status": "OBSERVED", "pid": 100, "comm": "docker-init", "memory": {"VmRSS": 3}},
                                         {"status": "OBSERVED", "pid": 101, "comm": "spark", "memory": {"VmRSS": 100}}],
                    "cgroup_files": {"memory.current": {"status": "OBSERVED", "text": "150\n"},
                                     "memory.events": {"status": "OBSERVED", "text": "oom 2\noom_kill 0\n"},
                                     "cgroup.procs": {"status": "OBSERVED", "text": "100\n101\n"}}}]}}


class ResourceNormalization(unittest.TestCase):
    def test_selects_exact_spark_rss_never_init_or_sum_and_records_time_window(self):
        result = normalize([raw(), raw(1)], ID, "spark", "oom_kill")
        self.assertEqual([r["elapsed_seconds"] for r in result], [0, 60])
        self.assertEqual(result[0]["rss_bytes"], 100)
        self.assertEqual(result[0]["cgroup_current_bytes"], 150)
        self.assertEqual(result[0]["oom_events"], 0)
        self.assertEqual(result[0]["oom_counter"], "oom_kill")
        self.assertEqual(result[0]["active_requests"], 1)
        self.assertEqual(result[0]["observation_started_at"], raw()["started_at"])
        self.assertEqual(result[0]["observation_ended_at"], raw()["ended_at"])
        self.assertEqual(result[0]["collection_seconds"], .1)
        self.assertNotIn("cuda_driver_free_bytes", result[0])

    def test_input_not_mutated_and_fixed_explicit_selection_required(self):
        rows = [raw()]; before = copy.deepcopy(rows)
        normalize(rows, ID, "spark", "oom_kill")
        self.assertEqual(rows, before)
        for selected in (("shortid", "spark", "oom_kill"), (ID, "", "oom_kill"), (ID, "spark", "invented")):
            with self.subTest(selected=selected), self.assertRaises(ValueError): normalize(rows, *selected)

    def test_missing_unobserved_or_ambiguous_process_and_counter_rejected(self):
        mutations = [lambda c: c.pop("member_processes"),
                     lambda c: c["member_processes"].append(copy.deepcopy(c["member_processes"][1])),
                     lambda c: c["member_processes"][1].update(comm="other"),
                     lambda c: c["member_processes"][1].update(status="UNAVAILABLE"),
                     lambda c: c["member_processes"][1]["memory"].pop("VmRSS"),
                     lambda c: c.pop("restart_count"),
                     lambda c: c["cgroup_files"]["memory.events"].update(text="oom 2\n"),
                     lambda c: c["cgroup_files"]["memory.current"].update(status="UNAVAILABLE"),
                     lambda c: c["cgroup_files"]["cgroup.procs"].update(text="100\n")]
        for mutation in mutations:
            value = raw(); mutation(value["raw"]["containers"][0])
            with self.subTest(mutation=mutations.index(mutation)), self.assertRaises(ValueError): normalize([value], ID, "spark", "oom_kill")

    def test_membership_must_cover_every_observed_cgroup_pid_exactly(self):
        value = raw()
        value["raw"]["containers"][0]["cgroup_files"]["cgroup.procs"]["text"] += "102\n"
        with self.assertRaises(ValueError): normalize([value], ID, "spark", "oom_kill")
        value = raw()
        value["raw"]["containers"][0]["member_processes"].append({"status": "OBSERVED", "pid": 102, "comm": "other", "memory": {"VmRSS": 1}})
        with self.assertRaises(ValueError): normalize([value], ID, "spark", "oom_kill")

    def test_identity_counter_and_time_drift_rejected(self):
        mutations = [lambda v: v["raw"]["containers"][0]["state"].update(StartedAt="2026-10-22T02:01:00Z"),
                     lambda v: v["raw"]["containers"][0]["member_processes"][1].update(pid=102),
                     lambda v: v["raw"]["containers"][0].update(container_id="b" * 64),
                     lambda v: v.update(started_at="2026-10-22T02:00:00+00:00"),
                     lambda v: v.update(ended_at="2026-10-22T01:59:00Z"),
                     lambda v: v.update(collection_seconds=float("nan")),
                     lambda v: v.update(sample_index=0)]
        for mutation in mutations:
            values = [raw(), raw(1)]; mutation(values[1])
            with self.subTest(mutation=mutations.index(mutation)), self.assertRaises(ValueError): normalize(values, ID, "spark", "oom_kill")
        values = [raw(), raw(1)]
        values[0]["raw"]["containers"][0]["restart_count"] = 1
        with self.assertRaises(ValueError): normalize(values, ID, "spark", "oom_kill")

    def test_jsonl_snapshot_rejects_incomplete_duplicate_nonfinite_or_blank_rows(self):
        value = json.dumps(raw()) + "\n"
        self.assertEqual(len(parse_observations(value)), 1)
        for text in (value.rstrip(), value + "\n", '{"key":1,"key":2}\n', '{"key":NaN}\n'):
            with self.subTest(text=text[:40]), self.assertRaises(ValueError): parse_observations(text)

    def test_atomic_cli_writes_direct_trend_list_and_refuses_overwrite(self):
        script = Path(__file__).resolve().parents[1] / "deploy/release/experiments/cli.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "observations.jsonl"
            source.write_text("".join(json.dumps(v) + "\n" for v in [raw(), raw(1)]))
            output = root / "normalized.json"
            command = [sys.executable, "-B", "-E", "-s", str(script), "normalize-resources",
                       "--raw-observations", str(source), "--container-id", ID, "--process-comm", "spark",
                       "--oom-counter", "oom_kill", "--output", str(output)]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["status"], "NORMALIZED")
            rows = json.loads(output.read_text())
            self.assertEqual(rows[0]["rss_bytes"], 100)
            self.assertEqual(rows[0]["raw_observations_file_sha256"], "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest())
            before = output.read_bytes()
            self.assertNotEqual(subprocess.run(command, capture_output=True, timeout=10).returncode, 0)
            self.assertEqual(output.read_bytes(), before)

    def test_metrics_missing_nonfinite_incoherent_or_duplicate_container_fail(self):
        mutations = [lambda v: v["raw"]["atlas_metrics"].update(status="UNAVAILABLE"),
                     lambda v: v["raw"]["atlas_metrics"]["values"].pop("atlas_requests_active"),
                     lambda v: v["raw"]["atlas_metrics"]["values"].update(atlas_requests_active=float("inf")),
                     lambda v: v["raw"]["atlas_metrics"]["active_requests"].update(value=2),
                     lambda v: v["raw"]["atlas_metrics"].update(allowlisted_raw_lines=[]),
                     lambda v: v["raw"]["containers"].append(copy.deepcopy(v["raw"]["containers"][0]))]
        for mutation in mutations:
            value = raw(); mutation(value)
            with self.subTest(mutation=mutations.index(mutation)), self.assertRaises(ValueError): normalize([value], ID, "spark", "oom_kill")


if __name__ == "__main__":
    unittest.main()
