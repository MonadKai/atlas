#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Request-only suites; agent owns deployment, signals, and recovery sequencing."""
import time

from acceptance_cases import (integer, metric, require, validate_cache, validate_completion,
                              validate_context, validate_error, validate_sse, validate_tool)
from acceptance_cases import validate_completed_work


def chat_payload(model, text, budget=32):
    return {"model": model, "messages": [{"role": "user", "content": text}],
            "temperature": 0, "max_tokens": budget,
            "chat_template_kwargs": {"enable_thinking": False}}


def generate_filler(cases):
    return "".join(cases["filler_template"].format(index=i) for i in range(6000))


def count_prompt(client, model, text, budget):
    result = client.json("/v1/messages/count_tokens", {
        "model": model, "messages": [{"role": "user", "content": text}],
        "max_tokens": budget, "thinking": {"type": "disabled"}, "temperature": 0})
    return integer(result.get("input_tokens"), "input_tokens")


def fit_prompt(count, build, max_chars, target, tolerance):
    """Bounded search using actual server tokenizer, never a characters/token estimate."""
    low, high, best = 0, max_chars, None
    for _ in range(24):
        if low > high:
            break
        middle = (low + high) // 2
        text = build(middle)
        value = count(text)
        if target - tolerance <= value <= target:
            return text, value
        if value <= target:
            best = (text, value)
            low = middle + 1
        else:
            high = middle - 1
    require(best is not None and target - tolerance <= best[1] <= target,
            "unable to fit frozen context target within count tolerance")
    return best


def public(client, model, cases):
    models = client.json("/v1/models")
    require(any(row.get("id") == model for row in models.get("data", [])), "model alias absent")
    before_metrics = metrics(client)
    before = metric(before_metrics, "atlas_decoded_tokens_total", missing_zero=True)
    rows = []
    for case in cases["public"]:
        body = client.json("/v1/chat/completions", chat_payload(model, case["prompt"]))
        rows.append(validate_completion(body, model, case["answer"]))
    tool = cases["tool"]
    payload = chat_payload(model, tool["prompt"], 128)
    payload.update(tools=[tool["definition"]], tool_choice="auto")
    body = client.json("/v1/chat/completions", payload)
    rows.append(validate_tool(body, model, tool["name"], tool["arguments"]))
    after_metrics = metrics(client)
    decoded = metric(after_metrics, "atlas_decoded_tokens_total", missing_zero=True) - before
    work = validate_completed_work(sum(row["usage"]["completion"] for row in rows),
                                   metric(before_metrics, "atlas_generation_tokens_total", missing_zero=True),
                                   metric(after_metrics, "atlas_generation_tokens_total"), len(rows),
                                   metric(before_metrics, "atlas_requests_total", missing_zero=True),
                                   metric(after_metrics, "atlas_requests_total"))
    require(metric(after_metrics, "atlas_kernel_lookups_unresolved") == 0,
            "unresolved kernel dispatch detected")
    return {"status": "PASS", "checks": rows,
            "completed_work": work, "stream_only_decoded_tokens_delta_observation": decoded,
            "unresolved_kernel_lookups": 0,
            "gpu_kernel_execution": "NOT_RUN; pair these public observations with independent native execution evidence",
            "scope": "existing text/Chinese and automatic get_weather schema; not business accuracy"}


def context(client, model, cases):
    limit, budget = cases["context_limit"], cases["context_output_budget"]
    tolerance, filler = cases["context_input_tolerance"], generate_filler(cases)
    markers = cases["context_markers"]

    def build(n):
        part = filler[:n]
        middle = len(part) // 2
        return (f"BEGIN marker: {markers[0]}. Remember all three marker values.\n" +
                part[:middle] + f"\nMIDDLE marker: {markers[1]}.\n" + part[middle:] +
                f"\nEND marker: {markers[2]}. Reply only with a JSON array of the BEGIN, MIDDLE, END "
                "marker integers in that order. No prose, no markdown.")

    counter = lambda text: count_prompt(client, model, text, budget)
    text, counted = fit_prompt(counter, build, len(filler), limit - budget, tolerance)
    body = client.json("/v1/chat/completions", chat_payload(model, text, budget))
    oracle = validate_completion(body, model, markers)
    boundary = validate_context(body, counted, limit, budget, tolerance)
    oversized, over_count = fit_prompt(counter, build, len(filler), limit + 32, 16)
    require(over_count >= limit, "oversized fixture did not reach context limit")
    status, result, _ = client.request("/v1/chat/completions", chat_payload(model, oversized, budget))
    error = validate_error(status, result, "Prompt too long")
    recovery = client.json("/v1/chat/completions", chat_payload(model, cases["public"][0]["prompt"]))
    return {"status": "PASS", "content_preservation": oracle, "near_boundary": boundary,
            "oversized_count": over_count, "oversized": error,
            "recovery": validate_completion(recovery, model, cases["public"][0]["answer"]),
            "budget_semantics": "input includes template; generation capped by max_tokens and sequence ceiling; sum is not pre-rejected"}


