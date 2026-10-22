#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Atomic plan/materialize/request/judge operations; never controls a service."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.model import canonical_sha256, resource_trend, summarize, validate_plan, frozen_cases, verify_raw_observation
from experiments.plans import example_plan
from experiments.router import IORouter
from experiments.workload import execute, prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    example = subs.add_parser("example-plan")
    example.add_argument("--kind", choices=("performance", "soak"), required=True)
    example.add_argument("--output", type=Path, required=True)
    for command in ("prepare", "run"):
        child = subs.add_parser(command)
        child.add_argument("--plan", type=Path, required=True)
        child.add_argument("--endpoint", required=True)
        child.add_argument("--model", required=True)
        child.add_argument("--output", type=Path, required=True)
        if command == "prepare":
            child.add_argument("--fixtures", type=Path, required=True)
        else:
            child.add_argument("--workload", type=Path, required=True)
    judge = subs.add_parser("judge")
    judge.add_argument("--directory", type=Path, required=True)
    judge.add_argument("--output", type=Path, required=True)
    trend = subs.add_parser("resource-trend")
    trend.add_argument("--plan", type=Path, required=True)
    trend.add_argument("--samples", type=Path, required=True)
    trend.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; retain the previous attempt and choose a new path")
    if args.command == "example-plan":
        plan = example_plan(args.kind)
        validate_plan(plan)
        IORouter.write_json(args.output, plan)
        result = {"plan_sha256": canonical_sha256(plan), "output": str(args.output)}
    elif args.command == "prepare":
        plan, fixture = IORouter.read_json(args.plan), IORouter.read_json(args.fixtures)
        validate_plan(plan)
        count_output = args.output.with_name(args.output.name + ".counts")
        count_output.mkdir()
        router = IORouter(args.endpoint, count_output, args.model,
                          plan["request_timeout_seconds"], time.monotonic())
        workload = prepare(plan, fixture, args.model, router)
        IORouter.write_json(args.output, workload)
        result = {"workload_sha256": canonical_sha256(workload), "plan_sha256": canonical_sha256(plan)}
    elif args.command == "run":
        plan, workload = IORouter.read_json(args.plan), IORouter.read_json(args.workload)
        validate_plan(plan)
        args.output.mkdir()
        IORouter.write_json(args.output / "plan.json", plan)
        IORouter.write_json(args.output / "workload.json", workload)
        start = time.monotonic()
        router = IORouter(args.endpoint, args.output, args.model, plan["request_timeout_seconds"], start)
        router.run_deadline = start + plan["max_run_seconds"]
        rows = execute(plan, workload, router)
        elapsed = time.monotonic() - start
        IORouter.write_json(args.output / "execution.json", {"elapsed_seconds": elapsed,
            "plan_sha256": canonical_sha256(plan), "workload_sha256": canonical_sha256(workload),
            "scope": "request-only host caller; not Docker daemon or service lifecycle"})
        result = summarize(plan, rows, elapsed)
        IORouter.write_json(args.output / "result.json", result)
    elif args.command == "judge":
        plan = IORouter.read_json(args.directory / "plan.json")
        meta = IORouter.read_json(args.directory / "execution.json")
        if canonical_sha256(plan) != meta["plan_sha256"]:
            raise ValueError("frozen execution plan hash mismatch")
        workload = IORouter.read_json(args.directory / "workload.json")
        if canonical_sha256(workload) != meta["workload_sha256"] or workload["plan_sha256"] != meta["plan_sha256"]:
            raise ValueError("frozen workload hash mismatch")
        rows = [json.loads(line) for line in (args.directory / "observations.jsonl").read_text().splitlines()]
        cases = frozen_cases(plan, workload)
        for row in rows:
            request_id = row["request_id"]
            if not isinstance(request_id, str) or len(request_id) != 6 or not request_id.startswith("r") or not request_id[1:].isdigit():
                raise ValueError("invalid request evidence member")
            root = args.directory / request_id
            if IORouter.read_json(root / "observation.json") != row:
                raise ValueError("request observation/ledger mismatch")
            for member, key in (("request.json", "request_sha256"), ("response.sse-or-json", "response_sha256")):
                actual = "sha256:" + hashlib.sha256((root / member).read_bytes()).hexdigest()
                if actual != row[key]:
                    raise ValueError("request raw evidence hash mismatch: " + member)
            ordinal = int(request_id[1:]) - 1
            if not 0 <= ordinal < len(cases):
                raise ValueError("request ID outside frozen schedule")
            verify_raw_observation(row, cases[ordinal], workload["model"],
                (root / "request.json").read_bytes(), (root / "response.sse-or-json").read_bytes())
        result = summarize(plan, rows, meta["elapsed_seconds"])
        IORouter.write_json(args.output, result)
    else:
        result = resource_trend(IORouter.read_json(args.samples), IORouter.read_json(args.plan))
        IORouter.write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 1 if result.get("status") == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
