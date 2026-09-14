"""Run integrated checks against disposable copies of canonical fixtures."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from startergen.check import check_project

FIXTURE_NAMES = ("minimal_completed_project", "mvp_canonical_project")


def _copy_without_generated_outputs(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("build", ".pytest_cache", "__pycache__"),
    )


def main() -> int:
    repository_root = Path(__file__).resolve().parents[3]
    fixture_root = repository_root / "tools" / "startergen" / "tests" / "fixtures"
    reports: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="startergen-reuse-") as temporary:
        workspace = Path(temporary)
        for name in FIXTURE_NAMES:
            project = workspace / name
            _copy_without_generated_outputs(fixture_root / name, project)
            reports[name] = check_project(project).to_dict()
    print(json.dumps(reports, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
