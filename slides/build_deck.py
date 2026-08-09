#!/usr/bin/env python3
"""Build the X Arcade pitch deck as one browser-native HTML presentation.

Forked from the Adjacency review deck pipeline. The project-specific charts
and figures have been replaced with X Arcade content, and the palette is the
app's own: electric cyan on near-black. Presenter notes toggle with N.
Keyboard, touch, print, and mobile-landscape controls are retained.
"""

from __future__ import annotations

import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
document = json.loads((HERE / "_deck_data.json").read_text(encoding="utf-8"))
result = document.get("result", document)
SLIDES = result["slides"]
DESIGN = result["design"]
PAL = DESIGN["palette"]
FONTS = DESIGN["fonts"]
TWEETS = result.get("tweets", document.get("tweets", {}))

CHARTS = {
    "gates": {
        "kind": "table",
        "head": ["Gate", "What it checks"],
        "rows": [
            ["G_SOURCE", "post and all 5 replies present, within length bounds"],
            ["G_SLURS", "denylist scan over the post and every reply"],
            ["G_DECOY_COUNT", "exactly one imposter, and the round points at it"],
            ["G_AUTHOR", "real replies carry real handles, never the decoy marker"],
            ["G_URL", "no links in reply text"],
        ],
    },
    "loop": {"kind": "loop"},
    "pipeline": {"kind": "pipeline"},
    "flywheel": {"kind": "flywheel"},
    # Tweet cards rendered natively from committed quote data, so there is no
    # dependency on loading x.com and no login-wall screenshot risk. Quotes are
    # verbatim public posts, with handle, date, and URL cited on the card.
    "bier_mission": {"kind": "tweet_hero", "tweet": TWEETS.get("mission", {})},
    "bier_receipts": {"kind": "tweet_row", "tweets": TWEETS.get("receipts", [])},
}

FIGURES = {
    "share_card": {
        "src": "share_card.jpg",
        "cap": (
            "Imagine-generated share card, a committed demo asset. The card a "
            "winner would post back to X. The demo stages that post-back "
            "rather than posting live."
        ),
    }
}

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<title>X Arcade: games generated from live X</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="__FONTS_HREF__" rel="stylesheet">
<style>
:root{
  --bg:__bg__;--bgAlt:__bgAlt__;--ink:__ink__;--inkDim:__inkDim__;
  --accent:__accent__;--accent2:__accent2__;--pcrf:__pcrf__;--pigrpo:__pigrpo__;
  --good:__good__;--warn:__warn__;--grid:__grid__;
  --fh:'Space Grotesk',sans-serif;--fb:'Inter',sans-serif;--fm:'JetBrains Mono',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:#030509;color:var(--ink);font-family:var(--fb);overflow:hidden}
#stageWrap{position:fixed;inset:0;display:flex;align-items:center;justify-content:center}
#stage{position:relative;width:1280px;height:720px;background:var(--bg);overflow:hidden;
  box-shadow:0 30px 120px rgba(0,0,0,.65);transform-origin:center center}
#progress{position:absolute;top:0;left:0;height:3px;background:var(--accent);z-index:40;transition:width .3s ease}
.slide{position:absolute;inset:0;padding:58px 80px 76px;display:none;flex-direction:column;opacity:0}
.slide.active{display:flex;opacity:1}
.kicker{font-family:var(--fm);font-size:13px;letter-spacing:.20em;text-transform:uppercase;color:var(--inkDim);
  display:flex;align-items:center;gap:10px;margin-bottom:22px}
