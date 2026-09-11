"""Safe, format-preserving source transformation for exercise targets.

The transformer operates on concrete syntax trees and never imports or
executes the canonical module. It resolves only lexical top-level functions
and methods directly owned by lexical classes, then replaces the selected
function suite while leaving the surrounding source tree untouched.
"""

from __future__ import annotations

import io
import json
import keyword
import os
import stat
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
    parts = tuple(symbol.split("."))
    if not parts or any(
        not part.isidentifier() or keyword.iskeyword(part) for part in parts
    ):
        raise TransformError(
            "invalid_symbol",
            f"target symbol must be a dotted Python name: {symbol!r}",
            target=target,
        )
    return parts


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
        final_component = name.rsplit(".", 1)[-1] if name is not None else None
        if final_component == "overload":
            return "overload groups are not transformable"
        if final_component in {
            "property",
            "cached_property",
            "setter",
            "getter",
            "deleter",
        }:
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
        scanned_matches = [
            item for item in scanner.definitions if item.qualified_name == target.symbol
        ]
        candidate_functions = [
            item.node for item in candidates if item.kind == "function"
        ]
        all_matching_functions = candidate_functions + [
            item.node
            for item in scanned_matches
            if all(item.node is not candidate for candidate in candidate_functions)
        ]
        decorator_error = next(
            (
                error
                for function in all_matching_functions
                if (error := _unsupported_decorator(function)) is not None
            ),
            None,
        )
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
        if len(candidates) > 1:
            raise TransformError(
                "duplicate_definition",
                f"target {target.symbol!r} has {len(candidates)} lexical definitions",
                target=target,
            )
        if len(scanned_matches) > 1:
            if any(item.conditional_depth > 0 for item in scanned_matches):
                raise TransformError(
                    "conditional_definition",
                    f"target {target.symbol!r} has a definition inside a conditional or compound block",
                    target=target,
                )
            if any(item.function_depth > 0 for item in scanned_matches):
                raise TransformError(
                    "local_target",
                    f"target {target.symbol!r} has a definition inside another function",
                    target=target,
                )
            raise TransformError(
                "duplicate_definition",
                f"target {target.symbol!r} has {len(scanned_matches)} lexical definitions",
                target=target,
            )
        if not candidates:
            if any(item.function_depth > 0 for item in scanned_matches):
                code = "local_target"
                message = f"target {target.symbol!r} is defined inside another function"
            elif any(item.conditional_depth > 0 for item in scanned_matches):
                code = "conditional_definition"
                message = f"target {target.symbol!r} is defined inside a conditional or compound block"
            elif scanned_matches:
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


def _docstring_expression(statement: cst.CSTNode) -> cst.Expr | None:
    if isinstance(statement, cst.Expr):
        expression = statement
    elif isinstance(statement, (cst.SimpleStatementSuite, cst.SimpleStatementLine)):
        body = statement.body
        if not body or not isinstance(body[0], cst.Expr):
            return None
        expression = body[0]
    else:
        return None
    if not isinstance(expression.value, cst.SimpleString):
        return None
    return expression


def _is_docstring_statement(statement: cst.CSTNode) -> bool:
    return _docstring_expression(statement) is not None


def _original_docstring(suite: cst.BaseSuite) -> cst.SimpleStatementLine | None:
    body = _suite_body(suite)
    if not body or not _is_docstring_statement(body[0]):
        return None
    expression = _docstring_expression(body[0])
    assert expression is not None
    # Rebuild the line to remove comments/whitespace owned by the solution
    # suite while preserving the exact string literal spelling.
    return cst.SimpleStatementLine(
        body=[expression.with_changes(semicolon=cst.MaybeSentinel.DEFAULT)]
    )


def _scaffold_statements(
    body: str | None, *, asynchronous: bool, target: TransformTarget
) -> tuple[cst.CSTNode, ...]:
    if body is None:
        message_literal = json.dumps(
            f"Exercise {target.exercise_id} is not implemented",
            ensure_ascii=True,
        )
        statement = cst.parse_statement(f"raise NotImplementedError({message_literal})")
        return (statement,)
    if not body.strip():
        raise TransformError(
            "empty_scaffold",
            "custom scaffold body must contain at least one statement",
            target=target,
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
            target=target,
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
                target=target,
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
    try:
        source, encoding = _decode_source(original)
    except TransformError as exc:
        if not target_tuple or exc.target is not None:
            raise
        raise TransformError(
            exc.code,
            str(exc),
            target=target_tuple[0],
        ) from exc
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        raise TransformError(
            "source_parse_error",
            f"could not parse {path}: {exc}",
            target=target_tuple[0] if target_tuple else None,
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
    try:
        compile(transformed_source, str(path), "exec", dont_inherit=True)
    except SyntaxError as exc:  # pragma: no cover - defensive invariant
        raise TransformError(
            "transformed_compile_error",
            f"transformed source for {path} did not compile: {exc}",
        ) from exc
    transformed = _encode_source(transformed_source, encoding)
    final_targets = resolve_targets(transformed_source, list(target_tuple))
    return TransformResult(path, original, transformed, final_targets)


def _safe_source_path(root: Path, relative_path: str) -> Path:
    """Resolve a project-relative source while inspecting every component."""

    path = root.joinpath(*relative_path.split("/"))
    current = root
    parts = relative_path.split("/")
    for index, part in enumerate(parts):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise TransformError(
                "symlink_source",
                f"source path must not contain a symlink component: {current}",
            )
        is_final = index == len(parts) - 1
        if not is_final and not stat.S_ISDIR(info.st_mode):
            raise TransformError(
                "source_not_directory",
                f"source path component is not a directory: {current}",
            )
        if is_final and not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise TransformError(
                "special_source",
                f"source path is not an ordinary file or directory: {current}",
            )
    return path


def _path_group_key(root: Path, relative_path: str) -> tuple[str, ...]:
    """Group aliases according to the actual filesystem and platform rules."""

    path = root.joinpath(*relative_path.split("/"))
    try:
        info = path.stat()
    except OSError:
        normalized = os.path.normcase(os.fspath(path))
        return ("path", normalized)
    return ("file", str(info.st_dev), str(info.st_ino))


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

    grouped: dict[tuple[str, ...], tuple[str, list[TransformTarget]]] = {}
    for relative_path, target in targets:
        if not is_safe_relative_path(relative_path):
            raise TransformError(
                "unsafe_path",
                f"source path must be a safe project-relative path: {relative_path!r}",
                target=target,
            )
        _safe_source_path(root, relative_path)
        key = _path_group_key(root, relative_path)
        if key not in grouped:
            grouped[key] = (relative_path, [])
        grouped[key][1].append(target)
    results: dict[Path, TransformResult] = {}
    for relative_path, file_targets in grouped.values():
        path = _safe_source_path(root, relative_path)
        result = transform_file(path, file_targets)
        results[path] = result
    if write:
        for path, result in results.items():
            if result.changed:
                path.write_bytes(result.transformed)
    return results
