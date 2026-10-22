# Private GB10 Qwen3.6 NVFP4 artifacts

This fork-owned path publishes one ARM64 / NVIDIA GB10 / Qwen3.6-35B-A3B-NVFP4
**development** artifact. It preserves the CUDA 13.0 native path and the current
model/quantization. It does not certify the upstream all-model matrix, production,
performance, long-term stability or business accuracy. The upstream `avarok-release`
public release and benchmark gates remain unchanged. Never run sync-pr, move public
latest/dev/nightly, push upstream or dispatch public release workflows for this path.

Release owns artifacts. Deploy owns an instance. An agent, human or CI sequences the
atomic operations below; none implicitly stops, deploys, waits for or rolls back a
service. Docker/Compose supply declarative health, restart and graceful-stop behavior.

## Obtain and verify an immutable bundle

A trusted release locator must fix the data-image reference **by remote manifest
digest**, its independent config ID, and `release.json` / `bundle-manifest.json`
SHA-256. Internal hashes establish consistency after the locator is trusted; they do
not authenticate an arbitrary publisher. Pull the pinned data image, create an
**unstarted** extraction container with `--entrypoint /never-run`, copy `/bundle/.`
to a new physical directory, then remove that extraction container. No model bytes
are distributed in this bundle. Do not install a skill from a source checkout or a
symlink. All consumer commands below use only that directory, local model files and
site parameters, with Python3.11+ standard library and Docker/Compose for Atlas operations. The
optional external co-load adapter requires its explicitly selected publisher-client
environment (see `experiments/README.md`); it never installs packages. No pip install,
Atlas checkout, other service repo or company infrastructure checkout is needed.

Before executing any extracted Python file, independently check the locator's two
JSON hashes and every `bundle-manifest.json` member (including sizes where listed).
Reject symlinks, missing/extra members and Git LFS pointers. Then use:

```bash
python3 -B -E -s "$BUNDLE_ROOT/toolkit.py" inspect
```

`product-release-v4`, `product-deployment-capability-v2`, `service-docker-v3` and
`service-release-qualification-v2` are the existing mounted-asset artifact formats.
The single component is `engine`; the explicit profile is
`gb10-qwen36-nvfp4-cu130-development`. `service-instance-v5` is this product's first
mounted-instance schema, passed opaquely by generic consumers; it does not reinterpret
or replace earlier products' instance schemas. Source and tool revisions may differ:
the actual binary's source commit is separate from the committed delivery tooling.

`inspect` also checks relationships inside the delivery: the compile receipt binds
input-lock and shipped compiler/native profile bytes, actual Cargo and fresh PTX
logs, all eight native library identities and the runtime binary. The exact remote
manifest binds its config ID and digest-pull receipt. Receipt paths are historical
metadata; these checks read only shipped evidence members, never the former host
paths. Recomputing member hashes does not waive source/image/profile consistency;
a trusted external locator remains necessary for publisher authenticity.

## Direct consumption (path A)

Read the packaged `skills/atlas-private-deploy/SKILL.md`. Admit the actual host,
GPU/runtime, free UMA memory, disk and CPU before mutation. Save original container,
manifest/config IDs, complete launch inputs, model/cache mounts and recovery method.
A 64GiB container limit does not prove 64GiB free physical capacity. Schedule model
instances sequentially when aggregate memory is insufficient; drain traffic before
an authorized maintenance window. Never stop an unknown or another task's instance.

Supply a JSON instance with **all** of these fields (replace every example with
explicit site facts; paths must already be real directories):

```json
{
  "schema_version": "service-instance-v5",
  "release_scope": "development",
  "project_name": "site-chosen-name",
  "container_name": "site-chosen-name",
  "host_ip": "127.0.0.1",
  "host_port": 18736,
  "manager": "site-chosen-manager",
  "binding_id": "site-chosen-binding",
  "components": {
    "engine": {
      "cpuset": "5-9,15-17",
      "cpu_limit_millicores": 8000,
      "memory_limit_bytes": 68719476736
    }
  },
  "uid": 1000,
  "gid": 1000,
  "model_root": "/absolute/verified/model",
  "cache_root": "/absolute/task-owned/cache",
  "model_alias": "qwen3-6-35b-a3b"
}
```

The CPU set is an example, not a portable default. Resources must satisfy the
published floors, actual site placement and admission. Cache must be writable and
owned by the declared non-root UID. It cannot overlap the read-only model directory.
The original model alias is preserved. This development artifact binds IPv4 loopback;
legacy wildcard exposure is a separately retained recovery fact, not silently granted
by this artifact. Generic external consumers pass UID/GID/model/cache/alias as opaque
`site-binding-v3.docker.runtime_parameters`, with neutral names/resources kept separate.

