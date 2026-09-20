"""Persistent browser workspace and automatic-mode boundaries."""
from __future__ import annotations
import json
import threading
from types import SimpleNamespace
import pytest
from nerya.integrations import browser_workspace as ws
from nerya.integrations.browser_agent import AgentController
from nerya.integrations.managed_browser import BrowserError, profile_directory

pytestmark=pytest.mark.smoke


def test_automatic_mode_default_persistence_and_profile_isolation(tmp_path):
    assert ws.preferences(tmp_path)=={'automatic':True}
    ws.set_preferences(tmp_path,'work',False)
    assert ws.preferences(tmp_path)=={'automatic':False}
    assert ws.preferences(tmp_path,'separate')=={'automatic':True}


@pytest.mark.parametrize('value',[None,1,'yes',[],{}])
def test_preferences_reject_non_boolean(tmp_path,value):
    with pytest.raises(BrowserError):ws.set_preferences(tmp_path,'work',value)


def test_history_omits_credentials_query_fragment_and_deduplicates(tmp_path):
    for url in ['https://example.test/a?token=test#fragment','https://example.test/a?other=value','file:///private','chrome://settings','about:blank']:
        ws.record_visit(tmp_path,'work',url)
    rows=ws.history(tmp_path)
    assert len(rows)==1 and rows[0]['url']=='https://example.test/a'
    assert not any('token' in str(row) or 'fragment' in str(row) for row in rows)
    ws.clear_history(tmp_path,'work')
    assert ws.history(tmp_path)==[]


def test_history_is_bounded_without_removing_latest_page(tmp_path):
    path=profile_directory(tmp_path,'work')/'history.json'
    path.write_text(json.dumps([{'id':str(i),'url':f'https://example.test/{i}','at':i} for i in range(300)]))
    ws.record_visit(tmp_path,'work','https://example.test/latest')
    rows=ws.history(tmp_path)
    assert len(rows)==300 and rows[0]['id']=='1' and rows[-1]['url'].endswith('/latest')


def test_preferences_do_not_follow_symlink(tmp_path):
    outside=tmp_path/'outside.json';outside.write_text('{"automatic":true}')
    (profile_directory(tmp_path,'work')/'preferences.json').symlink_to(outside)
    with pytest.raises(BrowserError):ws.set_preferences(tmp_path,'work',False)
    assert json.loads(outside.read_text())['automatic'] is True


def test_all_web_delegation_does_not_include_internal_schemes_or_self_grant(tmp_path):
    worker=SimpleNamespace(context=SimpleNamespace(pages=[],on=lambda *a:None,route=lambda *a:None),
        interrupted=threading.Event(),directory=tmp_path,config={'id':'work','extensions':[]},
        _protected_url=lambda url:True)
    controller=AgentController(worker)
    controller.grant_access({'all_web':True,'screenshots':True,'downloads':True})
    assert controller.allowed('https://a.test') and controller.allowed('http://b.test/path')
    for url in ['file:///private','chrome://settings','javascript:alert(1)','chrome-extension://unapproved/popup.html']:
        assert not controller.allowed(url)
    controller.revoke()
    assert not controller.grant and worker.interrupted.is_set()
