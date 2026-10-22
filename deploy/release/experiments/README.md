# Bounded voice Agent request experiments

These standard-library tools send and measure requests. They never build, start,
stop, replace, wait for readiness, restart or restore a service. The agent uses
the product's deployment skill and standard Docker/Compose operations separately.
Use the fixed released tools, checked model, runtime image and explicit site
parameters. Record original-resident and new-artifact experiments separately.

## Freeze before execution

State the question, runtime/config/native/model identities, legal fixture,
concurrent service load, sample counts, cache conditions and experimental targets
before sending generation requests. `plans.py` is the source of example targets;
the output plan is the frozen authority for one experiment. Its thresholds are
**EXPERIMENT_ONLY_NOT_BUSINESS_SLO**. A missed target is a retained experimental
FAIL; it is not permission to lower a gate or change the 32K/BF16/prefix-cache/
fast-MoE/no-TurboQuant baseline.

From an independently verified bundle containing this directory and the matching
`acceptance_cases.py` and `acceptance_suites.py`, run these separate atomic commands
with explicit new paths, endpoint and model alias:

```bash
python3 -B -E -s experiments/cli.py example-plan --kind performance --output PLAN.json
python3 -B -E -s experiments/cli.py prepare --plan PLAN.json \
  --fixtures experiments/fixtures.json --endpoint ENDPOINT --model ALIAS --output WORKLOAD.json
python3 -B -E -s experiments/cli.py run --plan PLAN.json --workload WORKLOAD.json \
  --endpoint ENDPOINT --model ALIAS --output NEW_EVIDENCE_DIRECTORY
python3 -B -E -s experiments/cli.py judge --directory NEW_EVIDENCE_DIRECTORY --output REJUDGED.json
```

`example-plan` also accepts `--kind soak`. Inspect, approve and hash the plan before
`prepare`; this materialization sends only actual token-count requests, not
generation. Preserve `WORKLOAD.json.counts/` containing raw count requests/replies.
Freeze/hash the resulting workload before `run`. Long-input requests are fitted
using the matching `/v1/messages/count_tokens`, including the chat template.
Near32K input is 32720–32736 tokens with 32 output slots; preparation rejects a
sum above32768. BEGIN/MIDDLE/END exact JSON marker retention is mandatory.
Every long request has a different frozen leading nonce; actual cached-token
counts, not the nonce's intent, establish whether it was a miss. Cache A/B/A
requests deliberately share the exact prefix and require distinct correct suffix
answers and real reuse after the first observation. The first cache request is
reported as observed, not assumed cold. Cache persistence after restart is not
promised; private SSM state correctness is NOT_RUN.

The performance example contains130 requests:40 sequential short voice replies,
five16K and five near32K requests, five shared-prefix A/B/A groups, five long/short
mixed pairs,20 short requests with two client workers, five automatic tool calls,
and five each of cancellation, deliberate caller timeout and structured error,
each followed by a short recovery request. This is115 normal completions and15
intentional interruptions/errors. Short TTFT p95 target is2s and E2E p95 is8s;
16K/near32K TTFT targets are30/60s; mixed short TTFT is5s. Long/cache scenarios
explicitly have only their TTFT target, without inheriting the short8s E2E target.
No normal transport, protocol or exact-content failures are permitted. Tool
requests use the existing nonstream contract: E2E is measurable; TTFT is unavailable.

The soak example spans at least7200s with a30s offered cycle. It has240 short
requests;48 alternating16K/near32K requests;80 cache A/B/A groups;24 cancellations,
24 errors and24 following short recoveries. Its600 total requests contain552
normal completions. Groups execute sequentially with explicit wall-clock release
times; late work records caller queue delay rather than inventing server queue
timing. A7440s maximum bounds the generator; missing frozen requests fail the
denominator. This is a finite open-loop release schedule with bounded sequential
groups, not an unbounded queue or service controller.

## Evidence and interpretation

Each request preserves exact payload, raw SSE/JSON response, event receive times,
request/response hashes, HTTP status, finish/`[DONE]`, usage, cancellation/timeout
and a separate verdict. The ledger includes failed requests. Rejudgment checks
the frozen schedule ID/case/termination, plan/workload, every raw request against
its exact prepared payload and every successful raw response against its content/
protocol/usage oracle, as well as saved byte hashes. A short request started before
a fault ended cannot count as its recovery. Client pacing and request reads are
bounded by the frozen experiment deadline; exhausting that budget is FAIL, including
for a deliberately timed-out request.
Output directories are never overwritten; retain each failed attempt and choose
a new output path after diagnosis.

