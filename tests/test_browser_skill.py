"""The public browser Skill is managed-only; legacy engine lanes are retired."""
from __future__ import annotations
import sys
from pathlib import Path
import pytest
from nerya.api import routes_browsers, routes_browsers_session, route_scopes
from nerya.skills.builtin.browser.scripts import browser_session
from nerya.skills.manifest import SkillManifest

pytestmark=pytest.mark.smoke


def test_skill_declares_automatic_managed_browser():
    manifest=SkillManifest.from_skill_md(Path(browser_session.__file__).parents[1]/'SKILL.md')
    assert manifest.id=='browser'
    assert 'right side panel' in manifest.instructions
    assert 'automatically launches' in manifest.instructions
    assert 'Do not bypass the input shield' in manifest.instructions
    assert route_scopes.required_scope('POST','/browsers/agent')=='write:tools'
    assert route_scopes.required_scope('POST','/browsers/desktop')=='admin:ops'


def test_no_legacy_install_selection_or_automation_routes():
    paths={path for _,path,_ in routes_browsers.routes()}
    assert '/browsers/agent' in paths and '/browsers/desktop' in paths
    assert not paths.intersection({'/browsers/install','/browsers/uninstall','/browsers/select','/browsers/configure','/browsers/probe'})
    assert routes_browsers_session.routes()==[]


@pytest.mark.parametrize('payload', [{'backend':'research'},{'engine':'camofox'},{'engine':'cloakbrowser'},{'engine':'lightpanda'},{'engine':'obscura'},{'session_id':'bs_old'}])
def test_legacy_arguments_do_not_dispatch(monkeypatch,payload):
    monkeypatch.setattr(browser_session,'_request',lambda *a,**kw:pytest.fail('legacy request escaped'))
    result=browser_session.run(operation='open',**payload)
    assert result['error']=='legacy_browser_removed_use_managed'


@pytest.mark.parametrize('operation', ['open','snapshot','click','fill','move','select_text','tabs','events','close'])
def test_commands_use_same_managed_api(monkeypatch,operation):
    calls=[]
    def request(method,path,**kw): calls.append((method,path,kw['payload'])); return {'ok':True}
    monkeypatch.setattr(browser_session,'_request',request)
    result=browser_session.run(operation=operation,session_id='mb_test',url='https://example.test',target={'label':'Name'})
    assert result['ok'] and len(calls)==1
    assert calls[0][0:2]==('POST','/browsers/agent')
    assert calls[0][2]['operation']==operation


def test_missing_session_is_not_replaced_with_someone_elses(monkeypatch):
    monkeypatch.setattr(browser_session,'_request',lambda *a,**kw:pytest.fail('implicit session'))
    assert browser_session.run(operation='snapshot')['error']=='session_id_required_use_open_receipt'


def test_click_error_never_falls_back_to_javascript(monkeypatch):
    calls=[]
    def request(*a,**kw): calls.append(kw['payload']); return {'ok':False,'error':'action_timeout'}
    monkeypatch.setattr(browser_session,'_request',request)
    result=browser_session.run(operation='click',session_id='mb_test',target={'selector':'#go'})
    assert not result['ok'] and len(calls)==1
    assert calls[0]['operation']=='click'


def test_registry_has_no_engine_choice():
    assert browser_session.run(operation='registry')=={'ok':True,'engine':'chromium','managed':True}


def test_cli_failure_is_nonzero(monkeypatch,capsys):
    monkeypatch.setattr(sys,'argv',['browser_session.py','--json','{"operation":"status"}'])
    monkeypatch.setattr(browser_session,'run',lambda **kw:{'ok':False,'error':'boom'})
    with pytest.raises(SystemExit) as error: browser_session.main()
    assert error.value.code==1
    assert '"ok": false' in capsys.readouterr().out
