#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure, fail-closed oracles for the private GB10 development profile."""
import json
import math
import re


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value, name):
    require(type(value) is int and value >= 0, f"{name} must be a nonnegative integer")
    return value


def validate_usage(usage):
    require(isinstance(usage, dict), "missing usage")
    p = integer(usage.get("prompt_tokens"), "prompt_tokens")
    c = integer(usage.get("completion_tokens"), "completion_tokens")
    t = integer(usage.get("total_tokens"), "total_tokens")
    require(p > 0 and c > 0 and t == p + c, "incoherent or empty usage")
    details = usage.get("prompt_tokens_details", {})
    require(isinstance(details, dict), "invalid prompt token details")
    cached = integer(details.get("cached_tokens", 0), "cached_tokens")
    require(cached <= p, "cached tokens exceed prompt")
    spec = usage.get("completion_tokens_details", {})
    require(isinstance(spec, dict), "invalid completion token details")
    accepted = integer(spec.get("accepted_prediction_tokens", 0), "accepted_prediction_tokens")
    rejected = integer(spec.get("rejected_prediction_tokens", 0), "rejected_prediction_tokens")
    return {"prompt": p, "completion": c, "total": t, "cached": cached,
            "accepted_prediction_tokens": accepted, "rejected_prediction_tokens": rejected}


def validate_completion(body, model, expected):
    require(isinstance(body, dict) and "error" not in body, "completion error")
    require(body.get("object") == "chat.completion", "wrong response object")
    require(isinstance(body.get("id"), str) and body["id"], "missing response id")
    require(body.get("model") == model, "model alias mismatch")
    choices = body.get("choices")
    require(isinstance(choices, list) and len(choices) == 1, "expected exactly one choice")
    choice = choices[0]
    require(choice.get("index") == 0, "wrong choice index")
    require(choice.get("finish_reason") in ("stop", "length"), "missing/unsupported finish reason")
    message = choice.get("message", {})
    require(message.get("role") == "assistant", "wrong message role")
    content = message.get("content")
    require(isinstance(content, str), "missing text content")
    actual = json.loads(content) if isinstance(expected, (dict, list)) else content.strip()
    require(actual == expected, "exact content oracle failed")
    counts = validate_usage(body.get("usage"))
    return {"status": "PASS", "content": content, "usage": counts,
            "mtp_actual_accepted": counts["accepted_prediction_tokens"] > 0}


def validate_tool(body, model, expected_name, expected_arguments):
    require(isinstance(body, dict) and body.get("object") == "chat.completion" and
            body.get("model") == model and isinstance(body.get("id"), str), "invalid tool response")
    choices = body.get("choices")
    require(isinstance(choices, list) and len(choices) == 1, "invalid tool choices")
    choice = choices[0]
    require(choice.get("index") == 0 and choice.get("finish_reason") == "tool_calls",
            "missing tool finish reason")
    message = choice.get("message", {})
    require(message.get("role") == "assistant", "invalid tool role")
    calls = message.get("tool_calls")
    require(isinstance(calls, list) and len(calls) == 1, "expected one actual tool call")
    call = calls[0]
    require(call.get("type") == "function" and isinstance(call.get("id"), str) and call["id"],
            "invalid tool call identity")
    function = call.get("function", {})
    require(function.get("name") == expected_name and
            json.loads(function.get("arguments", "")) == expected_arguments,
            "tool schema/arguments oracle failed")
    return {"status": "PASS", "function": function, "usage": validate_usage(body.get("usage"))}


def validate_context(body, counted, limit, output_budget, tolerance):
    counts = validate_usage(body.get("usage"))
    require(type(counted) is int and counts["prompt"] == counted,
            "count endpoint differs from actual chat template usage")
    target = limit - output_budget
    require(target - tolerance <= counted <= target, "not near frozen context boundary")
    require(counts["completion"] <= output_budget and counts["total"] <= limit,
            "output/context ceiling exceeded")
    return {"status": "PASS", "limit": limit, "counted_input": counted,
            "output_budget": output_budget, "usage": counts,
            "scope": "template-inclusive measured near-boundary request, not every 32K workload"}


def validate_cache(responses, model, answers, counter_before, counter_after):
    require(len(responses) == len(answers) == 3, "cache requires A/B/A observations")
    rows = [validate_completion(r, model, a) for r, a in zip(responses, answers)]
    require(answers[0] == answers[2] and answers[0] != answers[1], "suffix oracle is not distinct")
    require(all(r["usage"]["cached"] > 0 for r in rows[1:]), "no actual reused-prefix evidence")
    require(counter_after > counter_before, "global reused-prefix token counter did not increase")
    return {"status": "PASS", "observations": rows,
            "counter_delta": counter_after - counter_before,
            "ssm_internal_restore": "NOT_RUN",
            "scope": "shared-prefix reuse and observable suffix isolation; not deep SSM-state proof"}


