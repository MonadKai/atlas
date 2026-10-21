# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only full runtime comparison; mappings/fds do not prove GPU execution."""
from decimal import Decimal
import hashlib
import json
from pathlib import PurePosixPath
import re
import subprocess
import tempfile
from pathlib import Path


def canonical_hash(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def byte_size(value):
    if type(value) is int:
        return value
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kmgt]?)(?:i?b)?", str(value).lower())
    if not match:
        raise ValueError("invalid declared resource size")
    return int(Decimal(match[1]) * (1024 ** ("kmgt".find(match[2]) + 1) if match[2] else 1))


def nanoseconds(value):
    if isinstance(value, (int, float)):
        return int(Decimal(str(value)) * 1_000_000_000)
    factors = {"ns": 1, "us": 1000, "ms": 1_000_000, "s": 1_000_000_000, "m": 60_000_000_000, "h": 3_600_000_000_000}
    parts = re.findall(r"([0-9]+(?:\.[0-9]+)?)(ns|us|ms|s|m|h)", str(value))
    if not parts or "".join(number + unit for number, unit in parts) != str(value):
        raise ValueError("invalid declared duration")
    return int(sum(Decimal(number) * factors[unit] for number, unit in parts))


def environment(value):
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    result = {}
    for item in value or []:
        key, sep, val = item.partition("=")
        if not sep or key in result:
            raise ValueError("invalid or duplicate environment entry")
        result[key] = val
    return result


def compare_complete_environment(actual_config, image_config, service):
    complete_env = environment(image_config.get("Env"))
    complete_env.update(environment(service.get("environment")))
    if environment(actual_config.get("Env")) != complete_env:
        raise ValueError("runtime identity mismatch fields: complete_environment")


def port_bindings(service):
    result = {}
    for port in service.get("ports", []):
        if not isinstance(port, dict):
            raise ValueError("explicit long-syntax port contract required")
        key = f"{port['target']}/{port.get('protocol', 'tcp')}"
        result.setdefault(key, []).append({"HostIp": port["host_ip"], "HostPort": str(port["published"])})
    return {key: sorted(value, key=lambda item: (item["HostIp"], item["HostPort"])) for key, value in result.items()}


def tmpfs_options(service):
    result = {}
    for item in service.get("tmpfs", []):
        if not isinstance(item, str) or ":" not in item:
            raise ValueError("explicit tmpfs options required")
        path, options = item.split(":", 1)
        result[path] = sorted(options.split(","))
    return result


def device_requests(service):
    devices = service.get("deploy", {}).get("resources", {}).get("reservations", {}).get("devices", [])
    return [{"Driver": item["driver"], "Count": -1 if item.get("count") == "all" else item.get("count", 0), "DeviceIDs": item.get("device_ids", []), "Capabilities": [sorted(item["capabilities"])], "Options": item.get("options", {})} for item in devices]


def normalize_devices(items):
    result = [{"Driver": item.get("Driver", ""), "Count": item.get("Count", 0), "DeviceIDs": item.get("DeviceIDs") or [], "Capabilities": sorted(sorted(row) for row in item.get("Capabilities", [])), "Options": item.get("Options") or {}} for item in items or []]
    return sorted(result, key=canonical_hash)


def expected_mounts(service):
    result = []
    for mount in service.get("volumes", []):
        if not isinstance(mount, dict) or mount["type"] != "bind":
            raise ValueError("explicit bind mount declaration required")
        result.append({"type": "bind", "source": mount["source"], "target": mount["target"], "read_only": mount.get("read_only", False), "propagation": mount.get("bind", {}).get("propagation", "rprivate")})
    return sorted(result, key=lambda item: item["target"])


