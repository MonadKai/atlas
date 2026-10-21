# SPDX-License-Identifier: AGPL-3.0-only
"""Offline public release boundaries; external lifecycle is exercised for real."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

RELEASE = Path(__file__).resolve().parents[1] / "deploy/release"
sys.path.insert(0, str(RELEASE))
import assets


class ModelIdentityTests(unittest.TestCase):
    def test_corrupt_local_model_is_rejected_before_deployment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.json").write_bytes(b"wrong")
            expected = {"config.json": {"size_bytes": 5, "sha256": "sha256:" + hashlib.sha256(b"right").hexdigest()}}
            with self.assertRaisesRegex(ValueError, "hash"):
                assets.verify_files(root, expected)


class ReleaseContractTests(unittest.TestCase):
    def documents(self):
        import release_contract as contract
        import copy
        cap = json.loads((RELEASE / "profiles/gb10-qwen36-nvfp4.json").read_text())
        model = json.loads((RELEASE / "models/qwen36-nvfp4.json").read_text())
        component = {"id": "engine", "image_ref": "registry.100credit.cn/xdynamic/aarch64/nvidia/atlas:test@sha256:" + "3"*64,
                     "image_id": "sha256:"+"4"*64, "platform": "linux/arm64", "backend": "atlas-native-cu130-sm121f-nvfp4", "size_bytes": 100,
                     "provenance": {"source_revision": "1"*40, "source_repository": "https://github.com/MonadKai/atlas", "evidence_ref": "evidence/image-build.json"},
                     "publication_evidence_ref": "evidence/publication.json", "image_assets": [{"path": "/usr/local/bin/spark", "size_bytes": 5, "sha256": "sha256:"+"5"*64, "license": "AGPL-3.0-only"}]}
        artifact = {k: component[k] for k in ("image_ref", "image_id", "size_bytes")}
        artifact.update(component_ids=["engine"], oci_platform="linux/arm64", profile_id=contract.PROFILE, instance_schema_version="service-instance-v5")
        release = {"schema_version": "product-release-v4", "product_ref": contract.PRODUCT, "source_revision": "1"*40, "tool_revision": "2"*40,
                   "capability_source_revision": "2"*40, "capability_source": "deploy/release/profiles/gb10-qwen36-nvfp4.json", "capability_sha256": contract.canonical(cap),
                   "release_ref": "bounded-test", "service_version": "1.0.0-test", "distribution": {"status": "approved", "approval_ref": "explicit-test-data-only"},
                   "components": [component], "artifacts": [artifact], "assets": [{"id": "qwen36", "acquisition": "authorized-host-mount", "distribution": "not-included", "kind": "directory", "source": "known-local-model", "license": model["license"], "revision": model["hf_revision"], "files": model["files"]}]}
        qualification = {"schema_version": "service-release-qualification-v2", "status": "PASS", "scope": "artifact-release", "allowed_deployment_scope": "development", "production_status": "not_qualified", "source_revision": "1"*40, "profile_id": contract.PROFILE, "capability_sha256": contract.canonical(cap), "components": {"engine": {k: component[k] for k in ("image_ref", "image_id")}}, "evidence": [{"scope": "deterministic-test-data", "ref": "evidence/image-build.json", "sha256": "sha256:"+"6"*64}]}
        return release, cap, qualification

    def test_fixed_single_component_contract_accepts_its_declared_profile(self):
        import release_contract as contract
        release, cap, qualification = self.documents()
        self.assertEqual(contract.validate_release(release, cap, qualification)["profile_id"], "gb10-qwen36-nvfp4-cu130-development")

    def test_platform_source_model_and_child_incompleteness_are_rejected(self):
        import copy
        import release_contract as contract
        edits = [lambda r,c,q: c["profiles"][0].update(architecture="amd64"),
                 lambda r,c,q: r["components"][0]["provenance"].update(source_revision="7"*40),
                 lambda r,c,q: r["components"].clear(),
                 lambda r,c,q: r["assets"][0]["files"].pop("config.json"),
                 lambda r,c,q: r["components"][0].update(image_ref="sha256:"+"4"*64),
                 lambda r,c,q: q.update(production_status="qualified"),
                 lambda r,c,q: c["runtime"]["settings"].update(turboquant=True)]
        for edit in edits:
            with self.subTest(edit=edits.index(edit)):
                r,c,q=self.documents();edit(r,c,q)
                # Rebind summaries to ensure rejection is semantic rather than a stale outer hash.
                r["capability_sha256"]=q["capability_sha256"]=contract.canonical(c)
                with self.assertRaises(ValueError):contract.validate_release(r,c,q)

    def test_duplicate_keys_and_escaped_or_symlinked_members_fail(self):
        import release_contract as contract
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/"duplicate.json").write_text('{"value":1,"value":2}')
            with self.assertRaises(ValueError):contract.load(root/"duplicate.json")
            (root/"file.json").write_text('{}');(root/"link.json").symlink_to(root/"file.json")
            for name in ("../escape.json", "/absolute.json", "link.json", "missing.json"):
                with self.subTest(name=name), self.assertRaises(ValueError):contract.member(root,name)


if __name__ == "__main__":
    unittest.main()
