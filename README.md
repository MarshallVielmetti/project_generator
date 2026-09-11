# DASC Lab Starter Project

This repository contains the maintainer-side tooling for producing validated
teaching starter projects from a completed canonical project.

## Validate authoring contracts

Milestone 1 provides the installable `startergen` validator. From the project
root, run:

```bash
uv run --project tools/startergen startergen validate --root .
```

The command reads `teaching/config.yml` and `teaching/exercises.yml` without
changing the project. It exits `0` for valid input, `1` for a validation
failure, and `2` for command-line misuse. Add `--json` for structured
diagnostics in CI. The package has its own locked environment in
`tools/startergen/uv.lock`.

Design decisions for issue #1 are recorded in
[`IMPLEMENATION/ISSUE_1_DESIGN.md`](IMPLEMENATION/ISSUE_1_DESIGN.md).

## Assemble the starter artifact

After the contracts validate, assemble the configured student-facing tree:

```bash
uv run --project tools/startergen startergen build --root .
```

The build uses only the explicit starter allowlist, applies deny rules for
repository metadata, caches, tooling, credentials, and instructor material,
then transforms exercise targets in a locked staging tree before promoting
the result below `build/`. The previous successful artifact is preserved when
assembly fails. Output hashes, modes, and relative input provenance are
recorded in `build/starter/.startergen/manifest.json`.

Design decisions for issue #3 are recorded in
[`IMPLEMENATION/ISSUE_3_DESIGN.md`](IMPLEMENATION/ISSUE_3_DESIGN.md).
