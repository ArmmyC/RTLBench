# RTLBench mutation verification v0.1

## Scope and non-goals

`rtlbench verify-mutations` is a deterministic, local executable verifier for
existing original, mutated, and repaired RTL artifacts described by a JSONL
manifest. It produces one sanitized evidence JSON object per valid manifest
row. It never calls a model, HTTP endpoint, or network service.

Version 0.1 provides sequential compile, lint, functional simulation, generic
Yosys synthesis, limited Yosys equivalence, and an explicitly unavailable
activity state. It is not a dataset builder, mutation generator, universal
sequential-equivalence proof, signoff synthesis flow, power measurement flow,
or model evaluator. VCD toggle counting is not power evidence and is not run by
this command in v0.1.

## CLI contract and exit codes

The command is:

```text
rtlbench verify-mutations --manifest MANIFEST --output OUTPUT \
  --workspace-root ROOT --work-dir WORK [--timeout SECONDS] [--force]
```

The four paths are required. `--timeout` defaults to 30 seconds and bounds
each tool invocation (version probes use a separate five-second bound).
`--force` permits replacement of only the exact output path and its managed
partial path, provided neither path aliases the manifest or any declared RTL,
testbench, or support input. Rows are processed sequentially in manifest
order; v0.1 has no concurrency.

The legacy invocation remains valid, including
`rtlbench --config configs/verilogeval.yaml`; it continues to use the existing
benchmark configuration and runner.

Exit status is 0 when preflight succeeds and the final evidence file is
written, even when individual checks fail or tools are unavailable. Status 2
means argument or manifest validation failed. Status 3 means output/workspace
filesystem preflight failed. Status 4 means interruption or an unexpected
internal failure. A detected mutation is evidence, not a process failure.

## Manifest schema

Each nonblank line is one JSON object with exactly these top-level keys:

```json
{
  "schema_version": "rtl_mutation_manifest_v0.1",
  "mutation_id": "source_001_wrong_reset_polarity_0001",
  "source_id": "source_001",
  "top_module": "top",
  "original_rtl_path": "source_001/original.sv",
  "mutated_rtl_path": "source_001/mutated.sv",
  "repaired_rtl_path": "source_001/repaired.sv",
  "testbench_path": "source_001/testbench.sv",
  "support_files": [],
  "mutation_type": "wrong_reset_polarity",
  "mutated_signal": "rst_n",
  "changed_location": {
    "file": "source_001/mutated.sv",
    "line_start": 14,
    "line_end": 14
  },
  "requested_checks": {
    "compile": true,
    "lint": true,
    "simulation": true,
    "synthesis": true,
    "equivalence": false,
    "activity": false
  }
}
```

The schema version must match exactly. IDs, source ID, mutation type, and
mutated signal must be non-empty strings; IDs must be unique across the whole
file. `top_module` must be a legal Verilog/SystemVerilog identifier. All six
requested-check keys are required exactly once and values must be booleans.
Changed-location file is a relative artifact path and line numbers are
positive integers with `line_end >= line_start`. Support files are a list of
relative artifact paths. Unknown top-level or nested fields are rejected;
there is no type coercion. Blank lines are ignored, but a manifest with no
rows is invalid.

Every artifact path is resolved relative to `workspace_root`. Absolute paths,
`.`/`..` traversal, missing paths, directories, paths escaping the root, and
symlinked paths are rejected. The changed-location file must be one of the
declared RTL/support/testbench artifacts. Symlink rejection applies to every
path component, including a symlinked workspace root. Artifact contents are
never copied into evidence.

## Evidence schema

Each valid row produces exactly one JSONL object with this stable shape:

```json
{
  "schema_version": "rtl_mutation_evidence_v0.1",
  "mutation_id": "...",
  "source_id": "...",
  "top_module": "...",
  "mutation_type": "...",
  "mutated_signal": "...",
  "input_hashes": {
    "original_sha256": "...",
    "mutated_sha256": "...",
    "repaired_sha256": "...",
    "testbench_sha256": "...",
    "support_files": [{"path": "...", "sha256": "..."}]
  },
  "toolchain": {
    "iverilog": {"available": true, "version": "..."},
    "vvp": {"available": true, "version": "..."},
    "verilator": {"available": false, "version": null},
    "yosys": {"available": true, "version": "..."}
  },
  "checks": {},
  "evidence_tier": "A",
  "failure_category": "passed",
  "diagnostics": []
}
```

`input_hashes.support_files` is sorted by manifest path. SHA-256 is computed
from bytes. Tool names and versions are captured once per run; version output
is normalized to a bounded single-line sanitized string. A discovered
executable remains available when its bounded version probe fails, represented
as `{"available": true, "version": null}`. An executable that is not found is
represented as `{"available": false, "version": null}`.

Checks are nested by operation and artifact. Compile has `original`,
`mutated`, and `repaired`; lint and synthesis have `mutated` and `repaired`
(synthesis also records `original`); simulation has `original_passes`,
`mutated_detects_mutation`, and `repaired_passes`; equivalence is
`original_vs_repaired`; activity is `proxy`. Every leaf is exactly a
three-state status: `{attempted: true, passed: true|false, reason: null|...}`
or `{attempted: false, passed: null, reason: "not_requested"|"tool_unavailable"|...}`.
Optional deterministic `duration_ms` is not used, so wall-clock timing never
changes evidence output.

