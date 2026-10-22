#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Client-only active-request observer; coordinator owns signals and restart.

Writes --ready-file atomically only after an actual SSE content event. Continues
reading and delegates complete framing validation to the matched SSE oracle.
No Docker, process signals, lifecycle controller or source checkout is used.
The synthetic bounded output is a drain probe, not an accuracy evaluation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import stat
import tempfile
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True

from acceptance_cases import require, validate_sse
from acceptance_http import Recorder, write_json
from acceptance_suites import chat_payload


LEGACY_PROMPT = ("Print the integers from 1 through 180 in ascending order, "
                 "separated by one space. No prose or markdown.")
MAX_PROMPT_BYTES = 16384


def default_prompt():
    raw = LEGACY_PROMPT.encode("utf-8")
    return {"text": LEGACY_PROMPT, "identity": {"source": "legacy-default", "size_bytes": len(raw),
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}}


def load_prompt(path):
    """Read-only prompt I/O boundary: fixed-size regular UTF-8, no symlink parents."""
    path = Path(path).absolute()
    try:
        require(not any(part.is_symlink() for part in [path, *path.parents]), "prompt path contains symlink")
        before = path.stat()
        require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= MAX_PROMPT_BYTES,
                "prompt must be a nonempty regular file within 16384 bytes")
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with open(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            require(stat.S_ISREG(opened.st_mode), "prompt is not a regular file")
            raw = stream.read(MAX_PROMPT_BYTES + 1)
            finished = os.fstat(stream.fileno())
        after = path.stat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        require(all(identity(item) == identity(before) for item in (opened, finished, after)), "prompt changed during reading")
        require(not any(part.is_symlink() for part in [path, *path.parents]), "prompt path changed to symlink")
        require(0 < len(raw) <= MAX_PROMPT_BYTES, "prompt exceeds 16384-byte bound")
        text = raw.decode("utf-8", errors="strict")
        require(text.strip() and "\x00" not in text, "prompt must contain nonempty text without NUL")
        return {"text": text, "identity": {"source": "explicit-regular-file", "path": str(path),
                "size_bytes": len(raw), "utf8_characters": len(text), "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}}
    except (OSError, UnicodeError, RuntimeError) as error:
        raise ValueError("invalid prompt file: " + type(error).__name__) from error


def event_body(raw):
    """Extract one SSE event for readiness only; full oracle remains validate_sse."""
    lines = [line for line in raw.replace("\r\n", "\n").splitlines()
             if line and not line.startswith(":" )]
    if not lines:
        return None
    require(all(line.startswith("data:") for line in lines), "unexpected SSE event field")
    data = "\n".join(line[5:].lstrip(" ") for line in lines)
    if data == "[DONE]":
        return None
    body = json.loads(data)
    require(isinstance(body, dict), "nonobject SSE event")
    return body


def content_event(raw, model):
    body = event_body(raw)
    if body is None or "error" in body:
        return None
    require(body.get("model") == model and body.get("object") == "chat.completion.chunk" and
            isinstance(body.get("id"), str) and body["id"], "SSE identity mismatch before readiness")
    choices = body.get("choices")
    require(isinstance(choices, list) and len(choices) <= 1, "invalid readiness choices")
    if not choices:
        return None
    choice = choices[0]
    require(choice.get("index") == 0 and isinstance(choice.get("delta"), dict), "invalid readiness delta")
    require(choice["delta"].get("role", "assistant") == "assistant", "invalid readiness role")
    part = choice["delta"].get("content")
    if part is None or part == "":
        return None
    require(isinstance(part, str), "nontext readiness content")
    return {"response_id": body["id"], "model": model, "content": part}


def classify_stream(raw, model, require_usage=False):
    content, finish, usage = "", None, None
    try:
        for event in raw.replace("\r\n", "\n").split("\n\n"):
            if not event.strip():
                continue
            body = event_body(event)
            if body is None:
                continue
            if "error" in body:
                return {"status": "FAIL", "outcome": "TERMINAL_ERROR", "error": body["error"]}
            choices = body.get("choices", [])
            if choices:
                part = choices[0].get("delta", {}).get("content")
                content += part or ""
                finish = choices[0].get("finish_reason") or finish
            if body.get("usage") is not None:
                usage = body["usage"]
        result = validate_sse(raw, model, content.strip())
        require(bool(content.strip()), "no actual content generated")
        require(not require_usage or result["usage"] is not None, "requested stream usage was omitted")
        return {"status": "PASS", "outcome": "COMPLETED", "stream": result,
                "finish_reason": finish, "reported_usage": usage}
    except (ValueError, TypeError, AttributeError, IndexError) as error:
        return {"status": "FAIL", "outcome": "INCOMPLETE_OR_INVALID",
                "error": f"{type(error).__name__}: {error}"}


def atomic_ready(path, body):
    """Atomic no-replace publication: never overwrite an earlier observation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".atlas-ready-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(body, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def observe(client, model, output, ready, max_tokens, prompt_input=None):
    prompt_input = default_prompt() if prompt_input is None else prompt_input
    payload = chat_payload(model, prompt_input["text"], max_tokens)
    payload.update(stream=True, stream_options={"include_usage": True})
    request = json.dumps(payload, ensure_ascii=False).encode()
    (output / "request.json").write_bytes(request)
    result = {"schema": "atlas-active-shutdown-observation-v1", "started_unix": time.time(),
              "model": model, "max_tokens": max_tokens, "prompt_identity": prompt_input["identity"],
              "request_sha256": hashlib.sha256(request).hexdigest(),
              "scope": "client observation only; coordinator must independently prove active state and actual SIGTERM"}
    response = None
    raw = bytearray()
    published = False
    try:
        req = urllib.request.Request(client.origin + "/v1/chat/completions", data=request,
                                     headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(req, timeout=180)
        except urllib.error.HTTPError as error:
            response = error
        result.update(http_status=response.status, content_type=response.headers.get("Content-Type", ""))
        require(response.status == 200 and "text/event-stream" in result["content_type"], "expected HTTP200 SSE")
        event = bytearray()
        with (output / "response.sse").open("xb") as saved:
            while True:
                line = response.readline()
                if not line:
                    break
                saved.write(line)
                saved.flush()
                raw.extend(line)
                event.extend(line)
                if line not in (b"\n", b"\r\n"):
                    continue
                actual = content_event(event.decode("utf-8"), model)
                if actual is not None and not published:
                    notice = {"schema": "atlas-active-shutdown-ready-v1", "ready_unix": time.time(),
                              "ready_monotonic_ns": time.monotonic_ns(), "received_content_event": actual,
                              "response_id": actual["response_id"], "model": model,
                              "raw_prefix_sha256": hashlib.sha256(raw).hexdigest(),
                              "prompt_sha256": prompt_input["identity"]["sha256"]}
                    atomic_ready(ready, notice)
                    result["ready"] = notice
                    published = True
                event.clear()
        result.update(classify_stream(raw.decode("utf-8"), model, require_usage=True))
        require(published, "stream completed without a real content readiness event")
    except Exception as error:
        result.update(status="FAIL", outcome="TRANSPORT_OR_OBSERVER_ERROR",
                      error=f"{type(error).__name__}: {error}")
        if response is not None and response.status != 200:
            try:
                raw.extend(response.read())
            except Exception as secondary:
                result["error_body_read_error"] = f"{type(secondary).__name__}: {secondary}"
    finally:
        if response is not None:
            response.close()
        # Write even when connect/headers failed; partial bytes remain evidence.
        (output / "response.sse").write_bytes(raw)
        result.update(ended_unix=time.time(), raw_bytes=len(raw),
                      response_sha256=hashlib.sha256(raw).hexdigest())
        write_json(output / "observation.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--prompt-file", type=Path, help="Explicit regular UTF-8 fixture; max16384bytes, no symlink parents; legacy default retained")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    ready, output = args.ready_file.resolve(), args.output.resolve()
    for path in (ready, output):
        if path == root or root in path.parents:
            parser.error("observer output must be outside the immutable release bundle")
    if ready.exists() or output.exists() or not 1 <= args.max_tokens <= 512:
        parser.error("ready/output must be new paths and max-tokens must be within1..512")
    try:
        prompt_input = load_prompt(args.prompt_file) if args.prompt_file is not None else default_prompt()
        client = Recorder(args.base_url, output, 180)
    except ValueError as error:
        parser.error(str(error))
    output.mkdir(parents=True, exist_ok=False)
    result = observe(client, args.model, output, ready, args.max_tokens, prompt_input)
    print(json.dumps({"status": result["status"], "outcome": result["outcome"],
                      "observation": str(output / "observation.json")}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
