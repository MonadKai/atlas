#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Read an optional, already exported Nsight Systems SQLite capture.

No profiler, GPU, process control, or optional Python dependency is invoked.
--pid is the numeric spark PID recorded by the profiler in its target namespace,
not automatically the Docker host PID. Preserve the namespace mapping, capture
window, actual HTTP receipts and diagnostic-instance identity alongside output.
Kernel evidence is scoped to that process and capture window, not all workloads
or private SSM/MTP state. Synthetic parser tests never constitute GPU evidence.
"""
import argparse
from collections import defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote


def require(condition, message):
    if not condition:
        raise ValueError(message)


def columns(db, table, expected):
    actual = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
    require(set(expected) <= actual, f"missing required schema in {table}: {sorted(set(expected) - actual)}")


def sha256(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def inspect_profile(path, pid):
    require(type(pid) is int and 0 < pid < (1 << 24), "expected numeric target PID in 24-bit range")
    path = Path(path).resolve()
    require(path.is_file(), "exported SQLite input does not exist")
    digest = sha256(path)
    # The completed export is immutable for this inspection; do not create WAL,
    # SHM, temporary schema or rewrite vendor data as some statistics scripts do.
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        columns(db, "StringIds", ["id", "value"])
        columns(db, "CUPTI_ACTIVITY_KIND_KERNEL", ["start", "end", "deviceId", "contextId",
                "streamId", "correlationId", "globalPid", "demangledName"])
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        apis = set()
        api_tables = []
        for table in ("CUPTI_ACTIVITY_KIND_DRIVER", "CUPTI_ACTIVITY_KIND_RUNTIME"):
            if table not in tables:
                continue
            columns(db, table, ["start", "end", "globalTid", "correlationId"])
            for correlation, start, end in db.execute(
                    f'SELECT correlationId,start,end FROM "{table}" '
                    'WHERE ((globalTid >> 24) & 16777215)=?', (pid,)):
                require(type(correlation) is int and correlation >= 0 and
                        type(start) is int and type(end) is int and 0 <= start <= end,
                        "invalid CUDA API correlation or timestamps")
                apis.add(correlation)
            api_tables.append(table)
        require(api_tables and apis, "no target-process CUDA API correlation evidence")
        summary = defaultdict(lambda: {"events": 0, "duration_ns": 0})
        devices, contexts = set(), set()
        events, correlated, earliest, latest = 0, 0, None, None
        query = """SELECT k.start,k.end,k.deviceId,k.contextId,k.streamId,
                          k.correlationId,s.value
                   FROM CUPTI_ACTIVITY_KIND_KERNEL k
                   LEFT JOIN StringIds s ON s.id=k.demangledName
                   WHERE ((k.globalPid >> 24) & 16777215)=?"""
        for start, end, device, context, stream, correlation, name in db.execute(query, (pid,)):
            require(type(start) is int and type(end) is int and 0 <= start < end,
                    "invalid/zero CUDA kernel duration")
            require(isinstance(name, str) and name.strip(), "unresolved/empty CUDA kernel name")
            require(all(type(v) is int and v >= 0 for v in (device, context, stream, correlation)),
                    "invalid CUDA kernel device/context/stream/correlation identity")
            events += 1
            correlated += correlation in apis
            summary[name]["events"] += 1
            summary[name]["duration_ns"] += end - start
            devices.add(device)
            contexts.add(context)
            earliest = start if earliest is None else min(earliest, start)
            latest = end if latest is None else max(latest, end)
        require(events > 0, "no actual CUDA kernel events for expected PID")
        require(correlated > 0, "kernel events have no same-process CUDA API correlation")
    require(sha256(path) == digest,
            "SQLite input changed during read-only inspection")
    return {"schema": "atlas-private-kernel-evidence-v1", "status": "PASS",
            "sqlite": str(path), "sqlite_sha256": digest, "target_pid": pid,
            "pid_semantics": "profiler target namespace; (globalPid >> 24) & 0xFFFFFF",
            "kernel_events": events, "correlated_kernel_events": correlated,
            "uncorrelated_kernel_events": events - correlated,
            "api_tables": api_tables, "devices": sorted(devices), "contexts": sorted(contexts),
            "first_kernel_start_ns": earliest, "last_kernel_end_ns": latest,
            "kernels": [{"name": name, **values} for name, values in sorted(summary.items())],
            "scope": "actual named CUDA kernel execution in this PID/capture; correlate quiet request window externally",
            "ssm_internal_restore": "NOT_RUN", "mtp_internal_execution": "NOT_RUN",
            "performance_qualification": "NOT_RUN", "production_qualification": "not_qualified"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, type=Path)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    root = Path(__file__).resolve().parent
    if output == root or root in output.parents:
        parser.error("output must be outside the immutable release bundle")
    if output.exists():
        parser.error("output must be a new JSON file")
    try:
        result = inspect_profile(args.sqlite, args.pid)
    except (ValueError, sqlite3.Error, OSError) as error:
        result = {"schema": "atlas-private-kernel-evidence-v1", "status": "FAIL",
                  "error": f"{type(error).__name__}: {error}"}
    with output.open("x") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "output": str(output)}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
