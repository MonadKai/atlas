# SPDX-License-Identifier: AGPL-3.0-only
"""Pure experiment declarations, measurements and fail-closed verdicts."""
import hashlib
import json
import math
import statistics
import re

from acceptance_cases import require, validate_error, validate_sse, validate_tool


CASES = frozenset({"short", "context16", "context32", "cache_alpha", "cache_beta",
                   "cancel", "timeout", "invalid", "tool", "mixed_long", "mixed_short"})


def canonical_sha256(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def number(value, name, minimum=0):
    require(type(value) in (int, float) and math.isfinite(value) and value >= minimum,
            f"invalid finite {name}")
    return value


def positive_integer(value, name, maximum):
    require(type(value) is int and 0 < value <= maximum, f"invalid bounded integer {name}")
    return value


def validate_plan(plan):
    require(isinstance(plan, dict) and plan.get("schema_version") == "atlas-request-experiment-v1",
            "unsupported experiment plan")
    require(isinstance(plan.get("name"), str) and plan["name"], "missing experiment name")
    duration = number(plan.get("duration_seconds"), "duration_seconds", .001)
    require(duration <= 86400, "experiment exceeds bounded one-day scope")
    maximum = number(plan.get("max_run_seconds"), "max_run_seconds", duration)
    require(maximum <= 90000, "maximum run budget exceeds 25 hours")
    concurrency = positive_integer(plan.get("max_concurrency"), "max_concurrency", 4)
    timeout = number(plan.get("request_timeout_seconds"), "request_timeout_seconds", .001)
    number(plan.get("recovery_threshold_seconds"), "recovery_threshold_seconds", .001)
    require(timeout <= 300 and maximum >= duration, "invalid request/run budget")
    count_limit = positive_integer(plan.get("maximum_requests"), "maximum_requests", 10000)
    require(plan.get("threshold_scope") == "EXPERIMENT_ONLY_NOT_BUSINESS_SLO",
            "experiment thresholds must not be production promises")
    require(plan.get("schedule_mode") in ("SEQUENTIAL_GROUPS", "WALL_CLOCK"), "explicit schedule mode required")
    thresholds = plan.get("thresholds")
    require(isinstance(thresholds, dict), "missing explicit thresholds")
    for key in ("minimum_successful_requests", "maximum_failures", "ttft_p95_seconds", "e2e_p95_seconds"):
        number(thresholds.get(key), key)
    for case, limits in plan.get("scenario_thresholds", {}).items():
        require(case in CASES and isinstance(limits, dict), "invalid scenario threshold")
        for key, value in limits.items():
            require(key in ("ttft_p95_seconds", "e2e_p95_seconds"), "unsupported scenario threshold")
            number(value, key)
    if "resource_trend" in plan:
        for key in ("warmup_seconds", "median_growth_limit_bytes", "ols_growth_limit_bytes_per_hour"):
            number(plan["resource_trend"].get(key), key)
        number(plan["resource_trend"].get("window_seconds"), "window_seconds", .001)
    schedule = plan.get("schedule")
    require(isinstance(schedule, list) and schedule, "missing bounded schedule")
    requests, previous = 0, 0
    for group in schedule:
        require(isinstance(group, dict), "invalid schedule group")
        at = number(group.get("at_seconds"), "at_seconds")
        require(previous <= at <= duration, "schedule must be ordered and inside duration")
        previous = at
        require(group.get("case") in CASES, "unknown request case")
        repeats = positive_integer(group.get("repeats"), "repeats", 10000)
        parallel = positive_integer(group.get("concurrency"), "concurrency", concurrency)
        require(parallel <= repeats, "concurrency exceeds repeated requests")
        requests += repeats
        if "companion_case" in group:
            require(group["case"] == "mixed_long" and group["companion_case"] == "mixed_short" and
                    parallel == 2, "mixed pair requires two explicit worker slots")
            number(group.get("companion_delay_seconds"), "companion delay")
            require(at + group["companion_delay_seconds"] <= maximum, "companion release exceeds run budget")
            requests += repeats
    require(requests <= count_limit, "request count exceeds explicit bound")
    return plan


def percentiles(values):
    for value in values:
        number(value, "percentile observation")
    ordered = sorted(values)
    result = {"sample_count": len(ordered), "method": "nearest-rank"}
    for key, q in (("p50", .5), ("p95", .95), ("p99", .99)):
        result[key] = ordered[max(0, math.ceil(q * len(ordered)) - 1)] if ordered else None
    result["p99_qualification"] = "OBSERVATION_ONLY" if len(ordered) < 100 else "FINITE_SAMPLE_ESTIMATE"
    return result


def validate_observation(row):
    require(isinstance(row, dict), "invalid observation")
    times = [row.get(k) for k in ("scheduled_seconds", "dispatch_seconds", "start_seconds",
                                  "headers_seconds", "first_content_seconds", "end_seconds")]
    for value in times:
        number(value, "request timing")
    require(times[0] <= times[2] and times[1] <= times[2] and
            times[2:] == sorted(times[2:]), "non-monotonic request boundaries")
    events = row.get("content_event_seconds")
    require(isinstance(events, list) and events and events == sorted(events) and
            times[4] == events[0] and times[4] <= events[-1] <= times[-1], "invalid content event boundaries")
    for value in events:
        number(value, "content event")
    usage = row.get("usage")
    if row.get("termination") == "COMPLETED":
        require(isinstance(usage, dict), "missing completed request usage")
        for key in ("prompt", "completion", "total", "cached"):
            require(type(usage.get(key)) is int and usage[key] >= 0, "invalid usage counts")
        require(usage["prompt"] > 0 and usage["completion"] > 0 and
                usage["total"] == usage["prompt"] + usage["completion"] and
                usage["cached"] <= usage["prompt"], "incoherent usage counts")
    elapsed = times[-1] - times[2]
    decode = events[-1] - events[0]
    return {**row, "client_queue_seconds": times[2] - times[0],
            "worker_queue_seconds": times[2] - times[1], "ttft_seconds": times[4] - times[2],
            "e2e_seconds": elapsed, "scheduled_e2e_seconds": times[-1] - times[0],
            "server_queue_seconds": None, "server_queue_observability": "UNAVAILABLE",
            "content_chunk_intervals_seconds": [b - a for a, b in zip(events, events[1:])],
            "token_intervals_seconds": None,
            "token_intervals_observability": "UNAVAILABLE; SSE content chunks are not guaranteed single tokens",
            "output_tokens_per_second_end_to_end": usage["completion"] / elapsed if usage and elapsed else None,
            "output_tokens_per_second_decode_estimate": (usage["completion"] - 1) / decode if usage and decode else None}


def summarize(plan, rows, elapsed):
    validate_plan(plan)
    number(elapsed, "experiment elapsed")
    cases = schedule_cases(plan)
    planned = len(cases)
    require(len(rows) <= planned, "more observations than frozen requests")
    complete = [r for r in rows if r.get("status") == "PASS" and r.get("termination") == "COMPLETED"]
    failed = [r for r in rows if r.get("status") != "PASS"]
    expected = [r for r in rows if r.get("status") == "PASS" and r.get("termination") != "COMPLETED"]
    reasons = []
    require(len({r.get("request_id") for r in rows}) == len(rows), "duplicate request identities")
    expected_schedule = {f"r{index:05d}": name for index, name in enumerate(cases, 1)}
    for row in rows:
        name = expected_schedule.get(row.get("request_id"))
        termination = expected_termination(name)
        if name is None or row.get("case") != name or row.get("expected_termination") != termination:
            reasons.append(str(row.get("request_id")) + ": schedule identity/case/termination mismatch")
        elif row.get("status") == "PASS" and row.get("termination") != termination:
            reasons.append(row["request_id"] + ": actual termination differs from frozen expectation")
    rows = sorted(rows, key=lambda row: row["request_id"])
    thresholds = plan["thresholds"]
    if len(rows) < planned:
        reasons.append("frozen requests missing")
    if len(complete) < thresholds["minimum_successful_requests"]:
        reasons.append("insufficient successful completed requests")
    if len(failed) > thresholds["maximum_failures"]:
        reasons.append("failure threshold exceeded")
    if elapsed < plan["duration_seconds"]:
        reasons.append("minimum frozen duration not reached")
    recoveries = []
    for index, row in enumerate(rows):
        if row.get("termination") not in ("CALLER_CANCELLED", "TIMEOUT", "HTTP400"):
            continue
        recovery = next((candidate for candidate in rows[index + 1:]
                         if candidate.get("case") == "short"), None)
        if recovery is None:
            reasons.append(row["request_id"] + ": missing subsequent short recovery")
            continue
        delay = recovery["end_seconds"] - row["end_seconds"]
        passed = recovery.get("status") == "PASS" and recovery.get("termination") == "COMPLETED" and 0 <= delay <= plan["recovery_threshold_seconds"] and recovery["start_seconds"] >= row["end_seconds"]
        recoveries.append({"fault_request_id": row["request_id"], "recovery_request_id": recovery["request_id"],
                           "completed_after_fault_seconds": delay, "status": "PASS" if passed else "FAIL"})
        if not passed:
            reasons.append(row["request_id"] + ": recovery exceeded frozen target or failed")
    cache_rows = [r for r in complete if r.get("case") in ("cache_alpha", "cache_beta")]
    cache = [{"request_id": r["request_id"], "case": r["case"],
              "cached_tokens": r["usage"]["cached"], "prompt_tokens": r["usage"]["prompt"]} for r in cache_rows]
    if any(r["cached_tokens"] <= 0 for r in cache[1:]):
        reasons.append("shared-prefix reuse missing after the first cache request")
    long_cache = [{"request_id": r["request_id"], "case": r["case"],
                   "cached_tokens": r["usage"]["cached"], "prompt_tokens": r["usage"]["prompt"],
                   "uncached_fraction": 1 - r["usage"]["cached"] / r["usage"]["prompt"]}
                  for r in complete if r.get("case") in ("context16", "context32", "mixed_long")]
    groups = {}
    for case in sorted({r.get("case") for r in complete}):
        case_rows = [r for r in complete if r["case"] == case]
        samples = [validate_observation(r) for r in case_rows if r.get("first_content_seconds") is not None]
        stats = {name: percentiles([r[name] for r in samples]) for name in
                 ("ttft_seconds", "e2e_seconds", "client_queue_seconds", "scheduled_e2e_seconds")}
        if not samples:
            stats["e2e_seconds"] = percentiles([r["end_seconds"] - r["start_seconds"] for r in case_rows])
            stats["ttft_observability"] = "UNAVAILABLE_NONSTREAM"
        limits = plan.get("scenario_thresholds", {}).get(case, thresholds)
        for metric_name, threshold_name in (("ttft_seconds", "ttft_p95_seconds"),
                                            ("e2e_seconds", "e2e_p95_seconds")):
            if threshold_name in limits and stats[metric_name]["p95"] is not None and stats[metric_name]["p95"] > limits[threshold_name]:
                reasons.append(f"{case}: {threshold_name} exceeded")
        groups[case] = stats
    return {"schema_version": "atlas-request-experiment-result-v1", "status": "FAIL" if reasons else "PASS",
            "plan_sha256": canonical_sha256(plan), "planned_requests": planned,
            "observed_requests": len(rows), "missing_requests": planned - len(rows),
            "completed_requests": len(complete), "expected_interruptions_or_errors": len(expected),
            "failed_requests": len(failed), "elapsed_seconds": elapsed,
            "threshold_scope": plan["threshold_scope"], "failure_reasons": reasons, "scenarios": groups,
            "fault_recovery": recoveries, "cache_observations": cache,
            "long_prefill_cache_observations": long_cache,
            "ssm_private_state": "NOT_RUN", "cache_first_request_scope": "observed hit/miss, not assumed cold",
            "server_queue": "UNAVAILABLE", "business_accuracy": "NOT_RUN", "production": "not_qualified",
            "scope": "fixed finite request experiment; not long-term stability or production qualification"}


def build_prompt(kind, fixture, chars):
    require(type(chars) is int and 0 <= chars <= 1000000, "invalid filler size")
    template = fixture["filler_template"]
    minimum_line = len(template.format(index=0))
    require(minimum_line > 0, "empty filler template")
    filler = "".join(template.format(index=i) for i in range(chars // minimum_line + 1))[:chars]
    if kind == "context":
        a, b, c = fixture["markers"]
        mid = len(filler) // 2
        return (f"BEGIN marker: {a}. Remember all three marker values.\n" + filler[:mid] +
                f"\nMIDDLE marker: {b}.\n" + filler[mid:] + f"\nEND marker: {c}. "
                "Reply only with a JSON array of the BEGIN, MIDDLE, END marker integers in that order. No prose, no markdown.")
    require(kind in ("cache_alpha", "cache_beta"), "unknown prompt kind")
    codes = fixture["cache_codes"]
    key = "ALPHA" if kind == "cache_alpha" else "BETA"
    return (f"Lookup table: ALPHA={codes['ALPHA']}; BETA={codes['BETA']}.\n" + filler +
            f"\nReply only with the integer value for key {key}.")


def resource_trend(samples, plan):
    """Finite resource observation, not a leak diagnosis or host-memory inference."""
    validate_plan(plan)
    frozen = plan["resource_trend"]
    warmup, window = frozen["warmup_seconds"], frozen["window_seconds"]
    require(warmup + window <= plan["duration_seconds"], "resource windows exceed experiment duration")
    require(isinstance(samples, list) and len(samples) >= 3, "insufficient resource samples")
    identifiers = {r.get("container_id") for r in samples}
    starts = {r.get("container_started_at") for r in samples}
    require(len(identifiers) == 1 and isinstance(next(iter(identifiers)), str) and
            re.fullmatch(r"[0-9a-f]{64}", next(iter(identifiers))) is not None,
            "all resource samples require one fixed full container ID")
    require(len(starts) == 1 and isinstance(next(iter(starts)), str) and next(iter(starts)),
            "all resource samples require one fixed nonempty container StartedAt")
    times = [number(r.get("elapsed_seconds"), "resource timestamp") for r in samples]
    require(times == sorted(set(times)), "resource timestamps must be unique and ordered")
    require(times[-1] >= plan["duration_seconds"], "resource collection shorter than frozen experiment")
    usable = [r for r in samples if r["elapsed_seconds"] >= warmup]
    first = [r for r in usable if r["elapsed_seconds"] <= warmup + window]
    last = [r for r in usable if r["elapsed_seconds"] >= times[-1] - window]
    require(len(first) >= 3 and len(last) >= 3 and len(usable) >= 3, "insufficient frozen-window samples")
    result, reasons = {}, []
    for name in ("rss_bytes", "cgroup_current_bytes"):
        for row in samples:
            number(row.get(name), name)
        x = [r["elapsed_seconds"] for r in usable]
        y = [r[name] for r in usable]
        mean_x, mean_y = statistics.mean(x), statistics.mean(y)
        denominator = sum((t - mean_x) ** 2 for t in x)
        require(denominator > 0, "degenerate resource timing")
        slope = sum((t - mean_x) * (v - mean_y) for t, v in zip(x, y)) / denominator * 3600
        growth = statistics.median(r[name] for r in last) - statistics.median(r[name] for r in first)
        result[name] = {"sample_count": len(samples), "post_warmup_samples": len(usable),
            "peak": max(r[name] for r in samples), "median_growth_bytes": growth,
            "ols_growth_bytes_per_hour": slope}
        if growth > frozen["median_growth_limit_bytes"] or slope > frozen["ols_growth_limit_bytes_per_hour"]:
            reasons.append(name + " growth exceeds frozen experimental target")
    for name in ("restart_count", "oom_events"):
        values = [number(r.get(name), name) for r in samples]
        require(values == sorted(values), "resource event counter decreased/reset")
        if values[-1] > values[0]:
            reasons.append(name + " increased")
    active = [number(r.get("active_requests"), "active_requests") for r in samples]
    result.update(status="FAIL" if reasons else "PASS", failure_reasons=reasons,
        container_id=next(iter(identifiers)), container_started_at=next(iter(starts)),
        elapsed_seconds=times[-1], active_requests_final=active[-1],
        queue_drain="REQUIRES_SEPARATE_POST_LOAD_OBSERVATION",
        scope="frozen finite resource trend; neither cache growth nor positive slope proves a memory leak",
        host_cached_cuda_ledger_relationship="NOT_INFERRED")
    return result


def schedule_cases(plan):
    result = []
    for group in plan["schedule"]:
        for _ in range(group["repeats"]):
            result.append(group["case"])
            if "companion_case" in group:
                result.append(group["companion_case"])
    return result


def expected_termination(name):
    return {"cancel": "CALLER_CANCELLED", "timeout": "TIMEOUT", "invalid": "HTTP400"}.get(name, "COMPLETED")


def frozen_cases(plan, workload):
    """Exact expanded payload/oracle sequence, including precomputed long variants."""
    require(workload.get("plan_sha256") == canonical_sha256(plan), "workload does not bind frozen plan")
    indices, result = {}, []
    for name in schedule_cases(plan):
        case = workload["cases"][name]
        if "variants" in case:
            position = indices.get(name, 0)
            require(position < len(case["variants"]), "missing prepared long variant")
            case = case["variants"][position]
            indices[name] = position + 1
        require(case.get("case") == name and case.get("expected_termination") == expected_termination(name),
                "prepared case/termination differs from frozen schedule")
        result.append(case)
    return result


def verify_raw_observation(row, case, model, request, response):
    expected_request = (case["raw_body"].encode() if "raw_body" in case else
                        json.dumps(case["payload"], ensure_ascii=False).encode())
    require(request == expected_request, "raw request differs from frozen prepared payload")
    require(row["response_bytes"] == len(response), "response byte count mismatch")
    if row.get("status") != "PASS":
        return
    require(row.get("termination") == case["expected_termination"], "termination differs from frozen case")
    if row["termination"] == "HTTP400":
        validate_error(row["http_status"], json.loads(response))
        return
    if row["termination"] == "TIMEOUT":
        require(row.get("error") and not row.get("run_budget_exhausted"), "timeout lacks actual transport evidence")
        return
    if case["case"] == "tool":
        require(row.get("http_status") == 200, "invalid tool HTTP status")
        oracle = validate_tool(json.loads(response), model, case["tool_name"], case["tool_arguments"])
    else:
        require(row.get("http_status") == 200 and "text/event-stream" in row.get("content_type", ""),
                "invalid raw SSE envelope")
        content = ""
        for block in response.decode().replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
            if not data or data == "[DONE]":
                continue
            body = json.loads(data)
            require(body.get("object") == "chat.completion.chunk" and body.get("model") == model and
                    "error" not in body, "raw SSE identity/error mismatch")
            choices = body.get("choices", [])
            part = choices[0].get("delta", {}).get("content") if choices else None
            if part:
                require(isinstance(part, str), "invalid raw SSE text")
                content += part
        if row["termination"] == "CALLER_CANCELLED":
            require(content and row.get("first_content_seconds") is not None, "cancel before actual content")
            return
        actual = json.loads(content) if isinstance(case["answer"], (list, dict)) else content.strip()
        require(actual == case["answer"], "raw response fails frozen content oracle")
        oracle = validate_sse(response.decode(), model, content.strip())
    require(oracle["usage"] == row["usage"], "raw usage differs from observed measurement")
    if case.get("counted_prompt_tokens") is not None:
        require(oracle["usage"]["prompt"] == case["counted_prompt_tokens"], "raw template count differs")
