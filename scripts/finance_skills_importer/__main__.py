"""``python -m scripts.finance_skills_importer`` entrypoint."""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":  # pragma: no cover — wrapper
    sys.exit(main())
