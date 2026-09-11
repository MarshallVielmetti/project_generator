from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
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
    site_markdown = (result.site / "index.md").read_text(encoding="utf-8")
    site_html = (result.site / "index.html").read_text(encoding="utf-8")
    index = json.loads((result.generated_source / "exercises.json").read_text(encoding="utf-8"))

    assert "src/lab_project/dynamics/unicycle.py#L7-L8" in readme
    assert '{{ exercise("unicycle-dynamics") }}' in readme
    assert "> **Note — Learning goal**" in readme
    assert "!!! note" not in readme
    assert (result.readme.parent / "assets" / "lab-diagram.svg").is_file()
    assert "https://example.com/minimal-lab/v1/source/src/lab_project/dynamics/unicycle.py#L7-L8" in site_markdown
    assert "https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-mml-chtml.js" in site_html
    assert '<aside class="admonition note">' in site_html
    assert '<div class="arithmatex">\\[' in site_html
    assert "\\begin{bmatrix}" in site_html
    assert index["exercises"][0]["source"]["start_line"] == 7
    assert index["exercises"][0]["source"]["end_line"] == 8
    manifest = json.loads(
        (result.readme.parent / ".startergen" / "manifest.json").read_text(encoding="utf-8")
    )
    manifest_paths = {entry["path"] for entry in manifest["files"]}
    assert {"README.md", "assets/lab-diagram.svg"} <= manifest_paths


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
