# SPDX-License-Identifier: AGPL-3.0-only
"""Atomic private GB10 build operations; no push, deployment, or lifecycle I/O.

The committed profile fixes the current CUDA 13.0 path. ``inspect-inputs``
freezes the actual local environment; ``compile`` rechecks it and compiles into
an initially empty task directory; ``runtime-context`` packages that result.
Python 3.12+ standard library only. The engine image contains no Python.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "deploy/release/build_profile.json"
REVISION = re.compile(r"[0-9a-f]{40}\Z")


def file_identity(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"required regular file missing: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return {"size_bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def verify_file(path: Path, expected: dict) -> None:
    if file_identity(path) != {k: expected[k] for k in ("size_bytes", "sha256")}:
        raise ValueError(f"file identity mismatch: {path}")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: Path, data: dict) -> None:
    if path.exists():
        raise ValueError(f"refusing to overwrite evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def run(argv: list[str], **kwargs) -> str:
    return subprocess.check_output(argv, text=True, **kwargs).strip()


def clean_source(source: Path, revision: str) -> None:
    if not REVISION.fullmatch(revision):
        raise ValueError("source revision must be a full lowercase Git SHA")
    if run(["git", "-C", str(source), "rev-parse", "HEAD"]) != revision:
        raise ValueError("source revision differs from current HEAD")
    if run(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"]):
        raise ValueError("source must be clean and committed")


def tree_identity(directory: Path) -> dict:
    """Freeze native/toolchain inputs, including symlink targets and contents."""
    if not directory.is_dir():
        raise ValueError(f"required input directory missing: {directory}")
    members = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            item = file_identity(path)
            if path.is_symlink():
                item["symlink_target"] = os.readlink(path)
            members[str(path.relative_to(directory))] = item
        elif path.is_symlink():
            # Directory links must resolve; their referent is separately pinned
            # by declared input roots (CUDA include/lib64 are examples).
            if not path.exists():
                raise ValueError(f"broken build-input symlink: {path}")
            members[str(path.relative_to(directory))] = {"symlink_target": os.readlink(path)}
    digest = hashlib.sha256(json.dumps(members, sort_keys=True).encode()).hexdigest()
    return {"sha256": digest, "members": members}


def environment_identity(profile: dict, rust: Path, cuda: Path) -> dict:
    paths = {"rust": rust, "cuda": cuda}
    tools = {}
    for name, spec in profile["tools"].items():
        path = (paths[spec["root"]] / spec["path"]) if "root" in spec else Path(spec["path"])
        verify_file(path, spec)
        tools[name] = {"path": str(path.resolve()), **file_identity(path)}
    libraries = []
    for spec in profile["runtime_libraries"]:
        verify_file(Path(spec["host_path"]), spec)
        libraries.append(spec)
    trees = {}
    for name, spec in profile["input_trees"].items():
        path = (paths[spec["root"]] / spec["path"]) if "root" in spec else Path(spec["path"])
        trees[name] = {"path": str(path.resolve()), **tree_identity(path)}
    notices = {name: {"path": path, **file_identity(Path(path))} for name, path in profile["notices"].items()}
    return {"tools": tools, "runtime_libraries": libraries, "input_trees": trees, "notices": notices}


def inspect_inputs(args) -> dict:
    source = args.source.resolve()
    clean_source(source, args.source_revision)
    if args.profile.resolve() != source / "deploy/release/build_profile.json":
        raise ValueError("profile must be the fixed source-owned private profile")
    if file_identity(Path(__file__)) != file_identity(source / "deploy/release/build.py"):
        raise ValueError("build tool differs from declared source revision")
    profile = read_json(args.profile)
    if profile["schema_version"] != "atlas-private-build-profile-v1":
        raise ValueError("unsupported private build profile")
    if platform.machine() not in {"aarch64", "arm64"}:
        raise ValueError("private profile requires native AArch64")
    environment = environment_identity(profile, args.rust_toolchain.resolve(), args.cuda_home.resolve())
    git_objects = {
        rel: run(["git", "-C", str(source), "rev-parse", f"{args.source_revision}:{rel}"])
        for rel in profile["source_inputs"]
    }
    return {
        "schema_version": "atlas-private-build-input-lock-v1",
        "source_revision": args.source_revision,
        "source_tree": run(["git", "-C", str(source), "rev-parse", f"{args.source_revision}^{{tree}}"]),
        "source_inputs": git_objects,
        "cargo_lock": file_identity(source / "Cargo.lock"),
        "profile": profile,
        "profile_identity": file_identity(args.profile),
        "rust_toolchain": str(args.rust_toolchain.resolve()),
        "cuda_home": str(args.cuda_home.resolve()),
        "environment": environment,
        "scope": "private GB10 single-model development; not upstream matrix or production qualification",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def validate_lock(lock: dict, source: Path, revision: str) -> None:
    if lock["schema_version"] != "atlas-private-build-input-lock-v1":
        raise ValueError("unsupported build input lock")
    clean_source(source, revision)
    if file_identity(Path(__file__)) != file_identity(source / "deploy/release/build.py"):
        raise ValueError("build tool differs from declared source revision")
    if lock["source_revision"] != revision:
        raise ValueError("input lock source revision mismatch")
    verify_file(source / "deploy/release/build_profile.json", lock["profile_identity"])
    if read_json(source / "deploy/release/build_profile.json") != lock["profile"]:
        raise ValueError("profile body mismatch")
    for rel, oid in lock["source_inputs"].items():
        if run(["git", "-C", str(source), "rev-parse", f"{revision}:{rel}"]) != oid:
            raise ValueError(f"source input identity mismatch: {rel}")
    actual = environment_identity(lock["profile"], Path(lock["rust_toolchain"]), Path(lock["cuda_home"]))
    if actual != lock["environment"]:
        raise ValueError("native/toolchain input lock identity mismatch")


def compile_environment(lock: dict, target: Path, cargo_home: Path) -> dict:
    """No ambient compiler flags, optional backends, secrets, or stub settings."""
    rust, cuda = lock["rust_toolchain"], lock["cuda_home"]
    return {
        "HOME": os.environ["HOME"],
        "PATH": f"{rust}/bin:{cuda}/bin:/usr/bin:/bin",
        "CARGO_HOME": str(cargo_home),
        "CARGO_TARGET_DIR": str(target),
        "RUSTC": f"{rust}/bin/rustc",
        "RUSTUP_TOOLCHAIN": lock["profile"]["rust_version"],
        "CUDA_HOME": cuda,
        "LIBCLANG_PATH": lock["profile"]["libclang_path"],
        "LD_LIBRARY_PATH": f"{cuda}/lib64:/usr/lib/aarch64-linux-gnu",
        "CARGO_BUILD_JOBS": str(lock["profile"]["build_jobs"]),
        "AVAROK_TARGET_HW": "gb10",
        "AVAROK_TARGET_MODEL": "qwen3.6-35b-a3b",
        "AVAROK_TARGET_QUANT": "nvfp4",
    }


def kernel_proof(target: Path, profile: dict) -> dict:
    expected = profile["kernel_closure"]
    outputs = list((target / "release/build").glob("avarok-kernels-*/output"))
    matches = []
    for path in outputs:
        text = path.read_text()
        for line in text.splitlines():
            prefix = "cargo:rustc-env=AVAROK_TARGET_CLOSURES="
            if line.startswith(prefix):
                closure = json.loads(line[len(prefix):])
                if closure == expected:
                    if "183/183 unique nvcc invocations (0 cache hits" not in text:
                        raise ValueError("fresh complete native PTX compilation was not evidenced")
                    matches.append({"file": str(path), **file_identity(path), "closure": closure})
    if len(matches) != 1:
        raise ValueError("missing or ambiguous expected real CUDA kernel closure")
    return matches[0]


def compile_source(args) -> dict:
    source, target = args.source.resolve(), args.target_dir.resolve()
    lock = read_json(args.input_lock)
    validate_lock(lock, source, args.source_revision)
    if not args.target_dir.is_absolute() or target == source or source in target.parents:
        raise ValueError("target directory must be an explicit absolute task-owned path outside the source")
    if target.exists() and any(target.iterdir()):
        raise ValueError("compile requires an empty task target; never reuses an existing binary")
    if not args.cargo_home.is_absolute() or not args.cargo_home.is_dir():
        raise ValueError("explicit existing Cargo cache directory required")
    target.mkdir(parents=True, exist_ok=True)
    log = args.output.with_suffix(".cargo.txt")
    if log.exists() or args.output.exists():
        raise ValueError("refusing to overwrite compile evidence")
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [str(Path(lock["rust_toolchain"]) / "bin/cargo"), "build", "--release", "--locked", "--offline", "-p", "spark-server", "--bin", "spark", "--message-format=json-render-diagnostics"]
    started = time.time_ns()
    env = compile_environment(lock, target, args.cargo_home.resolve())
    with log.open("w") as stream:
        result = subprocess.run(argv, cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise ValueError(f"real source build failed (exit {result.returncode}); evidence retained: {log}")
    binary = target / "release/spark"
    if not binary.is_file() or binary.stat().st_mtime_ns < started:
        raise ValueError("newly built spark binary missing")
    artifacts = []
    for line in log.read_text().splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("reason") == "compiler-artifact" and item.get("executable") == str(binary):
            artifacts.append(item)
    if len(artifacts) != 1 or artifacts[0].get("fresh"):
        raise ValueError("Cargo did not report a freshly compiled spark executable")
    proof = kernel_proof(target, lock["profile"])
    # Source remains fixed during this potentially long operation.
    clean_source(source, args.source_revision)
    return {
        "schema_version": "atlas-private-build-receipt-v1",
        "source_revision": args.source_revision,
        "tool_revision": args.source_revision,
        "input_lock": {"path": str(args.input_lock.resolve()), **file_identity(args.input_lock)},
        "binary": {"path": str(binary), **file_identity(binary)},
        "kernel_proof": proof,
        "cargo_command": argv,
        "cargo_cache": {"path": str(args.cargo_home.resolve()), "scope": "registry/git source cache only; target initially empty"},
        "compile_environment": {k: v for k, v in env.items() if k != "HOME"},
        "compile_log": {"path": str(log.resolve()), **file_identity(log)},
        "started_at_ns": started,
        "completed_at_ns": time.time_ns(),
        "qualification": {"source_build": "PASS", "byte_reproducibility": "NOT_RUN", "runtime": "NOT_RUN", "upstream_matrix": "NOT_RUN", "production": "not_qualified"},
    }


def linked_libraries(binary: Path) -> dict:
    text = run(["ldd", str(binary)])
    if "not found" in text:
        raise ValueError("unresolved native library")
    return dict(re.findall(r"^\s*(\S+) => (/\S+) ", text, re.MULTILINE))


def runtime_context(args) -> dict:
    source = args.source.resolve()
    receipt = read_json(args.build_receipt)
    if receipt["schema_version"] != "atlas-private-build-receipt-v1":
        raise ValueError("unsupported source-build receipt")
    lock_path = Path(receipt["input_lock"]["path"])
    verify_file(lock_path, receipt["input_lock"])
    lock = read_json(lock_path)
    validate_lock(lock, source, receipt["source_revision"])
    binary = Path(receipt["binary"]["path"])
    verify_file(binary, receipt["binary"])
    verify_file(Path(receipt["compile_log"]["path"]), receipt["compile_log"])
    proof = kernel_proof(binary.parents[1], lock["profile"])
    if proof != receipt["kernel_proof"]:
        raise ValueError("kernel build receipt mismatch")
    destination = args.context.resolve()
    if destination.exists():
        raise ValueError("context must be a new task-owned directory")
    libraries = linked_libraries(binary)
    allowed = {item["soname"] for item in lock["environment"]["runtime_libraries"]}
    glibc = {"libc.so.6", "libm.so.6", "libdl.so.2", "librt.so.1", "libpthread.so.0"}
    if set(libraries) - allowed - glibc - {"libcuda.so.1"} or not allowed <= set(libraries):
        raise ValueError("unexpected or missing native dependency in binary link closure")
    for item in lock["environment"]["runtime_libraries"]:
        verify_file(Path(libraries[item["soname"]]), item)
    destination.mkdir(parents=True)
    (destination / "runtime").mkdir()
    for item in lock["environment"]["runtime_libraries"]:
        shutil.copyfile(item["host_path"], destination / "runtime" / item["soname"])
    shutil.copyfile(binary, destination / "spark")
    (destination / "spark").chmod(0o755)
    shutil.copytree(source / "jinja-templates", destination / "jinja-templates")
    shutil.copyfile(source / "kernels/gb10/qwen3.6-35b-a3b/MODEL.toml", destination / "MODEL.toml")
    shutil.copyfile(source / "LICENSE", destination / "LICENSE")
    shutil.copyfile(source / "docker/gb10/Dockerfile.private-runtime", destination / "Dockerfile")
    archive = destination / "source.tar.gz"
    with archive.open("wb") as stream:
        subprocess.run(["git", "-C", str(source), "archive", "--format=tar.gz", "--prefix=atlas/", receipt["source_revision"]], stdout=stream, check=True)
    (destination / "notices").mkdir()
    for name, path in lock["profile"]["notices"].items():
        shutil.copyfile(path, destination / "notices" / name)
    shutil.copyfile(args.build_receipt, destination / "build-receipt.json")
    shutil.copyfile(lock_path, destination / "build-input-lock.json")
    manifest = {
        "schema_version": "atlas-private-runtime-context-v1",
        "source_revision": receipt["source_revision"],
        "base_image": lock["profile"]["runtime_base"],
        "binary": file_identity(destination / "spark"),
        "runtime_libraries": lock["environment"]["runtime_libraries"],
        "members": {str(path.relative_to(destination)): file_identity(path) for path in sorted(destination.rglob("*")) if path.is_file()},
        "driver_library": "libcuda.so.1 injected by NVIDIA Container Toolkit; never bundled",
        "qualification": receipt["qualification"],
    }
    write_json(destination / "runtime-manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect-inputs")
    inspect.add_argument("--source", type=Path, required=True)
    inspect.add_argument("--source-revision", required=True)
    inspect.add_argument("--profile", type=Path, required=True)
    inspect.add_argument("--rust-toolchain", type=Path, required=True)
    inspect.add_argument("--cuda-home", type=Path, required=True)
    compile_cmd = commands.add_parser("compile")
    compile_cmd.add_argument("--source", type=Path, required=True)
    compile_cmd.add_argument("--source-revision", required=True)
    compile_cmd.add_argument("--input-lock", type=Path, required=True)
    compile_cmd.add_argument("--target-dir", type=Path, required=True)
    compile_cmd.add_argument("--cargo-home", type=Path, required=True)
    context = commands.add_parser("runtime-context")
    context.add_argument("--source", type=Path, required=True)
    context.add_argument("--build-receipt", type=Path, required=True)
    context.add_argument("--context", type=Path, required=True)
    for item in (inspect, compile_cmd, context):
        item.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = {"inspect-inputs": inspect_inputs, "compile": compile_source, "runtime-context": runtime_context}[args.command](args)
        write_json(args.output, result)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({"status": "PASS", "operation": args.command, "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
