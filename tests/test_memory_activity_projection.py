"""Focused coverage for memory activity scrubbing and compatibility projections."""

from __future__ import annotations

import json

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


def _config(tmp_path):  # noqa: ANN001
    return Config(paths=WorkspacePaths(root=tmp_path), data={})


def _write_events(path, events):  # noqa: ANN001
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )


def test_activity_scrub_covers_live_and_all_numeric_rotations(tmp_path):
    from nerya.memory.activity import MemoryActivityLog

    log = MemoryActivityLog(_config(tmp_path), keep_rotations=2)
    live = log.path
    rotation_one = live.with_name(live.name + ".1")
    old_rotation = live.with_name(live.name + ".9")
    _write_events(
        live,
        [
            {
                "kind": "write_ok",
                "actor_id": "alice",
                "key": "private.preference",
                "hash": "hash-key",
                "preview": "alice secret by key",
            },
            {
                "kind": "write_ok",
                "actor_id": "bob",
                "key": "private.preference",
                "hash": "hash-key",
                "preview": "bob must remain",
            },
        ],
    )
    _write_events(
        rotation_one,
        [
            {
                "kind": "write_ok",
                "actor_id": "alice",
                "key": "",
                "hash": "hash-only",
                "preview": "alice secret by hash",
            },
            {
                "kind": "search",
                "actor_id": "alice",
                "key": "unrelated",
                "hash": "keep",
                "query": "keep this event",
            },
        ],
    )
    _write_events(
        old_rotation,
        [
            {
                "kind": "write_skipped",
                "actor_id": "alice",
                "key": "",
                "hash": "other",
                "preview": "alice secret through extra key",
                "extra": {"key": "private.preference"},
            },
        ],
    )
    with old_rotation.open("ab") as handle:
        handle.write(b"not-json\n")

    removed = log.scrub(
        actor_id="alice",
        key="private.preference",
        hashes={"hash-only"},
    )

    assert removed == 3
    combined = "".join(
        path.read_text(encoding="utf-8") for path in (live, rotation_one, old_rotation)
    )
    assert "alice secret" not in combined
    assert "bob must remain" in combined
    assert "keep this event" in combined
    assert "not-json" in combined
    assert (
        log.scrub(
            actor_id="alice",
            key="private.preference",
            hashes="hash-only",
        )
        == 0
    )


def test_activity_search_never_persists_a_plaintext_secret(tmp_path):
    from nerya.memory.activity import MemoryActivityEvent, MemoryActivityLog

    log = MemoryActivityLog(_config(tmp_path))
    secret = "api_key=sk-direct-activity-secret-value-1234567890"

    log.append(
        MemoryActivityEvent.search(
            query=secret,
            result_count=0,
            actor_id="operator-1",
        )
    )

    raw = log.path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "[redacted unsafe memory query]" in raw


def test_projection_jsonl_excludes_session_and_carries_actor_marker(tmp_path):
    from nerya.memory.projection import (
        GENERATED_PROJECTION_MARKER,
        MemoryProjection,
    )
    from nerya.memory.store import MemoryStore

    config = Config(
        paths=WorkspacePaths(root=tmp_path),
        data={"memory": {"legacy_owner_actor": "operator-1"}},
    )
    store = MemoryStore(config.paths.db)
    common = {
        "actor_id": "operator-1",
        "writer_id": "test",
        "strategy_id": "",
        "category": "learning",
    }
    store.remember(
        **common,
        scope="global",
        scope_id="",
        session_id="",
        content="global projected fact",
        stable_key="global.fact",
        target_files=["memory/global.md"],
    )
    store.remember(
        **common,
        scope="session",
        scope_id="session-1",
        session_id="session-1",
        content="session private fact",
        stable_key="session.fact",
        target_files=[],
    )

    MemoryProjection(config, store).sync(actor_id="operator-1")

    rows = [
        json.loads(line)
        for line in config.paths.memory_index.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["actor_id"] == "operator-1"
    assert rows[0]["scope"] == "global"
    assert "session private fact" not in config.paths.memory_index.read_text(
        encoding="utf-8"
    )
    markdown = (config.paths.memory / "global.md").read_text(encoding="utf-8")
    assert GENERATED_PROJECTION_MARKER in markdown
    assert "do-not-import" in markdown


def test_projection_lock_uses_msvcrt_when_fcntl_is_unavailable(
    tmp_path,
    monkeypatch,
):
    import nerya.memory.projection as projection_module

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def locking(self, _fd: int, mode: int, size: int) -> None:
            self.calls.append((mode, size))

    fake = FakeMsvcrt()
    monkeypatch.setattr(projection_module, "_fcntl", None)
    monkeypatch.setattr(projection_module, "_msvcrt", fake)
    lock_path = tmp_path / ".projection.lock"

    with projection_module._file_lock(lock_path):
        assert lock_path.read_text(encoding="utf-8") == " "

    assert fake.calls == [(fake.LK_LOCK, 1), (fake.LK_UNLCK, 1)]
