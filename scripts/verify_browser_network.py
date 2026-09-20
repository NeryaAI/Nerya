"""Real Chromium + HTTP + native Skill Network acceptance. Synthetic local sites only.
--serve keeps the isolated API alive for UI acceptance until interrupted.
"""
from __future__ import annotations
import argparse
import json
import os
import signal
import sys
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nerya.api.local_server import build_server
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.integrations import managed_browser
from nerya.skills.builtin.browser.scripts import browser_session
from nerya.tools.native.skill import SkillIndex, script_run_handler
from nerya.tools.types import ToolCall

PAGE='''<!doctype html><meta charset="utf-8"><title>Network review</title>
<style>body{font:20px system-ui;background:#f3f5f7;color:#273343;padding:42px}main{max-width:820px;background:white;margin:auto;padding:36px;border-radius:10px}button,input{font:inherit;padding:12px;margin:8px 0}small{color:#626e7d}</style>
<main><small>LOCAL BROWSER ACCEPTANCE / SYNTHETIC DATA</small><h1>Review the report</h1><p>Network observation stays active while the Agent works.</p><label>Name <input id="name" placeholder="Report name"></label><br><button id="send">Save report</button> <button id="load">Load data</button><p id="result">Ready for review</p><p id="clock"></p><small id="network-ready"></small></main>
<script>document.querySelector('#send').onclick=async()=>{const r=await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer synthetic-auth'},body:JSON.stringify({name:document.querySelector('#name').value,password:'synthetic-hidden',token:'synthetic-token'})});document.querySelector('#result').textContent='Saved '+(await r.json()).count;};document.querySelector('#load').onclick=()=>fetch('/api/slow').then(r=>r.json()).then(()=>document.querySelector('#result').textContent='Data loaded');
fetch('/api/data?token=synthetic-secret').then(r=>r.json());fetch('/api/error').then(r=>r.json());fetch('/redirect').then(r=>r.json());let ticks=0;setInterval(()=>{document.querySelector('#clock').textContent='Live activity '+(++ticks);if(ticks<5)fetch('/api/tick?index='+ticks).then(r=>r.json());if(ticks>=4)document.querySelector('#network-ready').textContent='Network ready'},250);</script>'''
COUNTS={}
LOCK=threading.Lock()

