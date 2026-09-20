// Local-only display of an existing screenshot for capture/export diagnostics.
// Never opens a user browser profile or sends API/model requests.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');const {chromium}=require('@playwright/test');
const root=path.resolve(__dirname,'../test-results/main-chat-real-0918-service');
const image=path.resolve(root,process.env.REVIEW_IMAGE||'script-initial/02-conversation-page.png');
assert.ok(image.startsWith(root+path.sep)&&image.endsWith('.png')&&fs.existsSync(image));
(async()=>{const b=await chromium.launch({headless:false,args:['--window-position=60,80','--window-size=1400,1000']});try{
 const c=await b.newContext({viewport:{width:1360,height:890}});const p=await c.newPage();await p.goto(pathToFileURL(image).href);await p.waitForSelector('img');await p.bringToFront();
 const cd=await c.newCDPSession(p);const w=await cd.send('Browser.getWindowForTarget');
 const view=await p.evaluate(()=>({innerWidth,innerHeight,outerWidth,outerHeight,screenX,screenY,image:{width:document.querySelector('img').width,height:document.querySelector('img').height}}));
 console.log(JSON.stringify({window:w,view,source:path.relative(root,image),scope:'Temporary browser displays only the existing test screenshot'}));
 const finished=path.join(root,'finish-image-preview');const start=Date.now();while(Date.now()-start<150000&&!fs.existsSync(finished))await new Promise(r=>setTimeout(r,500));
 }finally{await b.close();}})().catch(e=>{console.error(e);process.exitCode=1});
