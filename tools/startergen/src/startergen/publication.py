"""Validated, retryable publication of starter artifacts and documentation.

Publication is deliberately a separate stage from artifact generation.  The
canonical checkout is validated first, then the immutable artifact and site
identities are recorded in a transaction file before either destination is
changed.  The starter repository and documentation destination have separate
state machines so a failed second stage can be retried without rebuilding or
republishing the first stage.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from startergen import __version__
from startergen.check import CheckReport, check_project
from startergen.schema import ProjectMetadata
from startergen.yaml_io import load_yaml_bytes

TRANSACTION_SCHEMA_VERSION = 1
RELEASE_METADATA_PATH = ".startergen/release.json"
_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class PublicationError(RuntimeError):
    """Actionable error raised when a release cannot be safely published."""

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
class ReleasePlan:
    """The immutable inputs and identities for one release attempt."""

    project: Path
    starter_artifact: Path
    site_artifact: Path
    starter_repository: str
    branch: str
    release_id: str
    docs_base_url: str
    source_commit: str
    artifact_id: str
    documentation_id: str
    artifact_files: int
    documentation_files: int
    generator_version: str

    @property
    def documentation_url(self) -> str:
        return f"{self.docs_base_url.rstrip('/')}/{self.release_id}/"

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project.as_posix(),
            "starter_artifact": self.starter_artifact.as_posix(),
            "site_artifact": self.site_artifact.as_posix(),
            "starter_repository": self.starter_repository,
            "branch": self.branch,
            "release_id": self.release_id,
            "docs_base_url": self.docs_base_url,
            "documentation_url": self.documentation_url,
            "source_commit": self.source_commit,
            "artifact_id": self.artifact_id,
            "documentation_id": self.documentation_id,
            "artifact_files": self.artifact_files,
            "documentation_files": self.documentation_files,
            "generator_version": self.generator_version,
        }


@dataclass(frozen=True, slots=True)
class StageResult:
    """Outcome of one independently retryable publication stage."""

    status: str
    commit: str | None = None
    release_path: str | None = None
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "commit": self.commit,
            "release_path": self.release_path,
            "verified": self.verified,
        }


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    """Summary returned after a dry run or completed release."""

    plan: ReleasePlan
    transaction: Path
    starter: StageResult
    documentation: StageResult
    template_marked: bool
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "released": not self.dry_run,
            "dry_run": self.dry_run,
            "plan": self.plan.to_dict(),
            "transaction": self.transaction.as_posix(),
            "starter": self.starter.to_dict(),
            "documentation": self.documentation.to_dict(),
            "template_marked": self.template_marked,
        }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _release_rank(value: str) -> tuple[tuple[int, object], ...]:
    """Return a natural, deterministic ordering for release identifiers."""

    pieces = re.findall(r"\d+|[A-Za-z]+", value.casefold())
    return tuple((0, int(piece)) if piece.isdigit() else (1, piece) for piece in pieces)


def _ensure_release_id(value: str) -> str:
    if not _RELEASE_ID.fullmatch(value):
        raise PublicationError(
            "invalid_release_id",
            f"release identifier is not URL-safe: {value!r}",
        )
    return value


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment.setdefault("GIT_AUTHOR_NAME", "startergen release")
    environment.setdefault(
        "GIT_AUTHOR_EMAIL", "startergen-release@users.noreply.github.com"
    )
    environment.setdefault("GIT_COMMITTER_NAME", environment["GIT_AUTHOR_NAME"])
    environment.setdefault("GIT_COMMITTER_EMAIL", environment["GIT_AUTHOR_EMAIL"])
    return environment


def _git(
    arguments: list[str],
    *,
    cwd: Path,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            env=_git_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise PublicationError(
            "git_unavailable",
            "publication requires the git executable",
            details={"command": ["git", *arguments]},
            cause=exc,
        ) from exc
    if result.returncode != 0 and not allow_failure:
        raise PublicationError(
            "git_command_failed",
            f"git command failed: git {' '.join(arguments)}",
            details={
                "command": ["git", *arguments],
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )
    return result


def _git_output(arguments: list[str], *, cwd: Path) -> str:
    return _git(arguments, cwd=cwd).stdout.strip()


def _canonical_identity(records: Iterable[dict[str, Any]]) -> str:
    normalized = [dict(record) for record in records]
    normalized.sort(key=lambda record: str(record.get("path", "")))
    return _sha256_bytes(_canonical_json(normalized))


def _artifact_identity(artifact: Path) -> tuple[str, int]:
    manifest_path = artifact / ".startergen" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(
            "manifest_invalid",
            f"validated starter artifact has no readable manifest: {manifest_path}",
            cause=exc,
        ) from exc
    records = manifest.get("files")
    if manifest.get("schema_version") != 1 or not isinstance(records, list):
        raise PublicationError(
            "manifest_invalid",
            f"starter manifest has an unsupported shape: {manifest_path}",
        )
    if any(not isinstance(record, dict) or "path" not in record for record in records):
        raise PublicationError(
            "manifest_invalid",
            f"starter manifest contains an invalid file record: {manifest_path}",
        )
    expected = {record["path"]: record for record in records}
    actual: list[dict[str, Any]] = []
    for path in sorted(
        artifact.rglob("*"),
        key=lambda candidate: candidate.relative_to(artifact).as_posix(),
    ):
        if path.is_symlink():
            raise PublicationError(
                "unsafe_output", f"starter artifact contains a symlink: {path}"
            )
        if not path.is_file():
            continue
        relative = path.relative_to(artifact).as_posix()
        if relative == ".startergen/manifest.json":
            continue
        raw = path.read_bytes()
        actual.append(
            {
                "path": relative,
                "mode": path.stat().st_mode & 0o7777,
                "size": len(raw),
                "sha256": _sha256_bytes(raw),
            }
        )
    if {record["path"]: record for record in actual} != expected:
        raise PublicationError(
            "artifact_changed",
            "starter artifact bytes no longer match its generated manifest",
            details={"manifest": manifest_path.as_posix()},
        )
    return _canonical_identity(records), len(records)


def _directory_identity(root: Path) -> tuple[str, int]:
    if not root.is_dir():
        raise PublicationError(
            "output_missing", f"generated documentation directory is missing: {root}"
        )
    records: list[dict[str, Any]] = []
    for path in sorted(
        root.rglob("*"), key=lambda candidate: candidate.relative_to(root).as_posix()
    ):
        if path.is_symlink():
            raise PublicationError(
                "unsafe_output",
                f"generated documentation contains a symlink: {path}",
            )
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        records.append(
            {
                "path": relative,
                "size": len(raw),
                "sha256": _sha256_bytes(raw),
            }
        )
    return _canonical_identity(records), len(records)


def _source_commit(root: Path) -> str:
    status = _git(["status", "--porcelain=v1"], cwd=root).stdout.strip()
    if status:
        raise PublicationError(
            "canonical_checkout_dirty",
            "the canonical checkout has uncommitted changes; release an exact commit instead",
            details={"status": status},
        )
    result = _git(["rev-parse", "--verify", "HEAD"], cwd=root, allow_failure=True)
    if result.returncode != 0:
        raise PublicationError(
            "canonical_commit_missing",
            "the canonical project is not at a verifiable Git commit",
            details={"stderr": result.stderr},
        )
    return result.stdout.strip()


def _load_config(root: Path) -> ProjectMetadata:
    try:
        return ProjectMetadata.model_validate(
            load_yaml_bytes((root / "teaching" / "config.yml").read_bytes())
        )
    except Exception as exc:
        raise PublicationError(
            "contract_load_failed",
            "publication configuration could not be loaded",
            cause=exc,
        ) from exc


def build_release_plan(
    root: Path,
    *,
    check: CheckReport | None = None,
    generator_version: str = __version__,
) -> ReleasePlan:
    """Create a release plan from a completed integrated check."""

    root = root.expanduser().resolve()
    config = _load_config(root)
    publication = config.publication
    if publication is None:
        raise PublicationError(
            "publication_config_missing",
            "teaching/config.yml does not define a publication section",
        )
    release_id = _ensure_release_id(publication.release_id)
    if check is None:
        check = check_project(root)
    artifact = check.artifact.resolve()
    site = (root / config.documentation.site_output).resolve()
    try:
        artifact.relative_to(root / "build")
        site.relative_to(root / "build")
    except ValueError as exc:
        raise PublicationError(
            "output_outside_build",
            "release outputs must remain below the canonical build directory",
            cause=exc,
        ) from exc
    artifact_id, artifact_files = _artifact_identity(artifact)
    documentation_id, documentation_files = _directory_identity(site)
    return ReleasePlan(
        project=root,
        starter_artifact=artifact,
        site_artifact=site,
        starter_repository=publication.starter_repository,
        branch=publication.branch,
        release_id=release_id,
        docs_base_url=publication.docs_base_url,
        source_commit=_source_commit(root),
        artifact_id=artifact_id,
        documentation_id=documentation_id,
        artifact_files=artifact_files,
        documentation_files=documentation_files,
        generator_version=generator_version,
    )


def _metadata(plan: ReleasePlan, *, stage: str) -> dict[str, Any]:
    return {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "stage": stage,
        "release_id": plan.release_id,
        "source_commit": plan.source_commit,
        "artifact_id": plan.artifact_id,
        "documentation_id": plan.documentation_id,
        "starter_repository": plan.starter_repository,
        "branch": plan.branch,
        "documentation_url": plan.documentation_url,
        "generator_version": plan.generator_version,
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _transaction_path(plan: ReleasePlan) -> Path:
    return plan.project / "build" / "release" / plan.release_id / "transaction.json"


def _new_transaction(plan: ReleasePlan) -> dict[str, Any]:
    return {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "status": "planned",
        "plan": plan.to_dict(),
        "starter": {"status": "pending"},
        "documentation": {"status": "pending"},
        "template": {"status": "pending"},
        "template_marked": False,
    }


def _load_or_create_transaction(plan: ReleasePlan) -> tuple[Path, dict[str, Any]]:
    path = _transaction_path(plan)
    if not path.exists():
        transaction = _new_transaction(plan)
        _write_json(path, transaction)
        return path, transaction
    try:
        transaction = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(
            "transaction_invalid",
            f"release transaction is not readable: {path}",
            cause=exc,
        ) from exc
    if transaction.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
        raise PublicationError(
            "transaction_invalid", f"unsupported transaction schema: {path}"
        )
    if transaction.get("plan") != plan.to_dict():
        raise PublicationError(
            "transaction_conflict",
            "existing release state does not match the current validated plan",
            details={"transaction": path.as_posix()},
        )
    return path, transaction


def _save_stage(
    path: Path,
    transaction: dict[str, Any],
    *,
    stage: str,
    result: StageResult | None = None,
    error: PublicationError | None = None,
) -> None:
    if result is not None:
        transaction[stage] = result.to_dict()
    if error is not None:
        transaction[stage] = {
            "status": "failed",
            "code": error.code,
            "message": str(error),
            "details": error.details,
        }
    transaction["status"] = f"{stage}_{transaction[stage].get('status', 'pending')}"
    _write_json(path, transaction)


def _read_release_metadata(root: Path) -> dict[str, Any] | None:
    if root.is_symlink() or (root / ".startergen").is_symlink():
        raise PublicationError(
            "unsafe_target", f"release metadata path contains a symlink: {root}"
        )
    path = root / RELEASE_METADATA_PATH
    if path.is_symlink():
        raise PublicationError(
            "unsafe_target", f"release metadata is a symlink: {path}"
        )
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(
            "release_metadata_invalid",
            f"release metadata is not readable: {path}",
            cause=exc,
        ) from exc
    if not isinstance(value, dict):
        raise PublicationError(
            "release_metadata_invalid", f"release metadata is not a mapping: {path}"
        )
    return value


def _assert_not_stale(plan: ReleasePlan, metadata: dict[str, Any] | None) -> None:
    if metadata is None:
        return
    current_release = metadata.get("release_id")
    if not isinstance(current_release, str):
        raise PublicationError(
            "release_metadata_invalid", "published release metadata has no release_id"
        )
    if current_release == plan.release_id:
        if (
            metadata.get("artifact_id") != plan.artifact_id
            or (
                metadata.get("documentation_id") is not None
                and metadata.get("documentation_id") != plan.documentation_id
            )
            or (
                metadata.get("source_commit") is not None
                and metadata.get("source_commit") != plan.source_commit
            )
        ):
            raise PublicationError(
                "immutable_release_conflict",
                f"release {plan.release_id} already exists with a different artifact",
            )
        return
    if _release_rank(current_release) > _release_rank(plan.release_id):
        raise PublicationError(
            "stale_release",
            f"release {plan.release_id} is older than already published {current_release}",
            details={"current_release": current_release},
        )


def _validate_git_worktree(worktree: Path, *, plan: ReleasePlan) -> None:
    worktree = worktree.expanduser().resolve()
    if worktree == plan.project:
        raise PublicationError(
            "canonical_destination",
            "the starter publication target cannot be the canonical checkout",
        )
    if not worktree.is_dir():
        raise PublicationError(
            "starter_target_missing", f"starter worktree does not exist: {worktree}"
        )
    if (
        _git(
            ["rev-parse", "--is-inside-work-tree"], cwd=worktree, allow_failure=True
        ).returncode
        != 0
    ):
        raise PublicationError(
            "starter_target_not_git",
            f"starter target is not a Git worktree: {worktree}",
        )
    status = _git(["status", "--porcelain=v1"], cwd=worktree).stdout.strip()
    if status:
        raise PublicationError(
            "starter_target_dirty",
            "starter publication target has uncommitted changes",
            details={"status": status},
        )


def _validate_starter_remote(worktree: Path, repository: str) -> None:
    """Reject an obvious GitHub target mismatch, while allowing local test remotes."""

    remote = _git_output(["remote", "get-url", "origin"], cwd=worktree)
    if not remote:
        raise PublicationError(
            "starter_remote_missing", "starter target has no origin remote"
        )
    if "://" not in remote and not remote.startswith("git@"):
        return
    normalized = remote.removesuffix(".git").rstrip("/")
    for prefix in (
        "https://github.com/",
        "http://github.com/",
        "ssh://git@github.com/",
    ):
        if normalized.casefold().startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    else:
        if normalized.startswith("git@github.com:"):
            normalized = normalized.removeprefix("git@github.com:")
        else:
            return
    if normalized.casefold() != repository.casefold():
        raise PublicationError(
            "starter_remote_mismatch",
            f"starter origin {remote!r} does not match configured repository {repository!r}",
            details={"remote": remote, "configured": repository},
        )


def _clear_worktree(worktree: Path) -> None:
    for child in worktree.iterdir():
        if child.name == ".git":
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            raise PublicationError(
                "unsafe_target", f"starter target contains a special file: {child}"
            )


def _copy_tree_contents(source: Path, destination: Path) -> None:
    for child in source.iterdir():
        target = destination / child.name
        if child.is_symlink():
            raise PublicationError(
                "unsafe_output", f"publication output contains a symlink: {child}"
            )
        if child.is_dir():
            shutil.copytree(child, target, copy_function=shutil.copy2)
        elif child.is_file():
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            shutil.copy2(child, target)
        else:
            raise PublicationError(
                "unsafe_output", f"publication output contains a special file: {child}"
            )


def _remote_ref(worktree: Path, ref: str) -> str | None:
    result = _git(["ls-remote", "origin", ref], cwd=worktree, allow_failure=True)
    if result.returncode != 0:
        raise PublicationError(
            "remote_verification_failed",
            f"could not inspect origin ref {ref}",
            details={"stderr": result.stderr},
        )
    line = next((line for line in result.stdout.splitlines() if line.strip()), None)
    return line.split("\t", 1)[0] if line is not None else None


def _verify_remote_release(
    worktree: Path, *, branch: str, release_id: str, commit: str
) -> None:
    branch_commit = _remote_ref(worktree, f"refs/heads/{branch}")
    tag_commit = _remote_ref(worktree, f"refs/tags/{release_id}")
    if branch_commit != commit or tag_commit != commit:
        raise PublicationError(
            "remote_verification_failed",
            "remote release does not match the committed artifact",
            details={
                "expected_commit": commit,
                "branch_commit": branch_commit,
                "tag_commit": tag_commit,
            },
        )


def publish_starter(
    plan: ReleasePlan,
    worktree: Path,
) -> StageResult:
    """Publish and remotely verify the starter Git stage."""

    worktree = worktree.expanduser()
    if worktree.is_symlink():
        raise PublicationError(
            "unsafe_target", f"starter target is a symlink: {worktree}"
        )
    worktree = worktree.resolve()
    _validate_git_worktree(worktree, plan=plan)
    _validate_starter_remote(worktree, plan.starter_repository)
    _git(["fetch", "--no-tags", "origin", plan.branch], cwd=worktree)
    current_branch = _git_output(["branch", "--show-current"], cwd=worktree)
    if current_branch != plan.branch:
        local_branch = _git(
            ["show-ref", "--verify", f"refs/heads/{plan.branch}"],
            cwd=worktree,
            allow_failure=True,
        )
        if local_branch.returncode == 0:
            _git(["switch", plan.branch], cwd=worktree)
        else:
            _git(
                ["switch", "--create", plan.branch, f"origin/{plan.branch}"],
                cwd=worktree,
            )
    _git(["reset", "--keep", f"origin/{plan.branch}"], cwd=worktree)
    remote_metadata = _read_release_metadata(worktree)
    _assert_not_stale(plan, remote_metadata)

    _clear_worktree(worktree)
    _copy_tree_contents(plan.starter_artifact, worktree)
    metadata = _metadata(plan, stage="starter")
    _write_json(worktree / RELEASE_METADATA_PATH, metadata)
    _git(["add", "--all"], cwd=worktree)
    diff = _git(["diff", "--cached", "--quiet"], cwd=worktree, allow_failure=True)
    if diff.returncode == 0:
        commit = _git_output(["rev-parse", "HEAD"], cwd=worktree)
        status = "skipped"
    else:
        _git(["commit", "-m", f"Release {plan.release_id}"], cwd=worktree)
        commit = _git_output(["rev-parse", "HEAD"], cwd=worktree)
        status = "published"

    existing_tag = _remote_ref(worktree, f"refs/tags/{plan.release_id}")
    if existing_tag is not None and existing_tag != commit:
        raise PublicationError(
            "immutable_tag_conflict",
            f"release tag {plan.release_id} already points to another commit",
            details={"existing": existing_tag, "expected": commit},
        )
    if existing_tag is None:
        _git(["push", "origin", f"HEAD:{plan.branch}"], cwd=worktree)
        _git(["tag", plan.release_id, commit], cwd=worktree)
        _git(["push", "origin", f"refs/tags/{plan.release_id}"], cwd=worktree)
    else:
        _git(["push", "origin", f"HEAD:{plan.branch}"], cwd=worktree)
    _verify_remote_release(
        worktree, branch=plan.branch, release_id=plan.release_id, commit=commit
    )
    return StageResult(status=status, commit=commit, verified=True)


def _atomic_replace_directory(staging: Path, destination: Path) -> None:
    backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
    had_previous = destination.exists()
    if had_previous:
        destination.rename(backup)
    try:
        staging.rename(destination)
    except BaseException:
        if had_previous and backup.exists() and not destination.exists():
            backup.rename(destination)
        raise
    if backup.exists():
        try:
            shutil.rmtree(backup)
        except OSError:
            # The new destination is already committed; keep the backup for
            # manual recovery rather than reporting a false failed release.
            pass


def _docs_metadata(plan: ReleasePlan) -> dict[str, Any]:
    value = _metadata(plan, stage="documentation")
    value["documentation_id"] = plan.documentation_id
    return value


def deploy_documentation(plan: ReleasePlan, docs_root: Path) -> StageResult:
    """Atomically deploy one version and then update the ``latest`` copy."""

    docs_root = docs_root.expanduser()
    if docs_root.is_symlink():
        raise PublicationError(
            "unsafe_target", f"documentation destination is a symlink: {docs_root}"
        )
    docs_root = docs_root.resolve()
    if docs_root == plan.project or plan.project in docs_root.parents:
        raise PublicationError(
            "canonical_destination",
            "documentation destination cannot be inside the canonical checkout",
        )
    docs_root.mkdir(mode=0o755, parents=True, exist_ok=True)
    releases = docs_root / "releases"
    releases.mkdir(mode=0o755, exist_ok=True)
    release = releases / plan.release_id
    existing = _read_release_metadata(release)
    latest_metadata = _read_release_metadata(docs_root / "latest")
    _assert_not_stale(plan, existing)
    _assert_not_stale(plan, latest_metadata)
    if (
        existing is not None
        and existing.get("documentation_id") == plan.documentation_id
        and latest_metadata is not None
        and latest_metadata.get("release_id") == plan.release_id
    ):
        return StageResult(
            status="skipped",
            release_path=release.as_posix(),
            verified=True,
        )

    staging_root = Path(
        tempfile.mkdtemp(prefix=f".startergen-{plan.release_id}-", dir=docs_root)
    )
    staging_release = staging_root / "release"
    try:
        shutil.copytree(plan.site_artifact, staging_release, copy_function=shutil.copy2)
        _write_json(staging_release / RELEASE_METADATA_PATH, _docs_metadata(plan))
        release.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        _atomic_replace_directory(staging_release, release)

        staging_latest = staging_root / "latest"
        shutil.copytree(release, staging_latest, copy_function=shutil.copy2)
        _atomic_replace_directory(staging_latest, docs_root / "latest")
    except PublicationError:
        raise
    except (OSError, shutil.Error) as exc:
        raise PublicationError(
            "documentation_deploy_failed",
            "documentation deployment failed before latest was updated",
            cause=exc,
        ) from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    published = _read_release_metadata(release)
    latest = _read_release_metadata(docs_root / "latest")
    if (
        published is None
        or latest is None
        or latest.get("artifact_id") != plan.artifact_id
    ):
        raise PublicationError(
            "documentation_verification_failed",
            "deployed documentation does not carry the validated release identity",
        )
    return StageResult(
        status="published", release_path=release.as_posix(), verified=True
    )


def _mark_template(repository: str) -> None:
    token = os.environ.get("STARTER_REPO_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise PublicationError(
            "template_token_missing",
            "--mark-template requires STARTER_REPO_TOKEN or GH_TOKEN for the target repository",
        )
    environment = _git_environment()
    environment["GH_TOKEN"] = token
    try:
        result = subprocess.run(
            ["gh", "repo", "edit", repository, "--template"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise PublicationError(
            "gh_unavailable", "--mark-template requires the gh executable", cause=exc
        ) from exc
    if result.returncode != 0:
        raise PublicationError(
            "template_mark_failed",
            f"GitHub template configuration failed for {repository}",
            details={"stdout": result.stdout, "stderr": result.stderr},
        )


def release_project(
    root: Path,
    *,
    starter_worktree: Path | None = None,
    docs_root: Path | None = None,
    dry_run: bool = False,
    mark_template: bool = False,
    timeout: float = 60.0,
) -> ReleaseResult:
    """Run the integrated check and publish its immutable outputs.

    ``timeout`` is passed to the integrated checker so release validation and
    direct checker invocation use the same operational budget.
    """

    root = root.expanduser().resolve()
    check = check_project(root, timeout=timeout)
    plan = build_release_plan(root, check=check)
    transaction_path, transaction = _load_or_create_transaction(plan)
    if dry_run:
        return ReleaseResult(
            plan=plan,
            transaction=transaction_path,
            starter=StageResult(status="planned"),
            documentation=StageResult(status="planned"),
            template_marked=False,
            dry_run=True,
        )
    if starter_worktree is None or docs_root is None:
        raise PublicationError(
            "publication_target_missing",
            "a real release requires both --starter-worktree and --docs-root",
        )
    if mark_template and (transaction.get("template_marked") is True):
        marked = True
    else:
        marked = False

    try:
        starter = publish_starter(plan, starter_worktree)
        _save_stage(transaction_path, transaction, stage="starter", result=starter)
    except PublicationError as exc:
        _save_stage(transaction_path, transaction, stage="starter", error=exc)
        raise
    try:
        documentation = deploy_documentation(plan, docs_root)
        _save_stage(
            transaction_path, transaction, stage="documentation", result=documentation
        )
    except PublicationError as exc:
        _save_stage(transaction_path, transaction, stage="documentation", error=exc)
        raise
    if mark_template and not marked:
        try:
            _mark_template(plan.starter_repository)
        except PublicationError as exc:
            transaction["template"] = {
                "status": "failed",
                "code": exc.code,
                "message": str(exc),
                "details": exc.details,
            }
            transaction["status"] = "template_failed"
            _write_json(transaction_path, transaction)
            raise
        transaction["template"] = {"status": "published"}
        transaction["template_marked"] = True
        marked = True
    transaction["status"] = "completed"
    _write_json(transaction_path, transaction)
    return ReleaseResult(
        plan=plan,
        transaction=transaction_path,
        starter=starter,
        documentation=documentation,
        template_marked=marked,
        dry_run=False,
    )
