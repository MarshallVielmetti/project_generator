"""Integrated MVP verification for canonical teaching projects.

The checker deliberately treats the generated starter as a separate runtime:
it installs the artifact into a temporary virtual environment, runs smoke and
public tests from copied starter workspaces, and never imports the canonical
checkout while validating student behavior.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from startergen.assembly import build_project
from startergen.documentation import DocumentationResult, build_documentation
from startergen.schema import BaselineTest, Exercise, ExerciseMetadata, ProjectMetadata
from startergen.transform import TransformTarget, transform_sources
from startergen.validate import validate_project
from startergen.yaml_io import load_yaml_bytes


class CheckError(RuntimeError):
    """Actionable error raised when an integrated MVP check fails."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.cause = cause


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Machine-readable summary of a complete integrated verification."""

    project: Path
    artifact: Path
    dependency_order: tuple[str, ...]
    canonical_tests: tuple[str, ...]
    baseline: dict[str, str]
    checkpoints: tuple[dict[str, Any], ...]
    completed_public: str
    reproducible: bool
    installed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": True,
            "project": self.project.as_posix(),
            "artifact": self.artifact.as_posix(),
            "dependency_order": list(self.dependency_order),
            "canonical_tests": list(self.canonical_tests),
            "starter": {
                "installed": self.installed,
                "baseline": dict(self.baseline),
                "checkpoints": list(self.checkpoints),
                "completed_public": self.completed_public,
            },
            "reproducible": self.reproducible,
        }


@dataclass(frozen=True, slots=True)
class _CaseResult:
    outcome: str
    details: str


@dataclass(frozen=True, slots=True)
class _Runtime:
    root: Path
    python: Path


def _load_contracts(root: Path) -> tuple[ProjectMetadata, ExerciseMetadata]:
    config = ProjectMetadata.model_validate(
        load_yaml_bytes((root / "teaching" / "config.yml").read_bytes())
    )
    exercises = ExerciseMetadata.model_validate(
        load_yaml_bytes((root / "teaching" / "exercises.yml").read_bytes())
    )
    return config, exercises


def _clean_environment(*, pythonpath: Path | None = None, virtualenv: Path | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    for credential in (
        "STARTER_REPO_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
    ):
        environment.pop(credential, None)
    environment["PYTHONNOUSERSITE"] = "1"
    if pythonpath is not None:
        environment["PYTHONPATH"] = str(pythonpath)
    if virtualenv is not None:
        environment["VIRTUAL_ENV"] = str(virtualenv)
    return environment


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
    code: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckError(
            "test_timeout",
            f"command exceeded the {timeout:g}-second timeout: {' '.join(command)}",
            details={"command": command, "stdout": exc.stdout or "", "stderr": exc.stderr or ""},
            cause=exc,
        ) from exc
    if result.returncode != 0:
        raise CheckError(
            code,
            f"command failed with exit code {result.returncode}: {' '.join(command)}",
            details={
                "command": command,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )
    return result


def _junit_cases(path: Path) -> list[tuple[str, str, str]]:
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError) as exc:
        raise CheckError(
            "test_report_invalid",
            f"pytest did not produce a readable JUnit report: {path}",
            cause=exc,
        ) from exc
    cases: list[tuple[str, str, str]] = []
    for case in root.iter("testcase"):
        nodeid = f"{case.attrib.get('classname', '')}::{case.attrib.get('name', '')}"
        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")
        if failure is not None:
            outcome = "failed"
            detail = " ".join(
                value
                for value in (
                    failure.attrib.get("message", ""),
                    failure.text or "",
                )
                if value
            )
        elif error is not None:
            outcome = "error"
            detail = " ".join(
                value
                for value in (error.attrib.get("message", ""), error.text or "")
                if value
            )
        elif skipped is not None:
            outcome = "skipped"
            detail = skipped.attrib.get("message", skipped.text or "")
        else:
            outcome = "passed"
            detail = ""
        cases.append((nodeid, outcome, detail))
    return cases


def _pytest_command(
    python: Path, nodes: list[str], report_path: Path
) -> list[str]:
    return [
        str(python),
        "-m",
        "pytest",
        "-q",
        "--disable-warnings",
        f"--junitxml={report_path}",
        *nodes,
    ]


def _clear_report(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise CheckError(
            "test_report_cleanup_failed",
            f"could not clear the previous JUnit report: {path}",
            cause=exc,
        ) from exc


def _run_case(
    runtime: _Runtime,
    project: Path,
    nodeid: str,
    *,
    timeout: float,
    report_dir: Path,
) -> _CaseResult:
    report_path = report_dir / f"{hashlib.sha256(nodeid.encode()).hexdigest()}.xml"
    _clear_report(report_path)
    command = _pytest_command(runtime.python, [nodeid], report_path)
    environment = _clean_environment(
        pythonpath=project / "src", virtualenv=runtime.root / "venv"
    )
    try:
        _run_command(
            command,
            cwd=project,
            environment=environment,
            timeout=timeout,
            code="test_runner_failed",
        )
    except CheckError as exc:
        if exc.code == "test_timeout":
            raise
        if not report_path.exists():
            raise CheckError(
                "test_report_missing",
                f"pytest did not produce a report for {nodeid}",
                details=exc.details,
                cause=exc,
            ) from exc
    cases = _junit_cases(report_path)
    if len(cases) != 1:
        raise CheckError(
            "test_selection_invalid",
            f"expected exactly one collected test for {nodeid}, got {len(cases)}",
            details={"cases": cases},
        )
    _reported_nodeid, outcome, details = cases[0]
    return _CaseResult(outcome, details)


def _run_suite(
    python: Path,
    project: Path,
    nodes: list[str],
    *,
    timeout: float,
    report_dir: Path,
    virtualenv: Path | None = None,
    pythonpath: Path | None = None,
) -> None:
    if not nodes:
        raise CheckError("tests_missing", "no tests were declared for the requested suite")
    report_path = report_dir / f"suite-{hashlib.sha256('|'.join(nodes).encode()).hexdigest()}.xml"
    _clear_report(report_path)
    command = _pytest_command(python, nodes, report_path)
    environment = _clean_environment(pythonpath=pythonpath, virtualenv=virtualenv)
    result = _run_command(
        command,
        cwd=project,
        environment=environment,
        timeout=timeout,
        code="suite_failed",
    )
    del result
    cases = _junit_cases(report_path)
    if not cases:
        raise CheckError("tests_missing", f"pytest collected no tests from {nodes}")
    invalid = [
        {"nodeid": nodeid, "outcome": outcome, "details": details}
        for nodeid, outcome, details in cases
        if outcome != "passed"
    ]
    if invalid:
        raise CheckError(
            "suite_outcome_invalid",
            f"suite contains unexpected outcomes: {nodes}",
            details={"cases": invalid},
        )


def _create_runtime(
    starter: Path,
    *,
    package: str,
    workspace: Path,
    timeout: float,
) -> _Runtime:
    venv_path = workspace / "venv"
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_path)
    except OSError as exc:
        raise CheckError("runtime_create_failed", "could not create an isolated virtual environment", cause=exc) from exc
    python = venv_path / "bin" / "python"
    if not python.is_file():
        python = venv_path / "Scripts" / "python.exe"
    if not python.is_file():
        raise CheckError("runtime_create_failed", f"isolated Python executable is missing: {venv_path}")
    environment = _clean_environment(virtualenv=venv_path)
    uv = shutil.which("uv")
    if uv is not None:
        installer = [uv, "pip", "install", "--python", str(python)]
        _run_command(
            [*installer, "--no-deps", str(starter)],
            cwd=workspace,
            environment=environment,
            timeout=timeout,
            code="install_failed",
        )
        _run_command(
            [*installer, "pytest"],
            cwd=workspace,
            environment=environment,
            timeout=timeout,
            code="pytest_install_failed",
        )
    else:
        _run_command(
            [str(python), "-m", "pip", "install", "--no-deps", str(starter)],
            cwd=workspace,
            environment=environment,
            timeout=timeout,
            code="install_failed",
        )
        _run_command(
            [str(python), "-m", "pip", "install", "pytest"],
            cwd=workspace,
            environment=environment,
            timeout=timeout,
            code="pytest_install_failed",
        )
    _run_command(
        [str(python), "-c", f"import {package}"],
        cwd=workspace,
        environment=environment,
        timeout=timeout,
        code="package_import_failed",
    )
    return _Runtime(workspace, python)


def _dependency_order(exercises: ExerciseMetadata) -> tuple[Exercise, ...]:
    by_id = {exercise.id: exercise for exercise in exercises.exercises}
    ordered: list[Exercise] = []
    visited: set[str] = set()

    def visit(exercise: Exercise) -> None:
        if exercise.id in visited:
            return
        for dependency in exercise.requires:
            visit(by_id[dependency])
        visited.add(exercise.id)
        ordered.append(exercise)

    for exercise in exercises.exercises:
        visit(exercise)
    return tuple(ordered)


def _target(exercise: Exercise) -> TransformTarget:
    return TransformTarget(
        exercise_id=exercise.id,
        symbol=exercise.source.symbol,
        strategy=exercise.starter.strategy,
        docstring=exercise.starter.docstring,
        body=exercise.starter.body,
    )


def _checkpoint_workspace(
    starter: Path,
    canonical: Path,
    destination: Path,
    exercises: tuple[Exercise, ...],
    restored: set[str],
) -> Path:
    shutil.copytree(starter, destination)
    by_source: dict[str, list[Exercise]] = {}
    for exercise in exercises:
        by_source.setdefault(exercise.source.file, []).append(exercise)
    for relative, source_exercises in by_source.items():
        if not any(exercise.id in restored for exercise in source_exercises):
            continue
        canonical_source = canonical.joinpath(*relative.split("/"))
        checkpoint_source = destination.joinpath(*relative.split("/"))
        checkpoint_source.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copy2(canonical_source, checkpoint_source)
        pending = [
            (relative, _target(exercise))
            for exercise in source_exercises
            if exercise.id not in restored
        ]
        if pending:
            transform_sources(destination, pending, write=True)
    return destination


def _assert_baseline(case: _CaseResult, baseline: BaselineTest, *, exercise_id: str) -> None:
    if case.outcome != "failed":
        raise CheckError(
            "baseline_outcome_invalid",
            f"baseline for {exercise_id} returned {case.outcome}, expected {baseline.expected}",
            details={"exercise_id": exercise_id, "outcome": case.outcome, "details": case.details},
        )
    if baseline.expected == "stub_error" and "NotImplementedError" not in case.details:
        raise CheckError(
            "baseline_reason_invalid",
            f"baseline for {exercise_id} did not fail with NotImplementedError",
            details={"exercise_id": exercise_id, "details": case.details},
        )
    if baseline.expected == "assertion_failure":
        assert baseline.assertion_message is not None
        if baseline.assertion_message not in case.details:
            raise CheckError(
                "baseline_reason_invalid",
                f"baseline for {exercise_id} did not contain its exact assertion message",
                details={"exercise_id": exercise_id, "details": case.details},
            )


def _public_contracts(exercises: tuple[Exercise, ...]) -> dict[str, tuple[Exercise, BaselineTest]]:
    contracts: dict[str, tuple[Exercise, BaselineTest]] = {}
    for exercise in exercises:
        seen_baselines: set[str] = set()
        duplicate_baselines: list[str] = []
        for baseline in exercise.tests.baseline:
            if baseline.nodeid in seen_baselines and baseline.nodeid not in duplicate_baselines:
                duplicate_baselines.append(baseline.nodeid)
            seen_baselines.add(baseline.nodeid)
        if duplicate_baselines:
            raise CheckError(
                "duplicate_baseline_test",
                f"exercise {exercise.id} declares duplicate baseline test(s): "
                + ", ".join(duplicate_baselines),
                details={"exercise_id": exercise.id, "nodeids": duplicate_baselines},
            )
        baselines = {baseline.nodeid: baseline for baseline in exercise.tests.baseline}
        if set(exercise.tests.public) != set(baselines):
            raise CheckError(
                "baseline_coverage_invalid",
                f"exercise {exercise.id} must declare exactly one baseline for every public test",
                details={"public": exercise.tests.public, "baseline": list(baselines)},
            )
        for nodeid in exercise.tests.public:
            if nodeid in contracts:
                raise CheckError(
                    "duplicate_public_test",
                    f"public test is assigned to more than one exercise: {nodeid}",
                )
            contracts[nodeid] = (exercise, baselines[nodeid])
    return contracts


def _copy_for_build(root: Path, destination: Path) -> None:
    ignored = shutil.ignore_patterns(
        "build", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__", ".git"
    )
    shutil.copytree(root, destination, ignore=ignored)


def _output_snapshot(root: Path, output_paths: tuple[str, ...]) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for relative_root in output_paths:
        output = root / relative_root
        if not output.is_dir():
            raise CheckError("output_missing", f"expected integrated output directory is missing: {relative_root}")
        for path in sorted(output.rglob("*")):
            if path.is_file():
                snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
    return snapshot


def _verify_reproducibility(
    root: Path, *, workspace: Path, output_paths: tuple[str, ...]
) -> bool:
    first = workspace / "repro-a"
    second = workspace / "repro-b"
    _copy_for_build(root, first)
    _copy_for_build(root, second)
    build_documentation(first)
    build_documentation(second)
    first_snapshot = _output_snapshot(first, output_paths)
    second_snapshot = _output_snapshot(second, output_paths)
    if first_snapshot != second_snapshot:
        differing = sorted(
            key
            for key in set(first_snapshot) | set(second_snapshot)
            if first_snapshot.get(key) != second_snapshot.get(key)
        )
        raise CheckError(
            "non_reproducible",
            "independent documentation builds produced different outputs",
            details={"differing_files": differing},
        )
    return True


def check_project(root: Path, *, timeout: float = 60.0) -> CheckReport:
    """Run canonical, isolated, baseline, checkpoint, and reproducibility checks."""

    root = root.expanduser().resolve()
    validation = validate_project(root)
    if not validation.ok:
        raise CheckError(
            "validation_failed",
            "project validation failed; integrated checks did not run",
            details={"diagnostics": [diagnostic.to_dict() for diagnostic in validation.diagnostics]},
        )
    config, metadata = _load_contracts(root)
    exercises = _dependency_order(metadata)
    contracts = _public_contracts(exercises)
    canonical_nodes = [
        relative
        for relative in ("tests/private", "tests/public", "tests/smoke")
        if (root / relative).is_dir()
    ]
    if not canonical_nodes:
        raise CheckError("canonical_tests_missing", "canonical project has no private, public, or smoke test suite")

    with tempfile.TemporaryDirectory(prefix="startergen-check-") as temporary:
        workspace = Path(temporary)
        report_dir = workspace / "reports"
        report_dir.mkdir()
        _run_suite(
            Path(sys.executable),
            root,
            canonical_nodes,
            timeout=timeout,
            report_dir=report_dir,
            pythonpath=root / "src",
        )
        build_result = build_project(root)
        documentation: DocumentationResult = build_documentation(
            root, build_result=build_result
        )
        artifact = documentation.readme.parent
        if build_result.output != artifact:
            raise CheckError(
                "artifact_mismatch",
                "documentation and build commands selected different starter artifacts",
            )
        runtime = _create_runtime(
            artifact,
            package=config.project.import_package,
            workspace=workspace / "runtime",
            timeout=timeout,
        )
        report_dir = workspace / "runtime-reports"
        report_dir.mkdir()
        initial = workspace / "checkpoint-0"
        _checkpoint_workspace(artifact, root, initial, exercises, set())
        _run_suite(
            runtime.python,
            initial,
            ["tests/smoke"],
            timeout=timeout,
            report_dir=report_dir,
            virtualenv=runtime.root / "venv",
            pythonpath=initial / "src",
        )
        baseline: dict[str, str] = {}
        for nodeid, (exercise, expected) in contracts.items():
            case = _run_case(runtime, initial, nodeid, timeout=timeout, report_dir=report_dir)
            _assert_baseline(case, expected, exercise_id=exercise.id)
            baseline[exercise.id] = expected.expected

        checkpoints: list[dict[str, Any]] = []
        restored: set[str] = set()
        for exercise in exercises:
            restored.add(exercise.id)
            checkpoint = workspace / f"checkpoint-{len(restored)}"
            _checkpoint_workspace(artifact, root, checkpoint, exercises, restored)
            _run_suite(
                runtime.python,
                checkpoint,
                ["tests/smoke"],
                timeout=timeout,
                report_dir=report_dir,
                virtualenv=runtime.root / "venv",
                pythonpath=checkpoint / "src",
            )
            for nodeid, (owner, expected) in contracts.items():
                case = _run_case(runtime, checkpoint, nodeid, timeout=timeout, report_dir=report_dir)
                if owner.id in restored:
                    if case.outcome != "passed":
                        raise CheckError(
                            "checkpoint_outcome_invalid",
                            f"restored exercise {owner.id} did not pass at its dependency checkpoint",
                            details={"nodeid": nodeid, "outcome": case.outcome, "details": case.details},
                        )
                else:
                    _assert_baseline(case, expected, exercise_id=owner.id)
            checkpoints.append({"restored": [item.id for item in exercises if item.id in restored], "public": "expected", "smoke": "passed"})

        completed = workspace / "completed"
        _checkpoint_workspace(artifact, root, completed, exercises, {exercise.id for exercise in exercises})
        _run_suite(
            runtime.python,
            completed,
            ["tests/public"],
            timeout=timeout,
            report_dir=report_dir,
            virtualenv=runtime.root / "venv",
            pythonpath=completed / "src",
        )
        reproducible = _verify_reproducibility(
            root,
            workspace=workspace / "repro",
            output_paths=(
                config.starter.output,
                config.documentation.generated_source,
                config.documentation.site_output,
            ),
        )
    return CheckReport(
        project=root,
        artifact=artifact,
        dependency_order=tuple(exercise.id for exercise in exercises),
        canonical_tests=tuple(canonical_nodes),
        baseline=baseline,
        checkpoints=tuple(checkpoints),
        completed_public="passed",
        reproducible=reproducible,
        installed=True,
    )


verify_project = check_project
