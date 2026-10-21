# SPDX-License-Identifier: AGPL-3.0-only
"""Publish one immutable private Atlas image; never build or deploy a service.

Standard Docker authentication is reused. Diagnostics persist operation names,
exit codes and output hashes, not arbitrary stderr, environment or credentials.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

REPOSITORIES = {"registry.100credit.cn/xdynamic/aarch64/nvidia/atlas", "registry.100credit.cn/xdynamic/aarch64/nvidia/atlas-release"}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
SOURCE = "https://github.com/MonadKai/atlas"


def require(condition, category):
    if not condition:
        raise ValueError(category)


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def validate_target(target):
    require(isinstance(target, str) and ":" in target and "@" not in target, "target_policy")
    repository, tag = target.rsplit(":", 1)
    require(repository in REPOSITORIES and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag), "target_policy")
    require(tag.lower() not in {"latest", "dev", "nightly", "main", "master", "stable", "current", "edge"}, "target_policy")
    return repository, tag


def validate_local_reference(reference):
    require(isinstance(reference, str) and len(reference) <= 1024 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", reference), "local_reference_policy")


def validate_image(info, revision):
    require(REVISION.fullmatch(revision), "source_revision_policy")
    require(isinstance(info, dict) and DIGEST.fullmatch(info.get("Id", "")), "local_image_identity")
    require(info.get("Os") == "linux" and info.get("Architecture") == "arm64", "image_platform")
    require(type(info.get("Size")) is int and info["Size"] > 0, "image_size")
    require(isinstance(info.get("Config"), dict), "local_image_configuration")
    labels = info["Config"].get("Labels") or {}
    require(isinstance(labels, dict), "local_image_configuration")
    require(labels.get("org.opencontainers.image.source") == SOURCE and labels.get("org.opencontainers.image.revision") == revision, "fork_source_identity")
    require(labels.get("org.opencontainers.image.version"), "image_version_identity")
    return {"image_id": info["Id"], "platform": "linux/arm64", "size_bytes": info["Size"], "source_revision": revision, "service_version": labels["org.opencontainers.image.version"]}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_manifest_member")
        result[key] = value
    return result


def manifest_identity(raw, expected_config=None):
    require(isinstance(raw, bytes) and 0 < len(raw) < 16 * 1024 * 1024, "remote_manifest_bytes")
    try:
        manifest = json.loads(raw, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("remote_manifest_json") from error
    require(isinstance(manifest, dict), "remote_manifest_shape")
    require("manifests" not in manifest and manifest.get("mediaType") not in {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}, "unsupported_multi_platform_index")
    require(manifest.get("schemaVersion") == 2 and manifest.get("mediaType") in {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}, "remote_manifest_shape")
    config = manifest.get("config", {})
    require(isinstance(config, dict) and config.get("mediaType") in {"application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json"} and DIGEST.fullmatch(config.get("digest", "")) and type(config.get("size")) is int and config["size"] > 0, "remote_manifest_config")
    require(isinstance(manifest.get("layers"), list), "remote_manifest_layers")
    for layer in manifest["layers"]:
        require(isinstance(layer, dict) and DIGEST.fullmatch(layer.get("digest", "")) and type(layer.get("size")) is int and layer["size"] >= 0, "remote_manifest_layers")
    if expected_config is not None:
        require(config["digest"] == expected_config, "immutable_tag_config_conflict")
    return {"manifest_digest": digest(raw), "config_id": config["digest"], "manifest_size_bytes": len(raw)}


def classify_remote_error(stderr, target):
    """Only a target-bound manifest absence is publishable; all else closes."""
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr)
    lower = text.lower()
    if any(word in lower for word in ("unauthorized", "authentication required", "access denied", "denied:", "forbidden", "insufficient_scope", "no basic auth credentials")):
        return "AUTHORIZATION_FAILED"
    if any(word in lower for word in ("timeout", "deadline exceeded", "connection refused", "no such host", "network is unreachable", "tls handshake", "certificate", "unexpected status", "context", "failed to get endpoint")):
        return "REMOTE_OR_CONTEXT_FAILED"
    # buildx resolver encodes a registry manifest404 as exactly this reference-
    # bound message. A generic/local 'not found' must never authorize a push.
    exact_missing = f"ERROR: {target}: not found"
    if text.strip() == exact_missing:
        return "MANIFEST_ABSENT"
    if target in text and re.search(r"\bmanifest[_ ]unknown\b", lower):
        return "MANIFEST_ABSENT"
    return "REMOTE_UNKNOWN_FAILED"


class Docker:
    def __init__(self, directory):
        self.directory = directory
        self.log = directory / "operations.jsonl"

    def operation(self, name, arguments, allow_absent=False, target=None):
        try:
            completed = subprocess.run(["docker", *arguments], capture_output=True, timeout=3600)
        except (OSError, subprocess.TimeoutExpired) as error:
            with self.log.open("a") as stream:
                stream.write(json.dumps({"operation": name, "status": "PROCESS_FAILED"}) + "\n")
            raise ValueError("docker_process_failed") from error
        category = "PASS" if completed.returncode == 0 else classify_remote_error(completed.stderr, target or "")
        with self.log.open("a") as stream:
            stream.write(json.dumps({"operation": name, "returncode": completed.returncode, "category": category, "stdout_bytes": len(completed.stdout), "stdout_sha256": digest(completed.stdout), "stderr_bytes": len(completed.stderr), "stderr_sha256": digest(completed.stderr)}) + "\n")
        if completed.returncode:
            if allow_absent and category == "MANIFEST_ABSENT":
                return None
            raise ValueError("docker_operation_failed:" + category)
        return completed.stdout

    def inspect_image(self, reference, name):
        raw = self.operation(name, ["image", "inspect", reference])
        try:
            values = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("image_inspect_json") from error
        require(isinstance(values, list) and len(values) == 1, "image_inspect_identity")
        return values[0]

    def remote(self, target, name, allow_absent=False):
        return self.operation(name, ["buildx", "imagetools", "inspect", "--raw", target], allow_absent=allow_absent, target=target)


def handoff(target, local_reference, identity, manifest):
    repository, tag = validate_target(target)
    return {"schema_version": 1, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "transport": "local-daemon", "source": {"requested_ref": local_reference, "resolved_ref": target, "content_digest": identity["image_id"], "manifest_digest": manifest["manifest_digest"]}, "image": {"engine_name": repository.rsplit("/", 1)[1], "engine_version": tag, "size_bytes": identity["size_bytes"]}, "platform": {"type": "aarch64", "oci": "linux/arm64", "provider": "nvidia"}, "archive": None, "target": {"registry": "registry.100credit.cn", "image": target}}


def publish(args):
    repository, _ = validate_target(args.target)
    validate_local_reference(args.local_image)
    require(REVISION.fullmatch(args.source_revision), "source_revision_policy")
    require(not args.output.exists(), "output_must_be_new")
    args.output.mkdir(parents=True)
    docker = Docker(args.output)
    local = validate_image(docker.inspect_image(args.local_image, "inspect-local"), args.source_revision)
    (args.output / "local-identity.json").write_text(json.dumps(local, indent=2) + "\n")
    before = docker.remote(args.target, "remote-tag-preflight", allow_absent=True)
    mode = "already-published-identical"
    if before is not None:
        (args.output / "remote-before.json").write_bytes(before)
        manifest_identity(before, local["image_id"])
    else:
        mode = "new-private-immutable-tag"
        docker.operation("tag-local", ["image", "tag", args.local_image, args.target])
        # Recheck after tagging to narrow races. Registry-enforced immutability
        # and globally unique version tags remain necessary across hosts.
        raced = docker.remote(args.target, "remote-tag-prepush", allow_absent=True)
        if raced is not None:
            (args.output / "remote-prepush.json").write_bytes(raced)
            manifest_identity(raced, local["image_id"])
            mode = "concurrently-published-identical"
        else:
            docker.operation("push-private-tag", ["image", "push", args.target])
    raw = docker.remote(args.target, "remote-tag-readback")
    (args.output / "remote-manifest.json").write_bytes(raw)
    manifest = manifest_identity(raw, local["image_id"])
    immutable = repository + "@" + manifest["manifest_digest"]
    pinned = docker.remote(immutable, "remote-digest-readback")
    (args.output / "remote-digest-readback.json").write_bytes(pinned)
    require(pinned == raw and digest(pinned) == manifest["manifest_digest"], "digest_readback_bytes_mismatch")
    docker.operation("pull-immutable-digest", ["image", "pull", "--platform", "linux/arm64", immutable])
    pulled = validate_image(docker.inspect_image(immutable, "inspect-digest-pull"), args.source_revision)
    require(pulled == local, "digest_pull_identity_mismatch")
    receipt = {"schema_version": "atlas-image-publication-v1", "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "image_ref": immutable, "image_id": local["image_id"], "platform": local["platform"], "size_bytes": local["size_bytes"], "source_revision": args.source_revision, "remote_manifest_digest": manifest["manifest_digest"], "requested_tag": args.target, "readback": "PASS", "digest_pull": "PASS", "mode": mode, "scope": "artifact publication only; no instance deployment or runtime qualification", "tag_policy": "private versioned tag; observed conflicts rejected before push; no registry CAS guarantee"}
    for name, document in (("publication.json", receipt), ("image.handoff.json", handoff(args.target, args.local_image, local, manifest))):
        (args.output / name).write_text(json.dumps(document, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("publish")
    command.add_argument("--local-image", required=True)
    command.add_argument("--target", required=True)
    command.add_argument("--source-revision", required=True)
    command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = publish(args)
    except (ValueError, OSError, TypeError, KeyError) as error:
        # No arbitrary exception text (including Docker stderr) crosses boundary.
        category = str(error) if isinstance(error, ValueError) and re.fullmatch(r"[A-Za-z0-9_:-]+", str(error)) else "publication_input_or_io_failed"
        print(json.dumps({"status": "FAIL", "category": category}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "PASS", "image_ref": result["image_ref"], "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
