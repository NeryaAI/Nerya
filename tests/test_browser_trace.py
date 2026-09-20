from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from nerya.integrations import browser_trace as bt
from nerya.api import routes_browser_desktop, route_scopes

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def isolate():
    with bt._LOCK:
        previous = dict(bt._TRACES)
        bt._TRACES.clear()
    yield
    with bt._LOCK:
        bt._TRACES.clear()
        bt._TRACES.update(previous)


def test_all_steps_are_kept_and_cursor_is_incremental(tmp_path):
    trace = bt.create(tmp_path, 'chat-a', 'tool-1')
    for i in range(12):
        trace.emit('step', index=i, phase='started', action='click')
        trace.emit('step', index=i, phase='completed', action='click')
    result = bt.read(tmp_path, 'chat-a', 'tool-1')
    assert len([e for e in result['events'] if e.get('phase') == 'completed']) == 12
    assert bt.read(tmp_path, 'chat-a', 'tool-1', after=result['cursor'])['events'] == []
    assert 'token' not in result


def test_identity_does_not_fall_back_to_latest_profile(tmp_path):
    trace = bt.create(tmp_path, 'chat-a', 'tool-1')
    trace.frame('data:image/jpeg;base64,synthetic', url='https://example.test/a')
    assert bt.read(tmp_path, 'chat-b', 'tool-1')['frame'] is None
    assert bt.read(tmp_path, 'chat-a', 'tool-other')['frame'] is None
    assert bt.resolve(tmp_path / 'other', trace.token) is None


def test_historical_frames_keep_their_original_metadata(tmp_path):
    trace = bt.create(tmp_path, 'chat', 'call')
    first = trace.frame('image-a', url='https://example.test/a')
    trace.emit('step', index=0, frame_id=first)
    trace.frame('image-b', url='https://example.test/b')
    result = bt.read(tmp_path, 'chat', 'call', frame_id=first)
    assert result['frame']['image'] == 'image-a'
    assert result['frame']['url'].endswith('/a')


def test_pixels_not_written_to_metadata_and_history_survives_restart(tmp_path):
    trace = bt.create(tmp_path, 'chat', 'call')
    frame = trace.frame('AUTHENTICATED_PIXELS', url='https://example.test')
    trace.emit('step', action='fill', index=0, phase='completed', frame_id=frame)
    trace.finish('completed')
    path = next((tmp_path / 'state/browser_traces').glob('*.json'))
    assert 'AUTHENTICATED_PIXELS' not in path.read_text()
    bt._TRACES.clear()
    result = bt.read(tmp_path, 'chat', 'call')
    assert result['status'] == 'completed' and result['frame_state'] == 'expired'
    assert any(e.get('action') == 'fill' for e in result['events'])


def test_takeover_hides_frames_without_worker_job(tmp_path):
    trace = bt.create(tmp_path, 'chat', 'call')
    worker = SimpleNamespace(paused=False, interrupted=threading.Event(), done=threading.Event(),
                             agent_controller=SimpleNamespace(session_id='mb_one'))
    trace.owner = worker
    trace.session_id = 'mb_one'
    trace.frame('image')
    assert bt.read(tmp_path, 'chat', 'call')['frame']
    worker.interrupted.set()
    state = bt.read(tmp_path, 'chat', 'call')
    assert state['paused'] and state['frame'] is None and state['controllable']
    worker.agent_controller.session_id = 'mb_other'
    assert not bt.read(tmp_path, 'chat', 'call')['controllable']


def test_memory_limit_expires_frames_not_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, '_MAX_BYTES', 10)
    trace = bt.create(tmp_path, 'chat', 'call')
    trace.emit('step', index=0, phase='completed')
    trace.frame('a' * 30)
    assert trace.frame_bytes == 0
    assert any(e['kind'] == 'step' for e in bt.read(tmp_path, 'chat', 'call')['events'])


