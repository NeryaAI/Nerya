"""Freeform adapter for the existing candidate-validation OS boundary.

macOS only; unavailable isolation fails closed. History must be prepared in
the package before execution. No gateway/configuration or network preparation
runs here. CPU/file/fd limits are hard; RSS and aggregate writable storage are
sampled every 50ms, as in candidate validation. The initial interpreter can
re-exec itself under the same sandbox; new child processes are denied.
"""
from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import sysconfig
import tempfile
import time

from .....core.errors import TradingError
from .....core.sandbox import sandbox_exec
from .....harness.cancellation import CancelledError, raise_if_cancelled
from .....strategies import candidate_validation as boundary
from .....strategies.package import _hash_files, load_package_from_dir
from .....strategies.validator import _validate_loaded

_ENV_PATHS = {'NERYA_BACKTEST_OUT_DIR', 'NERYA_STRATEGY_ROOT',
              'NERYA_WORKSPACE', 'NERYA_FREEFORM_BACKTEST'}
_EXCLUDED = {'secrets', 'vault', 'accounts', 'runs', 'state', 'versions',
             'reviews', 'sessions', 'agent_tasks', '__pycache__'}


def _files(package, script, artifact_names):
    names = set(package.files)
    # The ordinary package hash excludes backtests and history altogether.
    # Add prepared history and authored replay helpers, never old run outputs.
    for folder in ('backtests', 'history'):
        for directory, dirs, files in os.walk(package.root / folder, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in _EXCLUDED
                       and not re.fullmatch(r'(?:.*_)?[0-9]{8}_[0-9]{6}', d)]
            for name in files:
                path = Path(directory) / name
                if folder == 'backtests' and name in artifact_names:
                    continue
                names.add(path.relative_to(package.root).as_posix())
    names.add(script.relative_to(package.root).as_posix())
    return tuple(sorted(name for name in names
                        if not any(p.startswith('.') or p in _EXCLUDED for p in Path(name).parts)))


def _path_environment_only(path):
    """Retain the documented legacy output-path API, not arbitrary env reads."""
    tree = ast.parse(path.read_text())
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'os':
            if node.attr == 'getenv':
                return False
            if node.attr == 'environ':
                parent = parents.get(node)
                if not (isinstance(parent, ast.Subscript) and parent.value is node
                        and isinstance(parent.ctx, ast.Load)
                        and isinstance(parent.slice, ast.Constant) and parent.slice.value in _ENV_PATHS):
                    return False
    return True


def _read_artifact(root, name):
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
    finally:
        os.close(directory)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('replay artifact must be a regular, unlinked file')
        value = stream.read(boundary._FILE_BYTES + 1)
    if len(value) > boundary._FILE_BYTES:
        raise ValueError('replay artifact exceeds byte limit')
    return value


def uses_host_lp_replay(package, script):
    template = Path(__file__).parents[2] / 'strategy_author/templates/lp_sampled_replay.py.j2'
    return (template.is_file() and script.read_bytes() == template.read_bytes()
            and isinstance(package.manifest.extras.get('params', {}).get('lp_replay'), dict))


