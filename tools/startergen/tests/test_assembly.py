from __future__ import annotations

import hashlib
import json
import shutil
import stat
from pathlib import Path

import pytest
from startergen import assembly
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
    assert (output / "tests/public/test_unicycle.py").is_file()
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
    source_bytes = (project / "src/lab_project/dynamics/unicycle.py").read_bytes()
    source_record = next(
        entry
        for entry in manifest["provenance"]["inputs"]
        if entry["path"] == "src/lab_project/dynamics/unicycle.py"
    )
    assert source_record["sha256"] == hashlib.sha256(source_bytes).hexdigest()
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

    assert (result.output / "tests/public/test_unicycle.py").exists()
    assert not (result.output / "tests/instructor").exists()
    assert not (result.output / "tests/generation").exists()
    assert not (result.output / "tests/public/.env").exists()


def test_teaching_contracts_cannot_be_allowlisted(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)

    with pytest.raises(AssemblyError) as error:
        enumerate_allowlist(project, ["teaching"])

    assert error.value.code == "denied_input"


def test_case_colliding_directory_components_are_rejected(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    (project / "Foo").mkdir()
    (project / "Foo" / "a.py").write_text("a = 1\n", encoding="utf-8")
    if (project / "foo").exists():
        pytest.skip("test filesystem is case-insensitive")
    (project / "foo").mkdir()
    (project / "foo" / "b.py").write_text("b = 1\n", encoding="utf-8")

    with pytest.raises(AssemblyError) as error:
        enumerate_allowlist(project, ["Foo", "foo"])

    assert error.value.code == "artifact_collision"


def test_declared_hard_link_path_must_be_allowlisted(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    source = project / "src/lab_project/dynamics/unicycle.py"
    alias = source.with_name("alias.py")
    alias.hardlink_to(source)
    text = _config(project).read_text(encoding="utf-8")
    _config(project).write_text(
        text.replace(
            "    - src/lab_project\n", "    - src/lab_project/dynamics/alias.py\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssemblyError) as error:
        build_project(project)

    assert error.value.code == "source_not_allowlisted"


def test_read_only_source_is_writable_during_transform_then_restored(
    tmp_path: Path,
) -> None:
    project = copy_fixture(tmp_path)
    source = project / "src/lab_project/dynamics/unicycle.py"
    source.chmod(0o444)

    result = build_project(project)

    assert (
        stat.S_IMODE((result.output / source.relative_to(project)).stat().st_mode)
        == 0o444
    )


def test_duplicate_allowlist_outputs_fail_before_promotion(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    text = _config(project).read_text(encoding="utf-8")
    _config(project).write_text(
        text.replace(
            "    - tests/public\n",
            "    - tests/public\n    - tests/public/test_unicycle.py\n",
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
    assert "src/lab_project/dynamics/unicycle.py" in str(error.value)
    assert ".startergen-stage-" not in str(error.value)
    assert "unicycle-dynamics" in str(error.value)
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


def test_symlinked_lock_is_rejected_without_following_it(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    build_root = project / "build"
    build_root.mkdir()
    outside_lock = tmp_path / "outside-lock"
    outside_lock.write_bytes(b"original")
    (build_root / ".startergen.lock").symlink_to(outside_lock)

    with pytest.raises(AssemblyError) as error:
        build_project(project)

    assert error.value.code == "unsafe_lock"
    assert outside_lock.read_bytes() == b"original"


def test_lock_path_is_reserved_as_a_destination(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    text = _config(project).read_text(encoding="utf-8")
    _config(project).write_text(
        text.replace("build/starter", "build/.startergen.lock", 1),
        encoding="utf-8",
    )

    with pytest.raises(AssemblyError) as error:
        build_project(project)

    assert error.value.code == "reserved_destination"


def test_backup_cleanup_failure_is_nonfatal_after_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = copy_fixture(tmp_path)
    build_project(project)
    real_rmtree = assembly.shutil.rmtree

    def fail_backup_cleanup(path: str | Path, *args: object, **kwargs: object) -> None:
        if Path(path).name.startswith(".startergen-backup-"):
            raise OSError("simulated cleanup failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(assembly.shutil, "rmtree", fail_backup_cleanup)
    result = build_project(project)

    assert result.output.is_dir()
    assert any(
        path.name.startswith(".startergen-backup-")
        for path in (project / "build").iterdir()
    )


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
