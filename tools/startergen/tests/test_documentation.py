from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Self

import pytest
from startergen import documentation
from startergen.cli import main
from startergen.documentation import DocumentationError, build_documentation

FIXTURE = Path(__file__).parent / "fixtures" / "minimal_completed_project"


def copy_fixture(tmp_path: Path) -> Path:
    destination = tmp_path / "project"
    shutil.copytree(FIXTURE, destination)
    return destination


def test_documentation_build_covers_student_and_site_outputs(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)

    result = build_documentation(project)
    readme = result.readme.read_text(encoding="utf-8")
    site_html = (result.site / "index.html").read_text(encoding="utf-8")
    index = json.loads((result.generated_source / "exercises.json").read_text(encoding="utf-8"))

    assert "src/lab_project/dynamics/unicycle.py#L7-L8" in readme
    assert '{{ exercise("unicycle-dynamics") }}' in readme
    assert "> **Note — Learning goal**" in readme
    assert "!!! note" not in readme
    assert (result.readme.parent / "assets" / "lab-diagram.svg").is_file()
    assert (
        "https://example.com/minimal-lab/releases/v1/source/src/lab_project/"
        "dynamics/unicycle.py.html#L7-L8"
    ) in site_html
    source_page = result.site / "source/src/lab_project/dynamics/unicycle.py.html"
    assert source_page.is_file()
    source_page_text = source_page.read_text(encoding="utf-8")
    assert 'id="L7-L8"' in source_page_text
    assert 'id="L7"' in source_page_text
    assert "https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-mml-chtml.js" in site_html
    assert '<meta name="generator" content="mkdocs-' in site_html
    assert '<div class="admonition note">' in site_html
    assert '<div class="arithmatex">\\[' in site_html
    assert "\\begin{bmatrix}" in site_html
    assert index["exercises"][0]["source"]["start_line"] == 7
    assert index["exercises"][0]["source"]["end_line"] == 8
    manifest = json.loads(
        (result.readme.parent / ".startergen" / "manifest.json").read_text(encoding="utf-8")
    )
    manifest_paths = {entry["path"] for entry in manifest["files"]}
    assert {"README.md", "assets/lab-diagram.svg"} <= manifest_paths


def test_nested_assets_and_protected_asset_examples_are_preserved(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    nested = project / "teaching" / "assets" / "figures" / "plot.svg"
    nested.parent.mkdir()
    nested.write_text("<svg/>", encoding="utf-8")
    (project / "teaching" / "project.md").write_text(
        """# Asset page

![Nested plot](assets/figures/plot.svg)

```markdown
![Literal example](assets/not-copied.svg)
```
""",
        encoding="utf-8",
    )

    result = build_documentation(project)
    readme = result.readme.read_text(encoding="utf-8")
    site = (result.site / "index.html").read_text(encoding="utf-8")
    assert "assets/figures/plot.svg" in readme
    assert "assets/figures/plot.svg" in site
    assert (result.readme.parent / "assets" / "figures" / "plot.svg").is_file()
    assert "assets/not-copied.svg" in readme


def test_material_site_supports_configured_navigation_and_pages(
    tmp_path: Path,
) -> None:
    project = copy_fixture(tmp_path)
    pages = project / "teaching" / "pages"
    guide = pages / "guides" / "first-exercise.md"
    guide.parent.mkdir(parents=True)
    guide.write_text(
        """# First exercise

Use the generated source link below.

{{ exercise("unicycle-dynamics") }}

![Lab diagram](assets/lab-diagram.svg)
""",
        encoding="utf-8",
    )
    stylesheet = pages / "stylesheets" / "extra.css"
    stylesheet.parent.mkdir()
    stylesheet.write_text(":root { --lab-accent: #ffcb05; }\n", encoding="utf-8")
    site_config = project / "teaching" / "mkdocs.yml"
    site_config.write_text(
        """site_name: Replaced by startergen
theme:
  name: material
nav:
  - Home: index.md
  - First exercise: guides/first-exercise.md
plugins:
  - search
extra_css:
  - stylesheets/extra.css
""",
        encoding="utf-8",
    )
    config = project / "teaching" / "config.yml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "  readme_template: teaching/templates/README.md.j2\n",
            "  readme_template: teaching/templates/README.md.j2\n"
            "  pages: teaching/pages\n"
            "  site_config: teaching/mkdocs.yml\n",
        ),
        encoding="utf-8",
    )

    result = build_documentation(project)

    page = result.site / "guides" / "first-exercise" / "index.html"
    html = page.read_text(encoding="utf-8")
    assert "First exercise" in html
    assert (
        "https://example.com/minimal-lab/releases/v1/source/src/lab_project/"
        "dynamics/unicycle.py.html#L7-L8"
    ) in html
    assert "../../assets/lab-diagram.svg" in html
    assert (result.site / "stylesheets" / "extra.css").is_file()
    assert (
        '<link rel="canonical" href="https://example.com/minimal-lab/releases/'
        'v1/guides/first-exercise/">'
    ) in html


