# Issue #6 design decisions

This document records the design choices for **Milestone 6: Publication and release workflow**.

## Release boundary

Publication is a separate stage after the integrated MVP check, so an artifact cannot be published merely because a local build happened to exist.

`startergen release` runs the complete `startergen check` workflow before it creates a release plan.

The canonical checkout must be clean and at an exact Git commit, which makes the source revision in a release auditable and prevents uncommitted local edits from being published.

The release configuration continues to own the logical starter repository, target branch, immutable release identifier, and documentation base URL.

The destination paths are command-line inputs rather than authoring metadata because they are deployment-environment concerns and must never be confused with canonical project paths.

The starter worktree must be a clean Git checkout and cannot be the canonical checkout.

## Artifact identity

The starter artifact identity is the SHA-256 digest of the canonical JSON representation of the generated manifest file records.

Manifest records already contain sorted relative paths, modes, sizes, and content hashes, so the identity is independent of machine paths, timestamps, and directory traversal order.

The documentation identity is computed from sorted relative paths, sizes, and SHA-256 content hashes for the generated site tree.

The release metadata stores both identities, the source commit, the configured repository and branch, the release identifier, and the generated documentation URL.

The metadata is intentionally excluded from the starter artifact identity and is written only after the validated artifact has been copied.

## Transaction and retry semantics

Each release has an atomically written `build/release/<release_id>/transaction.json` file with separate `starter` and `documentation` stage records.

The plan is written before either external destination is changed, making `--dry-run` a reviewable operation with no target mutation.

Starter publication and documentation deployment update their transaction records independently, so a documentation failure can be retried without making a second starter commit.

The starter publisher never force-pushes and uses an immutable release tag equal to `release_id`.

The publisher skips a commit when the target already contains the same validated artifact, but it still verifies the branch and tag remotely.

An existing release identifier with a different artifact is rejected, and a release whose natural version ordering is older than the current published release is rejected as stale.

The stale check is performed against the target's release metadata after fetching the target branch, while a non-fast-forward push protects against a concurrent release that wins the race after the check.

Documentation uses an atomic temporary directory and promotes `releases/<release_id>` before atomically replacing `latest`.

The `latest` directory is not updated until the versioned release tree has been completely copied and its release metadata has been written.

## Dedicated repository and credential boundary

Only the configured starter artifact is copied into the dedicated starter repository, while that repository's own `.git` metadata is retained and the canonical repository metadata is never copied.

The optional `--mark-template` operation invokes `gh repo edit` only after starter and documentation publication succeed.

The manual release workflow supplies `STARTER_REPO_TOKEN` only to the target checkout, publication command, and optional template operation.

The workflow reads `STARTER_REPOSITORY` and `STARTER_BRANCH` from repository variables, so those deployment settings can be kept aligned with the canonical publication contract without putting environment paths in authoring YAML.

Pull-request and push validation workflows retain `contents: read` and do not receive publication credentials.

The workflow uploads the versioned documentation tree as a GitHub Pages artifact and then deploys it through the Pages deployment action.

## Deliberate non-goals

The release module does not create a GitHub repository or mint credentials because repository provisioning and secret management are administrator actions.

The filesystem documentation sink is intentionally provider-neutral, while the checked-in workflow supplies the GitHub Pages adapter for this repository.

The release identifier is not generated implicitly from wall-clock time; maintainers choose and review it through the publication configuration and workflow inputs.
