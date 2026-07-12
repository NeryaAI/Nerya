"""Memory API routes enforce operator-level scope before using the runtime."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


def test_memory_capture_never_echoes_rejected_secret_metadata(tmp_path):
    from nerya.api import routes_memory

    config = Config(paths=WorkspacePaths(root=tmp_path), data={})
    client = SimpleNamespace(config=config)
    route_map = {
        (method, path): handler for method, path, handler in routes_memory.routes()
    }
    secret_key = "api_key=sk-secret-api-key-value-1234567890"
    secret_title = "password=secret-api-title-value-1234567890"

    result = route_map[("POST", "/memory/capture")](
        client,
        {
            "category": "learning",
            "content": "Safe body with unsafe metadata.",
            "key": secret_key,
            "title": secret_title,
            "scope": "global",
        },
    )

    assert result["ok"] is False
    assert result["skip_reason"] == "unsafe_content"
    serialized = json.dumps(result, ensure_ascii=False)
    assert secret_key not in serialized
    assert secret_title not in serialized


def test_memory_capture_rejects_an_unknown_strategy(tmp_path):
    from nerya.api import routes_memory

    client = SimpleNamespace(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
    )
    route_map = {
        (method, path): handler for method, path, handler in routes_memory.routes()
    }

    result = route_map[("POST", "/memory/capture")](
        client,
        {
            "category": "decision",
            "content": "这个策略的风险上限是两倍杠杆。",
            "scope": "strategy",
            "strategy_id": "does-not-exist",
        },
    )

    assert result["ok"] is False
    assert result["skip_reason"] == "unknown_strategy"


def test_memory_forget_accepts_a_stable_key(tmp_path):
    from nerya.api import routes_memory
    from nerya.memory.runtime import MemoryRuntime

    config = Config(paths=WorkspacePaths(root=tmp_path), data={})
    runtime = MemoryRuntime(config)
    runtime.remember(
        category="preference",
        content="操作者旧的风险偏好是激进。",
        key="risk.preference",
        scope="global",
    )
    client = SimpleNamespace(config=config)
    route_map = {
        (method, path): handler for method, path, handler in routes_memory.routes()
    }

    result = route_map[("POST", "/memory/forget")](
        client,
        {
            "key": "risk.preference",
            "scope": "global",
        },
    )

    assert result["ok"] is True
    assert result["forgotten"] == 1
    assert MemoryRuntime(config).recall("风险偏好") == []


def test_memory_forget_accepts_a_memory_id_in_a_real_strategy_scope(tmp_path):
    from nerya.api import routes_memory
    from nerya.memory.runtime import MemoryRuntime

    config = Config(paths=WorkspacePaths(root=tmp_path), data={})
    (config.paths.strategies / "alpha").mkdir(parents=True)
    runtime = MemoryRuntime(config, strategy_id="alpha")
    remembered = runtime.remember(
        category="decision",
        content="Alpha 策略在高波动时最多使用两倍杠杆。",
        scope="strategy",
    )
    assert remembered.record is not None
    client = SimpleNamespace(config=config)
    route_map = {
        (method, path): handler for method, path, handler in routes_memory.routes()
    }

    result = route_map[("POST", "/memory/forget")](
        client,
        {
            "memory_id": remembered.record.memory_id,
            "scope": "strategy",
            "strategy_id": "alpha",
        },
    )

    assert result["ok"] is True
    assert result["forgotten"] == 1
    assert MemoryRuntime(config, strategy_id="alpha").recall("两倍杠杆") == []


def test_memory_forget_rejects_an_unknown_strategy(tmp_path):
    from nerya.api import routes_memory

    config = Config(paths=WorkspacePaths(root=tmp_path), data={})
    client = SimpleNamespace(config=config)
    route_map = {
        (method, path): handler for method, path, handler in routes_memory.routes()
    }

    result = route_map[("POST", "/memory/forget")](
        client,
        {
            "key": "risk.preference",
            "scope": "strategy",
            "strategy_id": "does-not-exist",
        },
    )

    assert result["ok"] is False
    assert result["skip_reason"] == "unknown_strategy"


def test_memory_test_uses_canonical_query_recall(tmp_path):
    from nerya.api import routes_memory
    from nerya.memory.runtime import MemoryRuntime

    config = Config(paths=WorkspacePaths(root=tmp_path), data={})
    MemoryRuntime(config).remember(
        category="preference",
        content="操作者的默认持仓周期是三到五天。",
        key="trading.default_horizon",
        scope="global",
    )
    client = SimpleNamespace(config=config)
    route_map = {
        (method, path): handler for method, path, handler in routes_memory.routes()
    }

    result = route_map[("POST", "/memory/test")](
        client,
        {
            "query": "默认持仓周期",
        },
    )

    builtin = next(item for item in result["backends"] if item["backend"] == "builtin")
    assert builtin["ok"] is True
    assert builtin["matches"] == 1
    assert builtin["preview"][0]["content"] == "操作者的默认持仓周期是三到五天。"


def test_memory_forget_route_scrubs_a_keyless_record_by_id(tmp_path):
    from nerya.api import routes_memory
    from nerya.memory.runtime import MemoryRuntime

    config = Config(paths=WorkspacePaths(root=tmp_path), data={})
    runtime = MemoryRuntime(config)
    written = runtime.remember(
        category="preference",
        content="这条临时偏好需要通过 operator API 删除。",
        scope="global",
    )
    assert written.record is not None
    client = SimpleNamespace(config=config)
    route_map = {
        (method, path): handler for method, path, handler in routes_memory.routes()
    }

    result = route_map[("POST", "/memory/forget")](
        client,
        {
            "scope": "global",
            "memory_id": written.record.memory_id,
        },
    )

    assert result == {"ok": True, "forgotten": 1, "scope": "global"}
    assert runtime.recall("临时偏好") == []
