#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Stdlib HTTP evidence boundary. No Docker, installs, or lifecycle operations."""
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


class Recorder:
    def __init__(self, base_url, output, timeout):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or
                parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.path.rstrip("/") not in ("", "/v1")):
            raise ValueError("base URL must be an HTTP(S) origin or /v1 URL without credentials")
        self.origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        self.output, self.timeout, self.index = Path(output), timeout, 0
        self.deadline = time.monotonic() + 880
        self.receipts = []

    def request(self, path, payload=None, *, raw=None, stream=False, cancel=False):
        self.index += 1
        stem = f"{self.index:03d}"
        body = raw if raw is not None else (json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None)
        if body is not None:
            (self.output / f"{stem}.request.json").write_bytes(body)
        start = time.monotonic()
        metadata = {"path": path, "method": "POST" if body is not None else "GET",
                    "request_sha256": hashlib.sha256(body).hexdigest() if body is not None else None}
        req = urllib.request.Request(self.origin + path, data=body,
                                     headers={"Content-Type": "application/json"})
        data = bytearray()
        response = None
        try:
            try:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("suite time budget exhausted")
                response = urllib.request.urlopen(req, timeout=min(self.timeout, remaining))
            except urllib.error.HTTPError as error:
                response = error
            metadata.update(http_status=response.status,
                            content_type=response.headers.get("Content-Type", ""))
            if stream and response.status == 200:
                while True:
                    if time.monotonic() >= self.deadline:
                        raise TimeoutError("suite time budget exhausted")
                    line = response.readline()
                    if not line:
                        break
                    data.extend(line)
                    if cancel and line.startswith(b"data:"):
                        try:
                            event = json.loads(line[5:])
                            choices = event.get("choices", [])
                            if choices and choices[0].get("delta", {}).get("content"):
                                metadata["caller_closed_after_content"] = True
                                break
                        except (ValueError, AttributeError, IndexError, TypeError):
                            # The pure SSE oracle will reject malformed frames.
                            pass
            else:
                data.extend(response.read())
        except Exception as error:
            metadata["transport_error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            if response is not None:
                response.close()
            suffix = "sse" if stream else "response"
            (self.output / f"{stem}.{suffix}").write_bytes(data)
            metadata.update(elapsed_seconds=time.monotonic() - start,
                            response_sha256=hashlib.sha256(data).hexdigest(), response_bytes=len(data))
            write_json(self.output / f"{stem}.receipt.json", metadata)
            self.receipts.append(metadata)
        text = data.decode("utf-8")
        if stream or metadata["content_type"].startswith("text/"):
            result = text
        else:
            result = json.loads(text)
        return metadata["http_status"], result, metadata

    def json(self, path, payload=None):
        status, result, _ = self.request(path, payload)
        if status != 200 or not isinstance(result, dict):
            raise ValueError(f"expected HTTP200 JSON at {path}; actual HTTP{status}")
        return result
