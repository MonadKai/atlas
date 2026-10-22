# SPDX-License-Identifier: AGPL-3.0-only
"""Linux/cgroup/NVML observations with explicitly separate memory abstractions."""
from datetime import datetime, timezone
from pathlib import Path
import re
import math
import urllib.error
import urllib.parse
import urllib.request
import subprocess
import time


def parse_meminfo(text):
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([A-Za-z_()0-9]+):\s+([0-9]+)(?: (kB))?", line)
        if not match or match[1] in result: raise ValueError("invalid or duplicate Linux meminfo field")
        result[match[1]] = int(match[2]) * (1024 if match[3] else 1)
    return result


def parse_key_values(text):
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[0] in result or not parts[1].isdecimal(): raise ValueError("invalid or duplicate kernel key/value field")
        result[parts[0]] = int(parts[1])
    return result


METRICS = {"atlas_requests_active", "atlas_requests_total", "atlas_generation_tokens_total", "atlas_prompt_tokens_total",
           "atlas_decoded_tokens_total", "atlas_prefix_cache_hit_tokens_total", "atlas_kernel_lookups_unresolved", "atlas_requests_queued"}


def parse_metrics(text):
    values, lines = {}, []
    for line in text.splitlines():
        name = line.split(" ", 1)[0]
        if name not in METRICS: continue
        parts = line.split()
        if len(parts) != 2 or name in values: raise ValueError("invalid or duplicate allowlisted metric")
        value = float(parts[1])
        if not math.isfinite(value) or value < 0: raise ValueError("invalid allowlisted metric value")
        values[name] = value; lines.append(line)
    return {"values": values, "allowlisted_raw_lines": lines,
            "active_requests": {"status": "OBSERVED", "value": values["atlas_requests_active"]} if "atlas_requests_active" in values else {"status": "NOT_EXPORTED"},
            "queue_depth": {"status": "OBSERVED", "value": values["atlas_requests_queued"]} if "atlas_requests_queued" in values else {"status": "NOT_EXPORTED", "reason": "no queue gauge exported by this version; active requests is not queue depth"}}


def resource_summary(raw):
    return {"linux": raw["linux_meminfo"], "containers": raw["containers"], "nvml_report": raw["nvidia_smi"],
            "cuda_driver_memory": {"status": "NOT_OBSERVED", "reason": "cuMemGetInfo is context-dependent; this default observer does not create a CUDA context"},
            "atlas_allocator": {"status": "NOT_OBSERVED", "reason": "requires separately timestamped Atlas allocator/log evidence; co-tenant/page-cache residual is not Linux Cached"},
            "interpretation": "Linux available/free/cache, cgroup accounting, process RSS, NVML and Atlas allocation abstractions are distinct; do not subtract them to invent causal attribution"}


