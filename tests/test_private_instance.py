# SPDX-License-Identifier: AGPL-3.0-only
"""Pure rendering and ownership seams; no Docker or GPU lifecycle simulation."""
import copy
from contextlib import ExitStack
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

RELEASE = Path(__file__).resolve().parents[1] / "deploy/release"
sys.path.insert(0, str(RELEASE))
import instance
from release_contract import PRODUCT, canonical


class PrivateInstanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model, self.cache = self.root / "model", self.root / "cache"
        self.model.mkdir()
        self.cache.mkdir()
        self.capability = json.loads((RELEASE / "profiles/gb10-qwen36-nvfp4.json").read_text())
        self.release = {"components": [{"id": "engine", "image_ref": "registry.100credit.cn/xdynamic/atlas@sha256:" + "1" * 64}]}
        self.value = {"schema_version": "service-instance-v5", "release_scope": "development",
            "project_name": "atlas-test", "container_name": "atlas-test", "host_ip": "127.0.0.1", "host_port": 18888,
            "manager": "xd-model-deploy-skills", "binding_id": "1" * 32,
            "components": {"engine": {"cpuset": "0-3", "cpu_limit_millicores": 4000, "memory_limit_bytes": 68719476736}},
            "uid": os.getuid(), "gid": os.getgid(), "model_root": str(self.model), "cache_root": str(self.cache),
            "model_alias": "qwen3-6-35b-a3b"}

    def render(self):
        return instance.render_contract(self.release, self.capability, self.value)

    def snapshot(self, desired):
        records = []
        for service in desired["services"].values():
            records.append({"kind": "container", "name": service["container_name"], "state": "absent"})
        for kind, section in [("network", "networks"), ("volume", "volumes")]:
            records.extend({"kind": kind, "name": resource["name"], "state": "absent"} for resource in desired.get(section, {}).values())
        return {"schema_version": "owned-resource-snapshot-v1", "execution_plane": "docker", "records": records}

    def test_full_runtime_template_is_preserved_with_explicit_site_values(self):
        original = copy.deepcopy(self.capability)
        desired = self.render()
        engine = desired["services"]["engine"]
        self.assertEqual(engine["container_name"], "atlas-test-engine")
        self.assertEqual(engine["image"], self.release["components"][0]["image_ref"])
        self.assertEqual(engine["ports"], ["127.0.0.1:18888:8888"])
        self.assertEqual(engine["user"], f"{os.getuid()}:{os.getgid()}")
        self.assertEqual(engine["cpuset"], "0-3")
        self.assertEqual(engine["cpus"], 4)
        self.assertEqual(engine["mem_limit"], 68719476736)
        self.assertEqual(engine["memswap_limit"], engine["mem_limit"])
        self.assertEqual(engine["mem_reservation"], 57982058496)
        self.assertEqual(engine["command"], [self.value["model_alias"] if x == "${instance:model_alias}" else x
            for x in original["runtime"]["compose"]["services"]["engine"]["command"]])
        for key in ["environment", "healthcheck", "entrypoint", "tmpfs", "cap_drop", "security_opt", "stop_grace_period"]:
            self.assertEqual(engine[key], original["runtime"]["compose"]["services"]["engine"][key])
        expected_deploy = copy.deepcopy(original["runtime"]["compose"]["services"]["engine"]["deploy"])
        expected_deploy["resources"]["reservations"]["memory"] = str(engine["mem_reservation"])
        self.assertEqual(engine["deploy"], expected_deploy)
        self.assertEqual(engine["volumes"][0]["source"], str(self.model))
        self.assertTrue(engine["volumes"][0]["read_only"])
        self.assertEqual(engine["volumes"][1]["source"], str(self.cache))
        self.assertEqual(engine["networks"], ["service"])
        self.assertEqual(desired["networks"]["service"]["name"], "atlas-test-service")
        self.assertEqual(engine["labels"]["org.atlas.instance-sha256"], canonical(self.value))
        self.assertEqual(self.capability, original)

    def test_render_contract_has_no_filesystem_or_process_identity_io(self):
        value = {**self.value, "model_root": "/unavailable/model", "cache_root": "/unavailable/cache"}
        with ExitStack() as stack:
            for method in ["stat", "is_dir", "is_symlink", "exists", "resolve", "open", "read_bytes"]:
                stack.enter_context(patch.object(Path, method, side_effect=AssertionError("pure renderer performed filesystem IO")))
            for method in ["access", "geteuid", "getuid", "getgid", "stat", "lstat"]:
                stack.enter_context(patch.object(os, method, side_effect=AssertionError("pure renderer observed host process or filesystem")))
            stack.enter_context(patch("builtins.open", side_effect=AssertionError("pure renderer opened a file")))
            desired = instance.render_contract(self.release, self.capability, value)
        self.assertEqual(desired["services"]["engine"]["volumes"][0]["source"], value["model_root"])

    def test_gpu_deploy_reservation_derives_from_single_memory_budget(self):
        self.capability["runtime"]["compose"]["services"]["engine"]["mem_reservation"] = 60 * 1024**3
        engine = self.render()["services"]["engine"]
        self.assertEqual(engine["deploy"]["resources"]["reservations"]["memory"], str(engine["mem_reservation"]))
        self.assertEqual(engine["mem_reservation"], 60 * 1024**3)
        self.capability["runtime"]["compose"]["services"]["engine"]["mem_reservation"] = 65 * 1024**3
        with self.assertRaisesRegex(ValueError, "reservation exceeds"):
            self.render()

    def test_standard_compose_schema_readonly_when_available(self):
        if shutil.which("docker") is None:
            self.skipTest("Docker CLI unavailable; independent readonly Compose schema check not run")
        probe = subprocess.run(["docker", "compose", "version"], text=True, capture_output=True)
        if probe.returncode:
            self.skipTest("Compose plugin unavailable; independent schema check not run")
        desired = self.render()
        path = self.root/"compose.json"
        path.write_text(json.dumps(desired))
        result = subprocess.run(["docker", "compose", "-f", str(path), "config", "--format", "json"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        engine = json.loads(result.stdout)["services"]["engine"]
        self.assertEqual(int(engine["mem_reservation"]), int(engine["deploy"]["resources"]["reservations"]["memory"]))
        self.assertEqual(int(engine["mem_limit"]), int(engine["memswap_limit"]))
        self.assertEqual(engine["deploy"]["resources"]["reservations"]["devices"][0]["device_ids"], ["0"])
        self.assertNotIn('variable is not set', result.stderr)

    def test_schema_identity_alias_resource_and_loopback_are_fail_closed(self):
        bad = [{"schema_version": "atlas-instance-v1"}, {"release_scope": "production"}, {"uid": 0}, {"gid": 0},
               {"uid": True}, {"host_ip": "0.0.0.0"}, {"host_port": 65536}, {"model_alias": "different-model"},
               {"project_name": "../escape"}, {"container_name": "foreign/name"}, {"manager": ""}, {"binding_id": ""},
               {"extra": "undeclared"}]
        for patch in bad:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                instance.render_contract(self.release, self.capability, {**self.value, **patch})
        for limits in [{"cpuset": "0-3,2"}, {"cpuset": "3-0"}, {"cpu_limit_millicores": 3000},
                       {"cpu_limit_millicores": True}, {"memory_limit_bytes": 68719476735}]:
            value = copy.deepcopy(self.value)
            value["components"]["engine"].update(limits)
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                instance.render_contract(self.release, self.capability, value)
        value = copy.deepcopy(self.value)
        value["components"]["foreign"] = value["components"]["engine"]
        with self.assertRaises(ValueError):
            instance.render_contract(self.release, self.capability, value)

    def path_preflight(self, value):
        instance.validate_instance(value, self.capability)
        return instance.validate_path_observations(value, instance.observe_paths(value))

    def test_path_boundary_preserves_existing_real_owned_writable_policy(self):
        self.assertEqual(self.path_preflight(self.value)["paths"]["cache_root"]["owner_uid"], self.value["uid"])
        for change in [{"cache_root": str(self.root / "absent")}, {"model_root": "relative"},
                       {"cache_root": str(self.model)}, {"cache_root": str(self.model / "child")}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.path_preflight({**self.value, **change})
        link = self.root / "link"
        link.symlink_to(self.cache, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.path_preflight({**self.value, "cache_root": str(link)})
        self.cache.chmod(0o500)
        with self.assertRaises(ValueError):
            self.path_preflight(self.value)
        self.cache.chmod(0o700)
        with self.assertRaises(ValueError):
            self.path_preflight({**self.value, "uid": os.getuid()+1})
        self.assertFalse((self.root / "absent").exists())

    def test_path_policy_is_pure_and_rejects_incomplete_or_mismatched_observations(self):
        observation = instance.observe_paths(self.value)
        with patch.object(Path, "stat", side_effect=AssertionError("policy performed IO")), patch.object(os, "access", side_effect=AssertionError("policy performed IO")):
            self.assertEqual(instance.validate_path_observations(self.value, observation), observation)
        for key, wrong in [("has_symlink", True), ("is_directory", False), ("owner_uid", self.value["uid"]+1),
                           ("mode", 0), ("writable_by_effective_uid", False), ("path", "/different/cache")]:
            changed = copy.deepcopy(observation)
            changed["paths"]["cache_root"][key] = wrong
            with self.subTest(key=key), self.assertRaises(ValueError):
                instance.validate_path_observations(self.value, changed)
        wrong = copy.deepcopy(observation)
        wrong["paths"].pop("model_root")
        with self.assertRaises(ValueError):
            instance.validate_path_observations(self.value, wrong)
        with self.assertRaises(ValueError):
            instance.validate_path_observations({**self.value, "binding_id": "different"}, observation)

    def test_guard_requires_complete_absence_or_matching_owned_objects(self):
        desired = self.render()
        snapshot = self.snapshot(desired)
        result = instance.guard(self.value, desired, snapshot)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["instance_sha256"], canonical(self.value))
        self.model.rmdir()
        self.cache.rmdir()
        self.assertEqual(instance.guard(self.value, desired, snapshot)["status"], "PASS")
        with self.assertRaises(ValueError):
            instance.guard(self.value, desired, {**snapshot, "records": snapshot["records"][:-1]})
        duplicate = copy.deepcopy(snapshot)
        duplicate["records"].append(duplicate["records"][0])
        with self.assertRaises(ValueError):
            instance.guard(self.value, desired, duplicate)
        for i, record in enumerate(snapshot["records"]):
            matching = copy.deepcopy(snapshot)
            labels = desired["services"]["engine"]["labels"] if record["kind"] == "container" else desired["networks"]["service"]["labels"]
            obj = {"Name": "/"+record["name"], "Config": {"Labels": labels}} if record["kind"] == "container" else {"Name": record["name"], "Labels": labels}
            matching["records"][i].update(state="present", object=obj)
            self.assertEqual(instance.guard(self.value, desired, matching)["status"], "PASS")
            for key in ["org.atlas.product", "org.atlas.binding-id", "org.atlas.manager"]:
                wrong = copy.deepcopy(matching)
                actual_labels = wrong["records"][i]["object"]["Config"]["Labels"] if record["kind"] == "container" else wrong["records"][i]["object"]["Labels"]
                actual_labels[key] = "foreign"
                with self.subTest(kind=record["kind"], key=key), self.assertRaises(ValueError):
                    instance.guard(self.value, desired, wrong)

    def test_malformed_snapshot_and_unresolved_template_are_rejected(self):
        desired = self.render()
        snapshot = self.snapshot(desired)
        for obj in [{"Name": None}, {"Name": "/atlas-test-engine", "Config": None},
                    {"Name": "/atlas-test-engine", "Config": {"Labels": []}}]:
            wrong = copy.deepcopy(snapshot)
            wrong["records"][0].update(state="present", object=obj)
            with self.subTest(obj=obj), self.assertRaises(ValueError):
                instance.guard(self.value, desired, wrong)
        wrong = copy.deepcopy(snapshot)
        for field in ["kind", "state"]:
            wrong = copy.deepcopy(snapshot)
            wrong["records"][0][field] = []
            with self.subTest(field=field), self.assertRaises(ValueError):
                instance.guard(self.value, desired, wrong)
        wrong_desired = copy.deepcopy(desired)
        wrong_desired["networks"]["service"]["name"] = []
        with self.assertRaises(ValueError):
            instance.guard(self.value, wrong_desired, snapshot)
        self.capability["runtime"]["compose"]["services"]["engine"]["environment"]["UNRESOLVED"] = "${instance:undeclared}"
        with self.assertRaisesRegex(ValueError, "placeholder"):
            self.render()

    def test_guard_covers_neutral_default_network_and_named_volume(self):
        desired = self.render()
        original_binding = "binding:atlas-neutral-test"
        self.value["binding_id"] = hashlib.sha256(original_binding.encode()).hexdigest()[:32]
        desired = self.render()
        neutral = {"com.xd.model-deploy.owner": PRODUCT, "com.xd.model-deploy.binding-id": original_binding}
        desired["networks"]["default"] = {"name": "atlas-test_default", "labels": neutral}
        desired["volumes"] = {"logs": {"name": "atlas-test-logs", "labels": desired["services"]["engine"]["labels"]}}
        snapshot = self.snapshot(desired)
        self.assertEqual(instance.guard(self.value, desired, snapshot)["status"], "PASS")
        external = copy.deepcopy(desired)
        external["networks"]["default"]["external"] = True
        with self.assertRaises(ValueError):
            instance.guard(self.value, external, snapshot)
        snapshot["records"][-1].update(state="present", object={"Name": "atlas-test-logs", "Labels": {}})
        with self.assertRaises(ValueError):
            instance.guard(self.value, desired, snapshot)


if __name__ == "__main__":
    unittest.main()
