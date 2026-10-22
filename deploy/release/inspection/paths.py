# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only filesystem observations, pure recovery/cleanup decisions, no mutations."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess


def canonical(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def absolute(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        raise ValueError("explicit canonical absolute path required")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value or value.startswith("//"):
        raise ValueError("explicit canonical absolute path required")
    return path


def overlaps(left, right):
    left, right = absolute(left), absolute(right)
    return left.is_relative_to(right) or right.is_relative_to(left)


class IORouter:
    """Only read/stat/resolve/hash and read-only Docker commands are available."""
    def path(self, name, hash_file):
        path = Path(str(absolute(name)))
        result = {"path": name, "resolved_path": None, "symlinks": [], "exists": False, "error": None}
        try:
            for parent in [*reversed(path.parents), path]:
                if parent.is_symlink():
                    result["symlinks"].append({"path": str(parent), "target": os.readlink(parent)})
            result["resolved_path"] = str(path.resolve(strict=True))
            before = path.stat()
            result.update(exists=True, kind="directory" if stat.S_ISDIR(before.st_mode) else "file" if stat.S_ISREG(before.st_mode) else "other",
                          stat_identity={"device": before.st_dev, "inode": before.st_ino}, size_bytes=before.st_size,
                          mtime_ns=before.st_mtime_ns, ctime_ns=before.st_ctime_ns)
            if hash_file and stat.S_ISREG(before.st_mode):
                descriptor = os.open(result["resolved_path"], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with open(descriptor, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode):
                        raise ValueError("hashed path changed to a nonregular file")
                    result["sha256"] = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
                    finished = os.fstat(stream.fileno())
                after = path.stat()
                identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
                if any(identity(item) != identity(before) for item in (opened, finished, after)) or str(path.resolve(strict=True)) != result["resolved_path"]:
                    result["error"] = "changed_during_hash"
        except (OSError, RuntimeError, ValueError) as error:
            result["error"] = type(error).__name__
        return result

    def docker(self, arguments):
        result = subprocess.run(["docker", *arguments], text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError("Docker read-only observation failed; no absence inferred")
        return result.stdout

    def containers(self):
        identifiers = self.docker(["container", "ls", "--all", "--quiet", "--no-trunc"]).splitlines()
        if not identifiers:
            return []
        if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in identifiers):
            raise ValueError("invalid Docker container identity listing")
        objects = json.loads(self.docker(["container", "inspect", *identifiers]))
        if not isinstance(objects, list) or {item.get("Id") for item in objects} != set(identifiers) or len(objects) != len(identifiers):
            raise ValueError("incomplete Docker observation")
        return objects


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"schema_version", "paths"} or plan["schema_version"] != "atlas-recovery-path-plan-v1" or not isinstance(plan["paths"], list) or not plan["paths"]:
        raise ValueError("explicit nonempty recovery path plan required")
    names = set()
    for item in plan["paths"]:
        if not isinstance(item, dict) or set(item) != {"path", "kind", "resolved_path", "symlink_policy", "stat_identity", "file_identity"}:
            raise ValueError("explicit path identity fields required")
        absolute(item["path"]); absolute(item["resolved_path"])
        if item["path"] in names or item["kind"] not in {"file", "directory"} or item["symlink_policy"] not in {"reject", "allow-fixed"}:
            raise ValueError("duplicate path, invalid kind or symlink policy")
        names.add(item["path"])
        identity = item["stat_identity"]
        if identity is not None and (not isinstance(identity, dict) or set(identity) != {"device", "inode"} or any(type(v) is not int or v < 0 for v in identity.values())):
            raise ValueError("invalid stat identity")
        identity = item["file_identity"]
        if item["kind"] == "file":
            if not isinstance(identity, dict) or set(identity) != {"size_bytes", "sha256"} or type(identity["size_bytes"]) is not int or identity["size_bytes"] < 0 or not isinstance(identity["sha256"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", identity["sha256"]):
                raise ValueError("fixed size/SHA-256 required for each file")
        elif identity is not None:
            raise ValueError("directory file_identity must explicitly be null")


def observe_plan(plan, router):
    validate_plan(plan)
    return {"schema_version": "atlas-recovery-path-observation-v1", "plan_sha256": canonical(plan),
            "paths": [router.path(item["path"], item["kind"] == "file") for item in plan["paths"]]}


def assess_assets(plan, observation):
    validate_plan(plan)
    if observation.get("schema_version") != "atlas-recovery-path-observation-v1" or observation.get("plan_sha256") != canonical(plan):
        raise ValueError("observation does not bind exact recovery plan")
    observed = observation.get("paths")
    if not isinstance(observed, list) or len(observed) != len(plan["paths"]) or {p.get("path") for p in observed} != {p["path"] for p in plan["paths"]}:
        raise ValueError("incomplete or duplicate path observation")
    by_path = {p["path"]: p for p in observed}
    results = []
    for expected in plan["paths"]:
        actual, reasons = by_path[expected["path"]], []
        for key in ("kind", "resolved_path"):
            if actual.get(key) != expected[key]: reasons.append(key + "_mismatch")
        if actual.get("exists") is not True: reasons.append("missing")
        if actual.get("error"): reasons.append(actual["error"])
        if expected["symlink_policy"] == "reject" and actual.get("symlinks"): reasons.append("symlink_in_path")
        if expected["stat_identity"] is not None and actual.get("stat_identity") != expected["stat_identity"]: reasons.append("stat_identity_mismatch")
        if expected["kind"] == "file":
            for key in ("size_bytes", "sha256"):
                if actual.get(key) != expected["file_identity"][key]: reasons.append(key + "_mismatch")
        results.append({"path": expected["path"], "status": "FAIL" if reasons else "PASS", "reasons": reasons})
    return {"schema_version": "atlas-recovery-path-verdict-v1", "status": "FAIL" if any(p["reasons"] for p in results) else "PASS",
            "plan_sha256": canonical(plan), "paths": results, "scope": "explicit required local path/byte identity at observation; not source authentication or future availability"}


def observe_references(references, router):
    if not isinstance(references, list) or not references:
        raise ValueError("explicit nonempty path references required")
    names, observed = set(), []
    for item in references:
        if not isinstance(item, dict) or set(item) != {"path", "reason"} or not isinstance(item["reason"], str) or not item["reason"]:
            raise ValueError("explicit path and reference reason required")
        absolute(item["path"])
        if item["path"] in names: raise ValueError("duplicate reference path")
        names.add(item["path"])
        observed.append({**router.path(item["path"], False), "reason": item["reason"]})
    return observed


def container_references(containers):
    """Include running AND stopped containers: either may be a recovery dependency."""
    references = {}
    for container in containers:
        identifier = container.get("Id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier): raise ValueError("full container identity required")
        for mount in container.get("Mounts", []):
            if mount.get("Type") not in {"bind", "volume"}: continue
            source = mount.get("Source"); absolute(source)
            reason = "container:" + identifier + ":" + str(container.get("State", {}).get("Status")) + ":" + str(mount.get("Destination"))
            references.setdefault(source, []).append(reason)
    return [{"path": path, "reason": ";".join(reasons)} for path, reasons in sorted(references.items())]


def assess_cleanup(candidates, protected):
    if not candidates or not protected: raise ValueError("complete candidate/protected observations required")
    conflicts, uncertain = [], []
    for item in candidates + protected:
        absolute(item["path"])
        if item.get("error") or item.get("resolved_path") is None:
            uncertain.append({"path": item["path"], "reason": item.get("error") or "unresolved"})
        else: absolute(item["resolved_path"])
    for candidate in candidates:
        for reference in protected:
            modes = []
            for mode, left, right in (("lexical", candidate["path"], reference["path"]), ("resolved", candidate.get("resolved_path"), reference.get("resolved_path"))):
                if left is not None and right is not None and overlaps(left, right): modes.append(mode)
            if modes: conflicts.append({"candidate": candidate["path"], "reference": reference["path"], "reason": reference["reason"], "overlap_by": modes})
    return {"schema_version": "atlas-cleanup-reference-verdict-v1", "status": "REFUSED" if conflicts or uncertain else "NO_REFERENCES_OBSERVED",
            "references": conflicts, "uncertain_paths": uncertain,
            "scope": "read-only reference snapshot; absence of observed references is not deletion authorization; reobserve immediately before any separately authorized cleanup"}


def assess_mounts(expected, actual):
    if set(expected) != {"container_id", "mounts"} or set(actual) != {"container_id", "mounts"}:
        raise ValueError("explicit container and mount identities required")
    reasons = []
    for snapshot in (expected, actual):
        if not re.fullmatch(r"[0-9a-f]{64}", snapshot["container_id"]): raise ValueError("full container identity required")
        destinations = []
        for mount in snapshot["mounts"]:
            if set(mount) != {"type", "source", "destination", "read_only"} or mount["type"] not in {"bind", "volume", "tmpfs"} or type(mount["read_only"]) is not bool:
                raise ValueError("explicit source/destination/type/access required")
            if mount["type"] == "tmpfs":
                if mount["source"] is not None: raise ValueError("tmpfs source must explicitly be null")
            else:
                absolute(mount["source"])
            absolute(mount["destination"])
            destinations.append(mount["destination"])
        if len(set(destinations)) != len(destinations): reasons.append("duplicate_destination")
        for index, path in enumerate(destinations):
            if any(overlaps(path, other) for other in destinations[index + 1:]): reasons.append("nested_mount_masks_required_path")
    if expected["container_id"] != actual["container_id"]: reasons.append("container_identity_mismatch")
    if sorted(expected["mounts"], key=canonical) != sorted(actual["mounts"], key=canonical): reasons.append("mount_identity_mismatch")
    return {"schema_version": "atlas-recovery-mount-verdict-v1", "status": "FAIL" if reasons else "PASS", "reasons": sorted(set(reasons))}
