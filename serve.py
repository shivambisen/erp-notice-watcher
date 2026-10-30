#!/usr/bin/env python3
"""Local web UI for managing who gets the ERP notice alerts.

    ./venv/bin/python serve.py      then open http://127.0.0.1:8765

Binds to loopback only. It writes state/recipients.json and never touches .env,
so no password ever passes through the web layer.
"""
import json
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

import config

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$")
# E.164: leading +, country code, 7-15 digits total.
PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
# A bridge target may also be a WhatsApp chat id: a group (...@g.us) or a
# direct chat (...@c.us).
CHAT_ID_RE = re.compile(r"^\d{5,25}(-\d+)?@[gc]\.us$")
LABEL = "com.shivam.erpnoticewatcher"

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ERP Notice Board</title><style>
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--fg:#1a1c1f;--muted:#6b7280;
--line:#e4e6eb;--accent:#0b6bcb;--danger:#c0392b;--ok:#1a7f4b}
@media(prefers-color-scheme:dark){:root{--bg:#15171a;--card:#1e2126;--fg:#e8eaed;
--muted:#9aa3ad;--line:#2d3138;--accent:#5aa3f0;--danger:#e57373;--ok:#5cc98d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
.wrap{max-width:660px;margin:0 auto;padding:28px 16px 48px}
h1{font-size:21px;margin:0 0 4px}.sub{color:var(--muted);font-size:13px;margin:0 0 18px}
nav{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:20px}
nav a{padding:9px 15px;font-size:14px;text-decoration:none;color:var(--muted);
border-bottom:2px solid transparent;margin-bottom:-1px;font-weight:500}
nav a:hover{color:var(--fg)}
nav a.on{color:var(--accent);border-bottom-color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:20px;margin-bottom:16px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
margin:0 0 14px;font-weight:600}
ul{list-style:none;margin:0 0 14px;padding:0}
li{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--line)}
li:last-child{border-bottom:none}.mail{flex:1;word-break:break-all}
button{font:inherit;border-radius:7px;border:1px solid var(--line);background:transparent;
color:var(--fg);padding:7px 13px;cursor:pointer}button:hover{border-color:var(--accent)}
.x{color:var(--danger);border:none;padding:4px 9px;font-size:18px;line-height:1}
.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:500}
.primary:disabled{opacity:.5;cursor:default}
.row{display:flex;gap:9px;flex-wrap:wrap}
input{flex:1;min-width:200px;font:inherit;padding:9px 12px;border-radius:7px;
border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.msg{margin-top:12px;font-size:13px;min-height:19px}
.err{color:var(--danger)}.ok{color:var(--ok)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
td{padding:5px 0;vertical-align:top}td:first-child{color:var(--muted);width:44%}
.empty{color:var(--muted);font-style:italic;padding:9px 0}
.head{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:14px}
.when{font-size:12px;color:var(--muted)}
.split{display:flex;gap:16px;align-items:flex-start}
.listpane{flex:0 0 50%;min-width:0}
.detailpane{flex:1;min-width:0}
.nrow{display:block;width:100%;text-align:left;border:1px solid transparent;
border-bottom:1px solid var(--line);border-radius:6px;padding:9px 10px;cursor:pointer;
background:transparent;color:var(--fg)}
.nrow:hover{background:var(--bg)}
.nrow.sel{background:var(--bg);border-color:var(--accent)}
.nrow .r1{display:flex;gap:7px;align-items:center;margin-bottom:3px}
/* company takes the whole first line; the timestamp moves down to the snippet
   row, otherwise names like "IIT Kharagpur: Preference Form" get cut to 8 chars */
.nrow .co{font-weight:600;font-size:13.5px;flex:1;min-width:0;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nrow .r2{display:flex;gap:8px;align-items:baseline}
.nrow .snip{font-size:12px;color:var(--muted);flex:1;min-width:0;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nrow:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.tag{font-size:10px;text-transform:uppercase;letter-spacing:.04em;padding:1px 6px;
border-radius:99px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.tag.urgent{color:var(--danger);border-color:var(--danger)}
.ts{font-size:11px;color:var(--muted);margin-left:auto;white-space:nowrap}
.pager{display:flex;align-items:center;gap:8px;margin-top:12px;font-size:12.5px;
color:var(--muted);justify-content:space-between}
.pager span{text-align:center;flex:1;min-width:0}
.pager button{padding:5px 11px}
.detailpane{border:1px solid var(--line);border-radius:8px;padding:16px;
position:sticky;top:16px;max-height:calc(100vh - 120px);overflow-y:auto}
.dtitle{font-weight:600;font-size:16px;margin-bottom:4px}
.dmeta{font-size:12px;color:var(--muted);margin-bottom:12px;
display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.dbody{font-size:14px;line-height:1.65;white-space:pre-wrap;word-break:break-word;
padding:12px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line);
margin-bottom:12px}
.back{display:none}
@media(max-width:720px){
  .split{display:block}
  .listpane,.detailpane{flex:none;width:auto}
  .detailpane{position:static;max-height:none;margin-top:0}
  body.reading .listpane{display:none}
  body:not(.reading) .detailpane{display:none}
  .back{display:inline-block;margin-bottom:12px}
}
.notice{border:1px solid var(--line);border-left:3px solid var(--accent);
border-radius:8px;padding:12px 14px;margin-bottom:10px}
.notice:last-child{margin-bottom:0}
.ntitle{font-weight:600;font-size:15px;margin-bottom:8px}
.notice table{font-size:13px}
.notice td:first-child{width:40%;color:var(--muted);padding-right:10px}
[hidden]{display:none!important}
</style></head><body><div class="wrap">
<h1>ERP Notice Board</h1>
<p class="sub">IIT Kharagpur placement board, checked every 30 minutes.</p>

<nav>
  <a href="/notices" data-view="notices">Notice board</a>
  <a href="/settings" data-view="settings">Settings</a>
</nav>

<section id="view-notices">
  <div class="card">
    <div class="head"><h2 style="margin:0">On the board</h2>
      <span class="when"><span id="count"></span>
        <span id="when" style="margin-left:8px"></span></span></div>
    <div class="split">
      <div class="listpane">
        <div id="notices"></div>
        <div class="pager" id="pager"></div>
      </div>
      <div class="detailpane" id="detail">
        <button class="back" onclick="closeDetail()">&larr; Back to list</button>
        <div id="detailbody"><div class="empty">Select a notice to read it.</div></div>
      </div>
    </div>
    <div class="msg" id="nmsg"></div>
    <div class="row" style="margin-top:14px">
      <button onclick="notices(1)">Refresh</button>
      <button id="loginbtn" onclick="relogin()" hidden>Log in now</button>
    </div>
  </div>
</section>

<section id="view-settings" hidden>
  <div class="card"><h2>Recipients</h2>
    <ul id="list"></ul>
    <div class="row">
      <input id="email" type="email" placeholder="someone@example.com"
             autocomplete="off" spellcheck="false">
      <button onclick="add()">Add</button>
    </div>
    <div class="msg" id="msg"></div>
    <div class="row" style="margin-top:14px">
      <button class="primary" id="save" onclick="save()" disabled>Save changes</button>
      <button onclick="test()">Send test mail</button>
    </div>
  </div>
  <div class="card">
    <div class="head"><h2 style="margin:0">WhatsApp numbers</h2>
      <span class="when" id="wastate"></span></div>
    <ul id="walist"></ul>
    <div class="row">
      <input id="phone" type="tel" placeholder="+919876543210"
             autocomplete="off" spellcheck="false">
      <button onclick="addwa()">Add</button>
    </div>
    <div class="msg" id="wamsg"></div>
    <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--line)">
      <h2>Try it</h2>
      <div class="row">
        <input id="wtext" placeholder="Type a message to send to the numbers above"
               value="Test from my ERP notice watcher.">
      </div>
      <div class="row" style="margin-top:9px">
        <button class="primary" onclick="wsend(0)">Send on WhatsApp</button>
        <button onclick="wsend(1)">Preview request</button>
        <button onclick="wgroups()">Find my groups</button>
      </div>
      <pre id="wout" hidden style="margin-top:12px;padding:12px;background:var(--bg);
border:1px solid var(--line);border-radius:7px;font-size:12px;overflow-x:auto;
white-space:pre-wrap;word-break:break-word"></pre>
    </div>
  </div>
  <div class="card"><h2>Watcher status</h2><table id="status"></table></div>
</section>
</div><script>
const $=i=>document.getElementById(i);
let mails=[], nums=[], dirty=false, view='notices';

/* ---- routing: /notices and /settings are real URLs, switched without reload ---- */
function show(name, push){
  view = (name==='settings') ? 'settings' : 'notices';
  $('view-notices').hidden = view!=='notices';
  $('view-settings').hidden = view!=='settings';
  document.querySelectorAll('nav a').forEach(a=>
    a.classList.toggle('on', a.dataset.view===view));
  document.title = view==='settings' ? 'ERP Alerts \u2014 Settings' : 'ERP Notice Board';
  if(push) history.pushState({view},'', '/'+view);
  refresh();
}
document.querySelectorAll('nav a').forEach(a=>a.addEventListener('click',e=>{
  e.preventDefault(); show(a.dataset.view, true);
}));
addEventListener('popstate',()=>show(location.pathname.replace(/^\//,'')||'notices',false));

function refresh(){ view==='settings' ? load() : notices(0); }

/* ---- notices ---- */
function ago(ts){
  if(!ts) return 'never fetched';
  const s=Math.max(0,Date.now()/1000-ts);
  if(s<60) return 'checked just now';
  if(s<3600) return 'checked '+Math.round(s/60)+' min ago';
  if(s<86400) return 'checked '+Math.round(s/3600)+' h ago';
  return 'checked '+Math.round(s/86400)+' d ago';
}
const PAGE_SIZE = 20;
let allRows = [], meta = {}, page = 0, selected = null;

function fmtWhen(s){ return String(s || '').trim(); }

function stampOf(r){
  const tc = (meta.time_col === undefined ? -1 : meta.time_col);
  const s = (tc >= 0 && r[tc]) ? String(r[tc]).trim() : '';
  const m = s.match(/^(\d{2})-(\d{2})-(\d{4})(?:\s+(\d{2}):(\d{2}))?/);
  return m ? new Date(+m[3], +m[2]-1, +m[1], +(m[4]||0), +(m[5]||0)).getTime() : -1;
}

async function notices(live){
  if(live){ $('nmsg').textContent='Fetching…'; $('nmsg').className='msg'; }
  const j=await(await fetch('/api/notices'+(live?'?refresh=1':''))).json();
  meta = j;
  $('when').textContent=ago(j.fetched_at);
  $('loginbtn').hidden = !j.expired;
  $('nmsg').textContent=j.note||''; $('nmsg').className='msg '+(j.note?'err':'');

  // Grid order differs between boards (54 is newest-first, 37 oldest-first),
  // so sort on the notice timestamp rather than trusting row order.
  const rows = j.rows.slice();
  if((j.time_col ?? -1) >= 0 && rows.some(r => stampOf(r) > 0)){
    rows.sort((a,b) => stampOf(b) - stampOf(a));
  }
  const changed = rows.length !== allRows.length;
  allRows = rows;
  if(changed){ page = 0; }
  $('count').textContent = rows.length
    ? rows.length+' notice'+(rows.length===1?'':'s')+', newest first' : '';
  drawList();
}

function drawList(){
  const box=$('notices'), pg=$('pager');
  box.innerHTML=''; pg.innerHTML='';
  if(!allRows.length){
    box.innerHTML='<div class="empty">Nothing on the board right now. '
      +'You will be emailed when something appears.</div>';
    return;
  }
  const pages = Math.max(1, Math.ceil(allRows.length / PAGE_SIZE));
  if(page >= pages) page = pages-1;
  const start = page*PAGE_SIZE;
  const slice = allRows.slice(start, start+PAGE_SIZE);
  const tcol = meta.title_col ?? 0;
  const bcol = meta.body_col ?? -1;
  const tc   = meta.time_col ?? -1;

  slice.forEach((r, i)=>{
    const idx = start + i;
    const b=document.createElement('button');
    b.className='nrow'+(selected===idx?' sel':'');
    b.onclick=()=>openDetail(idx);

    const r1=document.createElement('div'); r1.className='r1';
    const kind=String(r[2]||'').trim();
    if(kind){
      const tg=document.createElement('span');
      tg.className='tag'+(/urgent/i.test(kind)?' urgent':'');
      tg.textContent=kind; r1.append(tg);
    }
    const co=document.createElement('span'); co.className='co';
    co.textContent=String(r[tcol]||'Notice').trim()||'Notice'; r1.append(co);
    b.append(r1);

    const r2=document.createElement('div'); r2.className='r2';
    const sn=document.createElement('span'); sn.className='snip';
    sn.textContent=String((bcol>=0?r[bcol]:'')||'').trim();
    r2.append(sn);
    if(tc>=0 && r[tc]){
      const ts=document.createElement('span'); ts.className='ts';
      ts.textContent=fmtWhen(r[tc]); r2.append(ts);
    }
    b.append(r2);
    box.append(b);
  });

  const mk=(label,target,disabled)=>{
    const x=document.createElement('button'); x.textContent=label;
    x.disabled=!!disabled;
    x.onclick=()=>{ page=target; selected=null; drawList();
                    $('notices').scrollIntoView({block:'nearest'}); };
    return x;
  };
  pg.append(mk('← Prev', page-1, page===0));
  const info=document.createElement('span');
  info.textContent='Page '+(page+1)+' of '+pages
    +'  ('+(start+1)+'–'+Math.min(start+PAGE_SIZE, allRows.length)
    +' of '+allRows.length+')';
  pg.append(info);
  pg.append(mk('Next →', page+1, page>=pages-1));
}

function openDetail(idx){
  selected = idx;
  const r = allRows[idx];
  if(!r) return;
  const tcol = meta.title_col ?? 0;
  const bcol = meta.body_col ?? -1;
  const tc   = meta.time_col ?? -1;
  const hide = new Set(meta.hide_cols || []);
  const host = $('detailbody');
  host.innerHTML='';

  const h=document.createElement('div'); h.className='dtitle';
  h.textContent=String(r[tcol]||'Notice').trim()||'Notice'; host.append(h);

  const m=document.createElement('div'); m.className='dmeta';
  [[1,''],[2,'']].forEach(([i])=>{
    const v=String(r[i]||'').trim();
    if(!v) return;
    const tg=document.createElement('span');
    tg.className='tag'+(/urgent/i.test(v)?' urgent':''); tg.textContent=v; m.append(tg);
  });
  if(tc>=0 && r[tc]){
    const s=document.createElement('span'); s.textContent=fmtWhen(r[tc]); m.append(s);
  }
  host.append(m);

  if(bcol>=0 && String(r[bcol]||'').trim()){
    const bd=document.createElement('div'); bd.className='dbody';
    bd.textContent=String(r[bcol]).trim(); host.append(bd);
  }

  const tb=document.createElement('table');
  r.forEach((cell,i)=>{
    if(i===tcol||i===bcol||hide.has(i)||!cell||!String(cell).trim()) return;
    const tr=document.createElement('tr');
    const a=document.createElement('td'); a.textContent=(meta.columns||[])[i]||('Field '+(i+1));
    const b2=document.createElement('td'); b2.textContent=cell;
    tr.append(a,b2); tb.append(tr);
  });
  if(tb.children.length) host.append(tb);

  document.body.classList.add('reading');
  drawList();
  $('detail').scrollIntoView({block:'nearest'});
}

function closeDetail(){
  document.body.classList.remove('reading');
  selected=null; drawList();
}

async function relogin(){
  $('nmsg').textContent='Logging in \u2014 an OTP is being sent and read automatically\u2026';
  $('nmsg').className='msg';
  const j=await(await fetch('/api/notices?login=1')).json();
  $('nmsg').textContent=j.note||'Logged in.'; $('nmsg').className='msg '+(j.note?'err':'ok');
  notices(0);
}

/* ---- settings ---- */
const say=(t,c='')=>{$('msg').textContent=t;$('msg').className='msg '+c};
function draw(){
  $('list').innerHTML = mails.length ? ''
    : '<li class="empty">No recipients \u2014 no alerts will be sent.</li>';
  mails.forEach((m,i)=>{
    const li=document.createElement('li');
    const s=document.createElement('span'); s.className='mail'; s.textContent=m;
    const b=document.createElement('button'); b.className='x'; b.textContent='\u00d7';
    b.title='Remove'; b.onclick=()=>{mails.splice(i,1);dirty=true;draw()};
    li.append(s,b); $('list').append(li);
  });
  $('save').disabled=!dirty;
}
const sayw=(t,c='')=>{$('wamsg').textContent=t;$('wamsg').className='msg '+c};
function draww(){
  $('walist').innerHTML = nums.length ? ''
    : '<li class="empty">No numbers \u2014 WhatsApp alerts will not be sent.</li>';
  nums.forEach((n,i)=>{
    const li=document.createElement('li');
    const s=document.createElement('span'); s.className='mail'; s.textContent=n;
    const b=document.createElement('button'); b.className='x'; b.textContent='\u00d7';
    b.title='Remove'; b.onclick=()=>{nums.splice(i,1);dirty=true;draww();draw()};
    li.append(s,b); $('walist').append(li);
  });
}
function addwa(){
  const v=$('phone').value.trim().replace(/[\s()-]/g,'');
  if(!v) return;
  if(!/^\+[1-9]\d{6,14}$/.test(v))
    return sayw('Use international format with the country code, e.g. +919876543210.','err');
  if(nums.includes(v)) return sayw('Already in the list.','err');
  nums.push(v); $('phone').value=''; dirty=true; sayw(''); draww(); draw();
}
$('phone').addEventListener('keydown',e=>{if(e.key==='Enter')addwa()});

async function wgroups(){
  sayw('Asking the bridge for your groups\u2026');
  const j=await(await fetch('/api/wa/groups')).json();
  const out=$('wout');
  if(j.error){ out.hidden=true; return sayw(j.error,'err'); }
  const gs=j.groups||[];
  if(!gs.length){ out.hidden=true;
    return sayw('Linked, but no groups found on this account.','err'); }
  out.hidden=false; out.innerHTML='';
  const h=document.createElement('div');
  h.textContent='Click a group to add it as a target:';
  h.style.marginBottom='8px'; out.append(h);
  gs.forEach(g=>{
    const b=document.createElement('button');
    b.textContent=g.name+'  ('+g.id+')';
    b.style.cssText='display:block;width:100%;text-align:left;margin-bottom:5px';
    b.onclick=()=>{
      if(nums.includes(g.id)) return sayw('Already added.','err');
      nums.push(g.id); dirty=true; draww(); draw();
      sayw('Added \u2014 press Save changes to keep it.','ok');
    };
    out.append(b);
  });
  sayw('');
}

async function wsend(dry){
  const text=$('wtext').value.trim();
  if(!text) return sayw('Type a message first.','err');
  sayw(dry?'Building request\u2026':'Sending\u2026');
  const r=await fetch('/api/whatsapp',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text,dry_run:!!dry})});
  const j=await r.json();
  const out=$('wout');
  if(!r.ok){ out.hidden=true; return sayw(j.error||'Failed.','err'); }
  if(j.dry_run){
    out.hidden=false;
    out.textContent='Would send to: '+j.numbers.join(', ')
      +'\nDelivery mode: '+j.request.mode+'\n\n'
      +j.request.method+' '+j.request.url+'\nAuthorization: '+j.request.auth
      +'\n\n'+JSON.stringify(j.request.body,null,1);
    return sayw('Nothing was sent \u2014 this is the exact request.','ok');
  }
  out.hidden=true;
  const f=Object.keys(j.result.failed);
  sayw('Sent to '+j.result.sent.length+' number(s)'
       +(f.length?('; failed: '+f.map(n=>n+' ('+j.result.failed[n]+')').join(', ')):'.'),
       f.length?'err':'ok');
}

function add(){
  const v=$('email').value.trim();
  if(!v) return;
  if(!/^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$/.test(v))
    return say('That does not look like an email address.','err');
  if(mails.some(m=>m.toLowerCase()===v.toLowerCase())) return say('Already in the list.','err');
  mails.push(v); $('email').value=''; dirty=true; say(''); draw();
}
$('email').addEventListener('keydown',e=>{if(e.key==='Enter')add()});
async function save(){
  say('Saving\u2026');
  const r=await fetch('/api/recipients',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({recipients:mails,whatsapp:nums})});
  const j=await r.json();
  if(!r.ok) return say(j.error||'Could not save.','err');
  mails=j.recipients; nums=j.whatsapp||[]; dirty=false; draw(); draww();
  say(j.warning || 'Saved. The next check will use this list.', j.warning?'err':'ok');
}
async function test(){
  // Testing what is on screen, not what was last saved: otherwise addresses you
  // just typed are silently left out of the test.
  if(dirty){
    say('Saving changes first\u2026');
    await save();
    if(dirty) return;           // save failed; its error is already shown
  }
  say('Sending test mail\u2026');
  const r=await fetch('/api/test',{method:'POST'}); const j=await r.json();
  let m = r.ok?('Test mail sent to '+j.sent_to.join(', ')):(j.error||'Send failed.');
  if(j.whatsapp) m += ' | WhatsApp: '+j.whatsapp.sent.length+' sent, '
                      +Object.keys(j.whatsapp.failed).length+' failed';
  if(j.whatsapp_error) m += ' | WhatsApp: '+j.whatsapp_error;
  say(m, r.ok?'ok':'err');
}
async function load(){
  const j=await(await fetch('/api/state')).json();
  if(!dirty){mails=j.recipients.slice();nums=(j.whatsapp||[]).slice();draw();draww()}
  $('wastate').textContent = j.wa_enabled
    ? ('sending via '+j.wa_provider)
    : 'not configured \u2014 see README';
  $('status').innerHTML='';
  for(const [k,v] of j.status){
    const tr=document.createElement('tr');
    const a=document.createElement('td'); a.textContent=k;
    const b=document.createElement('td'); b.textContent=v;
    tr.append(a,b); $('status').append(tr);
  }
}

show(location.pathname.replace(/^\//,'')||'notices', false);
setInterval(refresh, 15000);
</script></body></html>"""


def _status():
    rows = []
    try:
        seen = json.loads(config.SEEN_FILE.read_text()) if config.SEEN_FILE.exists() else {}
    except Exception:
        seen = {}
    rows.append(("Notices remembered", str(len(seen.get("seen", [])))))
    rows.append(("Seeded", "yes" if seen.get("seeded") else "no"))
    rows.append(("ERP session cached", "yes" if config.COOKIE_FILE.exists() else "no (will log in)"))

    if config.LOG_FILE.exists():
        mtime = datetime.fromtimestamp(config.LOG_FILE.stat().st_mtime, timezone.utc)
        mins = (datetime.now(timezone.utc) - mtime).total_seconds() / 60
        rows.append(("Last activity", f"{mins:.0f} min ago" if mins >= 1 else "just now"))
        try:
            tail = [ln for ln in config.LOG_FILE.read_text().splitlines() if ln.strip()]
            if tail:
                rows.append(("Last log line", tail[-1].split(": ", 1)[-1][:70]))
        except Exception:
            pass

    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
        line = next((l for l in out.splitlines() if LABEL in l), None)
        if line:
            parts = line.split()
            rows.append(("Scheduled job", f"loaded (last exit {parts[1]})"))
        else:
            rows.append(("Scheduled job", "not loaded - run ./install-launchd.sh"))
    except Exception:
        pass
    return rows


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        route = self.path.split("?")[0].rstrip("/") or "/"
        # Every view is the same document; the script picks which section to show
        # from the URL, so deep links and reloads land on the right tab.
        if route in ("/", "/index.html", "/notices", "/settings"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/state":
            import whatsapp
            return self._send(200, json.dumps({
                "recipients": config.load_recipients(),
                "whatsapp": config.load_whatsapp_numbers(),
                "wa_enabled": whatsapp.enabled(),
                "wa_provider": config.WHATSAPP_PROVIDER,
                "status": _status(),
            }))
        if self.path == "/api/wa/groups":
            import urllib.error
            import urllib.request
            try:
                with urllib.request.urlopen(
                    f"{config.WA_BRIDGE_URL}/chats", timeout=20
                ) as r:
                    return self._send(200, r.read())
            except urllib.error.HTTPError as exc:
                return self._send(200, json.dumps(
                    {"error": exc.read().decode("utf-8", "replace")[:200]}))
            except Exception:
                return self._send(200, json.dumps({
                    "error": "No bridge running. Start it with: "
                             "cd wa-bridge && node bridge.js"}))
        if self.path.startswith("/api/notices"):
            return self._send(200, json.dumps(self._notices()))
        self._send(404, json.dumps({"error": "not found"}))

    def _notices(self) -> dict:
        """Board contents. Cached by default so merely opening the page never
        touches the ERP; ?refresh=1 re-fetches with the stored session; ?login=1
        is the only path allowed to start an OTP login."""
        import store

        mode = "cached"
        if "refresh=1" in self.path:
            mode = "refresh"
        elif "login=1" in self.path:
            mode = "login"

        snapshot = store.load_notices()
        try:
            import db
            if db.enabled():
                rows = db.load_notices()
                if rows:
                    snapshot = {"rows": rows,
                                "fetched_at": snapshot.get("fetched_at")}
        except Exception as exc:
            log_note = f"Database unreachable ({exc}); showing local copy."
            snapshot = dict(snapshot, note=log_note)

        result = {
            "columns": config.NOTICE_COLUMNS,
            "title_col": config.NOTICE_TITLE_COL,
            "time_col": config.NOTICE_TIME_COL,
            "body_col": config.NOTICE_BODY_COL,
            "hide_cols": config.NOTICE_HIDE_COLS,
            "rows": snapshot.get("rows", []),
            "fetched_at": snapshot.get("fetched_at"),
            "mode": mode,
            "note": snapshot.get("note", ""),
        }
        if mode == "cached":
            return result

        try:
            import erp_client
            client = erp_client.ErpClient()
            if mode == "login":
                rows = client.fetch_notices()          # may trigger an OTP
            else:
                rows = erp_client.parse_rows(client._raw_fetch())  # session only
            store.save_notices(rows)
            result.update(rows=rows, fetched_at=time.time())
        except Exception as exc:
            if type(exc).__name__ == "SessionExpired":
                result["note"] = (
                    "The saved ERP session has expired. The next scheduled check "
                    "will log in again, or use 'Log in now' to do it immediately "
                    "(that sends an OTP to your mailbox)."
                )
                result["expired"] = True
            elif type(exc).__name__ == "TruncatedResponse":
                result["note"] = (
                    "The ERP sent an incomplete response (it does this "
                    "occasionally on the large notice board). Showing the last "
                    "stored copy \u2014 try Refresh again in a moment."
                )
            elif isinstance(exc, requests.exceptions.ConnectionError):
                result["note"] = (
                    "Could not reach erp.iitkgp.ac.in. Check your internet "
                    "connection (the ERP is also unreachable from outside the "
                    "campus network without VPN). Showing the last saved copy."
                )
            elif isinstance(exc, requests.exceptions.Timeout):
                result["note"] = "The ERP did not respond in time. Showing the last saved copy."
            else:
                result["note"] = f"{type(exc).__name__}: {exc}"
        return result

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"

        if self.path == "/api/recipients":
            try:
                wanted = json.loads(raw or b"{}").get("recipients", [])
            except json.JSONDecodeError:
                return self._send(400, json.dumps({"error": "bad JSON"}))

            body = json.loads(raw or b"{}")
            wanted = body.get("recipients", [])
            wanted_wa = body.get("whatsapp", [])

            class PHONE_OR_CHAT:
                @staticmethod
                def match(v):
                    return PHONE_RE.match(v) or CHAT_ID_RE.match(v)

            def clean(items, rx, label):
                out, seen = [], set()
                for entry in items:
                    val = str(entry).strip().replace(" ", "")
                    if not rx.match(val):
                        raise ValueError(f"Invalid {label}: {entry}")
                    if val.lower() not in seen:
                        seen.add(val.lower())
                        out.append(val)
                return out

            try:
                cleaned = clean(wanted, EMAIL_RE, "email address")
                cleaned_wa = clean(
                    wanted_wa, PHONE_OR_CHAT,
                    "number or group id (use +919876543210, or pick a group)")
            except ValueError as exc:
                return self._send(400, json.dumps({"error": str(exc)}))

            tmp = config.RECIPIENTS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"recipients": cleaned, "whatsapp": cleaned_wa}, indent=1))
            tmp.replace(config.RECIPIENTS_FILE)

            try:
                import db
                if db.enabled():
                    db.init()
                    db.save_recipients(cleaned, cleaned_wa)
            except Exception as exc:
                # The file write already succeeded, so alerts still go to the
                # right people; surface the DB problem rather than hiding it.
                return self._send(200, json.dumps({
                    "recipients": cleaned, "whatsapp": cleaned_wa,
                    "warning": f"Saved locally, but not to the database: {exc}"}))
            config.MAIL_TO = cleaned or config.load_recipients()
            return self._send(200, json.dumps(
                {"recipients": cleaned, "whatsapp": cleaned_wa}))

        if self.path == "/api/whatsapp":
            import importlib

            import whatsapp
            importlib.reload(config)
            importlib.reload(whatsapp)
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send(400, json.dumps({"error": "bad JSON"}))

            text = (body.get("text") or "").strip()
            if not text:
                return self._send(400, json.dumps({"error": "Type a message first."}))
            numbers = config.load_whatsapp_numbers()
            if not numbers:
                return self._send(400, json.dumps(
                    {"error": "Add at least one number above, then Save."}))

            # Dry run always works, even with no credentials, so the exact
            # outgoing request can be inspected before anything is configured.
            if body.get("dry_run"):
                return self._send(200, json.dumps({
                    "dry_run": True,
                    "numbers": numbers,
                    "request": whatsapp.build_request(numbers[0], text),
                }))

            try:
                return self._send(200, json.dumps(
                    {"dry_run": False, "result": whatsapp.send(text, numbers)}))
            except Exception as exc:
                return self._send(400, json.dumps(
                    {"error": f"{type(exc).__name__}: {exc}"}))

        if self.path == "/api/test":
            import importlib

            import notifier
            importlib.reload(config)   # pick up the file we just wrote
            importlib.reload(notifier)
            try:
                notifier.send_alert(
                    "test message",
                    "Alerts for the ERP placement board will arrive at this address.",
                )
                out = {"sent_to": config.MAIL_TO}
                try:
                    import whatsapp
                    importlib.reload(whatsapp)
                    if whatsapp.enabled():
                        out["whatsapp"] = whatsapp.send(
                            "Test message from your ERP notice watcher.")
                except Exception as exc:
                    out["whatsapp_error"] = f"{type(exc).__name__}: {exc}"
                return self._send(200, json.dumps(out))
            except Exception as exc:
                return self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"}))

        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *args):
        pass  # keep the console quiet


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    print(f"Recipient manager: {url}   (Ctrl-C to stop)")
    threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)), daemon=True).start()
    try:
        HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
