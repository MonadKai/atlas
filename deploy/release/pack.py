# SPDX-License-Identifier: AGPL-3.0-only
"""Export fixed Git tools and supplied evidence as a closed private release bundle.

Read-only Git/receipt operations plus writes to one new output directory. This
command never builds, tags, pushes, contacts a registry or controls an instance.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys

from release_contract import COMMIT, DIGEST, IMAGE, PRODUCT, PROFILE, SOURCE_REPOSITORY, RELIABILITY_MEMBERS
from release_contract import canonical, require, sha, unique_object, validate_bundle

MODULES = ["release_contract", "toolkit", "verify", "instance", "assets", "runtime_identity",
           "acceptance", "acceptance_cases", "acceptance_http", "acceptance_suites", "acceptance_profile",
           "observe_shutdown", "build", "image", "pack"]
SOURCE_FILES = {name+".py": "deploy/release/"+name+".py" for name in MODULES}
SOURCE_FILES.update({
    "build_profile.json": "deploy/release/build_profile.json",
    "fixtures/cases.json": "deploy/release/fixtures/cases.json",
    "models/qwen36-nvfp4.json": "deploy/release/models/qwen36-nvfp4.json",
    "product-capability.json": "deploy/release/profiles/gb10-qwen36-nvfp4.json",
    "README.md": "deploy/release/README.md", "LICENSE": "LICENSE",
    "skills/atlas-private-deploy/SKILL.md": ".claude/skills/atlas-private-deploy/SKILL.md",
    "skills/avarok-release/references/private-gb10.md": ".claude/skills/avarok-release/references/private-gb10.md",
})

SOURCE_FILES.update({name: "deploy/release/" + name for name in sorted(RELIABILITY_MEMBERS)})

def git(source, *args):
    return subprocess.check_output(["git", "-C", str(source), *args], stderr=subprocess.PIPE)


def source_state(source, source_revision, tool_revision):
    require(COMMIT.fullmatch(source_revision) and COMMIT.fullmatch(tool_revision), "full binary source and tool revisions required")
    require(git(source, "rev-parse", "HEAD").decode().strip() == tool_revision, "tool revision must equal source HEAD")
    require(not git(source, "status", "--porcelain", "--untracked-files=all").strip(), "source tools must be clean and committed")
    ancestor = subprocess.run(["git", "-C", str(source), "merge-base", "--is-ancestor", source_revision, tool_revision], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    require(ancestor.returncode == 0, "binary source must be an existing ancestor of the tool revision")
    origin = git(source, "remote", "get-url", "origin").decode().strip().removesuffix(".git").lower()
    require(origin in {"https://github.com/monadkai/atlas", "git@github.com:monadkai/atlas", "ssh://git@github.com/monadkai/atlas"}, "publisher must use the authorized MonadKai fork origin")


def blob(source, revision, name):
    rel = PurePosixPath(name)
    require(not rel.is_absolute() and ".." not in rel.parts and str(rel) == name, "invalid committed source path")
    entry = git(source, "ls-tree", "-z", revision, "--", name).split(b"\0")
    require(len(entry) == 2 and not entry[1], "required committed source member missing: " + name)
    header, actual = entry[0].split(b"\t", 1)
    mode, kind, oid = header.decode().split()
    require(actual.decode() == name and kind == "blob" and mode in {"100644", "100755"}, "source member must be a regular Git blob: " + name)
    data = git(source, "cat-file", "blob", oid)
    require(not data.startswith(b"version https://git-lfs.github.com/spec/v1\n"), "unmaterialized Git LFS pointer: " + name)
    return data, mode


def identity(data):
    return {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def document(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "regular receipt input required")
    raw = path.read_bytes()
    value = json.loads(raw, object_pairs_hook=unique_object)
    require(isinstance(value, dict), "receipt must be a JSON object")
    return raw, value


def bound_artifact(specification, path_field):
    require(isinstance(specification, dict) and isinstance(specification.get(path_field), str), "bound compile artifact path required")
    path = Path(specification[path_field])
    require(path.is_absolute() and path.is_file() and not path.is_symlink(), "bound compile artifact must be a regular absolute file")
    raw = path.read_bytes()
    require(all(specification.get(key) == value for key, value in identity(raw).items()), "bound compile artifact size/hash mismatch")
    return raw, None


def validate_inputs(source, revision, records):
    compile_raw, compiled = records["evidence/source-build.json"]
    lock_raw, lock = records["evidence/build-input-lock.json"]
    _, image = records["evidence/image-build.json"]
    _, publication = records["evidence/publication.json"]
    _, probe = records["evidence/local-runtime-probe.json"]
    manifest_raw, manifest = records["evidence/remote-manifest.json"]
    require(lock.get("schema_version") == "atlas-private-build-input-lock-v1" and lock.get("source_revision") == revision, "build input lock source mismatch")
    require(lock.get("source_tree") == git(source, "rev-parse", revision+"^{tree}").decode().strip(), "source tree/input lock mismatch")
    profile_raw, _ = blob(source, revision, "deploy/release/build_profile.json")
    profile = json.loads(profile_raw, object_pairs_hook=unique_object)
    require(lock.get("profile") == profile and lock.get("profile_identity") == identity(profile_raw), "committed build profile/input lock mismatch")
    paths = profile.get("source_inputs")
    require(isinstance(paths, list) and paths and len(paths) == len(set(paths)) and set(lock.get("source_inputs", {})) == set(paths), "complete locked source inputs required")
    for name, oid in lock["source_inputs"].items():
        require(git(source, "rev-parse", revision+":"+name).decode().strip() == oid, "locked source input identity mismatch")
    require(lock.get("cargo_lock") == identity(blob(source, revision, "Cargo.lock")[0]), "Cargo.lock identity mismatch")
    require(compiled.get("schema_version") == "atlas-private-build-receipt-v1" and compiled.get("source_revision") == revision
            and compiled.get("tool_revision") == revision and compiled.get("qualification", {}).get("source_build") == "PASS", "fixed source compile receipt required")
    require(all(compiled.get("input_lock", {}).get(k) == v for k, v in identity(lock_raw).items()), "compile/input-lock byte identity mismatch")
    require(compiled.get("kernel_proof", {}).get("closure") == profile.get("kernel_closure") and profile.get("kernel_closure"), "compiled native kernel closure mismatch")
    binary = compiled.get("binary", {})
    require(type(binary.get("size_bytes")) is int and binary["size_bytes"] > 0 and DIGEST.fullmatch("sha256:"+binary.get("sha256", "")), "compiled binary identity required")
    require(image.get("schema_version") == "atlas-image-build-receipt-v1" and image.get("status") == "PASS" and image.get("source_revision") == revision
            and DIGEST.fullmatch(image.get("image_id", "")) and image.get("platform") == "linux/arm64"
            and type(image.get("size_bytes")) is int and image["size_bytes"] > 0, "image build source/config/platform mismatch")
    require(image.get("compile_receipt_sha256") == "sha256:"+identity(compile_raw)["sha256"] and image.get("binary_sha256") == "sha256:"+binary["sha256"], "compiled binary/image receipt mismatch")
    require(image.get("labels", {}).get("org.opencontainers.image.source") == SOURCE_REPOSITORY
            and image["labels"].get("org.opencontainers.image.revision") == revision, "image fork/source labels mismatch")
    assets = image.get("image_assets")
    require(isinstance(assets, list) and assets and all(isinstance(a, dict) and isinstance(a.get("path"), str) for a in assets), "native/code image inventory required")
    inventory = {a["path"]: a for a in assets}
    require(len(inventory) == len(assets), "duplicate image asset path")
    required = {"/usr/local/bin/spark": {"size_bytes": binary["size_bytes"], "sha256": "sha256:"+binary["sha256"]}}
    for item in profile["runtime_libraries"]:
        required["/opt/atlas/lib/"+item["soname"]] = {"size_bytes": item["size_bytes"], "sha256": "sha256:"+item["sha256"]}
    require(all(name in inventory and all(inventory[name].get(k) == value for k, value in expected.items()) for name, expected in required.items()), "image native/binary inventory differs from locked build inputs")
    require(publication.get("schema_version") == "atlas-image-publication-v1" and publication.get("source_revision") == revision
            and publication.get("readback") == "PASS" and publication.get("digest_pull") == "PASS"
            and IMAGE.fullmatch(publication.get("image_ref", "")) and all(publication.get(k) == image[k] for k in ("image_id", "platform", "size_bytes")), "publication manifest/config/platform receipt mismatch")
    remote_digest = "sha256:"+identity(manifest_raw)["sha256"]
    require(publication.get("remote_manifest_digest") == remote_digest and publication["image_ref"].rsplit("@", 1)[1] == remote_digest
            and manifest.get("schemaVersion") == 2 and manifest.get("config", {}).get("digest") == image["image_id"], "remote raw manifest/config identity mismatch")
    require(probe.get("status") == "PASS" and probe.get("scope") == "single-model-development-runtime"
            and probe.get("source_revision") == revision and probe.get("image_id") == image["image_id"], "matching actual local runtime probe required")
    return image, publication


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)+"\n")


def pack(args):
    source = args.source.resolve()
    output = args.output.resolve()
    require(not output.exists() and not args.output.is_symlink(), "bundle output must be a new directory; failed attempts are retained")
    require(not output.is_relative_to(source), "bundle output must be outside source checkout")
    output.mkdir(parents=True, exist_ok=False)
    source_state(source, args.source_revision, args.tool_revision)
    require(isinstance(args.release_ref, str) and args.release_ref.strip() == args.release_ref and args.release_ref
            and isinstance(args.service_version, str) and args.service_version.strip() == args.service_version and args.service_version, "explicit release name/version required")
    tools = {}
    for target, origin in SOURCE_FILES.items():
        raw, mode = blob(source, args.tool_revision, origin)
        path = output / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o755 if mode == "100755" else 0o644)
        tools[target] = {"source_repository": SOURCE_REPOSITORY, "source_revision": args.tool_revision,
                         "source_path": origin, "size_bytes": len(raw), "sha256": "sha256:"+identity(raw)["sha256"]}
    require((output / "pack.py").read_bytes() == Path(__file__).read_bytes(), "executing pack tool differs from committed tool revision")
    receipts = {"evidence/source-build.json": args.compile_receipt, "evidence/build-input-lock.json": args.input_lock,
                "evidence/local-runtime-probe.json": args.probe_record, "evidence/image-build.json": args.image_record,
                "evidence/publication.json": args.publication_record,
                "evidence/remote-manifest.json": args.publication_record.parent / "remote-manifest.json"}
    records = {name: document(path) for name, path in receipts.items()}
    compiled = records["evidence/source-build.json"][1]
    records["evidence/cargo-build.txt"] = bound_artifact(compiled.get("compile_log"), "path")
    records["evidence/kernel-build.txt"] = bound_artifact(compiled.get("kernel_proof"), "file")
    (output / "evidence").mkdir()
    for name, (raw, _) in records.items():
        (output / name).write_bytes(raw)
    image, publication = validate_inputs(source, args.source_revision, records)
    capability = json.loads((output / "product-capability.json").read_bytes(), object_pairs_hook=unique_object)
    model = json.loads((output / "models/qwen36-nvfp4.json").read_bytes(), object_pairs_hook=unique_object)
    component = {"id": "engine", "image_ref": publication["image_ref"], "image_id": image["image_id"],
        "platform": image["platform"], "size_bytes": image["size_bytes"], "backend": "atlas-native-cu130-sm121f-nvfp4",
        "provenance": {"source_revision": args.source_revision, "source_repository": SOURCE_REPOSITORY, "evidence_ref": "evidence/image-build.json"},
        "publication_evidence_ref": "evidence/publication.json", "image_assets": image["image_assets"]}
    evidence = [{"ref": name, "scope": name.removeprefix("evidence/").removesuffix(".json"),
                 "size_bytes": len(raw), "sha256": "sha256:"+identity(raw)["sha256"]} for name, (raw, _) in records.items()]
    qualification = {"schema_version": "service-release-qualification-v2", "status": "PASS", "scope": "artifact-release",
        "allowed_deployment_scope": "development", "production_status": "not_qualified", "source_revision": args.source_revision,
        "profile_id": PROFILE, "capability_sha256": canonical(capability), "components": {"engine": {k: component[k] for k in ("image_ref", "image_id")}}, "evidence": evidence}
    write_json(output / "qualification.json", qualification)
    release = {"schema_version": "product-release-v4", "product_ref": PRODUCT, "release_ref": args.release_ref,
        "service_version": args.service_version, "source_revision": args.source_revision, "tool_revision": args.tool_revision,
        "tool_sets": ["atlas-reliability-tools-v1"],
        "capability_source": SOURCE_FILES["product-capability.json"], "capability_source_revision": args.tool_revision,
        "capability_sha256": canonical(capability), "licenses_sha256": sha(output / "LICENSE"), "tools": tools,
        "distribution": {"status": "approved", "approval_ref": "operator-authorized-private-development:"+args.release_ref},
        "components": [component], "assets": [{"id": "qwen36", "kind": "directory", "source": model["hf_model"],
            "revision": model["hf_revision"], "mirror_repository": model["download_mirror"], "mirror_revision": model["mirror_revision"],
            "model_family": model["model_family"], "quantization": model["quantization"], "license": model["license"],
            "acquisition": "authorized-host-mount", "distribution": "not-included", "files": model["files"],
            "mount": "/models/qwen36:read-only", "provenance_unverified": model["provenance_unverified"]}],
        "artifacts": [{"profile_id": PROFILE, "image_ref": component["image_ref"], "image_id": component["image_id"],
            "oci_platform": "linux/arm64", "size_bytes": component["size_bytes"], "component_ids": ["engine"],
            "instance_schema_version": "service-instance-v5", "delivery": {"kind": "registry"},
            "verifier_ref": "bundle:verify.py", "verifier_sha256": sha(output / "verify.py")}],
        "qualifications": {"artifact_release": "PASS", "runtime": "matching local probe only; consumer acceptance separate",
            "upstream_matrix": "NOT_RUN", "performance": "NOT_RUN", "stability": "NOT_RUN", "business_accuracy": "NOT_RUN", "production": "not_qualified"},
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    manifest = {p.relative_to(output).as_posix(): sha(p) for p in sorted(output.rglob("*")) if p.is_file()}
    write_json(output / "bundle-manifest.json", manifest)
    release["bundle_manifest_sha256"] = sha(output / "bundle-manifest.json")
    write_json(output / "release.json", release)
    validate_bundle(output)
    source_state(source, args.source_revision, args.tool_revision)
    return {"status": "PASS", "output": str(output), "source_revision": args.source_revision, "tool_revision": args.tool_revision,
            "release_sha256": sha(output / "release.json"), "bundle_manifest_sha256": release["bundle_manifest_sha256"], "members": len(manifest)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("source", "image-record", "publication-record", "compile-receipt", "input-lock", "probe-record", "output"):
        parser.add_argument("--"+field, type=Path, required=True)
    for field in ("source-revision", "tool-revision", "release-ref", "service-version"):
        parser.add_argument("--"+field, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(pack(args), indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print("Atlas pack failed; partial output retained: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
