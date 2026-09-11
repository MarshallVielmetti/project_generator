"""Deterministic, recoverable assembly of student starter artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Any, Self

from pydantic import ValidationError

from startergen import __version__
from startergen.diagnostics import Diagnostic
from startergen.paths import is_safe_relative_path, project_path
from startergen.schema import ExerciseMetadata, ProjectMetadata
from startergen.transform import (
    ResolvedTarget,
    TransformError,
    TransformTarget,
    transform_sources,
)
from startergen.validate import validate_project
from startergen.yaml_io import YamlInputError, load_yaml_bytes

MANIFEST_SCHEMA_VERSION = 1

# These names are deliberately conservative. The allowlist remains the source
# of truth, while these rules prevent a broad directory allowlist from leaking
# repository metadata, caches, tooling, or instructor-only material.
DENIED_PATH_COMPONENTS = frozenset(
    {
        ".git",
        ".github",
        ".startergen",
        ".venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        "generation",
        "instructor",
        "private",
        "teaching",
        "tools",
    }
)
DENIED_FILE_NAMES = frozenset(
    {
        ".ds_store",
        ".env",
        "credentials",
        "credentials.json",
        "id_rsa",
        "secret",
        "secrets",
    }
)
DENIED_SUFFIXES = frozenset(
    {".key", ".pem", ".p12", ".pfx", ".pyc", ".pyo", ".sqlite", ".sqlite3"}
)


class AssemblyError(RuntimeError):
    """Actionable error raised when an artifact cannot be assembled."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: Iterable[Diagnostic] = (),
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = tuple(diagnostics)
        self.cause = cause


@dataclass(frozen=True, slots=True)
class ArtifactFile:
    """One file entry in the generated artifact manifest."""

    path: str
    mode: int
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "mode": self.mode,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class BuildResult:
    """Summary of a successfully promoted starter artifact."""

    output: Path
    manifest: Path
    files: tuple[ArtifactFile, ...]
    transformed_files: tuple[str, ...]
    targets: tuple[ResolvedTarget, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": self.output.as_posix(),
            "manifest": self.manifest.as_posix(),
            "files": [file.to_dict() for file in self.files],
            "transformed_files": list(self.transformed_files),
            "targets": [
                {
                    "exercise_id": target.target.exercise_id,
                    "symbol": target.target.symbol,
                    "qualified_name": target.qualified_name,
                    "source_range": {
                        "start_line": target.source_range.start_line,
                        "start_column": target.source_range.start_column,
                        "end_line": target.source_range.end_line,
                        "end_column": target.source_range.end_column,
                    },
                }
                for target in self.targets
            ],
        }


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    mode: int
    size: int
    sha256: str


def deny_reason(relative_path: str) -> str | None:
    """Return the built-in deny rule matching a project-relative path."""

    parts = PurePosixPath(relative_path).parts
    folded_parts = tuple(part.casefold() for part in parts)
    for part in folded_parts:
        if part in DENIED_PATH_COMPONENTS:
            return f"reserved path component {part!r}"
    filename = folded_parts[-1] if folded_parts else ""
    if filename in DENIED_FILE_NAMES:
        return f"reserved file name {filename!r}"
    if filename.startswith((".env.", "id_rsa")):
        return f"credential-like file name {filename!r}"
    if any(filename.endswith(suffix) for suffix in DENIED_SUFFIXES):
        return f"reserved file suffix in {filename!r}"
    if filename.endswith((".log", ".token")):
        return f"credential or generated log file {filename!r}"
    return None


def _path_key(relative_path: str) -> str:
    return relative_path.replace("\\", "/").casefold()


def _excluded(relative_path: str, exclusions: tuple[str, ...]) -> bool:
    key = _path_key(relative_path)
    return any(
        key == excluded or key.startswith(f"{excluded}/") for excluded in exclusions
    )


def _assert_safe_existing_path(root: Path, path: Path) -> None:
    """Reject symlink and special-file components before reading or writing."""

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise AssemblyError(
            "outside_root", f"path escapes the project root: {path}"
        ) from exc
    current = root
    for index, component in enumerate(relative.parts):
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise AssemblyError(
                "symlink_input",
                f"path contains a symlink component: {current.relative_to(root)}",
            )
        is_final = index == len(relative.parts) - 1
        if not is_final and not stat.S_ISDIR(info.st_mode):
            raise AssemblyError(
                "special_input",
                f"path component is not a directory: {current.relative_to(root)}",
            )
        if is_final and not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise AssemblyError(
                "special_input",
                f"path is not an ordinary file or directory: {current.relative_to(root)}",
            )


