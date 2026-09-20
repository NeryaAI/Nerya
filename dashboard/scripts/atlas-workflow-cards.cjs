const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('@playwright/test');
const root=path.resolve(__dirname,'../test-results/workflow-usability-release');
const images=path.join(root,'screenshots');
const groups=[
 ['脚本、数据源与策略Agent',['macd_agent-script-main-py-essential','macd_agent-source-data_sources-bars-essential','macd_agent-agent-runtime-essential']],
 ['策略目标、运行时间与风控',['macd_agent-strategy-btc_macd_observer_15m-essential','scheduled_agent-scheduler-trading-essential','macd_agent-risk-policy-essential']],
 ['账户、复盘数据与改进方案',['macd_agent-account-binance_paper-essential','evolution-evidence-review-essential','evolution-proposal-tuning-essential']],
 ['验证、人工确认与版本应用',['evolution-validation-tuning-essential','evolution-approval-operator-essential','evolution-apply-version-essential']],
 ['观察反馈、复盘Agent与复盘时间',['evolution-observation-feedback-essential','evolution-agent-tuner-essential','evolution-scheduler-tuning-essential']],
 ['自定义数据、市场配置与高级设置',['macd_agent-source-data_sources-bars-advanced','macd_agent-source-markets-BINANCE-BTCUSDT-essential','macd_agent-agent-runtime-advanced']],
];
(async()=>{const browser=await chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:1230,height:1000},deviceScaleFactor:1});try{for(let i=0;i<groups.length;i++){
const [title,prefixes]=groups[i];const cols=prefixes.map(prefix=>fs.readdirSync(images).filter(f=>f.startsWith(prefix+'-scroll-')&&f.endsWith('.png')).sort().map(f=>`<img src="screenshots/${f}" alt="${f}">`).join(''));
const content=`<!doctype html><meta charset="utf-8"><style>body{margin:16px;background:#0d0f17;color:#eee;font:16px/1.8 system-ui}h1{font-size:23px;margin:0}p{margin:4px 0 16px;color:#bbc1d1;font-size:13px}main{display:flex;gap:16px;align-items:start}section{flex:1;min-width:0}img{display:block;width:100%;height:auto;margin-bottom:14px;box-shadow:0 0 0 1px #333848}</style><h1>${title}</h1><p>真实页面截图拼版 · 每一列是一张卡片从上到下的连续滚动位置，并非新的界面布局。</p><main>${cols.map(c=>'<section>'+c+'</section>').join('')}</main>`;
const file=path.join(root,`cards-page-${i+1}.html`);fs.writeFileSync(file,content);await page.goto('file://'+file);await page.evaluate(()=>Promise.all([...document.images].map(im=>im.decode())));await page.screenshot({path:path.join(root,`nerya-cards-page-${i+1}.png`),fullPage:true,animations:'disabled'});console.log(`nerya-cards-page-${i+1}.png`);
}}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1});
