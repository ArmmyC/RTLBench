import json
import os
from pathlib import Path

import pytest

from rtlbench.candidate_manifest import (
    CandidateManifestValidationError,
    CandidateWorkspaceValidationError,
    load_manifest,
)


FIXTURE = Path(__file__).parent / "fixtures" / "candidate_verification"


def _row(**overrides):
    row = {
        "schema_version": "rtl_candidate_manifest_v0.1",
        "candidate_id": "candidate-1",
        "task_id": "task-1",
        "source_id": "source-1",
        "attempt": 1,
        "top_module": "TopModule",
        "candidate_rtl_path": "candidate.sv",
        "testbench_path": "testbench.sv",
        "support_files": [],
        "requested_checks": {
            "compile": True,
            "simulation": True,
            "lint": False,
            "synthesis": False,
        },
    }
    row.update(overrides)
    return row


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "candidate.sv").write_text("module TopModule; endmodule\n", encoding="utf-8")
    (root / "testbench.sv").write_text("module tb; endmodule\n", encoding="utf-8")
    (root / "support.sv").write_text("module helper; endmodule\n", encoding="utf-8")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps(_row()) + "\n", encoding="utf-8")
    return manifest, root


def _load(tmp_path: Path, rows) -> None:
    manifest, root = _workspace(tmp_path)
    manifest.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return load_manifest(manifest, root)


def test_valid_manifest_ignores_blank_lines(tmp_path: Path):
    manifest, root = _workspace(tmp_path)
    manifest.write_text("\n" + manifest.read_text() + "\n", encoding="utf-8")
    rows = load_manifest(manifest, root)
    assert rows[0].candidate_id == "candidate-1"
    assert rows[0].attempt == 1


@pytest.mark.parametrize(
    "content,needle",
    [
        ("not json\n", "malformed JSON"),
        (json.dumps(_row(schema_version="wrong")) + "\n", "schema_version"),
        (json.dumps({**_row(), "unknown": 1}) + "\n", "unknown fields"),
        (json.dumps({key: value for key, value in _row().items() if key != "task_id"}) + "\n", "missing fields"),
    ],
)
def test_schema_errors(tmp_path: Path, content: str, needle: str):
    manifest, root = _workspace(tmp_path)
    manifest.write_text(content, encoding="utf-8")
    with pytest.raises(CandidateManifestValidationError, match=needle):
        load_manifest(manifest, root)


@pytest.mark.parametrize("field", ["candidate_id", "task_id", "source_id"])
def test_required_ids_are_nonempty_strings(tmp_path: Path, field: str):
    manifest, root = _workspace(tmp_path)
    manifest.write_text(json.dumps(_row(**{field: "  "})) + "\n", encoding="utf-8")
    with pytest.raises(CandidateManifestValidationError, match=field):
        load_manifest(manifest, root)