def test_reserved_site_page_path_is_rejected(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    pages = project / "teaching" / "pages"
    pages.mkdir()
    (pages / "index.md").write_text("# Collision\n", encoding="utf-8")
    config = project / "teaching" / "config.yml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "  readme_template: teaching/templates/README.md.j2\n",
            "  readme_template: teaching/templates/README.md.j2\n"
            "  pages: teaching/pages\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(DocumentationError) as error:
        build_documentation(project)

    assert error.value.code == "site_path_collision"


def test_invalid_mkdocs_config_is_reported_as_documentation_error(
    tmp_path: Path,
) -> None:
    project = copy_fixture(tmp_path)
    site_config = project / "teaching" / "mkdocs.yml"
    site_config.write_text(
        "site_name: Broken\ntheme:\n  name: missing-theme\n", encoding="utf-8"
    )
    config = project / "teaching" / "config.yml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "  readme_template: teaching/templates/README.md.j2\n",
            "  readme_template: teaching/templates/README.md.j2\n"
            "  site_config: teaching/mkdocs.yml\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(DocumentationError) as error:
        build_documentation(project)

    assert error.value.code == "site_build_failed"


def test_allowlisted_assets_are_merged_without_loss(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    existing = project / "assets" / "student-data.txt"
    existing.parent.mkdir()
    existing.write_text("student asset\n", encoding="utf-8")
    config = project / "teaching" / "config.yml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "    - examples\n", "    - examples\n    - assets\n"
        ),
        encoding="utf-8",
    )

    result = build_documentation(project)

    assert (result.readme.parent / "assets" / "student-data.txt").read_text(
        encoding="utf-8"
    ) == "student asset\n"
    assert (result.readme.parent / "assets" / "lab-diagram.svg").is_file()


