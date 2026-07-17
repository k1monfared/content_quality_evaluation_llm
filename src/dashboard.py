"""Self-contained raw-data dashboard renderer for Phase C.

Produces a single HTML file with the per-passage rubric detail embedded as JSON
and rendered client-side. No framework, no external assets, no emojis. Works
from the local filesystem because the data is embedded rather than fetched.
"""
from __future__ import annotations

import json
from typing import Any

_CSS = """
:root { --bg:#0f1115; --panel:#171a21; --line:#2a2f3a; --ink:#e8ebf0;
        --muted:#9aa3b2; --accent:#4C72B0; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
header { padding:18px 22px 8px; }
h1 { font-size:20px; margin:0 0 6px; }
.disclaimer { background:#3a2b12; border:1px solid #6b5220; color:#f2d9a6;
              padding:10px 14px; border-radius:8px; margin:0 22px 12px;
              font-weight:600; }
.tabs { display:flex; gap:6px; padding:0 22px; border-bottom:1px solid var(--line); }
.tab { padding:8px 14px; cursor:pointer; color:var(--muted); border:1px solid transparent;
       border-bottom:none; border-radius:8px 8px 0 0; }
.tab.active { color:var(--ink); background:var(--panel); border-color:var(--line); }
.panel { display:none; padding:14px 22px 40px; }
.panel.active { display:block; }
.meta { color:var(--muted); margin:0 0 12px; max-width:1100px; }
.meta code { background:#20242e; padding:1px 5px; border-radius:4px; }
.legend { display:flex; flex-wrap:wrap; gap:14px; margin:8px 0 14px; color:var(--muted); }
.legend span { display:inline-flex; align-items:center; gap:6px; }
.sw { width:34px; height:14px; border-radius:3px; display:inline-block; border:1px solid #0006; }
.controls { margin:8px 0; color:var(--muted); }
.controls input { background:#20242e; border:1px solid var(--line); color:var(--ink);
                  padding:6px 8px; border-radius:6px; width:280px; }
.tablewrap { max-height:78vh; overflow:auto; border:1px solid var(--line);
             border-radius:8px; }
table { border-collapse:separate; border-spacing:0; font-size:12px; }
th,td { padding:4px 6px; white-space:nowrap; border-right:1px solid var(--line);
        border-bottom:1px solid var(--line); text-align:center; }
thead th { position:sticky; background:#11141b; z-index:3; cursor:pointer; user-select:none; }
thead tr.groups th { top:0; z-index:4; font-size:12px; }
thead tr.subs th { top:27px; font-weight:500; color:var(--muted); }
thead th.sorted { color:#fff; }
thead th .arrow { font-size:10px; color:var(--accent); }
td.num { color:#0c0e12; font-variant-numeric:tabular-nums; }
td.passage { max-width:360px; white-space:normal; text-align:left; color:var(--ink);
             cursor:pointer; }
td.passage.clip { max-height:52px; overflow:hidden; display:-webkit-box;
                  -webkit-line-clamp:3; -webkit-box-orient:vertical; }
td.id { color:var(--muted); font-family:monospace; }
.grp-h1 { background:#1d2740; } .grp-h2 { background:#1d2740; }
.grp-rh { background:#20222c; } .grp-judge { background:#122a2e; }
tbody tr:hover td { outline:1px solid #ffffff22; }
.tf-true { background:#2f7d4f; color:#eafff0; }
.tf-false { background:#9e3b3f; color:#ffecec; }
th.col-metric, td.col-metric { border-left:2px solid #e0a13a; border-right:2px solid #e0a13a; }
thead th.col-metric { color:#f2c879; }
.metric-note { color:#f2c879; }
.trylink { display:inline-block; background:var(--accent); color:#fff; font-weight:600;
           padding:6px 14px; border-radius:8px; text-decoration:none; margin-right:10px; }
.trylink:hover { filter:brightness(1.1); }
#panel-try { padding:0; }
.tryframe { width:100%; height:82vh; border:0; display:block; background:var(--bg); }
"""


