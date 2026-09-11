from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from startergen.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "mvp_canonical_project"


def copy_fixture(tmp_path: Path) -> Path:
    destination = tmp_path / "project"
    shutil.copytree(
        FIXTURE,
        destination,
        ignore=shutil.ignore_patterns("build", ".pytest_cache", "__pycache__"),
    )
    return destination


def test_check_cli_validates_mvp_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = copy_fixture(tmp_path)

    assert main(["check", "--root", str(project), "--json"]) == 0

    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["checked"] is True
    assert report["dependency_order"] == [
        "mean-function",
        "integrator-step",
        "rollout",
    ]
    assert report["starter"]["installed"] is True
    assert report["starter"]["completed_public"] == "passed"
    assert report["reproducible"] is True
