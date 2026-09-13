from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

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
