"""Command-line interface for the milestone-1 validator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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
    return 2  # pragma: no cover - protected by argparse's required subcommand


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
