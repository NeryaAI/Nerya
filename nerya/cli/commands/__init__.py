"""Per-topic command implementations for the Nerya CLI.

Each module exposes ``register(sub)`` which attaches its subcommands to
a shared ``argparse`` subparsers group. :func:`nerya.cli.app.build_parser`
wires all of them together so the public ``nerya <cmd>`` surface stays
identical after the split.
"""

from . import core, evolution, runtime, skills, strategy, wallet

__all__ = ["core", "evolution", "runtime", "skills", "strategy", "wallet"]
