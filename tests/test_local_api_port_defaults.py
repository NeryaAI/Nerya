"""Regression coverage for the single local API port contract."""

from __future__ import annotations

from copy import deepcopy
import inspect
import json
import math
import threading

import pytest

from nerya.api import local_server
from nerya.cli.app import build_parser
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


def test_cli_run_and_serve_default_to_single_local_api_port():
    parser = build_parser()

    assert parser.parse_args(["run"]).port == 18317
    assert parser.parse_args(["serve"]).port == 18317


def test_local_server_function_defaults_match_cli_port():
    assert inspect.signature(local_server.build_server).parameters["port"].default == 18317
    assert inspect.signature(local_server.serve).parameters["port"].default == 18317


def test_local_server_result_status_marker_is_not_returned_in_body():
    status, body = local_server._status_body_from_result(
        {"_status": 409, "ok": False, "error": "session_turn_in_progress"}
    )

    assert status == 409
    assert body == {"ok": False, "error": "session_turn_in_progress"}


def test_local_server_json_safe_replaces_non_finite_numbers():
    body = local_server._json_safe(
        {
            "ok": True,
            "metric": math.inf,
            "nested": [{"value": -math.inf}, {"value": math.nan}, {"value": 1.5}],
        }
    )

    assert body == {
        "ok": True,
        "metric": None,
        "nested": [{"value": None}, {"value": None}, {"value": 1.5}],
    }
    assert "Infinity" not in json.dumps(body, allow_nan=False)


def test_local_server_request_clients_are_thread_local(tmp_path):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    main_client = local_server._client_for_current_thread(cfg)
    first = main_client.triggers.emit(
        source="test",
        kind="thread.check",
        target="main",
        strategy_id="thread_strategy",
        idempotency_key="main-thread",
    )
    assert first["status"] == "routed"

    result: dict[str, object] = {}

    def run_in_worker() -> None:
        worker_client = local_server._client_for_current_thread(cfg)
        result["same_client"] = worker_client is main_client
        result["same_skills"] = worker_client.skills is main_client.skills
        result["emit"] = worker_client.triggers.emit(
            source="test",
            kind="thread.check",
            target="main",
            strategy_id="thread_strategy",
            idempotency_key="worker-thread",
        )

    worker = threading.Thread(target=run_in_worker)
    worker.start()
    worker.join(timeout=5)

    assert worker.is_alive() is False
    assert result["same_client"] is False
    assert result["same_skills"] is True
    assert result["emit"]["status"] == "routed"  # type: ignore[index]
