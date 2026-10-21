#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic SQLite fixtures test parsing only; no profiler or lifecycle claims."""
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/release"))
from acceptance_profile import inspect_profile


def fixture(path, *, pid=23, correlation=17, end=150, name="nvfp4_moe_gemm", api=True):
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE StringIds(id INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE CUPTI_ACTIVITY_KIND_KERNEL(
            start INTEGER, end INTEGER, deviceId INTEGER, contextId INTEGER,
            streamId INTEGER, correlationId INTEGER, globalPid INTEGER, demangledName INTEGER);
        """)
        db.execute("INSERT INTO StringIds VALUES(1,?)", (name,))
        db.execute("INSERT INTO CUPTI_ACTIVITY_KIND_KERNEL VALUES(100,?,0,1,7,?,?,1)",
                   (end, correlation, pid << 24))
        if api:
            db.execute("CREATE TABLE CUPTI_ACTIVITY_KIND_DRIVER(start INTEGER,end INTEGER,globalTid INTEGER,correlationId INTEGER)")
            db.execute("INSERT INTO CUPTI_ACTIVITY_KIND_DRIVER VALUES(90,110,?,17)", ((pid << 24) | 24,))


class ProfileOracle(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "profile.sqlite"

    def test_valid_target_kernel_and_driver_correlation(self):
        fixture(self.path)
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        result = inspect_profile(self.path, 23)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["kernel_events"], 1)
        self.assertEqual(result["correlated_kernel_events"], 1)
        self.assertEqual(result["kernels"][0]["name"], "nvfp4_moe_gemm")
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertFalse(Path(str(self.path) + "-wal").exists())

    def test_wrong_pid_rejected(self):
        fixture(self.path)
        with self.assertRaises(ValueError):
            inspect_profile(self.path, 24)

    def test_empty_or_graph_only_or_missing_schema_rejected(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE CUPTI_ACTIVITY_KIND_CUDA_GRAPH(start INTEGER,end INTEGER)")
            db.execute("INSERT INTO CUPTI_ACTIVITY_KIND_CUDA_GRAPH VALUES(1,2)")
        with self.assertRaises(ValueError):
            inspect_profile(self.path, 23)
        self.path.unlink()
        fixture(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM CUPTI_ACTIVITY_KIND_KERNEL")
        with self.assertRaises(ValueError):
            inspect_profile(self.path, 23)

    def test_invalid_duration_name_and_uncorrelated_rejected(self):
        for changes in [{"end": 100}, {"name": ""}, {"name": None}, {"correlation": 99}, {"api": False}]:
            if self.path.exists():
                self.path.unlink()
            fixture(self.path, **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                inspect_profile(self.path, 23)

    def test_wrong_api_process_does_not_match_same_correlation(self):
        fixture(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE CUPTI_ACTIVITY_KIND_DRIVER SET globalTid=?", ((99 << 24) | 24,))
        with self.assertRaises(ValueError):
            inspect_profile(self.path, 23)

    def test_24bit_pid_field_is_decoded_without_host_namespace_guess(self):
        fixture(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE CUPTI_ACTIVITY_KIND_KERNEL SET globalPid=globalPid+?", (1 << 48,))
            db.execute("UPDATE CUPTI_ACTIVITY_KIND_DRIVER SET globalTid=globalTid+?", (1 << 48,))
        self.assertEqual(inspect_profile(self.path, 23)["kernel_events"], 1)

    def test_absent_input_not_created_and_invalid_pid_rejected(self):
        with self.assertRaises(ValueError):
            inspect_profile(self.path, 23)
        self.assertFalse(self.path.exists())
        fixture(self.path)
        for pid in [True, 0, -1, 1 << 24]:
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                inspect_profile(self.path, pid)


if __name__ == "__main__":
    unittest.main()
