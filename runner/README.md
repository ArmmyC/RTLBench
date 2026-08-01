# Rootless RTLBench runner

This directory is the execution boundary for `rtlbench verify-candidates`.
It deliberately supports rootless Podman only. A missing runtime, a rootful
runtime, a mutable image reference, missing image identity labels, a handoff
symlink, or a handoff containing `reference.sv` fails closed before candidate
execution.

The runner mounts exactly these paths inside the container:

```text
/input   read-only candidate handoff
/output  writable evidence staging directory
/work    bounded writable tmpfs for compiler/simulator scratch
/tmp     bounded writable tmpfs for temporary files
```

The runtime user is `65532:65532`. The launcher applies `--network none`,
`--read-only`, `--cap-drop ALL`, `no-new-privileges`, private user and IPC
namespaces, CPU, memory, PID, file-size, open-file, tmpfs, and wall-clock
limits. It passes only fixed locale, path, home, and temporary-directory
variables to the container; host home directories, SSH keys, credential files,
cloud credentials, and container sockets are never mounted or forwarded.

## Build an immutable image

Use a digest-qualified Python base image and exact Debian package versions.
The build script refuses a base image without a SHA-256 digest and records the
RTLBench commit, Python version, Icarus/`vvp`, Verilator, Yosys, and runner
configuration versions as image labels:

```bash
./runner/build_rootless_image.sh \
  --base-image python:3.11-slim@sha256:<pinned-base-digest> \
  --tag localhost/rtlbench-runner:pilot \
  --rtlbench-commit "$(git rev-parse HEAD)" \
  --python-version <exact-python-version> \
  --iverilog-version <exact-iverilog-deb-version> \
  --vvp-version <exact-vvp-version> \
  --verilator-version <exact-verilator-deb-version> \
  --yosys-version <exact-yosys-deb-version>
```

Build with `--pull=never`, then inspect the resulting image and use only its
digest-qualified reference with the launcher. The launcher requires all
identity labels and emits a deterministic `candidate_evidence.jsonl.runner.json`
sidecar containing the image digest, RTLBench commit, tool versions, and
`rtlbench_rootless_runner_v0.1`; it contains no timestamps or host paths.

## Run a handoff

The input directory must contain `candidate_manifest.jsonl` and `workspace/`.
The output file must be outside that read-only directory:

```bash
python runner/run_rootless.py \
  --image registry.example/rtlbench-runner@sha256:<immutable-image-digest> \
  --input /path/to/attempt_01 \
  --output /path/to/attempt_01/candidate_evidence.jsonl
```

Inside the image the launcher executes exactly:

```text
rtlbench verify-candidates \
  --manifest /input/candidate_manifest.jsonl \
  --output /output/candidate_evidence.jsonl \
  --workspace-root /input/workspace \
  --work-dir /work \
  --force
```

The staging directory accepts only the final evidence file and the verifier's
managed `.rtlbench-partial` file. Final evidence is schema-checked before it
is atomically copied to the requested host output. Compile failures,
functional mismatches, timeouts, missing mismatch results, and candidate-level
internal failures are normal evidence rows and are preserved. If the outer
wall clock expires or the container fails before final publication, a managed
partial file is copied beside the requested output and the launcher returns a
non-zero status.

Do not run the launcher against a real candidate until the synthetic runner
acceptance suite has passed in the same immutable image. The acceptance suite
covers passing, compile failure, functional mismatch, timeout, missing result,
schema rejection, input-byte preservation, forbidden reference RTL, security
flags, network isolation configuration, and cleanup. A rootless Podman
integration run is required before promoting an image; fake-runtime unit tests
do not substitute for that final image check.
