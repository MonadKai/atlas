#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Version-matched request-only acceptance for one private development profile.

No source checkout, optional Python package, container control, or compiler is
required. Output must be a new caller-owned directory outside the release bundle.
Each suite makes bounded real requests; a failed attempt keeps its raw evidence.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True

from acceptance_http import Recorder, write_json
from acceptance_suites import SUITES


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--suite", required=True, choices=sorted(SUITES))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    output = args.output.resolve()
    if output == root or root in output.parents:
        parser.error("output must be outside the immutable release bundle")
    output.mkdir(parents=True, exist_ok=False)
    started = time.time()
    receipt = {"schema": "atlas-private-acceptance-receipt-v1", "suite": args.suite,
               "model": args.model, "started_unix": started,
               "scope": "GB10 ARM64 Qwen3.6 NVFP4 private development; not upstream full matrix qualification"}
    try:
        raw = (root / "fixtures/cases.json").read_bytes()
        cases = json.loads(raw)
        if cases.get("schema") != "atlas-private-acceptance-v1":
            raise ValueError("unsupported acceptance fixture version")
        receipt.update(fixture_sha256=hashlib.sha256(raw).hexdigest(), qualifications=cases["qualifications"])
        client = Recorder(args.base_url, output, cases["request_timeout_seconds"])
        receipt["result"] = SUITES[args.suite](client, args.model, cases)
        receipt["status"] = "PASS"
    except Exception as error:
        receipt.update(status="FAIL", error=f"{type(error).__name__}: {error}")
    receipt["elapsed_seconds"] = time.time() - started
    write_json(output / "acceptance.json", receipt)
    print(json.dumps({"status": receipt["status"], "suite": args.suite,
                      "receipt": str(output / "acceptance.json")}, ensure_ascii=False))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