def _js() -> str:
    return r"""
const DATA = JSON.parse(document.getElementById('data').textContent);
const DIMS = DATA.dims, PRUNED = DATA.pruned, JUDGES = DATA.judges;
const HUMANS = DATA.human_names;
const SCORECOLS = DIMS.concat(['flat','fitted','overall']);
const EVALS = HUMANS.concat(JUDGES);

// colour helpers -----------------------------------------------------------
function lerp(a,b,t){ return Math.round(a+(b-a)*t); }
function mix(c1,c2,t){ return `rgb(${lerp(c1[0],c2[0],t)},${lerp(c1[1],c2[1],t)},${lerp(c1[2],c2[2],t)})`; }
const RED=[201,58,63], WHITE=[255,255,255], GREEN=[62,150,86], BLUE=[33,110,190];
// human score 1..10 -> red-white-green diverging
function humanColor(v){
  if(v==null||isNaN(v)) return '';
  v=Math.max(1,Math.min(10,v)); const mid=5.5;
  if(v<=mid) return mix(RED,WHITE,(v-1)/(mid-1));
  return mix(WHITE,GREEN,(v-mid)/(10-mid));
}
// judge score -> white(far from avg human)..blue(match)
function judgeColor(dist,cap){
  if(dist==null||isNaN(dist)) return '';
  const t=Math.max(0,1-dist/cap); return mix(WHITE,BLUE,t);
}
function tfClass(b){ return b ? 'tf-true':'tf-false'; }

// Human cells are bias-normalized (continuous) on every dimension and on the
// overall, and the random human is their Monte-Carlo average, so all human-side
// cells are continuous. Judge dimension and overall scores are raw integers. The
// flat and fitted composites are derived and continuous for every rater.
function isDerived(field, ev){
  if(field==='flat'||field==='fitted') return true;
  if(JUDGES.includes(ev)) return false;
  return true;
}
function fmtScore(v, field, ev){
  if(v==null||isNaN(v)) return '';
  return isDerived(field, ev) ? v.toFixed(2) : String(Math.round(v));
}

// build header --------------------------------------------------------------
function buildHead(){
  const groups=[{k:'meta',label:'',span:2,cls:''}];
  groups.push({k:'human_1',label:'Human 1',span:SCORECOLS.length+1,cls:'grp-h1'});
  groups.push({k:'human_2',label:'Human 2',span:SCORECOLS.length+1,cls:'grp-h2'});
  groups.push({k:'random_human',label:'Random human',span:SCORECOLS.length+1,cls:'grp-rh'});
  JUDGES.forEach(j=>groups.push({k:j,label:j,span:SCORECOLS.length+5,cls:'grp-judge'}));
  const gtr=document.createElement('tr'); gtr.className='groups';
  groups.forEach(g=>{ const th=document.createElement('th'); th.colSpan=g.span;
    th.textContent=g.label; th.className=g.cls; gtr.appendChild(th); });
  const str=document.createElement('tr'); str.className='subs';
  const cols=[]; // each: {label, kind, ev?, field?, isJudge?}
  cols.push({label:'item', kind:'id'});
  cols.push({label:'passage', kind:'passage'});
  EVALS.forEach(ev=>{
    const isJudge=JUDGES.includes(ev);
    SCORECOLS.forEach(sc=>{
      const label = sc==='flat'?'flat':sc==='fitted'?'fit':sc==='overall'?'OVR':sc.slice(0,4);
      cols.push({label, ev, field:sc, isJudge, kind:'score'});
    });
    cols.push({label:'good', ev, field:'good', isJudge, kind:'good'});
    if(isJudge){
      cols.push({label:'minΔ', ev, field:'min_diff', isJudge, kind:'diff'});
      cols.push({label:'maxΔ', ev, field:'max_diff', isJudge, kind:'diff'});
      cols.push({label:'rhΔ*', ev, field:'rh_diff', isJudge, kind:'diff', metric:true});
      cols.push({label:'match', ev, field:'rh_match', isJudge, kind:'match', metric:true});
    }
  });
  cols.forEach((c,i)=>{ const th=document.createElement('th'); th.textContent=c.label;
    th.dataset.idx=i;
    th.title=(c.ev?c.ev+' · ':'')+(c.field||c.kind)+(c.metric?' (headline-metric basis)':'');
    if(c.metric) th.className='col-metric';
    th.addEventListener('click',()=>sortBy(i)); str.appendChild(th); });
  const thead=document.getElementById('thead'); thead.appendChild(gtr); thead.appendChild(str);
  return cols;
}

function cellValue(row,c){
  if(c.kind==='id') return row.item_id;
  if(c.kind==='passage') return row.text;
  const e=row.evals[c.ev]; if(!e) return null;
  return e[c.field];
}

let COLS, ROWS, sortIdx=-1, sortAsc=true;
function makeRowEl(row){
  const tr=document.createElement('tr');
  COLS.forEach(c=>{
    const td=document.createElement('td');
    if(c.kind==='id'){ td.className='id'; td.textContent=row.item_id; }
    else if(c.kind==='passage'){ td.className='passage clip'; td.textContent=row.text;
      td.addEventListener('click',()=>td.classList.toggle('clip')); }
    else {
      const e=row.evals[c.ev];
      if(!e){ td.textContent='n/a'; }
      else if(c.kind==='good'){ td.className=tfClass(e.good); td.textContent=e.good?'True':'False'; }
      else if(c.kind==='match'){ const b=e[c.field]; td.className=tfClass(b);
        td.textContent = b==null?'' : (b?'match':'mismatch'); }
      else if(c.kind==='diff'){ const v=e[c.field]; td.className='num';
        td.style.background=judgeColor(v,6); td.textContent=v==null?'':v.toFixed(2); }
      else { // score
        const v=e[c.field]; td.className='num';
        if(c.isJudge){ const h1=row.evals.human_1, h2=row.evals.human_2;
          const avg=(h1&&h2)?(h1[c.field]+h2[c.field])/2:null;
          const dist=(avg==null||v==null)?null:Math.abs(v-avg);
          td.style.background=judgeColor(dist,4);
        } else { td.style.background=humanColor(v); }
        td.textContent=fmtScore(v, c.field, c.ev);
      }
      if(c.metric) td.classList.add('col-metric');
    }
    tr.appendChild(td);
  });
  return tr;
}

function sortBy(i){
  if(sortIdx===i){ sortAsc=!sortAsc; } else { sortIdx=i; sortAsc=true; }
  const c=COLS[i];
  ROWS.sort((ra,rb)=>{
    let a=cellValue(ra,c), b=cellValue(rb,c);
    if(a==null) a=-Infinity; if(b==null) b=-Infinity;
    if(typeof a==='string'&&typeof b==='string') return sortAsc?a.localeCompare(b):b.localeCompare(a);
    return sortAsc?(a-b):(b-a);
  });
  document.querySelectorAll('thead tr.subs th').forEach(th=>{th.classList.remove('sorted');
    const s=th.querySelector('.arrow'); if(s) s.remove();});
  const th=document.querySelectorAll('thead tr.subs th')[i];
  th.classList.add('sorted'); const ar=document.createElement('span');
  ar.className='arrow'; ar.textContent=sortAsc?' ▲':' ▼'; th.appendChild(ar);
  render();
}

function render(){
  const tb=document.getElementById('tbody'); tb.innerHTML='';
  const q=document.getElementById('search').value.trim().toLowerCase();
  const frag=document.createDocumentFragment(); let shown=0;
  ROWS.forEach(row=>{ if(q && !row.text.toLowerCase().includes(q) && !row.item_id.includes(q)) return;
    frag.appendChild(makeRowEl(row)); shown++; });
  tb.appendChild(frag);
  document.getElementById('count').textContent=shown+' of '+ROWS.length+' passages';
}

function tab(id){ document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');
  document.getElementById('panel-'+id).classList.add('active');
  // Load the Try it iframe on first open so its Turnstile widgets only render
  // when the tab is actually viewed. Same origin, so Turnstile works normally.
  if(id==='try'){ const f=document.getElementById('tryframe');
    if(f && f.getAttribute('src')==='about:blank') f.src=f.dataset.src; } }

COLS=buildHead(); ROWS=DATA.rows.slice(); render();
document.getElementById('search').addEventListener('input',render);
"""


