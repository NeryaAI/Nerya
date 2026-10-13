import {snapshot} from './core.mjs';
import {config} from './config.mjs';
import {installTransport} from './transport.mjs';
import {parseInputs, compare, observationAt} from './model.mjs';
const $ = id => document.getElementById(id);
const usd = value => new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',maximumFractionDigits:2}).format(value);
const price = value => new Intl.NumberFormat('en-US', {minimumFractionDigits:2,maximumFractionDigits:value < 1 ? 6 : 2}).format(value);
const fmt = value => new Intl.NumberFormat('en-US', {maximumFractionDigits:2}).format(value);
let data = null, loading = false;
const proxyBase = document.querySelector('meta[name="nerya-proxy-base"]')?.content ?? config.proxyBase;
let transportError = null;
try { installTransport(proxyBase); } catch (error) { transportError = error; }
$('transport-mode').textContent = proxyBase ? 'Live requests use the public-data proxy at ' + proxyBase + '. Only allowlisted public reads are forwarded.' : 'Direct mode: this browser requests Perpl and Monad public endpoints. Cross-origin permission is required.';
function element(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function status(message, state) { $('source-status').textContent = message; $('source-status').dataset.state = state; }
function emptyComparison(message) { $('comparison').replaceChildren(element('p', message, 'empty')); }
function clearObservation() {
  data = null;
  for (const key of ['mark','bid','ask','spread','fee','observed','age','block']) $(key).textContent = '—';
  $('network').textContent = 'Awaiting chain verification';
  $('chart').replaceChildren(); $('chart-detail').textContent = 'No observation available.';
  $('chart-period').textContent = 'Up to 72 hours · live source';
  $('raw-observation').textContent = 'No observation received.';
  $('candle-source').hidden = true;
  emptyComparison('Waiting for live data. The comparison uses the venue’s reported taker fee.');
}
function drawChart(candles) {
  const points = candles.filter(row => Number.isFinite(row.time) && Number.isFinite(row.close) && row.close > 0).sort((a,b) => a.time-b.time);
  const box = $('chart'); box.replaceChildren();
  if (points.length < 2) { box.append(element('p','Not enough valid closed hourly candles to draw a chart.','empty')); return; }
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns,'svg'); svg.setAttribute('viewBox','0 0 560 170'); svg.setAttribute('role','img');
  const title = document.createElementNS(ns,'title'); title.textContent = data.symbol + ': ' + points.length + ' closed hourly prices, from ' + price(points[0].close) + ' to ' + price(points.at(-1).close) + ' USD. Not a forecast.'; svg.append(title);
  const min = Math.min(...points.map(p=>p.close)), max = Math.max(...points.map(p=>p.close)), span = max-min || max*.01;
  for (const y of [18,82,146]) {
    const line = document.createElementNS(ns,'line');
    for (const [key,val] of Object.entries({x1:0,x2:560,y1:y,y2:y,stroke:'#333747','stroke-dasharray':'3 6'})) line.setAttribute(key,val);
    svg.append(line);
  }
  const start = points[0].time, duration = points.at(-1).time-start || 1;
  const path = document.createElementNS(ns,'path');
  path.setAttribute('d',points.map((p,i)=>(i?'L':'M')+((p.time-start)/duration*556+2).toFixed(2)+','+(146-(p.close-min)/span*128).toFixed(2)).join(' '));
  for (const [key,val] of Object.entries({stroke:'#b48bff','stroke-width':2,fill:'none','vector-effect':'non-scaling-stroke'})) path.setAttribute(key,val);
  svg.append(path); box.append(svg);
  $('chart-period').textContent = points.length + ' closed hours';
  $('chart-detail').textContent = new Date(start).toLocaleString() + ' → ' + new Date(points.at(-1).time).toLocaleString() + ' · Low $' + price(min) + ' / high $' + price(max) + ' · Gaps retain their time spacing';
}
function resultColumn(result, label, candidate) {
  const section = element('section',undefined,'result'+(candidate?' candidate':''));
  section.append(element('p',label,'result-label'),element('h3',usd(result.inputs.notional)),element('span',result.status,'verdict'+(result.reasons.length?'':' within')));
  const dl = element('dl');
  for (const [title,value,total] of [['Adverse move loss',result.moveLoss],['Round-trip costs',result.roundTripCosts],['Stress loss',result.totalLoss,true]]) {
    const row = element('div',undefined,total?'total':undefined); row.append(element('dt',title),element('dd',usd(value))); dl.append(row);
  }
  const ratio = result.totalLoss/result.lossBudget*100;
  const meter = element('div',undefined,'meter'); meter.setAttribute('aria-hidden','true');
  const fill = element('span'); fill.style.width = Math.min(100,Math.max(0,ratio))+'%'; meter.append(fill);
  section.append(dl,meter,element('p',fmt(ratio)+'% of the loss budget','meter-caption'));
  if (result.reasons.length) { const reasons = element('ul',undefined,'reasons'); for (const reason of result.reasons) reasons.append(element('li',reason)); section.append(reasons); }
  return section;
}
function update() {
  let inputs;
  try { inputs = parseInputs(Object.fromEntries(new FormData($('inputs')))); $('input-error').hidden = true; }
  catch (error) { $('input-error').textContent = error.message; $('input-error').hidden = false; emptyComparison('Correct the scenario inputs to calculate a comparison.'); return; }
  for (const button of document.querySelectorAll('[data-notional]')) button.setAttribute('aria-pressed',String(Number(button.dataset.notional) === inputs.notional));
  if (!data || loading) return;
  try {
    const result = compare(data,inputs), current = observationAt(data);
    $('age').textContent = fmt(current.sourceAgeSeconds)+' seconds';
    const stale = current.sourceAgeSeconds > 120 || current.sourceAgeSeconds < -10;
    status(stale ? 'Observation is stale or clock-skewed. Refresh before interpreting this scenario.' : 'Public observation received · Monad chain 143 verified · no order submitted',stale?'stale':'live');
    const header = element('div',undefined,'budget-line'); header.append(element('span','Shared hypothetical loss budget'),element('strong',usd(result.candidate.lossBudget)));
    const columns = element('div',undefined,'compare-columns'); columns.append(resultColumn(result.baseline,'ORIGINAL EXPOSURE',false),resultColumn(result.candidate,'CANDIDATE EXPOSURE',true));
    const foot = element('div',undefined,'comparison-foot');
    const reduction = element('p'); reduction.append(element('strong',usd(result.reduction)+' less hypothetical stress loss'),' at the candidate size.');
    foot.append(reduction,element('p',result.candidate.maxNotionalForBudget === null ? 'No finite budget ceiling under a zero-loss assumption.' : 'Mathematical notional ceiling: '+usd(result.candidate.maxNotionalForBudget)+' under these assumptions.'),element('p','This is a scenario result. It does not authorize a trade.'));
    $('comparison').replaceChildren(header,columns,foot);
  } catch (error) { emptyComparison('Calculation unavailable: '+error.message); }
}
async function refresh() {
  if (loading) return;
  loading = true; clearObservation(); $('fetch-error').hidden = true;
  $('refresh').disabled = true; $('symbol').disabled = true; $('market-data').setAttribute('aria-busy','true');
  status('Reading Perpl context, verifying Monad, then loading closed hourly candles…','loading');
  try {
    if (transportError) throw transportError;
    data = await snapshot($('symbol').value);
    $('mark').textContent = '$'+price(data.mark); $('bid').textContent = '$'+price(data.bid); $('ask').textContent = '$'+price(data.ask);
    $('spread').textContent = fmt(data.spreadBps)+' bps'; $('fee').textContent = fmt(data.takerFeeBps)+' bps';
    $('network').textContent = data.network+' · chain '+data.chainId; $('observed').textContent = new Date(data.observedAt).toLocaleString(); $('block').textContent = String(data.blockNumber);
    drawChart(data.closedHourlyCandles); $('raw-observation').textContent = JSON.stringify(data,null,2);
    const until = Math.floor(Date.parse(data.fetchedAt)/3600000)*3600000;
    $('candle-source').href = 'https://app.perpl.xyz/api/v1/market-data/'+data.marketId+'/candles/3600/'+(until-72*3600000)+'-'+(until-1); $('candle-source').hidden = false;
  } catch (error) {
    clearObservation(); $('fetch-error').textContent = error.message+' No observation or comparison has been substituted. Retry with Refresh live data.'; $('fetch-error').hidden = false;
    status('Live observation unavailable','error'); emptyComparison('Live public data is unavailable. A hypothetical result will appear only after a successful observation.');
  } finally {
    loading = false; $('refresh').disabled = false; $('symbol').disabled = false; $('market-data').setAttribute('aria-busy','false'); update();
  }
}
$('inputs').addEventListener('submit',event=>event.preventDefault());
$('inputs').addEventListener('input',event=>{
  if (event.target.id === 'notional-range') $('notional').value = event.target.value;
  if (event.target.id === 'notional' && event.target.value !== '' && Number.isFinite(Number(event.target.value))) $('notional-range').value = event.target.value;
  update();
});
for (const button of document.querySelectorAll('[data-notional]')) button.addEventListener('click',()=>{ $('notional').value = button.dataset.notional; $('notional-range').value = button.dataset.notional; update(); });
$('refresh').addEventListener('click',refresh); $('symbol').addEventListener('change',refresh);
// No automatic network polling. Age the observation while the page is open.
setInterval(()=>{ if (data && !loading) update(); },5000);
void refresh();
