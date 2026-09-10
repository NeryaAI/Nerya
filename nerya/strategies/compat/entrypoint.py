"""Compat entrypoint — what a generated ``main.py`` delegates to.

A framework-compat strategy package's ``main.py`` is a three-liner::

    from pathlib import Path
    from nerya.strategies import run_compat_tick

    def run(ctx):
        return run_compat_tick(ctx, package_root=Path(__file__).resolve().parent)

:func:`run_compat_tick` then:

1. reads the ``framework`` block out of the package's ``strategy.yml``
   extras (``name`` / ``source_file`` / ``class`` / ``settings``);
2. installs the matching shims (only when the real framework is not
   importable) and imports the user's module from the package root;
3. instantiates the strategy class once per process (cached by
   package root + class name + source fingerprint, so ``--overwrite``
   re-imports invalidate) and applies ``settings``;
4. dispatches to the framework adapter
   (:func:`~nerya.strategies.compat.freqtrade_adapter.run_freqtrade_tick`
   or :func:`~nerya.strategies.compat.vnpy_adapter.run_vnpy_tick`)
   with ``ctx.config.markets[0]`` as the market.

Exceptions surface as ``ctx.result.error(..., kind="compat_error")`` —
the runner journals them like any other strategy failure.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from .detect import FREQTRADE, VNPY

_LOG = logging.getLogger(__name__)


class CompatRunError(Exception):
    """Raised when a compat package cannot be loaded or dispatched."""


_LOADED_LOCK = threading.Lock()
# cache key: (package root, class name, source-content fingerprint) —
# the fingerprint invalidates the cache after an ``--overwrite`` re-import.
_LOADED: dict[tuple[str, str, str], Any] = {}


def run_compat_tick(ctx: Any, *, package_root: Optional[str | Path] = None) -> Any:
    """Execute one tick of a framework-compat strategy package.

    ``package_root`` defaults to ``ctx.strategy_root`` (live runner) but
    generated entrypoints pass their own directory so backtest replay
    — whose mock context has no ``strategy_root`` — resolves the same
    package.
    """

    root = _resolve_package_root(ctx, package_root)
    framework_block = _load_framework_block(root)
    framework = str(framework_block.get("name") or "").strip().lower()
    if framework not in {FREQTRADE, VNPY}:
        return ctx.result.error(
            message=(
                f"unsupported compat framework {framework!r} (expected "
                f"{FREQTRADE!r} or {VNPY!r})"
            ),
            kind="compat_error",
        )

    markets = tuple(getattr(ctx.config, "markets", ()) or ())
    if not markets:
        return ctx.result.error(message="compat package has no market configured", kind="compat_error")

    # The manifest block's own keys (``timeframe`` / ``bar_timeframe`` /
    # ``stake_amount`` / ...) must reach the adapters — the documented
    # manifest-level overrides otherwise never apply. Explicit
    # ``settings`` entries win over block keys.
    raw_settings = dict(framework_block.get("settings") or {})
    settings = {
        key: value
        for key, value in framework_block.items()
        if key not in {"name", "source_file", "class", "settings"}
    }
    settings.update(raw_settings)

    try:
        strategy = load_framework_strategy(
            root,
            framework=framework,
            source_file=str(framework_block.get("source_file") or ""),
            class_name=str(framework_block.get("class") or ""),
            settings=settings,
        )
    except CompatRunError as exc:
        return ctx.result.error(message=f"compat_load_failed: {exc}", kind="compat_error")

    try:
        if framework == FREQTRADE:
            from .freqtrade_adapter import run_freqtrade_tick

            return run_freqtrade_tick(ctx, strategy, market=markets[0], settings=settings)
        from .vnpy_adapter import run_vnpy_tick

        return run_vnpy_tick(ctx, strategy, market=markets[0], settings=settings)
    except Exception as exc:
        _LOG.exception("compat tick failed for %s", markets[0])
        return ctx.result.error(message=f"compat_tick_failed: {exc}", kind="compat_error")


def load_framework_strategy(
    package_root: str | Path,
    *,
    framework: str,
    source_file: str,
    class_name: str,
    settings: dict[str, Any] | None = None,
) -> Any:
    """Import + instantiate the foreign strategy class (cached per process).

    The whole check → shim-install → import → instantiate sequence runs
    under ``_LOADED_LOCK``: installing shims and exec'ing the user's
    module are process-global mutations, so letting two threads do them
    concurrently could double-instantiate the strategy. The cache key
    includes a fingerprint of the package's Python sources so an
    ``--overwrite`` re-import invalidates previously cached instances.
    """

    root = Path(package_root).resolve()
    module_path = (root / source_file).resolve() if source_file else None
    if module_path is None or not module_path.exists():
        raise CompatRunError(f"strategy source not found: {source_file!r}")
    if not class_name:
        raise CompatRunError("framework block missing strategy class name")

    cache_key = (str(root), class_name, _source_fingerprint(root))
    with _LOADED_LOCK:
        cached = _LOADED.get(cache_key)
        if cached is not None:
            _apply_settings(cached, settings or {})
            return cached

        install_shims_for(framework)

        module_name = f"_nerya_compat.{root.name}.{module_path.stem}"
        spec = importlib.util.spec_from_file_location(
            module_name, module_path, submodule_search_locations=[str(root)]
        )
        if spec is None or spec.loader is None:
            raise CompatRunError(f"cannot import strategy source: {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        # Multi-file bundles use absolute sibling imports (``import
        # helpers``); the package root must be importable for the
        # duration of the exec only, and only our own path entry is
        # removed afterwards.
        sys.path.insert(0, str(root))
        modules_before = frozenset(sys.modules)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise CompatRunError(f"importing {source_file} failed: {exc}") from exc
        finally:
            try:
                sys.path.remove(str(root))
            except ValueError:
                pass
            # Drop the module itself plus any package-local helper
            # modules (``import helpers``) the exec pulled in. Without
            # this the bare names leak into the global module table and
            # the *second* compat package shipping ``helpers.py``
            # silently receives the first package's cached module
            # (cross-wired indicators). Mirrors the native runner path
            # (runner._pop_strategy_package_modules).
            sys.modules.pop(module_name, None)
            _pop_compat_package_modules(root, modules_before)

        strategy_cls = getattr(module, class_name, None)
        if strategy_cls is None:
            raise CompatRunError(f"{source_file} does not define class {class_name!r}")

        try:
            strategy = _instantiate(framework, strategy_cls, root, settings or {})
        except CompatRunError:
            raise
        except Exception as exc:
            raise CompatRunError(f"constructing {class_name} failed: {exc}") from exc

        _LOADED[cache_key] = strategy
        return strategy


def _source_fingerprint(root: Path) -> str:
    """Stable hash of the package's Python sources (main.py included).

    Recomputed per load call — reading a handful of small files is
    cheap next to an import, and it is what makes ``--overwrite``
    re-imports pick up a fresh instance in the same process.
    """

    digest = hashlib.sha256()
    candidates = dict.fromkeys([root / "main.py", *sorted(root.glob("*.py"))])
    for path in candidates:
        try:
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        except OSError:
            continue
    return digest.hexdigest()[:32]


def install_shims_for(framework: str) -> None:
    """Register the shims a framework needs (no-op when the real pkg exists)."""

    from .shims import install_freqtrade_shims, install_qtpylib_shims, install_vnpy_shims

    if framework == FREQTRADE:
        install_qtpylib_shims()
        install_freqtrade_shims()
    elif framework == VNPY:
        install_vnpy_shims()


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _pop_compat_package_modules(root: Path, before: frozenset[str]) -> None:
    """Drop ``sys.modules`` entries newly created inside ``root``.

    Copy of ``runner._pop_strategy_package_modules`` (that module owns
    the native runner path; importing it from here would pull the whole
    runner into the compat entrypoint). Package-local helper imports
    (``import helpers``) cache bare names in the global module table;
    left there, a second compat package shipping its own ``helpers.py``
    would silently receive the first package's cached module. We diff
    the module table around the exec and pop every new module whose
    file resolves inside the package root. Well-behaved single-file
    packages add nothing, so their behaviour is unchanged.
    """

    try:
        root_resolved = root.resolve()
    except OSError:  # pragma: no cover — defensive
        return
    for name in list(sys.modules):
        if name in before:
            continue
        module = sys.modules.get(name)
        origin = getattr(getattr(module, "__spec__", None), "origin", None)
        origin = origin or getattr(module, "__file__", None)
        if not origin:
            continue
        try:
            if Path(origin).resolve().is_relative_to(root_resolved):
                sys.modules.pop(name, None)
        except OSError:  # pragma: no cover — defensive
            continue


def _resolve_package_root(ctx: Any, package_root: Optional[str | Path]) -> Path:
    root: Optional[Path] = None
    if package_root:
        root = Path(package_root).resolve()
    elif getattr(ctx, "strategy_root", None):
        root = Path(str(ctx.strategy_root)).resolve()
    if root is None or not (root / "strategy.yml").exists():
        raise CompatRunError(f"cannot resolve strategy package root: {root}")
    return root


def _load_framework_block(root: Path) -> dict[str, Any]:
    from ...core import yaml_io

    raw = yaml_io.load(root / "strategy.yml")
    if not isinstance(raw, dict):
        raise CompatRunError(f"{root / 'strategy.yml'} is not a mapping")
    extras = raw.get("extras") or {}
    block = extras.get("framework") if isinstance(extras, dict) else None
    if not isinstance(block, dict):
        raise CompatRunError("strategy.yml extras.framework block missing")
    return dict(block)


def _instantiate(
    framework: str,
    strategy_cls: Any,
    root: Path,
    settings: dict[str, Any],
) -> Any:
    if framework == FREQTRADE:
        strategy = strategy_cls(config={"strategy_id": root.name})
        _apply_settings(strategy, settings)
        return strategy

    from .shims.vnpy_shim import CtaTemplate

    if not issubclass(strategy_cls, CtaTemplate):
        # Foreign strategy class built against the real vnpy install —
        # still constructible the same way.
        pass
    stub_engine = _StubEngine()
    vt_symbol = str(settings.get("vt_symbol") or f"{root.name}.LOCAL")
    strategy = strategy_cls(
        stub_engine,
        root.name,
        vt_symbol,
        dict(settings),
    )
    strategy.trading = False
    return strategy


def _apply_settings(strategy: Any, settings: dict[str, Any]) -> None:
    for name, value in (settings or {}).items():
        if not hasattr(strategy, name):
            continue
        current = getattr(strategy, name)
        if hasattr(current, "value") and not isinstance(current, (str, int, float, bool)):
            # Hyperopt-style parameter object.
            try:
                current.value = type(current.value)(value)
                continue
            except (TypeError, ValueError):
                pass
        try:
            setattr(strategy, name, value)
        except AttributeError:
            continue


class _StubEngine:
    """Placeholder engine used only while constructing a VNpy template.

    The vnpy adapter swaps in a real :class:`VnpyOrderBridge` before
    every tick, so none of these methods should ever fire.
    """

    def write_log(self, msg: str, strategy: Any = None) -> None:
        _LOG.debug("vnpy stub engine log: %s", msg)

    def get_engine_type(self) -> Any:
        from .shims.vnpy_shim import EngineType

        return EngineType.BACKTESTING

    def send_order(self, *args: Any, **kwargs: Any) -> list[str]:
        return []

    def load_bar(self, *args: Any, **kwargs: Any) -> None:
        pass

    def sync_data(self, strategy: Any = None) -> None:
        pass

    def cancel_all(self, strategy: Any = None) -> None:
        pass


__all__ = [
    "CompatRunError",
    "install_shims_for",
    "load_framework_strategy",
    "run_compat_tick",
]