def render_html(payload: dict[str, Any]) -> str:
    data_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    w = payload["weights"]
    wtxt = ", ".join(f"{d} {w[d]:+.3f}" for d in payload["pruned"])
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Content quality raw data</title>
<style>{_CSS}</style></head>
<body>
<header>
  <h1>Wikipedia writing-quality evaluation: raw scored data</h1>
  <p style="margin:0"><a class="trylink" href="try.html">Try your own passage</a>
  <span style="color:var(--muted)">run the live judge on text you paste</span></p>
</header>
<div class="disclaimer">This is a portfolio demonstration. The human ratings are
simulated, the passage content and the model judgments are real.</div>
<div class="tabs">
  <div class="tab active" id="tab-raw" onclick="tab('raw')">Raw data</div>
  <div class="tab" id="tab-about" onclick="tab('about')">About the columns</div>
  <div class="tab" id="tab-try" onclick="tab('try')">Try it</div>
</div>

<div class="panel active" id="panel-raw">
  <p class="meta">Every one of the 500 passages, with the two simulated human
  reviewers, the Monte-Carlo random human, and each of the four judges scored on the
  seven-dimension rubric. The two humans and the random human are shown in
  <b>bias-normalized</b> form on every dimension and on the <b>OVR</b> (holistic
  overall), the per-rater additive bias removed, so their cells are continuous rather
  than whole numbers. The four judges are not bias-corrected, so their per-dimension
  and OVR cells are the <b>raw integer</b> ratings shown as given. For
  every rater, both composites of the refined {len(payload["pruned"])} dimensions
  ({", ".join(payload["pruned"])}) are shown: <b>flat</b> is the equal-weight average
  and <b>fit</b> the fitted-weight average using the study's fitted weights. Composites
  are derived, continuous quantities.
  <b>good</b> is True when the overall is at or above {payload["good_threshold"]:.0f}.
  Each judge shows three deltas against the humans: <b>minΔ</b> (absolute overall
  distance to the closer human, the best case), <b>maxΔ</b> (to the farther human, the
  worst case), and <span class="metric-note"><b>rhΔ*</b></span> (absolute distance to
  the random human). <span class="metric-note">rhΔ* is the per-item basis of the
  headline metric: each judge's correlation with the random human.</span> The
  <span class="metric-note"><b>match</b></span> column shows whether the judge's
  good-or-bad decision agrees with the random human's at the threshold. Human cells run
  red (1) to green (10); judge cells run white (far from the average human) to blue (a
  match); good and match cells are green for agreement and red for disagreement. Click a
  passage to expand it, click any header to sort.</p>
  <div class="legend">
    <span><span class="sw" style="background:rgb(201,58,63)"></span>1</span>
    <span><span class="sw" style="background:rgb(255,255,255)"></span>5.5</span>
    <span><span class="sw" style="background:rgb(62,150,86)"></span>10 (human)</span>
    <span><span class="sw" style="background:rgb(255,255,255)"></span>far</span>
    <span><span class="sw" style="background:rgb(33,110,190)"></span>match (judge)</span>
    <span><span class="sw tf-true" style="width:20px"></span>good True / match</span>
    <span><span class="sw tf-false" style="width:20px"></span>good False / mismatch</span>
    <span class="metric-note">rhΔ* and match are the headline-metric columns</span>
  </div>
  <div class="controls">
    <input id="search" placeholder="filter passages by text or item id">
    <span id="count" style="margin-left:12px"></span>
  </div>
  <div class="tablewrap"><table>
    <thead id="thead"></thead><tbody id="tbody"></tbody>
  </table></div>
