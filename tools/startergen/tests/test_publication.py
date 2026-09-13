from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import pytest
from startergen import publication
from startergen.assembly import build_project
from startergen.check import CheckReport
from startergen.documentation import build_documentation
from startergen.publication import (
    PublicationError,
    build_release_plan,
    deploy_documentation,
    publish_starter,
    release_project,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mvp_canonical_project"


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _copy_fixture(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(
        FIXTURE,
        project,
        ignore=shutil.ignore_patterns("build", ".pytest_cache", "__pycache__"),
    )
    (project / ".gitignore").write_text("build/\n__pycache__/\n", encoding="utf-8")
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.name", "Publication Tests")
    _git(project, "config", "user.email", "publication@example.test")
    _git(
        project,
        "add",
        ".gitignore",
        "teaching",
        "src",
        "tests",
        "pyproject.toml",
        "uv.lock",
        "LICENSE",
    )
    _git(project, "commit", "-m", "canonical fixture")
    return project


def _checked_plan(project: Path):
    result = build_project(project)
    build_documentation(project)
    check = CheckReport(
        project=project,
        artifact=result.output,
        dependency_order=(),
        canonical_tests=(),
        baseline={},
        checkpoints=(),
        completed_public="passed",
        reproducible=True,
        installed=True,
    )
    return build_release_plan(project, check=check)


def _starter_worktree(tmp_path: Path) -> Path:
    remote = tmp_path / "starter.git"
    _git(tmp_path, "init", "--bare", remote.as_posix())
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.name", "Publication Tests")
    _git(seed, "config", "user.email", "publication@example.test")
    (seed / "README.md").write_text("starter repository\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "initialize starter repository")
    _git(seed, "remote", "add", "origin", remote.as_posix())
    _git(seed, "push", "origin", "main")
    worktree = tmp_path / "starter"
    _git(tmp_path, "clone", remote.as_posix(), worktree.as_posix())
    _git(worktree, "config", "user.name", "Publication Tests")
    _git(worktree, "config", "user.email", "publication@example.test")
    return worktree


def test_dry_run_writes_reviewable_plan_and_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_fixture(tmp_path)
    plan = _checked_plan(project)
    monkeypatch.setattr(
        publication,
        "check_project",
        lambda root, timeout: CheckReport(
            project=root,
            artifact=plan.starter_artifact,
            dependency_order=(),
            canonical_tests=(),
            baseline={},
            checkpoints=(),
            completed_public="passed",
            reproducible=True,
            installed=True,
        ),
    )

    result = release_project(project, dry_run=True)

    transaction = json.loads(result.transaction.read_text(encoding="utf-8"))
    assert result.dry_run is True
    assert transaction["status"] == "planned"
    assert transaction["plan"]["source_commit"] == plan.source_commit
    assert transaction["plan"]["artifact_id"] == plan.artifact_id
    assert not (tmp_path / "starter").exists()


def test_release_plan_rejects_branch_configuration_drift(tmp_path: Path) -> None:
    project = _copy_fixture(tmp_path)
    plan = _checked_plan(project)

    with pytest.raises(PublicationError) as error:
        build_release_plan(
            project,
            check=CheckReport(
                project=project,
                artifact=plan.starter_artifact,
                dependency_order=(),
                canonical_tests=(),
                baseline={},
                checkpoints=(),
                completed_public="passed",
                reproducible=True,
                installed=True,
            ),
            expected_starter_branch="release",
        )

    assert error.value.code == "publication_branch_mismatch"


def test_release_project_records_independent_completed_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_fixture(tmp_path)
    plan = _checked_plan(project)
    worktree = _starter_worktree(tmp_path)
    monkeypatch.setattr(
        publication,
        "check_project",
        lambda root, timeout: CheckReport(
            project=root,
            artifact=plan.starter_artifact,
            dependency_order=(),
            canonical_tests=(),
            baseline={},
            checkpoints=(),
            completed_public="passed",
            reproducible=True,
            installed=True,
        ),
    )

    result = release_project(
        project, starter_worktree=worktree, docs_root=tmp_path / "docs"
    )

    transaction = json.loads(result.transaction.read_text(encoding="utf-8"))
    assert transaction["status"] == "completed"
    assert transaction["starter"]["status"] == "published"
    assert transaction["starter"]["verified"] is True
    assert transaction["documentation"]["status"] == "published"
    assert transaction["documentation"]["verified"] is True


def test_starter_publication_is_tagged_verified_and_redundant_safe(
    tmp_path: Path,
) -> None:
    project = _copy_fixture(tmp_path)
    plan = _checked_plan(project)
    worktree = _starter_worktree(tmp_path)

    first = publish_starter(plan, worktree)
    second = publish_starter(plan, worktree)

    assert first.status == "published"
    assert second.status == "skipped"
    assert first.commit is not None
    assert _git(worktree, "ls-remote", "origin", "refs/heads/main").startswith(
        first.commit
    )
    assert _git(worktree, "ls-remote", "origin", "refs/tags/v1").startswith(
        first.commit
    )
    metadata = json.loads(
        (worktree / ".startergen" / "release.json").read_text(encoding="utf-8")
    )
    assert metadata["artifact_id"] == plan.artifact_id
    assert not (worktree / "tests/private").exists()


def test_starter_tag_push_retry_reuses_local_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_fixture(tmp_path)
    plan = _checked_plan(project)
    worktree = _starter_worktree(tmp_path)
    real_git = publication._git
    failed = False

    def fail_first_tag_push(
        arguments: list[str], *, cwd: Path, allow_failure: bool = False
    ):
        nonlocal failed
        if arguments == ["push", "origin", "refs/tags/v1"] and not failed:
            failed = True
            raise PublicationError("git_command_failed", "simulated tag push failure")
        return real_git(arguments, cwd=cwd, allow_failure=allow_failure)

    monkeypatch.setattr(publication, "_git", fail_first_tag_push)
    with pytest.raises(PublicationError) as error:
        publish_starter(plan, worktree)

    assert error.value.code == "git_command_failed"
    assert publication._local_ref(worktree, "refs/tags/v1") is not None
    retry = publish_starter(plan, worktree)

    assert failed is True
    assert retry.status == "skipped"
    assert retry.verified is True


def test_non_github_starter_remote_is_rejected(
    tmp_path: Path,
) -> None:
    project = _copy_fixture(tmp_path)
    plan = _checked_plan(project)
    worktree = _starter_worktree(tmp_path)
    _git(
        worktree,
        "remote",
        "set-url",
        "origin",
        "https://gitlab.com/example/mvp-starter.git",
    )

    with pytest.raises(PublicationError) as error:
        publish_starter(plan, worktree)

    assert error.value.code == "starter_remote_mismatch"


def test_documentation_update_is_atomic_and_latest_is_versioned(tmp_path: Path) -> None:
    project = _copy_fixture(tmp_path)
    plan = _checked_plan(project)
    docs_root = tmp_path / "docs"

    result = deploy_documentation(plan, docs_root)

    assert result.status == "published"
    assert (docs_root / "releases" / "v1" / "index.html").is_file()
    latest = json.loads(
        (docs_root / "latest" / ".startergen" / "release.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["release_id"] == "v1"
    assert latest["documentation_id"] == plan.documentation_id
    base_path = urlparse(plan.docs_base_url).path.rstrip("/") + "/"
    published_markdown = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (docs_root / "releases" / plan.release_id).rglob("*.md")
    )
    published_links = re.findall(r"https://[^\s)]+", published_markdown)
    assert published_links
    for link in published_links:
        parsed = urlparse(link)
        assert parsed.path.startswith(base_path)
        relative = parsed.path.removeprefix(base_path)
        assert (docs_root / relative).is_file(), link


def test_occupied_release_directory_without_metadata_is_immutable(
    tmp_path: Path,
) -> None:
    project = _copy_fixture(tmp_path)
    plan = _checked_plan(project)
    docs_root = tmp_path / "docs"
    occupied = docs_root / "releases" / plan.release_id
    occupied.mkdir(parents=True)
    (occupied / "index.html").write_text("manual content\n", encoding="utf-8")

    with pytest.raises(PublicationError) as error:
        deploy_documentation(plan, docs_root)

    assert error.value.code == "immutable_release_conflict"
    assert (occupied / "index.html").read_text(encoding="utf-8") == "manual content\n"


def test_symlinked_releases_parent_is_rejected(tmp_path: Path) -> None:
    project = _copy_fixture(tmp_path)
    plan = _checked_plan(project)
    docs_root = tmp_path / "docs"
    outside = tmp_path / "outside"
    outside.mkdir()
    docs_root.mkdir()
    (docs_root / "releases").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PublicationError) as error:
        deploy_documentation(plan, docs_root)

    assert error.value.code == "unsafe_target"
    assert not (outside / plan.release_id).exists()


def test_older_release_cannot_replace_newer_documentation(tmp_path: Path) -> None:
    project = _copy_fixture(tmp_path)
    plan = _checked_plan(project)
    docs_root = tmp_path / "docs"
    newer = replace(plan, release_id="v2")
    deploy_documentation(newer, docs_root)

    with pytest.raises(PublicationError) as error:
        deploy_documentation(plan, docs_root)

    assert error.value.code == "stale_release"
    latest = json.loads(
        (docs_root / "latest" / ".startergen" / "release.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["release_id"] == "v2"


def test_concurrent_documentation_promotions_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_fixture(tmp_path)
    plan = _checked_plan(project)
    docs_root = tmp_path / "docs"
    older = replace(plan, release_id="v2")
    newer = replace(plan, release_id="v3")
    staging_started = threading.Event()
    allow_older = threading.Event()
    newer_waiting_for_lock = threading.Event()
    newer_finished = threading.Event()
    original_copytree = publication.shutil.copytree
    paused = False
    outcomes: dict[str, object] = {}

    def pause_older_staging(
        source: str | Path, destination: str | Path, *args: object, **kwargs: object
    ):
        nonlocal paused
        if not paused and Path(source) == older.site_artifact:
            paused = True
            staging_started.set()
            assert allow_older.wait(timeout=5)
        return original_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(publication.shutil, "copytree", pause_older_staging)
    original_lock = publication._documentation_lock

    @contextmanager
    def observe_newer_lock(root: Path):
        if threading.current_thread().name == "v3":
            newer_waiting_for_lock.set()
        with original_lock(root):
            yield

    monkeypatch.setattr(publication, "_documentation_lock", observe_newer_lock)

    def run_release(name: str, release_plan) -> None:
        try:
            outcomes[name] = deploy_documentation(release_plan, docs_root)
        except PublicationError as exc:  # pragma: no cover - reported below
            outcomes[name] = exc
        finally:
            if name == "v3":
                newer_finished.set()

    older_thread = threading.Thread(target=run_release, args=("v2", older), name="v2")
    newer_thread = threading.Thread(target=run_release, args=("v3", newer), name="v3")
    older_thread.start()
    assert staging_started.wait(timeout=5)
    newer_thread.start()
    assert newer_waiting_for_lock.wait(timeout=5)
    assert not newer_finished.is_set()
    allow_older.set()
    older_thread.join(timeout=10)
    newer_thread.join(timeout=10)

    assert not older_thread.is_alive()
    assert not newer_thread.is_alive()
    assert isinstance(outcomes["v2"], publication.StageResult)
    assert isinstance(outcomes["v3"], publication.StageResult)
    latest = json.loads(
        (docs_root / "latest" / ".startergen" / "release.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["release_id"] == "v3"


def test_failed_documentation_stage_preserves_previous_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_fixture(tmp_path)
    plan = _checked_plan(project)
    docs_root = tmp_path / "docs"
    deploy_documentation(plan, docs_root)
    newer = replace(plan, release_id="v2")
    real_copytree = publication.shutil.copytree

    def fail_new_site(
        source: str | Path, destination: str | Path, *args: object, **kwargs: object
    ):
        if Path(source) == newer.site_artifact:
            raise OSError("simulated site failure")
        return real_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(publication.shutil, "copytree", fail_new_site)
    with pytest.raises(PublicationError) as error:
        deploy_documentation(newer, docs_root)

    assert error.value.code == "documentation_deploy_failed"
    latest = json.loads(
        (docs_root / "latest" / ".startergen" / "release.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["release_id"] == "v1"
    assert not (docs_root / "releases" / "v2").exists()
