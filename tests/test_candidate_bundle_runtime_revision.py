"""Runtime output must not invalidate reviewed workspace source."""
from pathlib import Path

import pytest

from nerya.evolution.candidate_bundle import workspace_revision


@pytest.mark.smoke
def test_revision_ignores_runtime_output_but_tracks_source(tmp_path, monkeypatch):
    source = ('nerya.yml', 'strategies/observer/main.py', 'approvals/kill_switch.yml',
              'strategies/observer/nerya.db')
    runtime = ('nerya.db', 'nerya.db-wal', 'nerya.db-shm', 'nerya.db-journal',
               'journals/agent.jsonl', 'approvals/pending.jsonl', 'approvals/approved.jsonl',
               'approvals/rejected.jsonl', 'approvals/.pending.jsonl.lock',
               'approvals/.approved.jsonl.lock', 'approvals/.rejected.jsonl.lock')
    for relative in (*source, *runtime):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('original')
    revision = workspace_revision(tmp_path)
    for relative in runtime:
        (tmp_path / relative).write_text('runtime wrote again')
    assert workspace_revision(tmp_path) == revision
    read_bytes = Path.read_bytes
    def read_source_only(path):
        assert path.relative_to(tmp_path).as_posix() not in runtime
        return read_bytes(path)
    monkeypatch.setattr(Path, 'read_bytes', read_source_only)
    assert workspace_revision(tmp_path) == revision
    for relative in source:
        (tmp_path / relative).write_text('operator edit')
        changed = workspace_revision(tmp_path)
        assert changed != revision, relative
        revision = changed
