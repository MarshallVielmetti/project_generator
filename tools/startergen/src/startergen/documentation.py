"""Student documentation generation and offline authoring checks.

The documentation pipeline intentionally has two small adapters rather than a
single renderer with output-specific conditionals:

* :func:`render_readme` emits GitHub-flavoured Markdown and links exercises to
  the final files in the generated starter tree.
* :func:`render_site` emits a MkDocs-compatible Markdown page and a static HTML
  preview.  Published source links use the configured, immutable release id.

The parser covers the subset used by teaching pages (headings, links, images,
fenced code, inline code, math, and ``!!!`` admonitions).  It rejects template
syntax in teaching Markdown so Jinja is confined to the maintainer-owned
layout template.
"""

from __future__ import annotations

import html
import io
import json
import os
import posixpath
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from jinja2 import Environment, StrictUndefined, TemplateError
from mkdocs.commands.build import build as mkdocs_build
from mkdocs.config import load_config as load_mkdocs_config
from mkdocs.exceptions import MkDocsException
from pydantic import ValidationError

from startergen.assembly import (
    BuildResult,
    build_project_locked,
    project_build_lock,
)
from startergen.diagnostics import Diagnostic
from startergen.paths import project_path
from startergen.schema import Exercise, ExerciseMetadata, ProjectMetadata
from startergen.transform import ResolvedTarget
from startergen.validate import validate_project
from startergen.yaml_io import YamlInputError, load_yaml_bytes

MATHJAX_VERSION = "3.2.2"
MATHJAX_SCRIPT = (
    "https://cdn.jsdelivr.net/npm/"
    f"mathjax@{MATHJAX_VERSION}/es5/tex-mml-chtml.js"
)

_DEFAULT_MKDOCS_CONFIG = """\
site_name: Starter project documentation
theme:
  name: material
  features:
    - navigation.sections
    - navigation.top
    - search.suggest
    - content.code.copy
  palette:
    - media: "(prefers-color-scheme: light)"
      scheme: default
      toggle:
        icon: material/brightness-7
        name: Switch to dark mode
    - media: "(prefers-color-scheme: dark)"
      scheme: slate
      toggle:
        icon: material/brightness-4
        name: Switch to light mode
markdown_extensions:
  - admonition
  - pymdownx.details
  - pymdownx.superfences
  - pymdownx.arithmatex:
      generic: true
  - toc:
      permalink: true
plugins:
  - search
extra_javascript:
  - _startergen/mathjax-config.js
  - https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-mml-chtml.js
"""

_SITE_RESERVED_PATHS = {"_startergen", "assets", "background", "index.md", "source"}

_DIRECTIVE = re.compile(
    r'''^\s*\{\{\s*exercise\(\s*["'](?P<id>[a-z0-9]+(?:-[a-z0-9]+)*)["']\s*\)\s*\}\}\s*$'''
)
_FENCE = re.compile(r"^\s*(?P<mark>`{3,}|~{3,})(?P<info>[^`]*)\s*$")
_HEADING = re.compile(r"^\s{0,3}(?P<marks>#{1,6})\s+(?P<title>.+?)\s*#*\s*$")
_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)]+)\)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_PROTECTED = re.compile(
    r"`+[^`\n]*`+|\$\$[^$\n]*\$\$|\$(?!\$)[^$\n]+\$(?!\$)|\\\([^\n]*?\\\)"
)


class DocumentationError(RuntimeError):
    """Actionable error raised by documentation parsing or generation."""

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
class ExerciseLink:
    """An exercise mapped to its final generated-source line range."""

    id: str
    title: str
    source_path: str
    symbol: str
    start_line: int
    end_line: int
    readme_href: str
    site_href: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "source": {
                "path": self.source_path,
                "symbol": self.symbol,
                "start_line": self.start_line,
                "end_line": self.end_line,
            },
            "links": {"readme": self.readme_href, "site": self.site_href},
        }


@dataclass(frozen=True, slots=True)
class DocumentationResult:
    """Paths and index entries produced by :func:`build_documentation`."""

    readme: Path
    generated_source: Path
    site: Path
    exercise_index: tuple[ExerciseLink, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "readme": self.readme.as_posix(),
            "generated_source": self.generated_source.as_posix(),
            "site": self.site.as_posix(),
            "exercise_index": [entry.to_dict() for entry in self.exercise_index],
        }


@dataclass(frozen=True, slots=True)
class _Reference:
    href: str
    image: bool
    line: int


@dataclass(frozen=True, slots=True)
class _ParsedMarkdown:
    lines: tuple[str, ...]
    directives: tuple[tuple[int, str], ...]
    headings: frozenset[str]
    references: tuple[_Reference, ...]
    external_links: tuple[str, ...]


def _mask_protected(line: str) -> str:
    """Blank inline code/math while retaining positions for diagnostics."""

    return _PROTECTED.sub(lambda match: " " * len(match.group(0)), line)


