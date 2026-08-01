# Rootless candidate runner v0.1

## Purpose

`rtlbench verify-candidates` executes generated RTL and therefore needs an
operating-system boundary outside the verifier. This spec defines the
RTLBench-provided rootless Podman launcher in `runner/`; it does not change the
candidate manifest or `rtl_candidate_evidence_v0.1` contracts.

## Immutable runtime

The image is built from a digest-qualified base image with `--pull=never`.
Exact package versions are supplied for Python, Icarus/`vvp`, Verilator, and
Yosys. The image labels record:

```text
RTLBench commit SHA
runner configuration version
Python version
Icarus version
vvp version
Verilator version
Yosys version
```

The launcher rejects mutable image references and missing or inconsistent
identity labels. The runner configuration is
`rtlbench_rootless_runner_v0.1`. It writes the immutable identity beside the
evidence as `candidate_evidence.jsonl.runner.json`; the record contains no
timestamp, username, hostname, or host path. The build wrapper verifies that
the requested RTLBench commit matches a clean checkout. The image build also
checks the actual Python interpreter, installed Debian package versions, and
reported executable versions before labels are published.

## Runtime boundary

Only rootless Podman is accepted, and it must report `rootless=true` before an
image is inspected or started. The container runs as UID/GID `65532:65532`
with a private user namespace, no network, all capabilities dropped,
`no-new-privileges`, a read-only root filesystem, and private scratch:

```text
/input   read-only handoff containing candidate_manifest.jsonl and workspace/
/output  writable staging for candidate_evidence.jsonl only
/work    writable bounded tmpfs for compiler/simulator work
/tmp     writable bounded tmpfs for temporary files
```

No host home directory, SSH keys, cloud credentials, production secrets, or
container socket is mounted or forwarded. The launcher supplies only fixed
locale, path, home, and temporary-directory values.

The fixed limits are two CPUs, 512 MiB memory, 128 processes, 32 MiB maximum
file size, 256 open files, 256 MiB `/work`, 128 MiB `/tmp`, 32 MiB evidence,
and a 120-second outer wall clock. The verifier's own per-tool and input
limits remain active inside the container.

## Command and publication

The command inside the image is exactly:

```text
rtlbench verify-candidates \
  --manifest /input/candidate_manifest.jsonl \
  --output /output/candidate_evidence.jsonl \
  --workspace-root /input/workspace \
  --work-dir /work \
  --force
```

The launcher rejects symlinks and special files in the input handoff and
rejects any `reference.sv`. It accepts only the final evidence file and the
verifier-managed `.rtlbench-partial` in the output staging directory. Final
JSONL is validated against `rtl_candidate_evidence_v0.1` by one strict
canonical validator before an atomic host publication. The validator enforces
accepted/check/category relationships, failure-category priority,
requested-check state, mismatch contracts, timeout evidence, and exact
SHA-256 input-hash records. Compile failures, functional mismatches, timeout
rows, missing mismatch results, and candidate-level internal-error rows are
valid evidence and remain preserved. A container or wall-clock failure with
only partial evidence publishes that partial beside the requested output and
returns non-zero. The identity sidecar binds publication to the manifest
hash, deterministic workspace-tree hash, final evidence hash, image digest,
and fixed runtime/resource policies.

## Acceptance

Synthetic acceptance tests cover passing, compile failure, functional mismatch,
timeout, missing mismatch result, internal failure, malformed evidence,
unexpected output, input-byte preservation, reference-RTL rejection, security
argument construction, secret non-forwarding, and temporary staging cleanup.
The same acceptance matrix must also be run in the built immutable image with
rootless Podman before a real candidate is evaluated. No real handoff is part
of the synthetic suite.
