# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only identity checks for the explicitly supplied local model files."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import math
import re
import struct
import time

SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
DTYPE_BYTES = {
    "BOOL": 1, "U8": 1, "I8": 1, "F8_E4M3": 1, "F8_E5M2": 1,
    "I16": 2, "U16": 2, "F16": 2, "BF16": 2,
    "I32": 4, "U32": 4, "F32": 4, "I64": 8, "U64": 8, "F64": 8,
}


def safe_relative(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("unsafe model member")
    relative = PurePosixPath(name)
    if str(relative) != name or relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("unsafe model member")
    return relative


def verify_files(root, expected):
    root = Path(root)
    if root.is_symlink():
        raise ValueError("model root symlink is not a delivered directory")
    root = root.resolve(strict=True)
    if not root.is_dir() or not isinstance(expected, dict) or not expected:
        raise ValueError("nonempty model file manifest required")
    checked = []
    for name, identity in sorted(expected.items()):
        relative = safe_relative(name)
        if not isinstance(identity, dict) or type(identity.get("size_bytes")) is not int or identity["size_bytes"] < 0 or not SHA256.fullmatch(identity.get("sha256", "")):
            raise ValueError("invalid model identity: " + name)
        path = root / relative
        if any((root.joinpath(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
            raise ValueError("model symlink is not a delivered file")
        if not path.is_file() or path.stat().st_size != identity["size_bytes"]:
            raise ValueError("model file missing or size mismatch: " + name)
        before = path.stat()
        with path.open("rb") as stream:
            if stream.read(512).startswith(b"version https://git-lfs.github.com/spec/v1"):
                raise ValueError("unresolved Git LFS pointer: " + name)
            stream.seek(0)
            actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != identity["sha256"]:
            raise ValueError("model hash mismatch: " + name)
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("model changed during verification: " + name)
        checked.append({"path": name, "size_bytes": path.stat().st_size, "sha256": actual})
    return checked


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def read_json(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=unique_object)


def read_tensor_header(path):
    """Validate the actual header and byte ranges without loading tensor data."""
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as stream:
        prefix = stream.read(8)
        if len(prefix) != 8:
            raise ValueError("truncated safetensors header length")
        length = struct.unpack("<Q", prefix)[0]
        if length < 2 or length > min(64 * 1024 * 1024, size - 8):
            raise ValueError("invalid safetensors header length")
        header = json.loads(stream.read(length), object_pairs_hook=unique_object)
    if not isinstance(header, dict):
        raise ValueError("safetensors header must be an object")
    tensors = {key: value for key, value in header.items() if key != "__metadata__"}
    if not tensors:
        raise ValueError("empty safetensors shard")
    ranges = []
    for key, value in tensors.items():
        if not isinstance(value, dict) or value.get("dtype") not in DTYPE_BYTES:
            raise ValueError("invalid safetensors tensor dtype: " + key)
        shape, offsets = value.get("shape"), value.get("data_offsets")
        if not isinstance(shape, list) or any(type(x) is not int or x < 0 for x in shape):
            raise ValueError("invalid safetensors tensor shape: " + key)
        if not isinstance(offsets, list) or len(offsets) != 2 or any(type(x) is not int for x in offsets):
            raise ValueError("invalid safetensors tensor offsets: " + key)
        start, end = offsets
        if not 0 <= start <= end <= size - 8 - length:
            raise ValueError("safetensors tensor offsets out of bounds: " + key)
        if end - start != math.prod(shape) * DTYPE_BYTES[value["dtype"]]:
            raise ValueError("safetensors shape/byte size mismatch: " + key)
        ranges.append((start, end, key))
    position = 0
    for start, end, key in sorted(ranges):
        if start != position:
            raise ValueError("safetensors overlapping or missing data range: " + key)
        position = end
    if position != size - 8 - length:
        raise ValueError("safetensors unreferenced trailing data")
    return {
        "header_bytes": length,
        "tensor_count": len(tensors),
        "data_bytes": size - 8 - length,
        "tensors": tensors,
    }


def verify_model(root, manifest):
    """Verify fixed product assets and the complete weight-map/header closure.

    This proves local byte identity only. Cloud/upstream provenance and actual
    MTP execution remain separate claims. Extra non-required local caches are
    permitted; every index-referenced shard must be an explicitly hashed member.
    """
    started = time.monotonic()
    if isinstance(manifest, (str, Path)):
        manifest = read_json(manifest)
    if manifest.get("schema_version") != "atlas-model-assets-v1":
        raise ValueError("unsupported model asset manifest")
    expected = manifest["files"]
    checked = verify_files(root, expected)
    root = Path(root).resolve(strict=True)
    index_path = manifest["safetensors_index"]
    safe_relative(index_path)
    if index_path not in expected:
        raise ValueError("safetensors index is not a hashed required member")
    index = read_json(root / index_path)
    weights = index.get("weight_map")
    if not isinstance(weights, dict) or not weights:
        raise ValueError("nonempty safetensors weight_map required")
    shards = set()
    for key, shard in weights.items():
        if not isinstance(key, str) or not key:
            raise ValueError("invalid weight_map tensor key")
        safe_relative(shard)
        if shard not in expected or not shard.endswith(".safetensors"):
            raise ValueError("weight_map references an unhashed required shard: " + shard)
        shards.add(shard)
    if shards != {name for name in expected if name.endswith(".safetensors")}:
        raise ValueError("unindexed required safetensors shard")
    expected_mtp = manifest["mtp"]["tensor_keys"]
    if not isinstance(expected_mtp, list) or len(set(expected_mtp)) != len(expected_mtp) or len(expected_mtp) != manifest["mtp"]["tensor_count"]:
        raise ValueError("invalid expected MTP tensor set")
    actual_mtp = {key for key in weights if key.startswith("mtp.")}
    if actual_mtp != set(expected_mtp):
        raise ValueError("MTP index tensor set mismatch")
    summaries, mtp_headers = {}, {}
    for shard in sorted(shards):
        header = read_tensor_header(root / shard)
        if set(header["tensors"]) != {key for key, filename in weights.items() if filename == shard}:
            raise ValueError("safetensors index/header tensor set mismatch: " + shard)
        summaries[shard] = {key: value for key, value in header.items() if key != "tensors"}
        for key in actual_mtp:
            if key in header["tensors"]:
                mtp_headers[key] = {"shard": shard, **header["tensors"][key]}
    return {
        "schema_version": "atlas-model-verification-v1",
        "status": "PASS",
        "scope": "local size/SHA-256 and safetensors structural identity; not source authentication or MTP execution",
        "root": str(root),
        "model": {key: manifest[key] for key in ("hf_model", "hf_revision", "download_mirror", "mirror_revision")},
        "manifest_sha256": "sha256:" + hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "file_count": len(checked),
        "total_bytes": sum(item["size_bytes"] for item in checked),
        "files": checked,
        "safetensors": {"status": "PASS", "tensor_count": len(weights), "shards": summaries, "index_header_offsets_consistent": True},
        "mtp": {"status": "PASS", "tensor_count": len(mtp_headers), "tensor_headers": mtp_headers, "execution": "NOT_RUN"},
        "provenance_unverified": manifest["provenance_unverified"],
        "elapsed_seconds": time.monotonic() - started,
    }
