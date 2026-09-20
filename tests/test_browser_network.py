"""Passive capture contracts; actual Chromium is tested by verify_browser_network.py."""
import json
import threading
import time
from types import SimpleNamespace
import pytest
from nerya.integrations.browser_network import NetworkLog, safe_url, safe_body, safe_headers
from nerya.integrations.browser_challenge import status, wait
from nerya.integrations.managed_browser import BrowserError
pytestmark=pytest.mark.smoke

class Page:
    url='https://example.test/'
    frames=[]
    def on(self,*args):pass

class Channel:
    def __init__(self):self.calls=[];self.body='{"result":"ready","token":"private-value"}'
    def on(self,*args):pass
    def detach(self):pass
    def send(self,name,params=None):
        self.calls.append(name)
        if name=='Page.getFrameTree':return {'frameTree':{'frame':{'id':'main'}}}
        if name=='Network.getResponseBody':return {'body':self.body,'base64Encoded':False}
        return {}

@pytest.fixture
def log():
    page=Page();channel=Channel()
    controller=SimpleNamespace(session_id='lease-a',allowed=lambda url:url.startswith('https://example.test'))
    worker=SimpleNamespace(context=SimpleNamespace(pages=[page],on=lambda *a:None,new_cdp_session=lambda p:channel),
        pages={'1':page},page=page,paused=False,interrupted=threading.Event(),agent_controller=controller,
        _protected_url=lambda url:not url.startswith(('https://','http://','about:blank')))
    network=NetworkLog(worker);worker.network=network
    return network,worker,channel

def request(log,index='1',url='https://example.test/api',**extra):
    network,worker,_=log
    network.request('1',worker.page,{'requestId':index,'request':{'url':url,'method':'GET','headers':{},**extra},'type':'Fetch','timestamp':1,'documentURL':worker.page.url})
    return network.read({})['requests'][-1]['id']

def response(log,index='1',kind='Fetch',frame='main',headers=None,status_code=200):
    network,worker,_=log
    network.response('1',worker.page,{'requestId':index,'type':kind,'frameId':frame,
        'response':{'url':'https://example.test/api','status':status_code,'mimeType':'application/json','headers':headers or {}}})

def test_credentials_and_nested_secrets_are_not_returned():
    url=safe_url('https://user:pass@example.test/api?token=private#secret')
    assert 'user:' not in url and 'private' not in url and '#secret' not in url
    assert safe_headers({'Authorization':'secret','Cookie':'secret','Set-Cookie':'secret','Content-Type':'application/json'})=={'content-type':'application/json'}
    cleaned=safe_body('{"data":[{"password":"hide","value":7}],"access_token":"hide"}','application/json')
    assert 'hide' not in cleaned and '"value":7' in cleaned

def test_default_listener_and_incremental_state_without_action_replay(log):
    network,worker,channel=log;identifier=request(log)
    first=network.read({});assert first['listening'] and first['requests'][0]['state']=='pending'
    response(log);network.finish('1',{'requestId':'1','timestamp':1.12,'encodedDataLength':99})
    updates=network.read({'after':first['cursor']})
    assert updates['requests'][0]['id']==identifier and updates['requests'][0]['state']=='finished'
    assert updates['requests'][0]['duration_ms']==120.0
    assert not network.read({'after':updates['cursor']})['requests']
    assert 'Network.getResponseBody' not in channel.calls
    network.detail({'network_id':identifier});assert 'Network.getResponseBody' not in channel.calls
    detail=network.detail({'network_id':identifier,'include_body':True})
    assert detail['request']['body_state']=='available' and 'private-value' not in detail['request']['body']
    assert channel.calls.count('Network.getResponseBody')==1

def test_finished_body_cache_survives_closed_target_and_is_bounded(log):
    network,worker,channel=log;network.MAX_BODY_BYTES=100
    identifier=request(log);response(log);network.finish('1',{'requestId':'1','timestamp':2,'encodedDataLength':50})
    network.drain();assert identifier in network.bodies and network.body_bytes<=100
    network.detach(worker.page)
    detail=network.detail({'network_id':identifier,'include_body':True})
    assert 'ready' in detail['request']['body'] and channel.calls.count('Network.getResponseBody')==1
    for i in range(5):
        row={'id':str(i),'state':'finished','mime_type':'text/plain','bytes':0,'_source':'new','_request':str(i)}
        network.rows[str(i)]=row;network.channels['new']=channel;channel.body='x'*60
        network._body(row)
    assert network.body_bytes<=100 and len(network.bodies)<=1