def _iter_input_files(
    root: Path,
    include_path: Path,
    *,
    exclusions: tuple[str, ...],
) -> Iterator[tuple[str, Path]]:
    """Yield ordinary files below one allowlisted path in stable order."""

    include_relative = include_path.relative_to(root).as_posix()
    if deny_reason(include_relative) is not None:
        raise AssemblyError(
            "denied_input",
            f"allowlisted input is prohibited: {include_relative}",
        )
    if include_path.is_file():
        if not _excluded(include_relative, exclusions):
            yield include_relative, include_path
        return

    pending: list[tuple[str, Path]] = [(include_relative, include_path)]
    while pending:
        relative, current = pending.pop()
        try:
            entries = sorted(
                os.scandir(current),
                key=lambda entry: entry.name.casefold(),
                reverse=True,
            )
        except OSError as exc:
            raise AssemblyError(
                "enumeration_failed",
                f"could not enumerate allowlisted directory {relative}: {exc}",
                cause=exc,
            ) from exc
        for entry in entries:
            child_relative = f"{relative}/{entry.name}"
            if _excluded(child_relative, exclusions) or deny_reason(child_relative):
                continue
            child = current / entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise AssemblyError(
                    "symlink_input",
                    f"allowlisted input contains a symlink: {child_relative}",
                )
            if stat.S_ISDIR(info.st_mode):
                pending.append((child_relative, child))
            elif stat.S_ISREG(info.st_mode):
                yield child_relative, child
            else:
                raise AssemblyError(
                    "special_input",
                    f"allowlisted input contains a special file: {child_relative}",
                )


def enumerate_allowlist(
    root: Path,
    include: Iterable[str],
    exclude: Iterable[str] = (),
) -> dict[str, Path]:
    """Enumerate the explicit publication allowlist.

    The returned keys are project-relative POSIX paths and the values are
    ordinary source files. Denied descendants are omitted; duplicate or
    case-colliding output paths fail rather than silently overwriting data.
    """

    root = root.expanduser().resolve()
    if not root.is_dir():
        raise AssemblyError(
            "root_not_directory", f"project root is not a directory: {root}"
        )
    exclusions: list[str] = []
    for value in exclude:
        if not is_safe_relative_path(value):
            raise AssemblyError("unsafe_path", f"exclude path is unsafe: {value!r}")
        exclusions.append(_path_key(value))
    results: dict[str, Path] = {}
    occupied: dict[str, tuple[str, bool]] = {}
    for value in include:
        if not is_safe_relative_path(value):
            raise AssemblyError("unsafe_path", f"include path is unsafe: {value!r}")
        path = project_path(root, value)
        assert path is not None
        _assert_safe_existing_path(root, path)
        if not path.exists():
            raise AssemblyError(
                "missing_input", f"allowlisted input does not exist: {value}"
            )
        for relative, source in _iter_input_files(
            root, path, exclusions=tuple(exclusions)
        ):
            parts = relative.split("/")
            for index in range(1, len(parts) + 1):
                prefix = "/".join(parts[:index])
                key = _path_key(prefix)
                is_directory = index < len(parts)
                previous = occupied.get(key)
                if previous is not None:
                    previous_path, previous_is_directory = previous
                    if previous_path != prefix or previous_is_directory != is_directory:
                        raise AssemblyError(
                            "artifact_collision",
                            f"allowlisted paths {previous_path!r} and {relative!r} collide in the publication tree",
                        )
                    if not is_directory:
                        raise AssemblyError(
                            "artifact_collision",
                            f"allowlisted paths repeat the output file {relative!r}",
                        )
                else:
                    occupied[key] = (prefix, is_directory)
            results[relative] = source
    return dict(sorted(results.items()))


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _artifact_files(root: Path) -> tuple[ArtifactFile, ...]:
    entries: list[ArtifactFile] = []
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        relative = path.relative_to(root).as_posix()
        if relative == ".startergen/manifest.json":
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            if stat.S_ISLNK(info.st_mode):
                raise AssemblyError(
                    "symlink_output", f"staged output contains a symlink: {relative}"
                )
            continue
        digest, size = _sha256(path)
        entries.append(
            ArtifactFile(
                path=relative,
                mode=stat.S_IMODE(info.st_mode),
                size=size,
                sha256=digest,
            )
        )
    return tuple(entries)


def _digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _input_provenance(
    contract_records: Iterable[dict[str, Any]],
    snapshots: dict[str, _SourceSnapshot],
) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for record in contract_records:
        records[record["path"]] = dict(record)
    for relative, snapshot in snapshots.items():
        records[relative] = {
            "path": relative,
            "sha256": snapshot.sha256,
            "size": snapshot.size,
        }
    return [records[key] for key in sorted(records)]


