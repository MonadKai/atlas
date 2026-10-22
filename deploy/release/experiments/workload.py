# SPDX-License-Identifier: AGPL-3.0-only
"""Request materialization through an explicit count-endpoint I/O boundary."""
from acceptance_cases import require
from acceptance_suites import chat_payload, fit_prompt
from .model import build_prompt, canonical_sha256, validate_plan, frozen_cases


def prepare(plan, fixture, model, router):
    validate_plan(plan)
    require(fixture.get("schema_version") == "atlas-request-fixtures-v1", "wrong fixture schema")
    cases = {}
    required = {g["case"] for g in plan["schedule"]} | {
        g["companion_case"] for g in plan["schedule"] if "companion_case" in g}
    require(plan.get("long_cache_policy") == "UNIQUE_PREFIX_PER_REQUEST", "explicit long-cache policy required")
    for name in sorted(required):
        case = {"case": name, "expected_termination": "COMPLETED"}
        if name in ("context16", "context32", "mixed_long", "cache_alpha", "cache_beta"):
            kind = name if name.startswith("cache_") else "context"
            counter = lambda text: router.count({"model": model, "messages": [{"role": "user", "content": text}],
                "max_tokens": plan["context_output_budget"], "thinking": {"type": "disabled"}, "temperature": 0})
            prompt, counted = fit_prompt(counter, lambda chars: build_prompt(kind, fixture, chars),
                400000, plan["context_targets"][name], plan["context_count_tolerance"])
            case.update(answer=(str(fixture["cache_codes"]["ALPHA" if name == "cache_alpha" else "BETA"])
                               if name.startswith("cache_") else fixture["markers"]), counted_prompt_tokens=counted)
            payload = chat_payload(model, prompt, plan["context_output_budget"])
        elif name in ("short", "mixed_short"):
            case["answer"] = fixture[name]["answer"]
            payload = chat_payload(model, fixture[name]["prompt"], 64)
        elif name in ("cancel", "timeout"):
            payload = chat_payload(model, fixture["cancel_prompt"], 512)
            case.update(expected_termination="CALLER_CANCELLED" if name == "cancel" else "TIMEOUT")
            if name == "timeout":
                case["timeout_seconds"] = .001
        elif name == "invalid":
            payload = {}
            case.update(raw_body='{"model":', expected_termination="HTTP400")
        elif name == "tool":
            tool = fixture["tool"]
            payload = chat_payload(model, tool["prompt"], 128)
            payload.update(tools=[tool["definition"]], tool_choice="auto")
            case.update(tool_name=tool["name"], tool_arguments=tool["arguments"])
        else:
            raise ValueError("unsupported case")
        if name != "tool":
            payload.update(stream=True, stream_options={"include_usage": True})
        case["payload"] = payload
        cases[name] = case
        if name in ("context16", "context32", "mixed_long"):
            count = sum(g["repeats"] for g in plan["schedule"] if g["case"] == name)
            variants = []
            for index in range(count):
                nonce = f"Isolated prefill {canonical_sha256(plan)[7:23]} {name} {index:05d}.\n"
                prompt, counted = fit_prompt(counter,
                    lambda chars: nonce + build_prompt("context", fixture, chars), 400000,
                    plan["context_targets"][name], plan["context_count_tolerance"])
                require(counted + plan["context_output_budget"] <= 32768,
                        "prepared prompt/output budget exceeds fixed 32K context")
                variant = {"case": name, "expected_termination": "COMPLETED",
                    "answer": fixture["markers"], "counted_prompt_tokens": counted,
                    "prefix_nonce": nonce, "cache_intent": "UNIQUE_PREFIX; actual cached tokens must still be reported",
                    "payload": {**chat_payload(model, prompt, plan["context_output_budget"]),
                                "stream": True, "stream_options": {"include_usage": True}}}
                variants.append(variant)
            cases[name] = {"variants": variants}
    # Both suffixes must share byte-for-byte prefix, not merely equal counts.
    if "cache_alpha" in cases and "cache_beta" in cases:
        a = cases["cache_alpha"]["payload"]["messages"][0]["content"]
        b = a.rsplit("for key ALPHA.", 1)[0] + "for key BETA."
        cases["cache_beta"]["payload"]["messages"][0]["content"] = b
        cases["cache_beta"]["counted_prompt_tokens"] = router.count({"model": model,
            "messages": [{"role": "user", "content": b}], "max_tokens": plan["context_output_budget"],
            "thinking": {"type": "disabled"}, "temperature": 0})
    return {"schema_version": "atlas-prepared-workload-v1", "plan_sha256": canonical_sha256(plan),
            "fixture_sha256": canonical_sha256(fixture), "model": model, "cases": cases,
            "token_count_scope": "actual matching count endpoint includes template, not raw-character estimates"}


def execute(plan, workload, router):
    """Only schedules bounded client work; no process/container lifecycle actions."""
    validate_plan(plan)
    require(workload.get("plan_sha256") == canonical_sha256(plan), "plan changed after workload preparation")
    require(workload.get("model") == router.model, "workload/model mismatch")
    frozen_cases(plan, workload)
    rows, index, variant_indices = [], 0, {}
    for group in plan["schedule"]:
        if router.now() >= plan["max_run_seconds"]:
            break
        at = group["at_seconds"] if plan["schedule_mode"] == "WALL_CLOCK" else router.now()
        router.pace(at)
        entries = []
        dispatch = router.now()
        for _ in range(group["repeats"]):
            index += 1
            case = workload["cases"][group["case"]]
            if "variants" in case:
                position = variant_indices.get(group["case"], 0)
                case = case["variants"][position]
                variant_indices[group["case"]] = position + 1
            entries.append((f"r{index:05d}", case, at, dispatch))
            if "companion_case" in group:
                index += 1
                entries.append((f"r{index:05d}", workload["cases"][group["companion_case"]],
                                at + group["companion_delay_seconds"], dispatch))
        rows.extend(router.group(entries, group["concurrency"]))
    if plan["schedule_mode"] == "WALL_CLOCK":
        router.pace(plan["duration_seconds"])
    return rows
