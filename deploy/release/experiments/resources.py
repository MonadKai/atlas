# SPDX-License-Identifier: AGPL-3.0-only
"""Pure normalization of observed resources; never infers missing counters or RSS."""
from datetime import datetime, timedelta
import re
import json

from acceptance_cases import require
from inspection.resources import parse_key_values, parse_metrics
from .model import canonical_sha256, number


def integer(value, name):
    require(type(value) is int and value >= 0, "explicit nonnegative integer required: " + name)
    return value


def utc(value):
    require(isinstance(value, str) and value, "explicit UTC observation timestamp required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid UTC timestamp") from error
    require(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0), "timestamp must be UTC")
    return parsed


def cgroup_text(container, name):
    files = container.get("cgroup_files")
    require(isinstance(files, dict) and isinstance(files.get(name), dict), "missing observed cgroup file: " + name)
    field = files[name]
    require(field.get("status") == "OBSERVED" and isinstance(field.get("text"), str), "unavailable cgroup file: " + name)
    return field["text"]


def normalize(raw_rows, container_id, process_comm, oom_counter):
    """Select one actual process, identity and counter; return resource-trend rows.

    This transforms observations only. It never judges duration, stability or a
    growth threshold, sums process RSS, or turns NVML into CUDA memory values.
    """
    require(isinstance(container_id, str) and re.fullmatch(r"[0-9a-f]{64}", container_id), "full selected container ID required")
    require(isinstance(process_comm, str) and process_comm and len(process_comm.encode()) <= 15 and
            not any(char in process_comm for char in "\x00\r\n"), "explicit exact Linux process comm required")
    require(oom_counter in {"oom", "oom_kill", "oom_group_kill"}, "explicit supported memory.events OOM counter required")
    require(isinstance(raw_rows, list) and 0 < len(raw_rows) <= 10000, "nonempty bounded raw observation list required")
    result, origin, previous, fixed = [], None, None, None
    previous_index, previous_restarts, previous_oom = None, None, None
    for sample in raw_rows:
        require(isinstance(sample, dict) and sample.get("schema_version") == "atlas-resource-observation-v1", "unsupported resource observation")
        start, end = utc(sample.get("started_at")), utc(sample.get("ended_at"))
        require(end >= start, "resource collection window runs backwards")
        collection = number(sample.get("collection_seconds"), "collection_seconds")
        index = integer(sample.get("sample_index"), "sample_index")
        require(previous is None or start > previous, "resource start timestamps must strictly increase")
        require(previous_index is None or index > previous_index, "resource sample indices must strictly increase")
        raw = sample.get("raw")
        require(isinstance(raw, dict) and isinstance(raw.get("containers"), list), "raw container observations required")
        matches = [c for c in raw["containers"] if isinstance(c, dict) and c.get("container_id") == container_id]
        require(len(matches) == 1 and matches[0].get("status") == "OBSERVED", "selected container missing, ambiguous or unobserved")
        container = matches[0]
        state = container.get("state")
        require(isinstance(state, dict) and state.get("Running") is True and state.get("Restarting") is False
                and type(state.get("OOMKilled")) is bool, "selected container state is missing or not running")
        started_at = state.get("StartedAt")
        require(utc(started_at) <= end, "container StartedAt lies after its observation")
        restarts = integer(container.get("restart_count"), "restart_count")
        members = container.get("member_processes")
        require(isinstance(members, list) and members and all(isinstance(m, dict) and m.get("status") == "OBSERVED" for m in members),
                "complete observed process membership required")
        pids = [integer(m.get("pid"), "member PID") for m in members]
        require(all(pid > 0 for pid in pids) and len(set(pids)) == len(pids), "unique positive member PIDs required")
        processes = [m for m in members if m.get("comm") == process_comm]
        require(len(processes) == 1, "selected process comm missing or ambiguous")
        process = processes[0]
        memory = process.get("memory")
        require(isinstance(memory, dict), "actual selected process RSS required")
        rss = integer(memory.get("VmRSS"), "selected process VmRSS")
        membership = cgroup_text(container, "cgroup.procs").splitlines()
        require(membership and all(p.isdecimal() for p in membership) and len(set(membership)) == len(membership), "invalid cgroup membership")
        require(set(pids) == {int(pid) for pid in membership}, "process observations do not cover exact cgroup membership")
        require(str(process["pid"]) in membership, "selected process absent from observed cgroup membership")
        identity = (container_id, started_at, process["pid"])
        require(fixed is None or identity == fixed, "container/StartedAt/selected process identity drift")
        current = cgroup_text(container, "memory.current").strip()
        require(current.isdecimal(), "actual numeric memory.current required")
        events = parse_key_values(cgroup_text(container, "memory.events"))
        require(oom_counter in events, "selected OOM counter not observed; zero cannot be inferred")
        oom = events[oom_counter]
        require(previous_restarts is None or restarts >= previous_restarts, "restart counter reset")
        require(previous_oom is None or oom >= previous_oom, "selected OOM counter reset")
        metrics = raw.get("atlas_metrics")
        require(isinstance(metrics, dict) and metrics.get("status") == "OBSERVED", "Atlas metrics unavailable")
        values, active = metrics.get("values"), metrics.get("active_requests")
        require(isinstance(values, dict) and isinstance(active, dict) and active.get("status") == "OBSERVED", "active request gauge unavailable")
        gauge = number(values.get("atlas_requests_active"), "observed active requests")
        require(float(gauge).is_integer() and active.get("value") == gauge, "active request gauge inconsistent")
        lines = metrics.get("allowlisted_raw_lines")
        require(isinstance(lines, list) and all(isinstance(line, str) for line in lines), "raw allowlisted gauge evidence required")
        parsed = parse_metrics("\n".join(lines))
        require(parsed["values"].get("atlas_requests_active") == gauge, "raw active gauge differs or missing")
        origin = start if origin is None else origin
        result.append({"elapsed_seconds": (start - origin).total_seconds(), "rss_bytes": rss,
            "cgroup_current_bytes": int(current), "restart_count": restarts, "oom_events": oom,
            "oom_counter": oom_counter, "active_requests": gauge, "container_id": container_id,
            "container_started_at": started_at, "process_comm": process_comm, "process_pid": process["pid"],
            "oom_killed_observed": state["OOMKilled"], "sample_index": index,
            "observation_started_at": sample["started_at"], "observation_ended_at": sample["ended_at"],
            "collection_seconds": collection, "elapsed_origin_utc": raw_rows[0]["started_at"],
            "raw_sample_sha256": canonical_sha256(sample),
            "scope": "one exact process VmRSS, no RSS sum or init substitution; actual selected memory.events counter; no CUDA/allocator inference or duration qualification"})
        previous, previous_index, previous_restarts, previous_oom, fixed = start, index, restarts, oom, identity
    return result


def parse_observations(text):
    """Decode a bounded complete JSONL snapshot; duplicate/nonfinite input fails."""
    require(isinstance(text, str) and text and text.endswith("\n"), "complete newline-terminated observation snapshot required")
    lines = text.splitlines()
    require(0 < len(lines) <= 10000 and all(line.strip() for line in lines), "nonempty bounded JSONL rows required")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "duplicate JSON observation key")
            value[key] = item
        return value
    def invalid(value):
        raise ValueError("nonfinite JSON observation: " + value)
    return [json.loads(line, object_pairs_hook=unique, parse_constant=invalid) for line in lines]
