# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic build identity checks; never mock an external lifecycle."""
import importlib.util
import pathlib
import tempfile
import unittest
import json
import os
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("atlas_private_build", ROOT / "deploy/release/build.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)

class IdentityTests(unittest.TestCase):
    def test_reject_changed_native_input(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "native.so"
            path.write_bytes(b"fixed native bytes")
            expected = BUILD.file_identity(path)
            path.write_bytes(b"different native bytes")
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                BUILD.verify_file(path, expected)

    def test_missing_native_input_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "required regular file missing"):
                BUILD.file_identity(pathlib.Path(folder) / "missing.so")

    def test_tree_identity_tracks_bytes_and_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / "one").write_bytes(b"a")
            (root / "two").write_bytes(b"a")
            (root / "alias").symlink_to("one")
            first = BUILD.tree_identity(root)
            (root / "alias").unlink()
            (root / "alias").symlink_to("two")
            self.assertNotEqual(first, BUILD.tree_identity(root))

    def test_broken_native_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / "alias").symlink_to("absent")
            with self.assertRaisesRegex(ValueError, "broken build-input symlink"):
                BUILD.tree_identity(root)

    def test_evidence_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "receipt.json"
            BUILD.write_json(path, {"first": True})
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                BUILD.write_json(path, {"first": False})
            self.assertEqual(json.loads(path.read_text()), {"first": True})

    def test_compiler_environment_has_no_ambient_stub_or_flags(self):
        lock = {"rust_toolchain": "/rust", "cuda_home": "/cuda", "profile": {"rust_version": "1.93.1", "libclang_path": "/clang", "build_jobs": 4}}
        previous = {key: os.environ.get(key) for key in ("AVAROK_SKIP_BUILD", "RUSTFLAGS", "CUTLASS_HOME")}
        try:
            os.environ.update(AVAROK_SKIP_BUILD="1", RUSTFLAGS="--cfg wrong", CUTLASS_HOME="/wrong")
            env = BUILD.compile_environment(lock, pathlib.Path("/task/target"), pathlib.Path("/cache"))
            for key in previous:
                self.assertNotIn(key, env)
            self.assertEqual(env["AVAROK_TARGET_MODEL"], "qwen3.6-35b-a3b")
            self.assertEqual(env["CARGO_TARGET_DIR"], "/task/target")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_profile_does_not_switch_existing_native_path(self):
        profile = BUILD.read_json(ROOT / "deploy/release/build_profile.json")
        self.assertEqual(len(profile["runtime_libraries"]), 8)
        self.assertNotIn("libcuda.so.1", {x["soname"] for x in profile["runtime_libraries"]})
        self.assertEqual(profile["rust_version"], "1.93.1")
        self.assertEqual(profile["cuda_version"], "13.0.88")
        self.assertIn("@sha256:", profile["runtime_base"])
        closure = profile["kernel_closure"]["gb10/qwen3.6-35b-a3b/nvfp4"]
        self.assertEqual(closure["arch"], "sm_121f")

    def test_real_kernel_closure_requires_complete_compilation(self):
        profile = BUILD.read_json(ROOT / "deploy/release/build_profile.json")
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            output = root / "release/build/avarok-kernels-example/output"
            output.parent.mkdir(parents=True)
            data = "cargo:rustc-env=AVAROK_TARGET_CLOSURES=" + json.dumps(profile["kernel_closure"])
            output.write_text(data)
            with self.assertRaisesRegex(ValueError, "fresh complete native PTX"):
                BUILD.kernel_proof(root, profile)
            output.write_text(data + "\ncargo:warning=avarok-kernels: dedup+parallel: 183/183 unique nvcc invocations (0 cache hits, 1.0× dedup), 4 parallel workers\n")
            self.assertEqual(BUILD.kernel_proof(root, profile)["closure"], profile["kernel_closure"])

    def test_wrong_kernel_closure_fails(self):
        profile = BUILD.read_json(ROOT / "deploy/release/build_profile.json")
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
                BUILD.kernel_proof(pathlib.Path(folder), profile)

    def test_git_identity_rejects_dirty_source_and_short_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "source").write_text("fixed")
            subprocess.run(["git", "-C", str(root), "add", "source"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Build Test", "-c", "user.email=build-test@example.invalid", "commit", "-qm", "fixture"], check=True)
            revision = BUILD.run(["git", "-C", str(root), "rev-parse", "HEAD"])
            BUILD.clean_source(root, revision)
            with self.assertRaisesRegex(ValueError, "full lowercase Git SHA"):
                BUILD.clean_source(root, revision[:8])
            with self.assertRaisesRegex(ValueError, "differs from current HEAD"):
                BUILD.clean_source(root, "0" * 40)
            (root / "source").write_text("changed")
            with self.assertRaisesRegex(ValueError, "clean and committed"):
                BUILD.clean_source(root, revision)

if __name__ == "__main__":
    unittest.main()
