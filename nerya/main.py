"""Nerya process entrypoint. Delegates to the CLI."""

from __future__ import annotations

from .cli.app import main as _cli_main


def main(argv: list[str] | None = None) -> int:
    return _cli_main(argv)


if __name__ == "__main__":
    import sys
    sys.exit(main())
