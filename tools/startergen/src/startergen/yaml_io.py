"""Safe YAML loading with duplicate-key detection and source locations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class YamlInputError(ValueError):
    """A YAML document could not be loaded under the startergen policy."""

    def __init__(
        self, message: str, *, line: int | None = None, code: str = "yaml_error"
    ):
        super().__init__(message)
        self.line = line
        self.code = code


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate mapping keys."""


def _construct_mapping(
    loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:  # pragma: no cover - defensive for exotic YAML
            raise YamlInputError(
                "mapping keys must be scalar values",
                line=key_node.start_mark.line + 1,
                code="yaml_key_type",
            ) from exc
        if duplicate:
            raise YamlInputError(
                f"duplicate mapping key {key!r}",
                line=key_node.start_mark.line + 1,
                code="duplicate_yaml_key",
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_yaml(path: Path) -> Any:
    """Load one YAML document without executing tags or accepting duplicates."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise YamlInputError(str(exc), code="yaml_read_error") from exc
    try:
        value = yaml.load(text, Loader=_StrictLoader)
    except YamlInputError:
        raise
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else None
        detail = getattr(exc, "problem", None) or str(exc)
        raise YamlInputError(detail, line=line) from exc
    if value is None:
        raise YamlInputError("document is empty", code="empty_yaml")
    return value