.kicker .dot{width:7px;height:7px;border-radius:50%;background:var(--sys,var(--accent));box-shadow:0 0 12px var(--sys,var(--accent))}
h1.title{font-family:var(--fh);font-weight:600;font-size:40px;line-height:1.11;letter-spacing:-.02em;max-width:27ch}
.title .em{color:var(--sys,var(--accent))}
.titleRule{width:64px;height:2px;background:var(--sys,var(--accent));margin-top:16px;border-radius:2px}
.body{margin-top:24px;display:flex;flex-direction:column;gap:12px;max-width:62ch}
.body li{list-style:none;position:relative;padding-left:22px;font-size:19px;line-height:1.42;color:#dcecf5}
.body li::before{content:"";position:absolute;left:0;top:11px;width:9px;height:1.5px;background:var(--sys,var(--accent))}
.hl{display:flex;gap:34px;margin-top:auto;padding-top:20px;flex-wrap:wrap}
.stat{display:flex;flex-direction:column;gap:3px}
.stat .v{font-family:var(--fh);font-weight:600;font-size:28px;color:var(--ink);letter-spacing:-.01em}
.stat .l{font-family:var(--fm);font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--inkDim)}
.foot{position:absolute;left:80px;right:80px;bottom:28px;display:flex;justify-content:space-between;align-items:center;
  font-family:var(--fm);font-size:11px;letter-spacing:.11em;text-transform:uppercase;color:var(--inkDim);
  border-top:1px solid var(--grid);padding-top:11px}
.foot .sec{color:var(--sys,var(--inkDim))}
.layout-title{justify-content:center}
.layout-title h1.title{font-size:54px;max-width:23ch}
.layout-title .sub{margin-top:20px;font-size:19px;color:var(--inkDim);max-width:75ch;line-height:1.5}
.layout-closing{justify-content:center}
.layout-closing h1.title{font-size:46px}
.split{display:grid;grid-template-columns:1fr 1fr;gap:42px;align-items:center;flex:1;margin-top:6px}
.split .col-r{align-self:stretch;display:flex;align-items:center;justify-content:center;min-width:0}
.chartWrap{flex:1;margin-top:16px;display:flex;flex-direction:column;min-height:0}
.chartWrap svg{width:100%;flex:1;min-height:260px}
.split .chartWrap{width:100%;margin-top:0}
.cap{font-size:12px;color:var(--inkDim);margin-top:8px;line-height:1.4;max-width:100ch}
.figWrap{flex:1;margin-top:13px;display:flex;flex-direction:column;align-items:center;min-height:0}
.figWrap img{max-width:100%;max-height:360px;object-fit:contain;border:1px solid var(--grid);border-radius:9px;background:#0b141e}
.split .figWrap{margin-top:0;justify-content:center}
table.tbl{width:100%;border-collapse:collapse;margin-top:12px;font-size:15px;table-layout:fixed}
table.tbl th,table.tbl td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--grid);vertical-align:top;line-height:1.35;overflow-wrap:anywhere}
table.tbl thead th{font-family:var(--fm);font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--accent);border-bottom:1px solid var(--inkDim)}
table.tbl td:first-child{font-family:var(--fm);font-size:13px;color:var(--accent);width:32%}
.tweetHero{flex:1;margin-top:14px;display:flex;align-items:center;justify-content:center;min-height:0}
.tweetRow{flex:1;margin-top:14px;display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:18px;align-items:stretch;min-height:0}
.tw{background:var(--bgAlt);border:1px solid var(--grid);border-radius:14px;padding:20px 22px;display:flex;flex-direction:column;gap:12px;min-width:0}
.tw.hero{max-width:760px;padding:26px 30px}
.tw .twhead{display:flex;align-items:center;gap:11px}
.tw .av{width:38px;height:38px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--pcrf));flex:none;
  display:flex;align-items:center;justify-content:center;font-family:var(--fh);font-weight:600;color:#04070b;font-size:16px}
