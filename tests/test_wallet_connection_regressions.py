"""Replay the connection failures from the Solana LP conversation offline."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from nerya.api import routes_wallet
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.llm.tool_compaction import compact_tool_result
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.types import ToolCall, ToolErrorKind
from nerya.wallet.errors import WalletTransportError
from nerya.wallet.providers.okx_os import OkxOsWallet
from nerya.wallet.providers.binance_agentic import BinanceAgenticWallet

pytestmark = pytest.mark.smoke


def _client(tmp_path):
    return SimpleNamespace(config=Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG)),
                           auth_actor_id='operator')


def _route(path):
    return next(handler for method, url, handler in routes_wallet.routes() if url == path and method == 'POST')


@pytest.mark.parametrize('provider', ['self_custody', 'metamask', 'okx_os', 'bitget', 'binance_agentic',
                                     'coinbase', 'byreal', 'evm_v2', 'external_adapter', 'binance', 'okx', 'bybit', 'deribit'])
def test_data_api_object_retains_provider_data(provider):
    output = {'provider': provider, 'action': 'pools', 'kind': 'object',
              'data': {'ok': True, 'count': 12, 'pools': [
                  {'address': f'pool{i}', 'tvl_usd': 1000, 'apr_24h': 8, 'padding': 'x' * 3000}
                  for i in range(12)], 'api_key': 'do-not-return'}}
    result = compact_tool_result('data_api', output)
    assert 'pool0' in str(result.kept)
    assert 'tvl_usd' in str(result.kept)
    assert 'do-not-return' not in str(result.kept)
    assert result.compacted_bytes < result.original_bytes / 2


def test_data_api_compaction_retains_nested_login_failure():
    output = {'provider': 'onchainos', 'action': 'wallet_status', 'kind': 'object',
              'data': {'ok': False, 'error': 'login_required', 'data': {'loggedIn': False}, 'padding': 'x' * 4000}}
    result = compact_tool_result('data_api', output)
    assert result.kept['data']['ok'] is False
    assert result.kept['data']['data']['loggedIn'] is False


@pytest.mark.parametrize('logged_in', [False, True])
def test_okx_readiness_checks_cli_login(monkeypatch, logged_in):
    monkeypatch.setattr(OkxOsWallet, '_onchainos_bin', lambda self: '/fake/onchainos')
    monkeypatch.setattr(OkxOsWallet, '_run_onchainos',
                        lambda self, args, **kw: {'ok': True, 'data': {'loggedIn': logged_in}})
    readiness = OkxOsWallet().readiness()
    assert readiness.ready is logged_in
    if not logged_in:
        assert any('login' in item for item in readiness.missing)
        assert '--phase' in readiness.install_hint


def test_okx_social_login_command_uses_current_cli():
    args, action, required = routes_wallet._auth_start_args('okx_os', {'chain': 'solana'})
    assert args == ['wallet', 'login', '--phase', 'init', '--chain', 'solana']
    assert action == 'wallet_popup_login'
    assert required == ['browser_login']
    args, error = routes_wallet._auth_verify_args('okx_os', {'auth_session_id': 'original-session'})
    assert args == ['wallet', 'login', '--phase', 'poll', '--session-id', 'original-session']
    assert error == ''


@pytest.mark.parametrize('provider', ['okx_os', 'coinbase', 'binance_agentic'])
def test_cli_exit_zero_does_not_mask_rejected_response(tmp_path, monkeypatch, provider):
    monkeypatch.setattr(routes_wallet, '_resolve_wallet_cli', lambda *args: (['/fake/cli'], None))
    monkeypatch.setattr(routes_wallet, '_wallet_cli_env', lambda *args: {})
    monkeypatch.setattr(routes_wallet, '_patch_coinbase_awal_windows', lambda *args: None)
    monkeypatch.setattr(routes_wallet.subprocess, 'run', lambda *args, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps({'ok': False, 'error': 'login_required'}), stderr=''))
    result = routes_wallet._run_wallet_cli(_client(tmp_path), provider, ['status'])
    assert result['ok'] is False


def test_okx_auth_reuses_pending_session_across_requests(tmp_path, monkeypatch):
    calls = []
    authenticated = False
    def run(client, provider, args, **kw):
        nonlocal authenticated
        calls.append(args)
        if args == ['wallet', 'status']:
            return {'ok': True, 'json': {'ok': True, 'data': {'loggedIn': authenticated}}}
        if '--phase' in args and args[args.index('--phase') + 1] == 'init':
            return {'ok': True, 'json': {'ok': True, 'data': {
                'authSessionId': 'original-session', 'loginUrl': 'https://web3.okx.com/login?session=original'}}}
        authenticated = True
        return {'ok': True, 'json': {'ok': True, 'data': {'loggedIn': True, 'currentAccountId': 'account1'}}}
    monkeypatch.setattr(routes_wallet, '_run_wallet_cli', run)
    client = _client(tmp_path)
    start = _route('/wallet/auth/start')
    first = start(client, {'provider': 'okx_os', 'install': False})
    second = start(_client(tmp_path), {'provider': 'okx_os', 'install': False})
    assert first['auth_session_id'] == second['auth_session_id'] == 'original-session'
    assert sum('init' in args for args in calls) == 1
    result = _route('/wallet/auth/verify')(_client(tmp_path), {'provider': 'okx_os'})
    assert result['ok'] is True
    assert ['wallet', 'login', '--phase', 'poll', '--session-id', 'original-session'] in calls
    assert ['wallet', 'add'] not in calls


def test_okx_status_does_not_bind_logged_out_wallet(tmp_path, monkeypatch):
    monkeypatch.setattr(routes_wallet, '_run_wallet_cli', lambda *args, **kw: {
        'ok': True, 'json': {'ok': True, 'data': {'loggedIn': False, 'accountCount': 0}}})
    monkeypatch.setattr(routes_wallet, '_ensure_provider_account', lambda *args: pytest.fail('must not create account'))
    monkeypatch.setattr(routes_wallet, '_maybe_save_auth_binding', lambda *args, **kw: pytest.fail('must not save binding'))
    result = _route('/wallet/auth/status')(_client(tmp_path), {'provider': 'okx_os', 'create_binding': True})
    assert result['authenticated'] is False


def test_data_api_nested_schema_rejects_invalid_argument_before_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(OkxOsWallet, '_run_onchainos', lambda *args, **kw: pytest.fail('invalid params reached CLI'))
    result = data_api_handler(ToolCall(name='data_api', arguments={
        'op': 'call', 'provider': 'onchainos', 'action': 'wallet_status', 'args': {'chain': 'solana'}}),
        config_like=_client(tmp_path).config)
    assert result.is_error
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    assert result.error.retryable is False


def test_binance_cli_install_does_not_claim_authenticated_readiness(monkeypatch):
    from nerya.wallet.providers import binance_cli
    monkeypatch.setattr(binance_cli, 'command', lambda provider: ['/fake/baw'])
    monkeypatch.setattr(binance_cli, 'call', lambda provider, args, **kw: {'loggedIn': False})
    assert BinanceAgenticWallet().readiness().ready is False


def test_okx_zero_exit_login_rejection_is_not_a_success(tmp_path, monkeypatch):
    monkeypatch.setattr(OkxOsWallet, '_onchainos_bin', lambda self: '/fake/onchainos')
    monkeypatch.setattr('nerya.wallet.providers.okx_os.subprocess.run', lambda *args, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps({'ok': False, 'error': 'You are not logged in'}), stderr=''))
    result = data_api_handler(ToolCall(name='data_api', arguments={
        'op': 'call', 'provider': 'onchainos', 'action': 'wallet_status'}), config_like=_client(tmp_path).config)
    assert result.is_error
    assert result.error.retryable is False
    assert result.error.recovery_hint['action'] == 'wallet_auth'


def test_native_wallet_auth_is_registered_and_resumes_original_session(tmp_path, monkeypatch):
    from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
    from nerya.tools.registry import ToolRegistry
    from nerya.tools.native.wallet_auth import wallet_auth_handler
    config = _client(tmp_path).config
    registry = ToolRegistry()
    deps = build_native_tool_deps(workspace_root=tmp_path, skill_roots=[tmp_path / 'skills'], paths=config.paths)
    deps.config = config
    register_native_tools(registry, deps)
    descriptor = registry.find('wallet_auth')
    assert descriptor is not None
    assert descriptor.is_concurrency_safe is False
    calls = []
    authenticated = False
    def run(client, provider, args, **kw):
        nonlocal authenticated
        calls.append(args)
        if args == ['wallet', 'status']:
            return {'ok': True, 'json': {'data': {'loggedIn': authenticated}}}
        if 'init' in args:
            return {'ok': True, 'json': {'data': {'authSessionId': 'original', 'loginUrl': 'https://web3.okx.com/login'}}}
        authenticated = True
        return {'ok': True, 'json': {'data': {'loggedIn': True}}}
    monkeypatch.setattr(routes_wallet, '_run_wallet_cli', run)
    monkeypatch.setattr(routes_wallet, '_ensure_provider_account', lambda *args: pytest.fail('login must not create wallets'))
    for op in ['start', 'start', 'verify', 'status']:
        result = wallet_auth_handler(ToolCall(name='wallet_auth', arguments={'op': op, 'provider': 'onchainos'}),
                                    config_like=config, actor_id='operator', session_id='conversation')
        assert not result.is_error, result.text()
    assert sum('init' in args for args in calls) == 1
    assert ['wallet', 'login', '--phase', 'poll', '--session-id', 'original'] in calls


def test_okx_already_logged_in_does_not_mint_a_login_link(tmp_path, monkeypatch):
    calls = []
    def run(client, provider, args, **kw):
        calls.append(args)
        return {'ok': True, 'json': {'data': {'loggedIn': True}}}
    monkeypatch.setattr(routes_wallet, '_run_wallet_cli', run)
    result = _route('/wallet/auth/start')(_client(tmp_path), {'provider': 'okx_os', 'install': False})
    assert result['authenticated'] is True
    assert calls == [['wallet', 'status']]


@pytest.mark.parametrize('provider', ['okx_os', 'coinbase', 'binance_agentic'])
def test_status_explicit_logged_out_overrides_stale_account_identity(provider):
    assert routes_wallet._cli_authenticated({'ok': True, 'provider': provider,
        'json': {'data': {'loggedIn': False, 'currentAccountId': 'stale'}}}) is False


def test_verify_recognizes_a_login_already_completed_outside_the_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(routes_wallet, '_run_wallet_cli', lambda client, provider, args, **kw: {
        'ok': True, 'json': {'data': {'loggedIn': True}}} if args == ['wallet', 'status']
        else pytest.fail('must not mint or poll another session'))
    result = _route('/wallet/auth/verify')(_client(tmp_path), {'provider': 'okx_os'})
    assert result['authenticated'] is True


@pytest.mark.parametrize('provider', ['byreal', 'okx_os', 'onchainos', 'bitget_onchain', 'binance_alpha', 'coinbase_wallet'])
def test_wallet_discovery_does_not_suggest_editing_host_exchange_code(provider):
    from nerya.tools.native.connectors import connector_view_handler
    result = connector_view_handler(ToolCall(name='connector_view', arguments={'id': provider}))
    data = result.content[0].data
    assert data['kind'] == 'wallet'
    assert data['next_required_action']['arguments']['action'] == 'readiness'


def test_a_competing_login_cannot_overwrite_another_context_pending_state(tmp_path, monkeypatch):
    calls = []
    def run(client, provider, args, **kw):
        calls.append(args)
        return {'ok': True, 'json': {'data': {'loggedIn': False}}} if args == ['wallet', 'status'] else {
            'ok': True, 'json': {'data': {'authSessionId': 'original', 'loginUrl': 'https://web3.okx.com/login'}}}
    monkeypatch.setattr(routes_wallet, '_run_wallet_cli', run)
    client = _client(tmp_path)
    start = _route('/wallet/auth/start')
    assert start(client, {'provider': 'okx_os', 'install': False, 'auth_context_id': 'first'})['ok']
    result = start(client, {'provider': 'okx_os', 'install': False, 'auth_context_id': 'second'})
    assert result['error'] == 'auth_pending_in_another_context'
    assert sum('init' in args for args in calls) == 1


def test_expired_auth_requires_explicit_restart_and_preserves_original_handoff(tmp_path, monkeypatch):
    calls = []
    def run(client, provider, args, **kw):
        calls.append(args)
        if args == ['wallet', 'status']:
            return {'ok': True, 'json': {'data': {'loggedIn': False}}}
        if 'poll' in args:
            return {'ok': False, 'json': {'error': 'Auth session expired'}}
        return {'ok': True, 'json': {'data': {'authSessionId': f'init{len(calls)}', 'loginUrl': 'https://web3.okx.com/login'}}}
    monkeypatch.setattr(routes_wallet, '_run_wallet_cli', run)
    client = _client(tmp_path)
    start = _route('/wallet/auth/start')
    body = {'provider': 'okx_os', 'install': False}
    original = start(client, body)['auth_session_id']
    failed = _route('/wallet/auth/verify')(client, body)
    assert failed['error'] == 'auth_session_expired'
    assert start(client, body)['restart_required'] is True
    assert sum('init' in args for args in calls) == 1
    assert routes_wallet._pending_okx_auth(client, body)['auth_session_id'] == original
    assert start(client, {**body, 'restart': True})['ok'] is True
    assert sum('init' in args for args in calls) == 2


def test_wallet_dependency_failure_has_non_retryable_install_guidance(tmp_path, monkeypatch):
    from nerya.wallet.errors import WalletDependencyError
    def missing(self, args, **kw):
        raise WalletDependencyError('okx_os', ['bin:onchainos'], 'Use the catalog install command')
    monkeypatch.setattr(OkxOsWallet, '_run_onchainos', missing)
    result = data_api_handler(ToolCall(name='data_api', arguments={
        'op': 'call', 'provider': 'onchainos', 'action': 'wallet_status'}), config_like=_client(tmp_path).config)
    assert result.is_error and result.error.retryable is False
    assert result.error.recovery_hint['action'] == 'wallet_install'


def test_readiness_compaction_keeps_all_builtin_wallets():
    from nerya.wallet.registry import PROVIDERS
    rows = [{'id': provider, 'readiness': {'ready': False, 'reason': 'not configured'}} for provider in PROVIDERS]
    result = compact_tool_result('data_api', {'provider': 'wallet', 'action': 'readiness',
        'kind': 'table', 'rows': rows, 'row_count': len(rows)}, size_threshold=0)
    assert {row['id'] for row in result.kept['rows_sample']} == set(PROVIDERS)


def test_cli_login_does_not_send_unsigned_api_quote_or_suggest_reinstall(tmp_path, monkeypatch):
    monkeypatch.setattr(OkxOsWallet, 'readiness', lambda self: SimpleNamespace(ready=True))
    monkeypatch.setattr(OkxOsWallet, '_decimals', lambda *args, **kw: 6)
    monkeypatch.setattr(OkxOsWallet, '_token_address', lambda self, chain, token: token)
    monkeypatch.setattr('nerya.connectors.http.UrllibHttp.request', lambda *args, **kw: pytest.fail('missing credentials reached API'))
    result = data_api_handler(ToolCall(name='data_api', arguments={'op': 'call', 'provider': 'wallet', 'action': 'quote',
        'args': {'provider': 'okx_os', 'chain': 'solana', 'token_in': 'SOL', 'token_out': 'USDC', 'amount_in': 1}}),
        config_like=_client(tmp_path).config)
    assert result.is_error and result.error.retryable is False
    assert result.error.recovery_hint['action'] == 'configure_credentials'


@pytest.mark.parametrize("error_type", [OSError, WalletTransportError])
def test_binance_status_failure_returns_unavailable_readiness(monkeypatch, error_type):
    from nerya.wallet.providers import binance_cli
    monkeypatch.setattr(binance_cli, "command", lambda provider: ["/fake/baw"])
    def unavailable(*args, **kwargs):
        raise error_type("status unavailable")
    monkeypatch.setattr(binance_cli, "call", unavailable)
    result = BinanceAgenticWallet().readiness()
    assert not result.ready
    assert result.missing == ["status:baw"]
