from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from startergen.transform import (
    TransformError,
    TransformTarget,
    resolve_targets,
    transform_file,
    transform_sources,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "source_transform"


def test_golden_fixture_replaces_multiple_targets_only() -> None:
    source_path = FIXTURE_ROOT / "mixed_targets.py"
    expected = (FIXTURE_ROOT / "mixed_targets_expected.py").read_bytes()
    result = transform_file(
        source_path,
        [
            TransformTarget("top-level", "top_level", docstring="preserve"),
            TransformTarget(
                "async-target",
                "Outer.Inner.async_target",
                docstring="preserve",
                body="return value * 2",
            ),
        ],
    )
    assert result.transformed == expected
    assert result.changed
    assert len(result.targets) == 2
    assert result.targets[0].source_range.start_line == 9
    assert result.targets[0].source_range.end_line == 13
    assert b"solution-only comment" not in result.transformed
    compile(result.transformed.decode("utf-8"), str(source_path), "exec")


def test_one_line_suite_is_expanded_without_changing_signature() -> None:
    path_source = (FIXTURE_ROOT / "one_line.py").read_text(encoding="utf-8")
    assert path_source.startswith("def one_line")
    target = TransformTarget("one-line", "one_line", body="return value * 3")
    result = _transform_text(path_source, target)
    assert result.startswith("def one_line(value: int) -> int:\n")
    assert "return value * 3" in result
    assert "value + 1" not in result


def test_docstring_drop_and_custom_async_scaffold() -> None:
    source = """async def compute(value):
    \"\"\"Remove this documentation.\"\"\"
    # solution comment
    return value
"""
    result = _transform_text(
        source,
        TransformTarget(
            "compute", "compute", docstring="drop", body="return await identity(value)"
        ),
    )
    assert '"""Remove this documentation."""' not in result
    assert "solution comment" not in result
    assert "return await identity(value)" in result
    compile(result, "compute.py", "exec")


def test_multistatement_one_line_docstring_is_preserved_once() -> None:
    source = 'def compute(): """Keep this docstring."""; return 1\n'
    result = _transform_text(
        source,
        TransformTarget("compute", "compute", docstring="preserve"),
    )
    assert result.count('"""Keep this docstring."""') == 1
    assert '"""Keep this docstring.""";' not in result
    assert "NotImplementedError" in result
    compile(result, "compute.py", "exec")


def test_latin1_and_crlf_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "latin1.py"
    source = (
        "# -*- coding: latin-1 -*-\r\n"
        "# café remains encoded in the original source\r\n"
        "def greet(name):\r\n"
        '    """Say café."""\r\n'
        "    return name\r\n"
    ).encode("latin-1")
    path.write_bytes(source)
    result = transform_file(
        path,
        [TransformTarget("greeting", "greet", body='return "café"')],
    )
    assert result.transformed.decode("latin-1").count("\r\n") == 5
    assert b"\n" not in result.transformed.replace(b"\r\n", b"")
    assert b"caf\xe9" in result.transformed


def test_transform_sources_rejects_unsafe_relative_paths(tmp_path: Path) -> None:
    with pytest.raises(TransformError) as error:
        transform_sources(
            tmp_path,
            [("../outside.py", TransformTarget("bad", "f"))],
        )
    assert error.value.code == "unsafe_path"


def test_transform_sources_rejects_symlinked_intermediate_component(
    tmp_path: Path,
) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    source = real_directory / "source.py"
    source.write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "alias").symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(TransformError) as error:
        transform_sources(
            tmp_path,
            [("alias/source.py", TransformTarget("bad", "f"))],
        )
    assert error.value.code == "symlink_source"


