"""Explicit public replay snapshots anchored in durable producer events."""
from contextlib import closing
from itertools import islice
import hashlib
import json
import os
import re
import stat

from ..db.sqlite import connect_readonly
from ..tools.result_contracts import parse_compacted_kept_jsonish
from .types import DataApiError


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()


def _directory(config, *, create=False):
    fd = os.open(config.paths.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in ('state', 'replay_inputs'):
            if create:
                try:
                    os.mkdir(name, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def save(config, call, provider, action, args, output):
    """Save before row compaction; return an opaque ref, never a filesystem path."""
    if not config.get('runtime.command_id'):
        return None
    if not isinstance(output, dict) or not isinstance(output.get('rows'), list) or not isinstance(output.get('_envelope'), dict):
        return None
    cid = config.get('runtime.command_id')
    with closing(connect_readonly(config.paths.db)) as con:
        command = con.execute('SELECT * FROM agent_commands WHERE command_id=?', (cid,)).fetchone()
        if (not command or command['state'] != 'running'
                or command['session_id'] != config.get('runtime.command_session_id')
                or command['turn_id'] != config.get('runtime.command_turn_id')):
            raise DataApiError('Replay snapshot command binding unavailable', retryable=False)
        record = dict(version=1, command_id=cid, session_id=command['session_id'],
                      actor_id=command['actor_id'], turn_id=command['turn_id'], call_id=call.id,
                      provider=provider, action=action, args=args,
                      output={'rows': output['rows'], '_envelope': output['_envelope']})
    blob = encoded(record)
    if len(blob) > 4_000_000:
        raise DataApiError('Replay snapshot exceeds input byte limit', retryable=False)
    digest = hashlib.sha256(blob).hexdigest()
    directory = _directory(config, create=True)
    try:
        try:
            fd = os.open(digest + '.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(blob)
    finally:
        os.close(directory)
    return 'replay://' + digest


def read(config, ref, provider, action, args):
    """Require the exact snapshot hash in a successful same-task tool event."""
    return _read(config, ref, provider, action, args)


def _read(config, ref, provider, action, args, *, task=None):
    """Verify producer bytes/receipt; only discovery supplies durable task rows."""
    from ..strategies.request_scope import _snapshot
    try:
        if not re.fullmatch(r'replay://[0-9a-f]{64}', ref):
            raise ValueError('invalid ref')
        directory = _directory(config)
        try:
            fd = os.open(ref[9:] + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4_000_000:
                raise ValueError('invalid snapshot')
            blob = stream.read(4_000_001)
        if len(blob) > 4_000_000:
            raise ValueError('invalid snapshot')
        if hashlib.sha256(blob).hexdigest() != ref[9:]:
            raise ValueError('snapshot hash mismatch')
        record = json.loads(blob)
        if (record['version'] != 1 or record['provider'] != provider
                or record['action'] != action or record['args'] != args):
            raise ValueError('snapshot declaration mismatch')
        with closing(connect_readonly(config.paths.db)) as con:
            con.execute('BEGIN')
            if task is None:
                current = _snapshot(config, active=True, connection=con)['binding']
                commands = current['documents']
            else:
                current, commands = task
            admitted = {doc['command_id'] for doc in commands}
            if (record['command_id'] not in admitted
                    or record['session_id'] != current['session_id']
                    or record['actor_id'] != current['actor_id']):
                raise ValueError('snapshot task mismatch')
            events = con.execute(
                "SELECT payload_json FROM agent_command_events WHERE command_id=? AND "
                "json_extract(payload_json,'$.kind')='tool.complete' AND "
                "json_extract(payload_json,'$.call_id')=? LIMIT 2",
                (record['command_id'], record['call_id'])).fetchall()
            for row in events:
                event = json.loads(row[0])
                result = event.get('result')
                if isinstance(result, str):
                    result = parse_compacted_kept_jsonish(result) or json.loads(result)
                if (event.get('action') == 'data_api' and event.get('skill_id', 'native') == 'native'
                        and event.get('ok') is True and not event.get('error')
                        and event.get('session_id') == record['session_id']
                        and event.get('turn_id') == record['turn_id']
                        and isinstance(result, dict) and result.get('producer_ref') == ref):
                    return record['output']
            raise ValueError('producer attestation missing')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DataApiError('Explicit replay snapshot unavailable or not bound to this task',
                           retryable=False) from exc


def _snapshots(config, registry, *, provider='', action='', args=None, discovery=False):
    """Read this task's attested snapshots in newest-first order."""
    from ..strategies.request_scope import _snapshot
    with closing(connect_readonly(config.paths.db)) as con:
        con.execute('BEGIN')
        task = None
        if discovery:
            from ..tools.native.file_ops import _proposal_task_commands
            task = _proposal_task_commands(config, con)
            commands = task[1]
        else:
            commands = _snapshot(config, active=True, connection=con)['binding']['documents']
        for doc in reversed(commands):
            rows = con.execute(
                "SELECT payload_json FROM agent_command_events WHERE command_id=? AND "
                "json_extract(payload_json,'$.kind')='tool.complete' AND "
                "json_extract(payload_json,'$.action')='data_api' AND "
                "json_extract(payload_json,'$.ok')=1 ORDER BY id DESC",
                (doc['command_id'],))
            for row in rows:
                event = json.loads(row[0])
                result, request = event.get('result'), event.get('payload') or {}
                if isinstance(result, str):
                    result = parse_compacted_kept_jsonish(result) or json.loads(result)
                if not isinstance(result, dict) or not result.get('producer_ref'):
                    continue
                p, a = result.get('provider'), result.get('action')
                if provider and registry.resolve_provider(p) != registry.resolve_provider(provider):
                    continue
                if action and registry._action_name(p, a) != registry._action_name(provider or p, action):
                    continue
                source_args = request.get('args') or request.get('payload') or {}
                if args is not None and encoded(source_args) != encoded(args):
                    continue
                registry.admit_replay_input(p, a, source_args)
                data = _read(config, result['producer_ref'], registry.resolve_provider(p),
                             registry._action_name(p, a), source_args, task=task)
                yield dict(provider=p, action=a, args=source_args, producer_ref=result['producer_ref'],
                                  row_count=len(data['rows']), coverage=data['_envelope'].get('coverage'),
                                  requested_window=data['_envelope'].get('requested_window'),
                                  identity={key: data['_envelope'][key] for key in
                                      ('pool', 'token_identities', 'pool_identity_scope', 'row_contract', 'rows_sha256')
                                      if key in data['_envelope']}), data


def list_snapshots(config, registry, *, provider='', action=''):
    """Discover this task's producer refs from durable events, never a file scan."""
    with closing(_snapshots(config, registry, provider=provider, action=action, discovery=True)) as rows:
        return [record for record, _ in islice(rows, 8)]


def find_snapshot(config, registry, provider, action, args):
    """Reuse the newest attested result for the exact declared replay input."""
    registry.admit_replay_input(provider, action, args)
    with closing(_snapshots(config, registry, provider=provider, action=action, args=args)) as rows:
        match = next(rows, None)
    return (match[0]['producer_ref'], match[1]) if match is not None else None