def _write_manifest(
    staging: Path,
    *,
    config: ProjectMetadata,
    generator_version: str,
    provenance: list[dict[str, Any]],
) -> tuple[Path, tuple[ArtifactFile, ...]]:
    files = _artifact_files(staging)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generator_version": generator_version,
        "project": {
            "name": config.project.name,
            "import_package": config.project.import_package,
        },
        "provenance": {"inputs": provenance},
        "files": [entry.to_dict() for entry in files],
    }
    manifest_path = staging / ".startergen" / "manifest.json"
    manifest_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(manifest_path, 0o644)
    return manifest_path, files


try:  # pragma: no cover - the fallback is exercised only on Windows.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class _BuildLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (
            stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
        ):
            raise AssemblyError(
                "unsafe_lock",
                f"build lock is not an ordinary file: {self.path}",
            )
        try:
            self.handle = self.path.open("a+b")
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            else:  # pragma: no cover
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
        except OSError as exc:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            raise AssemblyError(
                "lock_failed", f"could not acquire build lock: {self.path}", cause=exc
            ) from exc
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self.handle is not None
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


@contextmanager
def project_build_lock(root: Path) -> Iterator[None]:
    """Hold the lock shared by starter and documentation builds."""

    root = root.expanduser().resolve()
    build_root = root / "build"
    _assert_safe_existing_path(root, build_root)
    build_root.mkdir(mode=0o755, parents=True, exist_ok=True)
    with _BuildLock(build_root / ".startergen.lock"):
        yield


def _resolve_output(root: Path, value: str) -> tuple[Path, Path]:
    output = project_path(root, value)
    build_root = root / "build"
    if output is None:
        raise AssemblyError(
            "unsafe_destination", f"starter output path is unsafe: {value!r}"
        )
    try:
        relative_to_build = output.relative_to(build_root)
    except ValueError as exc:
        raise AssemblyError(
            "destination_outside_build", f"starter output must be below build/: {value}"
        ) from exc
    if not relative_to_build.parts:
        raise AssemblyError(
            "invalid_destination", "starter output cannot be build/ itself"
        )
    if relative_to_build.parts[0].casefold() == ".startergen.lock":
        raise AssemblyError(
            "reserved_destination",
            "starter output cannot use the internal build lock path",
        )
    _assert_safe_existing_path(root, build_root)
    _assert_safe_existing_path(root, output)
    if output.exists() and not output.is_dir():
        raise AssemblyError(
            "invalid_destination", f"starter output is not a directory: {value}"
        )
    build_root.mkdir(mode=0o755, parents=True, exist_ok=True)
    output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    return build_root, output


def _copy_files(files: dict[str, Path], staging: Path) -> dict[str, _SourceSnapshot]:
    snapshots: dict[str, _SourceSnapshot] = {}
    for relative, source in files.items():
        destination = staging.joinpath(*relative.split("/"))
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        source_info = source.lstat()
        if not stat.S_ISREG(source_info.st_mode):
            raise AssemblyError(
                "special_input", f"allowlisted source is not a file: {relative}"
            )
        digest = hashlib.sha256()
        size = 0
        with (
            source.open("rb") as source_handle,
            destination.open("wb") as output_handle,
        ):
            for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                output_handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        source_mode = stat.S_IMODE(source_info.st_mode)
        os.chmod(destination, source_mode | stat.S_IWUSR)
        snapshots[relative] = _SourceSnapshot(
            mode=source_mode,
            size=size,
            sha256=digest.hexdigest(),
        )
    return snapshots


def _restore_modes(staging: Path, snapshots: dict[str, _SourceSnapshot]) -> None:
    for relative, snapshot in snapshots.items():
        os.chmod(staging.joinpath(*relative.split("/")), snapshot.mode)


def _source_output_path(files: dict[str, Path], relative: str) -> str | None:
    if relative in files:
        return relative
    matches = [
        output_relative
        for output_relative in files
        if len(output_relative.split("/")) == len(relative.split("/"))
        and all(
            actual.casefold() == declared.casefold()
            for actual, declared in zip(output_relative.split("/"), relative.split("/"))
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _promote(staging: Path, output: Path, build_root: Path) -> None:
    backup = build_root / f".startergen-backup-{uuid.uuid4().hex}"
    had_previous = output.exists()
    if had_previous:
        output.rename(backup)
    try:
        staging.rename(output)
    except BaseException:
        if had_previous and backup.exists() and not output.exists():
            backup.rename(output)
        raise
    if backup.exists():
        try:
            shutil.rmtree(backup)
        except OSError:
            # Promotion has committed the new output. Retaining the backup is
            # safer than reporting failure after the visible state changed.
            pass


def _load_contracts(
    root: Path,
) -> tuple[ProjectMetadata, ExerciseMetadata, list[dict[str, Any]]]:
    try:
        records: list[dict[str, Any]] = []
        models: list[Any] = []
        for path, model in (
            (root / "teaching" / "config.yml", ProjectMetadata),
            (root / "teaching" / "exercises.yml", ExerciseMetadata),
        ):
            raw = path.read_bytes()
            models.append(model.model_validate(load_yaml_bytes(raw)))
            records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": _digest_bytes(raw),
                    "size": len(raw),
                }
            )
    except (YamlInputError, ValidationError) as exc:
        raise AssemblyError(
            "contract_load_failed",
            f"validated contracts could not be loaded: {exc}",
            cause=exc,
        ) from exc
    except OSError as exc:
        raise AssemblyError(
            "contract_read_failed", "validated contracts could not be read", cause=exc
        ) from exc
    return models[0], models[1], records