def compare_container(actual, component, service, network_names=None):
    """Pure comparison: errors report field names, never values/argv/env."""
    mismatches, checks = [], []

    def check(field, condition):
        (checks if condition else mismatches).append(field)

    config, host, state = actual.get("Config", {}), actual.get("HostConfig", {}), actual.get("State", {})
    check("container_name", actual.get("Name", "").removeprefix("/") == service["container_name"])
    check("image_config_id", actual.get("Image") == component["image_id"])
    check("immutable_image_reference", config.get("Image") == component["image_ref"] == service.get("image"))
    check("running", state.get("Running") is True and state.get("Paused") is not True and state.get("Restarting") is not True)
    check("command", config.get("Cmd") == service.get("command"))
    check("entrypoint", config.get("Entrypoint") == service.get("entrypoint"))
    check("user", config.get("User") == service.get("user"))
    check("readonly_rootfs", host.get("ReadonlyRootfs", False) == service.get("read_only", False))
    check("init", host.get("Init", False) == service.get("init", False))
    check("restart_policy", host.get("RestartPolicy", {}) == {"Name": service.get("restart", "no"), "MaximumRetryCount": 0})
    check("stop_grace", config.get("StopTimeout") == nanoseconds(service["stop_grace_period"]) // 1_000_000_000)
    check("stop_signal", config.get("StopSignal") in (None, "", service.get("stop_signal", "SIGTERM")))
    check("cpuset", host.get("CpusetCpus", "") == service.get("cpuset", ""))
    check("cpu_limit", host.get("NanoCpus", 0) == int(Decimal(str(service["cpus"])) * 1_000_000_000))
    check("cpu_quota_not_overridden", host.get("CpuQuota", 0) == 0 and host.get("CpuPeriod", 0) == 0)
    check("memory_limit", host.get("Memory") == byte_size(service["mem_limit"]))
    check("memory_swap_limit", host.get("MemorySwap") == byte_size(service["memswap_limit"]))
    check("memory_reservation", host.get("MemoryReservation") == byte_size(service["mem_reservation"]))
    check("shared_memory", host.get("ShmSize") == byte_size(service["shm_size"]))
    check("port_bindings", (host.get("PortBindings") or {}) == port_bindings(service))
    check("tmpfs", {key: sorted(value.split(",")) for key, value in (host.get("Tmpfs") or {}).items()} == tmpfs_options(service))
    mounts = [{"type": mount["Type"], "source": mount["Source"], "target": mount["Destination"], "read_only": not mount["RW"], "propagation": mount.get("Propagation", "")} for mount in actual.get("Mounts", []) if mount["Type"] != "tmpfs"]
    check("bind_mounts", sorted(mounts, key=lambda item: item["target"]) == expected_mounts(service))
    check("cap_drop", sorted(host.get("CapDrop") or []) == sorted(service.get("cap_drop", [])))
    check("cap_add", sorted(host.get("CapAdd") or []) == sorted(service.get("cap_add", [])))
    check("security_options", sorted(host.get("SecurityOpt") or []) == sorted(service.get("security_opt", [])))
    check("privileged", host.get("Privileged", False) is False)
    check("device_requests", normalize_devices(host.get("DeviceRequests")) == normalize_devices(device_requests(service)))
    check("additional_devices", not host.get("Devices"))
    expected_env, actual_env = environment(service.get("environment")), environment(config.get("Env"))
    check("expected_environment", all(actual_env.get(key) == value for key, value in expected_env.items()))
    check("expected_labels", all((config.get("Labels") or {}).get(key) == value for key, value in service.get("labels", {}).items()))
    health = service.get("healthcheck", {})
    expected_health = {"Test": health.get("test"), "Interval": nanoseconds(health["interval"]), "Timeout": nanoseconds(health["timeout"]), "StartPeriod": nanoseconds(health["start_period"]), "Retries": health["retries"]} if health else {}
    check("healthcheck_configuration", all((config.get("Healthcheck") or {}).get(key) == value for key, value in expected_health.items()))
    check("healthcheck_status", (state.get("Health") or {}).get("Status") == "healthy")
    check("logging", host.get("LogConfig") == {"Type": service["logging"]["driver"], "Config": service["logging"].get("options", {})})
    if network_names is not None:
        check("network_membership", set(actual.get("NetworkSettings", {}).get("Networks", {})) == set(network_names))
        check("network_mode", host.get("NetworkMode") in set(network_names))
    if mismatches:
        raise ValueError("runtime identity mismatch fields: " + ",".join(mismatches))
    return checks


def docker_read(arguments):
    try:
        result = subprocess.run(["docker", *arguments], text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("Docker read-only observation failed") from error
    if result.returncode:
        raise ValueError("Docker read-only observation failed")
    return result.stdout


def normalize_compose(desired):
    """Use Compose's read-only parser; never load a consumer cwd .env file."""
    with tempfile.TemporaryDirectory(prefix="atlas-compose-observe-") as directory:
        path = Path(directory) / "compose.json"
        path.write_text(json.dumps(desired))
        text = docker_read(["compose", "--env-file", "/dev/null", "--project-directory", directory, "-f", str(path), "config", "--format", "json"])
    result = json.loads(text)
    if not isinstance(result, dict) or set(result.get("services", {})) != {"engine"}:
        raise ValueError("invalid normalized compose declaration")
    return result


def engine_literals(value):
    """Compose config serializes literal dollars as $$ for later consumption."""
    if isinstance(value, str):
        return value.replace("$$", "$")
    if isinstance(value, list):
        return [engine_literals(item) for item in value]
    if isinstance(value, dict):
        return {key: engine_literals(item) for key, item in value.items()}
    return value


def image_files(container_id, inventory):
    paths = [item["path"] for item in inventory]
    if len(set(paths)) != len(paths) or any(not isinstance(path, str) or not path.startswith("/") or str(PurePosixPath(path)) != path or ".." in PurePosixPath(path).parts or any(char in path for char in "\x00\r\n\\") for path in paths):
        raise ValueError("unsafe or duplicate image asset path")
    hashes = {}
    for line in docker_read(["exec", container_id, "sha256sum", "--", *paths]).splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (/.+)", line)
        if not match or match[2] in hashes:
            raise ValueError("invalid image file hash observation")
        hashes[match[2]] = "sha256:" + match[1]
    sizes = {}
    for line in docker_read(["exec", container_id, "stat", "--printf=%s %n\\n", "--", *paths]).splitlines():
        size, sep, path = line.partition(" ")
        if not sep or not size.isdecimal() or path in sizes:
            raise ValueError("invalid image file size observation")
        sizes[path] = int(size)
    if set(hashes) != set(paths) or set(sizes) != set(paths):
        raise ValueError("image file observation completeness mismatch")
    result = []
    for item in inventory:
        path = item["path"]
        if hashes[path] != item["sha256"] or sizes[path] != item["size_bytes"]:
            raise ValueError("runtime identity mismatch fields: image_assets")
        result.append({"path": path, "sha256": hashes[path], "size_bytes": sizes[path], "status": "PASS"})
    return result


def loaded_native_observations(container_id):
    # Resolved executable/library/device paths only; no argv or environment.
    script = """for process in /proc/[0-9]*; do
exe=$(readlink "$process/exe" 2>/dev/null) || continue
case "$exe" in */spark)
printf 'PID %s\\n' "${process##*/}"
awk '$NF ~ /^\\// && $NF ~ /\\.so/ { print "MAP " $NF }' "$process/maps" 2>/dev/null
for fd in "$process"/fd/*; do target=$(readlink "$fd" 2>/dev/null) || continue; case "$target" in /dev/nvidia*) printf 'DEVICE %s\\n' "$target";; esac; done
;; esac
done"""
    text = docker_read(["exec", container_id, "/bin/sh", "-c", script])
    pids, libraries, descriptors = set(), set(), []
    for line in text.splitlines():
        kind, sep, value = line.partition(" ")
        if sep and kind == "PID" and value.isdecimal():
            pids.add(int(value))
        elif sep and kind == "MAP" and value.startswith("/"):
            libraries.add(value)
        elif sep and kind == "DEVICE" and value.startswith("/dev/nvidia"):
            descriptors.append(value)
    return {"scope": "container spark process native library mappings/device descriptors only", "spark_container_pids": sorted(pids), "loaded_library_paths": sorted(libraries), "nvidia_device_descriptors": sorted(descriptors), "gpu_request_execution": "NOT_ESTABLISHED_BY_THIS_OBSERVATION"}


def verify_runtime(release, instance, desired_compose):
    normalized = normalize_compose(desired_compose)
    services = normalized.get("services", {})
    if set(services) != {"engine"} or len(release.get("components", [])) != 1:
        raise ValueError("single engine runtime declaration required")
    component, service = release["components"][0], engine_literals(services["engine"])
    if service.get("container_name") != instance["container_name"] + "-engine":
        raise ValueError("runtime identity mismatch fields: declared_container_name")
    name = service["container_name"]
    actual = json.loads(docker_read(["container", "inspect", name]))
    if not isinstance(actual, list) or len(actual) != 1:
        raise ValueError("ambiguous container identity")
    actual = actual[0]
    network_names = [normalized.get("networks", {}).get(key, {}).get("name", instance["project_name"] + "_" + key) for key in service.get("networks", [])]
    checks = compare_container(actual, component, service, network_names)
    image = json.loads(docker_read(["image", "inspect", actual["Image"]]))[0]
    if image.get("Id") != component["image_id"] or image.get("Os") != "linux" or image.get("Architecture") != "arm64":
        raise ValueError("runtime identity mismatch fields: image_platform")
    if image.get("Size") != component["size_bytes"]:
        raise ValueError("runtime identity mismatch fields: image_size")
    compare_complete_environment(actual.get("Config", {}), image.get("Config", {}), service)
    files = image_files(actual["Id"], component["image_assets"])
    observations = loaded_native_observations(actual["Id"])
    return {"schema_version": "atlas-runtime-identity-v1", "status": "PASS", "container_id": actual["Id"], "container_name": name, "image_ref": component["image_ref"], "image_id": actual["Image"], "platform": "linux/arm64", "started_at": actual["State"]["StartedAt"], "host_pid": actual["State"]["Pid"], "command_sha256": canonical_hash(actual["Config"].get("Cmd")), "desired_compose_sha256": canonical_hash(desired_compose), "normalized_compose_sha256": canonical_hash(normalized), "checked_fields": checks + ["image_platform", "image_size", "complete_environment", "image_assets"], "expected_environment_count": len(environment(service.get("environment"))), "image_assets": files, "native_loaded_observations": observations, "scope": "declared runtime identity and installed file consistency; real inference/GPU execution qualified separately"}