def test_ring_eviction_is_bounded_and_cursor_restart_is_explicit(log):
    network,_,_=log;old=request(log)
    for i in range(510):request(log,str(i+2))
    assert len(network.rows)==500 and len(network.keys)==500 and network.dropped==11
    with pytest.raises(BrowserError):network.detail({'network_id':old})
    restart=network.read({'after':999999,'generation':'previous-process'})
    assert restart['reset'] and restart['requests'] and restart['generation']==network.generation
    other=NetworkLog(network.worker)
    assert other.generation!=network.generation

def test_session_boundary_and_human_requests_are_not_agent_evidence(log):
    network,worker,_=log;request(log)
    assert network.read({},worker.agent_controller)['requests']
    worker.agent_controller.session_id='lease-b'
    assert not network.read({},worker.agent_controller)['requests']
    worker.paused=True;request(log,'manual')
    worker.paused=False
    assert not network.read({},worker.agent_controller)['requests']
    assert len(network.read({})['requests'])==2

def test_cross_origin_and_non_text_bodies_are_not_returned(log):
    network,worker,channel=log;request(log,url='https://not-authorized.test/api')
    assert not network.read({},worker.agent_controller)['requests']
    identifier=request(log,'2');network.update('1',{'requestId':'2'},state='finished',mime_type='image/png')
    assert network.detail({'network_id':identifier,'include_body':True})['request']['body_state']=='non_text_body_omitted'
    network.update('1',{'requestId':'2'},mime_type='application/json',bytes=70000)
    assert network.detail({'network_id':identifier,'include_body':True})['request']['body_state']=='body_exceeds_capture_limit'
    assert 'Network.getResponseBody' not in channel.calls

@pytest.mark.parametrize('body',[{'after':-1},{'limit':True},{'limit':101},{'generation':[]},{'filter':[]},{'method':{}},{'errors_only':1}])
def test_invalid_list_parameters_rejected(log,body):
    with pytest.raises(BrowserError):log[0].read(body)

@pytest.mark.parametrize('body',[{'network_id':[]},{'network_id':''},{'network_id':'x','include_body':'yes'},{'network_id':'x','offset':-1}])
def test_invalid_detail_parameters_rejected(log,body):
    with pytest.raises(BrowserError):log[0].detail(body)

def test_xhr_and_subframe_challenge_do_not_block_top_document(log):
    network,worker,_=log;request(log)
    response(log,headers={'cf-mitigated':'challenge'},status_code=403)
    assert network.challenge_state(worker.page)['state']=='none'
    response(log,kind='Document',frame='child',headers={'cf-mitigated':'challenge'},status_code=403)
    assert network.challenge_state(worker.page)['state']=='none'
    response(log,kind='Document',headers={'cf-mitigated':'challenge'},status_code=403)
    assert network.challenge_state(worker.page)['state']=='waiting'
    response(log,kind='Document',status_code=200)
    assert network.challenge_state(worker.page)['state']=='cleared'

def test_challenge_wait_budget_interrupt_and_handoff_state(log):
    network,worker,_=log;request(log);response(log,kind='Document',headers={'cf-mitigated':'challenge'},status_code=403)
    controller=SimpleNamespace(worker=worker,deadline=time.monotonic()+1,guard=lambda:None,publish_surface=lambda:None,clear_refs=lambda:None)
    with pytest.raises(BrowserError,match='challenge_requires_handoff'):wait(controller,{'challenge_timeout_ms':0})
    assert network.challenge_state(worker.page)['state']=='handoff_required'
    with pytest.raises(BrowserError,match='challenge_requires_handoff'):wait(controller,{})
    response(log,kind='Document',status_code=200)
    assert wait(controller,{})['state']=='cleared'

def test_widget_presence_is_not_challenge_success(log):
    _,worker,_=log;worker.page.frames=[SimpleNamespace(url='https://challenges.cloudflare.com/widget')]
    result=status(SimpleNamespace(worker=worker))
    assert result['state']=='widget_present' and result['blocking'] is False
