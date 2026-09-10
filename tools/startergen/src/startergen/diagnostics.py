"""Stable, serializable diagnostics shared by validation and future commands."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One actionable validation diagnostic.

    ``code`` is intended for automation; the message is intended for a
    maintainer. Optional context is omitted from text output when unavailable.
    """

    code: str
    message: str
    file: str | None = None
    line: int | None = None
    exercise_id: str | None = None
    suggestion: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        for name in ("file", "line", "exercise_id", "suggestion"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result

    def format_text(self) -> str:
        location = self.file or "project"
        if self.line is not None:
            location += f":{self.line}"
        context = f" [{self.exercise_id}]" if self.exercise_id else ""
        text = f"{location}: {self.code}{context}: {self.message}"
        if self.suggestion:
            text += f" Suggestion: {self.suggestion}"
        return text


@dataclass(slots=True)
class ValidationReport:
    """Validation result that can be rendered for a terminal or CI."""

    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.diagnostics

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.ok,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)
