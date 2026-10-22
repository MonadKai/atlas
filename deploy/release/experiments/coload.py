#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded request-only consumption of pinned external ASR, BR and TEN tools.

No container, deployment, installation, repair or lifecycle operation is exposed.
External publisher code is trusted executable input, pinned before execution.
"""

import argparse
import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
import urllib.request

SERVICES = ("asr", "br", "ten")
REQUIRED_FILES = {"asr_client", "asr_scorer", "asr_audio", "asr_fixtures",
                  "br_client", "br_audio", "ten_client"}


def number(value, minimum, maximum):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise ValueError("invalid explicit numeric bound")


def validate_site(value):
    """Pure shape and bound checks; path existence and hashes are separate I/O."""
    if value["schema_version"] != "atlas-coload-v1":
        raise ValueError("unsupported site schema")
    number(value["cycles"], 1, 100000)
    if type(value["cycles"]) is not int:
        raise ValueError("integer cycles required")
    number(value["interval_seconds"], 0, 3600)
    if value["interval_semantics"] != "START_TO_START":
        raise ValueError("explicit START_TO_START cadence required")
    number(value["max_duration_seconds"], 1, 86400)
    number(value["request_timeout_seconds"], 1, 300)
    if value["expected_websockets_version"] != "12.0":
        raise ValueError("this legacy publisher client profile requires websockets 12.0")
    if not REQUIRED_FILES <= value["files"].keys():
        raise ValueError("missing pinned external tool or fixture")
    paths = []
    for identity in value["files"].values():
        if (not isinstance(identity["path"], str)
                or not Path(identity["path"]).is_absolute()
                or ".." in Path(identity["path"]).parts
                or type(identity["size_bytes"]) is not int or identity["size_bytes"] < 0
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", identity["sha256"])):
            raise ValueError("invalid pinned file identity")
        paths.append(identity["path"])
    if len(set(paths)) != len(paths):
        raise ValueError("duplicate file aliases are ambiguous")
    for name in SERVICES:
        endpoint = urlsplit(value[name]["endpoint"])
        schemes = {"http", "https"} if name == "br" else {"ws", "wss"}
        if (endpoint.scheme not in schemes or not endpoint.hostname
                or endpoint.username is not None or endpoint.password is not None
                or endpoint.query or endpoint.fragment):
            raise ValueError("explicit credential-free service endpoint required")
    asr = value["asr"]
    if asr["language"] not in {"zh", "en", "ja"}:
        raise ValueError("unsupported ASR language")
    number(asr["chunk_delay_seconds"], 0, 1)
    number(asr["maximum_error_rate"], 0, 1)
    number(asr["maximum_increase"], 0, 1)
    br = value["br"]
    if br["sample_rate_hz"] not in {8000, 16000}:
        raise ValueError("unsupported BR sample rate")
    for key in ("input_frames_per_message", "output_frames_per_message"):
        if type(br[key]) is not int or br[key] <= 0:
            raise ValueError("explicit BR framing required")
    return value


def site_digest(site):
    raw = json.dumps(site, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def bind_asr_fixture(site, manifest):
    """Bind the explicit audio/reference and preserve publisher policy ceilings."""
    args = site["asr"]
    fixture = manifest["files"][args["fixture_name"]]
    policy = manifest["regression_policy"]
    if (fixture["language"] != args["language"]
            or fixture["sha256"] != site["files"]["asr_audio"]["sha256"]
            or not isinstance(fixture["reference"], str) or not fixture["reference"].strip()
            or args["maximum_error_rate"] > policy["maximum_error_rate"]
            or args["maximum_increase"] > policy["maximum_increase_over_current"]):
        raise ValueError("ASR fixture identity/reference/publisher policy mismatch")
    number(fixture["current_error_rate"], 0, 1)
    return fixture


def select_br_profile(capabilities, desired):
    matches = []
    offer = {key: desired[key] for key in
             ("input_frames_per_message", "output_frames_per_message")}
    for profile in capabilities["qualified_profiles"]:
        if (profile["semantic_profile"] == desired["semantic_profile"]
                and profile["suppression"] == desired["suppression"]
                and profile["input"]["sample_rate_hz"] == desired["sample_rate_hz"]
                and offer in profile["framing"]["offers"]):
            matches.append(profile)
    if len(matches) != 1:
        raise ValueError("exactly one current BR profile and framing must match")
    return matches[0], offer


def evaluate_results(site, records):
    expected = {(cycle, service) for cycle in range(site["cycles"]) for service in SERVICES}
    seen = [(r["cycle"], r["service"]) for r in records]
    bound = all(r.get("site_sha256") == site_digest(site) for r in records)
    passed = (set(seen) == expected and len(seen) == len(expected) and bound
              and all(r.get("status") == "PASS" for r in records))
    return {"status": "PASS" if passed else "FAIL", "site_sha256": site_digest(site),
            "expected_requests": len(expected), "recorded_requests": len(records),
            "successful_requests": sum(r.get("status") == "PASS" for r in records),
            "complete_unique_cycles": set(seen) == expected and len(seen) == len(expected),
            "all_records_bound": bound,
            "unexecuted_requests": [{"cycle": c, "service": s} for c, s in sorted(expected - set(seen))],
            "scope": "fixed external publisher functional co-load; not a performance qualification"}


class IORouter:
    """All file, import, network, clock and request-process effects live here."""

    @staticmethod
    def read_json(path):
        return json.loads(Path(path).read_text())

    @staticmethod
    def save(path, value):
        with Path(path).open("x") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")

    @staticmethod
    def check_inputs(site):
        import websockets
        if websockets.__version__ != site["expected_websockets_version"]:
            raise ValueError("selected interpreter does not match pinned websockets")
        pinned = {Path(item["path"]) for item in site["files"].values()}
        for item in site["files"].values():
            path = Path(item["path"])
            if path.is_symlink() or not path.is_file() or path.resolve() != path:
                raise ValueError("missing, symlinked or overlaid pinned input: " + str(path))
            raw = path.read_bytes()
            if len(raw) != item["size_bytes"] or "sha256:" + hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError("pinned input size or SHA mismatch: " + str(path))
        # Publisher imports may use sibling modules. Every such Python source
        # in the selected directory must also be an explicit fixed input.
        for key in ("asr_client", "asr_scorer", "br_client", "ten_client"):
            for sibling in Path(site["files"][key]["path"]).parent.glob("*.py"):
                if sibling not in pinned:
                    raise ValueError("unbound publisher sibling module: " + str(sibling))
        bind_asr_fixture(site, IORouter.read_json(site["files"]["asr_fixtures"]["path"]))
        return {"python": sys.executable, "python_version": sys.version.split()[0],
                "websockets_version": websockets.__version__,
                "runner_sha256": "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "files": site["files"], "site_sha256": site_digest(site), "status": "PASS"}

    @staticmethod
    def load(path, name):
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(Path(path).parent))
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def request(self, service, site):
        """Delegate wire and numerical oracles to fixed publisher modules."""
        files = site["files"]
        if service == "asr":
            client = self.load(files["asr_client"]["path"], "pinned_legacy_asr")
            scorer = self.load(files["asr_scorer"]["path"], "pinned_publisher_asr_score")
            args = site["asr"]
            fixture = bind_asr_fixture(site, self.read_json(files["asr_fixtures"]["path"]))
            result = client.run(args["endpoint"], Path(files["asr_audio"]["path"]),
                                args["language"], site["request_timeout_seconds"],
                                chunk_delay_seconds=args["chunk_delay_seconds"], duplex=True)
            score = scorer.error_rate(fixture["reference"],
                                      " ".join(e["text"] for e in result["finals"]),
                                      args["language"])
            passed = (result["passed"] and score <= args["maximum_error_rate"]
                      and score <= fixture["current_error_rate"] + args["maximum_increase"])
            return {"status": "PASS" if passed else "FAIL", "publisher_result": result,
                    "reference_error_rate": score, "reference_fixture": fixture}
        if service == "br":
            client = self.load(files["br_client"]["path"], "pinned_legacy_br")
            args = site["br"]
            with urllib.request.urlopen(args["endpoint"].rstrip("/") + "/v2/capabilities",
                                        timeout=site["request_timeout_seconds"]) as response:
                capabilities = json.loads(response.read())
            profile, offer = select_br_profile(capabilities, args)
            pcm = client.read_pcm(Path(files["br_audio"]["path"]), args["sample_rate_hz"])
            url = args["endpoint"].replace("http://", "ws://", 1).replace("https://", "wss://", 1)
            result = asyncio.run(client.one_batch(url.rstrip("/") + "/v2/ws",
                                                 profile, offer, pcm, 1, False))
            passed = len(result["sessions"]) == 1 and "error" not in result["sessions"][0]
            return {"status": "PASS" if passed else "FAIL", "publisher_result": result,
                    "profile": profile, "offer": offer, "input_frames": len(pcm) // 2}
        client = self.load(files["ten_client"]["path"], "pinned_ten_golden")
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            asyncio.run(client.run(site["ten"]["endpoint"]))
        return {"status": "PASS", "publisher_stdout": captured.getvalue(),
                "oracle": "unchanged publisher run_golden.run; three complete golden transcripts"}

    def worker(self, site, service, output):
        started = datetime.now(timezone.utc).isoformat()
        before = time.monotonic()
        try:
            inputs = self.check_inputs(site)
            result = self.request(service, site)
            self.check_inputs(site)
            result["inputs"] = inputs
        except Exception as error:
            result = {"status": "FAIL", "error_type": type(error).__name__, "error": str(error)}
        result.update(service=service, started_at=started,
                      ended_at=datetime.now(timezone.utc).isoformat(),
                      elapsed_seconds=time.monotonic() - before, site_sha256=site_digest(site))
        self.save(output, result)
        return 0 if result["status"] == "PASS" else 1

    def run(self, site, output):
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        frozen_inputs = self.check_inputs(site)
        self.save(output / "inputs.json", frozen_inputs)
        before = time.monotonic()
        origin = datetime.now(timezone.utc).isoformat()
        self.save(output / "site.json", site)
        self.save(output / "schedule.json", {
            "started_at": origin, "interval_semantics": site["interval_semantics"],
            "interval_seconds": site["interval_seconds"], "cycles": site["cycles"],
            "max_duration_seconds": site["max_duration_seconds"],
            "site_sha256": site_digest(site)})
        records = []
        for cycle in range(site["cycles"]):
            scheduled = before + cycle * site["interval_seconds"]
            remaining = site["max_duration_seconds"] - (time.monotonic() - before)
            if remaining <= 0:
                break
            time.sleep(min(max(0, scheduled - time.monotonic()), remaining))
            cycle_started = time.monotonic()
            for service in SERVICES:
                name = f"{cycle:06d}-{service}"
                path = output / (name + ".json")
                remaining = site["max_duration_seconds"] - (time.monotonic() - before)
                if remaining <= 0:
                    break
                command = [sys.executable, "-B", "-E", "-s", str(Path(__file__).resolve()),
                           "worker", "--site", str(output / "site.json"), "--service", service,
                           "--output", str(path)]
                started = time.monotonic()
                with (output / (name + ".stdout")).open("x") as stdout, (output / (name + ".stderr")).open("x") as stderr:
                    try:
                        done = subprocess.run(command, stdout=stdout, stderr=stderr,
                                              timeout=min(site["request_timeout_seconds"], remaining))
                        result = self.read_json(path) if path.is_file() else {"status": "FAIL", "reason": "worker emitted no result"}
                        if done.returncode:
                            result["status"] = "FAIL"
                        if result.get("site_sha256") != site_digest(site):
                            result["status"] = "FAIL"
                            result["reason"] = "worker result does not bind frozen site"
                        if (result.get("status") == "PASS" and result.get("inputs", {}).get("runner_sha256")
                                != frozen_inputs["runner_sha256"]):
                            result["status"] = "FAIL"
                            result["reason"] = "request worker changed after runner freeze"
                        result["worker_exit_code"] = done.returncode
                    except subprocess.TimeoutExpired:
                        result = {"status": "FAIL", "reason": "bounded external request worker timeout",
                                  "site_sha256": site_digest(site)}
                result.update(cycle=cycle, service=service,
                              cycle_scheduled_seconds=scheduled - before,
                              cycle_actual_start_seconds=cycle_started - before,
                              cycle_start_lateness_seconds=max(0, cycle_started - scheduled),
                              experiment_started_seconds=started - before,
                              experiment_completed_seconds=time.monotonic() - before)
                self.save(output / (name + ".receipt.json"), result)
                records.append(result)
        summary = evaluate_results(site, records)
        summary["elapsed_seconds"] = time.monotonic() - before
        summary["started_at"] = origin
        summary["completed_at"] = datetime.now(timezone.utc).isoformat()
        summary["interval_semantics"] = site["interval_semantics"]
        summary["runner_sha256"] = frozen_inputs["runner_sha256"]
        self.save(output / "summary.json", summary)
        print(json.dumps(summary))
        return 0 if summary["status"] == "PASS" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "run", "worker"))
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--service", choices=SERVICES)
    args = parser.parse_args()
    router = IORouter()
    site = validate_site(router.read_json(args.site))
    if args.command == "check":
        print(json.dumps(router.check_inputs(site), indent=2))
        return 0
    if args.output is None or args.command == "worker" and args.service is None:
        parser.error("output and worker service must be explicit")
    if args.command == "worker":
        return router.worker(site, args.service, args.output)
    return router.run(site, args.output.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