```bash
python3 -B -E -s "$BUNDLE_ROOT/toolkit.py" verify-assets \
  --instance "$INSTANCE_JSON" --output "$NEW_ASSET_RECEIPT"
python3 -B -E -s "$BUNDLE_ROOT/toolkit.py" render \
  --instance "$INSTANCE_JSON" > "$COMPOSE_JSON"
docker compose -f "$COMPOSE_JSON" config --quiet
```

Rendering does not authorize mutation. Collect a complete
`owned-resource-snapshot-v1` of every declared container/network/volume using standard
read-only Docker inspection; distinguish truly absent resources from permission,
context, daemon and network failures. Then check ownership with
`toolkit.py guard --instance ... --desired ... --snapshot ...`. Protect both product
and neutral manager/binding labels. After existing authorization, the agent executes
`docker compose ... up -d`, observes readiness, and runs:

```bash
python3 -B -E -s "$BUNDLE_ROOT/toolkit.py" verify \
  --instance "$INSTANCE_JSON" --endpoint "$BOUND_ENDPOINT" --output "$NEW_EVIDENCE_DIR"
```

`verify` checks the actual immutable image/config, full declared runtime including
inherited environment, installed native/code files, all local model hashes and real
public requests. GPU device descriptors/library mappings alone are not kernel
execution. A separate short request-correlated Nsight capture can be parsed with
`acceptance_profile.py --sqlite ... --pid ... --output ...`; the optional profiler
installation is a separately pinned diagnostic input, not an engine dependency.
Use no extra privilege or host policy change merely to make a profiler work.

The external path B follows the same fixed contract and toolkit using a physical,
versioned deployment-skill archive. It performs real admission, binding, ownership,
Compose deployment and publisher acceptance; it never builds Atlas or implements an
Atlas algorithm check. A/B may share one owned instance when complete normalized
Compose, image/config/container and ownership identities are proven equal. Trace
claims must state whether they cover the host caller, Docker daemon or container.

## Explicit acceptance and recovery

Run publisher requests independently; these clients never control Docker:

```bash
python3 -B -E -s "$BUNDLE_ROOT/acceptance.py" --base-url "$BOUND_ENDPOINT" \
  --model qwen3-6-35b-a3b --suite context --output "$NEW_CONTEXT_EVIDENCE"
python3 -B -E -s "$BUNDLE_ROOT/acceptance.py" --base-url "$BOUND_ENDPOINT" \
  --model qwen3-6-35b-a3b --suite cache --output "$NEW_CACHE_EVIDENCE"
```

Other suites are `public`, `stream` and `errors`; frozen expectations and lawful
synthetic fixtures are in `fixtures/cases.json`. Representative factual/arithmetic,
automatic function-call and content-retention checks are distinct from business
accuracy. Save raw requests/responses/SSE and failures, not just a nonempty answer.
Do not reduce an oracle after observing a miss.

32768 includes the actual chat template and prompt plus generated slots. The context
suite measures through the matching count endpoint and confirms actual chat usage,
requires strict beginning/middle/end marker retention near the boundary and tests
prompt>=32768 structured rejection followed by recovery. The current server does not
promise a preflight400 for prompt+max_tokens>32768; generation is bounded by the
scheduler. The earlier ~29K check is not this boundary qualification.

Cache A/B/A suffix tests require real cached-token evidence and distinct correct
answers; they do not certify every private SSM snapshot. MTP stays auto-gated;
Accepted prediction tokens come from the actual scheduler and are recorded separately
from structural presence of its19weights. This source version hardcodes the rejected
prediction-token field to zero; that field cannot establish an observed zero rejection rate. Do not force experimental paths to manufacture a result.

`observe_shutdown.py` is only a client: its ready-file means real streamed content,
not just a role event. The agent separately verifies an active request and issues
standard SIGTERM/Compose stop, checks refusal of new TCP admission, drains within the
existing15s engine window and verifies exit/absence of OOM. A complete finish and
`[DONE]` is different from an explicit error or broken connection. Container grace
is120s; existing keepalive connections are not promised an immediate503.

After saving logs/evidence, restart and check actual inference/SSE and cold versus
warm prefix counts. Cache persistence across restart is **not promised**. For an
independent recreation, remove only the exact owned test instance after preserving
its identity/evidence; use the same fixed bundle, verified models and saved site
inputs to render/create a new instance, then re-run actual acceptance. Original
resident start and new artifact recreation are separate results. On failure restore
the original service first, preserve failure evidence and diagnose without silently
changing the backend, precision, model or qualification.

