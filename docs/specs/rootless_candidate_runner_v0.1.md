# Isolated candidate runner v0.1

## Purpose

`rtlbench verify-candidates` executes generated RTL and therefore needs an
operating-system boundary outside the verifier. This spec defines two
explicit RTLBench launcher profiles. It does not change the candidate
manifest or `rtl_candidate_evidence_v0.1` contracts.

`production-rootless` is the required profile for shared or production
systems. `pilot-docker` is an explicitly acknowledged, weaker rootful-Docker
profile for a small sequential pilot on one ordinary Linux machine. The
launcher never selects a profile automatically and never falls back between
Podman and Docker.

## Immutable runtime

Both profiles use the same image. It must be built from a digest-qualified
base image after local inspection, exact Debian package versions, exact
normalized executable versions, and a clean checkout whose commit SHA matches
the image label. Docker uses the Boolean build flag `--pull=false`; rootless
Podman uses the policy flag `--pull=never`. The image must include:

```text
non-root USER 65532:65532
fixed rtlbench entrypoint
verified RTLBench commit label
verified runner configuration label
verified Python, Icarus/vvp, Verilator, and Yosys labels
working CLI help smoke test
```

The production launcher accepts only:

```text
repository/name@sha256:<64 lowercase hexadecimal characters>
```

A bare tag is rejected in both profiles. For the explicitly acknowledged
`pilot-docker` profile, the launcher also accepts this immutable local Docker
image ID form:

```text
sha256:<64 lowercase hexadecimal characters>
```

The local image ID is inspected, must match exactly, and is the value passed
to `docker run`; it is not resolved from a mutable tag. Candidate execution
never pulls an image.

Build the image with the primary explicit-backend builder. The base image must
already be local and both builders use the same Dockerfile:

```bash
runner/build_isolated_image.sh \
  --builder docker \
  --base-image python:3.11-slim@sha256:<local-base-digest> \
  --tag rtlbench-runner:pilot \
  --rtlbench-commit "$(git rev-parse HEAD)" \
  --python-version <version> \
  --iverilog-package-version <version> \
  --iverilog-version <version> \
  --vvp-version <version> \
  --verilator-package-version <version> \
  --verilator-version <version> \
  --yosys-package-version <version> \
  --yosys-version <version>
```

`--builder podman-rootless` is the other explicit choice and requires
`rootless=true`; `build_rootless_image.sh` remains a compatibility wrapper
that always selects it. The builder requires a clean checkout and exact
`HEAD`/commit identity, and inspects the base image locally before either
build.

## Profiles and CLI

The primary entry point requires an explicit profile:

```bash
python runner/run_isolated.py \
  --profile pilot-docker \
  --acknowledge-rootful-runtime \
  --image localhost/rtlbench-runner@sha256:<digest> \
  --input /path/to/attempt_01 \
  --output /path/to/isolated-output/candidate_evidence.jsonl
```

The production profile is selected explicitly and rejects the pilot
acknowledgement flag:

```bash
python runner/run_isolated.py \
  --profile production-rootless \
  --image localhost/rtlbench-runner@sha256:<digest> \
  --input /path/to/attempt_01 \
  --output /path/to/isolated-output/candidate_evidence.jsonl
```

`runner/run_rootless.py` is retained as a compatibility wrapper and always
uses `production-rootless`; it does not silently change to Docker.

### production-rootless

This profile requires the `podman` executable and:

```bash
podman info --format '{{.Host.Security.Rootless}}'
```

must return exactly `true` after normalization. The command runs as
`65532:65532` with a private user namespace, no network, a read-only root
filesystem, read-only `/input`, dropped capabilities,
`no-new-privileges`, bounded `/work` and `/tmp`, and bounded CPU, memory,
process, file, output, and wall-clock resources. This is the stronger profile
for shared or production systems. Its fixed runtime command includes
`podman run --pull=never`; the Docker profile uses `docker run --pull never`.

### pilot-docker

This profile requires the user to pass both `--profile pilot-docker` and
`--acknowledge-rootful-runtime`. It probes the Docker server with:

```bash
docker version --format '{{json .Server.Version}}'
```

The sidecar identifies this explicitly as `runtime_mode: rootful-daemon` and
`rootless: false`. It is intended only for one-candidate-at-a-time local
pilots; the rootful daemon is a weaker trust boundary and this profile is not
production-approved.

