"""Cross-platform service installer helpers.

``nerya service install`` / ``uninstall`` / ``status`` delegate to the
functions in :mod:`nerya.install.service`. The top-level one-liner shell
scripts (``install/install.sh`` and ``install/install.ps1``) bootstrap
`uv`, clone the source, and then drive the same code path.
"""

from . import service  # noqa: F401

__all__ = ["service"]