def validate_sse(raw, model, expected):
    normalized = raw.replace("\r\n", "\n")
    require(normalized.endswith("\n\n"), "unterminated SSE event")
    events = normalized.split("\n\n")[:-1]
    content, identity, finish, done, chunks, usage = "", None, False, False, 0, None
    for event in events:
        lines = [line for line in event.splitlines() if line and not line.startswith(":" )]
        if not lines:
            continue
        require(not done, "event after DONE")
        require(all(line.startswith("data:") for line in lines), "unexpected SSE field")
        data = "\n".join(line[5:].lstrip(" ") for line in lines)
        if data == "[DONE]":
            require(finish, "DONE without finish reason")
            done = True
            continue
        body = json.loads(data)
        require(isinstance(body, dict) and "error" not in body, "SSE error event")
        require(body.get("object") == "chat.completion.chunk" and body.get("model") == model,
                "SSE response identity mismatch")
        require(isinstance(body.get("id"), str) and body["id"], "missing SSE id")
        identity = identity or body["id"]
        require(body["id"] == identity, "SSE id changed")
        choices = body.get("choices")
        require(isinstance(choices, list) and len(choices) <= 1, "invalid SSE choices")
        if not choices:
            require(isinstance(body.get("usage"), dict), "empty SSE choices without final usage")
        else:
            require(not finish, "choice after terminal choice")
            choice = choices[0]
            require(choice.get("index") == 0 and isinstance(choice.get("delta"), dict), "invalid SSE delta")
            delta = choice["delta"]
            require(delta.get("role", "assistant") == "assistant", "wrong SSE role")
            part = delta.get("content")
            require(part is None or isinstance(part, str), "nontext SSE content")
            if usage is not None:
                # Atlas emits usage-only BEFORE the final empty-delta finish.
                # A usage block is terminal accounting: no later content.
                require(not part and choice.get("finish_reason") is not None,
                        "content or nonterminal delta after usage")
            content += part or ""
            if choice.get("finish_reason") is not None:
                require(choice["finish_reason"] in ("stop", "length"), "unexpected SSE finish reason")
                finish = True
        if body.get("usage") is not None:
            require(usage is None, "duplicate SSE usage block")
            usage = validate_usage(body["usage"])
        chunks += 1
    require(done and chunks > 0 and content.strip() == expected, "incomplete SSE or exact content oracle failed")
    return {"status": "PASS", "content": content, "chunks": chunks, "done": done, "usage": usage}


def validate_error(status, body, message_contains=None):
    require(status == 400 and isinstance(body, dict), "expected HTTP400 JSON error")
    error = body.get("error")
    require(isinstance(error, dict) and isinstance(error.get("message"), str), "missing structured error")
    if message_contains:
        require(message_contains in error["message"], "wrong error message")
    return {"status": "PASS", "http_status": status, "error": error}


def validate_completed_work(tokens, generation_before, generation_after,
                            requests, requests_before, requests_after):
    """Blocking handlers synchronously increment completion counters before reply.

    DECODED_TOKENS_TOTAL belongs to streaming handle_token, not this path. Other
    traffic can add global work, so require at least this suite's actual usage;
    exact request/content checks remain the request-specific evidence.
    """
    require(type(tokens) is int and tokens > 0 and type(requests) is int and requests > 0,
            "missing expected completed request work")
    observed = (generation_before, generation_after, requests_before, requests_after)
    require(all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in observed),
            "invalid completed-work counters")
    generation_delta = generation_after - generation_before
    request_delta = requests_after - requests_before
    require(generation_delta >= tokens and request_delta >= requests,
            "completed generation/request counters do not cover actual response usage")
    return {"generation_tokens_delta": generation_delta, "requests_delta": request_delta,
            "expected_completion_tokens": tokens, "expected_requests": requests,
            "scope": "synchronous completed blocking-handler counters; not GPU kernel proof"}


def metric(text, name):
    rows = re.findall(r"^" + re.escape(name) + r" ([^\s]+)(?:[ \t].*)?$", text, re.M)
    require(len(rows) == 1, f"missing/duplicate metric {name}")
    value = float(rows[0])
    require(math.isfinite(value) and value >= 0, f"invalid metric {name}")
    return value


def native_log_evidence(text):
    """Read-only clues, explicitly insufficient to attest GPU kernel execution."""
    selected = [line for line in text.splitlines() if any(term in line.lower() for term in
                ("quantization format", "mtp", "kernel audit", "prefix cache", "shutdown complete"))]
    return {"selected_lines": selected, "gpu_kernel_execution": "NOT_RUN",
            "ssm_internal_restore": "NOT_RUN",
            "scope": "diagnostic log selection only; correlate independent actual execution evidence"}
