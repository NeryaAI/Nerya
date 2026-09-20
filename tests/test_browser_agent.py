"""Offline Agent browser contracts; real DOM is covered by the opt-in runner."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from nerya.api import route_scopes, local_server, routes_browser_desktop
from nerya.integrations.browser_agent import AgentController, BrowserError, origin, finite_number
from nerya.skills.builtin.browser.scripts import browser_session
from nerya.tools.native.skill import is_browser_skill_script_run

pytestmark=pytest.mark.smoke


@pytest.mark.parametrize('url,expected', [('https://EXAMPLE.com:443/a','https://example.com'),('http://localhost:8030/x','http://localhost:8030'),('http://[::1]:80/','http://[::1]')])
def test_site_origin_normalization(url, expected):
    assert origin(url)==expected


@pytest.mark.parametrize('url', ['file:///x','chrome://settings','javascript:1','https://user:pass@example.com','https://example.com:bad','about:blank','https://example.com\n'])
def test_site_grants_reject_privileged_or_ambiguous_urls(url):
    with pytest.raises(BrowserError): origin(url)


@pytest.mark.parametrize('value', [True,float('nan'),float('inf'),-1,99999,'500'])
def test_deadlines_and_coordinates_are_finite(value):
    with pytest.raises(BrowserError): finite_number(value,1,0,100)


@pytest.fixture
def controller(tmp_path):
    class Page:
        url = 'https://example.test/'
        def on(self, *args): pass
        def is_closed(self): return False
    page = Page()
    context=SimpleNamespace(pages=[page],on=lambda *a:None,route=lambda *a:None)
    worker=SimpleNamespace(context=context,page=page,pages={'1':page},directory=tmp_path,config={'id':'work','extensions':[]},
                           paused=False,interrupted=threading.Event(),_sync_pages=lambda:None,_has_protected_target=lambda:False,is_owner_thread=lambda:True)
    c=AgentController(worker)
    c.snapshot=lambda body:{'elements':[],'revision':c.revision}
    return c


def request(c, operation, actor='actor-a', **body):
    data={'operation':operation,'request_id':'request-'+operation,**body}
    if operation!='open': data.setdefault('session_id',c.session_id)
    return c.run(data,actor)


def authorize(c):
    c.grant_access({'origins':['https://example.test'],'ttl_s':3600})
    return request(c,'open')


def test_agent_needs_grant_and_cannot_grant_itself(controller):
    c=controller
    with pytest.raises(BrowserError,match='grant'): request(c,'open')
    authorize(c)
    with pytest.raises(BrowserError,match='unsupported'): request(c,'agent_grant')
    assert not c.grant['screenshots'] and not c.grant['downloads']


def test_session_ownership_and_no_implicit_second_lease(controller):
    c=controller
    opened=authorize(c)
    assert opened['session_id'].startswith('mb_')
    with pytest.raises(BrowserError,match='owner_mismatch'): request(c,'snapshot',actor='actor-b')
    with pytest.raises(BrowserError,match='in_use'): request(c,'open',request_id='another-open')
    assert request(c,'list',actor='actor-b')['sessions']==[]


def test_replay_works_but_expiration_and_pause_stop_old_evidence(controller):
    c=controller
    first=authorize(c)
    assert request(c,'open')['replayed']
    with pytest.raises(BrowserError,match='payload_conflict'): request(c,'open',url='https://example.test')
    c.worker.paused=True
    with pytest.raises(BrowserError,match='handoff'): request(c,'open')
    assert request(c,'status')['paused']
    c.worker.paused=False
    c.grant['expires_at']=time.time()-1
    with pytest.raises(BrowserError,match='expired'): request(c,'open')
    assert request(c,'close')['released']
    assert first['session_id'] != c.session_id


def test_navigation_policy_denies_ungranted_document_but_not_cdn_resources(controller):
    c=controller
    authorize(c)
    calls=[]
    route=SimpleNamespace(request=SimpleNamespace(url='https://other.test',is_navigation_request=lambda:True),abort=lambda why:calls.append('blocked'),continue_=lambda:calls.append('allowed'))
    c.navigation_policy(route)
    route.request.is_navigation_request=lambda:False
    c.navigation_policy(route)
    assert calls==['blocked','allowed']


def test_revoke_clears_lease_and_blocks_agent_resume(controller):
    c=controller
    authorize(c)
    c.revoke()
    assert not c.session_id and c.worker.interrupted.is_set()
    with pytest.raises(BrowserError): request(c,'resume')


def test_agent_cannot_upload_arbitrary_files_or_eval(controller):
    c=controller
    authorize(c)
    for operation in ('eval','wallet_sign','import_credentials','virtual_authenticator'):
        with pytest.raises(BrowserError,match='unsupported'): request(c,operation)
    assert not c.uploads


def test_browser_script_defaults_to_managed_and_retains_request_id(monkeypatch):
    calls=[]
    def fake(method,path,**kw):
        calls.append((method,path,kw['payload']))
        return {'ok':True}
    monkeypatch.setattr(browser_session,'_request',fake)
    result=browser_session.run(operation='open',url='https://example.test')
    assert calls[0][1]=='/browsers/agent'
    assert result['request_id']==calls[0][2]['request_id']
    assert not browser_session.run(operation='click')['ok']
    assert len(calls)==1


def test_transport_failure_is_not_retried_and_keeps_receipt_key(monkeypatch):
    calls=[]
    def fail(*args,**kwargs): calls.append(1); return {'ok':False,'error':'url_error'}
    monkeypatch.setattr(browser_session,'_request',fail)
    result=browser_session.run(operation='open',request_id='synthetic-request')
    assert result['request_id']=='synthetic-request' and result['retryable'] is False
    assert len(calls)==1


def test_scoped_agent_route_and_trusted_identity_stamp():
    assert '/browsers/agent' in local_server._TRUSTED_AUTH_PAYLOAD_PATHS
    assert route_scopes.authorize(['write:tools'],'POST','/browsers/agent')[0]
    assert not route_scopes.authorize(['write:tools'],'POST','/browsers/desktop')[0]
    assert not route_scopes.authorize(['read:runtime'],'POST','/browsers/agent')[0]
    handler=next(h for method,path,h in routes_browser_desktop.routes() if path=='/browsers/agent')
    response=handler(SimpleNamespace(config=SimpleNamespace(paths=SimpleNamespace(root='.'))),{'operation':'open'})
    assert response['error']=='trusted_actor_required'


def test_api_target_is_operator_configured_not_model_controlled(monkeypatch):
    monkeypatch.setenv('NERYA_API', 'http://127.0.0.1:18317')
    assert browser_session._api_base() == 'http://127.0.0.1:18317'
    with pytest.raises(ValueError, match='operator_configuration'):
        browser_session._api_base('https://untrusted.invalid')
    result=browser_session.run(operation='open',api_base='https://untrusted.invalid')
    assert result['error']=='invalid_or_unapproved_api_base'


def test_api_redirects_cannot_forward_credentials():
    from urllib.error import HTTPError
    from urllib.request import Request
    request=Request('http://127.0.0.1:18317/browsers/agent',headers={'Authorization':'Bearer synthetic-only'})
    with pytest.raises(HTTPError):
        browser_session._NoApiRedirect().redirect_request(request,None,302,'Found',{},'https://untrusted.invalid/')


def test_no_blanket_browser_named_script_approval():
    assert is_browser_skill_script_run({'skill_id':'browser','name':'browser_session.py'})
    for name in ('arbitrary.py','../escape.py','scripts/../../x.py',''):
        assert not is_browser_skill_script_run({'skill_id':'browser','name':name})