.tw .who{display:flex;flex-direction:column;line-height:1.2;min-width:0}
.tw .nm{font-family:var(--fb);font-weight:600;font-size:15px;color:var(--ink);display:flex;align-items:center;gap:5px}
.tw .nm .vf{color:var(--accent);font-size:14px}
.tw .hn{font-family:var(--fm);font-size:12px;color:var(--inkDim)}
.tw .xmark{margin-left:auto;font-family:var(--fh);font-weight:700;font-size:18px;color:var(--inkDim)}
.tw .twtext{font-family:var(--fb);font-size:15px;line-height:1.5;color:#e3f0f7;white-space:pre-wrap;overflow-wrap:anywhere;flex:1}
.tw.hero .twtext{font-size:18px;line-height:1.55}
.tw .twfoot{font-family:var(--fm);font-size:11px;color:var(--inkDim);letter-spacing:.04em;border-top:1px solid var(--grid);padding-top:9px;
  display:flex;justify-content:space-between;gap:10px;overflow-wrap:anywhere}
.tweetRow .tw .twtext{font-size:13.5px;line-height:1.46}
#notes{position:fixed;left:0;right:0;bottom:0;max-height:42vh;overflow:auto;background:rgba(8,11,20,.97);
  border-top:2px solid var(--accent);padding:18px 26px;z-index:60;display:none;backdrop-filter:blur(6px)}
#notes.show{display:block}
#notes .nh{font-family:var(--fm);font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--accent);margin-bottom:8px}
#notes .nb{font-size:15px;line-height:1.6;color:#cdd6ea;max-width:120ch}
#hud{position:fixed;right:14px;top:12px;z-index:60;font-family:var(--fm);font-size:11px;color:var(--inkDim);letter-spacing:.1em;display:flex;gap:14px}
.navbtn{position:fixed;bottom:calc(16px + env(safe-area-inset-bottom));z-index:55;width:54px;height:54px;border-radius:50%;
  background:rgba(18,24,41,.86);border:1px solid var(--grid);color:var(--ink);font-size:26px;display:none;align-items:center;justify-content:center}
#prevBtn{left:calc(14px + env(safe-area-inset-left))}#nextBtn{right:calc(14px + env(safe-area-inset-right))}
#rotateHint{position:fixed;inset:0;z-index:70;display:none;flex-direction:column;align-items:center;justify-content:center;
  text-align:center;gap:14px;padding:40px;background:rgba(5,7,13,.95);color:var(--ink)}
