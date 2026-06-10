from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from nerya.api import routes_search


pytestmark = pytest.mark.smoke


def test_search_engine_test_subprocess_uses_workspace_env(
    monkeypatch,
    tmp_path,
) -> None:
    route_map = {(method, path): handler for method, path, handler in routes_search.routes()}
    handler = route_map[("POST", "/search/engines/test")]
    captured: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = '{"ok": true, "engine_chain": ["searxng"]}'
        stderr = ""

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = SimpleNamespace(
        config=SimpleNamespace(
            paths=SimpleNamespace(root=tmp_path),
        ),
    )

    result = handler(client, {"query": "Nerya search probe"})

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["NERYA_WORKSPACE"] == str(tmp_path)
    assert captured["cwd"] == str(tmp_path)
    assert result["ok"] is True
    assert result["result"]["engine_chain"] == ["searxng"]