def _normalized_transform_error(
    error: TransformError,
    target_pairs: list[tuple[str, TransformTarget]],
) -> AssemblyError:
    target = error.target
    relative = target_pairs[0][0] if target_pairs else "source"
    if target is not None:
        for candidate_relative, candidate_target in target_pairs:
            if candidate_target == target:
                relative = candidate_relative
                break
        context = f" for exercise {target.exercise_id!r} target {target.symbol!r}"
    else:
        context = ""
    return AssemblyError(
        error.code,
        f"could not transform {relative}{context}: {error.code}",
        cause=error,
    )


def build_project(
    root: Path,
    *,
    generator_version: str = __version__,
) -> BuildResult:
    """Build and atomically promote a validated starter artifact."""

    try:
        return _build_project(root, generator_version=generator_version)
    except AssemblyError:
        raise
    except OSError as exc:
        raise AssemblyError(
            "filesystem_error",
            "starter assembly could not access the filesystem",
            cause=exc,
        ) from exc


def build_project_locked(
    root: Path,
    *,
    generator_version: str = __version__,
) -> BuildResult:
    """Build a starter while the caller owns :func:`project_build_lock`."""

    try:
        return _build_project(
            root, generator_version=generator_version, lock_held=True
        )
    except AssemblyError:
        raise
    except OSError as exc:
        raise AssemblyError(
            "filesystem_error",
            "starter assembly could not access the filesystem",
            cause=exc,
        ) from exc


def _build_project(
    root: Path,
    *,
    generator_version: str,
    lock_held: bool = False,
) -> BuildResult:

    root = root.expanduser().resolve()
    report = validate_project(root)
    if not report.ok:
        raise AssemblyError(
            "validation_failed",
            "project validation failed; no artifact was changed",
            diagnostics=report.diagnostics,
        )
    config, exercises, contract_provenance = _load_contracts(root)
    build_root, output = _resolve_output(root, config.starter.output)
    transformed_files: list[str] = []
    resolved_targets: list[ResolvedTarget] = []
    lock = nullcontext() if lock_held else _BuildLock(build_root / ".startergen.lock")
    with lock:
        staging = Path(tempfile.mkdtemp(prefix=".startergen-stage-", dir=build_root))
        try:
            files = enumerate_allowlist(
                root, config.starter.include, config.starter.exclude
            )
            snapshots = _copy_files(files, staging)
            provenance = _input_provenance(contract_provenance, snapshots)

            target_pairs: list[tuple[str, TransformTarget]] = []
            for exercise in exercises.exercises:
                output_relative = _source_output_path(files, exercise.source.file)
                if output_relative is None:
                    raise AssemblyError(
                        "source_not_allowlisted",
                        f"exercise source is not in the starter allowlist: {exercise.source.file}",
                    )
                target_pairs.append(
                    (
                        output_relative,
                        TransformTarget(
                            exercise_id=exercise.id,
                            symbol=exercise.source.symbol,
                            strategy=exercise.starter.strategy,
                            docstring=exercise.starter.docstring,
                            body=exercise.starter.body,
                        ),
                    )
                )
            if target_pairs:
                try:
                    transformed = transform_sources(staging, target_pairs, write=True)
                except TransformError as exc:
                    raise _normalized_transform_error(exc, target_pairs) from exc
                transformed_files.extend(
                    path.relative_to(staging).as_posix()
                    for path, result in transformed.items()
                    if result.changed
                )
                for result in transformed.values():
                    resolved_targets.extend(result.targets)
            _restore_modes(staging, snapshots)
            _manifest_path, artifact_files = _write_manifest(
                staging,
                config=config,
                generator_version=generator_version,
                provenance=provenance,
            )
            _promote(staging, output, build_root)
            staging = Path()
        except AssemblyError:
            raise
        except BaseException as exc:
            raise AssemblyError(
                "build_failed", f"starter assembly failed: {exc}", cause=exc
            ) from exc
        finally:
            if staging != Path() and staging.exists():
                shutil.rmtree(staging)

    return BuildResult(
        output=output,
        manifest=output / ".startergen" / "manifest.json",
        files=artifact_files,
        transformed_files=tuple(sorted(transformed_files)),
        targets=tuple(resolved_targets),
    )


assemble_project = build_project
