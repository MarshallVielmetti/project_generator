"""Project-level validation for the milestone-1 authoring contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from startergen.diagnostics import Diagnostic, ValidationReport
from startergen.paths import (
    validate_input_path,
    validate_output_path,
    validate_path_collisions,
    validate_source_output_overlap,
)
from startergen.schema import ExerciseMetadata, ProjectMetadata
from startergen.yaml_io import YamlInputError, load_yaml

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _format_location(location: tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in location) or "document"


def _parse_model(
    path: Path,
    model: type[_ModelT],
    *,
    root: Path,
    diagnostics: list[Diagnostic],
) -> _ModelT | None:
    display = _display_path(root, path)
    try:
        raw = load_yaml(path)
    except YamlInputError as exc:
        diagnostics.append(
            Diagnostic(
                exc.code,
                str(exc),
                file=display,
                line=exc.line,
                suggestion="Fix the YAML syntax and ensure every mapping key appears only once.",
            )
        )
        return None
    if not isinstance(raw, dict):
        diagnostics.append(
            Diagnostic(
                "schema_root_type",
                "the YAML document must contain a mapping at its root",
                file=display,
                suggestion="Start the document with named fields such as schema_version and project.",
            )
        )
        return None
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        for error in exc.errors():
            location = _format_location(error.get("loc", ()))
            error_type = error.get("type", "schema_error")
            if error_type == "missing":
                code = "missing_required_field"
                suggestion = f"Add the required field {location}."
            elif error_type == "extra_forbidden":
                code = "unknown_field"
                suggestion = f"Remove {location}; version 1 does not define it."
            else:
                code = "invalid_field"
                suggestion = (
                    f"Correct the value at {location} to match the version-1 schema."
                )
            diagnostics.append(
                Diagnostic(
                    code,
                    f"{location}: {error.get('msg', 'invalid value')}",
                    file=display,
                    suggestion=suggestion,
                )
            )
        return None


def _node_path(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _validate_exercise_references(
    root: Path,
    exercises: ExerciseMetadata,
    *,
    file: str,
    diagnostics: list[Diagnostic],
) -> list[tuple[str, Path]]:
    source_paths: list[tuple[str, Path]] = []
    by_id: dict[str, Any] = {}
    for exercise in exercises.exercises:
        if exercise.id in by_id:
            diagnostics.append(
                Diagnostic(
                    "duplicate_exercise_id",
                    f"exercise id {exercise.id!r} is declared more than once",
                    file=file,
                    exercise_id=exercise.id,
                    suggestion="Give each exercise a unique stable id.",
                )
            )
        by_id[exercise.id] = exercise
        source = validate_input_path(
            root,
            exercise.source.file,
            field=f"exercise {exercise.id} source.file",
            file=file,
            diagnostics=diagnostics,
            exercise_id=exercise.id,
        )
        if source is not None and not source.is_file():
            diagnostics.append(
                Diagnostic(
                    "source_not_file",
                    f"exercise source.file must name a file: {exercise.source.file}",
                    file=file,
                    exercise_id=exercise.id,
                    suggestion="Point source.file at the module containing the target symbol.",
                )
            )
        if source is not None:
            source_paths.append((f"exercise {exercise.id} source", source))
        # Dependencies are validated in a second pass below.
        for test in exercise.tests.public:
            test_path = _node_path(test)
            path = validate_input_path(
                root,
                test_path,
                field=f"exercise {exercise.id} public test",
                file=file,
                diagnostics=diagnostics,
                exercise_id=exercise.id,
            )
            if path is not None and not path.is_file():
                diagnostics.append(
                    Diagnostic(
                        "test_not_file",
                        f"public test nodeid must start with a file: {test}",
                        file=file,
                        exercise_id=exercise.id,
                        suggestion="Use a pytest node id such as tests/public/test_model.py::test_behavior.",
                    )
                )
        for baseline in exercise.tests.baseline:
            test_path = _node_path(baseline.nodeid)
            path = validate_input_path(
                root,
                test_path,
                field=f"exercise {exercise.id} baseline test",
                file=file,
                diagnostics=diagnostics,
                exercise_id=exercise.id,
            )
            if path is not None and not path.is_file():
                diagnostics.append(
                    Diagnostic(
                        "test_not_file",
                        f"baseline test nodeid must start with a file: {baseline.nodeid}",
                        file=file,
                        exercise_id=exercise.id,
                        suggestion="Use a pytest node id with an existing test file.",
                    )
                )
    for exercise in exercises.exercises:
        for dependency in exercise.requires:
            if dependency not in by_id:
                diagnostics.append(
                    Diagnostic(
                        "unknown_exercise_dependency",
                        f"exercise {exercise.id!r} requires unknown exercise {dependency!r}",
                        file=file,
                        exercise_id=exercise.id,
                        suggestion="Declare the dependency as another exercise id or remove it.",
                    )
                )
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(exercise_id: str) -> None:
        state[exercise_id] = 1
        stack.append(exercise_id)
        for dependency in by_id[exercise_id].requires:
            if dependency not in by_id:
                continue
            if state.get(dependency) == 1:
                cycle_start = stack.index(dependency)
                cycle = " -> ".join(stack[cycle_start:] + [dependency])
                diagnostics.append(
                    Diagnostic(
                        "exercise_dependency_cycle",
                        f"exercise dependency graph contains a cycle: {cycle}",
                        file=file,
                        exercise_id=exercise_id,
                        suggestion="Order exercises as a directed acyclic prerequisite graph.",
                    )
                )
            elif state.get(dependency, 0) == 0:
                visit(dependency)
        stack.pop()
        state[exercise_id] = 2

    for exercise in exercises.exercises:
        if state.get(exercise.id, 0) == 0:
            visit(exercise.id)
    return source_paths


def validate_project(root: Path) -> ValidationReport:
    """Validate a canonical project without changing its files."""

    root = root.expanduser()
    diagnostics: list[Diagnostic] = []
    if not root.exists():
        return ValidationReport(
            [Diagnostic("missing_root", f"project root does not exist: {root}")]
        )
    if not root.is_dir():
        return ValidationReport(
            [
                Diagnostic(
                    "root_not_directory", f"project root is not a directory: {root}"
                )
            ]
        )

    config_path = root / "teaching" / "config.yml"
    exercises_path = root / "teaching" / "exercises.yml"
    config_contract = (
        validate_input_path(
            root,
            "teaching/config.yml",
            field="project configuration",
            file="teaching/config.yml",
            diagnostics=diagnostics,
        )
        if config_path.exists()
        else None
    )
    exercises_contract = (
        validate_input_path(
            root,
            "teaching/exercises.yml",
            field="exercise metadata",
            file="teaching/exercises.yml",
            diagnostics=diagnostics,
        )
        if exercises_path.exists()
        else None
    )
    config = (
        _parse_model(config_path, ProjectMetadata, root=root, diagnostics=diagnostics)
        if config_contract is not None and config_contract.is_file()
        else None
    )
    if config is None and not config_path.exists():
        diagnostics.append(
            Diagnostic(
                "missing_config",
                "teaching/config.yml does not exist",
                file="teaching/config.yml",
                suggestion="Add the version-1 project, starter, documentation, and publication contract.",
            )
        )
    exercises = (
        _parse_model(
            exercises_path, ExerciseMetadata, root=root, diagnostics=diagnostics
        )
        if exercises_contract is not None and exercises_contract.is_file()
        else None
    )
    if exercises is None and not exercises_path.exists():
        diagnostics.append(
            Diagnostic(
                "missing_exercises",
                "teaching/exercises.yml does not exist",
                file="teaching/exercises.yml",
                suggestion="Add at least one version-1 exercise definition.",
            )
        )
    if config is None:
        return ValidationReport(diagnostics)

    config_file = "teaching/config.yml"
    input_paths: list[tuple[str, Path]] = []
    for index, value in enumerate(config.starter.include):
        path = validate_input_path(
            root,
            value,
            field=f"starter.include[{index}]",
            file=config_file,
            diagnostics=diagnostics,
        )
        if path is not None:
            input_paths.append((f"starter.include[{index}]", path))
    for index, value in enumerate(config.starter.exclude):
        path = validate_input_path(
            root,
            value,
            field=f"starter.exclude[{index}]",
            file=config_file,
            diagnostics=diagnostics,
        )
        if path is not None:
            input_paths.append((f"starter.exclude[{index}]", path))
    for field, value in (
        ("documentation.source", config.documentation.source),
        ("documentation.assets", config.documentation.assets),
        ("documentation.background", config.documentation.background),
        ("documentation.readme_template", config.documentation.readme_template),
    ):
        path = validate_input_path(
            root, value, field=field, file=config_file, diagnostics=diagnostics
        )
        if path is not None:
            input_paths.append((field, path))

    output_paths: list[tuple[str, Path]] = []
    for field, value in (
        ("starter.output", config.starter.output),
        ("documentation.generated_source", config.documentation.generated_source),
        ("documentation.site_output", config.documentation.site_output),
    ):
        path = validate_output_path(
            root, value, field=field, file=config_file, diagnostics=diagnostics
        )
        if path is not None:
            output_paths.append((field, path))
    validate_path_collisions(
        root, output_paths, file=config_file, diagnostics=diagnostics
    )
    validate_source_output_overlap(
        input_paths, output_paths, file=config_file, diagnostics=diagnostics
    )

    if exercises is not None:
        source_paths = _validate_exercise_references(
            root,
            exercises,
            file="teaching/exercises.yml",
            diagnostics=diagnostics,
        )
        validate_source_output_overlap(
            source_paths, output_paths, file=config_file, diagnostics=diagnostics
        )

    seen_inputs: dict[str, str] = {}
    for label, path in input_paths:
        key = path.relative_to(root).as_posix().casefold()
        if key in seen_inputs and seen_inputs[key] != label:
            diagnostics.append(
                Diagnostic(
                    "duplicate_source",
                    f"{label} repeats {seen_inputs[key]}: {path.relative_to(root)}",
                    file=config_file,
                    suggestion="List each source path once.",
                )
            )
        seen_inputs[key] = label
    return ValidationReport(diagnostics)
