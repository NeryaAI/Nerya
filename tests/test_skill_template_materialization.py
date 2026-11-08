"""Copy bounded builtin templates through the registered, task-bound file tool."""
import hashlib
import json

import pytest
from jsonschema import Draft202012Validator

from nerya.skills.registry import SkillRegistry, load_entry
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.native import file_ops
from nerya.tools.types import RiskLevel, ToolCall, ToolErrorKind
from nerya.tools.permissions import PermissionContext, PermissionEngine
from nerya.tools.native.conversation_files import conversation_files_dir
from nerya.evolution.patch_proposal import create_proposal
from test_proposal_file_task_binding import bound_tools as template_bound_tools, record_draft  # noqa: F401

pytestmark = pytest.mark.smoke


def payload(result):
    return next(part.data for part in result.content if part.type == 'json')


@pytest.fixture
def materialization(template_bound_tools, tmp_path, monkeypatch):  # noqa: F811
    config, deps, registry, proposal, target = template_bound_tools
    builtin = tmp_path / 'installed/skills/builtin'
    skill = builtin / 'strategy_author'
    asset = skill / 'templates/large.py.j2'
    asset.parent.mkdir(parents=True)
    (skill / 'SKILL.md').write_text(
        '---\nname: strategy_author\ndescription: fixture\n---\n# Author\n')
    asset.write_bytes(('VALUE = "模板"\r\n' * 320).encode('utf-8'))
    catalog = SkillRegistry()
    catalog.register(load_entry(skill / 'SKILL.md', source='builtin'))
    deps.skill_index._registry_provider = lambda: catalog
    deps.skill_index.reload()
    monkeypatch.setattr(file_ops, '_BUILTIN_SKILL_ROOT', builtin)
    record_draft(template_bound_tools)
    executor = NativeToolExecutor(registry=registry, permission_engine=PermissionEngine(),
                                  permission_context=PermissionContext())
    return template_bound_tools, asset, catalog, executor


def read_source(env, *, file='templates/large.py.j2'):
    tools, asset, catalog, executor = env
    result = executor.execute(ToolCall(name='Skill', arguments={
        'skill': 'strategy_author', 'file': file}))
    assert not result.is_error, result.text()
    return {'skill': 'strategy_author', 'file': file,
            'expected_revision': payload(result)['revision']}, result


def write(env, *, source=None, path=None, fresh=True, metadata=None, **arguments):
    tools, asset, catalog, executor = env
    target = path or tools[-1]
    if fresh and target.exists():
        result = executor.execute(ToolCall(name='read_file', arguments={'path': str(target)}))
        assert not result.is_error, result.text()
    if source is not None:
        arguments['source_asset'] = source
    return executor.execute(ToolCall(name='write_file',
        arguments={'path': str(target), **arguments}, metadata=metadata or {}))


def test_materializes_exact_full_skill_revision_without_model_body(materialization):
    source, read = read_source(materialization)
    asset = materialization[1]
    assert payload(read)['next_offset'] == 200  # Skill returns a bounded page.
    assert source['expected_revision'] == hashlib.sha256(asset.read_bytes()).hexdigest()
    result = write(materialization, source=source)
    assert not result.is_error, result.text()
    target = materialization[0][-1]
    assert target.read_bytes() == asset.read_bytes()
    assert payload(result)['path'] == target.relative_to(materialization[0][0].paths.root).as_posix()
    assert payload(result)['source_asset'] == {
        'skill': source['skill'], 'file': source['file'], 'revision': source['expected_revision']}
    assert str(asset.parent.parent) not in json.dumps(result.asdict())
    assert result.semantic_success is not True
    assert 'verification' not in result.metadata


@pytest.mark.parametrize('body', [
    {'contents': ''}, {'content': 'legacy\r\n'},
    {'contents': 'current'}, {'contents': 'ignored', 'content': 'legacy wins'},
])
def test_existing_body_forms_unchanged(materialization, body):
    result = write(materialization, **body)
    assert not result.is_error, result.text()
    assert materialization[0][-1].read_bytes() == body.get('content', body.get('contents')).encode()
    assert 'source_asset' not in payload(result)


