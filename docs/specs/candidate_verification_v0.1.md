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
  "candidate_rtl_path": "rtlgen_prob001/attempt_01/candidate.sv",
  "testbench_path": "rtlgen_prob001/testbench.sv",
  "support_files": [],
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
Verilog/SystemVerilog identifier. Unknown or missing fields, type coercion,
duplicate support paths, role-path collisions, and disabled compile or
simulation gates are rejected.

Artifact paths are normalized POSIX relative paths below `workspace_root`.
Absolute POSIX and Windows-drive paths, backslashes, NULs, empty components,
`.`/`..`, escapes, missing files, directories, symlinked workspace roots, and
symlinked existing path components are rejected.

## Evidence contract

One object is emitted for every valid manifest row. The exact top-level fields
are `schema_version`, `candidate_id`, `task_id`, `source_id`, `attempt`,
`top_module`, `requested_checks`, `input_hashes`, `toolchain`, `checks`,
`mismatch_summary`, `failure_category`, `accepted`, and `diagnostics`.

SHA-256 hashes are calculated from bytes during preflight before tools run.
Support hashes are sorted by manifest path. Tool availability and versions are
discovered once per run; an executable whose version probe fails remains
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

## Compile and simulation

The standalone compile gate invokes Icarus with argument arrays and
`shell=False`:

```text
iverilog -g2012 -s <top_module> -o <scratch-output>
  <candidate RTL> <support files>
```

The private testbench is intentionally excluded from this gate. Functional
simulation compiles candidate RTL, support files, and the testbench together,
without guessing the DUT top as the simulation root, then executes the result
with `vvp`.

Only complete, case-insensitive, whitespace-tolerant lines of this form are
parsed:

```text
Mismatches: <non-negative decimal integer>
```

All reported counts are retained in output order. `maximum_count` is their
maximum or `null` when no count was reported. Generic words such as `error`,
`fatal`, `failure`, or `mismatch` do not count as machine-readable evidence.

Simulation passes only when the simulation process starts, returns zero, and
no reported count is positive. A positive count is always
`functional_mismatch`, even with a nonzero return code. A zero count or no
count with return code zero passes. Nonzero return without a positive count is
`simulation_failure`; simulation startup failure is also
`simulation_failure`; and simulation compilation failure is `compile_failure`.
Timeouts are always `timeout`.

## Acceptance and failure categories

Acceptance is exactly:

```python
accepted = compile_passed and simulation_passed
```

Lint and synthesis never alter acceptance. A candidate can therefore be
accepted while its optional lint or synthesis leaf is failed.

The finite candidate categories are `passed`, `tool_unavailable`,
`compile_failure`, `functional_mismatch`, `simulation_failure`, `timeout`,
`internal_error`, and `partial_failure`. Required-gate classification uses
this priority:

```text
timeout
compile_failure
functional_mismatch
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