def _inline_math_diagnostic(line: str, *, file: str, number: int) -> Diagnostic | None:
    """Return a diagnostic for an unmatched supported inline delimiter."""

    without_code = re.sub(r"`+[^`\n]*`+", lambda match: " " * len(match.group(0)), line)
    tokens = re.finditer(r"(?<!\\)(\$\$|\$|\\\(|\\\)|\\\[|\\\])", without_code)
    dollar_open = False
    paren_open = False
    bracket_open = False
    for token in tokens:
        value = token.group(1)
        if value == "$$":
            continue
        if value == "$":
            dollar_open = not dollar_open
        elif value == "\\(":
            if paren_open:
                return Diagnostic(
                    "unclosed_math",
                    "inline math opens before the previous \\( expression closes",
                    file=file,
                    line=number,
                    suggestion="Close inline math with \\), or use a paired $ delimiter.",
                )
            paren_open = True
        elif value == "\\)":
            if not paren_open:
                return Diagnostic(
                    "unclosed_math",
                    "inline math closes without a preceding \\( delimiter",
                    file=file,
                    line=number,
                    suggestion="Start inline math with \\( before using \\).",
                )
            paren_open = False
        elif value == "\\[":
            bracket_open = True
        elif value == "\\]":
            if not bracket_open:
                return Diagnostic(
                    "unclosed_math",
                    "display math closes without a preceding \\[ delimiter",
                    file=file,
                    line=number,
                    suggestion="Use paired \\[ and \\] delimiters or the supported $$ block form.",
                )
            bracket_open = False
    if dollar_open or paren_open or bracket_open:
        return Diagnostic(
            "unclosed_math",
            "inline math delimiter is not closed",
            file=file,
            line=number,
            suggestion="Close the math expression with $, \\), or \\].",
        )
    return None


def _slugify(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value.casefold())
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    return re.sub(r"[-\s]+", "-", value).strip("-")


def parse_markdown(text: str, *, file: str) -> _ParsedMarkdown:
    """Parse supported Markdown constructs and find safe exercise directives."""

    lines = tuple(text.splitlines())
    diagnostics: list[Diagnostic] = []
    directives: list[tuple[int, str]] = []
    references: list[_Reference] = []
    external_links: list[str] = []
    headings: set[str] = set()
    fence_mark: str | None = None
    fence_line = 0
    math_block = False

    for number, line in enumerate(lines, start=1):
        fence = _FENCE.match(line)
        if fence_mark is not None:
            if fence and fence.group("mark")[0] == fence_mark:
                fence_mark = None
            continue
        if fence is not None:
            fence_mark = fence.group("mark")[0]
            fence_line = number
            continue
        stripped = line.strip()
        if math_block:
            if stripped.endswith("$$"):
                math_block = False
            continue
        if stripped == "$$" or (
            stripped.startswith("$$") and not stripped.endswith("$$")
        ):
            math_block = True
            continue

        masked = _mask_protected(line)
        directive = _DIRECTIVE.fullmatch(stripped)
        if directive is not None and _mask_protected(stripped).strip() == stripped:
            directives.append((number, directive.group("id")))
        elif "{{" in masked or "}}" in masked:
            diagnostics.append(
                Diagnostic(
                    "malformed_directive",
                    "only a standalone {{ exercise(\"id\") }} directive is supported",
                    file=file,
                    line=number,
                    suggestion="Put one exercise directive on its own Markdown line and use a declared exercise id.",
                )
            )
        if "{%" in masked or "%}" in masked or ":::" in masked:
            diagnostics.append(
                Diagnostic(
                    "unsupported_markdown",
                    "template blocks and ::: extensions are not supported in teaching Markdown",
                    file=file,
                    line=number,
                    suggestion="Use the supported Markdown subset or put layout logic in the README template.",
                )
            )
        if re.match(r"^\s*!!!\s*", masked) and not re.match(
            r'^\s*!{3}\s+(note|warning|tip)(?:\s+"[^"]+")?\s*$', masked
        ):
            diagnostics.append(
                Diagnostic(
                    "unsupported_markdown",
                    "only note, warning, and tip admonitions are supported",
                    file=file,
                    line=number,
                    suggestion="Use !!! note, !!! warning, or !!! tip.",
                )
            )
        inline_math = _inline_math_diagnostic(line, file=file, number=number)
        if inline_math is not None:
            diagnostics.append(inline_math)
        heading = _HEADING.match(line)
        if heading is not None:
            headings.add(_slugify(heading.group("title")))
        for match in _LINK.finditer(masked):
            href = match.group(3).strip().strip("<>")
            image = bool(match.group(1))
            references.append(_Reference(href, image, number))
            parsed = urlparse(href)
            if parsed.scheme in {"http", "https"}:
                external_links.append(href)

    if fence_mark is not None:
        diagnostics.append(
            Diagnostic(
                "unclosed_fence",
                "fenced code block is not closed",
                file=file,
                line=fence_line,
                suggestion="Close the block with a matching ``` or ~~~ fence.",
            )
        )
    if math_block:
        diagnostics.append(
            Diagnostic(
                "unclosed_math",
                "display math block is not closed",
                file=file,
                line=len(lines),
                suggestion="Close the block with a second $$ line.",
            )
        )
    if diagnostics:
        raise DocumentationError(
            "markdown_invalid", "teaching Markdown failed authoring checks", diagnostics=diagnostics
        )
    return _ParsedMarkdown(
        lines=lines,
        directives=tuple(directives),
        headings=frozenset(headings),
        references=tuple(references),
        external_links=tuple(dict.fromkeys(external_links)),
    )


def _load_contracts(root: Path) -> tuple[ProjectMetadata, ExerciseMetadata]:
    try:
        config = ProjectMetadata.model_validate(
            load_yaml_bytes((root / "teaching" / "config.yml").read_bytes())
        )
        exercises = ExerciseMetadata.model_validate(
            load_yaml_bytes((root / "teaching" / "exercises.yml").read_bytes())
        )
    except (OSError, YamlInputError, ValidationError) as exc:
        raise DocumentationError(
            "contract_load_failed", "documentation contracts could not be loaded", cause=exc
        ) from exc
    return config, exercises