def test_case_aliases_are_grouped_on_case_insensitive_filesystems(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text(
        "def first():\n    return 1\n\ndef second():\n    return 2\n",
        encoding="utf-8",
    )
    alias = tmp_path / "SOURCE.py"
    if not alias.exists() or not source.samefile(alias):
        pytest.skip("test filesystem is case-sensitive")
    results = transform_sources(
        tmp_path,
        [
            ("source.py", TransformTarget("first", "first")),
            ("SOURCE.py", TransformTarget("second", "second")),
        ],
    )
    assert len(results) == 1
    output = next(iter(results.values())).transformed.decode("utf-8")
    assert output.count("NotImplementedError") == 2


def test_empty_target_list_is_byte_for_byte_noop(tmp_path: Path) -> None:
    path = tmp_path / "untouched.py"
    original = b"# coding: utf-8\r\nvalue = 'caf\xc3\xa9'\r\n"
    path.write_bytes(original)
    result = transform_file(path, [])
    assert result.transformed == original
    assert not result.changed


def test_grouped_api_does_not_touch_unselected_files(tmp_path: Path) -> None:
    selected = tmp_path / "selected.py"
    untouched = tmp_path / "untouched.py"
    selected.write_text("def selected():\n    return 1\n", encoding="utf-8")
    untouched_bytes = b"# keep this exact\r\nvalue = 1\r\n"
    untouched.write_bytes(untouched_bytes)
    results = transform_sources(
        tmp_path,
        [("selected.py", TransformTarget("selected", "selected", body="return 2"))],
        write=True,
    )
    assert results[selected].changed
    assert untouched.read_bytes() == untouched_bytes
    assert "return 2" in selected.read_text(encoding="utf-8")


def _transform_text(source: str, target: TransformTarget) -> str:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "source.py"
        path.write_text(source, encoding="utf-8")
        return transform_file(path, [target]).transformed.decode("utf-8")


def _transform_text_many(source: str, targets: list[TransformTarget]) -> str:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "source.py"
        path.write_text(source, encoding="utf-8")
        return transform_file(path, targets).transformed.decode("utf-8")


@pytest.mark.parametrize(
    ("source", "symbol", "code"),
    [
        (
            "def f():\n    return 1\n\ndef f():\n    return 2\n",
            "f",
            "duplicate_definition",
        ),
        (
            "if enabled:\n    def f():\n        return 1\n",
            "f",
            "conditional_definition",
        ),
        ("def outer():\n    def f():\n        return 1\n", "outer.f", "local_target"),
        ("def f():\n    yield 1\n", "f", "unsupported_generator"),
        ("async def f():\n    yield 1\n", "f", "unsupported_generator"),
        (
            "class C:\n    @property\n    def f(self):\n        return 1\n",
            "C.f",
            "unsupported_property",
        ),
        (
            "@typing_extensions.overload\ndef f(value: int): ...\n",
            "f",
            "unsupported_overload",
        ),
    ],
)
def test_unsupported_targets_have_actionable_codes(
    source: str, symbol: str, code: str
) -> None:
    with pytest.raises(TransformError) as error:
        resolve_targets(source, [TransformTarget("bad-target", symbol)])
    assert error.value.code == code
    assert symbol in str(error.value)


def test_nested_function_yield_does_not_reject_outer_target() -> None:
    source = """def outer():
    def nested():
        yield 1
    return nested
"""
    resolved = resolve_targets(source, [TransformTarget("outer", "outer")])
    assert resolved[0].qualified_name == "outer"


def test_direct_and_conditional_duplicate_is_rejected() -> None:
    source = """def f():
    return 1
if enabled:
    def f():
        return 2
"""
    with pytest.raises(TransformError) as error:
        resolve_targets(source, [TransformTarget("bad", "f")])
    assert error.value.code == "conditional_definition"


def test_unicode_identifier_components_are_resolved() -> None:
    source = """class École:
    def étape(self):
        return 1
"""
    result = _transform_text(
        source,
        TransformTarget("unicode", "École.étape"),
    )
    assert "NotImplementedError" in result


def test_duplicate_target_specs_and_invalid_scaffold_are_rejected() -> None:
    with pytest.raises(TransformError, match="listed more than once"):
        _transform_text_many(
            "def f():\n    return 1\n",
            [TransformTarget("first", "f"), TransformTarget("second", "f")],
        )
    with pytest.raises(TransformError) as error:
        _transform_text(
            "def f():\n    return 1\n",
            TransformTarget("bad", "f", body="if:"),
        )
    assert error.value.code == "invalid_scaffold"

    with pytest.raises(TransformError) as error:
        _transform_text(
            "def f():\n    return 1\n",
            TransformTarget("empty", "f", body=""),
        )
    assert error.value.code == "empty_scaffold"
    assert error.value.target is not None
    assert error.value.target.exercise_id == "empty"


def test_scaffold_docstring_is_rejected_as_a_separate_policy() -> None:
    with pytest.raises(TransformError) as error:
        _transform_text(
            "def f():\n    return 1\n",
            TransformTarget("bad", "f", body='"""not allowed"""\nreturn 1'),
        )
    assert error.value.code == "scaffold_docstring"
    assert error.value.target is not None
    assert error.value.target.exercise_id == "bad"


def test_generated_stub_escapes_exercise_id_before_compiling() -> None:
    exercise_id = 'quote" slash\\ newline\nmarker'
    result = _transform_text(
        "def f():\n    return 1\n",
        TransformTarget(exercise_id, "f"),
    )
    compile(result, "escaped.py", "exec")
    assert 'quote\\" slash' in result


def test_decorator_group_is_specific_before_duplicate_definition() -> None:
    source = """@typing_extensions.overload
def f(value: int): ...
@typing_extensions.overload
def f(value: str): ...
"""
    with pytest.raises(TransformError) as error:
        resolve_targets(source, [TransformTarget("overloaded", "f")])
    assert error.value.code == "unsupported_overload"
