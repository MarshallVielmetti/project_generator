# Issue #5 design decisions

This document records the design choices for **Milestone 5: Integrated MVP and
isolated validation**. The checker composes the validated contracts,
deterministic assembly, source transformation, and documentation stages from
milestones 1–4.

## Representative canonical project

The fixture at
`tools/startergen/tests/fixtures/mvp_canonical_project/` is deliberately
small but exercises the contract boundaries that a real project needs:

- `mean` is a top-level function with a `NotImplementedError` starter;
- `Integrator.step` is a documented class method with a
  `NotImplementedError` starter; and
- `rollout` depends on `Integrator.step` and uses the custom `return initial`
  scaffold, producing a precise assertion failure until it is restored.

The two simulation exercises share one source file. The private, public, and
smoke suites live in the canonical fixture, while only public and smoke tests
are present in the generated starter allowlist.

## `startergen check`

`startergen check` is the integrated command. It first runs the canonical
private/public/smoke suites, then builds the starter and documentation outputs.
It creates a temporary virtual environment, installs the generated package and
pytest into that environment, and verifies an import without `PYTHONPATH`.
Scenario tests subsequently run with `PYTHONPATH` pointed only at copied
starter sources so body-restoration checkpoints cannot accidentally import the
canonical checkout.

The command reports stable JSON when `--json` is supplied. A nonzero exit code
means a validation, installation, test-selection, baseline, checkpoint,
timeout, or reproducibility invariant failed.

## Baseline and dependency checkpoints

Each public test must have exactly one matching baseline record. The initial
starter must fail with the declared reason: `stub_error` requires
`NotImplementedError`, while `assertion_failure` requires the exact configured
assertion message. Passes, skips, collection/setup errors, crashes, and other
failure reasons are rejected.

Exercises are restored in a topological order derived from `requires`. For a
shared source file, the checker copies the canonical file and reapplies starter
transformations only to exercises that remain unrecovered. This makes each
checkpoint observe the real multi-target transformation boundary. Restored
exercise tests must pass, unrecovered tests must retain their declared
baseline outcome, and smoke tests must pass at every checkpoint. A final copy
with every body restored must pass the complete public suite.

## Reproducibility and CI

Two independent copies of the canonical inputs are built from scratch. The
complete `build/starter`, `build/docs-src`, and `build/site` file trees are
compared byte-for-byte; transient locks and temporary directories are not part
of the comparison.

`.github/workflows/ci.yml` runs generator tests, canonical suites, the
integrated check, a passing starter smoke job, and an explicitly expected
failure job for the incomplete public exercise suite. All jobs grant only
read access to repository contents and receive no publication credentials.

## Deliberate non-goals

- The checker does not publish a release or mutate a remote starter repository;
  those operations belong to milestone 6.
- Public tests are selected by the exercise metadata rather than inferred from
  arbitrary repository tests, while canonical suites use the conventional
  `tests/private`, `tests/public`, and `tests/smoke` directories.
- Runtime dependencies are installed by `uv` when available and by the venv's
  `pip` otherwise; the generated artifact itself is installed with
  dependencies disabled because dependency publication is a later milestone.
