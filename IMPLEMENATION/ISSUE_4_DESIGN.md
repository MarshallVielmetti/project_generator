# Issue #4 design decisions

This document records the design choices for **Teaching outputs, documentation,
and links**. The implementation is intentionally a deterministic output stage
on top of the validated starter artifact from issue #3.

## Two documented adapters

- `startergen.documentation.render_readme` renders the maintainer-owned Jinja
  layout with `StrictUndefined`, then appends the teaching page unless the
  layout explicitly uses the `content` variable. It produces GitHub Markdown:
  exercise directives become relative links into the generated starter, local
  images become `assets/<name>`, and the supported `!!! note`, `!!! warning`,
  and `!!! tip` forms become blockquotes that GitHub can display.
- `startergen.documentation.render_site` emits a MkDocs-compatible Markdown
  page and a deterministic static HTML preview. Site exercise links use the
  immutable `publication.release_id` below
  `publication.docs_base_url/releases/`, so a branch rename cannot silently
  move a released link. The preview uses a
  pinned MathJax 3.2.2 URL and retains the same TeX delimiters for inline and
  display math.

Jinja is used only for the maintainer-owned README layout. Teaching Markdown
  is parsed as content; template blocks and unsupported `:::` extensions are
  rejected instead of being executed. A malformed or unknown exercise
  directive reports `teaching/project.md` and its one-based source line.

## Final-source index and line semantics

Issue #3's LibCST transformer returns `ResolvedTarget` records after serializing
each transformed file. Issue #4 carries those records in `BuildResult` and
builds `build/docs-src/exercises.json` from their inclusive final line ranges.
The generated source tree is copied under `build/docs-src/source/`, so the
human-readable index links to the exact serialized file and displays both its
qualified symbol and `path:start-end` context. This avoids deriving links from
pre-transform source positions.

The generated source and static-site trees retain the raw source files and add
`<path>.html` source views with `L7` single-line anchors and `L7-L8` range
anchors. Exercise links in the static site use those HTML views, so a browser
can land on the documented final range instead of opening an unanchored raw
text file. README links continue to target the generated starter files directly
because GitHub provides the corresponding code-view range behavior.

The starter manifest is refreshed after README and asset generation, while the
generated-source and site trees remain separate outputs under their configured
`build/` destinations.

## Markdown subset and validation

The parser recognizes headings, fenced code, inline code, inline/display math,
links, images, and the two common admonition families used by the teaching
fixture. Exercise directives are recognized only as a standalone line outside
fences, inline code, and display math; fenced examples therefore remain
literal. Local links, heading anchors, and image assets are checked without
network access. Public HTTP(S) links are collected separately and are checked
only when `--check-external-links` is requested.

Generated site assets include the teaching asset tree, background tree, source
tree, line-anchored source HTML views, `index.md`, `index.html`, and a small
MathJax configuration file. The documentation transaction holds the same
`.startergen.lock` used by starter assembly from its final build through all
README, manifest, and site promotions. Output directories are replaced through
temporary siblings so a failed site write does not leave a partially generated
directory or erase an output created by a concurrent build.

## Browser release acceptance

The representative fixture was served from the generated `build/site` tree and
opened in a browser. The acceptance page visibly contained the exercise link,
the note admonition, a loaded SVG image, inline math, the display matrix, and
the literal fenced exercise directive. The browser DOM confirmed one loaded
image, one admonition, two MathJax-rendered math containers, and the literal
fenced directive text. The observed acceptance record is kept in
[`ISSUE_4_BROWSER_ACCEPTANCE.md`](ISSUE_4_BROWSER_ACCEPTANCE.md).

## Deliberate non-goals

- This milestone does not publish to a remote hosting service. Browser
  acceptance is performed against the generated local site tree.
- The Markdown renderer is not a general CommonMark implementation. Adding a
  new extension requires an explicit parser rule and regression coverage.
- External link checking is opt-in because a normal build must be reproducible
  offline.