def execute_freeform(package, script, *, artifact_names, timeout_seconds, cancel_token=None, prepared_inputs=None,
                     trusted_lp_cadence=None):
    """Return bounded bytes and process facts; never import candidate code."""
    if sys.platform != 'darwin' or not boundary._SANDBOX.is_file():
        raise TradingError('macOS sandbox-exec is required; freeform replay was not executed')
    if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise TradingError('freeform timeout must be finite and positive')
    timeout = min(timeout_seconds, boundary._WALL_SECONDS)
    raise_if_cancelled(cancel_token)
    # Exact installed script is only a route selector. Candidate payloads and
    # receipts never grant execution verification; host accounting does.
    if uses_host_lp_replay(package, script):
        return _execute_sampled_lp(package, script, prepared_inputs or {}, cancel_token, timeout, trusted_lp_cadence)
    if trusted_lp_cadence is not None:
        raise TradingError('LP replay route changed after preparation')
    try:
        resident = boundary._memory_reader()
        with tempfile.TemporaryDirectory(prefix='nerya-freeform-') as temporary:
            base = Path(temporary).resolve()
            candidate, scratch = base / 'candidate', base / 'scratch'
            prepared = base / 'prepared_inputs'
            prepared.mkdir()
            input_hashes, input_bytes = {}, 0
            for name, blob in (prepared_inputs or {}).items():
                if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}[.]json', name) or not isinstance(blob, bytes):
                    raise ValueError('invalid prepared input')
                input_bytes += len(blob)
                if len(blob) > boundary._FILE_BYTES or input_bytes > boundary._SNAPSHOT_BYTES:
                    raise ValueError('prepared inputs exceed snapshot byte limit')
                (prepared / name).write_bytes(blob)
                input_hashes['prepared_inputs/' + name] = hashlib.sha256(blob).hexdigest()
            names = _files(package, script, artifact_names)
            boundary._snapshot(replace(package, files=names), candidate)
            if _hash_files(candidate, package.files) != package.content_hash:
                raise ValueError('source changed before snapshot; retry with current source')
            frozen_hash = _hash_files(candidate, names)
            snapshot_files = {name: hashlib.sha256((candidate / name).read_bytes()).hexdigest() for name in names}
            frozen = replace(load_package_from_dir(candidate), files=names, content_hash=frozen_hash)
            relative_script = script.relative_to(package.root).as_posix()
            validation = _validate_loaded(frozen, smoke_test=False, run_tests=False)
            blockers = [issue for issue in validation.issues if issue.severity == 'blocker'
                        and not (issue.code == 'env_access' and issue.where == relative_script
                                 and _path_environment_only(candidate / relative_script))]
            if blockers:
                raise TradingError('freeform structural validation failed: ' +
                                   ', '.join(sorted({issue.code for issue in blockers})))
            # Unique child of the copied package satisfies research._roots.
            (candidate / 'backtests').mkdir(exist_ok=True)
            output = Path(tempfile.mkdtemp(prefix='freeform_', dir=candidate / 'backtests'))
            scratch.mkdir()
            (base / 'worker.py').write_bytes(Path(__file__).with_name('freeform_worker.py').read_bytes())
            dependencies = sorted({str(Path(sysconfig.get_path(k)).resolve()) for k in ('purelib', 'platlib')})
            timezone_roots = boundary._timezone_roots()
            request = {'base': str(base), 'source': str(Path(__file__).resolve().parents[5]),
                       'dependencies': dependencies, 'script': relative_script,
                       'timezone_roots': [str(p) for p in timezone_roots],
                       'cpu_seconds': boundary._CPU_SECONDS, 'file_bytes': boundary._FILE_BYTES}
            (base / 'request.json').write_text(json.dumps(request))
            python = boundary._python_executable()
            policy = boundary._profile(base, python, dependencies, timezone_roots)
            policy += '\n(allow file-read* (subpath ' + json.dumps(str(prepared)) + '))'
            policy += '\n(allow file-read* file-write* (subpath ' + json.dumps(str(output)) + '))'
            profile = base / 'sandbox.sb'
            profile.write_text(policy)
            env = {'HOME': str(scratch), 'TMPDIR': str(scratch), 'PATH': '/usr/bin:/bin',
                   'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8',
                   'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1',
                   'MKL_NUM_THREADS': '1', 'VECLIB_MAXIMUM_THREADS': '1',
                   'NERYA_STRATEGY_ROOT': str(candidate), 'NERYA_BACKTEST_OUT_DIR': str(output),
                   'NERYA_WORKSPACE': str(scratch), 'NERYA_FREEFORM_BACKTEST': '1'}
            env['NERYA_REPLAY_INPUTS'] = str(prepared)
            command = [str(boundary._SANDBOX), '-f', str(profile), str(python), '-I', '-B', '-S',
                       str(base / 'worker.py'), str(base / 'request.json')]
            process = sandbox_exec(command, cwd=candidate, root=base, env=env, timeout=timeout,
                                   cancel_token=cancel_token, output_limit=boundary._OUTPUT_BYTES, background=True).process
            if process is None:
                raise RuntimeError('sandbox process manager returned no handle')
            limit, peak = None, 0
            try:
                while process.proc.poll() is None:
                    try:
                        peak = max(peak, resident(process.proc.pid))
                    except OSError:
                        if process.proc.poll() is None:
                            raise
                    sizes = [boundary._scratch_size(p) for p in (scratch, output)]
                    if peak > boundary._MEMORY_BYTES:
                        limit = 'sampled memory limit'
                    elif sum(s[0] for s in sizes) > boundary._SCRATCH_BYTES or sum(s[1] for s in sizes) > boundary._MAX_FILES:
                        limit = 'sampled scratch/output limit'
                    if limit:
                        process.stop()
                        break
                    time.sleep(0.05)
            finally:
                if sys.exc_info()[0] is not None:
                    process.stop()
                execution = process.wait()
            if execution.cancelled and not limit:
                error = CancelledError('freeform replay cancelled; worker stopped')
                error.result = execution
                raise error
            if execution.timed_out or limit:
                error = TradingError('freeform replay exceeded ' + (limit or 'wall-clock timeout') + '; worker stopped')
                error.result = execution
                raise error
            if not execution.process_exited or not execution.process_group_stopped:
                raise TradingError('freeform process cleanup was not confirmed')
            sizes = [boundary._scratch_size(p) for p in (scratch, output)]
            if sum(s[0] for s in sizes) > boundary._SCRATCH_BYTES or sum(s[1] for s in sizes) > boundary._MAX_FILES:
                raise ValueError('replay output exceeds aggregate limit')
            if _hash_files(candidate, names) != frozen_hash:
                raise ValueError('read-only snapshot changed during replay')
            if any(hashlib.sha256((base / name).read_bytes()).hexdigest() != digest
                   for name, digest in input_hashes.items()):
                raise ValueError('prepared inputs changed during replay')
            # Reuse bounded, no-follow copying for the post-run source check too.
            verified = base / 'verified'
            boundary._snapshot(replace(package, files=names), verified)
            if _hash_files(verified, names) != frozen_hash:
                raise ValueError('source changed during replay; result not accepted')
            artifacts = {}
            if execution.returncode == 0:
                for name in artifact_names:
                    data = _read_artifact(output, name)
                    if data is not None:
                        artifacts[name] = data
            safety = {'backend': 'macos-seatbelt', 'snapshot_sha256': frozen_hash,
                      'snapshot_files': snapshot_files, 'package_hash': package.content_hash,
                      'prepared_inputs': input_hashes,
                      'replay_script': relative_script, 'entrypoint': frozen.manifest.entrypoint,
                      'profile_sha256': hashlib.sha256(policy.encode()).hexdigest(),
                      'process_exited': execution.process_exited,
                      'process_group_stopped': execution.process_group_stopped,
                      'wall_seconds': timeout, 'peak_memory_bytes': peak,
                      'network': 'denied', 'process_spawn': 'denied',
                      'cpu_seconds': boundary._CPU_SECONDS, 'memory_bytes': boundary._MEMORY_BYTES,
                      'writable_bytes': boundary._SCRATCH_BYTES, 'file_bytes': boundary._FILE_BYTES,
                      'output_bytes_per_stream': boundary._OUTPUT_BYTES,
                      'output_truncated': execution.truncated,
                      'limitations': ['macOS only; no unsafe fallback',
                                      'RSS and aggregate writable limits are sampled every 50ms',
                                      'Initial interpreter may re-exec under the same sandbox',
                                      'Snapshot hashes identify bytes; no claim of data provenance or performance']}
            return execution, artifacts, safety
    except (OSError, ValueError) as exc:
        raise TradingError(f'freeform sandbox failed closed: {exc}') from exc


