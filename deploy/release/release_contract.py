# SPDX-License-Identifier: AGPL-3.0-only
"""Offline Atlas-specific validation of the existing product-release-v4 format."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import re

PRODUCT = "product:atlas-qwen36-nvfp4"
PROFILE = "gb10-qwen36-nvfp4-cu130-development"
SOURCE_REPOSITORY = "https://github.com/MonadKai/atlas"
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
IMAGE = re.compile(r"registry\.100credit\.cn/[^@\s]+@sha256:[0-9a-f]{64}\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON object key")
        result[key] = value
    return result


def load(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=unique_object)


def canonical(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def sha(path):
    with Path(path).open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def member(root, name):
    require(isinstance(name, str), "bundle member must be a relative path")
    rel = PurePosixPath(name)
    require(name and str(rel) == name and not rel.is_absolute() and ".." not in rel.parts, "unsafe bundle member")
    root = Path(root).resolve()
    path = root / rel
    require(not any(root.joinpath(*rel.parts[:i]).is_symlink() for i in range(1, len(rel.parts)+1)), "bundle symlink refused")
    require(path.is_file() and path.resolve().is_relative_to(root), "missing bundle member")
    return path


def validate_release(release, capability, qualification):
    require(isinstance(release, dict) and isinstance(capability, dict) and isinstance(qualification, dict), "release documents must be objects")
    require(release.get("schema_version") == "product-release-v4", "Atlas requires explicit product-release-v4")
    require(release.get("product_ref") == capability.get("product_ref") == PRODUCT, "Atlas product identity mismatch")
    require(capability.get("schema_version") == "product-deployment-capability-v2", "mounted capability-v2 required")
    for field in ("source_revision", "tool_revision", "capability_source_revision"):
        require(COMMIT.fullmatch(release.get(field, "")), "full source/tool revisions required")
    require(release.get("capability_sha256") == canonical(capability), "capability hash mismatch")
    require(release.get("release_ref") and release.get("service_version"), "versioned release required")
    require(release.get("distribution", {}).get("status") == "approved" and release["distribution"].get("approval_ref"), "explicit private distribution authorization required")
    require(release.get("capability_source") == "deploy/release/profiles/gb10-qwen36-nvfp4.json", "source-owned Atlas capability required")
    profiles = capability.get("profiles", [])
    require(len(profiles) == 1 and profiles[0] == {"profile_id": PROFILE, "execution_plane": "docker", "architecture": "arm64", "accelerator_backend": "nvidia-cuda", "allocation_mode": "shared", "memory_topology": "unified"}, "exact GB10 ARM64 CUDA development profile required")
    runtime = capability.get("runtime", {})
    require(runtime.get("deployment_protocol") == "service-docker-v3" and runtime.get("instance_schema_version") == "service-instance-v5" and runtime.get("admission_scope") == "development" and runtime.get("api_component") == "engine", "Atlas runtime scope mismatch")
    require(capability.get("allowed_exposure") == ["loopback"], "development artifact is loopback-only")
    require(capability.get("service", {}).get("protocol") == "http" and capability["service"].get("container_port") == 8888 and capability["service"].get("path") == "/v1/chat/completions", "public HTTP contract mismatch")
    require(runtime.get("contract_version") == "atlas-gb10-qwen36-runtime-v1" and runtime.get("protocol_version") == "atlas-openai-qwen36-v1", "runtime/protocol versions required")
    require(runtime.get("required_assets") == ["qwen36"], "required model asset completeness mismatch")
    require(runtime.get("minimum_resources") == {"engine": {"cpu_limit_millicores": 4000, "memory_limit_bytes": 68719476736}}, "qualified resource floors mismatch")
    settings = runtime.get("settings", {})
    require(settings.get("context_length") == 32768 and settings.get("prefix_cache") is True and settings.get("turboquant") is False and settings.get("kv_dtype") == "bf16" and settings.get("max_prefill_tokens") == 4096 and settings.get("mtp_gate") == "auto", "frozen model/cache/precision profile mismatch")
    children = release.get("components", [])
    require(len(children) == 1 and children[0].get("id") == "engine", "single engine component required")
    child = children[0]
    require(IMAGE.fullmatch(child.get("image_ref", "")) and DIGEST.fullmatch(child.get("image_id", "")), "private remote manifest and independent config ID required")
    require(child.get("platform") == "linux/arm64" and child.get("backend") == "atlas-native-cu130-sm121f-nvfp4" and type(child.get("size_bytes")) is int and child["size_bytes"] > 0, "component platform/backend identity mismatch")
    require(child.get("provenance", {}).get("source_revision") == release["source_revision"] and child["provenance"].get("source_repository") == SOURCE_REPOSITORY, "component source origin mismatch")
    require(child.get("image_assets") and len({a.get("path") for a in child["image_assets"]}) == len(child["image_assets"]), "unique native/code image inventory required")
    for item in child["image_assets"]:
        require(isinstance(item.get("path"), str) and item["path"].startswith("/") and ".." not in PurePosixPath(item["path"]).parts and DIGEST.fullmatch(item.get("sha256", "")) and type(item.get("size_bytes")) is int and item["size_bytes"] >= 0 and item.get("license"), "image asset path/size/hash/license required")
    require(set(runtime.get("compose", {}).get("services", {})) == {"engine"} and runtime["compose"]["services"]["engine"].get("image") == "${image:engine}", "runtime image/component binding mismatch")
    assets = release.get("assets", [])
    require(len(assets) == 1 and assets[0].get("id") == "qwen36", "full mounted model inventory required")
    asset = assets[0]
    require(asset.get("acquisition") == "authorized-host-mount" and asset.get("distribution") == "not-included" and asset.get("kind") == "directory" and asset.get("source") and asset.get("license") and asset.get("revision"), "model acquisition/version/license required")
    require(isinstance(asset.get("files"), dict) and len(asset["files"]) == 16, "fixed16-file model inventory required")
    for name, identity in asset["files"].items():
        rel = PurePosixPath(name)
        require(name == str(rel) and not rel.is_absolute() and ".." not in rel.parts and DIGEST.fullmatch(identity.get("sha256", "")) and type(identity.get("size_bytes")) is int and identity["size_bytes"] >= 0, "invalid model member identity")
    artifacts = release.get("artifacts", [])
    require(len(artifacts) == 1, "single selected artifact required")
    artifact = artifacts[0]
    require(all(artifact.get(k) == child.get(k) for k in ("image_ref", "image_id", "size_bytes")) and artifact.get("component_ids") == ["engine"] and artifact.get("oci_platform") == "linux/arm64" and artifact.get("profile_id") == PROFILE and artifact.get("instance_schema_version") == "service-instance-v5", "primary artifact completeness/identity mismatch")
    require(qualification.get("schema_version") == "service-release-qualification-v2" and qualification.get("status") == "PASS" and qualification.get("scope") == "artifact-release" and qualification.get("allowed_deployment_scope") == "development" and qualification.get("production_status") == "not_qualified", "development artifact qualification required")
    require(qualification.get("source_revision") == release["source_revision"] and qualification.get("profile_id") == PROFILE and qualification.get("capability_sha256") == canonical(capability) and qualification.get("components") == {"engine": {k: child[k] for k in ("image_ref", "image_id")}}, "qualification identity mismatch")
    require(qualification.get("evidence") and len({e.get("ref") for e in qualification["evidence"]}) == len(qualification["evidence"]), "unique evidence references required")
    for item in qualification["evidence"]:
        require(item.get("scope") and isinstance(item.get("ref"), str) and item["ref"].startswith("evidence/") and DIGEST.fullmatch(item.get("sha256", "")), "invalid qualification evidence")
    return profiles[0]


def recorded_file(root, name, record):
    path = member(root, name)
    require(isinstance(record, dict) and type(record.get("size_bytes")) is int
            and record["size_bytes"] == path.stat().st_size
            and "sha256:"+record.get("sha256", "") == sha(path), "recorded artifact byte identity mismatch: " + name)
    return path


def validate_build_evidence(root, release, child, build, published, compiled, qualification):
    """Recheck relationships from bundled bytes only; never open historical paths."""
    required = {"evidence/source-build.json", "evidence/build-input-lock.json", "evidence/local-runtime-probe.json",
                "evidence/image-build.json", "evidence/publication.json", "evidence/remote-manifest.json",
                "evidence/cargo-build.txt", "evidence/kernel-build.txt"}
    require(required <= {e["ref"] for e in qualification["evidence"]}, "complete build/publication evidence required")
    lock_path = recorded_file(root, "evidence/build-input-lock.json", compiled.get("input_lock"))
    lock = load(lock_path)
    require(lock.get("schema_version") == "atlas-private-build-input-lock-v1" and lock.get("source_revision") == release["source_revision"]
            and COMMIT.fullmatch(lock.get("source_tree", "")), "build input lock source/tree mismatch")
    profile_path = recorded_file(root, "build_profile.json", lock.get("profile_identity"))
    profile = load(profile_path)
    require(profile.get("schema_version") == "atlas-private-build-profile-v1" and lock.get("profile") == profile, "locked build profile differs from shipped profile")
    paths = profile.get("source_inputs")
    require(isinstance(paths, list) and paths and all(isinstance(p, str) for p in paths)
            and len(paths) == len(set(paths)) and isinstance(lock.get("source_inputs"), dict)
            and set(lock["source_inputs"]) == set(paths)
            and all(COMMIT.fullmatch(oid) for oid in lock["source_inputs"].values()), "locked source input object completeness mismatch")
    cargo = lock.get("cargo_lock", {})
    require(type(cargo.get("size_bytes")) is int and cargo["size_bytes"] > 0 and DIGEST.fullmatch("sha256:"+cargo.get("sha256", "")), "locked Cargo identity required")
    environment = lock.get("environment", {})
    require(environment.get("runtime_libraries") == profile.get("runtime_libraries"), "locked native environment differs from profile")
    tools = environment.get("tools", {})
    require(set(tools) == set(profile["tools"]) and all(all(tools[name].get(key) == spec[key] for key in ("size_bytes", "sha256"))
            for name, spec in profile["tools"].items()), "locked compiler/toolchain identity mismatch")
    require(compiled.get("tool_revision") == release["source_revision"] and compiled.get("kernel_proof", {}).get("closure") == profile.get("kernel_closure"), "compiled source tool/kernel identity mismatch")
    expected_environment = {"AVAROK_TARGET_HW": "gb10", "AVAROK_TARGET_MODEL": "qwen3.6-35b-a3b",
                            "AVAROK_TARGET_QUANT": "nvfp4", "RUSTUP_TOOLCHAIN": profile["rust_version"]}
    build_environment = compiled.get("compile_environment", {})
    require(all(build_environment.get(key) == value for key, value in expected_environment.items())
            and not {"AVAROK_SKIP_BUILD", "SKIP_AVAROK_BUILD"} & set(build_environment), "actual compile target/toolchain flags mismatch")
    command = compiled.get("cargo_command", [])
    require(isinstance(command, list) and all(isinstance(arg, str) for arg in command)
            and all(flag in command for flag in ("--locked", "--offline", "--release", "--bin")), "locked offline source compile command required")
    require(command[command.index("--bin"):command.index("--bin")+2] == ["--bin", "spark"], "explicit spark source compile target required")
    kernel_path = recorded_file(root, "evidence/kernel-build.txt", compiled.get("kernel_proof"))
    prefix = "cargo:rustc-env=AVAROK_TARGET_CLOSURES="
    kernel_text = kernel_path.read_text()
    closures = [json.loads(line[len(prefix):], object_pairs_hook=unique_object) for line in kernel_text.splitlines() if line.startswith(prefix)]
    require(closures == [profile["kernel_closure"]] and "183/183 unique nvcc invocations (0 cache hits" in kernel_text, "actual kernel log closure/fresh compile mismatch")
    cargo_path = recorded_file(root, "evidence/cargo-build.txt", compiled.get("compile_log"))
    cargo_records = []
    for line in cargo_path.read_text().splitlines():
        if line.startswith("{"):
            cargo_records.append(json.loads(line, object_pairs_hook=unique_object))
    finished = [r for r in cargo_records if r.get("reason") == "build-finished"]
    binaries = [r for r in cargo_records if r.get("reason") == "compiler-artifact" and r.get("target", {}).get("name") == "spark"
                and r.get("executable") == compiled.get("binary", {}).get("path")]
    require(len(finished) == 1 and finished[0].get("success") is True and len(binaries) == 1, "actual successful spark compilation not present in Cargo log")
    inventory = {a["path"]: a for a in child["image_assets"]}
    require(build.get("image_assets") == child["image_assets"] and build.get("platform") == child["platform"] and build.get("size_bytes") == child["size_bytes"], "image build inventory/platform differs from release")
    binary = compiled.get("binary", {})
    require(type(binary.get("size_bytes")) is int and binary["size_bytes"] > 0 and inventory["/usr/local/bin/spark"]["size_bytes"] == binary["size_bytes"], "compiled/runtime binary size mismatch")
    libraries = profile["runtime_libraries"]
    require(isinstance(libraries, list) and len(libraries) == 8 and len({a["soname"] for a in libraries}) == 8, "complete fixed native library closure required")
    for item in libraries:
        actual = inventory.get("/opt/atlas/lib/"+item["soname"], {})
        require(actual.get("size_bytes") == item["size_bytes"] and actual.get("sha256") == "sha256:"+item["sha256"], "runtime native file differs from fixed build profile")
    raw_path = member(root, "evidence/remote-manifest.json")
    remote_digest = sha(raw_path)
    remote = load(raw_path)
    require(published.get("digest_pull") == "PASS" and published.get("source_revision") == release["source_revision"]
            and published.get("remote_manifest_digest") == remote_digest and child["image_ref"].rsplit("@", 1)[1] == remote_digest
            and remote.get("schemaVersion") == 2 and remote.get("config", {}).get("digest") == child["image_id"], "actual remote manifest/config/digest-pull mismatch")


def validate_bundle(root):
    root = Path(root).resolve()
    release = load(member(root, "release.json"))
    require(sha(member(root, "bundle-manifest.json")) == release.get("bundle_manifest_sha256"), "bundle manifest mismatch")
    manifest = load(root / "bundle-manifest.json")
    required = {"toolkit.py", "verify.py", "release_contract.py", "instance.py", "assets.py", "acceptance.py", "acceptance_cases.py", "acceptance_http.py", "acceptance_suites.py", "acceptance_profile.py", "fixtures/cases.json", "models/qwen36-nvfp4.json", "runtime_identity.py", "observe_shutdown.py", "qualification.json", "product-capability.json", "README.md", "LICENSE", "skills/atlas-private-deploy/SKILL.md", "skills/avarok-release/references/private-gb10.md"}
    require(isinstance(manifest, dict) and required <= set(manifest), "required bundle member missing")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()}
    require(actual == set(manifest) | {"release.json", "bundle-manifest.json"}, "unmanifested or missing bundle member")
    for name, expected in manifest.items():
        require(DIGEST.fullmatch(expected) and sha(member(root, name)) == expected, "bundle member hash mismatch: " + name)
    capability = load(root / "product-capability.json")
    qualification = load(root / "qualification.json")
    profile = validate_release(release, capability, qualification)
    require(release.get("licenses_sha256") == sha(root / "LICENSE") and release["artifacts"][0].get("verifier_sha256") == sha(root / "verify.py"), "verifier/license binding mismatch")
    require(release.get("tools") and set(release["tools"]) == {name for name in manifest if not name.startswith("evidence/") and name != "qualification.json"}, "complete source tool closure missing")
    for name, origin in release["tools"].items():
        path = member(root, name)
        source_path = PurePosixPath(origin.get("source_path", ""))
        require(origin.get("source_revision") == release["tool_revision"] and origin.get("source_repository") == SOURCE_REPOSITORY
                and origin.get("sha256") == sha(path) and origin.get("size_bytes") == path.stat().st_size
                and origin.get("source_path") and not source_path.is_absolute() and ".." not in source_path.parts
                and not path.read_bytes().startswith(b"version https://git-lfs.github.com/spec/v1\n"), "tool source binding mismatch")
    require(release["capability_source_revision"] == release["tool_revision"]
            and release["tools"]["product-capability.json"]["source_path"] == release["capability_source"], "capability source/tool binding mismatch")
    model = load(root / "models/qwen36-nvfp4.json")
    require(release["assets"][0]["files"] == model["files"] and release["assets"][0]["revision"] == model["hf_revision"], "model inventory authority mismatch")
    for item in qualification["evidence"]:
        path = member(root, item["ref"])
        require(sha(path) == item["sha256"] and type(item.get("size_bytes")) is int and item["size_bytes"] == path.stat().st_size, "evidence content/size mismatch")
    child = release["components"][0]
    build = load(member(root, child["provenance"]["evidence_ref"]))
    published = load(member(root, child["publication_evidence_ref"]))
    require(build.get("schema_version") == "atlas-image-build-receipt-v1" and build.get("status") == "PASS" and build.get("source_revision") == release["source_revision"] and build.get("image_id") == child["image_id"], "actual image build identity mismatch")
    require(published.get("schema_version") == "atlas-image-publication-v1" and published.get("readback") == "PASS" and all(published.get(k) == child[k] for k in ("image_ref", "image_id", "platform", "size_bytes")), "publication manifest/config/platform mismatch")
    for name in (child["provenance"]["evidence_ref"], child["publication_evidence_ref"]):
        require(name in {e["ref"] for e in qualification["evidence"]}, "component evidence omitted from qualification")
    compile_receipt = load(member(root, "evidence/source-build.json"))
    require(compile_receipt.get("schema_version") == "atlas-private-build-receipt-v1" and compile_receipt.get("source_revision") == release["source_revision"] and compile_receipt.get("qualification", {}).get("source_build") == "PASS", "real fixed-source compile receipt required")
    require(build.get("compile_receipt_sha256") == sha(root / "evidence/source-build.json"), "compile receipt/image binding mismatch")
    require(build.get("binary_sha256") == "sha256:" + compile_receipt.get("binary", {}).get("sha256", ""), "compiled binary/image binding mismatch")
    require(any(a["path"] == "/usr/local/bin/spark" and a["sha256"] == build["binary_sha256"] for a in child["image_assets"]), "runtime binary differs from source compile")
    require(build.get("labels", {}).get("org.opencontainers.image.source") == SOURCE_REPOSITORY and build["labels"].get("org.opencontainers.image.revision") == release["source_revision"], "fork image origin/source labels mismatch")
    baseline = load(member(root, "evidence/local-runtime-probe.json"))
    require(baseline.get("status") == "PASS" and baseline.get("image_id") == child["image_id"] and baseline.get("source_revision") == release["source_revision"] and baseline.get("scope") == "single-model-development-runtime", "prepublication actual local model probe required")
    validate_build_evidence(root, release, child, build, published, compile_receipt, qualification)
    return release, capability, profile