def _source_target_map(result: BuildResult) -> dict[str, ResolvedTarget]:
    return {target.target.exercise_id: target for target in result.targets}


def _exercise_links(
    config: ProjectMetadata,
    exercises: ExerciseMetadata,
    result: BuildResult,
) -> tuple[ExerciseLink, ...]:
    targets = _source_target_map(result)
    publication = config.publication
    links: list[ExerciseLink] = []
    missing: list[Diagnostic] = []
    for exercise in exercises.exercises:
        target = targets.get(exercise.id)
        if target is None:
            missing.append(
                Diagnostic(
                    "missing_final_source_target",
                    f"exercise {exercise.id!r} has no final generated-source position",
                    file="teaching/exercises.yml",
                    exercise_id=exercise.id,
                    suggestion="Build the starter artifact before rendering documentation.",
                )
            )
            continue
        source = exercise.source.file
        start = target.source_range.start_line
        end = target.source_range.end_line
        encoded_source = quote(source, safe="/._-")
        readme_href = f"{encoded_source}#L{start}-L{end}"
        if publication is None:
            site_href = f"source/{encoded_source}.html#L{start}-L{end}"
        else:
            site_href = (
                f"https://github.com/{publication.starter_repository}/blob/"
                f"{publication.release_id}/{encoded_source}"
                f"#L{start}-L{end}"
            )
        links.append(
            ExerciseLink(
                id=exercise.id,
                title=exercise.title,
                source_path=source,
                symbol=target.qualified_name,
                start_line=start,
                end_line=end,
                readme_href=readme_href,
                site_href=site_href,
            )
        )
    if missing:
        raise DocumentationError(
            "missing_final_source_target",
            "documentation could not map every exercise to final source",
            diagnostics=missing,
        )
    return tuple(links)


def _placeholder_links(exercises: ExerciseMetadata) -> tuple[ExerciseLink, ...]:
    """Create links sufficient to preflight directives and templates."""

    return tuple(
        ExerciseLink(
            id=exercise.id,
            title=exercise.title,
            source_path=exercise.source.file,
            symbol=exercise.source.symbol,
            start_line=0,
            end_line=0,
            readme_href=f"{quote(exercise.source.file, safe='/._-')}#L0-L0",
            site_href=f"source/{quote(exercise.source.file, safe='/._-')}.html#L0-L0",
        )
        for exercise in exercises.exercises
    )


def _validate_references(
    root: Path,
    config: ProjectMetadata,
    parsed: _ParsedMarkdown,
    *,
    source_file: Path,
    source_display: str,
) -> None:
    assets = project_path(root, config.documentation.assets)
    assert assets is not None
    diagnostics: list[Diagnostic] = []

    def inside_root(candidate: Path) -> bool:
        try:
            candidate.relative_to(root)
        except ValueError:
            return False
        return True

    for reference in parsed.references:
        parsed_href = urlparse(reference.href)
        if parsed_href.scheme in {"http", "https", "mailto"}:
            continue
        path_part = parsed_href.path
        fragment = parsed_href.fragment
        if not path_part:
            if fragment and fragment not in parsed.headings:
                diagnostics.append(
                    Diagnostic(
                        "broken_anchor",
                        f"local anchor #{fragment} does not match a generated heading",
                        file=source_display,
                        line=reference.line,
                        suggestion="Use the slug of an existing heading or add that heading.",
                    )
                )
            continue
        if reference.image:
            candidate = (assets / path_part.removeprefix("assets/")).resolve()
            assets_root = assets.resolve()
            try:
                candidate.relative_to(assets_root)
            except ValueError:
                candidate = assets_root / ".missing-asset"
            if not candidate.is_file():
                diagnostics.append(
                    Diagnostic(
                        "missing_asset",
                        f"image asset does not exist: {path_part}",
                        file=source_display,
                        line=reference.line,
                        suggestion=f"Add the file below {config.documentation.assets}.",
                    )
                )
            continue
        candidate = (source_file.parent / path_part).resolve()
        if not inside_root(candidate) or not candidate.is_file():
            candidate = (root / path_part).resolve()
        if not inside_root(candidate) or not candidate.is_file():
            diagnostics.append(
                Diagnostic(
                    "broken_local_link",
                    f"local link target does not exist: {path_part}",
                    file=source_display,
                    line=reference.line,
                    suggestion="Correct the relative link or add the referenced file.",
                )
            )
        elif fragment and candidate.suffix.lower() in {".md", ".markdown"}:
            target_text = candidate.read_text(encoding="utf-8")
            target_headings = {
                _slugify(match.group("title"))
                for match in (_HEADING.match(value) for value in target_text.splitlines())
                if match is not None
            }
            if fragment not in target_headings:
                diagnostics.append(
                    Diagnostic(
                        "broken_anchor",
                        f"anchor #{fragment} is not present in {path_part}",
                        file=source_display,
                        line=reference.line,
                        suggestion="Use a heading anchor that exists in the linked Markdown file.",
                    )
                )
    if diagnostics:
        raise DocumentationError(
            "documentation_links_invalid",
            "teaching Markdown contains invalid local references",
            diagnostics=diagnostics,
        )


def _relative_to(path: Path, parent: Path) -> str | None:
    try:
        return path.resolve().relative_to(parent.resolve()).as_posix()
    except ValueError:
        return None