Simulation uses the same Icarus testbench for all three artifacts. Detection
requires successful mutated compilation, testbench execution, and a semantic
mismatch/nonzero testbench result recognized from mismatch/failure output. A
simulation compilation startup error or nonzero compilation return code is
`compile_failure` for original, mutated, and repaired artifacts. A compilation
timeout remains `timeout`. A post-compilation simulation startup error or
unrecognized nonzero simulation return code is `simulation_failure` for the
mutated artifact, while original and repaired execution failures use their
artifact-specific categories. An unexpectedly passing mutation is
`simulation_not_detected` only after compilation and simulation execution
succeed without a recognized mismatch or semantic failure. Repaired
simulation must pass.

Synthesis is independent generic Yosys parsing/synthesis and is never called
functional correctness. Equivalence compares original versus repaired only;
it is limited Yosys combinational/short-sequence support and unsupported or
missing-tool cases remain non-passing states. Activity is `unsupported` when
requested in v0.1.

## Tiers and failure categories

Tier computation is a pure function and the evidence-tier value is exactly
`"A"`, `"B"`, `"C"`, or `null`. Tier A requires valid metadata and all hashes,
all three compile checks passing, original simulation passing, mutated
simulation detecting the mutation, and repaired simulation passing. Tier B
requires valid metadata and hashes, all three compile checks passing, at least
one attempted lint or synthesis check passing, every attempted repaired lint
or synthesis check passing, and Tier A not being satisfied. Tier C requires
valid metadata and hashes, at least one attempted executable compile, lint,
simulation, synthesis, or equivalence check, and neither Tier A nor Tier B.
Tier C is limited executable evidence; it does not mean the design passed.
When metadata and hashes are valid but no executable check was attempted, the
tier is `null`, including when no checks were requested or every requested
tool is unavailable. Tier D is reserved for external teacher/model hypotheses
and is never emitted by `verify-mutations`. A row with preflight or hashing
failure is a batch/row error and is not emitted as valid evidence.

The finite categories are `passed`, `tool_unavailable`, `compile_failure`,
`lint_failure`, `simulation_failure`, `simulation_not_detected`, `original_simulation_failure`,
`repaired_simulation_failure`, `synthesis_failure`, `equivalence_failure`,
`activity_failure`, `timeout`, `path_validation_failure`, `hash_failure`,
`internal_error`, and `partial_failure`. Selection uses this fixed priority
(first applicable category wins): `timeout`, `compile_failure`,
`original_simulation_failure`, `simulation_failure`, `simulation_not_detected`,
`repaired_simulation_failure`, `lint_failure`, `synthesis_failure`,
`equivalence_failure`, `activity_failure`, `tool_unavailable`,
`partial_failure`, `passed`. Thus an attempted failure is never hidden by a
missing optional tool.

## Diagnostics and artifact hygiene

Subprocesses use argument arrays with `shell=False`, bounded timeouts, and
per-row directories inside a newly created managed run directory beneath
`work_dir` (with a random `.rtlbench-run-*` name). A pre-existing predictable
row directory or symlink inside `work_dir` is never used. Diagnostics are
capped at 4096 characters per item, contain command/tool context rather than
RTL, replace workspace, managed-run, and manifest paths with `<workspace>`,
`<work>`, and `<manifest>`, and never expose the random managed-run name.
Environment dumps, credentials, raw RTL, testbench text, and arbitrary shell
strings are not emitted. Generated binaries, logs, VCDs, synthesis scripts,
scratch directories, private RTL, model outputs, and evidence files are
ignored/not committed. Managed run directories are retained as caller-managed
scratch for diagnosis; callers should place `--work-dir` outside the repository
or beneath an ignored scratch path. The verifier never recursively deletes a
pre-existing directory.

## Batch, atomic output, and interruption behavior

The complete JSONL manifest and duplicate IDs are validated before any tool
executes. Existing output is refused unless `--force` is provided. Processing
writes rows to the exact managed partial file `<output>.rtlbench-partial`,
flushes each row, and on normal completion atomically replaces the exact
output path with `os.replace`. On interruption or unexpected failure, an
existing final output is left untouched and the managed partial is retained
for diagnosis; `--force` may replace that exact managed partial on a later
run. No arbitrary user file is removed.

Row-level check failures are represented in evidence and do not stop later
rows. Malformed JSON, schema errors, duplicate IDs, unsafe/missing paths,
unwritable output, or invalid arguments are preflight errors and produce no
replacement final output.

## Tests and compatibility

Unit tests cover strict manifest validation, paths/symlinks, deterministic
hashes/JSON/order, check states, tool absence and fake tool outcomes,
simulation classification, tiers, failure priority, diagnostics, atomic
batch behavior, interruption, and both CLI forms. Tests inject fake
executables and do not require Icarus, Verilator, Yosys, network access, or a
model endpoint. Optional real-tool integration tests are marked `integration`
and skip cleanly when tools are missing. The existing legacy parser and
runner tests remain authoritative for backward compatibility.
