"""Safe, format-preserving source transformation for exercise targets.

The transformer operates on concrete syntax trees and never imports or
executes the canonical module. It resolves only lexical top-level functions
and methods directly owned by lexical classes, then replaces the selected
function suite while leaving the surrounding source tree untouched.
"""

from __future__ import annotations

import io
import re
import textwrap
import tokenize
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import libcst as cst
from libcst import metadata

from startergen.paths import is_safe_relative_path

DocstringPolicy = Literal["preserve", "drop"]


@dataclass(frozen=True, slots=True)
class TransformTarget:
    """Description of one exercise body replacement in a source file."""

    exercise_id: str
    symbol: str
    strategy: Literal["replace_body"] = "replace_body"
    docstring: DocstringPolicy = "preserve"
    body: str | None = None


@dataclass(frozen=True, slots=True)
class SourceRange:
    """Final inclusive line range and zero-based columns for a target."""

    start_line: int
    start_column: int
    end_line: int
    end_column: int


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """A target resolved to a concrete function and source range."""

    target: TransformTarget
    qualified_name: str
    source_range: SourceRange


@dataclass(frozen=True, slots=True)
class TransformResult:
    """Result of transforming one source file."""

    path: Path
    original: bytes
    transformed: bytes
    targets: tuple[ResolvedTarget, ...]

    @property
    def changed(self) -> bool:
        return self.original != self.transformed


