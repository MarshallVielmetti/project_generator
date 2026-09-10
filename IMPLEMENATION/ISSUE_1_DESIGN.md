# Issue #1 design decisions

This folder records the design decisions for the first implementation
milestone, **Contracts and packaging**. The folder name intentionally follows
the requested repository spelling (`IMPLEMENATION`).

## Scope

The implementation is limited to static authoring validation. It does not
generate a starter tree, import or execute canonical code, rewrite source, or
create build outputs. Those behaviors belong to later milestones and should
consume the contracts established here.

## Contract decisions

1. `teaching/config.yml` and `teaching/exercises.yml` are the version-1 input
   documents. Both require `schema_version: 1`.
2. Pydantic models use strict types and reject unknown fields. This keeps
   misspelled metadata from silently changing a build.
3. YAML is loaded with a safe loader that rejects duplicate mapping keys before
   model validation. A duplicate key is an authoring error, not an override.
4. Paths are literal project-relative paths. Absolute paths, `..`, `.`
   components, Windows drive paths, backslashes, symlinks, and special files
   are rejected. This makes validation portable and prevents an allowlist from
   escaping the project root.
5. Generated destinations must be distinct child directories of `build/`.
   Case-insensitive collisions and source/output overlap are errors before any
   future build step can mutate the filesystem.
6. Diagnostics have stable machine-readable codes plus human-readable
   suggestions. CLI exit code `0` means valid, `1` means validation failure,
   and `2` means command-line misuse.

## Why this boundary

Keeping the validator read-only makes it safe to run in CI and gives later
milestones a trustworthy precondition. The minimal fixture exercises the
complete cross-file contract: package source, public and smoke tests,
documentation inputs, output destinations, and one baseline exercise.
