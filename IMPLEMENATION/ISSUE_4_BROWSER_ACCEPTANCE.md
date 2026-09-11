# Issue #4 browser acceptance

Date: 2026-09-11

The representative `minimal_completed_project` fixture was generated with:

```text
uv run --project tools/startergen startergen docs --root <fixture>
```

The resulting `build/site` directory was served locally and opened in a
browser. The release page showed all required representative content:

- the `Unicycle dynamics` exercise link with its final `7-8` source range;
- the `Learning goal` note admonition;
- the SVG lab image loaded successfully;
- inline math and the display matrix rendered by the pinned MathJax 3.2.2
  script; and
- the fenced `{{ exercise("unicycle-dynamics") }}` example remained literal.

The browser DOM checks recorded one loaded image, one admonition, two math
containers, and the literal fenced directive. The local server and temporary
fixture were removed after verification.