def _rewrite_local_links(
    text: str,
    *,
    root: Path,
    config: ProjectMetadata,
    result: BuildResult,
    source_file: Path,
    source_display: str,
    destination: str,
    output_file: str = "index.md",
) -> str:
    """Map authoring-relative links to files copied into each output."""

    assets = project_path(root, config.documentation.assets)
    background = project_path(root, config.documentation.background)
    pages = (
        project_path(root, config.documentation.pages)
        if config.documentation.pages is not None
        else None
    )
    assert assets is not None and background is not None
    published_files = {entry.path for entry in result.files}
    diagnostics: list[Diagnostic] = []

    def replacement(line: str) -> str:
        protected = [match.span() for match in _PROTECTED.finditer(line)]

        def replace(match: re.Match[str]) -> str:
            if bool(match.group(1)) or any(
                start <= match.start() < end for start, end in protected
            ):
                return match.group(0)
            href = match.group(3).strip().strip("<>")
            parsed = urlparse(href)
            if parsed.scheme in {"http", "https", "mailto"} or not parsed.path:
                return match.group(0)
            candidate = (source_file.parent / parsed.path).resolve()
            if _relative_to(candidate, root) is None or not candidate.is_file():
                candidate = (root / parsed.path).resolve()
            if _relative_to(candidate, root) is None or not candidate.is_file():
                return match.group(0)

            background_relative = _relative_to(candidate, background)
            assets_relative = _relative_to(candidate, assets)
            pages_relative = (
                _relative_to(candidate, pages) if pages is not None else None
            )
            root_relative = _relative_to(candidate, root)
            if background_relative is not None:
                output_path = f"background/{background_relative}"
            elif assets_relative is not None:
                output_path = f"assets/{assets_relative}"
            elif pages_relative is not None and destination == "site":
                output_path = pages_relative
            elif root_relative is not None and root_relative in published_files:
                output_path = (
                    f"source/{root_relative}"
                    if destination == "site"
                    else root_relative
                )
            else:
                diagnostics.append(
                    Diagnostic(
                        "unpublished_local_link",
                        f"local link target is not copied into the {destination}: {parsed.path}",
                        file=source_display,
                        line=line_number,
                        suggestion="Link to a starter allowlist file, documentation asset, or background file.",
                    )
                )
                return match.group(0)
            if destination == "site":
                output_path = posixpath.relpath(
                    output_path, start=posixpath.dirname(output_file) or "."
                )
            encoded = quote(output_path, safe="/._-")
            suffix = f"?{parsed.query}" if parsed.query else ""
            suffix += f"#{parsed.fragment}" if parsed.fragment else ""
            return f"[{match.group(2)}]({encoded}{suffix})"

        return _LINK.sub(replace, line)

    rewritten: list[str] = []
    fence_mark: str | None = None
    math_block = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        fence = _FENCE.match(line)
        if fence_mark is not None:
            rewritten.append(line)
            if fence and fence.group("mark")[0] == fence_mark:
                fence_mark = None
            continue
        if fence is not None:
            fence_mark = fence.group("mark")[0]
            rewritten.append(line)
            continue
        if math_block:
            rewritten.append(line)
            if line.strip().endswith("$$"):
                math_block = False
            continue
        if line.strip() == "$$" or (
            line.strip().startswith("$$") and not line.strip().endswith("$$")
        ):
            math_block = True
            rewritten.append(line)
            continue
        rewritten.append(replacement(line))
    if diagnostics:
        raise DocumentationError(
            "documentation_links_invalid",
            "some local links are not published into the generated outputs",
            diagnostics=diagnostics,
        )
    return "\n".join(rewritten) + ("\n" if text.endswith("\n") else "")


def _asset_relative_path(href: str, assets: Path) -> str:
    path_part = urlparse(href).path
    if path_part.startswith("assets/"):
        path_part = path_part.removeprefix("assets/")
    candidate = (assets / path_part).resolve()
    try:
        return candidate.relative_to(assets.resolve()).as_posix()
    except ValueError:
        return path_part


def _rewrite_assets(
    text: str, *, assets: Path, output_file: str = "index.md"
) -> str:
    def rewrite_line(line: str) -> str:
        protected = [match.span() for match in _PROTECTED.finditer(line)]

        def replace(match: re.Match[str]) -> str:
            if any(start <= match.start() < end for start, end in protected):
                return match.group(0)
            alt, href = match.group(1), match.group(2).strip()
            parsed = urlparse(href)
            if parsed.scheme or href.startswith("#"):
                return match.group(0)
            relative = _asset_relative_path(href, assets)
            output_path = posixpath.relpath(
                f"assets/{relative}", start=posixpath.dirname(output_file) or "."
            )
            return f"![{alt}]({quote(output_path, safe='/._-')})"

        return _IMAGE.sub(replace, line)

    result: list[str] = []
    fence_mark: str | None = None
    math_block = False
    for line in text.splitlines():
        fence = _FENCE.match(line)
        if fence_mark is not None:
            result.append(line)
            if fence and fence.group("mark")[0] == fence_mark:
                fence_mark = None
            continue
        if fence is not None:
            fence_mark = fence.group("mark")[0]
            result.append(line)
            continue
        if math_block:
            result.append(line)
            if line.strip().endswith("$$"):
                math_block = False
            continue
        if line.strip() == "$$" or (
            line.strip().startswith("$$") and not line.strip().endswith("$$")
        ):
            math_block = True
            result.append(line)
            continue
        result.append(rewrite_line(line))
    return "\n".join(result) + ("\n" if text.endswith("\n") else "")


