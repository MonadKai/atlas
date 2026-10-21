# SPDX-License-Identifier: AGPL-3.0-only
"""Pure Docker contract comparison, separate from actual lifecycle evidence."""
import importlib.util
from pathlib import Path
import unittest
import copy
import json
import shutil
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("atlas_runtime_identity", ROOT / "deploy/release/runtime_identity.py")
RUNTIME = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNTIME)

class RuntimeTests(unittest.TestCase):
    def fixture(self):
        profile = json.loads((ROOT / "deploy/release/profiles/gb10-qwen36-nvfp4.json").read_text())
        service = copy.deepcopy(profile["runtime"]["compose"]["services"]["engine"])
        service.update(container_name="test-engine", image="private@sha256:" + "a" * 64, user="1002:1002", cpus="4", cpuset="0-3", mem_limit=68719476736, memswap_limit=68719476736, ports=[{"target": 8888, "published": "18836", "host_ip": "127.0.0.1", "protocol": "tcp"}], labels={"org.atlas.product": "product:atlas-qwen36-nvfp4"})
        service["volumes"][0]["source"] = "/data/models"
        service["volumes"][1]["source"] = "/data/cache"
        component = {"id": "engine", "image_ref": service["image"], "image_id": "sha256:" + "b" * 64}
        actual = {
            "Name": "/test-engine", "Image": component["image_id"],
            "Config": {"Image": component["image_ref"], "Cmd": service["command"], "Entrypoint": ["/usr/local/bin/spark"], "User": "1002:1002", "StopTimeout": 120, "Env": [key + "=" + value for key, value in service["environment"].items()], "Labels": service["labels"], "Healthcheck": {"Test": service["healthcheck"]["test"], "Interval": 30000000000, "Timeout": 5000000000, "StartPeriod": 480000000000, "Retries": 3}},
            "State": {"Running": True, "Paused": False, "Restarting": False, "Health": {"Status": "healthy"}},
            "HostConfig": {"ReadonlyRootfs": True, "Init": True, "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}, "CpusetCpus": "0-3", "NanoCpus": 4000000000, "CpuQuota": 0, "CpuPeriod": 0, "Memory": 68719476736, "MemorySwap": 68719476736, "MemoryReservation": 57982058496, "ShmSize": 67108864, "PortBindings": {"8888/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18836"}]}, "Tmpfs": {"/tmp": "size=268435456,mode=1777"}, "CapDrop": ["ALL"], "CapAdd": None, "SecurityOpt": ["no-new-privileges:true"], "Privileged": False, "Devices": [], "DeviceRequests": [{"Driver": "nvidia", "Count": 0, "DeviceIDs": ["0"], "Capabilities": [["gpu"]], "Options": {}}], "LogConfig": {"Type": "json-file", "Config": {"max-size": "20m", "max-file": "3"}}, "NetworkMode": "test-service"},
            "Mounts": [{"Type": "bind", "Source": "/data/models", "Destination": "/models/qwen36", "RW": False, "Propagation": "rprivate"}, {"Type": "bind", "Source": "/data/cache", "Destination": "/cache", "RW": True, "Propagation": "rprivate"}],
            "NetworkSettings": {"Networks": {"test-service": {}}},
        }
        return copy.deepcopy(actual), copy.deepcopy(component), copy.deepcopy(service)

    def test_wrong_configuration_reports_field_only(self):
        config, component, service = self.fixture()
        config["Image"] = "sensitive-wrong-config-id"
        with self.assertRaisesRegex(ValueError, "image_config_id"):
            RUNTIME.compare_container(config, component, service)

    def test_complete_declared_runtime_matches(self):
        actual, component, service = self.fixture()
        checks = RUNTIME.compare_container(actual, component, service, ["test-service"])
        self.assertGreaterEqual(len(checks), 30)
        self.assertIn("expected_environment", checks)
        self.assertIn("network_membership", checks)

    def test_field_mutations_fail_closed_without_values(self):
        cases = [
            (("Config", "Image"), "immutable_image_reference", "wrong-ref"),
            (("Config", "Cmd"), "command", ["sensitive-argv"]),
            (("Config", "Entrypoint"), "entrypoint", ["/wrong"]),
            (("Config", "User"), "user", "0:0"),
            (("Config", "StopTimeout"), "stop_grace", 10),
            (("Config", "Env"), "expected_environment", ["SECRET=sensitive-env-value"]),
            (("Config", "Labels"), "expected_labels", {"secret": "sensitive-label-value"}),
            (("State", "Running"), "running", False),
            (("State", "Health"), "healthcheck_status", {"Status": "unhealthy"}),
            (("HostConfig", "ReadonlyRootfs"), "readonly_rootfs", False),
            (("HostConfig", "Init"), "init", False),
            (("HostConfig", "RestartPolicy"), "restart_policy", {"Name": "always", "MaximumRetryCount": 0}),
            (("HostConfig", "CpusetCpus"), "cpuset", "4-7"),
            (("HostConfig", "NanoCpus"), "cpu_limit", 8000000000),
            (("HostConfig", "CpuQuota"), "cpu_quota_not_overridden", 800000),
            (("HostConfig", "Memory"), "memory_limit", 0),
            (("HostConfig", "MemorySwap"), "memory_swap_limit", -1),
            (("HostConfig", "MemoryReservation"), "memory_reservation", 0),
            (("HostConfig", "ShmSize"), "shared_memory", 0),
            (("HostConfig", "PortBindings"), "port_bindings", {"8888/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18836"}]}),
            (("HostConfig", "Tmpfs"), "tmpfs", {"/tmp": "size=999"}),
            (("HostConfig", "CapDrop"), "cap_drop", []),
            (("HostConfig", "CapAdd"), "cap_add", ["SYS_ADMIN"]),
            (("HostConfig", "SecurityOpt"), "security_options", []),
            (("HostConfig", "Privileged"), "privileged", True),
            (("HostConfig", "DeviceRequests"), "device_requests", []),
            (("HostConfig", "Devices"), "additional_devices", [{"PathOnHost": "/dev/other"}]),
            (("HostConfig", "LogConfig"), "logging", {"Type": "none", "Config": {}}),
            (("HostConfig", "NetworkMode"), "network_mode", "host"),
        ]
        for (section, key), field, value in cases:
            with self.subTest(field=field):
                actual, component, service = self.fixture()
                actual[section][key] = value
                with self.assertRaisesRegex(ValueError, field) as error:
                    RUNTIME.compare_container(actual, component, service, ["test-service"])
                self.assertNotIn("sensitive", str(error.exception))

    def test_mount_access_mismatch_rejected(self):
        actual, component, service = self.fixture()
        actual["Mounts"][0]["RW"] = True
        with self.assertRaisesRegex(ValueError, "bind_mounts"):
            RUNTIME.compare_container(actual, component, service)

    def test_health_configuration_mismatch_rejected(self):
        actual, component, service = self.fixture()
        actual["Config"]["Healthcheck"]["StartPeriod"] = 1
        with self.assertRaisesRegex(ValueError, "healthcheck_configuration"):
            RUNTIME.compare_container(actual, component, service)

    def test_extra_network_rejected(self):
        actual, component, service = self.fixture()
        actual["NetworkSettings"]["Networks"]["foreign-network"] = {}
        with self.assertRaisesRegex(ValueError, "network_membership"):
            RUNTIME.compare_container(actual, component, service, ["test-service"])

    def test_binary_units_and_duration_normalization(self):
        self.assertEqual(RUNTIME.byte_size("64g"), 68719476736)
        self.assertEqual(RUNTIME.byte_size("54GiB"), 57982058496)
        self.assertEqual(RUNTIME.nanoseconds("2m"), 120000000000)
        self.assertEqual(RUNTIME.nanoseconds("1m30s"), 90000000000)
        for value in ("invalid", "10garbage"):
            with self.assertRaises(ValueError):
                RUNTIME.nanoseconds(value)

    def test_duplicate_environment_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            RUNTIME.environment(["KEY=one", "KEY=two"])

    def test_complete_image_environment_and_declared_override(self):
        image = {"Env": ["BASE=fixed", "OVERRIDE=original"]}
        service = {"environment": {"OVERRIDE": "declared"}}
        RUNTIME.compare_complete_environment({"Env": ["BASE=fixed", "OVERRIDE=declared"]}, image, service)
        with self.assertRaisesRegex(ValueError, "complete_environment") as error:
            RUNTIME.compare_complete_environment({"Env": ["BASE=fixed", "OVERRIDE=declared", "EXTRA=sensitive-value"]}, image, service)
        self.assertNotIn("sensitive", str(error.exception))

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose parser not installed")
    def test_real_readonly_compose_normalization_of_ports_and_literals(self):
        desired = {"name": "atlas-readonly-contract-test", "services": {"engine": {"image": "ubuntu:24.04", "ports": ["127.0.0.1:18836:8888"], "healthcheck": {"test": ["CMD-SHELL", 'test -n "$$VALUE"']}, "environment": {"LITERAL": "$$VALUE"}, "tmpfs": ["/tmp:size=268435456,mode=1777"]}}}
        normalized = RUNTIME.normalize_compose(desired)
        service = RUNTIME.engine_literals(normalized["services"]["engine"])
        self.assertEqual(RUNTIME.port_bindings(service), {"8888/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18836"}]})
        self.assertEqual(service["healthcheck"]["test"][1], 'test -n "$VALUE"')
        self.assertEqual(service["environment"]["LITERAL"], "$VALUE")
        self.assertIn("$$VALUE", desired["services"]["engine"]["healthcheck"]["test"][1])

    def test_declared_health_printf_emits_real_http_crlf(self):
        profile = json.loads((ROOT / "deploy/release/profiles/gb10-qwen36-nvfp4.json").read_text())
        script = profile["runtime"]["compose"]["services"]["engine"]["healthcheck"]["test"][3]
        match = re.search(r"printf '([^']*)'", script)
        self.assertIsNotNone(match)
        # Execute only the extracted fixed printf format, never the socket or
        # arbitrary declared script, and pass it as data rather than shell code.
        result = subprocess.run(["/bin/bash", "-c", 'printf "$1"', "atlas-health-format", match[1]], capture_output=True, check=True)
        self.assertEqual(result.stdout, b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        self.assertNotIn(b"\\r", result.stdout)

if __name__ == "__main__":
    unittest.main()
