"""Restart recovery skips tombstoned history and still recovers live work."""
import pytest

from nerya.agent.assistant_work import AssistantWork
from nerya.agent.assistants import AssistantStore
from nerya.agent.command_runtime import CommandRuntime
from nerya.agent.goals import recover
from nerya.tools.types import ToolCall
from test_conversation_commands import config, payload


@pytest.mark.smoke
def test_recover_skips_deleted_sessions_without_blocking_live_work(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    manager = CommandRuntime(cfg, lambda *_: {})
    kicks = []
    monkeypatch.setattr(manager, 'kick', kicks.append)
    work = AssistantWork(cfg.paths)
    parent = AssistantStore(cfg.paths).home('main')['session_id']
    tasks = [work.create(cfg, ToolCall(id=name, turn_id='original-turn',
        name='assistant_task_create', arguments={'title': name, 'prompt': 'Inspect evidence.'}),
        parent, 'operator', manager=manager) for name in ('deleted-task', 'live-task')]
    for sid in ('deleted-goal', 'live-goal'):
        request = payload(cid='command-' + sid)
        request['session_id'] = sid
        request['request']['work_mode'] = 'goal'
        manager.submit(request, start=False)
    deleted = (tasks[0]['task_session_id'], 'deleted-goal')
    with manager.store.transaction() as con:
        con.executemany('INSERT INTO agent_deleted_sessions(session_id,deleted_at) VALUES (?,1)',
                        [(sid,) for sid in deleted])
        before = [tuple(row) for row in con.execute(
            'SELECT * FROM agent_command_queues WHERE session_id IN (?,?) ORDER BY session_id', deleted)]
    kicks.clear()
    queued = []
    original_queue = manager.store._queue
    def queue(con, sid):
        queued.append(sid)
        return original_queue(con, sid)
    monkeypatch.setattr(manager.store, '_queue', queue)
    work.recover(manager)
    recover(manager)
    assert kicks == [tasks[1]['task_session_id']]
    assert 'live-goal' in queued
    assert not set(deleted) & set(queued)
    with manager.store.transaction() as con:
        after = [tuple(row) for row in con.execute(
            'SELECT * FROM agent_command_queues WHERE session_id IN (?,?) ORDER BY session_id', deleted)]
        assert before == after
        assert con.execute("SELECT status FROM agent_goals WHERE session_id='live-goal'").fetchone()[0] == 'active'
