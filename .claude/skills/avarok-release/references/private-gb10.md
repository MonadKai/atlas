# Private GB10 single-model development delivery

This route applies only to an explicitly authorized **MonadKai/atlas fork** and
private-registry release of **GB10 ARM64 Qwen3.6-35B-A3B-NVFP4**. It reuses Atlas
build and verification primitives with a closed product handoff. It does not run
upstream sync, write to Avarok-Cybersecurity/atlas, publish public images or move
public latest/dev/nightly tags. The public release skill's all-model coherence,
serve-matrix and human-controlled shipping rules remain unchanged.

A private development artifact-release PASS is narrower than an upstream shippable
image, runtime acceptance or production qualification. User authorization for this
private route does not relax technical identity checks or protected-branch rules.

## Source, build environment and qualification boundary

Freeze a clean committed fork revision and inspect remotes/default branch before
creating a task worktree. Record the full source and tool revisions separately,
including any reused image's actual provenance. Do not presume a prior observed
SHA is current. Preserve other worktrees, in-flight work and existing immutable tags.

The build input authority is `deploy/release/build_profile.json` and its inspected
lock. The current private profile intentionally retains the existing CUDA 13.0 /
Rust 1.93.1 / sm_121f path; selecting a different Dockerfile because it mentions
CUDA 13.2 would change the input set. The profile fixes the runtime base by digest,
Cargo/source closure, compiler/runtime trees, native libraries, kernel target and
required flags. Do not globally upgrade a host toolchain or disable optimized paths.
Missing or changed pins require diagnosis and reviewed input evolution, not an
unrecorded fallback. The driver libcuda is injected at runtime, never copied from
an unrelated project or mistaken for a bundled native dependency.

Use the declared pre-existing build environment and explicit task-owned outputs.
These are separate atomic operations; substitute explicit absolute paths and full
committed SOURCE_SHA, preserving each original receipt and failed attempt:

```bash
python3 deploy/release/build.py inspect-inputs \
  --source SOURCE --source-revision SOURCE_SHA \
  --profile SOURCE/deploy/release/build_profile.json \
  --rust-toolchain RUST_ROOT --cuda-home CUDA_ROOT --output input-lock.json
python3 deploy/release/build.py compile \
  --source SOURCE --source-revision SOURCE_SHA --input-lock input-lock.json \
  --target-dir NEW_TARGET --cargo-home EXISTING_CARGO_CACHE --output build.json
python3 deploy/release/build.py runtime-context \
  --source SOURCE --build-receipt build.json --context NEW_CONTEXT --output context.json
```

Compilation builds the fixed source; cache reuse is limited to what the receipt
actually records. Runtime-context checks the binary, linked native closure and
kernel proof before copying inputs, notices and source archive into a new offline
image context. Build that context using its Dockerfile, explicit platform,
ATLAS_SOURCE_REVISION, ATLAS_VERSION and ATLAS_BINARY_SHA256. Preserve the exact
build argv and image identity. A label-only relay or wrapper around the old image
is not a new source build. Input fixation, successful compilation, runtime success
and byte-for-byte reproducibility are separate verdicts.

Run applicable repository checks against the clean final commit. Keep full-model
qualification NOT_RUN for this scoped route. If a diff reaches PERF_PATHS, apply
the existing benchmark/oracle rules; do not recategorize a core change as tooling.
A regression that requires inference/core/kernel changes is a separately scoped
change after restoring the original service, not an implicit extension here.

## Private images and closed product bundle

Keep release and instance operations separate. Build and publication need no target
Kubernetes context and must not implicitly restart a service. Publish only new
immutable tags under the authorized private registry coordinates; distinguish
remote manifest digest from local image/config ID. Check remote tag absence before
publication; auth/network failures are not evidence that a tag is absent. Push,
read the remote manifest, pull by its digest and verify config/platform as separate
operations. Standard Docker commands or an optional existing image-handoff publisher
may perform transport; the Atlas product remains releasable without a consumer
or infrastructure checkout.

