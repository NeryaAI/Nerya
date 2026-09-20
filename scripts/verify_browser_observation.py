"""Real Chromium observation budgets on synthetic local content, no model calls."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import uuid
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nerya.api.local_server import build_server
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.integrations import managed_browser
from nerya.skills.builtin.browser.scripts import browser_session

ARTICLE = ' '.join(f'Research paragraph {i}: 本地合成样例，核对来源日期与证据。' for i in range(420))
PAGE = ('<!doctype html><meta charset="utf-8"><title>Observation budget test</title>'
        '<main><article id="report">'+ARTICLE+'</article><section id="actions">'
        + ''.join(f'<button>Report {i}</button>' for i in range(120))
        + '</section><section id="form"><label>Name<input></label><button id="save">Save example</button>'
          '<p id="result">Saved 0</p></section></main><script>let n=0;document.querySelector("#save").onclick=()=>{document.querySelector("#result").textContent="Saved "+(++n)};</script>')


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        data = PAGE.encode()
        self.send_response(200)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *args):
        pass


def main():
    report = {'ok':False,'checks':[]}
    worker = api = site = None
    previous_api = os.environ.get('NERYA_API')
    os.environ['NERYA_DISABLE_TUNNEL_RESTORE'] = '1'
    os.environ.setdefault('NERYA_VAULT_PASSPHRASE','synthetic-observation-test-only')
    with tempfile.TemporaryDirectory(prefix='nerya-observation-') as directory:
        root = Path(directory)
        try:
            site = ThreadingHTTPServer(('127.0.0.1',0),Site)
            threading.Thread(target=site.serve_forever,daemon=True).start()
            url = f'http://127.0.0.1:{site.server_address[1]}'
            cfg = Config(paths=WorkspacePaths(root),data=deepcopy(DEFAULT_CONFIG))
            api = build_server(cfg,port=0,start_cron=False,start_continuous=False)
            threading.Thread(target=api.serve_forever,daemon=True).start()
            os.environ['NERYA_API'] = f'http://127.0.0.1:{api.server_address[1]}'
            session = ''
            def call(operation, expected=True, **body):
                nonlocal session
                request = {'operation':operation,'request_id':uuid.uuid4().hex,**body}
                if session:
                    request['session_id'] = session
                result = browser_session.run(**request)
                assert result.get('ok') is expected, {k:v for k,v in result.items() if k!='image'}
                if operation == 'open' and expected:
                    session = result['session_id']
                return result
            opened = call('open',url=url)
            worker = managed_browser.worker_for(root,'work')
            compact = opened['snapshot']
            assert compact['mode']=='compact' and len(compact['text'])<=1600 and len(compact['elements'])==60
            assert compact['next_element_offset']==60 and compact['next_text_offset']==1600
            full = call('snapshot',observation='full')['snapshot']
            assert len(full['elements'])==122 and len(full['text'])==10000
            assert any(row.get('name')=='Save example' for row in full['elements'])
            report['checks'].append('default_compact_and_explicit_full_real_dom')
            repeated = call('snapshot',known_text_hash=compact['text_hash'])['snapshot']
            assert repeated['text_unchanged'] and 'text' not in repeated
            assert repeated['elements'][0]['ref'] != compact['elements'][0]['ref']
            report['checks'].append('unchanged_text_omitted_but_element_refs_refreshed')
            next_page = call('snapshot',element_offset=60,text_offset=1600)['snapshot']
            assert next_page['elements'][0]['name']=='Report 60'
            assert next_page['text']!=compact['text'] and next_page['next_element_offset']==120
            report['checks'].append('element_and_text_pagination_reaches_later_content')
            scoped = call('snapshot',scope='#form')['snapshot']
            assert len(scoped['elements'])==2 and 'Saved 0' in scoped['text']
            stale = call('click',expected=False,target={'ref':compact['elements'][0]['ref']})
            assert stale['error']=='stale_reference_refresh_snapshot'
            report['checks'].append('scoped_observation_and_old_ref_rejection')
            receipt_id = uuid.uuid4().hex
            receipt = call('click',target={'role':'button','name':'Save example'},observation='none',request_id=receipt_id)
            assert 'snapshot' not in receipt and receipt['needs_observation']
            assert receipt['next_action']=='observe_before_next_action'
            replay = call('click',target={'role':'button','name':'Save example'},observation='none',request_id=receipt_id)
            assert replay['replayed']
            result = call('read',target={'selector':'#result'})
            assert result['text']=='Saved 1' and 'snapshot' not in result
            report['checks'].append('receipt_only_mode_requires_verification_and_replay_does_not_click_twice')
            call('click',expected=False,target={'role':'button','name':'Save example'},max_elements=True)
            assert call('read',target={'selector':'#result'})['text']=='Saved 1'
            local = call('read',target={'selector':'#report'},offset=2000,max_chars=500)
            assert len(local['text'])==500 and local['next_offset']==2500
            report['checks'].append('invalid_budgets_stop_before_click_and_targeted_read_is_bounded')
            encode = lambda value: json.dumps(value,ensure_ascii=False,separators=(',',':'))
            sizes = {name:len(encode(value).encode()) for name,value in [('full',full),('compact',compact),('scoped',scoped),('unchanged',repeated)]}
            assert sizes['compact'] < sizes['full'] and sizes['scoped'] < sizes['compact']
            report['observation_json_bytes'] = sizes
            report['compact_vs_full_byte_reduction_percent'] = round((1-sizes['compact']/sizes['full'])*100,1)
            report['token_counts'] = None
            # Use an already-installed tokenizer only. No dependency installation or paid API.
            if importlib.util.find_spec('tiktoken'):
                try:
                    import tiktoken
                    tokenizer = tiktoken.get_encoding('o200k_base')
                    report['token_counts'] = {'encoding':'o200k_base','sample_only':True,**{name:len(tokenizer.encode(encode(value))) for name,value in [('full',full),('compact',compact),('scoped',scoped)]}}
                except Exception:
                    report['tokenizer_unavailable'] = True
            report['ok'] = True
        except Exception as exc:
            report.update(error_type=type(exc).__name__,error=str(exc)[:2500])
        finally:
            worker = worker or managed_browser.worker_for(root,'work')
            if worker:
                worker.close()
                worker.done.wait(10)
            for server in (api,site):
                if server:
                    server.shutdown()
                    server.server_close()
            if previous_api is None:
                os.environ.pop('NERYA_API',None)
            else:
                os.environ['NERYA_API'] = previous_api
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