Time boundaries use the caller's monotonic clock: scheduled, dispatched to a
worker, HTTP request start, response headers, first nonempty content, each content
chunk and response completion. TTFT is request-start to first content, not headers
or role-only event. Caller queue and scheduled E2E remain separate. Internal server
queue is **UNAVAILABLE**, not derived by subtraction. SSE content chunks are not
guaranteed single tokens: chunk intervals are measurable, token intervals are
**UNAVAILABLE**. Usage-derived output speed is reported separately as an E2E rate
and a decode-interval estimate, not a kernel throughput qualification. A caller
timeout may happen before server admission; its receipt cannot prove backend
cancellation. Subsequent actual short completion is required within30s of each
intentional failure. Use independent server activity observations to establish
resource reclamation, not just a socket closing.

Percentiles use nearest rank and include explicit sample counts. Fewer than100
samples makes p99 observational, not a qualified tail estimate. Exact synthetic
voice replies and marker/tool schemas are representative functional oracles,
not a business accuracy corpus. Compare original-resident and candidate evidence
without transferring qualification between their distinct image/config identities.

Run actual ASR/VAD/denoise requests with their own fixed publisher clients to claim
coexisting computation. Containers merely running do not establish that claim.
Correlate UTC request times and scope host-caller traces separately from Docker
daemon or container-internal coverage. Resource observers remain separate atomic
tools; do not stop competitors to turn a failed coexistence result into PASS.

## Fixed resource trend and recovery scope

The separate pure resource oracle consumes a JSON list with explicit
`elapsed_seconds`, `rss_bytes`, `cgroup_current_bytes`, `restart_count`,
`oom_events`, `active_requests`, `container_id` (full64-character ID) and
`container_started_at` (actual Docker StartedAt) per sample. Identity fields are
mandatory: changing container or StartedAt fails closed before comparing counters.
This explicit identity-bound sample schema supersedes unbound draft samples;
derive it from saved actual observations rather than filling missing identities
with guesses:

Use the publisher normalizer on the saved inspection JSONL before judging. Selection
is explicit: one full container ID, one exact Linux `comm`, and a named actual
`memory.events` counter. It selects the sole matching process's `VmRSS`, not init
RSS or a sum of shared pages. Missing observations, duplicate matching processes,
identity/PID drift, reset counters or a missing active-request gauge fail closed.
The normalizer preserves actual UTC collection windows, reports elapsed time from
the first sample start and binds both each raw row and the complete input snapshot
by SHA. It performs no network call, sampling, service operation or trend verdict.

```bash
python3 -B -E -s experiments/cli.py normalize-resources \
  --raw-observations INSPECTION_RESOURCES.jsonl --container-id FULL_CONTAINER_ID \
  --process-comm spark --oom-counter oom_kill --output RESOURCE_SAMPLES.json
python3 -B -E -s experiments/cli.py resource-trend --plan SOAK_PLAN.json \
  --samples RESOURCE_SAMPLES.json --output TREND.json
```

The selected counter `oom_kill` measures kills, while `oom` measures cgroup OOM
events; choose explicitly and keep that meaning in the report. A normalized partial
run is only valid input transformation, not a two-hour PASS. Use an immutable,
complete newline-terminated JSONL snapshot; an incomplete final write is rejected.
NVML GPU/process tables remain auxiliary raw observations and do not change the
frozen RSS/cgroup trend thresholds or establish a CUDA allocation ledger.

The example freezes600s warmup, first/last900s medians, a2GiB median-growth target
and a1GiB/hour post-warmup OLS slope target. Missing windows/counters fail closed.
An increase in container restart or OOM events fails the bounded trend check.
Positive slope or cache growth alone does not diagnose a leak. Queue drain and
actual post-load recovery require separate observations. Linux MemAvailable,
MemFree, Cached, CUDA driver memory, Atlas allocation ledgers, RSS and cgroup
memory are different quantities; no subtraction here labels residuals Linux
Cached or attributes an OOM retrospectively.

The agent separately freezes each cold-start condition, verifies mount sources
and file identities, records resources, drains/stops, starts/recreates from fixed
artifacts, and performs the publisher's true inference/stream acceptance. Process
cold, filesystem cache warm, engine prefix cache cold and retained JIT cache are
different conditions. No global cache dropping, OOM injection or threshold
relaxation belongs in this experiment. Restore original availability on failure,
preserve diagnostics, and retry only after the agent explicitly decides the next
atomic operation. Publication, health, function, observed performance, bounded
stability, business accuracy and production qualification remain distinct.

