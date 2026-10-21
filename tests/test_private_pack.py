# SPDX-License-Identifier: AGPL-3.0-only
"""Temporary committed Git fixtures exercise pack identity/closure, never lifecycle."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "deploy/release"))
import pack
from release_contract import validate_bundle


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class PrivatePackTests(unittest.TestCase):
    def git(self, *arguments):
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo), *arguments], stderr=subprocess.PIPE).decode().strip()

    def commit(self):
        self.git("add", "-A")
        self.git("-c", "user.name=Atlas Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture only")
        return self.git("rev-parse", "HEAD")

    def document(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, indent=2)+"\n")
        return path

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("remote", "add", "origin", "https://github.com/MonadKai/atlas.git")
        for name in pack.SOURCE_FILES.values():
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            original = SOURCE / name
            target.write_bytes(original.read_bytes() if original.exists() else b"# explicitly synthetic committed fixture\n")
        self.profile = json.loads((SOURCE / "deploy/release/build_profile.json").read_text())
        for name in self.profile["source_inputs"]:
            target = self.repo/name
            if not target.exists():
                if (SOURCE/name).is_dir():
                    target.mkdir(parents=True)
                    (target/"fixture.txt").write_text("synthetic Git tree; no compilation performed\n")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("synthetic committed source input\n")
        (self.repo / "deploy/release/build_profile.json").write_text(json.dumps(self.profile))
        (self.repo / "Cargo.lock").write_text("# explicitly synthetic Cargo identity\n")
        self.source_revision = self.commit()
        (self.repo / "deploy/release/README.md").write_text("# Committed tool revision fixture\n")
        self.tool_revision = self.commit()
        lock = {"schema_version": "atlas-private-build-input-lock-v1", "source_revision": self.source_revision,
            "source_tree": self.git("rev-parse", self.source_revision+"^{tree}"), "profile": self.profile,
            "source_inputs": {name: self.git("rev-parse", self.source_revision+":"+name) for name in self.profile["source_inputs"]},
            "cargo_lock": {"size_bytes": len((self.repo/"Cargo.lock").read_bytes()), "sha256": hashlib.sha256((self.repo/"Cargo.lock").read_bytes()).hexdigest()},
            "profile_identity": {"size_bytes": len((self.repo/"deploy/release/build_profile.json").read_bytes()),
                "sha256": hashlib.sha256((self.repo/"deploy/release/build_profile.json").read_bytes()).hexdigest()},
            "environment": {"runtime_libraries": self.profile["runtime_libraries"],
                "tools": {name: {"path": "/fixture/"+name, "size_bytes": item["size_bytes"], "sha256": item["sha256"]}
                    for name,item in self.profile["tools"].items()}}}
        self.lock = self.document("input-lock.json", lock)
        self.cargo_log = self.root/"cargo-build.txt"
        self.cargo_log.write_text(json.dumps({"reason": "compiler-artifact", "target": {"name": "spark"}, "executable": "/fixture/spark"})+"\n"+
            '{"reason":"build-finished","success":true}\n')
        self.kernel_log = self.root/"kernel-build.txt"
        self.kernel_log.write_text("183/183 unique nvcc invocations (0 cache hits)\n"+
            "cargo:rustc-env=AVAROK_TARGET_CLOSURES="+json.dumps(self.profile["kernel_closure"])+"\n")
        compile_receipt = {"schema_version": "atlas-private-build-receipt-v1", "source_revision": self.source_revision,
            "tool_revision": self.source_revision, "input_lock": {"path": str(self.lock), "size_bytes": self.lock.stat().st_size,
                "sha256": hashlib.sha256(self.lock.read_bytes()).hexdigest()},
            "binary": {"path": "/fixture/spark", "size_bytes": 10, "sha256": "b" * 64},
            "cargo_command": ["/fixture/cargo", "build", "--release", "--locked", "--offline", "-p", "spark-server", "--bin", "spark", "--message-format=json-render-diagnostics"],
            "compile_environment": {"AVAROK_TARGET_HW": "gb10", "AVAROK_TARGET_MODEL": "qwen3.6-35b-a3b", "AVAROK_TARGET_QUANT": "nvfp4", "RUSTUP_TOOLCHAIN": self.profile["rust_version"]},
            "compile_log": {"path": str(self.cargo_log), "size_bytes": self.cargo_log.stat().st_size, "sha256": hashlib.sha256(self.cargo_log.read_bytes()).hexdigest()},
            "kernel_proof": {"closure": self.profile["kernel_closure"], "file": str(self.kernel_log),
                "size_bytes": self.kernel_log.stat().st_size, "sha256": hashlib.sha256(self.kernel_log.read_bytes()).hexdigest()},
            "qualification": {"source_build": "PASS", "production": "not_qualified", "upstream_matrix": "NOT_RUN"}}
        self.compile = self.document("compile.json", compile_receipt)
        self.image_id = "sha256:" + "5" * 64
        self.image = self.document("image.json", {"schema_version": "atlas-image-build-receipt-v1", "status": "PASS",
            "source_revision": self.source_revision, "image_id": self.image_id, "platform": "linux/arm64", "size_bytes": 4096,
            "binary_sha256": "sha256:"+"b"*64, "compile_receipt_sha256": digest(self.compile.read_bytes()),
            "labels": {"org.opencontainers.image.source": "https://github.com/MonadKai/atlas", "org.opencontainers.image.revision": self.source_revision},
            "image_assets": [{"path": "/usr/local/bin/spark", "size_bytes": 10, "sha256": "sha256:"+"b"*64, "license": "AGPL-3.0-only"}] +
                [{"path": "/opt/atlas/lib/"+item["soname"], "size_bytes": item["size_bytes"], "sha256": "sha256:"+item["sha256"], "license": "synthetic fixture metadata only"}
                    for item in self.profile["runtime_libraries"]]})
        self.manifest = self.document("remote-manifest.json", {"schemaVersion": 2, "config": {"digest": self.image_id}, "layers": []})
        manifest_digest = digest(self.manifest.read_bytes())
        self.publication = self.document("publication.json", {"schema_version": "atlas-image-publication-v1", "readback": "PASS", "digest_pull": "PASS",
            "source_revision": self.source_revision, "image_ref": "registry.100credit.cn/xdynamic/atlas@"+manifest_digest,
            "image_id": self.image_id, "platform": "linux/arm64", "size_bytes": 4096, "remote_manifest_digest": manifest_digest,
            "requested_tag": "registry.100credit.cn/xdynamic/atlas:fixture-immutable"})
        self.probe = self.document("probe.json", {"scope": "single-model-development-runtime", "status": "PASS",
            "source_revision": self.source_revision, "image_id": self.image_id})
        self.args = argparse.Namespace(source=self.repo, source_revision=self.source_revision, tool_revision=self.tool_revision,
            release_ref="release:atlas:fixture-only", service_version="fixture-only", image_record=self.image, publication_record=self.publication,
            compile_receipt=self.compile, input_lock=self.lock, probe_record=self.probe, output=self.root/"bundle")

    def test_committed_tool_closure_and_distinct_binary_source_are_preserved(self):
        result = pack.pack(self.args)
        release, capability, _ = validate_bundle(self.args.output)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(release["source_revision"], self.source_revision)
        self.assertEqual(release["tool_revision"], self.tool_revision)
        self.assertNotEqual(self.source_revision, self.tool_revision)
        for target, original in pack.SOURCE_FILES.items():
            self.assertEqual((self.args.output/target).read_bytes(), subprocess.check_output(["git", "-C", str(self.repo), "show", self.tool_revision+":"+original]))
            self.assertEqual(release["tools"][target]["source_revision"], self.tool_revision)
        self.assertEqual((self.args.output/"evidence/source-build.json").read_bytes(), self.compile.read_bytes())
        self.assertEqual((self.args.output/"evidence/remote-manifest.json").read_bytes(), self.manifest.read_bytes())
        self.assertEqual((self.args.output/"evidence/cargo-build.txt").read_bytes(), self.cargo_log.read_bytes())
        self.assertEqual((self.args.output/"evidence/kernel-build.txt").read_bytes(), self.kernel_log.read_bytes())
        self.assertEqual(len(release["assets"][0]["files"]), 16)
        self.assertEqual(release["assets"][0]["distribution"], "not-included")
        self.assertEqual(capability["runtime"]["instance_schema_version"], "service-instance-v5")
        self.assertFalse(list(self.args.output.rglob("*.safetensors")))

    def test_published_render_keeps_real_path_gate_without_source_checkout(self):
        import instance
        pack.pack(self.args)
        model, cache = self.root/"model", self.root/"cache"
        model.mkdir();cache.mkdir()
        value = {"schema_version": "service-instance-v5", "release_scope": "development", "project_name": "published-test",
            "container_name": "published-test", "host_ip": "127.0.0.1", "host_port": 18888, "manager": "test",
            "binding_id": "published-test", "components": {"engine": {"cpuset": "0-3", "cpu_limit_millicores": 4000, "memory_limit_bytes": 68719476736}},
            "uid": os.getuid(), "gid": os.getgid(), "model_root": str(model), "cache_root": str(cache), "model_alias": "qwen3-6-35b-a3b"}
        self.assertEqual(instance.render(self.args.output, value)["services"]["engine"]["user"], f"{os.getuid()}:{os.getgid()}")
        cache.chmod(0o500)
        with self.assertRaisesRegex(ValueError, "cache must be writable"):
            instance.render(self.args.output, value)
        cache.chmod(0o700)
        model.rmdir()
        with self.assertRaisesRegex(ValueError, "existing real directory"):
            instance.render(self.args.output, value)

    def test_dirty_head_wrong_tool_revision_and_foreign_origin_reject(self):
        original = (self.repo/"deploy/release/README.md").read_bytes()
        (self.repo/"deploy/release/README.md").write_text("uncommitted")
        with self.assertRaisesRegex(ValueError, "clean"):
            pack.pack(self.args)
        self.assertTrue(self.args.output.is_dir())
        (self.repo/"deploy/release/README.md").write_bytes(original)
        self.args.output = self.root/"wrong-head"
        self.args.tool_revision = self.source_revision
        with self.assertRaisesRegex(ValueError, "HEAD"):
            pack.pack(self.args)
        self.args.tool_revision = self.tool_revision
        self.args.output = self.root/"foreign-origin"
        self.git("remote", "set-url", "origin", "https://github.com/Avarok-Cybersecurity/atlas.git")
        with self.assertRaisesRegex(ValueError, "fork"):
            pack.pack(self.args)

    def test_missing_committed_member_symlink_and_lfs_pointer_reject(self):
        for index, failure in enumerate(["missing", "symlink", "lfs"]):
            name = self.repo/"deploy/release/README.md"
            if name.exists() or name.is_symlink():
                name.unlink()
            if failure == "symlink":
                name.symlink_to("../../LICENSE")
            elif failure == "lfs":
                name.write_text("version https://git-lfs.github.com/spec/v1\noid sha256:"+"a"*64+"\nsize 17\n")
            self.args.tool_revision = self.commit()
            self.args.output = self.root/f"failed-member-{index}"
            with self.subTest(failure=failure), self.assertRaises((ValueError, subprocess.CalledProcessError)):
                pack.pack(self.args)
            self.assertTrue(self.args.output.is_dir())

    def test_receipt_source_input_binary_and_remote_identity_mismatches_reject(self):
        cases = [(self.image, "source_revision", "f"*40), (self.image, "binary_sha256", "sha256:"+"f"*64),
                 (self.publication, "image_id", "sha256:"+"f"*64), (self.publication, "digest_pull", "NOT_RUN"),
                 (self.probe, "scope", "HTTP-ready-only")]
        for index, (path, key, wrong) in enumerate(cases):
            raw = path.read_bytes()
            document = json.loads(raw)
            document[key] = wrong
            path.write_text(json.dumps(document))
            self.args.output = self.root/f"mismatch-{index}"
            with self.subTest(key=key), self.assertRaises(ValueError):
                pack.pack(self.args)
            path.write_bytes(raw)
        self.args.output = self.root/"wrong-manifest"
        self.manifest.write_text("{}")
        with self.assertRaisesRegex(ValueError, "manifest"):
            pack.pack(self.args)

    def test_locked_source_objects_compile_log_and_manifest_config_are_bound(self):
        lock_raw = self.lock.read_bytes()
        lock = json.loads(lock_raw)
        lock["source_inputs"]["Cargo.lock"] = "f"*40
        self.lock.write_text(json.dumps(lock))
        with self.assertRaisesRegex(ValueError, "source input"):
            pack.pack(self.args)
        self.lock.write_bytes(lock_raw)
        self.args.output = self.root/"changed-log"
        original = self.cargo_log.read_bytes()
        self.cargo_log.write_text("changed build evidence")
        with self.assertRaisesRegex(ValueError, "artifact size/hash"):
            pack.pack(self.args)
        self.cargo_log.write_bytes(original)
        self.args.output = self.root/"changed-manifest-config"
        manifest = json.loads(self.manifest.read_bytes())
        manifest["config"]["digest"] = "sha256:"+"f"*64
        self.manifest.write_text(json.dumps(manifest))
        publication = json.loads(self.publication.read_bytes())
        remote = digest(self.manifest.read_bytes())
        publication.update(remote_manifest_digest=remote, image_ref="registry.100credit.cn/xdynamic/atlas@"+remote)
        self.publication.write_text(json.dumps(publication))
        with self.assertRaisesRegex(ValueError, "manifest/config"):
            pack.pack(self.args)

    def test_native_inventory_cannot_depart_from_locked_build_inputs(self):
        image = json.loads(self.image.read_text())
        image["image_assets"][1]["sha256"] = "sha256:"+"f"*64
        self.image.write_text(json.dumps(image))
        with self.assertRaisesRegex(ValueError, "native"):
            pack.pack(self.args)

    def rebind_consumer(self):
        """Recompute outer hashes so negative cases reach semantic cross-bindings."""
        root = self.args.output
        release = json.loads((root/"release.json").read_bytes())
        compiled_path = root/"evidence/source-build.json"
        compiled = json.loads(compiled_path.read_bytes())
        for field, file in [("input_lock", "build-input-lock.json"), ("compile_log", "cargo-build.txt"), ("kernel_proof", "kernel-build.txt")]:
            raw = (root/"evidence"/file).read_bytes()
            compiled[field].update(size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        compiled_path.write_text(json.dumps(compiled))
        build_path = root/"evidence/image-build.json"
        build = json.loads(build_path.read_bytes())
        build["compile_receipt_sha256"] = digest(compiled_path.read_bytes())
        build_path.write_text(json.dumps(build))
        qualification_path = root/"qualification.json"
        qualification = json.loads(qualification_path.read_bytes())
        for evidence in qualification["evidence"]:
            raw = (root/evidence["ref"]).read_bytes()
            evidence.update(size_bytes=len(raw), sha256=digest(raw))
        qualification_path.write_text(json.dumps(qualification))
        manifest_path = root/"bundle-manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest.update({name: digest((root/name).read_bytes()) for name in manifest})
        manifest_path.write_text(json.dumps(manifest))
        release["bundle_manifest_sha256"] = digest(manifest_path.read_bytes())
        (root/"release.json").write_text(json.dumps(release))

    def test_consumer_rejects_rehashed_cross_receipt_source_and_native_changes(self):
        pack.pack(self.args)
        root = self.args.output
        originals = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        for kind in ["lock-source", "native", "native-environment", "kernel-log", "cargo-log", "remote-manifest", "digest-pull"]:
            for name, raw in originals.items():
                (root/name).write_bytes(raw)
            if kind == "lock-source":
                path = root/"evidence/build-input-lock.json"
                value = json.loads(path.read_bytes());value["source_revision"] = "f"*40;path.write_text(json.dumps(value))
            elif kind in {"native", "native-environment"}:
                path = root/("release.json" if kind == "native" else "evidence/build-input-lock.json")
                value = json.loads(path.read_bytes())
                if kind == "native":
                    value["components"][0]["image_assets"][1]["sha256"] = "sha256:"+"f"*64
                    build_path = root/"evidence/image-build.json";build=json.loads(build_path.read_bytes());build["image_assets"]=value["components"][0]["image_assets"];build_path.write_text(json.dumps(build))
                else:
                    value["environment"]["runtime_libraries"][0]["sha256"] = "f"*64
                path.write_text(json.dumps(value))
            elif kind == "kernel-log":
                path = root/"evidence/kernel-build.txt"
                path.write_text("183/183 unique nvcc invocations (0 cache hits)\ncargo:rustc-env=AVAROK_TARGET_CLOSURES={}\n")
            elif kind == "cargo-log":
                path = root/"evidence/cargo-build.txt"
                path.write_text('{"reason":"build-finished","success":false}\n')
            elif kind == "remote-manifest":
                path = root/"evidence/remote-manifest.json"
                value=json.loads(path.read_bytes());value["config"]["digest"]="sha256:"+"f"*64;path.write_text(json.dumps(value))
            else:
                path = root/"evidence/publication.json"
                value=json.loads(path.read_bytes());value["digest_pull"]="NOT_RUN";path.write_text(json.dumps(value))
            self.rebind_consumer()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validate_bundle(root)

    def test_existing_output_and_inside_source_output_are_not_overwritten(self):
        self.args.output.mkdir()
        marker = self.args.output/"previous-failure"
        marker.write_text("preserve")
        with self.assertRaises(ValueError):
            pack.pack(self.args)
        self.assertEqual(marker.read_text(), "preserve")
        self.args.output = self.repo/"bundle"
        with self.assertRaises(ValueError):
            pack.pack(self.args)
        self.assertFalse(self.args.output.exists())


if __name__ == "__main__":
    unittest.main()