def metrics(client):
    status, text, _ = client.request("/metrics")
    require(status == 200 and isinstance(text, str), "metrics unavailable")
    return text


def cache(client, model, cases):
    filler = generate_filler(cases)
    table = cases["cache_prefix"]
    build = lambda n: table + filler[:n] + "\nReply only with the integer value for key ALPHA."
    text, counted = fit_prompt(lambda t: count_prompt(client, model, t, 32), build,
                               len(filler), cases["cache_target_input"], 16)
    # Both requests preserve the complete prefix; only the final key differs.
    texts = [text, text.replace("for key ALPHA.", "for key BETA."), text]
    before = metric(metrics(client), "atlas_prefix_cache_hit_tokens_total", missing_zero=True)
    rows = [client.json("/v1/chat/completions", chat_payload(model, text)) for text in texts]
    after = metric(metrics(client), "atlas_prefix_cache_hit_tokens_total")
    result = validate_cache(rows, model, cases["cache_codes"], before, after)
    result.update(first_request_counted_input=counted,
                  first_request_cached_tokens=rows[0]["usage"].get("prompt_tokens_details", {}).get("cached_tokens", 0),
                  restart_persistence="NOT_RUN; caller must record restart and invoke this suite independently")
    return result


def stream(client, model, cases):
    case = cases["public"][0]
    payload = chat_payload(model, case["prompt"])
    payload.update(stream=True, stream_options={"include_usage": True})
    status, raw, metadata = client.request("/v1/chat/completions", payload, stream=True)
    require(status == 200 and "text/event-stream" in metadata["content_type"], "wrong SSE HTTP envelope")
    normal = validate_sse(raw, model, case["answer"])
    require(normal["usage"] is not None, "requested SSE usage was omitted")
    baseline = metric(metrics(client), "atlas_requests_active")
    require(baseline == 0, "cancellation test requires an idle isolated instance")
    cancel = chat_payload(model, "Write a detailed list of the integers from 1 through 300, one per line.", 512)
    cancel.update(stream=True)
    status, _, metadata = client.request("/v1/chat/completions", cancel, stream=True, cancel=True)
    require(status == 200 and metadata.get("caller_closed_after_content") is True,
            "client did not disconnect after real content")
    deadline = time.monotonic() + cases["cancel_recovery_seconds"]
    active = metric(metrics(client), "atlas_requests_active")
    while active != baseline and time.monotonic() < deadline:
        time.sleep(0.2)
        active = metric(metrics(client), "atlas_requests_active")
    require(active == baseline, "active request gauge did not recover after disconnect")
    recovery = client.json("/v1/chat/completions", chat_payload(model, case["prompt"]))
    return {"status": "PASS", "normal": normal, "caller_disconnect": metadata,
            "active_requests_after": active, "recovery": validate_completion(recovery, model, case["answer"]),
            "private_allocator_reclamation": "NOT_RUN", "ssm_internal_restore": "NOT_RUN"}


def errors(client, model, cases):
    rows = []
    status, body, _ = client.request("/v1/chat/completions", raw=b'{"model":')
    rows.append(validate_error(status, body))
    invalid = chat_payload(model, "No generation should run.")
    invalid["messages"] = "invalid string instead of array"
    status, body, _ = client.request("/v1/chat/completions", invalid)
    rows.append(validate_error(status, body))
    invalid = chat_payload(model, "No generation should run.")
    invalid["temperature"] = -1
    status, body, _ = client.request("/v1/chat/completions", invalid)
    rows.append(validate_error(status, body))
    case = cases["public"][0]
    body = client.json("/v1/chat/completions", chat_payload(model, case["prompt"]))
    return {"status": "PASS", "errors": rows,
            "recovery": validate_completion(body, model, case["answer"]),
            "missing_model_startup": "NOT_RUN; agent performs separate bounded startup fault"}


SUITES = {"public": public, "context": context, "cache": cache, "stream": stream, "errors": errors}
