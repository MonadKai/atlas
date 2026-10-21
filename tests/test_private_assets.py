# SPDX-License-Identifier: AGPL-3.0-only
"""Asset identity and safetensors closure checks, without GPU or model loading."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import struct

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("atlas_private_assets", ROOT / "deploy/release/assets.py")
ASSETS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ASSETS)

class AssetTests(unittest.TestCase):
    def identity(self, path):
        data = path.read_bytes()
        return {"size_bytes": len(data), "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}

    def fixture(self, root, header=None):
        if header is None:
            header = {"model.weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}, "mtp.fc.weight": {"dtype": "BF16", "shape": [2], "data_offsets": [4, 8]}}
        data = json.dumps(header).encode()
        (root / "model.safetensors").write_bytes(struct.pack("<Q", len(data)) + data + b"\x00" * 8)
        index = {"weight_map": {"model.weight": "model.safetensors", "mtp.fc.weight": "model.safetensors"}}
        (root / "model.safetensors.index.json").write_text(json.dumps(index))
        return {"schema_version": "atlas-model-assets-v1", "files": {p.name: self.identity(p) for p in root.iterdir()}, "safetensors_index": "model.safetensors.index.json", "mtp": {"tensor_count": 1, "tensor_keys": ["mtp.fc.weight"]}, "hf_model": "fixture", "hf_revision": "f" * 40, "download_mirror": "fixture-mirror", "mirror_revision": "master; content pinned by SHA-256", "provenance_unverified": {"source_chain": "NOT_VERIFIED_THIS_ROUND"}}

    def test_reject_lfs_pointer_even_with_matching_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"a" * 64 + b"\nsize 123\n"
            (root / "weights").write_bytes(data)
            expected = {"weights": {"size_bytes": len(data), "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}}
            with self.assertRaisesRegex(ValueError, "LFS pointer"):
                ASSETS.verify_files(root, expected)

    def test_identity_rejects_unsafe_members(self):
        for name in ("", "../weights", "/weights", "sub/../weights", "./weights", "sub//weights", "sub\\weights", "."):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsafe model member"):
                ASSETS.safe_relative(name)

    def test_missing_size_and_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "weights"
            path.write_bytes(b"1234")
            identity = self.identity(path)
            path.write_bytes(b"4321")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                ASSETS.verify_files(root, {path.name: identity})
            path.write_bytes(b"123")
            with self.assertRaisesRegex(ValueError, "size mismatch"):
                ASSETS.verify_files(root, {path.name: identity})
            path.unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                ASSETS.verify_files(root, {path.name: identity})

    def test_directory_and_file_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "weights").write_bytes(b"data")
            (root / "alias").symlink_to("weights")
            with self.assertRaisesRegex(ValueError, "symlink"):
                ASSETS.verify_files(root, {"alias": self.identity(root / "weights")})
            (root / "directory").symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                ASSETS.verify_files(root, {"directory/weights": self.identity(root / "weights")})
            with self.assertRaisesRegex(ValueError, "root symlink"):
                ASSETS.verify_files(root / "directory", {"weights": self.identity(root / "weights")})

    def test_complete_model_and_mtp_header_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.fixture(root)
            (root / "extra-cache").write_bytes(b"allowed undeclared nonrequired cache")
            receipt = ASSETS.verify_model(root, manifest)
            self.assertEqual(receipt["status"], "PASS")
            self.assertEqual(receipt["file_count"], 2)
            self.assertEqual(receipt["safetensors"]["tensor_count"], 2)
            self.assertEqual(receipt["mtp"]["tensor_count"], 1)
            self.assertEqual(receipt["mtp"]["execution"], "NOT_RUN")

    def test_unlisted_shard_reference_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.fixture(root)
            index_path = root / manifest["safetensors_index"]
            index = json.loads(index_path.read_text())
            index["weight_map"]["model.weight"] = "unlisted.safetensors"
            index_path.write_text(json.dumps(index))
            manifest["files"][index_path.name] = self.identity(index_path)
            with self.assertRaisesRegex(ValueError, "unhashed required shard"):
                ASSETS.verify_model(root, manifest)

    def test_missing_mtp_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.fixture(root)
            manifest["mtp"]["tensor_keys"] = ["mtp.other.weight"]
            with self.assertRaisesRegex(ValueError, "MTP index tensor set mismatch"):
                ASSETS.verify_model(root, manifest)

    def test_index_header_key_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.fixture(root)
            index_path = root / manifest["safetensors_index"]
            index = json.loads(index_path.read_text())
            index["weight_map"]["model.other"] = index["weight_map"].pop("model.weight")
            index_path.write_text(json.dumps(index))
            manifest["files"][index_path.name] = self.identity(index_path)
            with self.assertRaisesRegex(ValueError, "index/header tensor set mismatch"):
                ASSETS.verify_model(root, manifest)

    def test_tensor_bad_offsets_and_shapes_rejected(self):
        for value, message in (({"dtype": "F32", "shape": [1], "data_offsets": [0, 9]}, "out of bounds"), ({"dtype": "F32", "shape": [2], "data_offsets": [0, 4]}, "shape/byte size"), ({"dtype": "UNKNOWN", "shape": [1], "data_offsets": [0, 4]}, "dtype")):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.fixture(root, {"bad": value})
                with self.assertRaisesRegex(ValueError, message):
                    ASSETS.read_tensor_header(root / "model.safetensors")

    def test_overlapping_tensor_data_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tensor = {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}
            self.fixture(root, {"one": tensor, "two": tensor})
            with self.assertRaisesRegex(ValueError, "overlapping"):
                ASSETS.read_tensor_header(root / "model.safetensors")

    def test_truncated_or_unreasonable_header_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.safetensors"
            for data in (b"short", struct.pack("<Q", 1024) + b"{}"):
                path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, "header"):
                    ASSETS.read_tensor_header(path)

    def test_duplicate_json_key_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            json.loads('{"tensor":1,"tensor":2}', object_pairs_hook=ASSETS.unique_object)

    def test_product_manifest_preserves_fixed_mirror_and_mtp_identity(self):
        manifest = ASSETS.read_json(ROOT / "deploy/release/models/qwen36-nvfp4.json")
        self.assertEqual(len(manifest["files"]), 16)
        self.assertEqual(manifest["mtp"]["tensor_count"], 19)
        self.assertEqual(manifest["hf_revision"], "1355db6a052410cfd62085d94b58866fd0f2c3c5")
        self.assertEqual(manifest["mirror_revision"], "master; content pinned by SHA-256")
        self.assertNotIn("local_dir", manifest)

if __name__ == "__main__":
    unittest.main()
