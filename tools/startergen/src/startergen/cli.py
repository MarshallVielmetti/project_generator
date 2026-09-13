"""Command-line interface for the milestone-1 validator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from startergen.assembly import AssemblyError, build_project
from startergen.check import CheckError, check_project
from startergen.documentation import DocumentationError, build_documentation
from startergen.publication import PublicationError, release_project
from startergen.validate import validate_project


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="startergen", description="Validate and build teaching starter projects."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser(
        "validate", help="validate authoring contracts without changing outputs"
    )
    validate.add_argument(
        "--root", required=True, type=Path, help="canonical project root"
    )
    validate.add_argument(
        "--json", action="store_true", help="emit a machine-readable diagnostic report"
    )
    build = commands.add_parser("build", help="assemble and promote a starter artifact")
    build.add_argument(
        "--root", required=True, type=Path, help="canonical project root"
    )
    build.add_argument(
        "--json", action="store_true", help="emit a machine-readable build report"
    )
    docs = commands.add_parser(
        "docs", help="generate the student README and documentation site outputs"
    )
    docs.add_argument(
        "--root", required=True, type=Path, help="canonical project root"
    )
    docs.add_argument(
        "--check-external-links",
        action="store_true",
        help="check public HTTP(S) links in addition to offline validation",
    )
    docs.add_argument(
        "--json", action="store_true", help="emit a machine-readable documentation report"
    )
    check = commands.add_parser(
        "check", help="build and validate the integrated starter MVP in isolation"
    )
    check.add_argument(
        "--root", required=True, type=Path, help="canonical project root"
    )
    check.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="per-test and per-install timeout in seconds (default: 60)",
    )
    check.add_argument(
        "--json", action="store_true", help="emit a machine-readable check report"
    )
    release = commands.add_parser(
        "release", help="validate, plan, and publish an immutable starter release"
    )
    release.add_argument(
        "--root", required=True, type=Path, help="canonical project root"
    )
    release.add_argument(
        "--starter-worktree",
        type=Path,
        help="clean checkout of the configured starter repository",
    )
    release.add_argument(
        "--docs-root",
        type=Path,
        help="versioned documentation deployment directory",
    )
    release.add_argument(
        "--dry-run",
        action="store_true",
        help="write a release plan and transaction state without changing targets",
    )
    release.add_argument(
        "--mark-template",
        action="store_true",
        help="mark the configured starter repository as a GitHub template",
    )
    release.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="per-test and per-install timeout in seconds (default: 60)",
    )
    release.add_argument(
        "--json", action="store_true", help="emit a machine-readable release report"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        # argparse uses 2 for usage errors and 0 for --help.
        return int(exc.code)
    if args.command == "validate":
        report = validate_project(args.root)
        if args.json:
            print(report.to_json())
        elif report.ok:
            print(f"Valid startergen project: {args.root}")
        else:
            for diagnostic in report.diagnostics:
                print(diagnostic.format_text(), file=sys.stderr)
        return 0 if report.ok else 1
    if args.command == "build":
        try:
            result = build_project(args.root)
        except AssemblyError as exc:
            if args.json:
                print(
                    json.dumps(
                        {
                            "built": False,
                            "code": exc.code,
                            "message": str(exc),
                            "diagnostics": [
                                diagnostic.to_dict() for diagnostic in exc.diagnostics
                            ],
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(f"build failed: {exc.code}: {exc}", file=sys.stderr)
                for diagnostic in exc.diagnostics:
                    print(diagnostic.format_text(), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            print(f"Built starter artifact: {result.output}")
        return 0
    if args.command == "docs":
        try:
            result = build_documentation(
                args.root, check_external_links=args.check_external_links
            )
        except (DocumentationError, AssemblyError) as exc:
            if args.json:
                print(
                    json.dumps(
                        {
                            "built": False,
                            "code": exc.code,
                            "message": str(exc),
                            "diagnostics": [
                                diagnostic.to_dict() for diagnostic in exc.diagnostics
                            ],
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(f"documentation failed: {exc.code}: {exc}", file=sys.stderr)
                for diagnostic in exc.diagnostics:
                    print(diagnostic.format_text(), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            print(f"Generated documentation: {result.site}")
        return 0
    if args.command == "check":
        try:
            result = check_project(args.root, timeout=args.timeout)
        except (CheckError, DocumentationError, AssemblyError) as exc:
            details = (
                exc.details
                if isinstance(exc, CheckError)
                else {
                    "diagnostics": [
                        diagnostic.to_dict() for diagnostic in exc.diagnostics
                    ]
                }
            )
            if args.json:
                print(
                    json.dumps(
                        {
                            "checked": False,
                            "code": exc.code,
                            "message": str(exc),
                            "details": details,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(f"integrated check failed: {exc.code}: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            print(f"Integrated MVP checks passed: {result.project}")
        return 0
    if args.command == "release":
        try:
            result = release_project(
                args.root,
                starter_worktree=args.starter_worktree,
                docs_root=args.docs_root,
                dry_run=args.dry_run,
                mark_template=args.mark_template,
                timeout=args.timeout,
            )
        except (PublicationError, CheckError, DocumentationError, AssemblyError) as exc:
            details = (
                exc.details
                if isinstance(exc, (PublicationError, CheckError))
                else {
                    "diagnostics": [
                        diagnostic.to_dict() for diagnostic in exc.diagnostics
                    ]
                }
            )
            if args.json:
                print(
                    json.dumps(
                        {
                            "released": False,
                            "code": getattr(exc, "code", "release_failed"),
                            "message": str(exc),
                            "details": details,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(
                    f"release failed: {getattr(exc, 'code', 'release_failed')}: {exc}",
                    file=sys.stderr,
                )
            return 1
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        elif result.dry_run:
            print(f"Release plan written: {result.transaction}")
        else:
            print(f"Release published: {result.plan.release_id}")
        return 0
    return 2  # pragma: no cover - protected by argparse's required subcommand


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
