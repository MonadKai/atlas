# SPDX-License-Identifier: AGPL-3.0-only
"""Pure, pre-execution experimental targets, never business or production SLOs."""


def example_plan(kind):
    common = {"schema_version": "atlas-request-experiment-v1", "name": kind,
        "max_concurrency": 2, "request_timeout_seconds": 180,
        "maximum_requests": 2000, "threshold_scope": "EXPERIMENT_ONLY_NOT_BUSINESS_SLO",
        "thresholds": {"minimum_successful_requests": 1, "maximum_failures": 0,
                       "ttft_p95_seconds": 2, "e2e_p95_seconds": 8},
        "scenario_thresholds": {"context16": {"ttft_p95_seconds": 30},
            "context32": {"ttft_p95_seconds": 60}, "mixed_long": {"ttft_p95_seconds": 60},
            "mixed_short": {"ttft_p95_seconds": 5, "e2e_p95_seconds": 8},
            "cache_alpha": {"ttft_p95_seconds": 30}, "cache_beta": {"ttft_p95_seconds": 30}},
        "recovery_threshold_seconds": 30,
        "context_targets": {"context16": 16352, "context32": 32736,
                            "cache_alpha": 8192, "cache_beta": 8192, "mixed_long": 32736},
        "context_count_tolerance": 16, "context_output_budget": 32,
        "long_cache_policy": "UNIQUE_PREFIX_PER_REQUEST",
        "resource_trend": {"warmup_seconds": 600, "window_seconds": 900,
            "median_growth_limit_bytes": 2147483648, "ols_growth_limit_bytes_per_hour": 1073741824,
            "scope": "experimental target; cache growth is not automatically a leak"}}
    group = lambda case, repeats=1, concurrency=1, at=0: {
        "at_seconds": at, "case": case, "repeats": repeats, "concurrency": concurrency}
    if kind == "performance":
        schedule = [group("short", 40), group("context16", 5), group("context32", 5)]
        for _ in range(5):
            schedule.extend([group("cache_alpha"), group("cache_beta"), group("cache_alpha")])
        mixed = group("mixed_long", 5, 2)
        mixed.update(companion_case="mixed_short", companion_delay_seconds=.2)
        schedule.extend([mixed, group("short", 20, 2), group("tool", 5)])
        for fault in ("cancel", "timeout", "invalid"):
            for _ in range(5):
                schedule.extend([group(fault), group("short")])
        common.update(duration_seconds=.001, max_run_seconds=5400,
                      schedule_mode="SEQUENTIAL_GROUPS", schedule=schedule)
        common["thresholds"]["minimum_successful_requests"] = 115
    elif kind == "soak":
        schedule = []
        for cycle in range(240):
            at = cycle * 30
            schedule.append(group("short", at=at))
            if cycle % 5 == 0:
                schedule.append(group("context16" if (cycle // 5) % 2 == 0 else "context32", at=at))
            if cycle % 3 == 0:
                schedule.extend([group("cache_alpha", at=at), group("cache_beta", at=at), group("cache_alpha", at=at)])
            if cycle % 10 == 0:
                schedule.extend([group("cancel", at=at), group("invalid", at=at), group("short", at=at)])
        common.update(duration_seconds=7200, max_run_seconds=7440,
                      schedule_mode="WALL_CLOCK", schedule=schedule)
        common["thresholds"]["minimum_successful_requests"] = 552
    else:
        raise ValueError("kind must be performance or soak")
    return common
