# Isolated RTLBench runner

`runner/run_isolated.py` is the profile-aware execution boundary for
`rtlbench verify-candidates`. It supports two explicit profiles:

* `production-rootless` uses rootless Podman and is the required profile for
  shared or production systems.
* `pilot-docker` uses an ordinary rootful Docker daemon for a small,
  sequential, single-machine pilot. It is a weaker host boundary and requires
  an explicit acknowledgement on every invocation.

There is no automatic Docker/Podman fallback and no implicit profile choice in
the primary CLI. `runner/run_rootless.py` remains a compatibility wrapper that
always selects `production-rootless`.

## Build an immutable local image

Use the primary builder with an explicit backend. The Docker builder requires
only ordinary Docker; it does not probe or fall back to Podman. The
digest-qualified base image must already be local. Docker inspects it first
and builds with `--pull=false`; rootless Podman inspects it first and builds
with `--pull=never`:

```bash
runner/build_isolated_image.sh \
  --builder docker \
  --base-image python:3.11-slim@sha256:<local-base-digest> \
  --tag rtlbench-runner:pilot \
  --rtlbench-commit "$(git rev-parse HEAD)" \
  --python-version <exact-python-version> \
  --iverilog-package-version <exact-iverilog-package-version> \
  --iverilog-version <exact-iverilog-runtime-version> \
  --vvp-version <exact-vvp-runtime-version> \
  --verilator-package-version <exact-verilator-package-version> \
  --verilator-version <exact-verilator-runtime-version> \
  --yosys-package-version <exact-yosys-package-version> \
  --yosys-version <exact-yosys-runtime-version>
```

The builder prints the immutable local image ID. For a Docker-only local
pilot, pass that ID to the launcher:

```text
sha256:<64-lowercase-hex-image-id>
```

The compatibility `runner/build_rootless_image.sh` wrapper always selects
`--builder podman-rootless`; that backend requires Podman `rootless=true`.
Both builders use the same `runner/Dockerfile`, require a clean checkout
whose requested commit equals `HEAD`, verify exact package and runtime
versions, and require the base image to be present locally. These
backend-specific build flags are separate from the runtime no-pull flags
documented below.

For production-rootless, use a repository digest rather than the local image
ID:

```text
localhost/rtlbench-runner@sha256:<64-lowercase-hex-repository-digest>
```

Tags remain invalid for both profiles. The image must contain the fixed
`rtlbench` entrypoint, non-root user `65532:65532`, verified
RTLBench/tool-version labels, and the working CLI help smoke test. Candidate
execution does not pull images or require network access.

## Local pilot

For a small Linux machine, run one candidate manifest at a time with the
explicit rootful-runtime acknowledgement. The output directory must be
separate from the read-only handoff:

```bash
python runner/run_isolated.py \
  --profile pilot-docker \
  --acknowledge-rootful-runtime \
  --image sha256:<local-image-id> \
  --input /path/to/attempt_01 \
  --output /path/to/isolated-output/candidate_evidence.jsonl
```

`pilot-docker` is intended for a constrained local pilot only. It never
silently replaces rootless Podman and is not production-approved. The default
limits are conservative for an 8 GB host: one CPU, 512 MiB container memory,
512 MiB memory+swap, 64 processes, 32 MiB maximum file size, 256 open files,
128 MiB `/work`, 64 MiB `/tmp`, 32 MiB evidence, and a 120-second outer wall
clock. The 512 MiB value is the container limit, not the total memory required
by the host, Docker daemon, image, and kernel.

The Docker profile applies:

```text
network none                         user 65532:65532
read-only root filesystem             all capabilities dropped
no-new-privileges                     read-only /input bind mount
separate writable /output bind       bounded writable /work tmpfs
bounded writable /tmp tmpfs           fixed rtlbench command
stdin disabled                        process-group cleanup
```

It does not permit `--privileged`, host networking, host PID/IPC, devices,
Docker socket mounts, host-home or repository mounts, credential/secret
mounts, arbitrary environment forwarding, mutable image tags, or arbitrary
container commands.

The `/input` bind mount is explicitly read-only. The `/output` bind mount is
writable by default because no read-only option is supplied. The `rw` options
on `/tmp` and `/work` are tmpfs options and remain unchanged.

## Production execution

Use rootless Podman explicitly on shared or production systems:

```bash
python runner/run_isolated.py \
  --profile production-rootless \
  --image localhost/rtlbench-runner@sha256:<immutable-repository-digest> \
  --input /path/to/attempt_01 \
  --output /path/to/isolated-output/candidate_evidence.jsonl
```

The launcher requires `podman info --format '{{.Host.Security.Rootless}}'` to
normalize to `true`. It uses the stronger rootless profile with a private user
namespace, non-root execution, no network, read-only input and root
filesystem, dropped capabilities, no-new-privileges, bounded scratch and
resource limits. Passing `--acknowledge-rootful-runtime` to this profile is an
error.

## Filesystem and identity boundary

The fixed container layout is:

```text
/input   read-only candidate handoff
/output  writable evidence staging only
/work    bounded disposable compiler/simulator work
/tmp     bounded temporary filesystem
```

The input must contain `candidate_manifest.jsonl` and `workspace/`; symlinks,
special files, and any `reference.sv` are rejected. Never mount a host home,
SSH keys, cloud credentials, production secrets, repository root as writable,
or a Docker socket. The launcher forwards only its fixed sanitized environment
and never reads Docker client configuration or registry credentials.

Final evidence is validated by RTLBench's strict
`rtl_candidate_evidence_v0.1` validator before atomic publication. Partial
evidence is preserved on a bounded timeout or runtime failure. The adjacent
`candidate_evidence.jsonl.runner.json` sidecar records the profile/runtime
mode, image identity kind, supplied image, inspected local image ID, repository
digest when present, tool identity, resource policy, manifest hash,
deterministic workspace-tree hash, and final/partial evidence hashes. A Docker
local-image run uses `image_identity_kind: local-image-id` with
`image_digest: null`; a repository digest uses
`image_identity_kind: repository-digest`. It contains no
timestamps, hostnames, usernames, host paths, or secret paths.

The execution sequence is:

```text
build/import immutable image
→ run the synthetic live matrix
→ run candidate 1 only
→ validate evidence and sidecar
→ copy validated evidence into the canonical RTLSpecializer attempt directory
→ ingest it
```

Do not execute generated RTL directly on the host. Do not expose a real
candidate until the selected profile has passed its live synthetic acceptance
matrix.
