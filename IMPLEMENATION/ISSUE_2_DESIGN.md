# Issue #2 design decisions

This document records the design choices for **Milestone 2: Source
transformation**. The implementation uses LibCST and remains independent of
the canonical module's runtime dependencies.

## Resolution boundary

- A target is identified by the explicit project-relative file path plus an
  exact dotted lexical name such as `step` or `Vehicle.step`.
- Only top-level functions and methods directly owned by lexical classes are
  eligible. Nested classes are supported, so `Outer.Inner.step` is valid.
- Resolution never imports or executes source. Definitions hidden inside
  `if`, `for`, `while`, `try`, `with`, or `match` blocks are rejected instead
  of being guessed at. Functions nested inside another function are rejected
  as local targets.
- Duplicate definitions, overload decorators, property/accessor decorators,
  and generator/async-generator bodies are rejected with stable error codes.

## Transformation boundary

- LibCST replaces only the selected `FunctionDef.body`. Decorators,
  parameters, annotations, names, and surrounding concrete syntax remain in
  the tree untouched.
- The default replacement is a target-attributed `NotImplementedError`.
  Custom scaffolds are parsed inside a synthetic synchronous or asynchronous
  function so `await` and normal suite syntax are checked in the right
  context.
- Docstring handling is independent of the scaffold: `preserve` copies the
  original first string expression exactly once, while `drop` removes it.
  Comments owned by the removed solution suite are not copied.
- The source encoding is detected with Python's tokenizer and restored after
  transformation. CRLF files retain CRLF line endings. Files without targets
  are not read/written by the grouped transformation API.

## Verification boundary

Every transformed module is reparsed before it is returned. Final target
positions are resolved from the serialized output rather than from the input
tree, and line ranges convert LibCST's exclusive end position to an inclusive
range for later exercise links. Golden fixtures cover formatting, multiple
targets, async code, comments, one-line suites, Unicode/CRLF, and rejected
constructs.