</div>

<div class="panel" id="panel-about">
  <p class="meta">The rubric began with seven candidate dimensions:
  {", ".join(payload["dims"])}. A dimension-reduction analysis kept the subset whose
  LLM composite best tracks the random-human overall, dropping
  <b>{", ".join(payload["dropped"])}</b> and leaving the refined
  {len(payload["pruned"])}: {", ".join(payload["pruned"])}.</p>
  <p class="meta">The fitted composite applies these least-squares weights, the
  reconstruction of the human overall from the refined dimensions, to the
  {len(payload["pruned"])} refined dimensions (intercept {payload["intercept"]:+.3f}):<br>
  <code>{wtxt}</code></p>
  <p class="meta">The flat composite is the plain average of the same refined
  dimensions. Both composites, and the direct overall, are shown for every rater,
  the two humans, the random human, and all four judges, on the Raw data tab so the
  composite-versus-overall comparison can be inspected passage by passage.</p>
  <p class="meta">A reviewer, human or model, returns a whole-number 1 to 10 score.
  The judge cells show those raw integers directly. The two humans are shown
  bias-normalized instead: the study fits an additive item-plus-rater model per
  dimension and per overall, centers the rater biases to sum to zero, and subtracts
  each rater's bias, which turns every human score into a continuous, bias-corrected
  value on a comparable scale. The random human is the average of 1000 seeded random
  picks of one of the two normalized human evaluations per passage, so it too is
  continuous and close to but not exactly the mean of the two humans. The flat and
  fitted composites are derived and continuous for every rater.</p>
</div>

<div class="panel" id="panel-try">
  <iframe class="tryframe" id="tryframe" title="Try your own passage"
          data-src="try.html" src="about:blank" loading="lazy"></iframe>
</div>

<script id="data" type="application/json">{data_json}</script>
<script>{_js()}</script>
</body></html>
"""