def _rewrite_readme_admonitions(text: str) -> str:
    """Lower supported MkDocs admonitions to GitHub-renderable blockquotes."""

    lines = text.splitlines()
    result: list[str] = []
    index = 0
    fence_mark: str | None = None
    pattern = re.compile(r'^\s*!{3}\s+(note|warning|tip)(?:\s+"([^"]+)")?\s*$')
    while index < len(lines):
        fence = _FENCE.match(lines[index])
        if fence_mark is not None:
            result.append(lines[index])
            if fence and fence.group("mark")[0] == fence_mark:
                fence_mark = None
            index += 1
            continue
        if fence is not None:
            fence_mark = fence.group("mark")[0]
            result.append(lines[index])
            index += 1
            continue
        match = pattern.match(lines[index])
        if match is None:
            result.append(lines[index])
            index += 1
            continue
        label = match.group(1).title()
        title = match.group(2)
        heading = f"{label} — {title}" if title else label
        result.append(f"> **{heading}**")
        index += 1
        while index < len(lines) and lines[index].startswith("    "):
            result.append(f"> {lines[index][4:]}")
            index += 1
    return "\n".join(result) + ("\n" if text.endswith("\n") else "")


def _rewrite_exercises(
    text: str,
    *,
    links: Sequence[ExerciseLink],
    href_name: str,
    source_file: str,
) -> str:
    by_id = {link.id: link for link in links}
    result: list[str] = []
    fence_mark: str | None = None
    math_block = False
    for number, line in enumerate(text.splitlines(), start=1):
        fence = _FENCE.match(line)
        if fence_mark is not None:
            result.append(line)
            if fence and fence.group("mark")[0] == fence_mark:
                fence_mark = None
            continue
        if fence is not None:
            fence_mark = fence.group("mark")[0]
            result.append(line)
            continue
        if math_block:
            result.append(line)
            if line.strip().endswith("$$"):
                math_block = False
            continue
        if line.strip() == "$$" or (
            line.strip().startswith("$$") and not line.strip().endswith("$$")
        ):
            math_block = True
            result.append(line)
            continue
        match = _DIRECTIVE.fullmatch(line.strip())
        if match is None:
            result.append(line)
            continue
        exercise_id = match.group("id")
        link = by_id.get(exercise_id)
        if link is None:
            diagnostic = Diagnostic(
                "unknown_exercise_id",
                f"exercise directive refers to unknown id {exercise_id!r}",
                file=source_file,
                line=number,
                exercise_id=exercise_id,
                suggestion="Declare the id in teaching/exercises.yml or correct the directive.",
            )
            raise DocumentationError(
                "unknown_exercise_id",
                "teaching Markdown contains an unknown exercise directive",
                diagnostics=(diagnostic,),
            )
        href = getattr(link, href_name)
        indent = line[: len(line) - len(line.lstrip())]
        result.append(
            f"{indent}[{link.title} — {link.symbol} "
            f"({link.source_path}:{link.start_line}-{link.end_line})]({href})"
        )
    return "\n".join(result) + ("\n" if text.endswith("\n") else "")


def render_readme(
    source_markdown: str,
    template: str,
    *,
    config: ProjectMetadata,
    exercises: Sequence[Exercise],
    links: Sequence[ExerciseLink],
    assets: Path,
    source_file: str = "teaching/project.md",
) -> str:
    """Render a GitHub README using the maintainer-owned Jinja layout."""

    content = _rewrite_readme_admonitions(
        _rewrite_assets(
            _rewrite_exercises(
                source_markdown,
                links=links,
                href_name="readme_href",
                source_file=source_file,
            ),
            assets=assets,
        )
    )
    context = {
        "project": {
            "name": config.project.name,
            "import_package": config.project.import_package,
        },
        "content": content,
        "exercises": [
            {"id": exercise.id, "title": exercise.title}
            for exercise in exercises
        ],
        "exercise_index": [link.to_dict() for link in links],
    }
    try:
        rendered_template = Environment(
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        ).from_string(template).render(context)
    except TemplateError as exc:
        raise DocumentationError(
            "template_render_failed",
            "README layout template could not be rendered with the documented context",
            cause=exc,
        ) from exc
    if "{{" in template and re.search(r"\{\{\s*content\b", template):
        return rendered_template
    return rendered_template.rstrip() + "\n\n" + content.lstrip()


def _build_mkdocs_site(
    docs_dir: Path,
    site_dir: Path,
    *,
    title: str,
    site_url: str | None,
    config_file: Path | None,
) -> None:
    """Build one strict Material for MkDocs site into *site_dir*."""

    source: str | io.BytesIO
    if config_file is None:
        source = io.BytesIO(_DEFAULT_MKDOCS_CONFIG.encode("utf-8"))
    else:
        source = str(config_file)
    try:
        config = load_mkdocs_config(
            source,
            docs_dir=str(docs_dir),
            site_dir=str(site_dir),
            site_name=title,
            site_url=site_url,
            strict=True,
            use_directory_urls=True,
        )
        mkdocs_build(config)
    except (MkDocsException, OSError, ValueError) as exc:
        raise DocumentationError(
            "site_build_failed",
            "Material for MkDocs could not build the generated documentation site",
            cause=exc,
        ) from exc


