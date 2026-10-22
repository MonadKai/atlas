# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit I/O boundary for bounded request transport, timing and evidence."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from acceptance_cases import require, validate_sse, validate_tool, validate_error
from .model import validate_observation


class IORouter:
    def __init__(self, base_url, output, model, timeout, start):
        parsed = urllib.parse.urlsplit(base_url)
        require(parsed.scheme in ("http", "https") and parsed.netloc and not parsed.username and
                not parsed.password and not parsed.query and not parsed.fragment and
                parsed.path.rstrip("/") in ("", "/v1"), "invalid credential-free base URL")
        self.origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        self.output = Path(output)
        self.model, self.timeout, self.start = model, timeout, start
        self.run_deadline = None
        self.count_index = 0
        self.lock = threading.Lock()

    @staticmethod
    def read_json(path):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                require(key not in result, "duplicate JSON key")
                result[key] = value
            return result
        def invalid_constant(value):
            raise ValueError("nonfinite JSON constant: " + value)
        return json.loads(Path(path).read_text(), object_pairs_hook=unique, parse_constant=invalid_constant)

    @staticmethod
    def write_json(path, value):
        Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")

    def now(self):
        return time.monotonic() - self.start

    def pace(self, at):
        remaining = at - self.now()
        if self.run_deadline is not None:
            remaining = min(remaining, self.run_deadline - time.monotonic())
        if remaining > 0:
            time.sleep(remaining)

    def count(self, payload):
        self.count_index += 1
        stem = self.output / f"count-{self.count_index:04d}"
        stem.mkdir()
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        (stem / "request.json").write_bytes(encoded)
        request = urllib.request.Request(self.origin + "/v1/messages/count_tokens",
            data=encoded, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            require(response.status == 200, "count endpoint did not return HTTP200")
            raw = response.read(8 * 1024 * 1024)
            (stem / "response.json").write_bytes(raw)
            return json.loads(raw)["input_tokens"]

    def execute(self, request_id, case, scheduled, dispatch):
        self.pace(scheduled)
        stem = self.output / request_id
        stem.mkdir()
        payload = case["payload"]
        body = (case["raw_body"].encode() if "raw_body" in case else
                json.dumps(payload, ensure_ascii=False).encode())
        (stem / "request.json").write_bytes(body)
        start = self.now()
        deadline = self.start + start + case.get("timeout_seconds", self.timeout)
        if self.run_deadline is not None:
            deadline = min(deadline, self.run_deadline)
        record = {"request_id": request_id, "case": case["case"],
                  "scheduled_seconds": scheduled, "dispatch_seconds": dispatch,
                  "start_seconds": start, "started_at": datetime.now(timezone.utc).isoformat(),
                  "headers_seconds": None, "first_content_seconds": None,
                  "content_event_seconds": [], "server_queue_seconds": None,
                  "expected_termination": case["expected_termination"], "usage": None,
                  "request_sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
                  "status": "FAIL", "termination": "UNKNOWN", "prepared_prompt_tokens": case.get("counted_prompt_tokens")}
        response, raw = None, bytearray()
        events, event_lines, content = [], [], ""
        try:
            if deadline <= time.monotonic():
                record["run_budget_exhausted"] = True
                raise TimeoutError("experiment/request absolute budget exhausted before send")
            request = urllib.request.Request(self.origin + "/v1/chat/completions", data=body,
                                             headers={"Content-Type": "application/json"})
            try:
                response = urllib.request.urlopen(request, timeout=max(.001, deadline - time.monotonic()))
            except urllib.error.HTTPError as error:
                response = error
            record.update(headers_seconds=self.now(), http_status=response.status,
                          content_type=response.headers.get("Content-Type", ""))
            if case["expected_termination"] == "HTTP400":
                raw.extend(response.read(8 * 1024 * 1024))
                validate_error(response.status, json.loads(raw))
                record.update(status="PASS", termination="HTTP400")
            elif case["case"] == "tool":
                raw.extend(response.read(8 * 1024 * 1024))
                require(response.status == 200, "tool request HTTP failure")
                oracle = validate_tool(json.loads(raw), self.model, case["tool_name"], case["tool_arguments"])
                record.update(status="PASS", termination="COMPLETED", usage=oracle["usage"],
                              ttft_observability="UNAVAILABLE_NONSTREAM_TOOL")
            else:
                require(response.status == 200 and "text/event-stream" in record["content_type"],
                        "expected HTTP200 SSE")
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("absolute request deadline exhausted")
                    # urllib keeps the response's socket after Connection:close;
                    # bound each read to the remaining absolute request budget.
                    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if sock is not None:
                        sock.settimeout(remaining)
                    line = response.readline(8 * 1024 * 1024)
                    if not line:
                        break
                    raw.extend(line)
                    require(len(raw) <= 64 * 1024 * 1024, "response evidence exceeds bounded size")
                    if line.strip():
                        event_lines.append(line)
                        continue
                    encoded = b"".join(event_lines)
                    event_lines = []
                    if not encoded:
                        continue
                    at = self.now()
                    events.append({"at_seconds": at, "bytes": len(encoded)})
                    data_lines = [x[5:].lstrip() for x in encoded.splitlines() if x.startswith(b"data:")]
                    if not data_lines:
                        continue
                    data = b"\n".join(data_lines)
                    if data == b"[DONE]":
                        continue
                    event = json.loads(data)
                    choices = event.get("choices", [])
                    delta = choices[0].get("delta", {}) if choices else {}
                    part = delta.get("content")
                    if part:
                        require(isinstance(part, str), "nontext SSE content")
                        content += part
                        record["content_event_seconds"].append(at)
                        if record["first_content_seconds"] is None:
                            record["first_content_seconds"] = at
                        if case["expected_termination"] == "CALLER_CANCELLED":
                            record.update(status="PASS", termination="CALLER_CANCELLED")
                            break
                if case["expected_termination"] != "CALLER_CANCELLED":
                    require(case["expected_termination"] == "COMPLETED", "expected timeout did not occur")
                    actual = json.loads(content) if isinstance(case["answer"], (list, dict)) else content.strip()
                    require(actual == case["answer"], "exact representative-content oracle failed")
                    oracle = validate_sse(raw.decode(), self.model, content.strip())
                    require(oracle["usage"] is not None, "requested SSE usage missing")
                    if case.get("counted_prompt_tokens") is not None:
                        require(oracle["usage"]["prompt"] == case["counted_prompt_tokens"],
                                "actual template-inclusive chat usage differs from count endpoint")
                    record.update(status="PASS", termination="COMPLETED", usage=oracle["usage"],
                                  finish_done=True)
        except (TimeoutError, socket.timeout) as error:
            exhausted = record.get("run_budget_exhausted") or (self.run_deadline is not None and time.monotonic() >= self.run_deadline)
            record.update(termination="TIMEOUT", error=f"{type(error).__name__}: {error}",
                          run_budget_exhausted=bool(exhausted),
                          timeout_origin="EXPERIMENT_BUDGET" if exhausted else "REQUEST_DEADLINE",
                          status="PASS" if case["expected_termination"] == "TIMEOUT" and not exhausted else "FAIL")
        except urllib.error.URLError as error:
            timed_out = isinstance(error.reason, (TimeoutError, socket.timeout))
            exhausted = self.run_deadline is not None and time.monotonic() >= self.run_deadline
            record.update(termination="TIMEOUT" if timed_out else "ERROR",
                          error=f"{type(error).__name__}: {error}",
                          run_budget_exhausted=exhausted,
                          timeout_origin=("EXPERIMENT_BUDGET" if exhausted else "REQUEST_DEADLINE") if timed_out else None,
                          status="PASS" if timed_out and not exhausted and case["expected_termination"] == "TIMEOUT" else "FAIL")
        except Exception as error:
            record.update(error=f"{type(error).__name__}: {error}", termination="ERROR")
        finally:
            if response is not None:
                response.close()
            record.update(end_seconds=self.now(), response_bytes=len(raw),
                          response_sha256="sha256:" + hashlib.sha256(raw).hexdigest())
            (stem / "response.sse-or-json").write_bytes(raw)
            self.write_json(stem / "events.json", events)
            if record["status"] == "PASS" and record["first_content_seconds"] is not None:
                try:
                    record = validate_observation(record)
                except ValueError as error:
                    record.update(status="FAIL", error=str(error))
            self.write_json(stem / "observation.json", record)
            with self.lock:
                with (self.output / "observations.jsonl").open("a") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        return record

    def group(self, entries, concurrency):
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            return list(pool.map(lambda args: self.execute(*args), entries))