@pytest.mark.parametrize('case', [
    'neither', 'both', 'both_legacy', 'null', 'empty', 'missing_skill',
    'missing_file', 'missing_revision', 'wrong_type', 'blank', 'short_revision', 'extra',
])
def test_rejects_invalid_sources_without_write(materialization, case):
    source, _ = read_source(materialization)
    arguments = {'source_asset': source}
    if case == 'neither':
        arguments = {}
    elif case in {'both', 'both_legacy'}:
        arguments['contents' if case == 'both' else 'content'] = 'no'
    elif case == 'null':
        arguments['source_asset'] = None
    elif case == 'empty':
        arguments['source_asset'] = {}
    elif case.startswith('missing_'):
        del source[{'missing_skill': 'skill', 'missing_file': 'file',
                    'missing_revision': 'expected_revision'}[case]]
    elif case == 'wrong_type':
        source['file'] = 7
    elif case == 'blank':
        source['skill'] = ''
    elif case == 'short_revision':
        source['expected_revision'] = 'abc'
    else:
        source['path'] = '/external'
    before = materialization[0][-1].read_bytes()
    result = write(materialization, **arguments)
    assert result.is_error and result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    assert materialization[0][-1].read_bytes() == before


def test_schema_describes_exclusive_body_and_complete_source(materialization):
    source, _ = read_source(materialization)
    schema = materialization[0][2].get('write_file').input_schema
    validator = Draft202012Validator(schema)
    for body in ({'source_asset': source}, {'contents': ''}, {'content': ''},
                 {'contents': 'a', 'content': 'b'}):
        validator.validate({'path': 'main.py', **body})
    for body in ({}, {'source_asset': {}}, {'source_asset': source, 'contents': ''},
                 {'source_asset': source, 'content': ''}):
        assert not validator.is_valid({'path': 'main.py', **body})


@pytest.mark.parametrize('change', ['bytes', 'expected'])
def test_stale_source_is_retryable_and_does_not_write(materialization, change):
    source, _ = read_source(materialization)
    if change == 'bytes':
        materialization[1].write_text('NEW = 1\n')
    else:
        source['expected_revision'] = '0' * 64
    before = materialization[0][-1].read_bytes()
    result = write(materialization, source=source)
    assert result.is_error and result.error.kind == ToolErrorKind.STALE_FILE
    assert result.error.retryable
    assert result.error.recovery_hint['next_required_action']['tool'] == 'Skill'
    assert materialization[0][-1].read_bytes() == before


@pytest.mark.parametrize('file', [
    '../templates/large.py.j2', 'templates/../templates/large.py.j2',
    '/tmp/template.py.j2', 'C:\\templates\\large.py.j2',
    'templates\\large.py.j2', 'scripts/run.py.j2', 'templates/large.txt',
    'templates/missing.py.j2', 'templates',
])
def test_source_path_confinement(materialization, file):
    source, _ = read_source(materialization)
    source['file'] = file
    before = materialization[0][-1].read_bytes()
    result = write(materialization, source=source)
    assert result.is_error
    assert materialization[0][-1].read_bytes() == before
    assert str(materialization[1].parent.parent) not in json.dumps(result.asdict())


@pytest.mark.parametrize('kind', ['asset', 'templates', 'skill', 'playbook', 'builtin'])
def test_symlinks_are_rejected_even_with_matching_revision(materialization, kind):
    source, _ = read_source(materialization)
    asset = materialization[1]
    path = {'asset': asset, 'templates': asset.parent, 'skill': asset.parent.parent,
            'playbook': asset.parent.parent / 'SKILL.md',
            'builtin': asset.parent.parent.parent}[kind]
    moved = path.with_name(path.name + '-real')
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=moved.is_dir())
    result = write(materialization, source=source)
    assert result.is_error and result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    assert materialization[0][-1].read_text() == 'MARKET = "old"'


@pytest.mark.parametrize('kind', ['too_big', 'binary', 'nul', 'directory', 'fifo', 'limit'])
def test_bounded_utf8_regular_file(materialization, kind):
    source, _ = read_source(materialization)
    asset = materialization[1]
    if kind in {'directory', 'fifo'}:
        asset.unlink()
        if kind == 'directory':
            asset.mkdir()
        else:
            import os
            os.mkfifo(asset)
    else:
        raw = {'too_big': b'x' * (1024 * 1024 + 1), 'binary': b'\xff',
               'nul': b'a\x00b', 'limit': b'x' * (1024 * 1024)}[kind]
        asset.write_bytes(raw)
        source['expected_revision'] = hashlib.sha256(raw).hexdigest()
    result = write(materialization, source=source)
    if kind == 'limit':
        assert not result.is_error, result.text()
        assert materialization[0][-1].read_bytes() == raw
    else:
        assert result.is_error and result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
        assert materialization[0][-1].read_text() == 'MARKET = "old"'


