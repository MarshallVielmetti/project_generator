from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import conftest
import pytest
import startergen.check as check_module
from startergen.assembly import AssemblyError
from startergen.check import (
    CheckError,
    _create_runtime,
    _load_contracts,
    _output_snapshot,
    _public_contracts,
    _run_case,
    _Runtime,
    _verify_reproducibility,
    check_project,
)
from startergen.cli import main
from startergen.documentation import DocumentationError

FIXTURE = Path(__file__).parent / "fixtures" / "mvp_canonical_project"
MINIMAL_FIXTURE = Path(__file__).parent / "fixtures" / "minimal_completed_project"


def copy_fixture(tmp_path: Path) -> Path:
    destination = tmp_path / "project"
    shutil.copytree(
        FIXTURE,
        destination,
        ignore=shutil.ignore_patterns("build", ".pytest_cache", "__pycache__"),
    )
    return destination


@pytest.mark.parametrize(
    ("fixture", "dependency_order"),
    [
        (MINIMAL_FIXTURE, ["unicycle-dynamics"]),
        (FIXTURE, ["mean-function", "integrator-step", "rollout"]),
    ],
)
def test_check_cli_validates_both_canonical_fixtures(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fixture: Path,
    dependency_order: list[str],
) -> None:
    project = tmp_path / "project"
    shutil.copytree(
        fixture,
        project,
        ignore=shutil.ignore_patterns("build", ".pytest_cache", "__pycache__"),
    )

    assert main(["check", "--root", str(project), "--json"]) == 0

    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["checked"] is True
    assert report["dependency_order"] == dependency_order
    assert report["starter"]["installed"] is True
    assert report["starter"]["completed_public"] == "passed"
    assert report["reproducible"] is True
    assert not (fixture / "build").exists()


def test_fixture_collection_guard_accepts_new_project_names(tmp_path: Path) -> None:
    new_fixture_test = (
        tmp_path / "tests" / "fixtures" / "new_project" / "test_example.py"
    )
    ordinary_test = tmp_path / "tests" / "test_example.py"
    generator_config = SimpleNamespace(invocation_params=SimpleNamespace(dir=tmp_path))
    fixture_config = SimpleNamespace(
        invocation_params=SimpleNamespace(dir=new_fixture_test.parent.parent)
    )

    assert conftest.pytest_ignore_collect(new_fixture_test, generator_config) is True
    assert conftest.pytest_ignore_collect(ordinary_test, generator_config) is False
    assert conftest.pytest_ignore_collect(new_fixture_test, fixture_config) is False