def test_duplicate_candidate_ids_and_task_attempts_are_rejected(tmp_path: Path):
    manifest, root = _workspace(tmp_path)
    manifest.write_text(
        json.dumps(_row()) + "\n" + json.dumps(_row(task_id="task-2")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(CandidateManifestValidationError, match="duplicate candidate_id"):
        load_manifest(manifest, root)

    manifest.write_text(
        json.dumps(_row(candidate_id="candidate-1"))
        + "\n"
        + json.dumps(_row(candidate_id="candidate-2"))
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(CandidateManifestValidationError, match="task_id, attempt"):
        load_manifest(manifest, root)


@pytest.mark.parametrize("attempt", [0, -1, True, False, "1", 1.0])
def test_attempt_is_a_non_boolean_positive_integer(tmp_path: Path, attempt):
    manifest, root = _workspace(tmp_path)
    manifest.write_text(json.dumps(_row(attempt=attempt)) + "\n", encoding="utf-8")
    with pytest.raises(CandidateManifestValidationError, match="attempt"):
        load_manifest(manifest, root)


@pytest.mark.parametrize("top_module", ["", "bad-name", "1top", "a b", "a\\b"])
def test_top_module_is_an_identifier(tmp_path: Path, top_module: str):
    manifest, root = _workspace(tmp_path)
    manifest.write_text(json.dumps(_row(top_module=top_module)) + "\n", encoding="utf-8")
    with pytest.raises(CandidateManifestValidationError, match="top_module"):
        load_manifest(manifest, root)


@pytest.mark.parametrize(
    "checks",
    [
        {"compile": True, "simulation": True, "lint": False},
        {"compile": False, "simulation": True, "lint": False, "synthesis": False},
        {"compile": True, "simulation": False, "lint": False, "synthesis": False},
        {"compile": 1, "simulation": True, "lint": False, "synthesis": False},
        {"compile": True, "simulation": True, "lint": 0, "synthesis": False},
    ],
)
def test_requested_checks_are_exact_booleans_and_gates(tmp_path: Path, checks):
    manifest, root = _workspace(tmp_path)
    manifest.write_text(json.dumps(_row(requested_checks=checks)) + "\n", encoding="utf-8")
    with pytest.raises(CandidateManifestValidationError, match="requested_checks"):
        load_manifest(manifest, root)


@pytest.mark.parametrize(
    "field,value",
    [
        ("support_files", ["support.sv", "support.sv"]),
        ("support_files", ["candidate.sv"]),
        ("support_files", ["testbench.sv"]),
        ("testbench_path", "candidate.sv"),
    ],
)
def test_duplicate_support_and_role_paths_are_rejected(tmp_path: Path, field: str, value):
    manifest, root = _workspace(tmp_path)
    manifest.write_text(json.dumps(_row(**{field: value})) + "\n", encoding="utf-8")
    with pytest.raises(CandidateManifestValidationError, match="support_files|path"):
        load_manifest(manifest, root)


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/candidate.sv",
        "C:/candidate.sv",
        "C:\\candidate.sv",
        "dir\\candidate.sv",
        "candidate//nested.sv",
        "./candidate.sv",
        "dir/../candidate.sv",
        "../candidate.sv",
        "candidate.sv\x00bad",
    ],
)
def test_artifact_paths_must_be_normalized_posix_relative_paths(tmp_path: Path, path: str):
    manifest, root = _workspace(tmp_path)
    manifest.write_text(json.dumps(_row(candidate_rtl_path=path)) + "\n", encoding="utf-8")
    with pytest.raises(CandidateManifestValidationError, match="path"):
        load_manifest(manifest, root)


def test_missing_file_and_directory_are_rejected(tmp_path: Path):
    manifest, root = _workspace(tmp_path)
    manifest.write_text(json.dumps(_row(candidate_rtl_path="missing.sv")) + "\n", encoding="utf-8")
    with pytest.raises(CandidateManifestValidationError, match="regular file"):
        load_manifest(manifest, root)

    manifest.write_text(json.dumps(_row(candidate_rtl_path=".")) + "\n", encoding="utf-8")
    with pytest.raises(CandidateManifestValidationError):
        load_manifest(manifest, root)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_symlinked_artifact_and_workspace_root_are_rejected(tmp_path: Path):
    manifest, root = _workspace(tmp_path)
    (root / "candidate-link.sv").symlink_to(root / "candidate.sv")
    manifest.write_text(json.dumps(_row(candidate_rtl_path="candidate-link.sv")) + "\n", encoding="utf-8")
    with pytest.raises(CandidateManifestValidationError, match="symlink"):
        load_manifest(manifest, root)

    linked_root = tmp_path / "workspace-link"
    linked_root.symlink_to(root, target_is_directory=True)
    manifest.write_text(json.dumps(_row()) + "\n", encoding="utf-8")
    with pytest.raises(CandidateWorkspaceValidationError, match="workspace-root"):
        load_manifest(manifest, linked_root)


def test_empty_manifest_is_rejected(tmp_path: Path):
    manifest, root = _workspace(tmp_path)
    manifest.write_text("\n  \n", encoding="utf-8")
    with pytest.raises(CandidateManifestValidationError, match="no nonblank"):
        load_manifest(manifest, root)


def test_fixture_manifest_preserves_order_and_paths():
    rows = load_manifest(FIXTURE / "manifest.jsonl", FIXTURE)
    assert [row.candidate_id for row in rows] == [
        "rtlgen_prob001_attempt_01",
        "rtlgen_prob001_compile_failure_attempt_01",
        "rtlgen_prob001_functional_mismatch_attempt_01",
    ]
    assert rows[0].candidate_rtl_path == "passing/candidate.sv"
