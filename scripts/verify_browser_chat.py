"""Real native-script / HTTP / Chromium acceptance for in-chat observations.
No external sites, live accounts, or model calls. Run with the project's Python.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nerya.api.local_server import build_server
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.integrations import managed_browser, browser_trace
from nerya.skills.builtin.browser.scripts import browser_session
from nerya.tools.native.skill import SkillIndex, script_run_handler
from nerya.tools.types import ToolCall

PAGE = '''<!doctype html><meta charset="utf-8"><title>Browser chat acceptance</title>
<style>body{font:20px system-ui;padding:40px}input,button{padding:12px;margin:12px}#pulse{width:120px;height:25px;background:#aab}</style>
<h1>Browser chat live test</h1><label>Name <input></label><button id="start">Start loading</button>
<p id="status">Ready</p><div id="pulse"></div><button id="once">Count</button><p id="count">Count 0</p>
<script>let n=0,t=0;document.querySelector('#once').onclick=()=>document.querySelector('#count').textContent='Count '+(++n);
document.querySelector('#start').onclick=()=>{document.querySelector('#status').textContent='Loading';setTimeout(()=>document.querySelector('#status').textContent='Finished loading',2400)};
setInterval(()=>document.querySelector('#pulse').style.width=(120+(++t%8)*20)+'px',120);</script>'''


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        data=PAGE.encode()
        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self,*args): pass


def main():
    report={'ok':False,'checks':[]}
    worker=api=site=None
    os.environ['NERYA_DISABLE_TUNNEL_RESTORE']='1'
    os.environ.setdefault('NERYA_VAULT_PASSPHRASE','synthetic-browser-chat-test-only')
    with tempfile.TemporaryDirectory(prefix='nerya-chat-browser-') as directory:
        root=Path(directory)
        try:
            site=ThreadingHTTPServer(('127.0.0.1',0),Site)
            threading.Thread(target=site.serve_forever,daemon=True).start()
            origin=f'http://127.0.0.1:{site.server_address[1]}'
            cfg=Config(paths=WorkspacePaths(root),data=deepcopy(DEFAULT_CONFIG))
            api=build_server(cfg,port=0,start_cron=False,start_continuous=False)
            threading.Thread(target=api.serve_forever,daemon=True).start()
            base=f'http://127.0.0.1:{api.server_address[1]}'
            os.environ['NERYA_API']=base
            def admin(**body):
                result=browser_session._request('POST','/browsers/desktop',payload=body)
                assert result.get('ok'),result
                return result
            index=SkillIndex([Path(browser_session.__file__).parents[2]])
            def native(call_id, **payload):
                result=script_run_handler(ToolCall(id=call_id,name='script_run',arguments={
                    'skill_id':'browser','name':'browser_session.py','args':['--json',json.dumps(payload)]}),
                    skill_index=index,cwd=root,conversation_id='chat-acceptance')
                return result
            opened=native('open-browser',operation='open',url=origin)
            worker=managed_browser.worker_for(root,'work')
            assert not opened.is_error,opened.text()
            access=admin(operation='surface')['agent_access']
            assert access['all_web'] and access['screenshots'] and access['downloads']
            report['checks'].append('automatic_open_without_manual_site_configuration')
            session=next(p.data['session_id'] for p in opened.content if p.type=='json')
            outputs=[]
            runner=threading.Thread(target=lambda:outputs.append(native('batch-live',operation='batch',session_id=session,steps=[
                {'action':'fill','target':{'label':'Name'},'text':'Synthetic preview'},
                {'action':'click','target':{'role':'button','name':'Start loading'}},
                {'action':'wait_for','target':{'text':'Finished loading'}},
                {'action':'click','target':{'role':'button','name':'Count'}},
            ])))
            runner.start()
            observed=[]; latencies=[]; frames=set(); deadline=time.monotonic()+15
            while runner.is_alive() and time.monotonic()<deadline:
                started=time.monotonic()
                view=admin(operation='trace',conversation_id='chat-acceptance',call_id='batch-live')
                latencies.append((time.monotonic()-started)*1000)
                if view.get('frame'): frames.add(view['frame']['frame_id'])
                if any(e.get('action')=='wait_for' and e.get('phase')=='started' for e in view['events']) and runner.is_alive():
                    observed.append(view)
                time.sleep(.08)
            runner.join(10)
            assert not runner.is_alive() and outputs and not outputs[0].is_error, outputs[0].text() if outputs else 'no output'
            assert observed and any(v.get('frame') for v in observed), 'no frame visible while batch is executing'
            assert len(frames)>3, f'no changing live frames: {frames}'
            final=admin(operation='trace',conversation_id='chat-acceptance',call_id='batch-live')
            completed=[e for e in final['events'] if e.get('phase')=='completed']
            assert len(completed)==4 and final['status']=='completed',final['events']
            assert max(latencies)<1000,latencies
            report['checks'].append('live_frames_and_started_steps_before_native_tool_returns')
            report['checks'].append('trace_reads_do_not_wait_for_browser_batch_queue')
            first=admin(operation='trace',conversation_id='chat-acceptance',call_id='batch-live',frame_id=completed[0]['frame_id'])
            assert first['frame'] and first['frame']['frame_id']==completed[0]['frame_id']
            assert not admin(operation='trace',conversation_id='another-chat',call_id='batch-live').get('frame')
            report['checks'].append('step_frame_replay_and_conversation_isolation')
            assert any(e.get('kind')=='visual' and e.get('action')=='fill' and e.get('boxes') for e in final['events'])
            assert any(e.get('kind')=='visual' and e.get('action')=='dom' and e.get('boxes') for e in final['events'])
            report['checks'].append('actual_element_bounds_and_dom_observation_visuals')
            blocked=browser_session._request('POST','/browsers/desktop',payload={'operation':'human_command','command':'new_tab','session_id':session})
            assert not blocked['ok'] and blocked['error']=='take_over_before_manual_input'
            visits=admin(operation='history')['history']
            assert any(r['url'].startswith(origin) for r in visits)
            admin(operation='trace_control',conversation_id='chat-acceptance',call_id='batch-live',control='handoff')
            paused=admin(operation='trace',conversation_id='chat-acceptance',call_id='batch-live')
            assert paused['paused'] and paused['frame'] is None
            control_id=admin(operation='surface')['control_id']
            manual=admin(operation='human_command',control_id=control_id,session_id=session,command='new_tab',payload={'url':origin+'/manual?private=omitted'})
            assert manual['human_control'] and manual.get('image') and len(manual['tabs'])==2
            admin(operation='human_command',control_id=control_id,session_id=session,command='close_tab',payload={})
            assert len(admin(operation='surface')['tabs'])==1
            assert all('private=' not in row['url'] for row in admin(operation='history')['history'])
            report['checks'].append('server_input_shield_takeover_manual_tabs_preview_and_history')
            admin(operation='trace_control',conversation_id='chat-acceptance',call_id='batch-live',control='resume')
            admin(operation='trace_control',conversation_id='chat-acceptance',call_id='batch-live',control='handoff')
            stale=browser_session._request('POST','/browsers/desktop',payload={'operation':'human_command','session_id':session,'control_id':control_id,'command':'new_tab'})
            assert stale['error']=='human_control_changed'
            assert len(admin(operation='surface')['tabs'])==1
            admin(operation='trace_control',conversation_id='chat-acceptance',call_id='batch-live',control='resume')
            report['checks'].append('in_chat_handoff_resume_and_stale_control_round_rejection')
            failed=native('batch-fails',operation='batch',session_id=session,steps=[
                {'action':'click','target':{'text':'missing'},'timeout_ms':100},
                {'action':'click','target':{'role':'button','name':'Count'}}])
            assert failed.is_error
            failure=admin(operation='trace',conversation_id='chat-acceptance',call_id='batch-fails')
            assert [e.get('phase') for e in failure['events'] if e['kind']=='step']==['started','failed','skipped']
            report['checks'].append('failed_and_skipped_actions_are_not_reported_as_success')
            moved=native('pointer-move',operation='move',session_id=session,x=200,y=220)
            assert not moved.is_error,moved.text()
            moved_trace=admin(operation='trace',conversation_id='chat-acceptance',call_id='pointer-move')
            assert any(e.get('action')=='move' and e.get('cursor')=={'x':200.0,'y':220.0} for e in moved_trace['events'])
            selected=native('selection',operation='select_text',session_id=session,target={'role':'heading','name':'Browser chat live test'})
            assert not selected.is_error,selected.text()
            selected_trace=admin(operation='trace',conversation_id='chat-acceptance',call_id='selection')
            assert any(e.get('action')=='select_text' and e.get('boxes') for e in selected_trace['events'])
            report['checks'].append('real_cursor_movement_and_native_text_selection')
            # Exercise reviewed extension add, restart, disable, enable and removal.
            # Package and key are synthetic, created inside the temporary test workspace.
            import base64
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            package=root/'test-extension';package.mkdir()
            key=rsa.generate_private_key(public_exponent=65537,key_size=2048).public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
            (package/'manifest.json').write_text(json.dumps({'manifest_version':3,'name':'Sidebar verification extension','version':'1.0','key':base64.b64encode(key).decode(),'action':{'default_popup':'popup.html'}}))
            (package/'popup.html').write_text('<!doctype html><button>Verify extension</button><script src="popup.js"></script>')
            (package/'popup.js').write_text('document.querySelector("button").onclick=e=>{e.target.textContent="Extension verified";};')
            reviewed=admin(operation='review_extension',path=str(package))['review']
            extension={**reviewed,'control_ui':True}
            blocked=browser_session._request('POST','/browsers/desktop',payload={'operation':'extension_apply','extensions':[extension]})
            assert blocked['error']=='take_over_before_changing_extensions'
            for enabled in (True,False,True):
                admin(operation='command',command='handoff',payload={})
                applied=admin(operation='extension_apply',extensions=[{**extension,'enabled':enabled}])
                assert applied['restarted'] and applied['config']['extensions'][0]['enabled'] is enabled
                worker=managed_browser.worker_for(root,'work')
                admin(operation='command',command='resume',payload={})
                opened=native('extension-open-'+str(enabled)+'-'+str(time.monotonic_ns()),operation='open',url=origin)
                assert not opened.is_error,opened.text()
                session=next(p.data['session_id'] for p in opened.content if p.type=='json')
                listed=native('extension-list-'+str(time.monotonic_ns()),operation='extensions',session_id=session)
                entries=next(p.data['extensions'] for p in listed.content if p.type=='json')
                assert len(entries)==(1 if enabled else 0)
                if enabled:
                    shown=native('extension-page-'+str(time.monotonic_ns()),operation='extension_open',session_id=session,extension_id=extension['extension_id'])
                    assert not shown.is_error,shown.text()
                    clicked=native('extension-click-'+str(time.monotonic_ns()),operation='click',session_id=session,target={'role':'button','name':'Verify extension'})
                    assert not clicked.is_error and 'Extension verified' in clicked.text(),clicked.text()
            admin(operation='command',command='handoff',payload={})
            admin(operation='extension_apply',extensions=[])
            worker=managed_browser.worker_for(root,'work')
            assert not admin(operation='surface')['config']['extensions']
            report['checks'].append('reviewed_mv3_add_disable_enable_remove_restart_and_real_ui')
            assert all('data:image' not in f.read_text() for f in (root/'state/browser_traces').glob('*.json'))
            browser_trace._TRACES.clear()
            history=admin(operation='trace',conversation_id='chat-acceptance',call_id='batch-live')
            assert history['frame_state']=='expired' and len([e for e in history['events'] if e.get('phase')=='completed'])==4
            report['checks'].append('persistent_step_log_without_persisting_authenticated_pixels')
            report.update(ok=True,observations_during_wait=len(observed),distinct_live_frames=len(frames),max_trace_read_ms=round(max(latencies),2))
        except Exception as exc:
            report.update(error_type=type(exc).__name__,error=str(exc)[:2000])
        finally:
            if worker: worker.close(); worker.done.wait(10)
            for server in (api,site):
                if server: server.shutdown(); server.server_close()
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['ok'] else 1


if __name__=='__main__': raise SystemExit(main())
