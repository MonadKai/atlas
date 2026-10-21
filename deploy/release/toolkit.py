# SPDX-License-Identifier: AGPL-3.0-only
"""Portable release-owned primitives; no implicit service lifecycle operations."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_contract import load, validate_bundle

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("inspect")
    for name in ("render", "verify-assets", "guard", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--instance", type=Path, required=True)
        if name in {"verify-assets", "verify"}:
            command.add_argument("--output", type=Path, required=True)
        if name == "verify":
            command.add_argument("--endpoint", required=True)
        if name == "guard":
            command.add_argument("--snapshot", type=Path, required=True)
            command.add_argument("--desired", type=Path, required=True)
    args = parser.parse_args()
    try:
        release, capability, _ = validate_bundle(ROOT)
        if args.operation == "inspect":
            print(json.dumps(release, indent=2))
            return 0
        from instance import render
        declaration = load(args.instance)
        desired = render(ROOT, declaration)
        if args.operation == "render":
            print(json.dumps(desired, indent=2))
        elif args.operation == "verify-assets":
            from assets import verify_model
            if args.output.resolve().is_relative_to(ROOT):
                raise ValueError("evidence output must be outside immutable release bundle")
            result = verify_model(Path(declaration["model_root"]), load(ROOT / "models/qwen36-nvfp4.json"))
            with args.output.open("x") as stream:
                json.dump(result, stream, indent=2)
            print(json.dumps({"status": result["status"], "output": str(args.output)}))
        elif args.operation == "guard":
            from instance import guard
            result = guard(declaration, load(args.desired), load(args.snapshot))
            print(json.dumps(result, indent=2))
        else:
            return subprocess.run([sys.executable, "-B", "-E", "-s", str(ROOT / "verify.py"), "--instance", str(args.instance), "--endpoint", args.endpoint, "--output", str(args.output)], cwd=ROOT, check=False).returncode
    except (ValueError, OSError, KeyError, TypeError) as error:
        print("Atlas release operation failed: " + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
