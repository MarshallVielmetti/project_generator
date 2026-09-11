from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from startergen.cli import main
from startergen.validate import validate_project

FIXTURE = Path(__file__).parent / "fixtures" / "minimal_completed_project"


def copy_fixture(tmp_path: Path) -> Path:
    destination = tmp_path / "project"
    shutil.copytree(FIXTURE, destination)
    return destination


def write_config(project: Path, text: str) -> None:
    (project / "teaching" / "config.yml").write_text(text, encoding="utf-8")


def write_exercises(project: Path, text: str) -> None:
    (project / "teaching" / "exercises.yml").write_text(text, encoding="utf-8")


def test_minimal_fixture_is_valid() -> None:
    report = validate_project(FIXTURE)
    assert report.ok, report.to_json()


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    write_config(project, "schema_version: 1\nschema_version: 1\n")
    report = validate_project(project)
    assert not report.ok
    assert any(d.code == "duplicate_yaml_key" for d in report.diagnostics)


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    write_config(
        project,
        """schema_version: 1
project:
  name: Minimal
  import_package: lab_project
  typo: should-fail
starter:
  output: build/starter
  include: [src/lab_project]
documentation:
  source: teaching/project.md
  assets: teaching/assets
  background: teaching/background
  readme_template: teaching/templates/README.md.j2
  generated_source: build/docs-src
  site_output: build/site
""",
    )
    report = validate_project(project)
    assert any(d.code == "unknown_field" for d in report.diagnostics)


def test_missing_required_values_are_rejected(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    write_config(project, "schema_version: 1\nproject: {}\n")
    report = validate_project(project)
    assert any(d.code == "missing_required_field" for d in report.diagnostics)


@pytest.mark.parametrize(
    "unsafe", ["/tmp/output", "../output", "folder/../output", r"C:\\output"]
)
def test_unsafe_output_paths_are_rejected(tmp_path: Path, unsafe: str) -> None:
    project = copy_fixture(tmp_path)
    text = (project / "teaching" / "config.yml").read_text(encoding="utf-8")
    write_config(project, text.replace("build/starter", unsafe, 1))
    report = validate_project(project)
    assert any(
        d.code in {"unsafe_destination", "destination_outside_build"}
        for d in report.diagnostics
    )


def test_source_output_overlap_is_rejected(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    text = (project / "teaching" / "config.yml").read_text(encoding="utf-8")
    write_config(project, text.replace("build/starter", "src/lab_project", 1))
    report = validate_project(project)
    assert any(d.code == "source_output_overlap" for d in report.diagnostics)


def test_destination_case_collision_is_rejected(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    text = (project / "teaching" / "config.yml").read_text(encoding="utf-8")
    write_config(project, text.replace("build/site", "build/STARTER", 1))
    report = validate_project(project)
    assert any(d.code == "destination_collision" for d in report.diagnostics)


def test_dependency_cycle_is_rejected(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    write_exercises(
        project,
        """schema_version: 1
exercises:
  - id: first
    title: First
    source: {file: src/lab_project/dynamics/unicycle.py, symbol: UnicycleDynamics.f}
    requires: [second]
    starter: {strategy: replace_body, docstring: preserve}
    tests:
      public: [tests/public/cases.py::test_forward_motion]
      baseline: [{nodeid: tests/public/cases.py::test_forward_motion, expected: stub_error}]
  - id: second
    title: Second
    source: {file: src/lab_project/dynamics/unicycle.py, symbol: UnicycleDynamics.f}
    requires: [first]
    starter: {strategy: replace_body, docstring: preserve}
    tests:
      public: [tests/public/cases.py::test_forward_motion]
      baseline: [{nodeid: tests/public/cases.py::test_forward_motion, expected: stub_error}]
""",
    )
    report = validate_project(project)
    assert any(d.code == "exercise_dependency_cycle" for d in report.diagnostics)


def test_symlinked_input_is_rejected(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    link = project / "teaching" / "linked-assets"
    link.symlink_to(project / "teaching" / "assets", target_is_directory=True)
    text = (project / "teaching" / "config.yml").read_text(encoding="utf-8")
    write_config(project, text.replace("teaching/assets", "teaching/linked-assets", 1))
    report = validate_project(project)
    assert any(d.code == "symlink_input" for d in report.diagnostics)


def test_json_cli_has_stable_exit_and_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = copy_fixture(tmp_path)
    write_config(project, "schema_version: 1\n")
    exit_code = main(["validate", "--root", str(project), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["valid"] is False
    assert isinstance(payload["diagnostics"], list)


def test_cli_validates_fixture(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", "--root", str(FIXTURE)]) == 0
    assert "Valid startergen project" in capsys.readouterr().out
