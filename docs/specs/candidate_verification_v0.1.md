# RTL Candidate Verification v0.1

## Scope and non-goals

`rtlbench verify-candidates` is a deterministic, local, model-independent
verifier for one generated RTL candidate per JSONL manifest row:

```text
candidate manifest -> strict preflight -> candidate compile
  -> functional simulation -> optional lint/synthesis
  -> sanitized rtl_candidate_evidence_v0.1 JSONL
```

It never calls a model, API, network service, teacher generator, repair loop,
reference-RTL comparison, mutation generator, equivalence checker, activity or
power measurement, scoring flow, or concurrent executor. Reference RTL is not
required. The private testbench is a filesystem input and is never copied into
evidence.

## CLI and exit codes

```bash
rtlbench verify-candidates \
  --manifest candidate_manifest.jsonl \
  --output candidate_evidence.jsonl \
  --workspace-root workspace \
  --work-dir /tmp/rtlbench-candidates \
  --timeout 30 \
  --max-artifact-bytes 8388608 \
  --force
```

The four paths are required. `--timeout` defaults to 30 seconds and applies
independently to every tool invocation, including the one-time tool-version
probes. Rows run sequentially in manifest order.

Exit status 0 means preflight succeeded and the final evidence was published,
including when individual candidates fail. Status 2 means an argument or
candidate-manifest validation error. Status 3 means workspace, output, or
publication filesystem preflight failed. Status 4 means interruption or an
unrecoverable batch-level internal error.

## Manifest contract

Each nonblank line is a JSON object with exactly these fields:

```json
{
  "schema_version": "rtl_candidate_manifest_v0.1",
  "candidate_id": "rtlgen_prob001_attempt_01",
  "task_id": "rtlgen_verilogeval_prob001_ab12cd34ef56",
  "source_id": "Prob001_zero",
  "attempt": 1,
  "top_module": "TopModule",
  "testbench_top": "tb",
  "candidate_rtl_path": "rtlgen_prob001/attempt_01/candidate.sv",
  "testbench_path": "rtlgen_prob001/testbench.sv",
  "support_files": [],
  "simulation_result_contract": "mismatch_count_v1",
  "requested_checks": {
    "compile": true,
    "simulation": true,
    "lint": false,
    "synthesis": false
  }
}
```

The schema version is exact. IDs and `source_id` are non-empty strings;
`candidate_id` is globally unique; `(task_id, attempt)` is also unique;
`attempt` is an integer at least 1; and `top_module` is a valid
Verilog/SystemVerilog identifier. `testbench_top` is a required identifier
used only as the simulation elaboration root; it is never inferred from the
candidate. `simulation_result_contract` is exactly `mismatch_count_v1` or
`exit_code_v1`. Unknown or missing fields, type coercion, duplicate support
paths, role-path collisions, and disabled compile or simulation gates are
rejected.

Artifact paths are normalized POSIX relative paths below `workspace_root`.
Absolute POSIX and Windows-drive paths, backslashes, NULs, empty components,
`.`/`..`, escapes, missing files, directories, symlinked workspace roots, and
symlinked existing path components are rejected. Candidate, testbench, and
support files are compared after resolution; identical paths, hard links, and
duplicate inodes are rejected even when their manifest strings differ.

## Evidence contract

One object is emitted for every valid manifest row. The exact top-level fields
are `schema_version`, `candidate_id`, `task_id`, `source_id`, `attempt`,
`top_module`, `testbench_top`, `simulation_result_contract`,
`requested_checks`, `input_hashes`, `toolchain`, `checks`, `mismatch_summary`,
`failure_category`, `accepted`, and `diagnostics`.

SHA-256 hashes are calculated while streaming each validated workspace input
into a newly created managed snapshot, before tools run. The verifier executes
only those snapshots; the original workspace paths are never passed to a
tool. Support hashes retain their logical manifest paths and are sorted by
manifest path. Tool availability and versions are discovered once per run; an executable whose version probe fails remains
`available: true` with `version: null`, while a missing executable is explicit
`available: false` with `version: null`.

Every check leaf uses the same three-state shape:

```json
{"attempted":true,"passed":true,"reason":null}
```

or:

```json
{"attempted":false,"passed":null,"reason":"tool_unavailable"}
```

