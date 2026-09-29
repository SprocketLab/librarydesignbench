"""The persisted LDB result validates its references and omits absent values."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from lib_design_bench.models.reports import LdbResult


@pytest.fixture
def document() -> dict[str, Any]:
    """Provide one small valid result."""
    return {
        "schema_version": 1,
        "id": "evaluation-1",
        "meta": {
            "type": "evaluation",
            "started_at": "2026-09-24T13:49:38Z",
            "repo_commit": "abc",
            "tasks_hash": "tasks",
            "complete": True,
        },
        "implementors": {"impl": {"agent": "codex", "model": "m"}},
        "libraries": {"p/no-library": {"type": "no-library", "task": "p"}},
        "trials": [
            {
                "name": "t1",
                "task": "p",
                "attempt": 1,
                "problem": "q",
                "library": "p/no-library",
                "implementor": "impl",
                "outcome": "finished",
                "score": 0.5,
            }
        ],
    }


def test_roundtrip_omits_absent_metrics(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    """Absent metrics stay absent through a save and load."""
    result = LdbResult.model_validate(document)
    path = tmp_path / "ldb-result.json"
    path.write_text(result.to_json())
    saved = json.loads(path.read_text())
    assert saved["schema_version"] == 1
    assert "simplicity" not in saved["trials"][0]
    assert LdbResult.load(path) == result


def test_trial_references_are_checked(document: dict[str, Any]) -> None:
    """Every compact row references a known implementor and same-task library."""
    for changes in (
        {"library": "missing"},
        {"implementor": "missing"},
        {"task": "different"},
    ):
        broken = {**document, "trials": [{**document["trials"][0], **changes}]}
        with pytest.raises(ValidationError):
            LdbResult.model_validate(broken)
    duplicate = {**document, "trials": document["trials"] * 2}
    with pytest.raises(ValidationError):
        LdbResult.model_validate(duplicate)
