"""Filesystem policy for version-1 authoring inputs and generated outputs."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from pathlib import Path, PureWindowsPath

from startergen.diagnostics import Diagnostic

_PROHIBITED_COMPONENTS = {".git", ".venv", "__pycache__"}


def is_safe_relative_path(value: str) -> bool:
    """Return whether *value* is an unambiguous project-relative path."""

    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if value.startswith(("/", "\\")) or Path(value).is_absolute():
        return False
    if PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
        return False
    if "\\" in value:
        return False
    parts = value.split("/")
    return not any(part in {"", ".", ".."} for part in parts)


def project_path(root: Path, value: str) -> Path | None:
    if not is_safe_relative_path(value):
        return None
    return root.joinpath(*value.split("/"))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _overlap(first: Path, second: Path) -> bool:
    return _is_within(first, second) or _is_within(second, first)


def _path_key(root: Path, path: Path) -> str:
    return os.fspath(path.relative_to(root)).replace(os.sep, "/").casefold()


def _existing_components_are_safe(root: Path, path: Path) -> tuple[str, Path] | None:
    """Return ``(kind, offending_path)`` for an unsafe existing component."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return ("outside_root", path)
    current = root
    for component in relative.parts:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            return ("symlink", current)
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            return ("special_file", current)
    return None


def _walk_for_unsafe_entries(path: Path) -> Iterable[tuple[str, Path]]:
    if path.is_file():
        yield from ()
        return
    for current, directories, files in os.walk(path, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            candidate = current_path / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                yield "symlink", candidate
                directories.remove(name)
            elif not stat.S_ISDIR(info.st_mode):
                yield "special_file", candidate
                directories.remove(name)
        for name in files:
            candidate = current_path / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                yield "symlink", candidate
            elif not stat.S_ISREG(info.st_mode):
                yield "special_file", candidate


def validate_input_path(
    root: Path,
    value: str,
    *,
    field: str,
    file: str,
    diagnostics: list[Diagnostic],
    exercise_id: str | None = None,
) -> Path | None:
    """Validate one required project-relative source path."""

    path = project_path(root, value)
    if path is None:
        diagnostics.append(
            Diagnostic(
                "unsafe_path",
                f"{field} must be a relative path without '.', '..', or platform-specific separators: {value!r}",
                file=file,
                exercise_id=exercise_id,
                suggestion="Use a literal path relative to the project root, such as src/lab_project.",
            )
        )
        return None
    prohibited = next(
        (
            part
            for part in path.relative_to(root).parts
            if part in _PROHIBITED_COMPONENTS or part == "build"
        ),
        None,
    )
    if prohibited is not None:
        diagnostics.append(
            Diagnostic(
                "prohibited_source_path",
                f"{field} includes reserved component {prohibited!r}: {value}",
                file=file,
                exercise_id=exercise_id,
                suggestion="Keep generated build outputs and repository metadata out of authoring inputs.",
            )
        )
    unsafe = _existing_components_are_safe(root, path)
    if unsafe is not None:
        kind, offending = unsafe
        code = "symlink_input" if kind == "symlink" else "special_input"
        diagnostics.append(
            Diagnostic(
                code,
                f"{field} reaches unsafe {kind.replace('_', ' ')} {offending.relative_to(root)}",
                file=file,
                exercise_id=exercise_id,
                suggestion="Replace symlinks and special files with ordinary files or directories.",
            )
        )
        return None
    if not path.exists():
        diagnostics.append(
            Diagnostic(
                "missing_input",
                f"{field} does not exist: {value}",
                file=file,
                exercise_id=exercise_id,
                suggestion="Create the referenced file or directory, or correct the relative path.",
            )
        )
        return None
    for kind, offending in _walk_for_unsafe_entries(path):
        diagnostics.append(
            Diagnostic(
                "symlink_input" if kind == "symlink" else "special_input",
                f"{field} contains unsafe {kind.replace('_', ' ')} {offending.relative_to(root)}",
                file=file,
                exercise_id=exercise_id,
                suggestion="Only ordinary files and directories may be included.",
            )
        )
    return path


def validate_output_path(
    root: Path,
    value: str,
    *,
    field: str,
    file: str,
    diagnostics: list[Diagnostic],
) -> Path | None:
    """Validate one output directory without creating or deleting it."""

    path = project_path(root, value)
    build_root = root / "build"
    if path is None:
        diagnostics.append(
            Diagnostic(
                "unsafe_destination",
                f"{field} must be a relative path: {value!r}",
                file=file,
                suggestion="Place generated outputs below build/ using a literal relative path.",
            )
        )
        return None
    if not _is_within(path, build_root) or path == build_root:
        diagnostics.append(
            Diagnostic(
                "destination_outside_build",
                f"{field} must be a child of build/: {value}",
                file=file,
                suggestion="Use a distinct directory such as build/starter.",
            )
        )
    unsafe = _existing_components_are_safe(root, path)
    if unsafe is not None:
        kind, offending = unsafe
        diagnostics.append(
            Diagnostic(
                "symlink_destination" if kind == "symlink" else "special_destination",
                f"{field} reaches unsafe {kind.replace('_', ' ')} {offending.relative_to(root)}",
                file=file,
                suggestion="Remove the unsafe destination component before building.",
            )
        )
    if path.exists() and not path.is_dir():
        diagnostics.append(
            Diagnostic(
                "invalid_destination",
                f"{field} already exists but is not a directory: {value}",
                file=file,
                suggestion="Choose an unused directory below build/.",
            )
        )
    return path


def validate_path_collisions(
    root: Path,
    paths: list[tuple[str, Path]],
    *,
    file: str,
    diagnostics: list[Diagnostic],
) -> None:
    """Reject case-insensitive duplicates and overlapping destinations."""

    by_key: dict[str, str] = {}
    for label, path in paths:
        key = _path_key(root, path)
        previous = by_key.get(key)
        if previous is not None:
            diagnostics.append(
                Diagnostic(
                    "destination_collision",
                    f"{label} collides with {previous} on a case-insensitive filesystem",
                    file=file,
                    suggestion="Give each generated destination a unique path and spelling.",
                )
            )
        else:
            by_key[key] = label
    for index, (first_label, first_path) in enumerate(paths):
        for second_label, second_path in paths[index + 1 :]:
            if _overlap(first_path, second_path) and _path_key(
                root, first_path
            ) != _path_key(root, second_path):
                diagnostics.append(
                    Diagnostic(
                        "destination_overlap",
                        f"{first_label} overlaps {second_label}",
                        file=file,
                        suggestion="Use sibling output directories under build/.",
                    )
                )


def validate_source_output_overlap(
    source_paths: Iterable[tuple[str, Path]],
    output_paths: Iterable[tuple[str, Path]],
    *,
    file: str,
    diagnostics: list[Diagnostic],
) -> None:
    for source_label, source_path in source_paths:
        for output_label, output_path in output_paths:
            if _overlap(source_path, output_path):
                diagnostics.append(
                    Diagnostic(
                        "source_output_overlap",
                        f"source {source_label} overlaps generated destination {output_label}",
                        file=file,
                        suggestion="Keep authoring inputs outside the build/ output tree.",
                    )
                )