## Optional external speech-service co-load

`coload.py` is an optional request-only consumer of **external publisher code**.
It is separate from the Atlas standard-library measurement profile: the selected
consumer interpreter must already provide `websockets==12.0` for the fixed legacy
ASR, BR and TEN clients. Neither `check` nor `run` installs dependencies. The Atlas
bundle does not redistribute their models, native libraries, algorithms or wire
implementations. Obtain the authorized external client/fixture archives at fixed
versions and verify them before binding their files; no service development
checkout is required. The external code is executable trusted input, not sandboxed
by a matching hash. Pin provenance separately from local byte consistency.

The following portable **site template** is intentionally incomplete until every
`REPLACE` value, file size, endpoint and remaining publisher dependency is bound.
The invalid example hosts are not site facts. Add a distinct file descriptor for
every Python sibling module in each selected publisher tool directory, and pin
all fixture/golden files that those tools consume. Prefer the complete published
bundle manifest over selecting a few scripts. Every descriptor requires absolute
canonical `path`, integer `size_bytes` and full `sha256:...`; symlinks, changed
bytes, unbound Python siblings and duplicate path aliases fail the input check.

```json
{
  "schema_version": "atlas-coload-v1",
  "cycles": 20,
  "interval_semantics": "START_TO_START",
  "interval_seconds": 30,
  "max_duration_seconds": 1200,
  "request_timeout_seconds": 60,
  "expected_websockets_version": "12.0",
  "files": {
    "asr_client": {"path": "/released/asr-legacy/verify_ws.py", "size_bytes": 0, "sha256": "sha256:REPLACE"},
    "asr_scorer": {"path": "/released/asr-tools/asr_probe.py", "size_bytes": 0, "sha256": "sha256:REPLACE"},
    "asr_audio": {"path": "/released/asr-tools/zh-16000.wav", "size_bytes": 0, "sha256": "sha256:REPLACE"},
    "asr_fixtures": {"path": "/released/asr-tools/fixtures.json", "size_bytes": 0, "sha256": "sha256:REPLACE"},
    "br_client": {"path": "/released/br-legacy/benchmark_v2_server_load.py", "size_bytes": 0, "sha256": "sha256:REPLACE"},
    "br_audio": {"path": "/released/audio/strong16.wav", "size_bytes": 0, "sha256": "sha256:REPLACE"},
    "ten_client": {"path": "/released/ten-tools/tests/run_golden.py", "size_bytes": 0, "sha256": "sha256:REPLACE"}
  },
  "asr": {
    "endpoint": "ws://asr.example.invalid:8080/v1/ws",
    "language": "zh", "fixture_name": "zh-16000.wav",
    "chunk_delay_seconds": 0.1,
    "maximum_error_rate": 0.6, "maximum_increase": 0.15
  },
  "br": {
    "endpoint": "http://denoise.example.invalid:8080",
    "semantic_profile": "v024-non-ja", "suppression": "balanced",
    "sample_rate_hz": 16000,
    "input_frames_per_message": 320, "output_frames_per_message": 320
  },
  "ten": {"endpoint": "ws://vad.example.invalid:8080/"}
}
```

From the verified Atlas bundle root and the selected interpreter:

```bash
SELECTED_PYTHON -B -E -s experiments/coload.py check --site FROZEN_SITE.json
SELECTED_PYTHON -B -E -s experiments/coload.py run --site FROZEN_SITE.json \
  --output NEW_COLOAD_EVIDENCE_DIRECTORY
```

`check` hashes inputs and verifies the interpreter, dependency and ASR
audio/reference/publisher-policy binding; it imports no external request client
and sends no request. `run` freezes a copy of the site, records its digest and
the runner's SHA, then invokes exactly one ASR, one BR and one TEN publisher job
per cycle, sequentially. Each job runs in a separate bounded request worker.
Failures remain in the ledger and do not trigger service repair or silently
disappear from the denominator. A worker timeout is a failed attempt; later
requests still execute when the fixed total budget permits them.

Cadence is explicitly **start to start** from the original monotonic experiment
origin, not a sleep after cycle completion. Each receipt retains scheduled start,
actual cycle start, lateness, worker interval and raw publisher result. A slow
cycle does not reset the schedule or hide missed times. Twenty cycles at 30s are
scheduled at 0..570s; 121 cycles at 60s are scheduled at 0..7200s, with an explicit
7440s budget available for completion. These are example finite schedules, not
automatically a two-hour PASS: require actual completed requests spanning the
frozen interval, identify overlap with the Atlas workload, and retain failures.
Imports and hash checks contribute worker wall time; publisher timing fields
retain their own narrower boundaries. Missing cases fail the final summary.