The fixed Docker command is equivalent to:

```text
docker run --pull never --rm --network none --user 65532:65532 --read-only
  --cap-drop ALL --security-opt no-new-privileges
  --cpus 1 --memory 536870912 --memory-swap 536870912
  --pids-limit 64 --ulimit fsize=33554432:33554432
  --ulimit nofile=256:256 --ulimit nproc=64:64
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=67108864,mode=1777
  --tmpfs /work:rw,nosuid,nodev,size=134217728,uid=65532,gid=65532,mode=700
  --mount type=volume,src=<temporary-input-volume>,dst=/input,readonly,volume-nocopy
  --mount type=bind,src=<staged-output>,dst=/output
  --workdir /work
  --env HOME=/tmp --env TMPDIR=/tmp --env LANG=C --env LC_ALL=C
  --env PYTHONPATH=/opt/rtlbench/src
  --env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
  --entrypoint rtlbench <digest-qualified-image>
  verify-candidates --manifest /input/candidate_manifest.jsonl
  --output /output/candidate_evidence.jsonl
  --workspace-root /input/workspace --work-dir /work --force
```

The Docker defaults are one CPU, 512 MiB memory, 512 MiB memory+swap, 64
processes, 32 MiB maximum file size, 256 open files, 128 MiB `/work`, 64 MiB
`/tmp`, 32 MiB evidence, and a 120-second outer wall clock. The 512 MiB
container limit is not the total host-memory requirement: Docker, the image,
the kernel, and the host still need memory on an 8 GB machine.

Neither profile permits `--privileged`, host networking, host PID/IPC,
devices, Docker socket mounts, host home or repository mounts, credential or
secret mounts, arbitrary environment forwarding, or arbitrary container
commands. Candidate stdin is disabled; the population helper receives only
the normalized archive on stdin. The launcher terminates the entire process
group on the outer timeout.

The canonical handoff is never mounted directly into the candidate container.
The launcher creates a normalized private snapshot containing only
`candidate_manifest.jsonl` and `workspace/`, streams it through stdin to a
fixed root-only population helper, and transfers it into a short-lived,
runtime-managed named volume. A fixed probe then opens and hashes that volume
as UID/GID `65532:65532`. Docker mounts it with
`type=volume,src=<temporary-input-volume>,dst=/input,readonly,volume-nocopy`;
rootless Podman uses the equivalent already-created named-volume mount with
`ro` (and no anonymous or host bind volume). The candidate sees that volume
read-only as `/input`.

The `/output` host bind mount is writable by default because no read-only
option is supplied. The `rw` options on `/tmp` and `/work` are tmpfs options
and remain unchanged.

Canonical handoffs may remain private mode `0700`; the launcher never chmods
or chowns them. The private uncompressed tar is sorted by normalized POSIX
path, contains no plan, instruction, evidence, sidecar, report, or reference
RTL, uses `uid/gid 0`, empty owner names, `mtime 0`, directory mode `0555`,
and regular-file mode `0444`, and is kept as a launcher-owned `0600` file in a
private temporary directory. Source payload is bounded by
`DEFAULT_MAX_RUN_INPUT_BYTES`. The temporary volume name is cryptographically
random (`rtlbench-input-` plus 32 lowercase hex digits), is labeled
`io.rtlbench.runner.temporary-input=rtlbench_runtime_input_v0.1`, and is
removed before evidence publication completes.

The population helper is the only fixed staging operation allowed to run as
UID 0. It has no shell, host bind mounts, Docker/Podman socket, credentials,
or network and cannot execute RTL, testbench, or manifest content. The
runtime-user probe rejects links, special files, unexpected paths, unreadable
files, and hash disagreement before candidate execution. The host launcher and
probe use the canonical `rtlbench.candidate_evidence.sha256_workspace_tree`
implementation; the probe's separate validation pass does not define another
hash serialization format. Cleanup terminates
managed processes, removes the temporary volume, closes/deletes the private
archive, and deletes launcher temporary directories. Failure to remove the
managed volume fails closed and prevents final evidence or sidecar
publication; unrelated volumes are never pruned.

