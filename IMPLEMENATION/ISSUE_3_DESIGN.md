# Issue #3 design decisions

This document records the design choices for **Milestone 3: Artifact
assembly and provenance**. The implementation builds a student-facing tree
from the version-1 contracts and the source transformer without copying the
canonical repository wholesale.

## Assembly boundary

- `startergen.assembly.build_project(root)` validates the authoring contracts,
  enumerates the configured `starter.include` allowlist, transforms declared
  exercise targets in a staging tree, writes a manifest, and promotes the
  result to `starter.output`.
- The allowlist is explicit and path-preserving: an included directory keeps
  its project-relative prefix in the starter. Paths are enumerated in sorted
  order, and duplicate or case-colliding output paths fail before promotion.
- `starter.exclude` removes exact paths and their descendants from an
  allowlisted directory. Built-in deny rules additionally omit repository
  metadata, virtual environments, caches, build/tooling directories,
  instructor/private/generation material, and credential-like files.
- Symlinks and special files are never followed or copied. Existing path
  components are checked with `lstat` before they are read or used as output
  parents, so the build cannot escape the canonical root or write through an
  output symlink.

## Staging and recovery

- Builds run below the owned `build/` root and take an exclusive lock at
  `build/.startergen.lock`. The lock covers enumeration, transformation,
  manifest creation, and promotion.
- A unique staging directory is created below `build/`. The previous output is
  renamed to a temporary backup only during promotion; the new tree is then
  renamed into place. If promotion fails, the backup is restored. Failures
  before promotion remove only the staging directory, preserving the previous
  successful output byte-for-byte.
- The final output is replaced as a whole rather than updated in place. This
  prevents stale files from an older build from surviving a newer allowlist.

## Manifest and provenance

- `.startergen/manifest.json` is generated after transformation. It records
  the manifest schema version, generator version, project identity, sorted
  output file paths, POSIX mode bits, sizes, and SHA-256 hashes.
- The manifest itself is intentionally excluded from its `files` hash list so
  it does not self-reference. Input provenance records relative paths, sizes,
  and hashes for the two contracts and every allowlisted canonical input.
- No absolute local paths, credentials, instructor mappings, transformation
  reports, or detailed validation reports are written into the distributable
  tree. Build diagnostics remain in the returned exception/report or CLI
  output.

## Transformation integration

- Each declared exercise source must be present in the allowlist. Exercise
  targets are grouped by their copied relative source path and passed to the
  existing LibCST transformer inside staging.
- A transformation failure aborts before promotion and retains the previous
  artifact. The manifest hashes the transformed starter bytes, while input
  provenance hashes the original canonical bytes.

## Deliberate non-goals

- Documentation rendering, public-test execution, isolated installation, and
  release publication remain later milestones. This milestone only assembles
  and proves the filesystem artifact boundary on which those workflows rely.