Compile is recorded under `checks.compile.candidate`, simulation under
`checks.simulation.candidate_passes`, lint under `checks.lint.candidate`, and
synthesis under `checks.synthesis.candidate`. Unrequested optional checks are
`not_requested` and missing optional tools are `tool_unavailable`.

Evidence uses stable sorted-key JSON, newline termination, manifest order, and
no timestamps or durations. It contains no RTL, testbench/support contents,
raw subprocess output, environment dumps, secrets, reference RTL, or private
absolute paths.

## Input snapshots and size limits

After complete manifest and filesystem validation, every row is copied into an
unpredictable managed run directory before tool discovery or execution. A row
uses a layout like:

```text
<managed-run>/inputs/000001_<candidate-digest>/
  candidate.sv
  testbench.sv
  support/0000_<safe-name>
```

Copying is bounded, chunked, hash-coupled, and performed without following a
final source symlink. Snapshots are flushed, closed, and read-only where the
platform permits. Hashes and executed bytes therefore describe the same
prepared inputs. Changes or deletion of original workspace files after
preparation cannot change verification. Snapshots are scratch artifacts and
are never evidence outputs.

The defaults are 8 MiB per artifact, 32 MiB per row, and 256 MiB for the full
run. The hard caps are 32 MiB per artifact, 128 MiB per row, and 512 MiB per
run. `--max-artifact-bytes`, `--max-row-input-bytes`, and
`--max-run-input-bytes` may lower these limits, subject to the hard caps.
Limits are enforced during streaming, including for files that grow while
being read. Any limit failure is a batch preflight failure; no tool is run and
no final evidence is published.

## Compile and simulation

The standalone compile gate invokes Icarus with argument arrays and
`shell=False`:

```text
iverilog -g2012 -s <top_module> -o <scratch-output>
  <candidate RTL> <support files>
```

The private testbench is intentionally excluded from this gate. Functional
simulation compiles candidate RTL, support files, and the testbench together,
and explicitly selects the declared testbench root:

```text
iverilog -g2012 -s <testbench_top> -o <scratch-output>
  <candidate RTL> <support files> <testbench>
```

It then executes the result with `vvp`. The standalone compile gate remains
`iverilog -g2012 -s <top_module>` and excludes the private testbench.

For `mismatch_count_v1`, only complete, case-insensitive lines with horizontal
whitespace tolerance in one of these forms are parsed:

```text
Mismatches: <non-negative decimal integer>
Mismatches: <non-negative decimal integer> in <non-negative decimal integer> samples
```

All reported counts and sample counts are retained in output order. A short
form has a corresponding `null` sample count. The exact deterministic
`mismatch_summary` shape is:

```json
{
  "contract": "mismatch_count_v1",
  "reported_counts": [3],
  "reported_sample_counts": [40],
  "maximum_count": 3,
  "timeout_reported": false
}
```

Generic words such as `error`, `fatal`, `failure`, `mismatch`, or prose
containing `timeout` do not count as machine-readable evidence. The only
testbench timeout marker is a complete `TIMEOUT` line, recognized
case-insensitively. A Python subprocess timeout is also reported as
`timeout`.

With `mismatch_count_v1`, simulation passes only when the process starts,
returns zero, has no timeout marker, reports at least one supported mismatch
line, and every count is zero. A positive count is always
`functional_mismatch`, even with a nonzero return code. A missing report is
`simulation_result_missing`. Zero counts with a nonzero return are
`simulation_failure`.

With `exit_code_v1`, a zero return code with no timeout marker and no positive
recognized count passes; a mismatch report is optional. Timeout evidence has
priority over counts and process status.

## Acceptance and failure categories

Acceptance is exactly:

```python
accepted = compile_passed and simulation_passed
```

Lint and synthesis never alter acceptance. A candidate can therefore be
accepted while its optional lint or synthesis leaf is failed.

The finite candidate categories are `passed`, `tool_unavailable`,
`compile_failure`, `functional_mismatch`, `simulation_result_missing`,
`simulation_failure`, `timeout`, `internal_error`, and `partial_failure`.
Required-gate classification uses
this priority:

```text
timeout
compile_failure
functional_mismatch
simulation_result_missing
simulation_failure
tool_unavailable
internal_error
partial_failure
passed
```

The category describes the compile/simulation acceptance gates only; optional
lint/synthesis failures remain visible in their leaves.

## Optional lint and synthesis