Both the image and launcher pin module discovery to the immutable image source
directory with `PYTHONPATH=/opt/rtlbench/src`. The launcher always supplies
this fixed value and never takes it from the host or forwards arbitrary
environment variables. The Dockerfile build smoke tests use the same minimal
`env -i` environment as runtime and verify both the fixed wrapper and direct
`rtlbench.cli` import.

After copying the source, the Dockerfile normalizes the RTLBench tree to root
ownership (`0:0`), directory mode `0555`, and regular-file mode `0444`. The
application is therefore readable and traversable by UID/GID `65532` but is not
writable by that user, regardless of checkout or build-host umask. The fixed
`rtlbench` entrypoint remains root-owned with mode `0555`. The import and CLI
smoke tests run after `USER 65532:65532`; a root-only build-time import does not
establish runtime-user readability.

## Evidence and publication

Both profiles share the same input, execution, validation, and publication
logic:

```text
/input   read-only runtime-managed handoff snapshot
/output  writable evidence staging only
/work    bounded disposable compiler/simulator work
/tmp     bounded temporary filesystem
```

The handoff must contain `candidate_manifest.jsonl` and `workspace/`. The
launcher rejects symlinks, special files, path escapes, and `reference.sv`.
Only `candidate_evidence.jsonl` and the verifier-managed
`candidate_evidence.jsonl.rtlbench-partial` may appear in staging. Final
evidence is validated with the strict canonical evidence validator before
atomic host publication. Failed or timed-out executions preserve valid
partial evidence and return non-zero.

The sidecar `candidate_evidence.jsonl.runner.json` uses deterministic JSON and
contains no timestamp, hostname, username, absolute host path, temporary
directory, or credential path. Its identity fields include:

```json
{
  "schema_version": "rtlbench_runner_identity_v0.2",
  "profile": "pilot-docker",
  "runtime": "docker",
  "runtime_mode": "rootful-daemon",
  "rootless": false,
  "image_identity_kind": "repository-digest",
  "image": "name@sha256:<digest>",
  "image_id": "sha256:<inspected-local-image-id>",
  "image_digest": "sha256:<digest>",
  "rtlbench_commit": "<40-hex-sha>",
  "runner_config_version": "<version>",
  "python_version": "<version>",
  "iverilog_version": "<version>",
  "vvp_version": "<version>",
  "verilator_version": "<version>",
  "yosys_version": "<version>",
  "network_policy": "none",
  "resource_limits": {},
  "manifest_sha256": "<sha256>",
  "workspace_tree_sha256": "<sha256>",
  "evidence_sha256": "<sha256>",
  "partial_evidence_sha256": null
}
```

For `production-rootless`, the corresponding identity is
`profile: production-rootless`, `runtime: podman`, `runtime_mode: rootless`,
and `rootless: true`. The sidecar accompanies, but does not replace,
RTLBench's candidate-evidence contract.

The launcher continuously drains top-level container-runtime output and keeps
only a fixed 65,536-byte diagnostic tail. Before a launcher/runtime failure is
reported, the retained output is sanitized for control sequences and
launcher-owned host paths. It is not a complete container log. Diagnostics are
shown only for launcher/runtime failures and are never written to candidate
evidence or the identity sidecar; successful executions do not print captured
runtime output.

For a Docker local-image run, the identity fields are instead:

```json
{
  "image_identity_kind": "local-image-id",
  "image": "sha256:<image-id>",
  "image_id": "sha256:<image-id>",
  "image_digest": null
}
```

## Acceptance and operating sequence

Normal tests use fake runtimes and cover both profiles without requiring live
Docker or Podman. A designated live matrix must run in the same immutable
image before a real candidate. The Docker matrix is selected with
`RTLBench_DOCKER_RUNNER_IMAGE`; the Podman matrix is selected with
`RTLBench_RUNNER_IMAGE`. If a live matrix is explicitly selected, unavailable
runtime or image prerequisites are failures rather than successful skips.

The safe sequence is:

```text
build/import immutable image
→ run the selected synthetic live matrix
→ run candidate 1 only
→ validate evidence and sidecar
→ copy validated evidence into the canonical RTLSpecializer attempt directory
→ ingest it
```

Do not run generated RTL, simulators, or EDA tools directly on the host. Never
mount a host home, SSH keys, cloud credentials, production secrets, repository
root as writable, or a Docker socket.