The unchanged ASR client runs full-duplex and validates its exact completion,
audio accounting and connection behavior. The published reference scorer checks
the observed transcript; nonempty output is insufficient. Site thresholds cannot
exceed the pinned publisher fixture policy. The unchanged BR `one_batch` consumes
its advertised profile/framing and validates ready/QID, PDN2 audio ranges and
full finish drain. The unchanged TEN `run_golden.run` compares all three complete
golden transcripts, including speech events and termination. These checks delegate
service semantics rather than implementing another ASR/BR/VAD oracle in Atlas.

The example BR job uses `balanced` on the original BR service; it is **not** a
Triton/BR16K compute test. A separately frozen strong BR16K site requires its own
real remote-call and Triton inference-statistic evidence. Likewise, ASR backend
inference logs, actual GPU execution observations and resource samples must be
collected independently in the same request window; these clients alone do not
prove GPU execution. A running container is insufficient. Keep private business
client exemptions and original/resident versus new-image qualification scopes
explicit. No stop, start, replacement, readiness loop or recovery operation is
part of this co-load runner.

## GB10 Qwen3.6 case study: observations and unresolved hypotheses

The 2026-10-22 experiment compared the original `d8229456` runtime and the
previously built `963f71b9` runtime, each with the same hash-bound Qwen3.6-35B-A3B
NVFP4 local asset set, 32768 context, BF16 KV, prefix caching, fast MoE layout,
4096 maximum prefill tokens and no TurboQuant. The release manifest binds full
source, image/config, native and model identities. Site-specific configuration,
maintenance windows, frozen inputs, raw evidence and qualifications are recorded
in `xd-infra/host/dgx-spark-1/atlas-reliability-20261022/`; this method is not a
second current deployment registry. Local SHA consistency does not authenticate
the upstream-to-cloud-to-host provenance chain, which was not verified.

The question was whether sequential recoveries and realistic mixed request load
exposed a reproducible availability or latency limitation. Three process-cold
new-runtime recreations used retained filesystem/JIT caches without dropping
shared caches. Every recreation passed actual inference, SSE and prefix A/B/A
isolation; the original runtime was restored separately. No changed algorithm,
backend, memory threshold or context length was used to obtain those results.

Each frozen 130-request performance run produced 115 normal completions and
15 expected interruptions/errors, with no failed normal completion. Short-reply
TTFT p95 was about 0.37s. Five near32K requests per runtime had TTFT p95 about
14s. However, all five long-prefill/short-reply pairs per runtime violated the
frozen mixed-short TTFT 5s and E2E 8s targets: p95 was approximately 14.3–14.5s.
The performance experiment therefore remains **FAIL**. Actual ASR, BR balanced
and TEN VAD publisher requests overlapped the Atlas request windows. The BR
profile does not establish Triton computation.

Short mixed requests began while their long partner was in flight and produced
first content after that partner completed. This supports a head-of-line or
shared-scheduler contention hypothesis; internal queue timestamps were unavailable
and the exact cause remains unproven. The original and new runs differed in CPU
limits, tracing, host time and cache history. Their small latency differences
are not a controlled engine speed comparison. Per-scenario sample counts below
100 do not qualify p99 or business accuracy.

An integer-count shutdown fixture ended early under the existing content-loop
watchdog. Changing the bounded request fixture to prose, while retaining the
existing 512-token maximum and leaving the engine unchanged, allowed an actual
active-request SIGTERM/drain observation on the original runtime. Two earlier
observation timing misses remain NOT_ESTABLISHED. The request fixture, server
active gauge, signal time, client completion, drain log, exit status and subsequent
real recovery must all be retained before asserting graceful active drain.

Historical BR asset disappearance has no established actor or exact time.
Historical Atlas startup memory rejection, CUDA initialization error and missing
assets are separate observations; page-cache residuals and later free memory do
not establish their causes. The preventive response is read-only hash/mount/path
reference checks before lifecycle actions and synchronous resource evidence at
failure, rather than an invented causal narrative.

The separately frozen two-hour workload and resource oracle measure only the
recorded runtime, duration and actual co-load. A successful original-runtime soak
cannot qualify the new runtime or establish long-term stability, production
readiness or a business SLO. Consult the immutable site experiment receipt for
the completed duration, all failures and the final bounded verdict.
