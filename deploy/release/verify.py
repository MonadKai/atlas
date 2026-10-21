# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded actual-runtime and public acceptance; no service lifecycle operations."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from release_contract import canonical, load, require, validate_bundle
from instance import render
from assets import verify_model
from runtime_identity import verify_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.resolve().is_relative_to(ROOT), "evidence must be outside immutable bundle")
    args.output.mkdir(parents=True, exist_ok=False)
    receipt = {"schema_version": "service-verification-receipt-v1", "product_ref": "product:atlas-qwen36-nvfp4", "scope": "public-contract", "release_qualification": False, "started_at": datetime.now(timezone.utc).isoformat(), "status": "FAIL"}
    try:
        release, capability, _ = validate_bundle(ROOT)
        instance = load(args.instance)
        desired = render(ROOT, instance)
        endpoint = urlsplit(args.endpoint)
        require(endpoint.scheme == "http" and endpoint.hostname == instance["host_ip"] and endpoint.port == instance["host_port"] and not endpoint.username and not endpoint.password and endpoint.path in ("", "/", "/v1", "/v1/") and not endpoint.query and not endpoint.fragment, "endpoint must match the actual bound development instance")
        assets = verify_model(Path(instance["model_root"]), load(ROOT / "models/qwen36-nvfp4.json"))
        (args.output / "assets.json").write_text(json.dumps(assets, indent=2)+"\n")
        runtime = verify_runtime(release, instance, desired)
        (args.output / "runtime.json").write_text(json.dumps(runtime, indent=2)+"\n")
        base = f"http://{instance['host_ip']}:{instance['host_port']}"
        for suite in ("public", "stream", "errors"):
            command = [sys.executable, "-B", "-E", "-s", str(ROOT / "acceptance.py"), "--base-url", base, "--model", instance["model_alias"], "--suite", suite, "--output", str(args.output / suite)]
            with (args.output / (suite+".stdout.txt")).open("x") as stdout, (args.output / (suite+".stderr.txt")).open("x") as stderr:
                result = subprocess.run(command, cwd=ROOT, stdout=stdout, stderr=stderr, timeout=240, check=False)
            require(result.returncode == 0, "actual public acceptance failed: " + suite)
        receipt.update(status="PASS", release_ref=release["release_ref"], source_revision=release["source_revision"], tool_revision=release["tool_revision"], instance_sha256=canonical(instance), checks=["immutable image/native and full declared runtime configuration", "all local model files and MTP header/index integrity", "real arithmetic/factual/automatic tool call", "SSE completion/cancellation/resource gauge/subsequent inference", "errors and subsequent recovery"], gpu_kernel_execution="separate request-correlated Nsight evidence required", context_boundary="separate acceptance.py --suite context", prefix_cache="separate acceptance.py --suite cache", performance="NOT_RUN", stability="NOT_RUN", business_accuracy="NOT_RUN", production="not_qualified")
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        # No observed command/env values; raw protocol responses live only in their
        # explicitly selected evidence directory and contain no credentials.
        receipt["error"] = str(error)
    finally:
        receipt["completed_at"] = datetime.now(timezone.utc).isoformat()
        (args.output / "verification.json").write_text(json.dumps(receipt, indent=2)+"\n")
    print(json.dumps({"status": receipt["status"], "scope": receipt["scope"], "output": str(args.output)}))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