class Site(BaseHTTPRequestHandler):
    def answer(self, code, data, mime='application/json', headers=None):
        raw=data.encode();self.send_response(code)
        self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)))
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.end_headers()
        try:self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError):pass
    def do_GET(self):
        path=urlsplit(self.path).path
        with LOCK:COUNTS[path]=COUNTS.get(path,0)+1
        if path=='/redirect':return self.answer(302,'','text/plain',{'Location':'/api/data'})
        if path=='/challenge':return self.answer(403,'<!doctype html><title>Local challenge fixture</title><p>Checking…</p><script>setTimeout(()=>location.replace("/done"),600)</script>','text/html',{'cf-mitigated':'challenge'})
        if path=='/stuck':return self.answer(403,'<!doctype html><title>Local challenge fixture</title><p>Operator required</p>','text/html',{'cf-mitigated':'challenge'})
        if path=='/api/slow':time.sleep(1.6)
        if path=='/api/large':return self.answer(200,'x'*100000,'text/plain')
        if path=='/api/error':return self.answer(503,'{"message":"temporary example failure"}')
        if path.startswith('/api/'):
            return self.answer(200,json.dumps({'items':[{'name':'Research notes','status':'ready'}],'access_token':'synthetic-hidden','password':'synthetic-hidden'}),headers={'Set-Cookie':'private=synthetic-cookie','Cache-Control':'no-store'})
        self.answer(200,PAGE,'text/html; charset=utf-8')
    def do_POST(self):
        path=urlsplit(self.path).path
        self.rfile.read(int(self.headers.get('Content-Length','0')))
        with LOCK:COUNTS[path]=COUNTS.get(path,0)+1;count=COUNTS[path]
        self.answer(200,json.dumps({'saved':True,'count':count}))
    def log_message(self,*args):pass


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--serve',action='store_true');args=parser.parse_args()
    report={'ok':False,'checks':[]};worker=api=site=None
    def stop(_signum, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop)
    os.environ['NERYA_DISABLE_TUNNEL_RESTORE']='1'
    os.environ.setdefault('NERYA_VAULT_PASSPHRASE','synthetic-network-test-only')
    with tempfile.TemporaryDirectory(prefix='nerya-network-') as directory:
        root=Path(directory)
        try:
            site=ThreadingHTTPServer(('127.0.0.1',0),Site);threading.Thread(target=site.serve_forever,daemon=True).start()
            url=f'http://127.0.0.1:{site.server_address[1]}'
            api=build_server(Config(paths=WorkspacePaths(root),data=deepcopy(DEFAULT_CONFIG)),port=0,start_cron=False,start_continuous=False)
            threading.Thread(target=api.serve_forever,daemon=True).start()
            base=f'http://127.0.0.1:{api.server_address[1]}';os.environ['NERYA_API']=base
            index=SkillIndex([Path(browser_session.__file__).parents[2]])
            session=''
            def call(operation,expected=True,**body):
                result=script_run_handler(ToolCall(id='net-test-'+uuid.uuid4().hex,name='script_run',arguments={'skill_id':'browser','name':'browser_session.py','args':['--json',json.dumps({'operation':operation,**({'session_id':session} if session else {}),**body})]}),skill_index=index,cwd=root,conversation_id='network-acceptance')
                data=next(p.data for p in result.content if p.type=='json')
                assert data.get('ok') is expected,str(data)[:1800]
                return data
            def admin(**body):
                data=browser_session._request('POST','/browsers/desktop',payload=body);assert data.get('ok'),data;return data
            opened=call('open',url=url);session=opened['session_id'];worker=managed_browser.worker_for(root,'work')
            call('wait_for',target={'text':'Network ready'},timeout_ms=4000)
            listed=call('network',limit=100)
            assert listed['listening'] and not listed['attach_errors'] and listed['requests'],listed
            assert any(r['type']=='fetch' and r['status']==200 for r in listed['requests'])
            assert any(r['status']==503 for r in listed['requests'])
            assert any(r.get('redirect_from') for r in listed['requests'])
            assert 'synthetic-secret' not in json.dumps(listed)
            report['checks'].append('default_capture_fetch_errors_redirects_and_redacted_query')
            data_row=next(r for r in listed['requests'] if '/api/data?' in r['url'])
            meta=call('network_detail',network_id=data_row['id'])
            assert 'body' not in meta['request'] and 'set-cookie' not in meta['request']['response_headers']
            body=call('network_detail',network_id=data_row['id'],include_body=True)
            assert body['request']['body_state']=='available',body
            assert 'Research notes' in body['request']['body'] and 'synthetic-hidden' not in body['request']['body']
            report['checks'].append('on_demand_response_body_headers_and_json_redaction')
            cursor=listed['cursor']
            call('batch',steps=[{'action':'fill','target':{'label':'Name'},'text':'Browser network report'}, {'action':'click','target':{'role':'button','name':'Save report'}},{'action':'wait_for','target':{'text':'Saved 1'}}])
            changed=call('network',after=cursor,method='POST')
            post=next(r for r in changed['requests'] if r['method']=='POST')
            detail=call('network_detail',network_id=post['id'],include_body=True)
            assert 'synthetic-hidden' not in json.dumps(detail) and 'synthetic-token' not in json.dumps(detail)
            assert 'authorization' not in detail['request']['request_headers']
            assert COUNTS['/api/save']==1
            report['checks'].append('native_skill_batch_post_payload_and_no_replay_on_inspection')
            before=call('network')['cursor'];outputs=[]
            thread=threading.Thread(target=lambda:outputs.append(call('batch',steps=[{'action':'click','target':{'role':'button','name':'Load data'}},{'action':'wait_for','target':{'text':'Data loaded'},'timeout_ms':5000}])))
            thread.start();samples=[];saw_pending=False
            while thread.is_alive():
                start=time.monotonic();view=call('network',after=before,filter='/api/slow');samples.append((time.monotonic()-start)*1000)
                saw_pending=saw_pending or any(r['state'] in {'pending','receiving'} for r in view['requests'])
                ready=call('network_detail',network_id=post['id'],include_body=True)
                assert ready['request']['body_state']=='available'
                time.sleep(.05)
            thread.join();assert saw_pending and outputs and max(samples)<1000,(saw_pending,samples)
            final=call('network',after=before,filter='/api/slow');assert any(r['state']=='finished' for r in final['requests'])
            report['checks'].append('network_metadata_during_active_batch_and_pending_to_finished')
            view=admin(operation='surface');assert view.get('image')
            report['checks'].append('live_view_refreshes_between_agent_calls')
            passed=call('navigate',url=url+'/challenge')
            assert passed.get('challenge',{}).get('state')=='cleared',passed
            assert 'Review the report' in passed['snapshot']['text']
            report['checks'].append('challenge_header_waits_for_actual_normal_navigation_without_solving')
            failed=call('navigate',expected=False,url=url+'/stuck',challenge_timeout_ms=350)
            assert failed['error']=='challenge_requires_handoff',failed
            assert admin(operation='surface')['challenge']['state']=='handoff_required'
            admin(operation='command',command='handoff',payload={})
            control=admin(operation='surface')['control_id']
            admin(operation='human_command',control_id=control,session_id=session,command='navigate',payload={'url':url})
            admin(operation='command',command='resume',payload={})
            assert call('snapshot')['snapshot']['text']
            report['checks'].append('bounded_challenge_timeout_operator_takeover_and_resume')
            cached=call('network_detail',network_id=post['id'],include_body=True)
            assert '"count":1' in cached['request']['body'],cached
            assert COUNTS['/api/save']==1
            report['checks'].append('cached_post_response_survives_navigation_without_resending')
            report.update(ok=True,network_reads_during_batch=len(samples),max_network_read_ms=round(max(samples),2),retained=call('network')['retained'])
            print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
            if args.serve:
                print(json.dumps({'review_api':base,'site':url,'session_id':session,'synthetic_only':True}),flush=True)
                while True:time.sleep(1)
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            report.update(error_type=type(exc).__name__,error=str(exc)[:2200],protection_reason=getattr(worker,'protection_reason',''));print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
        finally:
            worker=worker or managed_browser.worker_for(root,'work')
            if worker:worker.close();worker.done.wait(10)
            for server in (api,site):
                if server:server.shutdown();server.server_close()
    return 0 if report['ok'] else 1

if __name__=='__main__':raise SystemExit(main())
