from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from nerya.api import routes_capability
from nerya.agent import route_manifests
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


RUNTIME_ROOTS = (
    Path("nerya/agent"),
    Path("nerya/tools/native"),
)

def _name_words(name: str) -> set[str]:
    return {part for part in name.upper().split("_") if part}


def _forbidden_symbol(name: str) -> bool:
    words = _name_words(name)
    if {"INTENT", "MARKERS"}.issubset(words):
        return True
    if {"KEYWORD", "MARKERS"}.issubset(words):
        return True
    if {"NATIVE", "ROUTE", "WEB"}.issubset(words) and name.upper().endswith("_RE"):
        return True
    if {"PREFERS", "MARKET", "ANALYSIS", "RATING"}.issubset(words):
        return True
    return False


def _forbidden_text(text: str) -> bool:
    compact = " ".join(text.lower().split())
    return ("native " + "route " + "discovery") in compact


def _forbidden_chinese_fallback_text(text: str) -> bool:
    forbidden = (
        "无法继续执行后续工具",
        "未执行的后续工具",
        "仍缺的必要动作",
        "为避免在超时边界启动半截操作",
    )
    return any(item in text for item in forbidden)


def test_runtime_has_no_known_prompt_or_web_route_hardcoding() -> None:
    offenders: list[str] = []
    for root in RUNTIME_ROOTS:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if _forbidden_symbol(node.name):
                        offenders.append(f"{path}:{node.name}")
                elif isinstance(node, ast.Name) and _forbidden_symbol(node.id):
                    offenders.append(f"{path}:{node.id}")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if _forbidden_text(node.value):
                        offenders.append(f"{path}:native-route-discovery-text")
                    if _forbidden_chinese_fallback_text(node.value):
                        offenders.append(f"{path}:hardcoded-chinese-fallback-text")

    assert offenders == []


def test_default_config_uses_manifest_without_inline_route_table() -> None:
    planner = DEFAULT_CONFIG["agent"]["planner"]

    assert planner["manifest"] == "trading-v1"
    assert planner["routes"] == {}
    assert planner["fallback"] == "generic"


def test_builtin_route_manifests_are_declarative_resources() -> None:
    source = Path(route_manifests.__file__).read_text(encoding="utf-8")

    assert "_TRADING_V1" not in source
    assert "price_signal" not in source
    assert "text_contains" not in source

    manifests = {m.id: m for m in route_manifests.builtin_manifests()}
    assert set(manifests) == {"general-operator-v1", "minimal-v1", "trading-v1"}
    for manifest in manifests.values():
        assert manifest.routes == {}
        assert manifest.fallback == "generic"


def test_builtin_route_manifest_resources_do_not_ship_match_tables() -> None:
    offenders: list[str] = []
    for path in Path("nerya/agent/route_manifest_presets").glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        if "routes:" in text or "match:" in text:
            offenders.append(str(path))

    assert offenders == []


def test_active_manifest_does_not_fall_back_to_inline_route_table(tmp_path) -> None:
    data = deepcopy(DEFAULT_CONFIG)
    data["agent"]["planner"]["manifest"] = "trading-v1"
    data["agent"]["planner"]["routes"] = {
        "legacy_prompt_route": {
            "match": ["legacy.*"],
            "skills": ["legacy"],
            "subagents": [],
            "tier": "light",
        }
    }
    cfg = Config(paths=WorkspacePaths(tmp_path), data=data)

    planner = routes_capability._planner_section(SimpleNamespace(config=cfg))

    assert planner["active_manifest"] == "trading-v1"
    assert planner["routes"] == []
    assert planner["fallback"] == "generic"
