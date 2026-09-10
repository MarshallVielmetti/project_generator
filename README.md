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
