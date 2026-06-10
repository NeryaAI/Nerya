from __future__ import annotations

import pytest

from nerya.core import jsonl, yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.messaging.pipeline import MessagePipeline


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    yaml_io.dump(
        cfg.paths.messages_channels,
        {
            "channels": {
                "telegram": {"kind": "local"},
                "discord": {"kind": "local"},
            },
            "severity_routes": {
                "info": ["telegram"],
                "critical": ["telegram", "discord"],
                "silent": [],
            },
        },
    )
    return cfg


def test_message_pipeline_routes_by_severity(tmp_path):
    cfg = _config(tmp_path)
    pipeline = MessagePipeline(config=cfg)

    info = pipeline.send(channel="operator", text="info event", severity="info")
    critical = pipeline.send(channel="operator", text="critical event", severity="critical")

    assert info["channels"] == ["telegram"]
    assert critical["channels"] == ["telegram", "discord"]

    sent = [
        row
        for row in jsonl.read_all(cfg.paths.journal("messages"))
        if row.get("kind") == "message.sent"
    ]
    assert [(row["severity"], row["channel"]) for row in sent] == [
        ("info", "telegram"),
        ("critical", "telegram"),
        ("critical", "discord"),
    ]


def test_message_pipeline_suppresses_silent_severity(tmp_path):
    cfg = _config(tmp_path)
    pipeline = MessagePipeline(config=cfg)

    result = pipeline.send(channel="operator", text="silent event", severity="silent")

    assert result["suppressed"] is True
    assert result["channels"] == []
    rows = jsonl.read_all(cfg.paths.journal("messages"))
    assert [row.get("kind") for row in rows] == ["message.suppressed"]
    assert not list(cfg.paths.outbox_messages.glob("*.json"))
