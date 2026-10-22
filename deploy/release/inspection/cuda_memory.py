# SPDX-License-Identifier: AGPL-3.0-only
"""OPT-IN one-shot CUDA memory read with a short-lived own primary context.

Never called by the default resource observer. The operator explicitly accepts context
creation overhead; no device allocation, model load, request, cache flush or service
operation occurs. cuMemGetInfo describes this process context at this timestamp.
"""
import argparse
import ctypes
from datetime import datetime, timezone
import json
import multiprocessing
import os
from pathlib import Path
import time

MINIMUM_AVAILABLE = 4 * 1024 ** 3


def validate_request(device, timeout, minimum_available, acknowledged):
    if type(device) is not int or device < 0 or device > 255:
        raise ValueError("explicit device ordinal 0..255 required")
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 30:
        raise ValueError("explicit timeout 1..30 seconds required")
    if type(minimum_available) is not int or minimum_available < MINIMUM_AVAILABLE:
        raise ValueError("MemAvailable guard must be at least 4GiB")
    if acknowledged is not True:
        raise ValueError("must explicitly acknowledge own short-lived primary context")


def linux_memory():
    result = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in {"MemAvailable", "MemFree", "Cached"}:
            value, unit = rest.split()
            if unit != "kB": raise ValueError("unexpected meminfo unit")
            result[key] = int(value) * 1024
    if set(result) != {"MemAvailable", "MemFree", "Cached"}: raise ValueError("incomplete Linux memory guard")
    return result


def rss():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"): return int(line.split()[1]) * 1024
    raise ValueError("process RSS unavailable")


def driver_worker(connection, device, minimum_available):
    output = {"status": "UNKNOWN", "worker_pid": os.getpid(), "device_ordinal": device,
              "primary_context_retain_succeeded": False, "primary_context_release_succeeded": False,
              "started_at": datetime.now(timezone.utc).isoformat(), "before_linux": linux_memory(), "before_rss_bytes": rss()}
    driver, selected, retained = None, ctypes.c_int(), False
    try:
        if output["before_linux"]["MemAvailable"] < minimum_available:
            raise ValueError("fresh worker MemAvailable below explicit precondition")
        driver = ctypes.CDLL("libcuda.so.1")
        signatures = {"cuInit": ([ctypes.c_uint], ctypes.c_int), "cuDriverGetVersion": ([ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
                      "cuDeviceGet": ([ctypes.POINTER(ctypes.c_int), ctypes.c_int], ctypes.c_int),
                      "cuDeviceGetName": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_int),
                      "cuDevicePrimaryCtxRetain": ([ctypes.POINTER(ctypes.c_void_p), ctypes.c_int], ctypes.c_int),
                      "cuCtxSetCurrent": ([ctypes.c_void_p], ctypes.c_int),
                      "cuMemGetInfo_v2": ([ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)], ctypes.c_int),
                      "cuDevicePrimaryCtxRelease_v2": ([ctypes.c_int], ctypes.c_int)}
        for name, (arguments, result) in signatures.items():
            function = getattr(driver, name); function.argtypes = arguments; function.restype = result
        calls = []
        output["driver_calls"] = calls
        def call(name, *arguments):
            code = getattr(driver, name)(*arguments); calls.append({"function": name, "returncode": code, "at": datetime.now(timezone.utc).isoformat()})
            if code: raise ValueError(name + " returned CUDA error " + str(code))
        call("cuInit", 0)
        version = ctypes.c_int(); call("cuDriverGetVersion", ctypes.byref(version)); output["driver_api_version"] = version.value
        call("cuDeviceGet", ctypes.byref(selected), device)
        name = ctypes.create_string_buffer(256); call("cuDeviceGetName", name, len(name), selected.value); output["device_name"] = name.value.decode(errors="replace")
        context = ctypes.c_void_p(); call("cuDevicePrimaryCtxRetain", ctypes.byref(context), selected.value)
        retained = True; output["primary_context_retain_succeeded"] = True
        call("cuCtxSetCurrent", context)
        available, total = ctypes.c_size_t(), ctypes.c_size_t()
        call("cuMemGetInfo_v2", ctypes.byref(available), ctypes.byref(total))
        output.update(status="OBSERVED", cuda_driver_free_bytes=available.value, cuda_driver_total_bytes=total.value,
                      observed_at=datetime.now(timezone.utc).isoformat(), context_rss_bytes=rss(), context_linux=linux_memory())
    except (OSError, ValueError, AttributeError) as error:
        output.update(status="UNKNOWN", reason=str(error))
    finally:
        if retained:
            clear_code = driver.cuCtxSetCurrent(None)
            release_code = driver.cuDevicePrimaryCtxRelease_v2(selected.value)
            output.update(context_clear_returncode=clear_code, context_release_returncode=release_code,
                          primary_context_release_succeeded=release_code == 0)
            if clear_code or release_code: output.update(status="UNKNOWN", reason="context cleanup failed")
        output.update(after_rss_bytes=rss(), after_linux=linux_memory(), ended_at=datetime.now(timezone.utc).isoformat())
        connection.send(output); connection.close()


def observe(device, timeout, minimum_available, acknowledged):
    validate_request(device, timeout, minimum_available, acknowledged)
    before = linux_memory()
    result = {"schema_version": "atlas-cuda-driver-memory-observation-v1", "status": "UNKNOWN", "device_ordinal": device,
              "timeout_seconds": timeout, "minimum_available_bytes": minimum_available, "parent_pid": os.getpid(),
              "before_linux": before, "started_at": datetime.now(timezone.utc).isoformat(),
              "scope": "opt-in own short-lived process primary context; context creation has memory/driver overhead; no model request or explicit device allocation; not Atlas allocator or historical failure evidence"}
    if before["MemAvailable"] < minimum_available:
        result["reason"] = "MemAvailable below explicit precondition; no CUDA process created"
        return result
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    process = ctx.Process(target=driver_worker, args=(sender, device, minimum_available))
    started = time.monotonic(); process.start(); sender.close()
    try:
        if receiver.poll(timeout):
            try:
                result["worker"] = receiver.recv(); result["status"] = result["worker"]["status"]
            except EOFError:
                result["reason"] = "CUDA observer exited without evidence"
        else:
            result["reason"] = "single bounded CUDA attempt timed out; no retry; context release unconfirmed"
    finally:
        process.join(timeout=max(0, timeout - (time.monotonic() - started)))
        if process.is_alive():
            process.terminate(); process.join(timeout=1)
            if process.is_alive(): process.kill(); process.join(timeout=1)
            result["status"] = "UNKNOWN"
            result["forced_observer_exit"] = True
        receiver.close()
        if process.exitcode != 0:
            result.update(status="UNKNOWN", reason="observer process exited abnormally; no successful context cleanup inferred")
        result.update(worker_exitcode=process.exitcode, elapsed_seconds=time.monotonic() - started,
                      after_linux=linux_memory(), ended_at=datetime.now(timezone.utc).isoformat())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--minimum-available-bytes", type=int, required=True)
    parser.add_argument("--ack-primary-context", action="store_true", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = observe(args.device, args.timeout_seconds, args.minimum_available_bytes, args.ack_primary_context)
        with Path(args.output).open("x") as stream: stream.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return 0 if result["status"] == "OBSERVED" else 1
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
