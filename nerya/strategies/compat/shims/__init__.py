"""Shim installation for framework-compat strategy packages."""

from __future__ import annotations

from .freqtrade_shim import install as install_freqtrade_shims
from .qtpylib_shim import install as install_qtpylib_shims
from .vnpy_shim import install as install_vnpy_shims

__all__ = [
    "install_freqtrade_shims",
    "install_qtpylib_shims",
    "install_vnpy_shims",
]