def _write_mathjax_config(destination: Path) -> None:
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    destination.write_text(
        """window.MathJax = {
  tex: {
    inlineMath: [[\"\\\\(\", \"\\\\)\"], [\"$\", \"$\"]],
    displayMath: [[\"\\\\[\", \"\\\\]\"], [\"$$\", \"$$\"]],
    tags: \"all\",
    processEnvironments: true
  },
  options: {
    ignoreHtmlClass: \"mathjax_ignore\",
    processHtmlClass: \"arithmatex\"
  }
};
""",
        encoding="utf-8",
    )


def render_site(source_markdown: str, *, title: str) -> tuple[str, str]:
    """Render one Markdown page with the default Material for MkDocs theme."""

    with tempfile.TemporaryDirectory(prefix="startergen-mkdocs-") as temporary:
        root = Path(temporary)
        docs_dir = root / "docs"
        site_dir = root / "site"
        docs_dir.mkdir()
        (docs_dir / "index.md").write_text(source_markdown, encoding="utf-8")
        _write_mathjax_config(docs_dir / "_startergen" / "mathjax-config.js")
        _build_mkdocs_site(
            docs_dir,
            site_dir,
            title=title,
            site_url=None,
            config_file=None,
        )
        return source_markdown, (site_dir / "index.html").read_text(encoding="utf-8")


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise DocumentationError("missing_input", f"documentation directory does not exist: {source}")
    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink():
            raise DocumentationError("symlink_input", f"documentation input contains a symlink: {relative}")
        if path.is_dir():
            target.mkdir(mode=0o755, parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _site_page_sources(
    root: Path, config: ProjectMetadata
) -> tuple[tuple[Path, str], ...]:
    """Return additional Markdown pages and reject generated-path collisions."""

    if config.documentation.pages is None:
        return ()
    pages = project_path(root, config.documentation.pages)
    assert pages is not None
    if not pages.is_dir():
        raise DocumentationError(
            "pages_not_directory",
            f"documentation.pages must name a directory: {config.documentation.pages}",
        )
    markdown: list[tuple[Path, str]] = []
    for path in sorted(
        pages.rglob("*"), key=lambda item: item.relative_to(pages).as_posix()
    ):
        relative = path.relative_to(pages)
        if relative.parts and relative.parts[0].casefold() in _SITE_RESERVED_PATHS:
            raise DocumentationError(
                "site_path_collision",
                f"documentation.pages uses reserved generated path: {relative}",
            )
        if path.is_file() and path.suffix.casefold() in {".md", ".markdown"}:
            markdown.append((path, relative.as_posix()))
    return tuple(markdown)


def _documentation_site_url(config: ProjectMetadata) -> str | None:
    publication = config.publication
    if publication is None:
        return None
    return (
        f"{publication.docs_base_url.rstrip('/')}/releases/"
        f"{publication.release_id}/"
    )


def _merge_tree(source: Path, destination: Path) -> None:
    """Copy a generated tree without deleting or overwriting existing files."""

    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise DocumentationError(
            "artifact_collision", f"documentation destination is not a directory: {destination}"
        )
    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink():
            raise DocumentationError("symlink_input", f"documentation input contains a symlink: {relative}")
        if path.is_dir():
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise DocumentationError("artifact_collision", f"asset path collides with a file: {relative}")
            target.mkdir(mode=0o755, parents=True, exist_ok=True)
        elif path.is_file():
            if target.is_symlink() or target.exists():
                raise DocumentationError("artifact_collision", f"documentation asset collides with an existing file: {relative}")
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _copy_artifact_source(starter: Path, destination: Path) -> None:
    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    for path in sorted(starter.rglob("*"), key=lambda item: item.relative_to(starter).as_posix()):
        relative = path.relative_to(starter)
        if relative.parts and relative.parts[0] == ".startergen":
            continue
        target = destination / relative
        if path.is_dir():
            target.mkdir(mode=0o755, parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _render_source_html(
    source: Path, *, ranges: Sequence[tuple[int, int]], title: str
) -> str:
    """Render a source file with stable single-line and range anchors."""

    lines = source.read_text(encoding="utf-8").splitlines()
    ranges_by_start: dict[int, list[tuple[int, int]]] = {}
    for start, end in ranges:
        if start > 0 and end >= start:
            ranges_by_start.setdefault(start, []).append((start, end))
    rendered: list[str] = []
    for number, line in enumerate(lines, start=1):
        range_anchors = "".join(
            f'<a id="L{start}-L{end}"></a>'
            for start, end in ranges_by_start.get(number, ())
        )
        rendered.append(
            f'{range_anchors}<span class="line" id="L{number}">'
            f'<a class="line-number" href="#L{number}">{number}</a>'
            f"{html.escape(line, quote=False)}</span>"
        )
    body = "\n".join(rendered)
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        "<style>"
        "body{margin:0;background:#f7f7f7;color:#222;font:14px/1.5 monospace;}"
        "pre{margin:0;padding:1rem;overflow:auto;}"
        ".line{display:block;min-width:max-content;padding-right:2rem;}"
        ".line:target{background:#fff2a8;}"
        ".line-number{display:inline-block;width:4rem;margin-right:1rem;"
        "color:#777;text-align:right;text-decoration:none;user-select:none;}"
        "a[id^=L]{display:block;position:relative;top:-1rem;}"
        "</style></head><body><pre>"
        f"{body}\n"
        "</pre></body></html>\n"
    )


def _copy_anchored_sources(
    starter: Path, destination: Path, links: Sequence[ExerciseLink]
) -> None:
    """Copy raw sources and line-anchored pages used by generated indexes."""

    _copy_artifact_source(starter, destination)
    ranges_by_source: dict[str, list[tuple[int, int]]] = {}
    for link in links:
        ranges_by_source.setdefault(link.source_path, []).append(
            (link.start_line, link.end_line)
        )
    for relative, ranges in ranges_by_source.items():
        source = starter.joinpath(*relative.split("/"))
        if not source.is_file():
            raise DocumentationError(
                "missing_final_source_target",
                f"final source file is missing from the generated artifact: {relative}",
            )
        page = destination.joinpath(*relative.split("/")).with_name(
            f"{source.name}.html"
        )
        page.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        page.write_text(
            _render_source_html(source, ranges=ranges, title=relative),
            encoding="utf-8",
        )


def _replace_directory(path: Path, writer: Callable[[Path], None]) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{path.name}-", dir=path.parent))
    backup = path.with_name(f".{path.name}-backup")
    had_previous = path.exists()
    try:
        writer(staging)
        if backup.exists():
            shutil.rmtree(backup)
        if had_previous:
            path.rename(backup)
        try:
            staging.rename(path)
        except BaseException:
            if had_previous and backup.exists() and not path.exists():
                os.rename(backup, path)
            raise
        if backup.exists():
            try:
                shutil.rmtree(backup)
            except OSError:
                pass
        staging = Path()
    except BaseException:
        if staging != Path() and staging.exists():
            shutil.rmtree(staging)
        raise


def _snapshot_directory(path: Path) -> Path | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_dir():
        raise DocumentationError("unsafe_destination", f"documentation output is not an ordinary directory: {path}")
    snapshot = Path(tempfile.mkdtemp(prefix=f".snapshot-{path.name}-", dir=path.parent))
    shutil.rmtree(snapshot)
    shutil.copytree(path, snapshot)
    return snapshot


def _restore_directory(path: Path, snapshot: Path | None) -> None:
    if path.exists():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    if snapshot is not None and snapshot.exists():
        snapshot.rename(path)


def _refresh_manifest(result: BuildResult) -> None:
    """Include generated README/assets in the already-promoted artifact."""

    from startergen.assembly import _artifact_files

    manifest_path = result.manifest
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = [entry.to_dict() for entry in _artifact_files(result.output)]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _check_external_links(urls: Sequence[str], *, source_file: str) -> None:
    diagnostics: list[Diagnostic] = []
    for url in urls:
        try:
            request = Request(url, method="HEAD", headers={"User-Agent": "startergen"})
            try:
                with urlopen(request, timeout=10) as response:
                    status = response.status
            except (OSError, ValueError) as exc:
                status = getattr(exc, "code", None)
                if status not in {405, 501}:
                    raise
            if status in {405, 501}:
                request = Request(
                    url,
                    method="GET",
                    headers={"Range": "bytes=0-0", "User-Agent": "startergen"},
                )
                with urlopen(request, timeout=10) as response:
                    status = response.status
                    if status >= 400:
                        raise OSError(f"HTTP {status}")
            elif status >= 400:
                raise OSError(f"HTTP {status}")
        except (OSError, ValueError) as exc:
            diagnostics.append(
                Diagnostic(
                    "external_link_failed",
                    f"public external link could not be checked: {url} ({exc})",
                    file=source_file,
                    suggestion="Fix the URL or run documentation validation with external checks disabled for offline work.",
                )
            )
    if diagnostics:
        raise DocumentationError(
            "external_links_invalid",
            "public external link checks failed",
            diagnostics=diagnostics,
        )


def build_documentation(
    root: Path,
    *,
    build_result: BuildResult | None = None,
    check_external_links: bool = False,
) -> DocumentationResult:
    """Generate README, final-source index, and MkDocs/site outputs."""

    root = root.expanduser().resolve()
    report = validate_project(root)
    if not report.ok:
        raise DocumentationError(
            "validation_failed", "project validation failed; documentation was not generated", diagnostics=report.diagnostics
        )
    config, exercises = _load_contracts(root)

    source_path = project_path(root, config.documentation.source)
    template_path = project_path(root, config.documentation.readme_template)
    assets = project_path(root, config.documentation.assets)
    background = project_path(root, config.documentation.background)
    pages = (
        project_path(root, config.documentation.pages)
        if config.documentation.pages is not None
        else None
    )
    site_config = (
        project_path(root, config.documentation.site_config)
        if config.documentation.site_config is not None
        else None
    )
    generated_source = project_path(root, config.documentation.generated_source)
    site = project_path(root, config.documentation.site_output)
    assert (
        source_path is not None
        and template_path is not None
        and assets is not None
        and background is not None
        and generated_source is not None
        and site is not None
    )
    if site_config is not None and not site_config.is_file():
        raise DocumentationError(
            "site_config_not_file",
            f"documentation.site_config must name a file: {config.documentation.site_config}",
        )
    page_sources = _site_page_sources(root, config)
    try:
        source_markdown = source_path.read_text(encoding="utf-8")
        template = template_path.read_text(encoding="utf-8")
        page_markdown = {
            output_file: path.read_text(encoding="utf-8")
            for path, output_file in page_sources
        }
    except OSError as exc:
        raise DocumentationError("documentation_read_failed", "documentation inputs could not be read", cause=exc) from exc
    source_display = source_path.relative_to(root).as_posix()
    parsed = parse_markdown(source_markdown, file=source_display)
    _validate_references(
        root,
        config,
        parsed,
        source_file=source_path,
        source_display=source_display,
    )
    if check_external_links:
        _check_external_links(parsed.external_links, source_file=source_display)
    for page_path, output_file in page_sources:
        page_display = page_path.relative_to(root).as_posix()
        parsed_page = parse_markdown(page_markdown[output_file], file=page_display)
        _validate_references(
            root,
            config,
            parsed_page,
            source_file=page_path,
            source_display=page_display,
        )
        if check_external_links:
            _check_external_links(
                parsed_page.external_links, source_file=page_display
            )

    placeholders = _placeholder_links(exercises)
    render_readme(
        source_markdown,
        template,
        config=config,
        exercises=exercises.exercises,
        links=placeholders,
        assets=assets,
        source_file=source_display,
    )
    _ = _rewrite_assets(
        _rewrite_exercises(
            source_markdown,
            links=placeholders,
            href_name="site_href",
            source_file=source_display,
        ),
        assets=assets,
    )
    for page_path, output_file in page_sources:
        _rewrite_assets(
            _rewrite_exercises(
                page_markdown[output_file],
                links=placeholders,
                href_name="site_href",
                source_file=page_path.relative_to(root).as_posix(),
            ),
            assets=assets,
            output_file=output_file,
        )

    starter_output = project_path(root, config.starter.output)
    assert starter_output is not None
    output_paths = (starter_output, generated_source, site)
    lock = project_build_lock(root)
    lock.__enter__()
    snapshots: dict[Path, Path | None] = {}
    try:
        for output_path in output_paths:
            output_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            snapshots[output_path] = _snapshot_directory(output_path)
    except BaseException:
        lock.__exit__(None, None, None)
        raise

    try:
        result = build_result or build_project_locked(root)
        links = _exercise_links(config, exercises, result)
        readme_source = _rewrite_local_links(
            source_markdown,
            root=root,
            config=config,
            result=result,
            source_file=source_path,
            source_display=source_display,
            destination="readme",
        )
        readme = render_readme(
            readme_source,
            template,
            config=config,
            exercises=exercises.exercises,
            links=links,
            assets=assets,
            source_file=source_display,
        )
        site_source = _rewrite_local_links(
            source_markdown,
            root=root,
            config=config,
            result=result,
            source_file=source_path,
            source_display=source_display,
            destination="site",
            output_file="index.md",
        )
        site_source = _rewrite_assets(
            _rewrite_exercises(
                site_source,
                links=links,
                href_name="site_href",
                source_file=source_display,
            ),
            assets=assets,
            output_file="index.md",
        )
        site_pages: dict[str, str] = {}
        for page_path, output_file in page_sources:
            page_display = page_path.relative_to(root).as_posix()
            page_source = _rewrite_local_links(
                page_markdown[output_file],
                root=root,
                config=config,
                result=result,
                source_file=page_path,
                source_display=page_display,
                destination="site",
                output_file=output_file,
            )
            site_pages[output_file] = _rewrite_assets(
                _rewrite_exercises(
                    page_source,
                    links=links,
                    href_name="site_href",
                    source_file=page_display,
                ),
                assets=assets,
                output_file=output_file,
            )

        starter_readme = result.output / "README.md"
        _merge_tree(assets, result.output / "assets")
        _merge_tree(background, result.output / "background")
        starter_readme.write_text(readme, encoding="utf-8")
        _refresh_manifest(result)

        def write_generated(destination: Path) -> None:
            _copy_anchored_sources(result.output, destination / "source", links)
            (destination / "exercises.json").write_text(
                json.dumps(
                    {"schema_version": 1, "exercises": [link.to_dict() for link in links]},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            index_lines = ["# Exercise source index", ""]
            for link in links:
                encoded_source = quote(link.source_path, safe="/._-")
                index_lines.append(
                    f"- [{link.title} — `{link.symbol}`](source/{encoded_source}.html"
                    f"#L{link.start_line}-L{link.end_line}) "
                    f"({link.source_path}:{link.start_line}-{link.end_line})"
                )
            (destination / "index.md").write_text(
                "\n".join(index_lines) + "\n", encoding="utf-8"
            )

        def write_site(destination: Path) -> None:
            docs_dir = Path(
                tempfile.mkdtemp(prefix=".startergen-site-source-", dir=site.parent)
            )
            try:
                if pages is not None:
                    _copy_tree(pages, docs_dir)
                (docs_dir / "index.md").write_text(site_source, encoding="utf-8")
                for output_file, content in site_pages.items():
                    page = docs_dir.joinpath(*output_file.split("/"))
                    page.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    page.write_text(content, encoding="utf-8")
                _copy_anchored_sources(result.output, docs_dir / "source", links)
                _copy_tree(assets, docs_dir / "assets")
                _copy_tree(background, docs_dir / "background")
                _write_mathjax_config(
                    docs_dir / "_startergen" / "mathjax-config.js"
                )
                _build_mkdocs_site(
                    docs_dir,
                    destination,
                    title=config.project.name,
                    site_url=_documentation_site_url(config),
                    config_file=site_config,
                )
            finally:
                shutil.rmtree(docs_dir, ignore_errors=True)

        _replace_directory(generated_source, write_generated)
        _replace_directory(site, write_site)
        return DocumentationResult(
            readme=starter_readme,
            generated_source=generated_source,
            site=site,
            exercise_index=links,
        )
    except BaseException:
        for output_path, snapshot in snapshots.items():
            _restore_directory(output_path, snapshot)
        raise
    finally:
        for snapshot in snapshots.values():
            if snapshot is not None and snapshot.exists():
                shutil.rmtree(snapshot)
        lock.__exit__(None, None, None)


generate_documentation = build_documentation