class TransformError(ValueError):
    """Actionable error raised when a target cannot be transformed."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        target: TransformTarget | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.target = target


@dataclass(frozen=True, slots=True)
class _Definition:
    qualified_name: str
    node: cst.FunctionDef | cst.ClassDef
    kind: Literal["function", "class"]


@dataclass(frozen=True, slots=True)
class _ScannedDefinition:
    qualified_name: str
    node: cst.FunctionDef
    function_depth: int
    conditional_depth: int


def _symbol_parts(symbol: str, target: TransformTarget) -> tuple[str, ...]:
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", symbol):
        raise TransformError(
            "invalid_symbol",
            f"target symbol must be a dotted Python name: {symbol!r}",
            target=target,
        )
    return tuple(symbol.split("."))


def _suite_body(
    suite: cst.BaseSuite,
) -> tuple[cst.BaseSmallStatement | cst.BaseStatement, ...]:
    """Return the statements in either an indented or one-line suite."""

    return tuple(suite.body)


def _direct_definitions(
    body: tuple[cst.CSTNode, ...],
    prefix: tuple[str, ...] = (),
) -> list[_Definition]:
    definitions: list[_Definition] = []
    for statement in body:
        if isinstance(statement, cst.FunctionDef):
            definitions.append(
                _Definition(
                    ".".join(prefix + (statement.name.value,)), statement, "function"
                )
            )
        elif isinstance(statement, cst.ClassDef):
            definitions.append(
                _Definition(
                    ".".join(prefix + (statement.name.value,)), statement, "class"
                )
            )
            nested_body = _suite_body(statement.body)
            definitions.extend(
                _direct_definitions(nested_body, prefix + (statement.name.value,))
            )
    return definitions


class _DefinitionScanner(cst.CSTVisitor):
    """Find definitions hidden inside unsupported conditional/local scopes."""

    _conditional_nodes = (
        cst.If,
        cst.For,
        cst.While,
        cst.Try,
        cst.With,
        cst.Match,
    )

    def __init__(self) -> None:
        self.scope_stack: list[str] = []
        self.function_depth = 0
        self.conditional_depth = 0
        self.definitions: list[_ScannedDefinition] = []

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self.scope_stack.append(node.name.value)

    def leave_ClassDef(self, original_node: cst.ClassDef) -> None:
        self.scope_stack.pop()

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        prefix = ".".join(self.scope_stack)
        qualified_name = f"{prefix}.{node.name.value}" if prefix else node.name.value
        self.definitions.append(
            _ScannedDefinition(
                qualified_name,
                node,
                self.function_depth,
                self.conditional_depth,
            )
        )
        self.scope_stack.append(node.name.value)
        self.function_depth += 1

    def leave_FunctionDef(self, original_node: cst.FunctionDef) -> None:
        self.scope_stack.pop()
        self.function_depth -= 1

    def _enter_conditional(self) -> None:
        self.conditional_depth += 1

    def _leave_conditional(self) -> None:
        self.conditional_depth -= 1

    def visit_If(self, node: cst.If) -> None:
        self._enter_conditional()

    def leave_If(self, original_node: cst.If) -> None:
        self._leave_conditional()

    def visit_For(self, node: cst.For) -> None:
        self._enter_conditional()

    def leave_For(self, original_node: cst.For) -> None:
        self._leave_conditional()

    def visit_While(self, node: cst.While) -> None:
        self._enter_conditional()

    def leave_While(self, original_node: cst.While) -> None:
        self._leave_conditional()

    def visit_Try(self, node: cst.Try) -> None:
        self._enter_conditional()

    def leave_Try(self, original_node: cst.Try) -> None:
        self._leave_conditional()

    def visit_With(self, node: cst.With) -> None:
        self._enter_conditional()

    def leave_With(self, original_node: cst.With) -> None:
        self._leave_conditional()

    def visit_Match(self, node: cst.Match) -> None:
        self._enter_conditional()

    def leave_Match(self, original_node: cst.Match) -> None:
        self._leave_conditional()


def _decorator_name(decorator: cst.Decorator) -> str | None:
    expression = decorator.decorator
    parts: list[str] = []
    while isinstance(expression, cst.Attribute):
        parts.append(expression.attr.value)
        expression = expression.value
    if isinstance(expression, cst.Name):
        parts.append(expression.value)
        return ".".join(reversed(parts))
    return None


def _unsupported_decorator(node: cst.FunctionDef) -> str | None:
    for decorator in node.decorators:
        name = _decorator_name(decorator)
        if name == "overload" or name == "typing.overload" or name == "overload":
            return "overload groups are not transformable"
        if name in {"property", "cached_property"} or (
            name is not None
            and name.rsplit(".", 1)[-1] in {"setter", "getter", "deleter"}
        ):
            return "property accessor groups are not transformable"
    return None


class _YieldFinder(cst.CSTVisitor):
    """Detect yields in the selected function while ignoring nested functions."""

    def __init__(self) -> None:
        self.found = False

    def visit_Yield(self, node: cst.Yield) -> None:
        self.found = True

    def visit_YieldFrom(self, node: cst.YieldFrom) -> None:
        self.found = True

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        return False

    def visit_AsyncFunctionDef(self, node: cst.AsyncFunctionDef) -> bool:
        return False


def _contains_yield(node: cst.FunctionDef) -> bool:
    finder = _YieldFinder()
    for statement in _suite_body(node.body):
        statement.visit(finder)
    return finder.found


def _position_range(
    positions: dict[cst.CSTNode, metadata.Position], node: cst.CSTNode
) -> SourceRange:
    position = positions[node]
    end_line = position.end.line
    if position.end.column == 0 and end_line > position.start.line:
        end_line -= 1
    return SourceRange(
        start_line=position.start.line,
        start_column=position.start.column,
        end_line=end_line,
        end_column=position.end.column,
    )


def _resolve_nodes(
    module: cst.Module,
    targets: tuple[TransformTarget, ...],
    *,
    positions: dict[cst.CSTNode, metadata.Position],
) -> dict[int, ResolvedTarget]:
    direct = _direct_definitions(tuple(module.body))
    by_name: defaultdict[str, list[_Definition]] = defaultdict(list)
    for definition in direct:
        by_name[definition.qualified_name].append(definition)

    scanner = _DefinitionScanner()
    module.visit(scanner)
    resolved: dict[int, ResolvedTarget] = {}
    symbols_seen: set[str] = set()
    for target in targets:
        _symbol_parts(target.symbol, target)
        if target.strategy != "replace_body":
            raise TransformError(
                "unsupported_strategy",
                f"unsupported transformation strategy {target.strategy!r}",
                target=target,
            )
        if target.symbol in symbols_seen:
            raise TransformError(
                "duplicate_target",
                f"target {target.symbol!r} is listed more than once in the same file",
                target=target,
            )
        symbols_seen.add(target.symbol)
        candidates = by_name.get(target.symbol, [])
        if len(candidates) > 1:
            raise TransformError(
                "duplicate_definition",
                f"target {target.symbol!r} has {len(candidates)} lexical definitions",
                target=target,
            )
        if not candidates:
            hidden = [
                item
                for item in scanner.definitions
                if item.qualified_name == target.symbol
            ]
            if any(item.function_depth > 0 for item in hidden):
                code = "local_target"
                message = f"target {target.symbol!r} is defined inside another function"
            elif any(item.conditional_depth > 0 for item in hidden):
                code = "conditional_definition"
                message = f"target {target.symbol!r} is defined inside a conditional or compound block"
            elif hidden:
                code = "unsupported_target_scope"
                message = (
                    f"target {target.symbol!r} is not a direct module/class definition"
                )
            else:
                code = "target_not_found"
                message = f"could not resolve lexical target {target.symbol!r}"
            raise TransformError(code, message, target=target)
        definition = candidates[0]
        if definition.kind != "function":
            raise TransformError(
                "target_not_function",
                f"target {target.symbol!r} resolves to a class, not a function or method",
                target=target,
            )
        function = definition.node
        decorator_error = _unsupported_decorator(function)
        if decorator_error:
            code = (
                "unsupported_overload"
                if "overload" in decorator_error
                else "unsupported_property"
            )
            raise TransformError(
                code,
                f"target {target.symbol!r}: {decorator_error}",
                target=target,
            )
        if _contains_yield(function):
            raise TransformError(
                "unsupported_generator",
                f"target {target.symbol!r} is a generator or async generator",
                target=target,
            )
        result = ResolvedTarget(
            target=target,
            qualified_name=definition.qualified_name,
            source_range=_position_range(positions, function),
        )
        node_key = id(function)
        if node_key in resolved:
            raise TransformError(
                "overlapping_target",
                f"multiple exercises resolve to target {target.symbol!r}",
                target=target,
            )
        resolved[node_key] = result
    return resolved


def resolve_targets(
    source: str, targets: list[TransformTarget] | tuple[TransformTarget, ...]
) -> tuple[ResolvedTarget, ...]:
    """Resolve targets in source and return their current source positions."""

    target_tuple = tuple(targets)
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        raise TransformError(
            "source_parse_error", f"could not parse source: {exc}"
        ) from exc
    wrapper = metadata.MetadataWrapper(module, unsafe_skip_copy=True)
    positions = wrapper.resolve(metadata.PositionProvider)
    resolved = _resolve_nodes(module, target_tuple, positions=positions)
    return tuple(resolved.values())


def _is_docstring_statement(statement: cst.CSTNode) -> bool:
    if not isinstance(statement, cst.SimpleStatementLine) or len(statement.body) != 1:
        return False
    small_statement = statement.body[0]
    return isinstance(small_statement, cst.Expr) and isinstance(
        small_statement.value, cst.SimpleString
    )


def _original_docstring(suite: cst.BaseSuite) -> cst.SimpleStatementLine | None:
    body = _suite_body(suite)
    if not body or not _is_docstring_statement(body[0]):
        return None
    first = body[0]
    assert isinstance(first, cst.SimpleStatementLine)
    # Rebuild the line to remove comments/whitespace owned by the solution
    # suite while preserving the exact string literal spelling.
    return cst.SimpleStatementLine(body=[first.body[0]])


def _scaffold_statements(
    body: str | None, *, asynchronous: bool, exercise_id: str
) -> tuple[cst.CSTNode, ...]:
    if body is None:
        statement = cst.parse_statement(
            f'raise NotImplementedError("Exercise {exercise_id} is not implemented")'
        )
        return (statement,)
    if not body.strip():
        raise TransformError(
            "empty_scaffold",
            "custom scaffold body must contain at least one statement",
        )
    indented_body = textwrap.indent(body.rstrip("\n"), "    ")
    prefix = "async " if asynchronous else ""
    wrapper = cst.parse_module(
        f"{prefix}def __startergen_scaffold__():\n{indented_body}\n"
    )
    function = wrapper.body[0]
    if not isinstance(function, cst.FunctionDef):  # pragma: no cover - parser invariant
        raise TransformError(
            "invalid_scaffold", "could not parse custom scaffold function"
        )
    statements = _suite_body(function.body)
    if statements and _is_docstring_statement(statements[0]):
        raise TransformError(
            "scaffold_docstring",
            "custom scaffold must not start with a docstring; docstring policy is separate",
        )
    return statements


def _replacement_suite(
    original: cst.FunctionDef,
    target: TransformTarget,
) -> cst.IndentedBlock:
    try:
        statements = list(
            _scaffold_statements(
                target.body,
                asynchronous=original.asynchronous is not None,
                exercise_id=target.exercise_id,
            )
        )
    except cst.ParserSyntaxError as exc:
        raise TransformError(
            "invalid_scaffold",
            f"custom scaffold could not be parsed: {exc}",
            target=target,
        ) from exc
    if target.docstring == "preserve":
        docstring = _original_docstring(original.body)
        if docstring is not None:
            statements.insert(0, docstring)
    return cst.IndentedBlock(body=statements)


class _BodyTransformer(cst.CSTTransformer):
    def __init__(self, resolved: dict[int, ResolvedTarget]) -> None:
        self.resolved = resolved

    def leave_FunctionDef(
        self,
        original_node: cst.FunctionDef,
        updated_node: cst.FunctionDef,
    ) -> cst.FunctionDef:
        resolved = self.resolved.get(id(original_node))
        if resolved is None:
            return updated_node
        return updated_node.with_changes(
            body=_replacement_suite(original_node, resolved.target)
        )


def _decode_source(raw: bytes) -> tuple[str, str]:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        return raw.decode(encoding), encoding
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise TransformError(
            "source_decode_error", f"could not decode Python source: {exc}"
        ) from exc


def _encode_source(text: str, encoding: str) -> bytes:
    try:
        return text.encode(encoding)
    except UnicodeEncodeError as exc:
        raise TransformError(
            "source_encode_error",
            f"transformed source cannot be encoded as {encoding}: {exc}",
        ) from exc


def _preserve_newlines(original: str, transformed: str) -> str:
    if "\r\n" not in original:
        return transformed
    return transformed.replace("\r\n", "\n").replace("\n", "\r\n")


def transform_file(
    path: Path,
    targets: list[TransformTarget] | tuple[TransformTarget, ...],
) -> TransformResult:
    """Transform one file in memory; callers decide whether to write it."""

    if not path.exists():
        raise TransformError("missing_source", f"source file does not exist: {path}")
    if path.is_symlink():
        raise TransformError(
            "symlink_source", f"source file must not be a symlink: {path}"
        )
    if not path.is_file():
        raise TransformError(
            "source_not_file", f"source path is not a regular file: {path}"
        )
    original = path.read_bytes()
    target_tuple = tuple(targets)
    if not target_tuple:
        return TransformResult(path, original, original, ())
    source, encoding = _decode_source(original)
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        raise TransformError(
            "source_parse_error", f"could not parse {path}: {exc}"
        ) from exc
    wrapper = metadata.MetadataWrapper(module, unsafe_skip_copy=True)
    positions = wrapper.resolve(metadata.PositionProvider)
    resolved = _resolve_nodes(module, target_tuple, positions=positions)
    transformed_module = wrapper.visit(_BodyTransformer(resolved))
    transformed_source = _preserve_newlines(source, transformed_module.code)
    try:
        cst.parse_module(transformed_source)
    except cst.ParserSyntaxError as exc:  # pragma: no cover - defensive invariant
        raise TransformError(
            "transformed_parse_error",
            f"transformed source for {path} did not reparse: {exc}",
        ) from exc
    transformed = _encode_source(transformed_source, encoding)
    final_targets = resolve_targets(transformed_source, list(target_tuple))
    return TransformResult(path, original, transformed, final_targets)


def transform_sources(
    root: Path,
    targets: list[tuple[str, TransformTarget]]
    | tuple[tuple[str, TransformTarget], ...],
    *,
    write: bool = False,
) -> dict[Path, TransformResult]:
    """Transform grouped project-relative files, optionally writing results.

    ``write=False`` is the default so validation and tests can inspect all
    results before making a filesystem change. Files with no targets are never
    touched, preserving their bytes exactly.
    """

    grouped: defaultdict[str, list[TransformTarget]] = defaultdict(list)
    for relative_path, target in targets:
        grouped[relative_path].append(target)
    results: dict[Path, TransformResult] = {}
    for relative_path, file_targets in grouped.items():
        if not is_safe_relative_path(relative_path):
            raise TransformError(
                "unsafe_path",
                f"source path must be a safe project-relative path: {relative_path!r}",
            )
        path = root.joinpath(*relative_path.split("/"))
        result = transform_file(path, file_targets)
        results[path] = result
    if write:
        for path, result in results.items():
            if result.changed:
                path.write_bytes(result.transformed)
    return results