def _execute_sampled_lp(package, script, prepared_inputs, cancel_token, timeout, cadence=None):
    """Rate-replay pattern: isolated saved strategy, host-owned LP accounting."""
    import csv
    import io
    from types import SimpleNamespace
    from .....core import yaml_io
    from .....strategies.result import StrategyResult, StrategyResultStatus
    from .freeform_worker import lp_entrypoint_bridge
    from .isolated_strategy import IsolatedStrategy
    from .lp_replay import _State, _inputs, _cadence_inputs, cadence_receipt, simulate_lp_policy

    names = _files(package, script, ())
    files = {name: (package.root / name).read_text() for name in names}
    if files[script.relative_to(package.root).as_posix()] != (
            Path(__file__).parents[2] / 'strategy_author/templates/lp_sampled_replay.py.j2').read_text():
        raise ValueError('Sampled LP replay route changed before snapshot')
    snapshot_files = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}
    saved = yaml_io.loads(files['strategy.yml'])
    dataset_id = saved['params']['lp_replay']['dataset_id']
    blob = prepared_inputs[dataset_id + '.json']
    data = json.loads(blob)
    if cadence is None:
        _inputs(saved, data)  # Validate all observations before any candidate code.
    else:
        from .....strategies.verification import source_revision
        from .....strategies.workflow_service import _read_files
        source_files, omitted = _read_files(package.root)
        if (omitted or source_revision({name: files[name] for name in source_files}) != cadence['source_revision']
                or {k: v for k, v in data.items() if k != '_envelope'} != cadence_receipt(cadence)):
            raise ValueError('LP cadence source/input changed before execution')
        _cadence_inputs(saved, cadence)
    bridge = '_nerya_lp_entry.py'
    if bridge in files:
        raise ValueError('Reserved LP bridge source already exists')
    frozen = {name: text for name, text in files.items() if name.endswith('.py') or name == 'strategy.yml'}
    frozen[bridge] = lp_entrypoint_bridge(saved['entrypoint'])
    frozen['strategy.yml'] = yaml_io.dumps({**saved, 'entrypoint': bridge + ':run'})
    state = _State()
    def dispatch(method, args, kwargs):
        if method == 'state.get':
            return state.get(*args, **kwargs)
        if method == 'state.set':
            return state.set(*args, **kwargs)
        raise ValueError('Sampled LP strategy operation unavailable')
    worker = IsolatedStrategy(package, frozen_files=frozen, cancel_token=cancel_token,
        lifecycle_timeout=timeout, max_total_calls=(min(100_000, max(2000, len(cadence['decision_timestamps']) * 10))
                                                   if cadence is not None else 2000))
    def run(ctx):
        raise_if_cancelled(cancel_token)
        source = saved['params']['lp_source']['source_id']
        value = worker.run({'strategy_id': package.manifest.strategy_id, 'run_id': 'lp-replay',
            'mode': 'backtest', 'runmode': 'backtest', 'config': saved,
            'clock': {'ts': ctx.clock.now_ms() / 1000},
            'trigger': {'lp_snapshot': ctx.inputs.source(source)}}, dispatch)
        value = dict(value)
        value.pop('is_terminal', None)
        value['status'] = StrategyResultStatus(value['status'])
        return StrategyResult(**value)
    with worker:
        modeled = simulate_lp_policy(run, saved, data, trusted_cadence=cadence)
    if any((package.root / name).read_text() != text for name, text in files.items()):
        raise ValueError('Sampled LP source changed during replay')
    def csv_bytes(rows, fields):
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue().encode()
    equity = [{'ts': row['timestamp'], 'equity': (float(row['modeled_value_token1'])
        if cadence is not None else row['modeled_value_token1'])} for row in modeled['modeled_curve']]
    decisions = [{'ts': row['timestamp'], 'block_number': row['block_number'],
        'action': row['lp_decision']['action'], 'reason': row['lp_decision']['reason']} for row in modeled['decisions']]
    payload = {**modeled, 'accounting_currency': 'token1:' + modeled['quote_token'], 'equity_curve': equity, 'trades': []}
    if cadence is not None:
        # Full curves/decisions live once in CSV; verified archives own the
        # historical checkpoints. Do not duplicate a month of records in JSON.
        for key in ('modeled_curve', 'decisions', 'actions', 'observations', 'equity_curve'):
            payload.pop(key, None)
    artifacts = {'result.json': json.dumps(payload, allow_nan=False).encode(),
        'equity.csv': csv_bytes(equity, ['ts', 'equity']),
        'trades.csv': csv_bytes([], ['ts', 'side', 'size', 'price']),
        'decisions.csv': csv_bytes(decisions, ['ts', 'block_number', 'action', 'reason']),
        'report.md': ('# ' + ('Cadence' if cadence is not None else 'Sampled')
            + ' fixed-range LP research\n\nModeled actions: ' + str(modeled['actions_modeled'])
            + '\nQuote token: ' + modeled['quote_token']
            + '\nLeading gap seconds: ' + str(modeled['leading_gap_seconds'])
            + '\nTrailing gap seconds: ' + str(modeled['trailing_gap_seconds'])
            + '\nRequested window complete: ' + str(modeled['requested_window_complete']).lower()
            + '\nPerformance evidence: false'
            + '\nLimitations: ' + ', '.join(modeled['limitations'])).encode()}
    if any(len(blob) > boundary._FILE_BYTES for blob in artifacts.values()):
        raise ValueError('LP replay artifact exceeds byte limit')
    safety = {**worker.safety, 'snapshot_files': snapshot_files,
        'prepared_inputs': {'prepared_inputs/' + name: hashlib.sha256(value).hexdigest()
                            for name, value in prepared_inputs.items()},
        'lp_execution': {key: modeled[key] for key in ('scope', 'decision_count', 'interval_count',
            'dataset_id', 'action_counts', 'actions_modeled', 'allocation_modeled',
            'input_observations', 'admitted_observations', 'excluded_observations',
            'leading_gap_seconds', 'trailing_gap_seconds', 'gaps', 'limitations',
            'requested_window_complete', 'performance_evidence', 'financial_execution_allowed', 'funds_sent')},
        'entrypoint': saved['entrypoint']}
    safety['lp_execution'].update(entrypoint=saved['entrypoint'],
        prepared_input_sha256=hashlib.sha256(blob).hexdigest())
    if cadence is None:
        safety['lp_execution']['rows_sha256'] = data['_envelope']['rows_sha256']
    else:
        safety['lp_execution']['cadence_sha256'] = data['prepared_sha256']
    return SimpleNamespace(returncode=0, stdout='', stderr=''), artifacts, safety