def test_documented_runner_leaves_checked_in_fixtures_clean() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    runner = (
        repository_root
        / "tools"
        / "startergen"
        / "scripts"
        / "check_canonical_fixtures.py"
    )
    result = subprocess.run(
        [sys.executable, str(runner)],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    reports = json.loads(result.stdout)
    assert all(report["checked"] for report in reports.values())
    assert not (MINIMAL_FIXTURE / "build").exists()
    assert not (FIXTURE / "build").exists()


@pytest.mark.parametrize("uv", ["/usr/bin/uv", None])
def test_runtime_installers_include_project_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uv: str | None
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    commands: list[list[str]] = []

    class FakeEnvBuilder:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def create(self, path: Path) -> None:
            python = path / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()

    def capture_command(command: list[str], **kwargs: object) -> None:
        del kwargs
        commands.append(command)

    monkeypatch.setattr(check_module.venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(check_module.shutil, "which", lambda _executable: uv)
    monkeypatch.setattr(check_module, "_run_command", capture_command)

    runtime = _create_runtime(
        project,
        package="example",
        workspace=tmp_path / "runtime",
        timeout=1,
    )

    assert runtime.python == tmp_path / "runtime" / "venv" / "bin" / "python"
    if uv is not None:
        assert commands[0] == [
            uv,
            "pip",
            "install",
            "--python",
            str(runtime.python),
            str(project),
        ]
    else:
        assert commands[0] == [
            str(runtime.python),
            "-m",
            "pip",
            "install",
            str(project),
        ]
    assert "--no-deps" not in commands[0]


def test_canonical_suite_uses_canonical_project_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(Path("/isolated/canonical-runtime"), Path("/isolated/python"))
    created_for: list[Path] = []

    def create_runtime(project: Path, **kwargs: object) -> _Runtime:
        del kwargs
        created_for.append(project)
        return runtime

    def stop_after_canonical_suite(
        python: Path, project: Path, nodes: list[str], **kwargs: object
    ) -> None:
        assert python == runtime.python
        assert project == FIXTURE.resolve()
        assert nodes == ["tests/private", "tests/public", "tests/smoke"]
        assert kwargs["virtualenv"] == runtime.root / "venv"
        assert kwargs["pythonpath"] == FIXTURE.resolve() / "src"
        raise CheckError("canonical_suite_observed", "stop after canonical suite")

    monkeypatch.setattr(check_module, "_create_runtime", create_runtime)
    monkeypatch.setattr(check_module, "_run_suite", stop_after_canonical_suite)

    with pytest.raises(CheckError) as error:
        check_project(FIXTURE)

    assert error.value.code == "canonical_suite_observed"
    assert created_for == [FIXTURE.resolve()]


def test_case_does_not_reuse_a_stale_junit_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    nodeid = "tests/public/test_example.py::test_example"
    report_path = report_dir / (
        check_module.hashlib.sha256(nodeid.encode()).hexdigest() + ".xml"
    )
    report_path.write_text("stale", encoding="utf-8")

    def fail_before_writing_report(*args: object, **kwargs: object) -> None:
        assert not report_path.exists()
        raise CheckError("test_runner_failed", "simulated pytest crash")

    monkeypatch.setattr(check_module, "_run_command", fail_before_writing_report)
    with pytest.raises(CheckError) as error:
        _run_case(
            _Runtime(tmp_path / "runtime", Path("python")),
            project,
            nodeid,
            timeout=1,
            report_dir=report_dir,
        )

    assert error.value.code == "test_report_missing"


def test_validation_child_environment_excludes_publication_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    nodeid = "tests/smoke/test_example.py::test_example"
    seen: dict[str, str] = {}

    for name in (
        "STARTER_REPO_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
    ):
        monkeypatch.setenv(name, "must-not-reach-validation")

    def capture_environment(*args: object, **kwargs: object) -> None:
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        seen.update(environment)
        report_path = report_dir / (
            check_module.hashlib.sha256(nodeid.encode()).hexdigest() + ".xml"
        )
        report_path.write_text(
            '<testsuite><testcase classname="tests.smoke" name="test_example" />'
            "</testsuite>",
            encoding="utf-8",
        )

    monkeypatch.setattr(check_module, "_run_command", capture_environment)
    result = _run_case(
        _Runtime(tmp_path / "runtime", Path("python")),
        project,
        nodeid,
        timeout=1,
        report_dir=report_dir,
    )

    assert result.outcome == "passed"
    assert all(
        name not in seen
        for name in (
            "STARTER_REPO_TOKEN",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
        )
    )


def test_reproducibility_uses_configured_output_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "input.txt").write_text("input", encoding="utf-8")
    output_paths = ("build/student", "build/generated", "build/web")

    def build_custom_outputs(project: Path, **kwargs: object) -> None:
        for relative in output_paths:
            output = project / relative
            output.mkdir(parents=True)
            (output / "marker.txt").write_text("same", encoding="utf-8")

    monkeypatch.setattr(check_module, "build_documentation", build_custom_outputs)

    assert _verify_reproducibility(
        root, workspace=tmp_path / "workspace", output_paths=output_paths
    )
    assert _output_snapshot(
        tmp_path / "workspace" / "repro-a", output_paths
    ) == _output_snapshot(tmp_path / "workspace" / "repro-b", output_paths)


@pytest.mark.parametrize("error_type", [AssemblyError, DocumentationError])
def test_check_cli_serializes_stage_failures(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[AssemblyError | DocumentationError],
) -> None:
    error = error_type("stage_failed", "simulated stage failure")

    def fail_check(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr("startergen.cli.check_project", fail_check)

    assert main(["check", "--root", str(tmp_path), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "checked": False,
        "code": "stage_failed",
        "message": "simulated stage failure",
        "details": {"diagnostics": []},
    }


def test_duplicate_baselines_are_rejected_before_lookup() -> None:
    _config, metadata = _load_contracts(FIXTURE)
    first = metadata.exercises[0]
    duplicate = first.model_copy(
        update={
            "tests": first.tests.model_copy(
                update={"baseline": first.tests.baseline * 2}
            )
        }
    )

    with pytest.raises(CheckError) as error:
        _public_contracts((duplicate, *metadata.exercises[1:]))

    assert error.value.code == "duplicate_baseline_test"