The pack operation reads committed Git blobs at a clean TOOL_SHA HEAD. BINARY_SHA
is the actual compiled source revision and may be an ancestor; do not substitute
the later tool revision into the binary's history. Preserve the publication's
adjacent `remote-manifest.json` and the original compile/kernel logs referenced by
the compile receipt; pack verifies and includes their bytes. For example:

```bash
python3 SOURCE/deploy/release/pack.py --source SOURCE \
  --source-revision BINARY_SHA --tool-revision TOOL_SHA \
  --release-ref RELEASE_REF --service-version VERSION \
  --image-record image-build.json --publication-record publication.json \
  --compile-receipt build.json --input-lock input-lock.json \
  --probe-record local-runtime-probe.json --output NEW_BUNDLE
```

It binds the build/publication/local-probe receipts and complete runtime/native/
model/fixture/tool closure. A failed pack retains its partial output; retry into a
new directory after diagnosis. Do not fabricate probe or qualification receipts
to make pack pass. The data-only bundle uses product-release-v4,
product-deployment-capability-v2 and service-docker-v3; preserve the established
artifact-release development / production not_qualified qualification distinction.

The bundle contains all matching toolkit modules, clients, fixtures, skills,
licenses, contracts and checks, not Git LFS pointer placeholders or private
uncommitted files. Verify release and bundle hashes, every required member and
absence of undeclared additions after independent extraction. Also prove Git-hosted
evidence is in the final commit using a clean archive, not only the working tree.
Publish the data-only bundle by its own immutable digest; record its config ID
separately from the runtime image and ship the locator with both retrieval identities.

Use verified local model assets as fixed inputs. Bind every required relative path,
size and SHA-256, explicit mount/access and license/acquisition requirements. Keep
HF revision, ModelScope revision and local directory shorthand distinct. Local
hash matches prove consistency; cloud object identity and the upstream-to-cloud-to-
local source chain remain NOT_VERIFIED_THIS_ROUND unless actually checked. Preserve
valid weights and caches rather than reacquiring them for publication.

## Real consumption and recovery

Use [atlas-private-deploy](../../atlas-private-deploy/SKILL.md) for path A solely
from the released bundle, verified local models and site inputs. Path B uses a
fixed physical consumer-skill archive with the same bundle and explicit neutral
admission/site-binding-v3. The generic consumer is loopback-only; do not silently
widen that policy to claim a resident wildcard migration. Reusing one isolated
instance requires equal complete declarations, common ownership, actual external
Compose deployment and matching image/container/resource identities.

Freeze legal fixtures, expected behavior and verdicts before the GPU window.
Invoke the version-matched toolkit for functional acceptance and the released
acceptance suites individually for context, prefix-cache isolation, stream/cancel
and errors. Scope 32K by the tokenizer/template/input/output budget and test both
legal near-boundary and rejected overflow requests; a previous ~29K result is not
a full boundary PASS. Check representative retained content, not only success or
nonempty text. GPU/native execution needs actual evidence. Prefix hit, SSM and MTP
are separate claims; preserve the original auto gates and mark unproven internals
NOT_RUN rather than forcing an experimental mode.

The agent separately observes activity, drains/stops, checks admission refusal and
active completion, restarts or recreates, and runs inference/streaming afterward.
Independent recovery uses only the fixed delivery, local models and site inputs,
without a development checkout or a disappeared temporary configuration. Do not
assume caches persist across restart. Keep original-image restoration distinct
from new-product recreation. Preserve the original public address, port, alias,
paths and authentication method; failed candidates trigger restoration before
further diagnosis.

Record publication, health, protocol compatibility, representative function,
backend execution, boundary/cache and recovery separately from performance,
stability, business accuracy and production qualification. Keep failures and
NOT_RUN scopes. Actual company deployment facts/history belong in xd-infra, never
in a second product lifecycle controller. Complete fork-only review and integration
before cleaning only task-owned resources; retain source tags, immutable products,
models, native input material, valid caches and recovery evidence.
