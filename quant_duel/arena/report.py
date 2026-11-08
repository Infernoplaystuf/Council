"""The leaderboard: an HTML page (table + money-over-time chart) and a short
Markdown summary, written to reports/arena/.

    leaderboard-<day>.html / .md   the standings at that close
    latest.html / latest.md        a copy of the newest
"""
from __future__ import annotations

import datetime as dt
import html
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from . import book as bk

# Validated categorical palette (light, dark), slots in fixed order.
SERIES = [("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"),
          ("#1baf7a", "#199e70"), ("#eda100", "#c98500"),
          ("#e87ba4", "#d55181"), ("#008300", "#008300"),
          ("#6250d6", "#9085e9"), ("#e34948", "#e66767")]


def _rows(arena, books, day: dt.date) -> List[Dict[str, Any]]:
    week_ago = day - dt.timedelta(days=7)
    prices = arena.prices()
    rows = []
    for b in books.values():
        view = dict(b, marks={k: v for k, v in b["marks"].items()
                              if k <= str(day)})
        r = bk.summary(view, week_ago)
        value = {t: n * (bk.price_on(prices, t, day, "close") or 0)
                 for t, n in r["holdings"].items()}
        eq = r["equity"] or 1
        r["top"] = [(t, v / eq * 100) for t, v in
                    sorted(value.items(), key=lambda kv: -kv[1])]
        rows.append(r)
    rows.sort(key=lambda r: -r["equity"])
    return rows


def _pct(x) -> str:
    return "–" if x is None else f"{x:+.2f}%"


def markdown(arena, rows, day, final: bool) -> str:
    head = (f"## {'FINAL standings' if final else 'Standings'} at the close "
            f"on {day}\n\nDuel: {arena.start} to {arena.end}, "
            f"${arena.cfg['start_cash']:,} each.\n")
    lines = [head, "| # | Contestant | Model | Money | Total | Week | Trades |",
             "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        lines.append(f"| {i} | {r['name']} | {r['model']} | "
                     f"${r['equity']:,.2f} | {_pct(r['total_pct'])} | "
                     f"{_pct(r['week_pct'])} | {r['trades']} |")
    return "\n".join(lines) + "\n"


def page(arena, books, rows, day, final: bool) -> str:
    names = [c["name"] for c in arena.cfg["contestants"]]
    color = {n: SERIES[i % len(SERIES)] for i, n in enumerate(names)}
    series = []
    for name, b in books.items():
        m = {k: v for k, v in sorted(b["marks"].items()) if k <= str(day)}
        series.append({"name": name, "model": b["model"],
                       "light": color.get(name, ("#7a7a75", "#a3a29a"))[0],
                       "dark": color.get(name, ("#7a7a75", "#a3a29a"))[1],
                       "bench": name not in color,
                       "points": list(m.items())})
    trs, why = [], []
    for i, r in enumerate(rows, 1):
        held = ", ".join(f"{t} {p:.0f}%" for t, p in r["top"][:4]) or "cash"
        trs.append(
            "<tr>" + "".join(f"<td>{c}</td>" for c in [
                i, html.escape(r["name"]), html.escape(r["model"]),
                f"${r['equity']:,.2f}", _pct(r["total_pct"]),
                _pct(r["week_pct"]), f"{r['max_dd_pct']:.1f}%",
                r["trades"], f"${r['costs']:,.2f}", r["days_failed"],
                html.escape(held)]) + "</tr>")
        if r["last_reason"]:
            why.append(f"<li><b>{html.escape(r['name'])}:</b> "
                       f"{html.escape(r['last_reason'][:300])}</li>")
    title = "Final standings" if final else "Model Arena standings"
    return TEMPLATE.replace("%TITLE%", title).replace(
        "%SUB%", f"At the close on {day}. Duel runs {arena.start} to "
        f"{arena.end}; everyone started with ${arena.cfg['start_cash']:,} "
        "of pretend money. Most money at the end wins.").replace(
        "%ROWS%", "\n".join(trs)).replace(
        "%WHY%", "\n".join(why) or "<li>No decisions yet.</li>").replace(
        "%DATA%", json.dumps(series)).replace(
        "%START%", json.dumps(float(arena.cfg["start_cash"])))


def write(arena, books, day: dt.date, final: bool = False) -> Path:
    out = Path(arena.reports)
    out.mkdir(parents=True, exist_ok=True)
    rows = _rows(arena, books, day)
    tag = f"leaderboard-{day}" + ("-final" if final else "")
    p = out / f"{tag}.html"
    p.write_text(page(arena, books, rows, day, final), encoding="utf-8")
    md = out / f"{tag}.md"
    md.write_text(markdown(arena, rows, day, final), encoding="utf-8")
    shutil.copyfile(p, out / "latest.html")
    shutil.copyfile(md, out / "latest.md")
    return p


TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%TITLE%</title><style>
:root{--bg:#fbfaf7;--surface:#ffffff;--ink:#1f1f1d;--ink2:#5c5b55;
--muted:#8a8981;--grid:#e6e4dd;--mode:light}
@media (prefers-color-scheme:dark){:root{--bg:#121211;--surface:#1a1a19;
--ink:#f0efec;--ink2:#c3c2b7;--muted:#8f8e86;--grid:#2c2c2a;--mode:dark}}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 system-ui,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}p.sub{color:var(--ink2);margin:0 0 20px}
.card{background:var(--surface);border:1px solid var(--grid);
border-radius:10px;padding:16px;margin-bottom:20px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid);
white-space:nowrap}th{color:var(--ink2);font-weight:600}
h2{font-size:15px;margin:0 0 8px}ul.why{margin:0;padding-left:18px;
color:var(--ink2)}ul.why li{margin:4px 0}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin:0 0 8px;
color:var(--ink2);font-size:13px}.legend i{display:inline-block;width:14px;
height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
svg text{fill:var(--muted);font-size:11px}#tip{position:fixed;
pointer-events:none;background:var(--surface);border:1px solid var(--grid);
border-radius:8px;padding:8px 10px;font-size:12px;display:none;
box-shadow:0 2px 10px rgba(0,0,0,.12);color:var(--ink)}
</style></head><body><main>
<h1>%TITLE%</h1><p class="sub">%SUB%</p>
<div class="card"><table><thead><tr><th>#</th><th>Contestant</th>
<th>Model</th><th>Money</th><th>Total</th><th>Last 7 days</th>
<th>Worst drop</th><th>Trades</th><th>Costs</th><th>Failed days</th>
<th>Biggest holdings</th></tr></thead><tbody>
%ROWS%</tbody></table></div>
<div class="card"><div class="legend" id="legend"></div>
<svg id="chart" width="100%" height="320" role="img"
aria-label="Money over time for each contestant"></svg></div>
<div class="card"><h2>What each model said last</h2><ul class="why">
%WHY%</ul></div><div id="tip"></div></main><script>
const S=%DATA%, START=%START%;
const dark=getComputedStyle(document.documentElement)
  .getPropertyValue('--mode').trim()==='dark';
const svg=document.getElementById('chart'),tip=document.getElementById('tip');
const lg=document.getElementById('legend');
S.forEach(s=>{const c=dark?s.dark:s.light;lg.insertAdjacentHTML('beforeend',
 `<span><i style="background:${c}${s.bench?';opacity:.8':''}"></i>${s.name}</span>`)});
function draw(){
 const W=svg.clientWidth,H=320,L=64,R=12,T=10,B=28;svg.innerHTML='';
 const days=[...new Set(S.flatMap(s=>s.points.map(p=>p[0])))].sort();
 if(days.length<1){svg.innerHTML='<text x="10" y="30">No closes yet.</text>';return}
 const vals=S.flatMap(s=>s.points.map(p=>p[1])).concat([START]);
 let lo=Math.min(...vals),hi=Math.max(...vals);const pad=(hi-lo)*.08||50;
 lo-=pad;hi+=pad;
 const x=i=>L+(days.length<2?0:(W-L-R)*i/(days.length-1));
 const y=v=>T+(H-T-B)*(1-(v-lo)/(hi-lo));const ns='http://www.w3.org/2000/svg';
 const el=(n,a)=>{const e=document.createElementNS(ns,n);
  for(const k in a)e.setAttribute(k,a[k]);svg.appendChild(e);return e};
 for(let k=0;k<=4;k++){const v=lo+(hi-lo)*k/4;
  el('line',{x1:L,x2:W-R,y1:y(v),y2:y(v),stroke:'var(--grid)'});
  el('text',{x:L-6,y:y(v)+4,'text-anchor':'end'}).textContent=
   '$'+Math.round(v).toLocaleString()}
 el('line',{x1:L,x2:W-R,y1:y(START),y2:y(START),stroke:'var(--muted)',
  'stroke-dasharray':'2 3'});
 const step=Math.max(1,Math.ceil(days.length/8));
 days.forEach((d,i)=>{if(i%step===0)el('text',{x:x(i),y:H-8,
  'text-anchor':'middle'}).textContent=d.slice(5)});
 S.forEach(s=>{if(!s.points.length)return;const c=dark?s.dark:s.light;
  const pts=s.points.map(p=>[x(days.indexOf(p[0])),y(p[1])]);
  el('path',{d:'M'+pts.map(p=>p.join(',')).join('L'),fill:'none',stroke:c,
   'stroke-width':2,'stroke-dasharray':s.bench?'6 4':'',
   'stroke-linejoin':'round'})});
 const cross=el('line',{y1:T,y2:H-B,stroke:'var(--muted)',opacity:0});
 svg.onmousemove=e=>{const r=svg.getBoundingClientRect();
  const i=Math.max(0,Math.min(days.length-1,Math.round((e.clientX-r.left-L)/
   ((W-L-R)/Math.max(1,days.length-1)))));const d=days[i];
  cross.setAttribute('x1',x(i));cross.setAttribute('x2',x(i));
  cross.setAttribute('opacity',1);
  const rows=S.map(s=>[s,(s.points.find(p=>p[0]===d)||[])[1]])
   .filter(r=>r[1]!=null).sort((a,b)=>b[1]-a[1]);
  tip.innerHTML='<b>'+d+'</b><br>'+rows.map(([s,v])=>
   `<i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${dark?s.dark:s.light}"></i> ${s.name}: $${v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}`).join('<br>');
  tip.style.display='block';tip.style.left=Math.min(e.clientX+14,innerWidth-230)+'px';
  tip.style.top=(e.clientY+14)+'px'};
 svg.onmouseleave=()=>{tip.style.display='none';cross.setAttribute('opacity',0)};
}
draw();addEventListener('resize',draw);
</script></body></html>"""
