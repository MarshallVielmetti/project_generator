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
