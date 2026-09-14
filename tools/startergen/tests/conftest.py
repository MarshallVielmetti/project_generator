"""Keep all canonical-project fixtures out of generator test collection."""

from pathlib import Path


def pytest_ignore_collect(collection_path: Path, config: object) -> bool:
    """Run canonical fixture tests only through the integrated checker."""

    del config
    return "fixtures" in collection_path.parts
