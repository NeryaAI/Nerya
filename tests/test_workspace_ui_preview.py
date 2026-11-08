from types import SimpleNamespace

import pytest

from nerya.api import route_scopes, routes_inbox, routes_workspace_ui
from nerya.core import yaml_io
from nerya.core.paths import WorkspacePaths
from nerya.workspace import ui

pytestmark = pytest.mark.smoke


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / 'workspace'
    proposal = root / 'evolution/proposals/prp_preview'
    file = proposal / 'after/ui/workspace.yml'
    file.parent.mkdir(parents=True)
    meta = {'id': 'prp_preview', 'kind': 'core_config_patch', 'target': 'ui/workspace.yml',
            'state': 'pending_review', 'metadata': {'workspace_ui': True}}
    yaml_io.dump(proposal / 'proposal.yml', meta)
    yaml_io.dump(file, {'version': 1, 'home': {'widgets': []}, 'pages': [{
        'id': 'market-account-overview', 'title': 'Market overview', 'widgets': [
            {'id': 'btc', 'kind': 'market_ticker', 'config': {'market': 'BTC/USDT'}},
            {'id': 'eth', 'kind': 'market_ticker', 'config': {'market': 'ETH/USDT'}},
            {'id': 'account', 'kind': 'portfolio'},
        ]}]})
    return root, proposal, file, meta


def test_preview_route_is_scoped_read_only(candidate, monkeypatch):
    root, proposal, file, meta = candidate
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    def forbidden(*args, **kwargs):
        pytest.fail('preview must not read live UI/extensions or mutate')
    for name in ('read', 'propose', 'apply'):
        monkeypatch.setattr(ui, name, forbidden)
    monkeypatch.setattr(routes_workspace_ui, '_extensions', forbidden)
    client = SimpleNamespace(config=SimpleNamespace(paths=WorkspacePaths(root)))
    handler = {(method, path): fn for method, path, fn in routes_workspace_ui.routes()}[('GET', '/workspace/ui/preview')]
    result = handler(client, {'proposal_id': meta['id']})
    assert result['ok'] and result['state'] == 'pending_review'
    assert result['applied'] is False and result['data_validation'] == 'not_checked'
    assert result['structural_validation'] == 'passed'
    assert len(result['manifest']['pages'][0]['widgets']) == 3
    assert result['digest'] == ui.manifest_digest(result['manifest'])
    assert route_scopes.required_scope('GET', '/workspace/ui/preview') == 'read:runtime'
    assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('pid', ['', '../prp_preview', 'prp_preview/after', '..', 'missing'])
def test_invalid_or_missing_id(candidate, pid):
    result = ui.preview(candidate[0], pid)
    assert not result['ok'] and result['_status'] in (400, 404)
    assert 'manifest' not in result


@pytest.mark.parametrize('part', ['', 'evolution', 'evolution/proposals',
    'evolution/proposals/prp_preview', 'evolution/proposals/prp_preview/proposal.yml',
    'evolution/proposals/prp_preview/after', 'evolution/proposals/prp_preview/after/ui',
    'evolution/proposals/prp_preview/after/ui/workspace.yml'])
def test_rejects_symlink_at_every_boundary(candidate, tmp_path, part):
    root = candidate[0]
    target = root / part if part else root
    moved = tmp_path / 'moved'
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=moved.is_dir())
    result = ui.preview(root, 'prp_preview')
    assert not result['ok'] and 'manifest' not in result


@pytest.mark.parametrize('payload', ['[invalid', '[]', 'version: 99',
    'version: 1\nhome:\n  widgets:\n  - id: x\n    kind: markdown\n    config: {html: unsafe}',
    'x' * (ui.UI_MAX_BYTES + 1)])
def test_rejects_invalid_or_oversized_manifest(candidate, payload):
    candidate[2].write_text(payload)
    assert not ui.preview(candidate[0], 'prp_preview')['ok']


@pytest.mark.parametrize('changes', [{'kind': 'strategy_patch'}, {'target': '../other'}, {'id': 'other'}])
def test_rejects_non_ui_or_mismatched_proposal(candidate, changes):
    root, proposal, _, meta = candidate
    yaml_io.dump(proposal / 'proposal.yml', {**meta, **changes})
    assert not ui.preview(root, 'prp_preview')['ok']


def test_inbox_preview_does_not_change_apply(candidate):
    meta = candidate[3]
    actions = routes_inbox._proposal_item(meta)['actions']
    preview = next(a for a in actions if a['id'] == 'preview')
    assert 'proposal_id=prp_preview' in preview['href'] and not preview.get('method')
    other = routes_inbox._proposal_item({**meta, 'target': 'other.yml'})['actions']
    assert [a for a in actions if a['id'] != 'preview'] == other
