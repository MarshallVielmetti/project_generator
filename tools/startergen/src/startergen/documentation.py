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
import json
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from jinja2 import Environment, StrictUndefined, TemplateError
from pydantic import ValidationError

from startergen.assembly import BuildResult, build_project
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
        heading = _HEADING.match(line)
        if heading is not None:
            headings.add(_slugify(heading.group("title")))
        for match in _LINK.finditer(masked):
            href = match.group(3).strip().strip("<>")
            image = bool(match.group(1))
            references.append(_Reference(href, image, number))
            parsed = urlparse(href)
            if parsed.scheme in {"http", "https", "mailto"}:
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
        readme_href = f"{source}#L{start}-L{end}"
        if publication is None:
            site_href = f"source/{source}#L{start}-L{end}"
        else:
            base = publication.docs_base_url.rstrip("/")
            encoded_source = quote(source, safe="/._-")
            site_href = (
                f"{base}/{publication.release_id}/source/{encoded_source}"
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
        path_part, separator, fragment = reference.href.partition("#")
        if not path_part and separator:
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
            if not inside_root(candidate) or not candidate.is_file():
                candidate = (assets / Path(path_part).name).resolve()
            if not inside_root(candidate) or not candidate.is_file():
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


def _rewrite_assets(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        alt, href = match.group(1), match.group(2).strip()
        parsed = urlparse(href)
        if parsed.scheme or href.startswith("#"):
            return match.group(0)
        return f"![{alt}](assets/{Path(parsed.path).name})"

    return _IMAGE.sub(replace, text)


def _rewrite_readme_admonitions(text: str) -> str:
    """Lower supported MkDocs admonitions to GitHub-renderable blockquotes."""

    lines = text.splitlines()
    result: list[str] = []
    index = 0
    pattern = re.compile(r'^\s*!{3}\s+(note|warning|tip)(?:\s+"([^"]+)")?\s*$')
    while index < len(lines):
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
        result.append(f"[{link.title} — {link.symbol} ({link.source_path}:{link.start_line}-{link.end_line})]({href})")
    return "\n".join(result) + ("\n" if text.endswith("\n") else "")


def render_readme(
    source_markdown: str,
    template: str,
    *,
    config: ProjectMetadata,
    exercises: Sequence[Exercise],
    links: Sequence[ExerciseLink],
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
            )
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


def _inline_html(value: str) -> str:
    pattern = re.compile(
        r"!\[([^\]]*)\]\(([^)]+)\)|\[([^\]]+)\]\(([^)]+)\)|`([^`]+)`|"
        r"(\\\([^\n]*?\\\)|\$[^$\n]+\$)"
    )
    result: list[str] = []
    cursor = 0
    for match in pattern.finditer(value):
        result.append(html.escape(value[cursor : match.start()]))
        if match.group(1) is not None:
            result.append(
                f'<img src="{html.escape(match.group(2), quote=True)}" '
                f'alt="{html.escape(match.group(1), quote=True)}">'
            )
        elif match.group(3) is not None:
            result.append(
                f'<a href="{html.escape(match.group(4), quote=True)}">'
                f"{html.escape(match.group(3))}</a>"
            )
        elif match.group(5) is not None:
            result.append(f"<code>{html.escape(match.group(5))}</code>")
        else:
            result.append(f'<span class="arithmatex">{html.escape(match.group(6))}</span>')
        cursor = match.end()
    result.append(html.escape(value[cursor:]))
    rendered = "".join(result)
    rendered = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", rendered)
    return rendered


def render_site(source_markdown: str, *, title: str) -> tuple[str, str]:
    """Render MkDocs-compatible Markdown and a deterministic static preview."""

    markdown = source_markdown
    lines = markdown.splitlines()
    output: list[str] = []
    paragraph: list[str] = []
    fence: tuple[str, str] | None = None
    math_block: list[str] | None = None
    index = 0

    def flush_paragraph() -> None:
        if paragraph:
            output.append(f"<p>{_inline_html(' '.join(paragraph))}</p>")
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        fence_match = _FENCE.match(line)
        if fence is not None:
            if fence_match and fence_match.group("mark")[0] == fence[0]:
                output.append(f"</code></pre>")
                fence = None
            else:
                output.append(html.escape(line))
            index += 1
            continue
        if fence_match is not None:
            flush_paragraph()
            info = fence_match.group("info").strip()
            language = f' class="language-{html.escape(info)}"' if info else ""
            output.append(f"<pre><code{language}>")
            fence = (fence_match.group("mark")[0], info)
            index += 1
            continue
        if math_block is not None:
            if line.strip() == "$$":
                output.append(
                    '<div class="arithmatex">\\[ '
                    + html.escape(" ".join(math_block))
                    + r" \]</div>"
                )
                math_block = None
            else:
                math_block.append(line.strip())
            index += 1
            continue
        if line.strip() == "$$":
            flush_paragraph()
            math_block = []
            index += 1
            continue
        if line.strip().startswith("$$") and line.strip().endswith("$$"):
            flush_paragraph()
            content = line.strip()[2:-2].strip()
            output.append(
                '<div class="arithmatex">\\[ '
                + html.escape(content)
                + r" \]</div>"
            )
            index += 1
            continue
        admonition = re.match(r'^\s*!{3}\s+(note|warning|tip)(?:\s+"([^"]+)")?\s*$', line)
        if admonition:
            flush_paragraph()
            body: list[str] = []
            index += 1
            while index < len(lines):
                candidate = lines[index]
                if candidate.startswith("    "):
                    body.append(candidate[4:])
                    index += 1
                elif not candidate.strip():
                    body.append("")
                    index += 1
                else:
                    break
            heading = admonition.group(2) or admonition.group(1).title()
            output.append(
                f'<aside class="admonition {admonition.group(1)}">'
                f"<h3>{html.escape(heading)}</h3>"
                f"<p>{_inline_html(' '.join(body).strip())}</p></aside>"
            )
            continue
        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            level = len(heading.group("marks"))
            text = heading.group("title").strip().rstrip("#").strip()
            output.append(
                f'<h{level} id="{html.escape(_slugify(text), quote=True)}">'
                f"{_inline_html(text)}</h{level}>"
            )
            index += 1
            continue
        if not line.strip():
            flush_paragraph()
            index += 1
            continue
        list_match = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if list_match:
            flush_paragraph()
            items = [list_match.group(1)]
            index += 1
            while index < len(lines):
                next_match = re.match(r"^\s*[-*+]\s+(.+)$", lines[index])
                if next_match is None:
                    break
                items.append(next_match.group(1))
                index += 1
            output.append("<ul>" + "".join(f"<li>{_inline_html(item)}</li>" for item in items) + "</ul>")
            continue
        paragraph.append(line.strip())
        index += 1
    flush_paragraph()
    if fence is not None:
        raise DocumentationError("unclosed_fence", "site preview encountered an unclosed code fence")
    if math_block is not None:
        raise DocumentationError("unclosed_math", "site preview encountered an unclosed display math block")
    body = "\n".join(output)
    html_page = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<script>window.MathJax = {{tex: {{inlineMath: [['\\\\(', '\\\\)'], ['$', '$']]}}}};</script>
<script async src="{MATHJAX_SCRIPT}"></script>
</head>
<body>
{body}
</body>
</html>
'''
    return markdown, html_page


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


def _replace_directory(path: Path, writer: Callable[[Path], None]) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{path.name}-", dir=path.parent))
    try:
        writer(staging)
        backup = path.with_name(f".{path.name}-backup")
        if backup.exists():
            shutil.rmtree(backup)
        if path.exists():
            path.rename(backup)
        staging.rename(path)
        if backup.exists():
            shutil.rmtree(backup)
        staging = Path()
    except BaseException:
        if staging != Path() and staging.exists():
            shutil.rmtree(staging)
        raise


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
            with urlopen(request, timeout=10) as response:
                if response.status >= 400:
                    raise OSError(f"HTTP {response.status}")
        except Exception as exc:
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
    result = build_result or build_project(root)
    links = _exercise_links(config, exercises, result)

    source_path = project_path(root, config.documentation.source)
    template_path = project_path(root, config.documentation.readme_template)
    assert source_path is not None and template_path is not None
    try:
        source_markdown = source_path.read_text(encoding="utf-8")
        template = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DocumentationError("documentation_read_failed", "documentation inputs could not be read", cause=exc) from exc
    source_display = source_path.relative_to(root).as_posix()
    parsed = parse_markdown(source_markdown, file=source_display)
    _validate_references(root, config, parsed, source_file=source_path, source_display=source_display)
    if check_external_links:
        _check_external_links(parsed.external_links, source_file=source_display)

    readme = render_readme(
        source_markdown,
        template,
        config=config,
        exercises=exercises.exercises,
        links=links,
        source_file=source_display,
    )
    site_source = _rewrite_assets(
        _rewrite_exercises(
            source_markdown,
            links=links,
            href_name="site_href",
            source_file=source_display,
        )
    )
    site_markdown, site_html = render_site(site_source, title=config.project.name)

    starter_readme = result.output / "README.md"
    assets = project_path(root, config.documentation.assets)
    assert assets is not None
    starter_assets = result.output / "assets"
    if starter_assets.exists():
        shutil.rmtree(starter_assets)
    _copy_tree(assets, starter_assets)
    starter_readme.write_text(readme, encoding="utf-8")
    _refresh_manifest(result)

    generated_source = project_path(root, config.documentation.generated_source)
    site = project_path(root, config.documentation.site_output)
    assert generated_source is not None and site is not None

    def write_generated(destination: Path) -> None:
        _copy_artifact_source(result.output, destination / "source")
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
            index_lines.append(
                f"- [{link.title} — `{link.symbol}`](source/{link.source_path}"
                f"#L{link.start_line}-L{link.end_line}) "
                f"({link.source_path}:{link.start_line}-{link.end_line})"
            )
        (destination / "index.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    def write_site(destination: Path) -> None:
        _copy_artifact_source(result.output, destination / "source")
        _copy_tree(assets, destination / "assets")
        background = project_path(root, config.documentation.background)
        assert background is not None
        _copy_tree(background, destination / "background")
        (destination / "index.md").write_text(site_markdown, encoding="utf-8")
        (destination / "index.html").write_text(site_html, encoding="utf-8")
        (destination / "mathjax-config.js").write_text(
            "window.MathJax = {tex: {inlineMath: [['\\\\(', '\\\\)'], ['$', '$']]}};\n",
            encoding="utf-8",
        )

    _replace_directory(generated_source, write_generated)
    _replace_directory(site, write_site)
    return DocumentationResult(
        readme=starter_readme,
        generated_source=generated_source,
        site=site,
        exercise_index=links,
    )


generate_documentation = build_documentation
