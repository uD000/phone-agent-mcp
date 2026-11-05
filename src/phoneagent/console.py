"""The human's local console page (served by the daemon on loopback, behind a token)."""
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phone Agent</title>
<style>
:root{--bg:#f4f4f5;--card:#fff;--fg:#18181b;--mut:#71717a;--line:#e4e4e7;--ok:#15803d;--bad:#b91c1c;--acc:#2563eb}
@media(prefers-color-scheme:dark){:root{--bg:#0b0b0d;--card:#151518;--fg:#f4f4f5;--mut:#a1a1aa;--line:#27272a;--ok:#4ade80;--bad:#f87171;--acc:#60a5fa}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--card)}
h1{font-size:16px;margin:0;flex:1}.pill{padding:2px 10px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.ok{color:var(--ok)}.bad{color:var(--bad)}main{display:grid;grid-template-columns:minmax(240px,380px) 1fr;gap:16px;padding:16px;max-width:1100px;margin:auto}
@media(max-width:760px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
#screen{width:100%;border-radius:10px;background:#000;display:block;aspect-ratio:1216/2656;object-fit:contain}
select{font:inherit;padding:5px 8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);max-width:46vw}button{font:inherit;padding:7px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.kill{background:var(--bad);color:#fff;border-color:var(--bad)}button:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}table{width:100%;border-collapse:collapse;font-size:12.5px}
td,th{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mut);font-weight:500}
td.msg{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.mut{color:var(--mut)}pre{margin:0;white-space:pre-wrap}
</style></head><body>
<header><h1>Phone Agent</h1><select id="dev" aria-label="Device"></select><span id="conn" class="pill">…</span><span id="kill" class="pill"></span></header>
<main>
<section class="card"><img id="screen" alt="Live phone screen"><div class="mut" id="age"></div></section>
<section style="display:grid;gap:16px;align-content:start">
 <div class="card"><div class="row">
  <button id="connect">Connect</button><select id="q" aria-label="Wi-Fi video quality"><option value="2">Wi-Fi 2 Mbit/s (coolest)</option><option value="4">Wi-Fi 4 Mbit/s</option><option value="6">Wi-Fi 6 Mbit/s</option><option value="8">Wi-Fi 8 Mbit/s (sharpest)</option></select><button id="stop" class="kill">KILL: stop all input</button><button id="resume">Resume…</button></div>
  <div id="status" class="mut"></div><div id="msg" class="mut" role="status"></div></div>
 <div class="card"><b>Policy</b> <span class="mut">(read-only here; change it with the CLI)</span><pre id="policy" class="mut"></pre></div>
 <div class="card"><b>Recent actions</b><table><thead><tr><th>Time</th><th>Tool</th><th></th><th>Detail</th></tr></thead><tbody id="log"></tbody></table></div>
</section></main>
<script>
const $=id=>document.getElementById(id);let DEV='';const q=(p)=>p+(p.includes('?')?'&':'?')+'device='+encodeURIComponent(DEV);
async function api(path,opt){const r=await fetch(path,Object.assign({credentials:'same-origin',headers:{'X-PA':'1','Content-Type':'application/json'}},opt));
  if(!r.ok&&r.status!==503)throw new Error(path+' '+r.status);return r}
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function status(){try{const s=await (await api(q('/api/status'))).json();
 const opts=(s.devices||[]);const have=[...$('dev').options].map(o=>o.value).join('|');const want=opts.map(d=>d.udid).join('|');
 if(have!==want){$('dev').innerHTML=opts.map(d=>`<option value="${esc(d.udid)}">${esc(d.name)} · ${esc(d.platform)} · ${esc(d.connection)}${d.connected?' ●':''}</option>`).join('');if(!opts.length)$('dev').innerHTML='<option>No devices found</option>'}
 if(!DEV&&opts.length){DEV=(opts.find(d=>d.connected)||opts[0]).udid;frame()}$('dev').value=DEV;
 $('conn').textContent=s.connected?('Connected · '+s.connection):'Not connected';$('conn').className='pill '+(s.connected?'ok':'bad');
 $('kill').textContent=s.kill_switch_on?'KILL SWITCH ON':'';$('kill').className='pill '+(s.kill_switch_on?'bad':'');
 $('status').textContent=(s.error?'Last error: '+s.error+' · ':'')+(s.video_fps!=null?s.video_fps+' fps from the phone':'frame #'+s.frame_id)+(s.max_bitrate?' · '+(s.max_bitrate/1e6)+' Mbit/s':'');
 if(s.max_bitrate){const m=String(Math.round(s.max_bitrate/1e6));if([...$('q').options].some(o=>o.value===m)&&document.activeElement!==$('q'))$('q').value=m}
 $('policy').textContent=JSON.stringify(s.policy,null,1)}catch(e){$('conn').textContent='Console offline'}}
function frame(){$('screen').src=q('/api/stream.mjpeg?w=460&fps=60&_='+Date.now());$('age').textContent='Live'}
$('screen').onerror=()=>setTimeout(frame,2000);
async function log(){try{const rows=await (await api('/api/audit?n=25')).json();$('log').innerHTML=rows.reverse().map(a=>`<tr><td class="mut">${new Date(a.ts*1000).toLocaleTimeString()}</td><td>${esc(a.tool)}</td><td class="${a.ok?'ok':'bad'}">${a.ok?'✓':'✗'}</td><td class="msg" title="${esc(a.message)}">${esc((a.message||'').slice(0,90))}</td></tr>`).join('')}catch(e){}}
async function post(path,body,label){$('msg').textContent=label+'…';try{const r=await api(path,{method:'POST',body:JSON.stringify(body||{})});const j=await r.json();$('msg').textContent=j.message||'';status()}catch(e){$('msg').textContent='Failed: '+e.message}}
$('dev').onchange=()=>{DEV=$('dev').value;status();frame()};$('q').onchange=()=>post('/api/quality',{mbit:Number($('q').value)},'Changing video quality');$('connect').onclick=()=>post('/api/connect',{udid:DEV},'Connecting');$('stop').onclick=()=>post('/api/kill',{},'Stopping');
$('resume').onclick=()=>post('/api/resume',{},'Waiting for you to confirm in the macOS dialog');
status();frame();log();setInterval(status,2000);setInterval(log,2500);
</script></body></html>"""
