# Issue #7 design decisions

This document records the design choices for **Milestone 7: Reuse across a second canonical project**.

## Second-project selection

The existing `tools/startergen/tests/fixtures/minimal_completed_project` fixture is the second canonical project for this milestone.

It is intentionally distinct from `mvp_canonical_project`: it has a different import package, one exercise instead of three, a different source module, a different teaching page and template, a different publication configuration, and a smaller starter allowlist.

The fixture already represented the version-1 authoring contract, so this milestone completes its canonical test and documentation inputs instead of adding a project-specific branch to generator implementation code.

## Canonical test discovery

The second project uses the standard pytest `test_*.py` naming convention for its public and smoke suites.

This keeps `startergen check` provider-neutral and lets both canonical projects use pytest's ordinary directory discovery without project-specific configuration or command-line exceptions.

The exercise metadata points at the renamed public test node, and the starter allowlist continues to include the complete public and smoke directories.

## Cross-project verification

The check test now runs the same installed generator command against both canonical fixtures and asserts each project's dependency order and completed public suite.

The integrated check therefore exercises validation, canonical suites, artifact assembly, documentation generation, isolated package installation, baseline outcomes, restoration checkpoints, completed public tests, and reproducibility for both projects.

No generator module contains a project name, import-package special case, exercise-id special case, or fixture-specific path branch.

## Output and maintenance boundary

Generated starter, documentation, report, cache, and release outputs remain disposable build products and are excluded from the committed fixture inputs.

Adding another canonical project should require only the documented project tree, configuration, exercise metadata, tests, and dependencies, followed by adding its fixture to the cross-project integration matrix.

## Acceptance evidence

The milestone is complete when both fixtures pass `startergen check`, the generator source remains project-agnostic, and the configuration guide identifies the required onboarding inputs and validation command.