class ResourceIORouter:
    def read(self, path):
        return Path(path).read_text()

    def optional(self, path):
        try: return {"status": "OBSERVED", "text": self.read(path)}
        except OSError as error: return {"status": "UNAVAILABLE", "reason": type(error).__name__}

    def command(self, argv):
        try:
            result = subprocess.run(argv, text=True, capture_output=True, timeout=15)
            return {"status": "OBSERVED" if result.returncode == 0 else "UNAVAILABLE", "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"status": "UNAVAILABLE", "reason": type(error).__name__}

    def metrics(self, url):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password or parsed.path != "/metrics" or parsed.query or parsed.fragment:
            raise ValueError("explicit credential-free HTTP /metrics URL required")
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=2) as response:
                if response.status != 200 or response.url != url: raise ValueError("metrics response/redirect rejected")
                body = response.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024: raise ValueError("metrics body exceeds bounded observation")
            return {"status": "OBSERVED", "endpoint": url, **parse_metrics(body.decode())}
        except (OSError, ValueError, urllib.error.URLError) as error:
            return {"status": "UNAVAILABLE", "reason": type(error).__name__}

    def container(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{64}", identifier): raise ValueError("full container ID required")
        # Read only selected non-secret properties, never environment or argv.
        value = self.command(["docker", "container", "inspect", "--format", '{"state":{{json .State}},"restart_count":{{.RestartCount}}}', identifier])
        import json
        if value["status"] != "OBSERVED": return {"container_id": identifier, "status": "UNAVAILABLE", "evidence": value}
        envelope = json.loads(value["stdout"])
        state = envelope["state"]
        restarts = envelope["restart_count"]
        if type(restarts) is not int or restarts < 0: raise ValueError("explicit Docker restart count required")
        pid = state.get("Pid")
        if type(pid) is not int or pid < 1:
            return {"container_id": identifier, "status": "NOT_RUNNING", "restart_count": restarts, "state": {k: state.get(k) for k in ("Status", "Running", "Restarting", "OOMKilled", "ExitCode", "StartedAt", "FinishedAt")}}
        try:
            return {**self.running_container(identifier, state, pid), "restart_count": restarts}
        except (OSError, ValueError) as error:
            return {"container_id": identifier, "status": "UNAVAILABLE", "host_pid": pid, "restart_count": restarts, "reason": type(error).__name__,
                    "scope": "selected process/cgroup observation failed or raced process exit; no zero/OOM absence inferred"}

    def running_container(self, identifier, state, pid):
        cgroup = self.read(f"/proc/{pid}/cgroup")
        unified = [line.removeprefix("0::") for line in cgroup.splitlines() if line.startswith("0::")]
        if len(unified) != 1 or not unified[0].startswith("/") or ".." in Path(unified[0]).parts: raise ValueError("explicit cgroup v2 process path required")
        root = Path("/sys/fs/cgroup") / unified[0].lstrip("/")
        files = {name: self.optional(root / name) for name in ("cgroup.procs", "memory.current", "memory.peak", "memory.max", "memory.high", "memory.swap.current", "memory.swap.max", "memory.events", "memory.events.local", "memory.stat", "memory.pressure", "cpu.stat", "cpu.max", "cpu.pressure", "io.pressure", "pids.current")}
        status = self.read(f"/proc/{pid}/status")
        rss = {}
        for line in status.splitlines():
            if line.split(":", 1)[0] in {"VmRSS", "VmHWM", "RssAnon", "RssFile", "RssShmem", "VmSwap", "Threads"}: rss.update(parse_meminfo(line))
        processes = []
        members = files["cgroup.procs"]
        if members["status"] == "OBSERVED":
            identifiers = members["text"].splitlines()
            if len(identifiers) > 4096 or any(not member.isdecimal() for member in identifiers):
                raise ValueError("invalid or excessive cgroup process membership")
            for member in identifiers:
                try:
                    text = self.read(f"/proc/{member}/status")
                    memory = {}
                    for line in text.splitlines():
                        if line.split(":", 1)[0] in {"VmRSS", "VmHWM", "RssAnon", "RssFile", "RssShmem", "VmSwap", "Threads"}: memory.update(parse_meminfo(line))
                    processes.append({"pid": int(member), "status": "OBSERVED", "comm": self.read(f"/proc/{member}/comm").strip(), "memory": memory})
                except OSError as error:
                    processes.append({"pid": int(member), "status": "UNAVAILABLE", "reason": type(error).__name__})
        return {"container_id": identifier, "status": "OBSERVED", "host_pid": pid, "cgroup_v2_path": str(root), "process_status": rss, "member_processes": processes,
                "state": {k: state.get(k) for k in ("Status", "Running", "Restarting", "OOMKilled", "ExitCode", "StartedAt", "FinishedAt")}, "cgroup_files": files,
                "scope": "main init PID plus individually observed cgroup.procs member RSS; RSS is not summed because shared pages would double-count; cgroup memory includes all members and accounted cache"}


def observe_resources(container_ids, router, metrics_url):
    started = datetime.now(timezone.utc).isoformat()
    monotonic = time.monotonic()
    raw = {"linux_meminfo": parse_meminfo(router.read("/proc/meminfo")),
           "linux_cpu_stat": router.read("/proc/stat"), "linux_vmstat": parse_key_values(router.read("/proc/vmstat")),
           "linux_loadavg": router.read("/proc/loadavg"), "pressure": {kind: router.optional("/proc/pressure/" + kind) for kind in ("cpu", "memory", "io")},
           "containers": [router.container(identifier) for identifier in container_ids],
           "atlas_metrics": router.metrics(metrics_url) if metrics_url is not None else {"status": "NOT_REQUESTED"},
           "nvidia_smi": router.command(["nvidia-smi", "--query-gpu=index,uuid,name,driver_version,memory.total,memory.used,memory.free,utilization.gpu,utilization.memory", "--format=csv,noheader,nounits"])}
    return {"schema_version": "atlas-resource-observation-v1", "started_at": started, "ended_at": datetime.now(timezone.utc).isoformat(),
            "collection_seconds": time.monotonic() - monotonic, "summary": resource_summary(raw), "raw": raw,
            "scope": "single sequential read-only snapshot; fields collected within stated interval, not exactly simultaneous; no CUDA context, no cache flush, no lifecycle operations"}