def test_read_only_tool_token_cannot_read_operator_pixels():
    assert not route_scopes.authorize(['write:tools'], 'POST', '/browsers/desktop')[0]
    assert not route_scopes.authorize(['read:runtime'], 'POST', '/browsers/desktop')[0]
    assert route_scopes.authorize(['admin:ops'], 'POST', '/browsers/desktop')[0]


def test_trace_api_does_not_call_worker_for_observation(tmp_path, monkeypatch):
    trace = bt.create(tmp_path, 'chat', 'call')
    trace.emit('step', index=0, phase='started')
    monkeypatch.setattr(routes_browser_desktop.browser, 'worker_for', lambda *a: pytest.fail('read must not wait in browser queue'))
    handler = next(h for method, path, h in routes_browser_desktop.routes() if method == 'POST' and path == '/browsers/desktop')
    result = handler(SimpleNamespace(config=SimpleNamespace(paths=SimpleNamespace(root=tmp_path))),
                     {'operation':'trace','conversation_id':'chat','call_id':'call'})
    assert result['ok'] and result['events'][-1]['phase'] == 'started'


def test_unavailable_producer_marks_interrupted_after_restart(tmp_path):
    trace = bt.create(tmp_path, 'chat', 'call')
    trace.status = 'running'
    trace.emit('step', index=0, phase='started')
    bt._TRACES.clear()
    assert bt.read(tmp_path, 'chat', 'call')['status'] == 'interrupted'


def test_trace_directory_symlink_rejected(tmp_path):
    (tmp_path / 'outside').mkdir()
    (tmp_path / 'state').symlink_to(tmp_path / 'outside', target_is_directory=True)
    trace = bt.create(tmp_path, 'chat', 'call')
    assert trace.storage_error
    assert not list((tmp_path / 'outside').iterdir())


def test_parent_conversation_comes_from_executor_metadata_not_arguments(tmp_path, monkeypatch):
    from nerya.tools.native import bootstrap
    from nerya.tools.types import ToolCall
    seen = []
    monkeypatch.setattr(bootstrap, 'script_run_handler', lambda call, **kw: seen.append(kw['conversation_id']))
    deps = SimpleNamespace(skill_index=None, workspace_root=tmp_path, active_session_id='child-session')
    call = ToolCall(name='script_run', arguments={'conversation_id':'spoofed'},
                    metadata={'agent_parent_session_id':'parent-chat'})
    bootstrap._wrap_script_run(deps)(call)
    assert seen == ['parent-chat']


def test_unvalidated_session_does_not_capture_or_expose_current_browser(tmp_path):
    trace = bt.create(tmp_path, 'chat', 'call')
    worker = SimpleNamespace(config={'id':'work'}, paused=False, interrupted=threading.Event())
    controller = SimpleNamespace(worker=worker, session_id='someone-elses-session', actor='same-shared-actor')
    recorder = bt.Recorder(controller, trace, 'same-shared-actor')
    assert not recorder.safe() and recorder.metadata() == {}
    assert trace.owner is None and not trace.session_id
    recorder.finish({'ok':False})
    assert trace.status == 'failed' and not trace.frames


def test_preview_fault_preserves_actual_action_outcome(tmp_path, monkeypatch):
    trace = bt.create(tmp_path, 'chat', 'call')
    worker = SimpleNamespace(config={'id':'work'}, paused=False, interrupted=threading.Event())
    controller = SimpleNamespace(worker=worker, session_id='session', actor='actor')
    recorder = bt.Recorder(controller, trace, 'actor')
    def broken(*args): raise RuntimeError('synthetic preview failure')
    monkeypatch.setattr(recorder, '_step', broken)
    monkeypatch.setattr(recorder, 'checkpoint', broken)
    recorder.step(0, {'action':'click'}, 'completed')
    recorder.finish({'ok':True})
    assert trace.status == 'completed'
    assert any(e.get('phase') == 'completed' and e.get('preview_unavailable') for e in trace.events)
