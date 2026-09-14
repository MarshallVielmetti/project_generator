"""Keep all canonical-project fixtures out of generator test collection."""

from pathlib import Path


def pytest_ignore_collect(collection_path: Path, config: object) -> bool:
    """Run canonical fixture tests only through the integrated checker."""

    if "fixtures" not in collection_path.parts:
        return False
    invocation_dir = Path(config.invocation_params.dir).resolve()
    return "fixtures" not in invocation_dir.parts
