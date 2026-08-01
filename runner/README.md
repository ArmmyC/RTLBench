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

## Immutable image

Both profiles use the same immutable image format. Build the image from a
digest-qualified base with exact package and executable versions, then use its
repository digest—not a mutable tag—as the execution reference:

```text
localhost/rtlbench-runner@sha256:<64-lowercase-hex-digest>
```

The image must contain the fixed `rtlbench` entrypoint, non-root user
`65532:65532`, verified RTLBench/tool-version labels, and the working CLI help
smoke test. Candidate execution does not need network access after the image
has been imported or pulled.

## Local pilot

For a small Linux machine, run one candidate manifest at a time with the
explicit rootful-runtime acknowledgement. The output directory must be
separate from the read-only handoff:

```bash
python runner/run_isolated.py \
  --profile pilot-docker \
  --acknowledge-rootful-runtime \
  --image localhost/rtlbench-runner@sha256:<immutable-image-digest> \
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

## Production execution

Use rootless Podman explicitly on shared or production systems:

```bash
python runner/run_isolated.py \
  --profile production-rootless \
  --image localhost/rtlbench-runner@sha256:<immutable-image-digest> \
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
mode, image and tool identity, resource policy, manifest hash, deterministic
workspace-tree hash, and final/partial evidence hashes. It contains no
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