## Publisher-only build and release

Read the packaged private release reference (or the same source-owned
`.claude/skills/avarok-release/references/private-gb10.md`). Publishing to the private
registry is explicitly authorized by the operator for this task. This is not approval
to touch upstream or public release surfaces. Use only your own fork origin and
non-public-release git tag prefixes. PRs record What/Why/Benchmarks/Authorship, actual
local checks and narrowly scoped evidence; protected branch requirements remain real.

The fixed build profile preserves Rust1.93.1, nvcc13.0.88, GB10/qwen3.6-35b-a3b/nvfp4,
sm_121f, flags and eight native libraries. The optional CUDA13.2 builder path is not
selected. CUDA/host compiler, headers, Cargo.lock, native trees, kernel closure and
licenses are bound by size/SHA or Git object identities. No key optimization is
silently disabled. Prebuilt external optional CUTLASS/FlashInfer inputs remain absent
exactly as on the retained native baseline, not a newly disabled feature.

`build.py inspect-inputs`, `compile` and `runtime-context` are separate source-owned
operations; `compile` requires a clean full fixed source commit and initially empty
external target, and performs locked/offline actual Cargo/PTX compilation. Cargo
registry/git cache may be reused; old compiled spark is not a new build. Keep a
separate target for GPU-free checks (AVAROK_SKIP_BUILD); never package a stub.
`runtime-context` packages only the actual new binary, fixed native libraries,
source archive, license notices and exact receipts. Then standard
`docker build --pull=false --network=none --platform linux/arm64` builds the pinned
runtime Dockerfile, with explicit source/version/binary-SHA build arguments. Input
pinning, build success, runtime success and byte reproducibility are separate claims.

Before private publication, `instance.render_contract` is the source-owned public
pure declaration API for a local development probe; it validates lexical paths and
instance fields without observing the filesystem or host process identity. A
publisher must separately obtain fresh `instance.observe_paths` observations and
apply `instance.validate_path_observations` before creating a probe. Existing real
model/cache directories, absence of symlinks, cache ownership and write access
remain mandatory. The pure declaration grants neither admission nor published
artifact qualification. Published consumption uses `toolkit.py render`, whose
boundary validates the approved closed bundle and collects/checks those path
observations; `verify-assets` and runtime verification retain that boundary gate.
Record an actual local model probe and image
identity before artifact approval. `image.py publish` is a single-artifact private
push/readback/digest-pull operation, never deployment; it rejects tag replacement and
keeps remote manifest digest distinct from config ID. It can publish runtime or
release data-image independently of company checkouts.

`pack.py` exports fixed committed tool blobs, runtime/protocol/model/native identities
and exact build/publication/probe evidence into a new closed bundle, using existing
v4formats. It does not build/push/deploy. Publish that data image at a new immutable
private coordinate; record both source/tool commits and every remote digest/config.
All declared members must be independently extractable and hash-matching. Git evidence
must be checked from a clean committed archive, not merely present in a working tree.

Company deployment facts belong in xd-infra; other consumers can use their own
registry. This repository requires neither xd-infra nor xd-model-deploy-skills to
build, publish, deploy from artifacts or verify Atlas. Keep original failures,
NOT_RUN scopes and old qualifiers; never promote local byte consistency to cloud
provenance authentication. Cloud object identities and upstream-cloud-local source
chain remain NOT_VERIFIED_THIS_ROUND in this release.

## Atomic recovery inspection and voice-agent experiments

Bundles explicitly declaring `atlas-reliability-tools-v1` or
`atlas-reliability-tools-v2` additionally ship
[read-only inspection](inspection/README.md) and
[bounded request measurement](experiments/README.md). Their schemas, commands,
frozen experimental targets and limits live in those guides. Older v4 bundles
without the declaration retain their original closure and remain supported.
The extension changes tools, not the runtime image, model, inference backend,
public protocol or production qualification. Obtain a new immutable tool bundle
when the tools change; do not modify an already published extraction in place.

These clients do not stop, start, replace, wait for or repair a service. Agents
use separate standard platform operations, preserve original recovery identities,
and label observations per actual image/config/parameters. Bind paths must exist
and match their manifests before a stop, even if Docker still shows a healthy
process using now-unlinked files. Cleanup references include both lexical and
resolved ancestor/descendant relationships and stopped recovery instances.

The v2 tool set adds `experiments/resources.py`, required by its normalization
CLI. The v1 tool closure remains unchanged and does not retroactively require
this module. Consumers validate the tool set declared by their exact bundle;
removing a required v2 module fails before execution. Normalization checks raw
identity and accounting, while the separately frozen trend oracle decides scope.
