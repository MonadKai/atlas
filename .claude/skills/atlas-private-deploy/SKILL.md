---
name: atlas-private-deploy
description: >-
  Deploy and accept a fixed private Atlas GB10 ARM64 Qwen3.6 NVFP4 development
  bundle using verified local models and explicit site inputs, without an Atlas
  source checkout. Covers independent recovery; excludes builds and upstream release.
---

# Deploy a private Atlas product bundle

This is the direct product consumption path. Use a fixed published bundle,
verified local models and explicit site inputs. No Atlas development checkout,
xd-model-deploy-skills checkout or xd-infra checkout is required to deploy and
verify the product. Company deployment facts still belong in xd-infra; other
users may use their own registry. Build and publication belong to the
[publisher-only release route](../avarok-release/references/private-gb10.md),
and do not implicitly start or replace an instance. Run consumption commands
from the extracted BUNDLE_ROOT; its root README.md is the authoritative consumer
guide. The shipped skills and tools remain usable without the source tree.

## Verify inputs and plan the instance

Obtain the trusted publisher locator, runtime image and data-only bundle image
by immutable manifest digest; retain their distinct image/config IDs. Extract the
bundle with a stopped `docker create` / `docker cp`, then remove that extraction
container. Verify the locator's release and bundle-manifest hashes and every
member before executing the matching tools. Missing members, Git LFS pointers,
symlink escapes, platform/config mismatches and altered fixtures fail closed.

Use the bundle's Python prerequisites; do not install unpinned packages during
acceptance. From the bundle directory, inspect `toolkit.py --help` and run:

```bash
python3 -B toolkit.py inspect
python3 -B toolkit.py render --instance instance.json > compose.json
python3 -B toolkit.py verify-assets --instance instance.json --output assets.json
docker compose -f compose.json config --quiet
```

Copy the provider's explicit instance schema, using the exact release and local
model identity, owned cache, UID/GID, CPU set, resource limits, access policy and
model alias. Required values have no implicit site defaults. Model files remain
read-only; runtime cache is separately writable. Local SHA validation establishes
asset consistency, not cloud/upstream provenance. Preserve distinct HF/ModelScope
revisions and the recorded unverified source-chain scope.

Before mutation, compare current dependencies, active requests and CPU/GPU/UMA,
swap and disk headroom. Save the original container, image, full configuration,
model/cache mounts, access policy and recovery inputs. Prefer an isolated loopback
test instance and sequential GPU windows when two full models would exceed capacity.
Use the released `guard` with a complete resource snapshot and desired declaration;
never adopt or relabel an existing foreign container/network/volume:

```bash
python3 -B toolkit.py guard --instance instance.json \
  --snapshot owned-resources.json --desired compose.json
```

## Deploy and accept

The agent separately pulls the runtime image by digest, runs standard Compose,
observes readiness and invokes the provider verifier:

```bash
python3 -B toolkit.py verify --instance instance.json   --endpoint http://127.0.0.1:TEST_PORT/v1 --output acceptance-new
```

Use the actual explicit endpoint, replacing TEST_PORT with the bound port. Run
additional released acceptance suites for the required scope; do not infer full
32K boundary, content retention, cache isolation or private SSM/MTP correctness
from a short response or prefix-cache counter. Preserve auto-gated speculation
and the released backend/quantization/settings rather than forcing an experimental
path. Actual GPU/native execution needs its own evidence.

The shipped request-only acceptance interface is:

```bash
python3 -B acceptance.py --base-url http://127.0.0.1:TEST_PORT/v1 \
  --model PUBLIC_ALIAS --suite context --output context-new
```

The explicit suites are `public`, `context`, `cache`, `stream` and `errors`.
Use a new output directory per suite/attempt and the exact public alias. These
request suites never start or stop a container; lifecycle operations remain agent
steps with separate observations. Their fixture identity and raw replies remain
part of each verdict.

Record new product acceptance separately from compatibility with the original
public callers. Verify alias, fields, response/usage/error schema, SSE frames and
termination, representative expected content, cancellation and follow-up recovery.
Only test optional capabilities that are already part of the promised service.
The fixture and scorer determine verdicts; do not loosen them after failure.

## Exit, recreate and recover

Agent operations remain separate: observe/drain requests, stop, inspect exit and
new-request refusal, then restart or remove/recreate only owned resources. Use
the shipped request/observer tools while standard Docker/Compose controls the
lifecycle. Do not add an all-in-one stop/build/push/deploy/wait/rollback script.

For independent recovery, retain only the fixed bundle, verified local model and
saved site parameters; recreate with its declared image/config and compare the
full actual identity. Run real inference, streaming and required cache checks
afterward. Restarted cache behavior must be measured; persistence is not assumed.
Original-version restoration and new-product recreation are separate results.
If the candidate fails, restore the original usable public service first, retain
the failure and diagnose before retrying. Preserve the original wildcard address,
port, `/v1` paths, public alias and access method unless a separately authorized
change has passed compatibility and functional acceptance.

Register the actual state and maintenance window. Distinguish artifact publication,
health, wire compatibility, representative function, backend execution and recovery
from performance, stability, business accuracy and production qualification. The
private development route does not pass the upstream all-model gate. Clean only
safely integrated task-owned resources; keep immutable products, models, native
inputs, valid caches and failure/recovery evidence.
