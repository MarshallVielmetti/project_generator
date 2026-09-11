from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from startergen.assembly import AssemblyError, build_project, enumerate_allowlist
from startergen.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "minimal_completed_project"


def copy_fixture(tmp_path: Path) -> Path:
    destination = tmp_path / "project"
    shutil.copytree(FIXTURE, destination)
    return destination


def _config(project: Path) -> Path:
    return project / "teaching" / "config.yml"


def test_build_assembles_transforms_and_writes_deterministic_manifest(
    tmp_path: Path,
) -> None:
    project = copy_fixture(tmp_path)

    result = build_project(project)
    output = project / "build" / "starter"
    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))

    assert result.output == output
    assert output.is_dir()
    assert "NotImplementedError" in (
        output / "src/lab_project/dynamics/unicycle.py"
    ).read_text(encoding="utf-8")
    assert (output / "tests/public/cases.py").is_file()
    assert not (output / "teaching").exists()
    assert not (output / "tools").exists()
    assert not any("__pycache__" in path.parts for path in output.rglob("*"))
    assert ".startergen/manifest.json" not in {
        entry["path"] for entry in manifest["files"]
    }
    assert [entry["path"] for entry in manifest["files"]] == sorted(
        entry["path"] for entry in manifest["files"]
    )
    assert {entry["path"] for entry in manifest["provenance"]["inputs"]} >= {
        "teaching/config.yml",
        "teaching/exercises.yml",
        "src/lab_project/dynamics/unicycle.py",
    }
    assert str(project) not in result.manifest.read_text(encoding="utf-8")
    assert result.transformed_files == ("src/lab_project/dynamics/unicycle.py",)


def test_allowlist_skips_denied_descendants(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    (project / "tests" / "instructor").mkdir()
    (project / "tests" / "instructor" / "answer.py").write_text(
        "solution = 42\n", encoding="utf-8"
    )
    (project / "tests" / "generation").mkdir()
    (project / "tests" / "generation" / "test_generator.py").write_text(
        "assert False\n", encoding="utf-8"
    )
    (project / "tests" / "public" / ".env").write_text(
        "TOKEN=secret\n", encoding="utf-8"
    )
    text = _config(project).read_text(encoding="utf-8")
    text = text.replace("tests/public", "tests").replace("    - tests/smoke\n", "")
    _config(project).write_text(text, encoding="utf-8")

    result = build_project(project)

    assert (result.output / "tests/public/cases.py").exists()
    assert not (result.output / "tests/instructor").exists()
    assert not (result.output / "tests/generation").exists()
    assert not (result.output / "tests/public/.env").exists()


def test_duplicate_allowlist_outputs_fail_before_promotion(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    text = _config(project).read_text(encoding="utf-8")
    _config(project).write_text(
        text.replace(
            "    - tests/public\n", "    - tests/public\n    - tests/public/cases.py\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssemblyError) as error:
        build_project(project)

    assert error.value.code == "artifact_collision"
    assert not (project / "build" / "starter").exists()


def test_failed_build_preserves_previous_successful_output(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    build_project(project)
    output = project / "build" / "starter"
    previous = (output / "src/lab_project/dynamics/unicycle.py").read_bytes()
    source = project / "src/lab_project/dynamics/unicycle.py"
    source.write_text("class (\n", encoding="utf-8")

    with pytest.raises(AssemblyError) as error:
        build_project(project)

    assert error.value.code == "source_parse_error"
    assert (output / "src/lab_project/dynamics/unicycle.py").read_bytes() == previous
    assert not any(
        path.name.startswith(".startergen-stage-")
        for path in (project / "build").iterdir()
    )


def test_symlinked_output_is_rejected_without_touching_target(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "build").mkdir()
    (project / "build" / "starter").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AssemblyError) as error:
        build_project(project)

    assert error.value.code == "validation_failed"
    assert not any(outside.iterdir())


def test_repeated_builds_have_identical_artifact_bytes(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    first = build_project(project)

    def snapshot() -> dict[str, bytes]:
        output = project / "build" / "starter"
        return {
            path.relative_to(output).as_posix(): path.read_bytes()
            for path in output.rglob("*")
            if path.is_file()
        }

    first_snapshot = snapshot()
    second = build_project(project)

    assert second.files == first.files
    assert snapshot() == first_snapshot


def test_enumeration_rejects_symlinked_allowlist_component(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    (project / "alias").symlink_to(project / "src", target_is_directory=True)

    with pytest.raises(AssemblyError) as error:
        enumerate_allowlist(project, ["alias"])

    assert error.value.code == "symlink_input"


def test_build_cli_emits_json_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = copy_fixture(tmp_path)

    assert main(["build", "--root", str(project), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["output"].endswith("build/starter")
    assert payload["transformed_files"] == ["src/lab_project/dynamics/unicycle.py"]