@pytest.mark.parametrize('kind', ['unknown', 'disabled', 'workspace', 'external', 'role', 'nested'])
def test_current_catalog_and_skill_access_gates(materialization, kind):
    source, _ = read_source(materialization)
    tools, asset, catalog, executor = materialization
    metadata = {}
    if kind == 'unknown':
        source['skill'] = 'unknown'
    elif kind == 'disabled':
        catalog.by_id.clear()
    elif kind == 'workspace':
        catalog.get('strategy_author').manifest.source = 'workspace'
    elif kind == 'external':
        external = asset.parent.parent.parent.parent / 'external'
        (external / 'templates').mkdir(parents=True)
        (external / 'templates/large.py.j2').write_bytes(asset.read_bytes())
        (external / 'SKILL.md').write_bytes((asset.parent.parent / 'SKILL.md').read_bytes())
        catalog.register(load_entry(external / 'SKILL.md', source='builtin'))
    elif kind == 'role':
        metadata = {'allowed_skills': ['unrelated']}
    else:
        nested = asset.parent / 'nested'
        nested.mkdir()
        (nested / 'SKILL.md').write_text('---\nname: nested\ndescription: fixture\n---\n# Nested')
        (nested / 'large.py.j2').write_bytes(asset.read_bytes())
        catalog.register(load_entry(nested / 'SKILL.md', source='builtin'))
        source['file'] = 'templates/nested/large.py.j2'
        metadata = {'allowed_skills': ['strategy_author']}
    # Force the live provider snapshot to observe mutations, including source/path changes.
    tools[1].skill_index.reload()
    result = write(materialization, source=source, metadata=metadata)
    assert result.is_error
    assert tools[-1].read_text() == 'MARKET = "old"'
    if kind in {'role', 'nested'}:
        assert result.error.kind == ToolErrorKind.PERMISSION_DENIED


def test_foreign_proposal_cannot_be_claimed_by_source_copy(materialization):
    source, _ = read_source(materialization)
    tools, asset, catalog, executor = materialization
    proposal = create_proposal(tools[0].paths, kind='strategy_package_proposal', summary='foreign',
        extra_files={'after/strategies/foreign/main.py': 'ORIGINAL'})
    target = proposal.path / 'after/strategies/foreign/main.py'
    result = write(materialization, source=source, path=target,
        command_id='scope-1', skip_binding=True)
    assert result.is_error and result.error.kind == ToolErrorKind.PERMISSION_DENIED
    assert result.error.detail['reason'] == 'proposal_task_binding_unverified'
    assert target.read_text() == 'ORIGINAL'


@pytest.mark.parametrize('change', ['unread', 'changed'])
def test_target_fresh_read_still_required(materialization, change):
    source, _ = read_source(materialization)
    tools, asset, catalog, executor = materialization
    if change == 'changed':
        executor.execute(ToolCall(name='read_file', arguments={'path': str(tools[-1])}))
        tools[-1].write_text('OTHER EDIT')
    before = tools[-1].read_bytes()
    result = write(materialization, source=source, fresh=False)
    assert result.is_error and result.error.kind == ToolErrorKind.STALE_FILE
    assert tools[-1].read_bytes() == before


@pytest.mark.parametrize('path', ['strategies/live/main.py', 'nerya.yml', 'skills/enabled.yml', '../escape.py'])
def test_target_proposal_and_path_guards_are_identical(materialization, path):
    source, _ = read_source(materialization)
    root = materialization[0][0].paths.root
    arguments = {'path': path}
    descriptor = materialization[0][2].get('write_file')
    # Invoke the registered handler to check domain guards beneath the risk gate.
    plain = descriptor.handler(ToolCall(name='write_file', arguments={**arguments, 'contents': 'x'}))
    copied = descriptor.handler(ToolCall(name='write_file', arguments={**arguments, 'source_asset': source}))
    assert plain.is_error and copied.is_error
    assert plain.error.asdict() == copied.error.asdict()
    assert not (root / path).exists()


def test_placement_and_risk_approval_are_unchanged(materialization):
    source, _ = read_source(materialization)
    tools, asset, catalog, executor = materialization
    root = tools[0].paths.root
    result = write(materialization, source=source, path=root / 'copy.py')
    assert not result.is_error, result.text()
    assert payload(result)['placement'] == 'conversation_reroute'
    assert (conversation_files_dir(root, 'scope-session') / 'copy.py').read_bytes() == asset.read_bytes()
    assert not (root / 'copy.py').exists()
    descriptor = tools[2].get('write_file')
    for path, extra in [('nerya.yml', {}), ('other.py', {'allow_outside_conversation': True,
                                  'outside_conversation_reason': 'fixture'})]:
        for body in ({'contents': 'x'}, {'source_asset': source}):
            args = {'path': path, **extra, **body}
            assert descriptor.per_call_risk(args) == RiskLevel.DANGEROUS
            denied = executor.execute(ToolCall(name='write_file', arguments=args))
            assert denied.is_error and denied.error.kind == ToolErrorKind.PERMISSION_PENDING
            assert denied.error.recovery_hint['action'] == 'await_approval'
            assert not (root / path).exists()
