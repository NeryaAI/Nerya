"""Nerya CLI — top-level entry point.

Until recently this module was a 700-line wall of ``cmd_*`` functions
+ ``argparse`` wiring. The implementation of every subcommand now
lives in :mod:`nerya.cli.commands.<topic>`; this file only:

* builds the root parser,
* asks each topic module to register its subcommands,
* dispatches ``args.func(args)``.

If you're looking for the list of ``nerya`` subcommands, read
``docs/runbook.md``. If you're looking for the implementation of one,
grep for ``cmd_<name>`` in ``nerya/cli/commands/``.

Subcommand topic map:

* :mod:`commands.core`       — init, run, serve, dashboard, doctor, service
* :mod:`commands.skills`     — skill, trigger, trading, strategy, review, portfolio, messages
* :mod:`commands.evolution`  — reflect, evolve, proposals, scripts
* :mod:`commands.runtime`    — vault, llm, mcp, acp, cron, dev
* :mod:`commands.wallet`     — wallet list / status / install-hint / use

Public surface: :func:`build_parser`, :func:`main`. These are imported
by ``pyproject.toml::nerya = "nerya.cli.app:main"`` — do not rename.
"""

from __future__ import annotations

import argparse
import sys

from .commands import core, evolution, runtime, skills, strategy, wallet


def build_parser() -> argparse.ArgumentParser:
    """Assemble the full ``nerya`` argparse tree.

    Each topic module owns its own ``register(sub)`` method so new
    subcommands don't force anyone to edit a central dispatch table.
    """
    parser = argparse.ArgumentParser(prog="nerya")
    sub = parser.add_subparsers(dest="cmd", required=True)

    core.register(sub)
    skills.register(sub)
    strategy.register(sub)
    evolution.register(sub)
    runtime.register(sub)
    wallet.register(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["build_parser", "main"]
