"""On-demand, workspace-local SDK environments; no provider-specific imports."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import venv

from ..core.atomic_write import atomic_write_text
from ..core.ids import snapshot_id
from ..trading.asset_observations import normalize_observation
from .dep_installer import is_auto_install_allowed


class SDKError(ValueError):
    pass


def root(config):
    return config.paths.root / 'runtime' / 'sdk_accounts'


def path_for(config, identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', identifier):
        raise SDKError('invalid_sdk_connector_id')
    path = root(config) / identifier
    path.resolve().relative_to(config.paths.root.resolve())
    if path.is_symlink():
        raise SDKError('sdk_path_symlink_refused')
    return path


def save(path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, allow_nan=False))
    path.chmod(0o600)


def load(path):
    if path.is_symlink():
        raise SDKError('sdk_state_symlink_refused')
    return json.loads(path.read_text()) if path.is_file() else None


@contextmanager
def lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.lock').open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.write(b'0'); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def process_env(config):
    keys = {'PATH', 'HOME', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'TEMP', 'TMP', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}
    env = {key: value for key, value in os.environ.items() if key.upper() in keys}
    from ..core.proxy import proxy_env_for_workspace
    env.update(proxy_env_for_workspace(config.paths.root))
    return env


def run(command, *, cwd, env, timeout=600, input=None):
    try:
        result = subprocess.run(command, cwd=cwd, env=env, input=input, text=True,
                                capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SDKError('sdk_process_' + type(exc).__name__) from exc
    if result.returncode:
        # SDK output can contain credential-bearing URLs. Keep failure codes,
        # never arbitrary stdout/stderr, in UI state and Agent tool responses.
        missing = re.search(r"ModuleNotFoundError: No module named ['\"]([A-Za-z0-9_.-]+)['\"]", result.stderr or '')
        if missing:
            raise SDKError('sdk_module_missing:' + missing.group(1))
        raise SDKError(f'sdk_process_exit_{result.returncode}')
    return result.stdout


def validate_requirements(runtime, requirements):
    if not isinstance(requirements, list) or len(requirements) > 20:
        raise SDKError('sdk_requirements_invalid')
    pattern = (r'[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.+-]*' if runtime == 'python'
               else r'(?:@[a-z0-9_-]+/)?[a-z0-9][a-z0-9_.-]*@[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?')
    if any(not isinstance(item, str) or not re.fullmatch(pattern, item) for item in requirements):
        raise SDKError('sdk_dependencies_require_exact_registry_versions')
    return sorted(set(requirements))


def install_environment(config, runtime, requirements, directory):
    env = process_env(config)
    if runtime == 'python':
        target = directory / 'env'
        venv.EnvBuilder(with_pip=bool(requirements)).create(target)
        interpreter = target / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not requirements:
            return str(interpreter)
        run([str(interpreter), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
             '--index-url', 'https://pypi.org/simple', *requirements], cwd=directory, env=env)
        run([str(interpreter), '-m', 'pip', 'check'], cwd=directory, env=env, timeout=30)
        return str(interpreter)
    node, npm = shutil.which('node'), shutil.which('npm')
    if not node or not npm:
        raise SDKError('sdk_node_runtime_required')
    if requirements:
        save(directory / 'package.json', {'private': True, 'name': 'nerya-sdk-account'})
        run([npm, 'install', '--no-audit', '--no-fund', '--prefix', str(directory), *requirements],
            cwd=directory, env=env)
    return node


def revision_for(source, record):
    shape = {key: record.get(key) for key in ('runtime', 'requirements', 'skill_id', 'script', 'payload', 'env_refs')}
    return hashlib.sha256(source + json.dumps(shape, sort_keys=True).encode()).hexdigest()


def shared_environment(config, runtime, requirements):
    key = hashlib.sha256(json.dumps([runtime, requirements], sort_keys=True).encode()).hexdigest()
    directory = root(config) / '.environments' / key
    directory.resolve().relative_to(config.paths.root.resolve())
    with lock(directory):
        receipt = load(directory / 'environment.json')
        if receipt and Path(receipt['interpreter']).is_file():
            return receipt['interpreter']
        interpreter = install_environment(config, runtime, requirements, directory)
        save(directory / 'environment.json', {'runtime': runtime, 'requirements': requirements,
                                              'interpreter': interpreter, 'created_at': time.time()})
        return interpreter


def prepare(config, spec, *, script_path, approve=False):
    identifier = spec['connector_id']
    directory = path_for(config, identifier)
    runtime = spec.get('runtime', 'python')
    if runtime not in {'python', 'node'}:
        raise SDKError('unsupported_sdk_runtime')
    script_path = Path(script_path)
    if not script_path.is_file() or script_path.is_symlink() or script_path.stat().st_size > 256_000:
        raise SDKError('sdk_script_invalid')
    if script_path.suffix not in ({'.py'} if runtime == 'python' else {'.js', '.mjs', '.cjs'}):
        raise SDKError('sdk_script_runtime_mismatch')
    requirements = validate_requirements(runtime, spec.get('requirements', []))
    refs = spec.get('env_refs') or {}
    if not isinstance(refs, dict) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', key)
       or not isinstance(value, str) or not value.startswith('vault://') for key, value in refs.items()):
        raise SDKError('sdk_environment_requires_vault_refs')
    payload = spec.get('payload') or {}
    if not isinstance(payload, dict):
        raise SDKError('sdk_payload_must_be_object')
    serialized = json.dumps(payload, allow_nan=False)
    if len(serialized.encode()) > 64_000:
        raise SDKError('sdk_payload_too_large')
    def public_only(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if re.search(r'(?i)(api[_-]?key|password|passphrase|private[_-]?key|mnemonic|seed|secret|authorization|cookie|access[_-]?token|refresh[_-]?token)', key):
                    raise SDKError('sdk_credentials_must_use_env_refs')
                public_only(item)
        elif isinstance(value, list):
            for item in value:
                public_only(item)
    public_only(payload)
    source = script_path.read_bytes()
    binding = hashlib.sha256(json.dumps({'payload': spec.get('payload') or {}, 'env_refs': refs}, sort_keys=True).encode()).hexdigest()
    revision = revision_for(source, {**spec, 'runtime': runtime, 'requirements': requirements,
                                     'payload': spec.get('payload') or {}, 'env_refs': refs})
    with lock(directory):
        previous = load(directory / 'manifest.json')
        if previous and previous['revision'] == revision and previous.get('phase') == 'ready' and Path(previous.get('interpreter', '')).is_file():
            return {'ok': True, 'connector': public_state(config, previous), 'reused': True}
        record = {'connector_id': identifier, 'label': str(spec.get('label') or identifier)[:128],
                  'runtime': runtime, 'requirements': requirements, 'revision': revision,
                  'binding': binding,
                  'needs_refresh': True,
                  'skill_id': spec['skill_id'], 'script': spec['script'], 'payload': spec.get('payload') or {},
                  'env_refs': refs, 'phase': 'preparing', 'updated_at': time.time()}
        save(directory / 'manifest.json', record)
        allowed, _ = is_auto_install_allowed(config.data, approve=approve)
        if requirements and not allowed:
            record.update(phase='installation_required', error='auto_install_not_enabled')
            save(directory / 'manifest.json', record)
            return {'ok': False, 'error': 'installation_required', 'connector': public_state(config, record)}
        target = directory / 'revisions' / revision
        target.mkdir(parents=True, exist_ok=True)
        target.resolve().relative_to(directory.resolve())
        adapter = target / ('adapter' + script_path.suffix)
        adapter.write_bytes(source)
        try:
            interpreter = shared_environment(config, runtime, requirements)
            if runtime == 'node' and requirements:
                # ESM resolves packages beside the adapter, not through NODE_PATH.
                environment = root(config) / '.environments' / hashlib.sha256(
                    json.dumps([runtime, requirements], sort_keys=True).encode()).hexdigest()
                node_modules = target / 'node_modules'
                if not node_modules.exists():
                    node_modules.symlink_to(environment / 'node_modules', target_is_directory=True)
            record.update(phase='ready', interpreter=interpreter, adapter=str(adapter))
            record.pop('error', None)
        except Exception as exc:
            record.update(phase='error', error=str(exc) if isinstance(exc, SDKError) else type(exc).__name__)
        record['updated_at'] = time.time()
        save(directory / 'manifest.json', record)
        return {'ok': record['phase'] == 'ready', 'connector': public_state(config, record), 'error': record.get('error')}


def collect(config, identifier):
    directory = path_for(config, identifier)
    with lock(directory):
        record = load(directory / 'manifest.json')
        if not record or record.get('phase') != 'ready':
            raise SDKError('sdk_connection_not_prepared')
        target = Path(record['adapter'])
        target.resolve().relative_to(directory.resolve())
        if target.is_symlink() or not target.is_file():
            raise SDKError('sdk_adapter_revision_missing')
        revision = revision_for(target.read_bytes(), record)
        if revision != record['revision']:
            raise SDKError('sdk_adapter_revision_changed')
        env = process_env(config)
        try:
            if record.get('env_refs'):
                from ..security.secrets import SecretVault
                vault = SecretVault.open(config.paths.vault_enc)
                for name, ref in record['env_refs'].items():
                    meta = vault.meta(ref.removeprefix('vault://'))
                    if meta.kind in {'private_key', 'signer', 'mnemonic', 'seed'}:
                        raise SDKError('sdk_signing_secret_not_allowed')
                    env[name] = vault.resolve(ref.removeprefix('vault://'), required_scope='sdk.read')
            output = run([record['interpreter'], str(target)], cwd=target.parent, env=env, timeout=45,
                         input=json.dumps({'protocol_version': 1, 'command': 'account_state', 'payload': record['payload']}))
            raw = json.loads(output.strip())
            if len(output.encode()) > 2_000_000:
                raise SDKError('sdk_response_too_large')
            if isinstance(raw, dict) and raw.get('error'):
                code = raw['error'].get('code') if isinstance(raw['error'], dict) else None
                allowed = {'auth_required','rate_limited','sdk_missing','rpc_unavailable','unsupported_operation',
                           'wrong_network','permission_denied','connection_error'}
                raise SDKError('sdk_provider_' + (code if code in allowed else 'connection_error'))
            if record['payload'].get('address') and raw.get('address') != record['payload']['address']:
                raise SDKError('sdk_account_owner_mismatch')
            observed = normalize_observation(raw, connector_id=identifier, source={
                'kind': 'sdk', 'skill_id': record['skill_id'], 'script': record['script'],
                'revision': revision, 'binding': record['binding'], 'requirements': record['requirements'],
                'evidence_hash': hashlib.sha256(output.encode()).hexdigest()})
            observed['observation_id'] = snapshot_id()
            save(directory / 'observation.json', observed)
            record.pop('collection_error', None)
            record['needs_refresh'] = False
        except Exception as exc:
            from ..trading.asset_observations import ObservationError
            record['collection_error'] = str(exc) if isinstance(exc, (SDKError, ObservationError)) else type(exc).__name__
            record['last_attempt_at'] = time.time()
            save(directory / 'manifest.json', record)
            raise SDKError('sdk_collection_failed:' + record['collection_error']) from exc
        finally:
            env.clear()
        record['last_attempt_at'] = time.time()
        save(directory / 'manifest.json', record)
        return {'ok': True, 'connector': public_state(config, record)}


def public_state(config, record):
    observed = load(path_for(config, record['connector_id']) / 'observation.json')
    if observed and observed['source'].get('binding') != record.get('binding'):
        observed = None
    stale = bool(observed and (time.time() - observed['as_of'] > 300 or record.get('collection_error')
                              or record.get('phase') != 'ready' or record.get('needs_refresh')))
    return {key: record.get(key) for key in ('connector_id', 'label', 'runtime', 'requirements', 'revision',
            'skill_id', 'script', 'phase', 'error', 'collection_error', 'updated_at', 'last_attempt_at')} | {
            'observation': observed, 'stale': stale, 'live_verified': False}


def list_connections(config):
    items = []
    if root(config).exists():
        for directory in sorted(root(config).iterdir()):
            if directory.name.startswith('.') or not directory.is_dir() or directory.is_symlink():
                continue
            try:
                record = load(directory / 'manifest.json')
                if record:
                    if record.get('connector_id') != directory.name:
                        raise SDKError('sdk_connection_identity_mismatch')
                    items.append(public_state(config, record))
            except (SDKError, ValueError, KeyError, TypeError, OSError):
                items.append({'connector_id':directory.name,'label':directory.name,'runtime':'',
                    'requirements':[],'revision':'','skill_id':'','script':'','phase':'error',
                    'error':'sdk_connection_state_invalid','observation':None,'stale':False,'live_verified':False})
    return {'ok': True, 'connectors': items, 'count': len(items)}