#rotateHint .ic{font-size:56px;color:var(--accent)}
#rotateHint .t{font-family:var(--fh);font-weight:600;font-size:23px}
#rotateHint .s{color:var(--inkDim);font-size:15px;max-width:32ch;line-height:1.55}
#rotateHint button{margin-top:8px;font-family:var(--fm);font-size:13px;color:var(--accent);background:none;border:1px solid var(--accent);border-radius:999px;padding:9px 20px}
@media (pointer:coarse),(max-width:820px){.navbtn{display:flex}#hud .help{display:none}}
@media print{html,body{overflow:visible;height:auto}#stageWrap{position:static}#stage{transform:none!important;box-shadow:none;width:1280px;height:720px}
  .slide{display:flex!important;opacity:1!important;position:relative;page-break-after:always}#progress,#hud,#notes,.navbtn,#rotateHint{display:none!important}}
</style>
</head>
<body>
<div id="progress"></div>
<div id="hud"><span id="counter">1 / 9</span><span class="help">left/right nav · N notes · F full · P print</span></div>
<div id="stageWrap"><div id="stage"></div></div>
<div id="notes"><div class="nh">Presenter notes</div><div class="nb" id="notesBody"></div></div>
<div id="rotateHint"><div class="ic">↻</div><div class="t">Rotate to landscape</div><div class="s">This deck is designed for a wide screen. Swipe or use the arrows after rotating.</div><button id="rhDismiss">View anyway</button></div>
<script>
const PAL=__PALETTE__,SLIDES=__SLIDES__,CHARTS=__CHARTS__,FIGURES=__FIGURES__;
const stage=document.getElementById('stage');
if(new URLSearchParams(location.search).has('clean'))document.getElementById('hud').style.display='none';
const esc=s=>(s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const emph=t=>esc(t).replace(/\*(.+?)\*/g,'<span class="em">$1</span>');
function box(x,y,w,h,label,sub,hue='accent'){
  return `<g><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="10" fill="var(--bgAlt)" stroke="var(--${hue})" stroke-width="1.5"/>
  <text x="${x+w/2}" y="${y+h/2-5}" text-anchor="middle" fill="var(--ink)" font-family="var(--fh)" font-weight="600" font-size="16">${esc(label)}</text>
  <text x="${x+w/2}" y="${y+h/2+17}" text-anchor="middle" fill="var(--inkDim)" font-family="var(--fm)" font-size="11.5">${esc(sub)}</text></g>`;
}
function arrow(x1,y1,x2,y2,hue='grid'){
  return `<path d="M ${x1} ${y1} L ${x2} ${y2}" stroke="var(--${hue})" stroke-width="2" fill="none" marker-end="url(#arr)"/>`;
}
function frame(body,W=960,H=360){return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet"><defs><marker id="arr" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0,0 L7,4 L0,8 Z" fill="var(--inkDim)"/></marker></defs>${body}</svg>`;}
function gameLoop(){
  let g=box(130,10,300,66,'REAL THREAD','pulled live from X','accent');
  g+=box(130,104,300,66,'4 REAL + 1 GROK','imposter hidden by seed','pigrpo');
  g+=box(130,198,300,66,'30-SECOND RACE','two players, first tap wins','accent');
  g+=box(130,292,300,66,'REVEAL','real authors + decoy rationale','good');
  g+=arrow(280,76,280,104)+arrow(280,170,280,198)+arrow(280,264,280,292);
  return frame(g,560,368);
}
function pipeline(){
  const xs=[18,205,392,579,766],labels=[
    ['1 · X_SEARCH','find a live thread'],['2 · REPLIES','4 real, read verbatim'],['3 · IMPOSTER','Grok writes one'],
    ['4 · GATES','5 checks, fail closed'],['5 · QUEUE','only clean rounds serve']];
  let g='';
  xs.forEach((x,i)=>{g+=box(x,105,176,92,labels[i][0],labels[i][1],i===3?'pigrpo':(i===4?'good':'accent'));if(i<4)g+=arrow(x+176,151,xs[i+1],151);});
  g+=`<text x="480" y="255" text-anchor="middle" fill="var(--inkDim)" font-family="var(--fm)" font-size="13">Gates rejected 1 of 6 rounds built today (G_SOURCE + G_URL), so 5 serve</text>`;
  g+=`<text x="480" y="282" text-anchor="middle" fill="var(--inkDim)" font-family="var(--fm)" font-size="13">Proven offline: full two-player round, zero network egress (artifacts/integration_trace.txt)</text>`;
  return frame(g);
}
function flywheel(){
  let g=box(155,10,250,62,'PLAYERS GUESS','30-second rounds','accent');
  g+=box(305,150,245,62,'LABELED SIGNAL','human judgments, aggregated','pcrf');
  g+=box(155,290,250,62,'BOT DETECTION','the consumer of the signal','good');
  g+=box(10,150,245,62,'SHARPER DECOYS','harder rounds, more fun','pigrpo');
  g+=arrow(355,72,420,150)+arrow(420,212,355,290)+arrow(205,290,140,212)+arrow(140,150,205,72);
  return frame(g,560,368);
}
function bars(c){
  const W=560,H=330,left=120,top=38,max=Math.max(...c.bars.map(b=>b.value)),avail=355;
  let g='';
  c.bars.forEach((b,i)=>{const y=top+i*84,w=Math.max(8,avail*b.value/max);g+=`<g>
    <text x="${left-16}" y="${y+26}" text-anchor="end" fill="var(--inkDim)" font-family="var(--fh)" font-size="16">${esc(b.label)}</text>
    <rect x="${left}" y="${y}" width="${w}" height="34" rx="6" fill="var(--${b.hue})"/>
    <text x="${left+w+12}" y="${y+24}" fill="var(--ink)" font-family="var(--fm)" font-size="15" font-weight="600">${esc(b.detail)}</text></g>`;});
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${g}</svg><div class="cap">${esc(c.caption)}</div>`;
}
function table(c){let h='<table class="tbl"><thead><tr>'+c.head.map(x=>`<th>${esc(x)}</th>`).join('')+'</tr></thead><tbody>';
  c.rows.forEach(r=>{h+='<tr>'+r.map(x=>`<td>${esc(x)}</td>`).join('')+'</tr>';});return h+'</tbody></table>';}
function tweetCard(t,hero){
  const nm=esc(t.name||(t.handle||'').replace('@',''));
  return `<div class="tw${hero?' hero':''}">
    <div class="twhead"><div class="av">${esc((t.handle||'x').replace('@','').charAt(0).toUpperCase())}</div>
      <div class="who"><span class="nm">${nm} <span class="vf">✓</span></span><span class="hn">${esc(t.handle||'')}</span></div>
      <span class="xmark">𝕏</span></div>
    <div class="twtext">${esc(t.text||'')}</div>
    <div class="twfoot"><span>${esc(t.date||'')}</span><span>${esc(t.url||'')}</span></div></div>`;
}
function tweetHero(c){return `<div class="tweetHero">${tweetCard(c.tweet||{},true)}</div>`;}
function tweetRow(c){return `<div class="tweetRow">${(c.tweets||[]).map(t=>tweetCard(t,false)).join('')}</div>`;}
function renderChart(ref){const c=CHARTS[ref];if(!c)return'';let content='';
  if(c.kind==='table')content=table(c);else if(c.kind==='bars')content=bars(c);else if(c.kind==='loop')content=gameLoop();
  else if(c.kind==='pipeline')content=pipeline();else if(c.kind==='flywheel')content=flywheel();
  else if(c.kind==='tweet_hero')return tweetHero(c);else if(c.kind==='tweet_row')return tweetRow(c);
  return `<div class="chartWrap">${content}</div>`;}
function blockBody(s){return s.body?.length?`<ul class="body">${s.body.map(b=>`<li>${esc(b)}</li>`).join('')}</ul>`:'';}
function blockHighlights(s){return s.highlights?.length?`<div class="hl">${s.highlights.map(h=>`<div class="stat"><span class="v">${esc(h.value)}</span><span class="l">${esc(h.label)}</span></div>`).join('')}</div>`:'';}
function blockFigure(s){if(!s.figureRef)return'';const f=FIGURES[s.figureRef];return `<div class="figWrap"><img src="${f.src}" alt="X Arcade share card"/><div class="cap">${esc(f.cap)}</div></div>`;}
function buildSlide(s,i,total){const el=document.createElement('section');el.className='slide layout-'+s.layout;el.style.setProperty('--sys','var(--accent)');
  const kick=`<div class="kicker"><span class="dot"></span>${esc(s.kicker||s.section)}</div>`;
  const title=`<h1 class="title">${emph(s.title)}</h1><div class="titleRule"></div>`;let mid='';
  if(s.layout==='title')mid=`<div class="sub">${s.body.map(esc).join(' · ')}</div>`+blockHighlights(s);
  else if(s.layout==='split')mid=`<div class="split"><div class="col-l">${blockBody(s)}</div><div class="col-r">${s.figureRef?blockFigure(s):renderChart(s.chartRef)}</div></div>`+blockHighlights(s);
  else if(s.layout==='chart')mid=renderChart(s.chartRef)+blockHighlights(s);
  else if(s.layout==='figure')mid=blockFigure(s)+blockBody(s);
  else mid=blockBody(s)+blockHighlights(s);
  el.innerHTML=kick+title+mid+`<div class="foot"><span class="sec">${esc(s.section)}</span><span>${String(i+1).padStart(2,'0')} / ${total}</span></div>`;return el;}
let idx=0;const total=SLIDES.length,slideEls=[];SLIDES.forEach((s,i)=>{const e=buildSlide(s,i,total);stage.appendChild(e);slideEls.push(e);});
const notesEl=document.getElementById('notes'),notesBody=document.getElementById('notesBody'),counter=document.getElementById('counter'),progress=document.getElementById('progress');
function show(n){idx=Math.max(0,Math.min(total-1,n));slideEls.forEach((e,i)=>e.classList.toggle('active',i===idx));counter.textContent=(idx+1)+' / '+total;progress.style.width=((idx+1)/total*100)+'%';notesBody.textContent=SLIDES[idx].notes||'';try{location.hash=idx+1}catch(e){}}
function fit(){const sx=innerWidth/1280,sy=innerHeight/720;stage.style.transform='scale('+Math.min(sx,sy)+')';}
addEventListener('resize',fit);document.addEventListener('keydown',e=>{if(['ArrowRight','PageDown',' '].includes(e.key)){show(idx+1);e.preventDefault()}else if(['ArrowLeft','PageUp'].includes(e.key))show(idx-1);else if(e.key==='Home')show(0);else if(e.key==='End')show(total-1);else if(['n','N'].includes(e.key))notesEl.classList.toggle('show');else if(['f','F'].includes(e.key)){if(!document.fullscreenElement)document.documentElement.requestFullscreen();else document.exitFullscreen()}else if(['p','P'].includes(e.key))print();});
let tx=0,ty=0;stage.addEventListener('touchstart',e=>{tx=e.changedTouches[0].clientX;ty=e.changedTouches[0].clientY},{passive:true});stage.addEventListener('touchend',e=>{const t=e.changedTouches[0],dx=t.clientX-tx,dy=t.clientY-ty;if(Math.abs(dx)>45&&Math.abs(dx)>Math.abs(dy)*1.4)show(idx+(dx<0?1:-1))},{passive:true});
['prev','next'].forEach(d=>{const b=document.createElement('div');b.className='navbtn';b.id=d+'Btn';b.textContent=d==='prev'?'‹':'›';b.addEventListener('click',e=>{e.stopPropagation();show(idx+(d==='next'?1:-1))});document.body.appendChild(b)});
let dismissed=false;const hint=document.getElementById('rotateHint');document.getElementById('rhDismiss').addEventListener('click',()=>{dismissed=true;hint.style.display='none'});function orient(){hint.style.display=(innerHeight>innerWidth&&Math.min(innerWidth,innerHeight)<560&&!dismissed)?'flex':'none'}addEventListener('resize',orient);fit();orient();show(parseInt(location.hash.slice(1))-1||0);
</script>
</body>
</html>"""


def build() -> str:
    replacements = {
        "__FONTS_HREF__": FONTS["googleFontsHref"],
        "__bg__": PAL["bg"],
        "__bgAlt__": PAL["bgAlt"],
        "__ink__": PAL["ink"],
        "__inkDim__": PAL["inkDim"],
        "__accent__": PAL["accent"],
        "__accent2__": PAL["accent2"],
        "__pcrf__": PAL["pcrf"],
        "__pigrpo__": PAL["pigrpo"],
        "__good__": PAL["good"],
        "__warn__": PAL["warn"],
        "__grid__": PAL["grid"],
        "__PALETTE__": json.dumps(PAL),
        "__SLIDES__": json.dumps(SLIDES, ensure_ascii=False),
        "__CHARTS__": json.dumps(CHARTS, ensure_ascii=False),
        "__FIGURES__": json.dumps(FIGURES, ensure_ascii=False),
    }
    output = HTML
    for source, target in replacements.items():
        output = output.replace(source, target)
    return output


if __name__ == "__main__":
    deck = build()
    (HERE / "index.html").write_text(deck, encoding="utf-8")
    print(f"wrote index.html ({len(SLIDES)} slides, {len(deck)} bytes)")
