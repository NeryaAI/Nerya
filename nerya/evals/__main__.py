"""``python -m nerya.evals`` — see :mod:`nerya.evals.cli`."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
