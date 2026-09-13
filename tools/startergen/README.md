# startergen

`startergen` is the maintainer-side tool for the teaching starter-project
pipeline. Milestone 1 establishes the version-1 authoring contracts and the
path policy used by later build stages.

Run it from the canonical project root:

```bash
uv run --project tools/startergen startergen validate --root .
```

The validator reads `teaching/config.yml` and `teaching/exercises.yml`. A
successful validation prints a concise summary and exits `0`. Invalid
authoring input exits `1`; malformed CLI usage exits `2`. Use `--json` in CI
to receive a machine-readable report containing stable diagnostic codes.

## Assemble a starter artifact

After validation, build the configured starter tree with:

```bash
uv run --project tools/startergen startergen build --root .
```

The build copies only `starter.include` entries, applies the built-in deny
rules and `starter.exclude` paths, transforms declared exercise targets, and
promotes the result below `build/` under an exclusive lock. A failed build
leaves the previous successful output unchanged. The generated
`build/starter/.startergen/manifest.json` contains sorted output hashes, mode
bits, and relative input provenance; the manifest is not included in its own
file hash list. Use `--json` for a machine-readable build summary.

Source transformation is exposed as a read-only Python API in
`startergen.transform`. `TransformTarget` accepts an exercise id, exact
qualified symbol, `replace_body`, `preserve`/`drop` docstring policy, and an
optional custom scaffold. `transform_sources(..., write=False)` returns
reparsed results without changing files; pass `write=True` only after the
caller has reviewed the results.

The assembly API is available from `startergen.assembly` as
`build_project(root)` and `enumerate_allowlist(root, include, exclude)`. The
design boundary for this milestone is documented in
[`IMPLEMENATION/ISSUE_3_DESIGN.md`](../../IMPLEMENATION/ISSUE_3_DESIGN.md).

## Generate documentation

Generate the student README and site outputs with:

```bash
uv run --project tools/startergen startergen docs --root .
```

Teaching Markdown supports headings, links, images, fenced code, inline or
display math, `!!! note`/`!!! warning` admonitions, and standalone
`{{ exercise("id") }}` directives. Directives are ignored inside fenced code,
inline code, and math. README links point into the generated starter tree;
site links use line-anchored generated source HTML below
`publication.release_id` and the configured documentation base URL. Local
references are checked offline; public links are checked only when
`--check-external-links` is supplied.

The issue #4 design record is in
[`IMPLEMENATION/ISSUE_4_DESIGN.md`](../../IMPLEMENATION/ISSUE_4_DESIGN.md).

## Integrated MVP validation

Run the integrated check against a canonical project after the build and docs
contracts validate:

```bash
uv run --project tools/startergen startergen check --root <canonical-root> --json
```

The check runs canonical private/public/smoke suites, installs the generated
starter in a temporary virtual environment, verifies declared baseline failure
reasons and dependency-ordered restoration checkpoints, runs the completed
public suite, and compares independent starter, generated-source, and site
outputs byte-for-byte. The representative fixture is
`tests/fixtures/mvp_canonical_project`; its design record is in
[`IMPLEMENATION/ISSUE_5_DESIGN.md`](../../IMPLEMENATION/ISSUE_5_DESIGN.md).

## Publish an immutable release

Create a reviewable plan with no deployment-side effects:

```bash
uv run --project tools/startergen startergen release \
  --root <canonical-root> --dry-run --json
```

Publish only from a clean canonical commit by providing a clean checkout of
the configured starter repository and a documentation deployment directory:

```bash
uv run --project tools/startergen startergen release \
  --root <canonical-root> \
  --starter-worktree <starter-checkout> \
  --docs-root <versioned-docs-root> --json
```

The starter stage commits and pushes the validated artifact, creates an
immutable release tag, and verifies both remote refs. The documentation stage
atomically updates `releases/<release_id>` and `latest`; both stages persist
independent retry status in `build/release/<release_id>/transaction.json`.

The issue #6 design record is in
[`IMPLEMENATION/ISSUE_6_DESIGN.md`](../../IMPLEMENATION/ISSUE_6_DESIGN.md).
