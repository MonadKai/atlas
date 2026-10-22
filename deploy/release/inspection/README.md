# Read-only recovery observations

These publisher tools collect evidence and make bounded decisions. They never stop,
start, repair, delete, change a backend or flush caches. An agent separately decides
lifecycle order, resources, maintenance windows and recovery. Run from a fixed
published bundle root with Python3.11+ standard library; no service checkout or pip
installation is required. All output files must be new paths.

## Required paths and mounts

Before a maintenance operation, check the actual files, not only strings returned by
Docker inspect. Pass an explicit `atlas-recovery-path-plan-v1` document:

```json
{
  "schema_version": "atlas-recovery-path-plan-v1",
  "paths": [
    {
      "path": "/site/model/config.json",
      "kind": "file",
      "resolved_path": "/site/model/config.json",
      "symlink_policy": "reject",
      "stat_identity": null,
      "file_identity": {
        "size_bytes": 123,
        "sha256": "sha256:REPLACE_WITH_FIXED_MANIFEST_HASH"
      }
    },
    {
      "path": "/site/model",
      "kind": "directory",
      "resolved_path": "/site/model",
      "symlink_policy": "reject",
      "stat_identity": {"device": 123, "inode": 456},
      "file_identity": null
    }
  ]
}
```

Replace every identity with a fixed publisher manifest or previously saved site
identity. `stat_identity: null` explicitly permits inode/device changes while still
checking resolved location and file bytes. Directory rows verify only that directory,
not all descendants: list every required file separately. `reject` covers symlinks in
all parents as well as the leaf. `allow-fixed` explicitly permits links only to the
supplied fixed resolved path; broken links and loops fail. File hashing checks
before/open/after identity and rejects nonregular files and identity changes.

```bash
python3 -B -E -s -m inspection assets --plan "$PATH_PLAN" --output "$NEW_PATH_EVIDENCE"
python3 -B -E -s -m inspection mounts --expected "$MOUNT_PLAN" --output "$NEW_MOUNT_EVIDENCE"
```

The mount plan is an object with `container_id` (full 64-character hex ID) and `mounts`. Each mount
contains exactly `type` (`bind`, `volume` or `tmpfs`), `source` (absolute for
bind/volume, explicitly null for tmpfs), absolute `destination`, and boolean
`read_only`. Every observed mount destination is retained; a nested tmpfs can mask
model/cache bytes just as a nested bind can. The tool freshly inspects that exact
container, rejects mount identity changes, duplicate destinations and nested mounts
that mask a required path. This supplements file checking; neither observation
proves future availability. Expected snapshots must come from the saved actual
instance, not from an untrusted proposed replacement.

## Cleanup reference guard

Candidates and explicit protected references are nonempty JSON arrays of objects
containing `path` (canonical absolute) and `reason`. Protected references include
models, original recovery materials and shared caches. The tool additionally reads
**all running and stopped** Docker containers and includes each bind/volume source;
stopped containers can still be recovery dependencies. Docker errors never mean an
empty mount set.

```bash
python3 -B -E -s -m inspection cleanup-references \
  --candidates "$CANDIDATE_PATHS" --protected "$PROTECTED_PATHS" \
  --output "$NEW_REFERENCE_EVIDENCE"
```

Both lexical and fully resolved paths are compared in both ancestor directions.
Deleting a parent of a protected model, a child within a shared cache or a symlink
alias is refused. Unresolved/missing references are reported and fail closed rather
than silently discarded. `NO_REFERENCES_OBSERVED` is a snapshot finding, **not**
deletion authorization, ownership proof or integration approval. The agent still
checks exact task ownership and integration, and reobserves just before cleanup.

## Linux, cgroup, NVML and API sampling

```bash
python3 -B -E -s -m inspection resources \
  --container "$FULL_ATLAS_ID" --container "$FULL_BR_ID" \
  --samples 120 --interval-seconds 1 \
  --metrics-url http://127.0.0.1:18336/metrics --output "$NEW_RESOURCE_JSONL"
```

Sampling is finite (at most 10000 samples and one day of declared duration). A sample
contains start/end timestamps and collection duration; it is sequential, **not** an
exact simultaneous measurement. Main init PID and each observed cgroup process RSS
are separate; their shared pages are not summed. cgroup memory.current/stat, swap,
max/high/peak, OOM event counters, CPU, PSI and actual Docker `RestartCount` remain
separate. A race with process exit is unavailable evidence, never zero usage.
Linux `MemAvailable`, `MemFree` and `Cached` keep their original names and meanings.
`nvidia-smi` reports are labeled NVML; UMA may report N/A. No CUDA context is created
by this observer, and no Atlas allocator amount is inferred from system totals.

The optional metrics GET has a 2s timeout, bounded body and numeric allowlist. It
records actual active requests and counters. If this version exports no queue-depth
gauge, the result is `NOT_EXPORTED`; active requests cannot substitute for queued
requests. Absent counters are not silently zero. Compute deltas only between samples
with matching container ID, StartedAt and restart count; preserve unavailable samples
and counter resets. Sampling does not impose performance/stability thresholds.

## Explicit CUDA driver memory probe

This separate command creates its **own short-lived primary CUDA context**, which
has driver/RSS/UMA overhead. It neither attaches to Atlas nor allocates model buffers.
Use only after resource-impact review and explicit operator/agent decision. The
ordinary sampler never invokes it.

```bash
python3 -B -E -s -m inspection.cuda_memory \
  --device 0 --timeout-seconds 10 --minimum-available-bytes 4294967296 \
  --ack-primary-context --output "$NEW_DRIVER_OBSERVATION"
```

The Linux guard cannot be set below 4GiB; the bounded worker rechecks it immediately
before initialization. Device ordinal, driver API version, PID, timestamps, Linux
before/context/after memory and RSS are recorded. One `cuMemGetInfo` read follows
primary-context retain/set; the tool clears/releases it and records return codes.
A timeout (1..30s) or failed cleanup is `UNKNOWN`, with no automatic retry or invented
CUDA values. Forced observer termination, if needed, is limited to its own worker
process; context cleanup then remains unconfirmed. cuMemGetInfo is this process's
context at this timestamp, not proof of Atlas allocation or a historical failure.

On integrated GB10, Atlas's `free_memory` abstraction can use the maximum of driver
free and Linux MemAvailable. Its allocation ledger and co-tenant/page-cache residual
are a different accounting surface; that residual is **not Linux Cached**. Preserve
synchronous source-version-matched startup logs alongside these observations.
Successful later recovery does not identify an earlier deletion actor, cache owner,
OOM cause or exact transient memory condition. Never use global drop_caches, sysctl
changes, deliberate OOM or shared-cache deletion to obtain a favorable result.
