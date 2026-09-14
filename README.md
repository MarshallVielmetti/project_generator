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

## Generate teaching documentation

Generate the student README, final-source exercise index, and MkDocs-compatible
site outputs after the starter artifact is built:

```bash
uv run --project tools/startergen startergen docs --root .
```

The command validates exercise directives, local links, anchors, and assets
offline. Use `--check-external-links` only when public HTTP(S) link checks are
intended. The generated source index records inclusive line ranges from the
serialized transformed files, and published exercise links use the configured
release identifier rather than a moving branch name.

Design decisions for issue #4 are recorded in
[`IMPLEMENATION/ISSUE_4_DESIGN.md`](IMPLEMENATION/ISSUE_4_DESIGN.md).

## Run the integrated MVP check

Milestone 5 runs canonical private/public/smoke suites, builds the starter and
documentation outputs, installs the generated package in a temporary isolated
environment, checks exact exercise baseline outcomes and dependency-ordered
restoration checkpoints, and compares two independent builds:

```bash
uv run --project tools/startergen startergen check \
  --root tools/startergen/tests/fixtures/mvp_canonical_project --json
```

The representative three-exercise canonical project and its design decisions
are documented in
[`IMPLEMENATION/ISSUE_5_DESIGN.md`](IMPLEMENATION/ISSUE_5_DESIGN.md).

## Publish a release

After the integrated check passes, create a reviewable release plan without
changing deployment targets:

```bash
uv run --project tools/startergen startergen release \
  --root tools/startergen/tests/fixtures/mvp_canonical_project \
  --dry-run --json
```

The transaction is written to
`build/release/<release_id>/transaction.json`. A real release requires a clean
checkout of the configured starter repository and a documentation deployment
directory:

```bash
uv run --project tools/startergen startergen release \
  --root . \
  --starter-worktree /path/to/starter-checkout \
  --docs-root /path/to/versioned-docs \
  --json
```

The command verifies the pushed target branch and immutable release tag, writes
`releases/<release_id>` and `latest` documentation trees atomically, and records
independent retry state. The manual `Publish starter release` workflow supplies
the target checkout token and deploys the versioned site through GitHub Pages.
Configure the `STARTER_REPOSITORY` repository variable, keep `STARTER_BRANCH`
aligned with `publication.branch`, and store a narrowly scoped
`STARTER_REPO_TOKEN` in the `starter-release` environment before dispatching it.

Design decisions for issue #6 are recorded in
[`IMPLEMENATION/ISSUE_6_DESIGN.md`](IMPLEMENATION/ISSUE_6_DESIGN.md).

## Reuse across a second canonical project

Milestone 7 verifies that the same installed generator supports the distinct
`minimal_completed_project` and `mvp_canonical_project` canonical fixtures.
Each project supplies its own source tree, teaching metadata, exercise tests,
packaging inputs, documentation, and publication settings; the generator has
no project-specific branches. Run the full reusable-project check with:

```bash
uv run --project tools/startergen startergen check \
  --root tools/startergen/tests/fixtures/minimal_completed_project --json
uv run --project tools/startergen startergen check \
  --root tools/startergen/tests/fixtures/mvp_canonical_project --json
```

The design decisions for issue #7 are recorded in
[`IMPLEMENATION/ISSUE_7_DESIGN.md`](IMPLEMENATION/ISSUE_7_DESIGN.md).
