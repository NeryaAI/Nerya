"""Denied file reads suggest the canonical Skill route; never grant file access."""
import pytest

from nerya.agent.file_state import FileStateCache
from nerya.tools.native import file_ops
from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.types import ToolCall, ToolErrorKind

pytestmark = pytest.mark.smoke


@pytest.fixture
def roots(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    builtin = tmp_path / 'installed' / 'skills' / 'builtin'
    skill = builtin / 'strategy_author'
    (skill / 'templates').mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: strategy_author\ndescription: fixture\n---\n# Author\n')
    asset = skill / 'templates' / 'strategy_execution.py.j2'
    asset.write_text('fixture = "not read by file_ops"\n')
    # Simulate an installed package without touching runtime roots or services.
    monkeypatch.setattr(file_ops, '_BUILTIN_SKILL_ROOT', builtin, raising=False)
    return workspace, builtin, asset


def denied(path, workspace):
    result = file_ops.read_file_handler(
        ToolCall(name='read_file', arguments={'path': str(path)}),
        root=workspace, file_state=FileStateCache())
    assert result.is_error and result.error.kind == ToolErrorKind.PERMISSION_DENIED
    assert 'access refused' in result.error.message
    assert not result.context_modifiers
    return result.error.recovery_hint


def test_installed_template_denial_returns_exact_usable_skill_route(roots, monkeypatch):
    workspace, builtin, asset = roots
    def must_not_read(path):
        pytest.fail('permission recovery must not read the requested file')
    monkeypatch.setattr(file_ops, '_read_text', must_not_read)
    hint = denied(asset, workspace)
    assert hint == {'next_required_action': {
        'tool': 'Skill',
        'arguments': {'skill': 'strategy_author', 'file': 'templates/strategy_execution.py.j2'},
        'reason': 'builtin_skill_asset_requires_skill',
    }}
    action = hint['next_required_action']
    fetched = skill_tool_handler(ToolCall(name=action['tool'], arguments=action['arguments']),
                                skill_index=SkillIndex([builtin]))
    assert not fetched.is_error, fetched.text()
    assert 'fixture = ' in fetched.text()


def test_playbook_route_uses_manifest_id_not_directory_name(roots):
    workspace, _, asset = roots
    playbook = asset.parent.parent / 'SKILL.md'
    playbook.write_text('---\nname: author_fixture\ndescription: fixture\n---\n# Author\n')
    assert denied(playbook, workspace)['next_required_action']['arguments'] == {
        'skill': 'author_fixture', 'file': 'SKILL.md',
    }


@pytest.mark.parametrize('kind', ['external', 'missing', 'directory', 'prefix', 'unregistered'])
def test_unknown_paths_keep_plain_permission_error(roots, kind):
    workspace, builtin, asset = roots
    paths = {
        'external': builtin.parent / 'unrelated.py',
        'missing': asset.parent / 'missing.py',
        'directory': asset.parent,
        'prefix': builtin.with_name('builtin-impostor') / 'strategy_author/templates/file.py',
        'unregistered': builtin / 'unknown/file.py',
    }
    path = paths[kind]
    if kind not in {'missing', 'directory'}:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('not a registered skill asset')
    assert not denied(path, workspace)


@pytest.mark.parametrize('kind', ['asset', 'folder', 'playbook', 'inside_alias', 'outside_alias', 'traversal'])
def test_symlink_and_traversal_cannot_create_trusted_skill_hint(roots, kind):
    workspace, builtin, asset = roots
    external = builtin.parent / 'external'
    external.mkdir()
    (external / 'secret.txt').write_text('never expose')
    path = asset
    if kind == 'asset':
        path = asset.parent / 'secret.py'
        path.symlink_to(external / 'secret.txt')
    elif kind == 'folder':
        folder = asset.parent / 'escape'
        folder.symlink_to(external, target_is_directory=True)
        path = folder / 'secret.txt'
    elif kind == 'playbook':
        playbook = asset.parent.parent / 'SKILL.md'
        playbook.unlink()
        playbook.symlink_to(external / 'secret.txt')
    elif kind == 'inside_alias':
        path = asset.parent / 'alias.py'
        path.symlink_to(asset)
    elif kind == 'outside_alias':
        path = builtin.parent / 'alias.py'
        path.symlink_to(asset)
    else:
        path = asset.parent / '..' / 'templates' / asset.name
    assert not denied(path, workspace)


def test_nearest_nested_skill_id_and_role_permissions_remain_authoritative(roots):
    workspace, builtin, asset = roots
    nested = asset.parent.parent / 'nested'
    nested.mkdir()
    (nested / 'SKILL.md').write_text('---\nname: nested_confirmation\ndescription: fixture\n---\n# Nested\n')
    reference = nested / 'reference.md'
    reference.write_text('nested reference')
    action = denied(reference, workspace)['next_required_action']
    assert action['arguments'] == {'skill': 'nested_confirmation', 'file': 'reference.md'}
    result = skill_tool_handler(ToolCall(name='Skill', arguments=action['arguments'],
                                        metadata={'allowed_skills': ['unrelated']}),
                                skill_index=SkillIndex([builtin]))
    assert result.is_error and result.error.kind == ToolErrorKind.PERMISSION_DENIED


def test_workspace_reads_are_unchanged(roots):
    workspace, _, _ = roots
    path = workspace / 'note.md'
    path.write_text('workspace note')
    result = file_ops.read_file_handler(ToolCall(name='read_file', arguments={'path': 'note.md'}),
                                        root=workspace, file_state=None)
    assert not result.is_error and 'workspace note' in result.text()
