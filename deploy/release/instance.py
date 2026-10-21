# SPDX-License-Identifier: AGPL-3.0-only
"""Strict site binding, pure Compose rendering and ownership checks for Atlas."""
import copy
import hashlib
import os
from pathlib import Path
import re
import stat

from release_contract import IMAGE, PRODUCT, canonical, require, validate_bundle

SCHEMA = "service-instance-v5"
FIELDS = {"schema_version", "release_scope", "project_name", "container_name", "host_ip", "host_port",
          "manager", "binding_id", "components", "uid", "gid", "model_root", "cache_root", "model_alias"}


def positive(value):
    return type(value) is int and value > 0


def cpu_set(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*", value), "invalid explicit CPU set")
    selected = set()
    for item in value.split(","):
        ends = [int(x) for x in item.split("-")]
        start, end = ends[0], ends[-1]
        require(start <= end and end - start < 65536, "invalid CPU range")
        current = set(range(start, end+1))
        require(not selected & current, "overlapping CPU set")
        selected.update(current)
    return selected


def real_directory(value):
    require(isinstance(value, str) and value and "\x00" not in value, "explicit directory path required")
    path = Path(value)
    require(path.is_absolute() and ".." not in path.parts and path.is_dir(), "existing absolute directory required")
    require(not any(part.is_symlink() for part in [path, *path.parents]), "directory symlink refused")
    require(str(path) == value, "canonical directory path required")
    return path


def validate_instance(value, capability):
    require(isinstance(value, dict) and set(value) == FIELDS, "instance fields must match service-instance-v5")
    require(value["schema_version"] == SCHEMA and value["release_scope"] == "development", "explicit Atlas development instance required")
    require(capability["runtime"]["instance_schema_version"] == SCHEMA, "instance/capability schema mismatch")
    for field, pattern in [("project_name", r"[a-z0-9][a-z0-9_-]*"), ("container_name", r"[a-zA-Z0-9][a-zA-Z0-9_.-]*"),
                           ("manager", r"[a-zA-Z0-9][a-zA-Z0-9_.:-]*"), ("binding_id", r"[a-zA-Z0-9][a-zA-Z0-9_.:-]*")]:
        require(isinstance(value[field], str) and re.fullmatch(pattern, value[field]), "invalid instance identity: " + field)
    require(value["host_ip"] == "127.0.0.1" and positive(value["host_port"]) and value["host_port"] <= 65535, "Atlas development binding is IPv4 loopback-only")
    require(value["model_alias"] == "qwen3-6-35b-a3b", "public model alias mismatch")
    require(positive(value["uid"]) and positive(value["gid"]), "explicit non-root UID/GID required")
    components = value["components"]
    require(isinstance(components, dict) and set(components) == {"engine"}, "single engine resource binding required")
    limits = components["engine"]
    require(isinstance(limits, dict) and set(limits) == {"cpuset", "cpu_limit_millicores", "memory_limit_bytes"}, "explicit component resource fields required")
    cpus = cpu_set(limits["cpuset"])
    floor = capability["runtime"]["minimum_resources"]["engine"]
    for field in ("cpu_limit_millicores", "memory_limit_bytes"):
        require(positive(limits[field]) and limits[field] >= floor[field], "component resource floor violated")
    require(limits["cpu_limit_millicores"] <= len(cpus)*1000, "CPU quota exceeds bound CPU set")
    model, cache = real_directory(value["model_root"]), real_directory(value["cache_root"])
    require(not model.is_relative_to(cache) and not cache.is_relative_to(model), "model/cache roots overlap")
    identity = cache.stat()
    require(identity.st_uid == value["uid"] and identity.st_mode & stat.S_IWUSR and identity.st_mode & stat.S_IXUSR, "cache must be writable and owned by declared UID")
    if value["uid"] == os.geteuid():
        require(os.access(cache, os.W_OK | os.X_OK), "cache is not writable by runtime UID")
    return value


def labels(value):
    return {"org.atlas.product": PRODUCT, "org.atlas.manager": value["manager"],
            "org.atlas.binding-id": value["binding_id"], "org.atlas.instance-sha256": canonical(value)}


def substitute(value, replacements):
    if isinstance(value, dict):
        return {key: substitute(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, replacements) for item in value]
    if isinstance(value, str):
        if value in replacements:
            return replacements[value]
        require(not re.search(r"\$\{[^}]+\}", value), "unknown or embedded runtime placeholder")
    return value


def render_contract(release, capability, value):
    """Publisher declaration API; consumer entrypoint is bundle-validated render()."""
    validate_instance(value, capability)
    require(len(release["components"]) == 1 and release["components"][0]["id"] == "engine", "runtime component mismatch")
    image = release["components"][0]["image_ref"]
    require(IMAGE.fullmatch(image), "digest-pinned private runtime image required")
    compose = substitute(copy.deepcopy(capability["runtime"]["compose"]), {
        "${image:engine}": image, "${instance:model_alias}": value["model_alias"],
        "${instance:model_root}": value["model_root"], "${instance:cache_root}": value["cache_root"]})
    require(set(compose["services"]) == {"engine"}, "unexpected runtime service")
    service = compose["services"]["engine"]
    limits = value["components"]["engine"]
    require(service["mem_reservation"] <= limits["memory_limit_bytes"], "reservation exceeds component limit")
    # Compose compares service and deploy memory whenever GPU reservations exist.
    # Derive both representations from the one frozen capacity reservation.
    service.setdefault("deploy", {}).setdefault("resources", {}).setdefault("reservations", {})["memory"] = str(service["mem_reservation"])
    service.update(container_name=value["container_name"]+"-engine", user=f"{value['uid']}:{value['gid']}",
                   ports=[f"127.0.0.1:{value['host_port']}:{capability['service']['container_port']}"],
                   cpuset=limits["cpuset"], cpus=limits["cpu_limit_millicores"]/1000,
                   mem_limit=limits["memory_limit_bytes"], memswap_limit=limits["memory_limit_bytes"], networks=["service"])
    service.setdefault("labels", {}).update(labels(value))
    compose["name"] = value["project_name"]
    compose["networks"] = {"service": {"name": value["project_name"]+"-service", "driver": "bridge", "labels": labels(value)}}
    return compose


def render(root, value):
    release, capability, _ = validate_bundle(root)
    return render_contract(release, capability, value)


def desired_resources(desired):
    require(isinstance(desired, dict) and isinstance(desired.get("services"), dict) and desired["services"], "desired Compose services required")
    result = {}
    for service in desired["services"].values():
        require(isinstance(service, dict), "invalid desired service")
        name = service.get("container_name")
        require(isinstance(name, str) and name, "named desired container required")
        key = ("container", name)
        require(key not in result, "duplicate desired resource")
        result[key] = service.get("labels", {})
    for kind, section in [("network", "networks"), ("volume", "volumes")]:
        require(isinstance(desired.get(section, {}), dict), "desired resource section must be an object")
        for resource in desired.get(section, {}).values():
            require(isinstance(resource, dict) and not resource.get("external") and isinstance(resource.get("name"), str) and resource["name"], "only named task-owned resources permitted")
            key = (kind, resource["name"])
            require(key not in result, "duplicate desired resource")
            result[key] = resource.get("labels", {})
    return result


def ownership(value, desired_labels):
    require(isinstance(desired_labels, dict), "desired ownership labels required")
    own = labels(value)
    has_atlas = any(key.startswith("org.atlas.") for key in desired_labels)
    has_neutral = any(key.startswith("com.xd.model-deploy.") for key in desired_labels)
    require(has_atlas or has_neutral, "desired resource is not owned")
    expected = {}
    if has_atlas:
        require(all(desired_labels.get(key) == item for key, item in own.items()), "desired Atlas ownership mismatch")
        expected.update(own)
    if has_neutral:
        binding = desired_labels.get("com.xd.model-deploy.binding-id")
        require(desired_labels.get("com.xd.model-deploy.owner") == PRODUCT and isinstance(binding, str)
                and hashlib.sha256(binding.encode()).hexdigest()[:32] == value["binding_id"], "desired neutral ownership mismatch")
        expected.update({key: item for key, item in desired_labels.items() if key.startswith("com.xd.model-deploy.")})
    return expected


def guard(value, desired, snapshot):
    """Pure complete-snapshot ownership check; never queries Docker or touches paths."""
    resources = desired_resources(desired)
    require(isinstance(snapshot, dict) and snapshot.get("schema_version") == "owned-resource-snapshot-v1"
            and snapshot.get("execution_plane") == "docker" and isinstance(snapshot.get("records"), list), "complete Docker ownership snapshot required")
    records = {}
    for record in snapshot["records"]:
        require(isinstance(record, dict) and isinstance(record.get("kind"), str) and record["kind"] in {"container", "network", "volume"} and isinstance(record.get("name"), str), "invalid ownership snapshot record")
        key = (record["kind"], record["name"])
        require(key not in records and isinstance(record.get("state"), str) and record["state"] in {"absent", "present"}, "duplicate or invalid ownership snapshot record")
        records[key] = record
    require(set(records) == set(resources), "ownership snapshot coverage mismatch")
    for key, desired_labels in resources.items():
        expected = ownership(value, desired_labels)
        record = records[key]
        if record["state"] == "absent":
            require("object" not in record, "absent resource cannot contain inspected object")
            continue
        actual = record.get("object")
        require(isinstance(actual, dict) and isinstance(actual.get("Name"), str) and actual["Name"].lstrip("/") == key[1], "inspected resource identity mismatch")
        if key[0] == "container":
            require(isinstance(actual.get("Config"), dict), "container configuration missing")
            actual_labels = actual["Config"].get("Labels", {})
        else:
            actual_labels = actual.get("Labels", {})
        require(isinstance(actual_labels, dict) and all(actual_labels.get(label) == item for label, item in expected.items()), "existing resource ownership mismatch")
    return {"schema_version": "atlas-ownership-guard-v1", "status": "PASS", "instance_sha256": canonical(value),
            "resources": [{"kind": kind, "name": name, "state": records[(kind, name)]["state"]} for kind, name in sorted(resources)]}
