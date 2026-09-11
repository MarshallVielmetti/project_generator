# startergen

`startergen` is the maintainer-side validator for the teaching starter-project
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

Source transformation is exposed as a read-only Python API in
`startergen.transform`. `TransformTarget` accepts an exercise id, exact
qualified symbol, `replace_body`, `preserve`/`drop` docstring policy, and an
optional custom scaffold. `transform_sources(..., write=False)` returns
reparsed results without changing files; pass `write=True` only after the
caller has reviewed the results.