def test_asset_collision_rolls_back_the_previous_starter(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    previous = build_documentation(project)
    previous_readme = previous.readme.read_bytes()
    previous_index = previous.generated_source.joinpath("exercises.json").read_bytes()
    existing = project / "assets" / "lab-diagram.svg"
    existing.parent.mkdir()
    existing.write_text("<svg>different</svg>\n", encoding="utf-8")
    config = project / "teaching" / "config.yml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "    - examples\n", "    - examples\n    - assets\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(DocumentationError) as error:
        build_documentation(project)

    assert error.value.code == "artifact_collision"
    assert previous.readme.read_bytes() == previous_readme
    assert previous.generated_source.joinpath("exercises.json").read_bytes() == previous_index


def test_background_and_starter_links_are_rewritten_per_output(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    background = project / "teaching" / "background" / "theory.md"
    background.write_text("# Theory\n\nBackground material.\n", encoding="utf-8")
    data = project / "teaching" / "background" / "data.csv"
    data.write_text("x,y\n1,2\n", encoding="utf-8")
    source = project / "teaching" / "project.md"
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\n[Theory](background/theory.md)\n"
        + "[Data](background/data.csv?download=1)\n"
        + "[Implementation](../src/lab_project/dynamics/unicycle.py)\n",
        encoding="utf-8",
    )

    result = build_documentation(project)
    readme = result.readme.read_text(encoding="utf-8")
    site = (result.site / "index.html").read_text(encoding="utf-8")
    assert "[Theory](background/theory.md)" in readme
    assert 'href="background/theory/">Theory</a>' in site
    assert "[Data](background/data.csv?download=1)" in readme
    assert 'href="background/data.csv?download=1">Data</a>' in site
    assert "[Implementation](src/lab_project/dynamics/unicycle.py)" in readme
    assert (
        'href="source/src/lab_project/dynamics/unicycle.py">Implementation</a>'
        in site
    )
    assert (result.readme.parent / "background" / "theory.md").is_file()
    assert (result.site / "background" / "theory" / "index.html").is_file()


def test_unsupported_admonition_and_unclosed_inline_math_are_rejected(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    source = project / "teaching" / "project.md"
    source.write_text("# Page\n\n!!! danger\n    Do not use this.\n", encoding="utf-8")
    with pytest.raises(DocumentationError) as admonition:
        build_documentation(project)
    assert admonition.value.diagnostics[0].code == "unsupported_markdown"

    source.write_text("# Page\n\nUnclosed $x.\n", encoding="utf-8")
    with pytest.raises(DocumentationError) as math:
        build_documentation(project)
    assert math.value.diagnostics[0].code == "unclosed_math"
    assert math.value.diagnostics[0].line == 3


def test_mailto_is_not_sent_to_http_external_checker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = copy_fixture(tmp_path)
    source = project / "teaching" / "project.md"
    source.write_text("# Page\n\n[Contact](mailto:teacher@example.com)\n", encoding="utf-8")
    monkeypatch.setattr(
        documentation,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("mailto should not be checked as HTTP"),
    )

    build_documentation(project, check_external_links=True)


def test_external_link_checker_retries_get_when_head_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = copy_fixture(tmp_path)
    source = project / "teaching" / "project.md"
    source.write_text("# Page\n\n[Docs](https://example.com/download)\n", encoding="utf-8")
    methods: list[str] = []

    class Response:
        def __init__(self, status: int) -> None:
            self.status = status

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(request: object, *, timeout: int) -> Response:
        del timeout
        method = request.get_method()  # type: ignore[attr-defined]
        methods.append(method)
        return Response(405 if method == "HEAD" else 200)

    monkeypatch.setattr(documentation, "urlopen", fake_urlopen)

    build_documentation(project, check_external_links=True)

    assert methods == ["HEAD", "GET"]


def test_documentation_transaction_keeps_shared_lock_while_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = copy_fixture(tmp_path)
    active = False
    observed: list[str] = []

    @contextmanager
    def fake_lock(root: Path):
        nonlocal active
        del root
        active = True
        observed.append("enter")
        try:
            yield
        finally:
            active = False
            observed.append("exit")

    real_build = documentation.build_project_locked

    def fake_build(root: Path):
        assert active
        observed.append("build")
        return real_build(root)

    real_replace = documentation._replace_directory

    def checked_replace(path: Path, writer: object) -> None:
        assert active
        observed.append(f"replace:{path.name}")
        real_replace(path, writer)  # type: ignore[arg-type]

    monkeypatch.setattr(documentation, "project_build_lock", fake_lock)
    monkeypatch.setattr(documentation, "build_project_locked", fake_build)
    monkeypatch.setattr(documentation, "_replace_directory", checked_replace)

    build_documentation(project)

    assert observed[0] == "enter"
    assert observed[-1] == "exit"
    assert "build" in observed
    assert "replace:docs-src" in observed
    assert "replace:site" in observed


def test_inline_code_formatting_stays_literal_in_site_preview(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    (project / "teaching" / "project.md").write_text(
        "# Page\n\nUse `**literal**` and $x$.\n", encoding="utf-8"
    )

    result = build_documentation(project)
    site_html = (result.site / "index.html").read_text(encoding="utf-8")
    assert "<code>**literal**</code>" in site_html
    assert "<code><strong>literal</strong></code>" not in site_html


def test_failed_directory_promotion_restores_previous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "site"
    destination.mkdir()
    (destination / "old.txt").write_text("old\n", encoding="utf-8")
    real_rename = Path.rename

    def fail_new_promotion(self: Path, target: str | Path) -> Path:
        if self.name.startswith(".site-") and Path(target) == destination:
            raise OSError("simulated promotion failure")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_new_promotion)
    with pytest.raises(OSError):
        documentation._replace_directory(
            destination,
            lambda path: (path / "new.txt").write_text("new\n", encoding="utf-8"),
        )

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old\n"
    assert not (destination / "new.txt").exists()


def test_docs_cli_reports_assembly_failures_as_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = copy_fixture(tmp_path)
    (project / "src" / "lab_project" / "dynamics" / "unicycle.py").write_text(
        "class (\n", encoding="utf-8"
    )

    assert main(["docs", "--root", str(project), "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["built"] is False
    assert payload["code"] == "source_parse_error"


def test_directives_inside_fences_and_math_are_literal(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    (project / "teaching" / "project.md").write_text(
        """# Protected examples

`{{ exercise(\"unknown\") }}`

$$
{{ exercise("unknown") }}
$$

```python
{{ exercise("unknown") }}
```
""",
        encoding="utf-8",
    )

    result = build_documentation(project)
    readme = result.readme.read_text(encoding="utf-8")
    assert readme.count('{{ exercise("unknown") }}') == 3


def test_unknown_directive_reports_authoring_file_and_line(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    (project / "teaching" / "project.md").write_text(
        '# Title\n\n{{ exercise("not-declared") }}\n', encoding="utf-8"
    )

    with pytest.raises(DocumentationError) as error:
        build_documentation(project)

    assert error.value.code == "unknown_exercise_id"
    diagnostic = error.value.diagnostics[0]
    assert diagnostic.file == "teaching/project.md"
    assert diagnostic.line == 3
    assert diagnostic.exercise_id == "not-declared"


def test_malformed_directive_and_unsupported_extension_are_rejected(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    (project / "teaching" / "project.md").write_text(
        "# Title\n\nText {{ exercise(\"unicycle-dynamics\") }}\n", encoding="utf-8"
    )
    with pytest.raises(DocumentationError) as malformed:
        build_documentation(project)
    assert malformed.value.diagnostics[0].code == "malformed_directive"
    assert malformed.value.diagnostics[0].line == 3

    (project / "teaching" / "project.md").write_text(
        "# Title\n\n::: note\nText\n", encoding="utf-8"
    )
    with pytest.raises(DocumentationError) as unsupported:
        build_documentation(project)
    assert unsupported.value.diagnostics[0].code == "unsupported_markdown"


@pytest.mark.parametrize(
    ("markdown", "code"),
    [
        ("![Missing](assets/missing.png)\n", "missing_asset"),
        ("[Missing](#not-a-heading)\n", "broken_anchor"),
    ],
)
def test_local_references_fail_offline(tmp_path: Path, markdown: str, code: str) -> None:
    project = copy_fixture(tmp_path)
    (project / "teaching" / "project.md").write_text(markdown, encoding="utf-8")

    with pytest.raises(DocumentationError) as error:
        build_documentation(project)

    assert error.value.diagnostics[0].code == code
    assert error.value.diagnostics[0].file == "teaching/project.md"


def test_layout_template_uses_strict_undefined_variables(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    (project / "teaching" / "templates" / "README.md.j2").write_text(
        "{{ project.not_declared }}\n", encoding="utf-8"
    )

    with pytest.raises(DocumentationError) as error:
        build_documentation(project)

    assert error.value.code == "template_render_failed"


def test_docs_cli_emits_machine_readable_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = copy_fixture(tmp_path)

    assert main(["docs", "--root", str(project), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["site"].endswith("build/site")
    assert payload["exercise_index"][0]["source"]["symbol"] == "UnicycleDynamics.f"
