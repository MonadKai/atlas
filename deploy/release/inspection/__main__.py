# SPDX-License-Identifier: AGPL-3.0-only
"""Independent read-only atomic inspection CLI; never performs cleanup or lifecycle."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import subprocess
import time
from .paths import IORouter, assess_assets, assess_cleanup, assess_mounts, container_references, observe_plan, observe_references
from .resources import ResourceIORouter, observe_resources


def load(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    return json.loads(Path(path).read_text(), object_pairs_hook=unique)


def emit(path, value):
    with Path(path).open("x") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2) + "\n")


def normalized_mounts(container):
    return {"container_id": container["Id"], "mounts": [{"type": m["Type"], "source": None if m["Type"] == "tmpfs" else m.get("Source"), "destination": m["Destination"], "read_only": not m["RW"]}
            for m in container.get("Mounts", [])]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    asset = commands.add_parser("assets"); asset.add_argument("--plan", required=True)
    mounts = commands.add_parser("mounts"); mounts.add_argument("--expected", required=True)
    cleanup = commands.add_parser("cleanup-references")
    cleanup.add_argument("--candidates", required=True); cleanup.add_argument("--protected", required=True)
    resource = commands.add_parser("resources")
    resource.add_argument("--container", action="append", required=True)
    resource.add_argument("--metrics-url", help="Optional credential-free HTTP /metrics; only allowlisted numeric values saved")
    resource.add_argument("--samples", type=int, required=True); resource.add_argument("--interval-seconds", type=float, required=True)
    for command in (asset, mounts, cleanup, resource): command.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        router = IORouter()
        now = datetime.now(timezone.utc).isoformat()
        if args.command == "assets":
            plan = load(args.plan); observed = observe_plan(plan, router)
            result = {"observed_at": now, "observation": observed, "verdict": assess_assets(plan, observed)}
        elif args.command == "mounts":
            expected = load(args.expected)
            objects = json.loads(router.docker(["container", "inspect", expected["container_id"]]))
            if len(objects) != 1: raise ValueError("ambiguous inspected container")
            observed = normalized_mounts(objects[0])
            result = {"observed_at": now, "observation": observed, "verdict": assess_mounts(expected, observed)}
        elif args.command == "cleanup-references":
            candidates = observe_references(load(args.candidates), router)
            # Never infer an empty mount set from a daemon/permission failure.
            objects = router.containers()
            refs = container_references(objects) + load(args.protected)
            grouped = {}
            for reference in refs: grouped.setdefault(reference["path"], []).append(reference["reason"])
            protected = observe_references([{"path": p, "reason": ";".join(v)} for p, v in sorted(grouped.items())], router)
            result = {"observed_at": now, "container_ids": [c["Id"] for c in objects], "candidates": candidates, "protected": protected, "verdict": assess_cleanup(candidates, protected)}
        else:
            if args.samples < 1 or args.samples > 10000 or not 0.1 <= args.interval_seconds <= 3600 or args.samples * args.interval_seconds > 86400:
                raise ValueError("bounded samples/interval within one day required")
            if len(set(args.container)) != len(args.container): raise ValueError("duplicate selected container")
            with Path(args.output).open("x") as stream:
                started = time.monotonic()
                for index in range(args.samples):
                    value = observe_resources(args.container, ResourceIORouter(), args.metrics_url)
                    value["sample_index"] = index
                    stream.write(json.dumps(value, sort_keys=True) + "\n"); stream.flush()
                    if index + 1 < args.samples: time.sleep(max(0, started + (index + 1) * args.interval_seconds - time.monotonic()))
            return 0
        emit(args.output, result)
        return 0 if result["verdict"]["status"] in {"PASS", "NO_REFERENCES_OBSERVED"} else 1
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "FAIL_CLOSED", "reason": type(error).__name__, "message": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
