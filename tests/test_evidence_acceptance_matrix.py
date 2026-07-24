import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / ".scratch" / "evidence-integrity-remediation" / "spec.md"
MAPPING = ROOT / "tests" / "fixtures" / "evidence_integrity_acceptance_matrix.json"


def _acceptance_rows() -> tuple[tuple[str, str], ...]:
    lines = SPEC.read_text(encoding="utf-8").splitlines()
    start = lines.index("## Acceptance matrix")
    rows = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        if cells == ("Scenario", "Required result"):
            continue
        assert len(cells) == 2
        rows.append(cells)
    return tuple(rows)


def _test_function(node_id: str) -> tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]:
    relative_path, separator, function_name = node_id.partition("::")
    assert separator, f"test node ID must use path::function: {node_id}"
    path = ROOT / relative_path
    assert path.is_file(), f"mapped test file does not exist: {relative_path}"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches = tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    )
    assert len(matches) == 1, f"mapped test function does not exist once: {node_id}"
    return path, matches[0]


def _decorator_tokens(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return {
        token
        for decorator in node.decorator_list
        for token in ast.unparse(decorator).split(".")
    }


@pytest.mark.unit
def test_every_acceptance_matrix_row_maps_to_existing_deterministic_tests():
    mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
    expected_rows = _acceptance_rows()
    mapped_rows = tuple(
        (entry["scenario"], entry["required_result"])
        for entry in mapping["rows"]
    )

    assert mapping["matrix_version"] == "1.0"
    assert mapped_rows == expected_rows
    for entry in mapping["rows"]:
        assert entry["tests"], f"acceptance row has no tests: {entry['scenario']}"
        for node_id in entry["tests"]:
            path, function = _test_function(node_id)
            assert not path.name.endswith("_live.py")
            assert not {"live", "network"}.intersection(_decorator_tokens(function))
