"""Allow ``python -m startergen`` to invoke the CLI."""

from startergen.cli import main

if __name__ == "__main__":  # pragma: no cover - exercised by the entry point
    raise SystemExit(main())