Requested lint uses Verilator when available, targeting `top_module` and
including candidate/support files. Requested synthesis uses Yosys to read the
candidate/support files, select `top_module`, and perform bounded generic
`proc`, `opt`, and `stat` processing. Neither is called correctness evidence,
and no area score is reported. Missing tools are unattempted
`tool_unavailable`; failures remain in their check leaves and do not change
`accepted` or the required-gate `failure_category`.

## Diagnostics and privacy

Diagnostics are deduplicated, deterministic, and capped at 4096 characters per
item. They contain bounded actionable tool context. Workspace, managed work,
manifest, artifact, and random managed-run paths are replaced with stable
placeholders. Apparent API keys, tokens, passwords, bearer credentials, and
secrets are redacted. Source lines and artifact contents are removed. A
successful testbench's output is not included merely because it printed
`Mismatches: 0`.

Redaction indexes are built once from bounded snapshots. At most 20,000 source
lines, 1,024 bytes per indexed line, and 10,000 long tokens are indexed. If an
indexing limit is reached, verification continues with a conservative policy
that omits raw tool text. Simulation diagnostics prefer structured stage,
return-code, timeout, output-limit, and evidence fields; compile, lint, and
synthesis retain bounded sanitized error summaries. Source-context and caret
lines are removed.

Each tool invocation has a 65,536-byte combined stdout/stderr limit by
default. The child is terminated when the limit is exceeded and only bounded
sanitized text is retained; the diagnostic states `output_limit_exceeded`.
`--max-output-bytes` can lower or raise the limit up to the conservative
4 MiB cap. Tool subprocesses receive a minimal deterministic environment with
scratch-directed temporary and home directories. Parent API keys, tokens,
passwords, cloud credentials, and private application variables are not
forwarded. `PATH` contains only safe absolute caller entries, platform
defaults, and directories containing discovered executables, so required
helper tools remain usable without inheriting arbitrary environment values.
This environment reduction is defense-in-depth, not a sandbox.

## Execution security

Generated RTL is executable simulator input. Path validation, immutable input
snapshots, bounded process output, diagnostic sanitization, and process-tree
cleanup are not an operating-system sandbox.
Teacher-generated candidates must be evaluated inside a disposable,
least-privileged container or VM with no network access. The workspace should
be read-only except for managed scratch, no production secrets should be
present, and CPU, memory, process, output, disk, and time limits should be
applied externally. This PR does not implement a universal sandbox, and host
execution must not be treated as safe merely because paths are validated.

## Filesystem and atomic output behavior

The full manifest and all duplicate identities are validated before tool
execution. All hashes are computed before tool execution. A fresh unpredictable
`.rtlbench-run-*` directory is created below `work_dir`, with a separate row
scratch directory. Predictable pre-existing row directories are not reused,
and caller-owned directories are never recursively deleted.

The verifier writes to the exact `<output>.rtlbench-partial`, flushes every
completed row, and atomically replaces the final output with `os.replace` on
normal completion. Existing final/partial outputs require `--force`; force may
replace only those exact managed paths. Output and partial symlinks, directories,
hard-link aliases, input aliases, and output/partial collisions are rejected.
The work directory may not alias a regular input, and output paths may not
alias managed snapshots. Snapshot destinations are newly created and are not
reused across runs. On POSIX, each tool runs in a new process session;
timeout, interruption, and output overflow terminate the process group with a
bounded TERM-then-KILL sequence. Windows uses a new process group and best-
effort CTRL-BREAK/kill cleanup; descendant cleanup has platform limitations.
An interruption retains the managed partial and leaves an existing final output
untouched. Candidate-level exceptions become bounded `internal_error` rows and
later rows continue. Batch-level preflight/publication errors do not create a
misleading final evidence file.

## Relationship to `verify-mutations`

This is a parallel path. `rtlbench verify-mutations`, the mutation manifest and
evidence schemas, mutation categories, evidence tiers, and mismatch-detection
semantics remain unchanged. Candidate evidence deliberately has no mutation
evidence tier and does not reuse mutation-only categories such as
`simulation_not_detected` or `repaired_simulation_failure`.

The manifest naming is interoperable with the later RTLSpecializer workflow:
candidate rows carry `task_id`, `source_id`, `top_module`, a candidate artifact,
and private verification assets. Later integration may generate those rows,
but this command itself remains local and model-independent.
