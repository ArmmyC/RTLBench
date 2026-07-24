import json
import os
from pathlib import Path

import pytest

from rtlbench.mutation_manifest import ManifestValidationError, load_manifest


FIXTURE = Path(__file__).parent / "fixtures" / "mutation_verification"


def _row(**overrides):
    row = {
        "schema_version": "rtl_mutation_manifest_v0.1",
        "mutation_id": "m1",
        "source_id": "s1",
        "top_module": "top",
        "original_rtl_path": "source_001/original.sv",
        "mutated_rtl_path": "source_001/mutated.sv",
        "repaired_rtl_path": "source_001/repaired.sv",
        "testbench_path": "source_001/testbench.sv",
        "support_files": [],
        "mutation_type": "wrong_operator",
        "mutated_signal": "y",
        "changed_location": {"file": "source_001/mutated.sv", "line_start": 2, "line_end": 2},
        "requested_checks": {
            "compile": True,
            "lint": True,
            "simulation": True,
            "synthesis": True,
            "equivalence": False,
            "activity": False,
        },
    }
    row.update(overrides)
    return row


def _manifest(tmp_path: Path, rows) -> tuple[Path, Path]:
    root = tmp_path / "root"
    (root / "source_001").mkdir(parents=True)
    for name in ("original.sv", "mutated.sv", "repaired.sv", "testbench.sv"):
        (root / "source_001" / name).write_text("module top; endmodule\n", encoding="utf-8")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return manifest, root


def test_valid_manifest_and_blank_lines(tmp_path: Path):
    manifest, root = _manifest(tmp_path, [_row()])
    manifest.write_text("\n" + manifest.read_text() + "\n", encoding="utf-8")
    rows = load_manifest(manifest, root)
    assert rows[0].mutation_id == "m1"
    assert rows[0].changed_location.line_start == 2


@pytest.mark.parametrize(
    "content,needle",
    [
        ("not json\n", "malformed JSON"),
        (json.dumps(_row(schema_version="wrong")) + "\n", "schema_version"),
        (json.dumps({**_row(), "unknown": 1}) + "\n", "unknown fields"),
        (json.dumps({key: value for key, value in _row().items() if key != "mutation_type"}) + "\n", "missing fields"),
    ],
)
def test_manifest_schema_errors(tmp_path: Path, content: str, needle: str):
    manifest, root = _manifest(tmp_path, [_row()])
    manifest.write_text(content, encoding="utf-8")
    with pytest.raises(ManifestValidationError, match=needle):
        load_manifest(manifest, root)


def test_duplicate_ids_are_rejected(tmp_path: Path):
    manifest, root = _manifest(tmp_path, [_row(), _row(mutation_type="other")])
    with pytest.raises(ManifestValidationError, match="duplicate"):
        load_manifest(manifest, root)


@pytest.mark.parametrize("path", ["/tmp/original.sv", "../original.sv", "source_001/../original.sv"])
def test_unsafe_paths_are_rejected(tmp_path: Path, path: str):
    manifest, root = _manifest(tmp_path, [_row(original_rtl_path=path)])
    with pytest.raises(ManifestValidationError, match="path"):
        load_manifest(manifest, root)


def test_wrong_types_and_requested_check_keys_are_rejected(tmp_path: Path):
    manifest, root = _manifest(tmp_path, [_row(requested_checks={"compile": 1})])
    with pytest.raises(ManifestValidationError):
        load_manifest(manifest, root)


def test_missing_file_directory_and_symlink_are_rejected(tmp_path: Path):
    manifest, root = _manifest(tmp_path, [_row(original_rtl_path="source_001/missing.sv")])
    with pytest.raises(ManifestValidationError, match="regular file"):
        load_manifest(manifest, root)

    manifest, root = _manifest(tmp_path / "directory", [_row(original_rtl_path="source_001")])
    with pytest.raises(ManifestValidationError, match="regular file"):
        load_manifest(manifest, root)

    if hasattr(os, "symlink"):
        manifest, root = _manifest(tmp_path / "symlink", [_row()])
        source = root / "source_001" / "original.sv"
        target = root / "source_001" / "alias.sv"
        target.symlink_to(source)
        manifest.write_text(json.dumps(_row(original_rtl_path="source_001/alias.sv")) + "\n", encoding="utf-8")
        with pytest.raises(ManifestValidationError, match="symlink"):
            load_manifest(manifest, root)
