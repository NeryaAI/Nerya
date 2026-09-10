"""AST-based detection of foreign quant-framework strategy source.

Detection is deliberately import-free: we never execute uploaded code
here, we only walk its syntax tree. A file is classified as

* ``freqtrade`` — declares a class whose bases mention ``IStrategy``
  or imports anything from a ``freqtrade.*`` module;
* ``vnpy``      — declares a class whose bases mention ``CtaTemplate``
  (or the legacy ``CtaTemplate`` aliases) or imports from
  ``vnpy_ctastrategy`` / ``vnpy.*``;
* ``unknown``   — otherwise.

When several files are scanned together
(:func:`detect_frameworks`) the strongest signal wins; ties fall back
to the first framework seen. Import statements alone are enough to
classify a file (some Freqtrade strategies only subclass ``IStrategy``
indirectly), but the detected strategy class name is reported when a
framework base class is found so the importer can re-derive it.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

FREQTRADE: str = "freqtrade"
VNPY: str = "vnpy"
UNKNOWN: str = "unknown"

_FREQTRADE_BASES: frozenset[str] = frozenset({"IStrategy"})
_VNPY_BASES: frozenset[str] = frozenset({"CtaTemplate"})

_FREQTRADE_MODULE_RE = re.compile(r"^freqtrade(\.|$)")
_VNPY_MODULE_RE = re.compile(r"^(vnpy|vnpy_ctastrategy|vnpy_ctabacktester)(\.|$)")


@dataclass
class FrameworkInfo:
    """Detection result for one source file (or a file bundle)."""

    framework: str  # "freqtrade" | "vnpy" | "unknown"
    source_file: str = ""
    class_name: str = ""
    class_bases: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def detected(self) -> bool:
        return self.framework in {FREQTRADE, VNPY}

    def asdict(self) -> dict[str, Any]:
        return {
            "framework": self.framework,
            "source_file": self.source_file,
            "class_name": self.class_name,
            "class_bases": list(self.class_bases),
            "imports": list(self.imports),
            "warnings": list(self.warnings),
            "extras": dict(self.extras),
        }


def detect_framework(source: str, *, source_file: str = "") -> FrameworkInfo:
    """Classify one Python source buffer."""

    text = str(source or "")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return FrameworkInfo(
            framework=UNKNOWN,
            source_file=source_file,
            warnings=(f"syntax error: {exc}",),
        )

    imports: list[str] = []
    import_ways: set[str] = set()  # "freqtrade" | "vnpy" — how modules were reached
    best: tuple[int, str, str, tuple[str, ...]] | None = None
    # rank 3 = direct framework base class; 2 = base-name hint without
    # matching import; 1 = framework import only.
    warnings: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = str(alias.name or "").strip()
                if not mod:
                    continue
                imports.append(mod)
                if _FREQTRADE_MODULE_RE.match(mod):
                    import_ways.add("freqtrade")
                elif _VNPY_MODULE_RE.match(mod):
                    import_ways.add("vnpy")
        elif isinstance(node, ast.ImportFrom):
            mod = str(node.module or "").strip()
            if not mod:
                continue
            imports.append(mod)
            if _FREQTRADE_MODULE_RE.match(mod):
                import_ways.add("freqtrade")
            elif _VNPY_MODULE_RE.match(mod):
                import_ways.add("vnpy")
        elif isinstance(node, ast.ClassDef):
            base_names = _base_names(node)
            framework = _framework_from_bases(base_names)
            if not framework:
                continue
            rank = 3 if framework in import_ways else 2
            if best is None or rank > best[0]:
                best = (rank, framework, node.name, tuple(base_names))

    if best is not None:
        rank, framework, class_name, bases = best
        return FrameworkInfo(
            framework=framework,
            source_file=source_file,
            class_name=class_name,
            class_bases=bases,
            imports=tuple(imports),
            warnings=tuple(warnings),
        )

    if "freqtrade" in import_ways:
        return FrameworkInfo(
            framework=FREQTRADE,
            source_file=source_file,
            imports=tuple(imports),
            warnings=tuple(warnings + ["no IStrategy subclass found; using import-only detection"]),
        )
    if "vnpy" in import_ways:
        return FrameworkInfo(
            framework=VNPY,
            source_file=source_file,
            imports=tuple(imports),
            warnings=tuple(warnings + ["no CtaTemplate subclass found; using import-only detection"]),
        )
    return FrameworkInfo(
        framework=UNKNOWN,
        source_file=source_file,
        imports=tuple(imports),
    )


def detect_frameworks(
    files: "list[tuple[str, str]] | dict[str, str]",
) -> FrameworkInfo:
    """Classify a bundle of ``(filename, source)`` pairs.

    Returns the :class:`FrameworkInfo` for the file that defines the
    framework strategy class. Files that merely import framework
    helpers act as fallbacks; a bundle with no signal classifies as
    ``unknown``.
    """

    items: list[tuple[str, str]] = (
        [(str(k), str(v)) for k, v in files.items()]
        if isinstance(files, dict)
        else [(str(f), str(s)) for f, s in files]
    )
    best: FrameworkInfo | None = None
    for name, source in items:
        info = detect_framework(source, source_file=name)
        if not info.detected:
            continue
        rank = 2 if info.class_name else 1
        if best is None or rank > (2 if best.class_name else 1):
            best = info
        if best is not None and best.class_name:
            break
    if best is not None:
        return best
    first = FrameworkInfo(framework=UNKNOWN, source_file=items[0][0] if items else "")
    return first


def _base_names(node: ast.ClassDef) -> list[str]:
    names: list[str] = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            # e.g. freqtrade.strategy.IStrategy
            parts: list[str] = [base.attr]
            cur: ast.expr = base.value
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            names.append(".".join(reversed(parts)))
    return names


def _framework_from_bases(base_names: list[str]) -> str | None:
    for name in base_names:
        tail = name.rsplit(".", 1)[-1]
        if tail in _FREQTRADE_BASES or name in _FREQTRADE_BASES:
            return FREQTRADE
        if tail in _VNPY_BASES or name in _VNPY_BASES:
            return VNPY
    return None


__all__ = [
    "FREQTRADE",
    "FrameworkInfo",
    "UNKNOWN",
    "VNPY",
    "detect_framework",
    "detect_frameworks",
]
