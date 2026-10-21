# SPDX-License-Identifier: AGPL-3.0-only
"""Private publication policy and identity decisions; no lifecycle mocks."""
import importlib.util
from pathlib import Path
import unittest
import copy
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("atlas_private_image", ROOT / "deploy/release/image.py")
IMAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMAGE)

class ImageTests(unittest.TestCase):
    TARGET = "registry.100credit.cn/xdynamic/aarch64/nvidia/atlas:1.0.0-gabcdef01-private-20261022"
    REVISION = "abcdef01" + "a" * 32
    CONFIG = "sha256:" + "b" * 64

    def image(self):
        return {"Id": self.CONFIG, "Architecture": "arm64", "Os": "linux", "Size": 1024, "Config": {"Labels": {"org.opencontainers.image.source": "https://github.com/MonadKai/atlas", "org.opencontainers.image.revision": self.REVISION, "org.opencontainers.image.version": "private-version"}}}

    def manifest(self):
        return {"schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json", "config": {"digest": self.CONFIG, "size": 512, "mediaType": "application/vnd.docker.container.image.v1+json"}, "layers": [{"digest": "sha256:" + "c" * 64, "size": 1024, "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip"}]}

    def test_moving_target_rejected(self):
        with self.assertRaisesRegex(ValueError, "target_policy"):
            IMAGE.validate_target("registry.100credit.cn/xdynamic/aarch64/nvidia/atlas:latest")

    def test_only_explicit_private_versioned_coordinates(self):
        IMAGE.validate_target(self.TARGET)
        IMAGE.validate_target(self.TARGET.replace("/atlas:", "/atlas-release:"))
        for target in (self.TARGET.replace("registry.100credit.cn", "docker.io"), self.TARGET.replace("/atlas:", "/other:"), self.TARGET.split(":")[0], "avarok/atlas-gb10:fixed", self.TARGET.replace("private-20261022", "private bad")):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "target_policy"):
                IMAGE.validate_target(target)
        for tag in ("latest", "dev", "nightly", "main", "stable", "LATEST"):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "target_policy"):
                IMAGE.validate_target(self.TARGET.rsplit(":", 1)[0] + ":" + tag)

    def test_local_reference_cannot_supply_options_or_shell_text(self):
        IMAGE.validate_local_reference(self.CONFIG)
        IMAGE.validate_local_reference("atlas-private:source-abcdef01")
        for reference in ("--format=secret", "ref;echo secret", "ref\nsecret", "ref $(secret)"):
            with self.subTest(reference=reference), self.assertRaisesRegex(ValueError, "local_reference_policy"):
                IMAGE.validate_local_reference(reference)

    def test_source_platform_and_config_are_independent(self):
        identity = IMAGE.validate_image(self.image(), self.REVISION)
        self.assertEqual(identity["image_id"], self.CONFIG)
        self.assertEqual(identity["platform"], "linux/arm64")
        for key, value, category in (("Architecture", "amd64", "image_platform"), ("Os", "windows", "image_platform"), ("Size", 0, "image_size"), ("Id", "not-a-digest", "local_image_identity")):
            data = self.image()
            data[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, category):
                IMAGE.validate_image(data, self.REVISION)

    def test_fork_and_complete_source_revision_required(self):
        for source in ("https://github.com/Avarok-Cybersecurity/atlas", "https://github.com/other/atlas"):
            image = self.image()
            image["Config"]["Labels"]["org.opencontainers.image.source"] = source
            with self.assertRaisesRegex(ValueError, "fork_source_identity"):
                IMAGE.validate_image(image, self.REVISION)
        with self.assertRaisesRegex(ValueError, "source_revision_policy"):
            IMAGE.validate_image(self.image(), self.REVISION[:8])
        with self.assertRaisesRegex(ValueError, "fork_source_identity"):
            IMAGE.validate_image(self.image(), "d" * 40)

    def test_manifest_digest_binds_raw_bytes_not_config_or_normalized_json(self):
        raw = json.dumps(self.manifest(), indent=2).encode()
        identity = IMAGE.manifest_identity(raw, self.CONFIG)
        self.assertEqual(identity["manifest_digest"], "sha256:" + hashlib.sha256(raw).hexdigest())
        self.assertEqual(identity["config_id"], self.CONFIG)
        self.assertNotEqual(identity["manifest_digest"], identity["config_id"])
        self.assertNotEqual(identity["manifest_digest"], IMAGE.manifest_identity(raw + b"\n", self.CONFIG)["manifest_digest"])

    def test_remote_config_conflict_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "immutable_tag_config_conflict"):
            IMAGE.manifest_identity(json.dumps(self.manifest()).encode(), "sha256:" + "e" * 64)

    def test_manifest_index_is_never_mistaken_for_config(self):
        index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []}
        with self.assertRaisesRegex(ValueError, "unsupported_multi_platform_index"):
            IMAGE.manifest_identity(json.dumps(index).encode())

    def test_malformed_remote_manifest_rejected(self):
        for data in (b"not json", b"[]", b'{"schemaVersion":2,"schemaVersion":2}', json.dumps({**self.manifest(), "config": []}).encode()):
            with self.subTest(data=data), self.assertRaises(ValueError):
                IMAGE.manifest_identity(data)

    def test_only_exact_target_manifest_absence_is_retryable(self):
        self.assertEqual(IMAGE.classify_remote_error("ERROR: " + self.TARGET + ": not found\n", self.TARGET), "MANIFEST_ABSENT")
        self.assertEqual(IMAGE.classify_remote_error(self.TARGET + ": manifest unknown", self.TARGET), "MANIFEST_ABSENT")
        for text in ("not found", "ERROR: other/image: not found", "manifest unknown", "context not found", "failed to get endpoint", "network timeout", "unauthorized: " + self.TARGET + ": manifest unknown", "unexpected status 404 Not Found"):
            with self.subTest(text=text):
                self.assertNotEqual(IMAGE.classify_remote_error(text, self.TARGET), "MANIFEST_ABSENT")

    def test_handoff_keeps_config_and_manifest_separate(self):
        identity = IMAGE.validate_image(self.image(), self.REVISION)
        manifest = IMAGE.manifest_identity(json.dumps(self.manifest()).encode(), self.CONFIG)
        handoff = IMAGE.handoff(self.TARGET, "atlas-local:fixed", identity, manifest)
        self.assertEqual(handoff["schema_version"], 1)
        self.assertEqual(handoff["transport"], "local-daemon")
        self.assertIsNone(handoff["archive"])
        self.assertEqual(handoff["source"]["content_digest"], self.CONFIG)
        self.assertEqual(handoff["source"]["manifest_digest"], manifest["manifest_digest"])
        self.assertEqual(handoff["target"]["image"], self.TARGET)

if __name__ == "__main__":
    unittest.main()
