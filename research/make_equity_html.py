"""生成净值曲线对比 HTML (长桥数据复刻回测)。

输出: results/orb_equity.html  —— 自包含, 浏览器直接打开。
对比: 基线 / 顺势+未补缺(新默认) / +早突破 / 原4因子 / 仅顺势。
"""

import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from orb_lab import load, prep, slices, daily
from orb_factors import build

QTY = 80


def stats(df):
    p = df.pnl
    gw, gl = p[p > 0].sum(), p[p < 0].sum()
    eq = p.cumsum()
    return dict(n=len(p), win=round((p > 0).mean() * 100, 1),
                pf=round(gw / abs(gl), 2) if gl else None,
                tot=round(p.sum()), mdd=round((eq.cummax() - eq).max()),
                exp=round(p.mean(), 1))


def main():
    D = prep(load()); dl = daily(D); D["_days"] = slices(D)
    tr = build(D, dl).reset_index(drop=True)   # build 已按交易日顺序
    tr["t"] = pd.to_datetime(tr.date).astype("int64") // 10**6  # ms

    E = tr.entry_min <= 630
    G = tr.gap_atr.abs() >= 0.1
    S = tr.open_dist_atr >= 0.15
    F = tr.gap_fill == 0
    V = tr.or_vol_rel >= 0.9
    ALL = pd.Series(True, index=tr.index)

    cfg = {
        "基线 (v2, 顺其自然)":        (ALL, "#8a94a6", True),
        "顺势发力 (仅免费因子1)":      (S,   "#4cafef", False),
        "顺势+未补缺 (新默认)":        (S & F, "#00d1a0", True),
        "顺势+未补缺+早≤10:30":       (S & F & E, "#f6c344", False),
        "原4因子 (早+跳空+势+OR量)":   (E & G & S & V, "#ef5a6f", False),
    }

    series = []
    table = []
    for name, (mask, color, on) in cfg.items():
        x = tr[mask]
        eq = x.pnl.cumsum()
        pts = [[int(t), round(float(v), 1)] for t, v in zip(x.t, eq)]
        series.append(dict(name=name, color=color, on=on, pts=pts))
        s = stats(x); s["name"] = name; s["color"] = color
        table.append(s)

    tmin = int(tr.t.min()); tmax = int(tr.t.max())
    # 逐年分片 (每个 config 的年度 pnl) 供柱状图
    tr["yr"] = pd.to_datetime(tr.date).dt.year
    years = sorted(tr.yr.unique().tolist())
    yearly = []
    for name, (mask, color, on) in cfg.items():
        vals = [round(float(tr[mask & (tr.yr == y)].pnl.sum())) for y in years]
        yearly.append(dict(name=name, color=color, vals=vals))

    data = dict(series=series, table=table, tmin=tmin, tmax=tmax,
                years=years, yearly=yearly)

    html = HTML_TEMPLATE.replace("/*__DATA__*/", json.dumps(data, ensure_ascii=False))
    out = Path("results/orb_equity.html")
    out.write_text(html, encoding="utf-8")
    print("written", out.resolve())


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ORB30 v2 — 长桥数据复刻回测 净值对比</title>
<style>
  :root{--bg:#0f141b;--panel:#161d27;--line:#232c39;--txt:#dbe3ee;--mut:#8a94a6;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:14px/1.5 -apple-system,"PingFang SC","Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
  .wrap{max-width:1080px;margin:0 auto;padding:28px 20px 60px;}
  h1{font-size:20px;margin:0 0 4px;} .sub{color:var(--mut);font-size:13px;margin-bottom:22px;}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;
         padding:16px 16px 8px;margin-bottom:18px;}
  .panel h2{font-size:14px;margin:0 0 12px;font-weight:600;color:#cfd8e5;}
  canvas{width:100%;display:block;}
  .legend{display:flex;flex-wrap:wrap;gap:10px 18px;margin:6px 0 14px;}
  .legend label{display:flex;align-items:center;gap:7px;cursor:pointer;color:#c3ccda;
                font-size:13px;user-select:none;}
  .legend input{accent-color:#3d8bff;}
  .dot{width:11px;height:11px;border-radius:3px;display:inline-block;}
  table{width:100%;border-collapse:collapse;font-size:13px;}
  th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap;}
  th:first-child,td:first-child{text-align:left;}
  thead th{color:var(--mut);font-weight:500;}
  tbody tr:hover{background:#1b2430;}
  .pill{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:7px;vertical-align:middle;}
  .best{color:#00d1a0;font-weight:600;}
  .mut{color:var(--mut);} .foot{color:var(--mut);font-size:12px;margin-top:8px;}
</style>
</head>
<body>
<div class="wrap">
  <h1>ORB30 v2 · 长桥数据复刻回测 · 净值对比</h1>
  <div class="sub">SPY 5min · 2020-07 ~ 2026-09 · 80股固定 · 锚 OR 边界 SL/TP · 强平 12:00 ET
    &nbsp;|&nbsp; 曲线为逐笔累计盈亏（美元），非复利。</div>

  <div class="panel">
    <h2>累计净值曲线（点击图例可开关）</h2>
    <div id="legend" class="legend"></div>
    <canvas id="eq" height="380"></canvas>
  </div>

  <div class="panel">
    <h2>逐年盈亏（美元）</h2>
    <canvas id="yr" height="220"></canvas>
    <div id="yrlegend" class="legend"></div>
  </div>

  <div class="panel">
    <h2>指标</h2>
    <table>
      <thead><tr><th>方案</th><th>笔数</th><th>胜率</th><th>PF</th>
        <th>期望/笔</th><th>总盈亏</th><th>最大回撤</th></tr></thead>
      <tbody id="tbody"></tbody>
    </table>
    <div class="foot">数据源：长桥 SPY 2min → 5min；与 TV(BATS) 可能有细微差异，成交量类因子对数据源较敏感。</div>
  </div>
</div>

<script>
const DATA = /*__DATA__*/;

/* ---------- 累计净值 ---------- */
const eq = document.getElementById('eq');
const ectx = eq.getContext('2d');
let visible = DATA.series.map(s => s.on);

function fmtDate(ms){
  const d = new Date(ms);
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0');
}

function drawEquity(){
  const dpr = window.devicePixelRatio || 1;
  const W = eq.clientWidth, H = 380;
  eq.width = W*dpr; eq.height = H*dpr; eq.style.height = H+'px';
  ectx.setTransform(dpr,0,0,dpr,0,0);
  ectx.clearRect(0,0,W,H);
  const pad = {l:58,r:14,t:14,b:26};
  const iw = W-pad.l-pad.r, ih = H-pad.t-pad.b;

  const vis = DATA.series.filter((s,i)=>visible[i]);
  let ymin=0, ymax=0;
  const xmin = DATA.tmin, xmax = DATA.tmax;
  vis.forEach(s=>s.pts.forEach(p=>{ymin=Math.min(ymin,p[1]); ymax=Math.max(ymax,p[1]);}));
  const padY = (ymax-ymin)*0.06 || 1; ymin-=padY; ymax+=padY;
  const X = t => pad.l + (t-xmin)/(xmax-xmin)*iw;
  const Y = v => pad.t + (ymax-v)/(ymax-ymin)*ih;

  // grid
  ectx.strokeStyle='#232c39'; ectx.fillStyle='#8a94a6'; ectx.lineWidth=1;
  ectx.font='11px -apple-system,sans-serif'; ectx.textAlign='right'; ectx.textBaseline='middle';
  for(let k=0;k<=5;k++){
    const v = ymin + (ymax-ymin)*k/5, y=Y(v);
    ectx.beginPath(); ectx.moveTo(pad.l,y); ectx.lineTo(W-pad.r,y); ectx.stroke();
    ectx.fillText('$'+Math.round(v), pad.l-8, y);
  }
  // zero line
  ectx.strokeStyle='#3a4557'; ectx.beginPath();
  ectx.moveTo(pad.l,Y(0)); ectx.lineTo(W-pad.r,Y(0)); ectx.stroke();
  // x ticks
  ectx.textAlign='center'; ectx.textBaseline='top';
  const y0 = new Date(xmin).getFullYear(), y1 = new Date(xmax).getFullYear();
  for(let y=y0;y<=y1;y++){
    const t = new Date(y,0,1).getTime();
    if(t<xmin||t>xmax) continue;
    ectx.fillStyle='#8a94a6'; ectx.fillText(y, X(t), H-pad.b+6);
  }

  vis.forEach(s=>{
    ectx.strokeStyle=s.color; ectx.lineWidth=2; ectx.beginPath();
    s.pts.forEach((p,i)=>{ const x=X(p[0]), y=Y(p[1]); i?ectx.lineTo(x,y):ectx.moveTo(x,y); });
    ectx.stroke();
  });
  // final labels
  vis.forEach(s=>{
    const last=s.pts[s.pts.length-1];
    ectx.fillStyle=s.color; ectx.textAlign='left'; ectx.textBaseline='middle';
    ectx.font='11px -apple-system,sans-serif';
    ectx.fillText('$'+Math.round(last[1]), X(last[0])+4, Y(last[1]));
  });
}

/* ---------- 逐年柱状 ---------- */
const yc = document.getElementById('yr');
const yctx = yc.getContext('2d');
let yvisible = DATA.yearly.map(d=>d.name.includes('基线')||d.name.includes('新默认'));
function drawYearly(){
  const dpr = window.devicePixelRatio||1;
  const W=yc.clientWidth, H=220; yc.width=W*dpr; yc.height=H*dpr; yc.style.height=H+'px';
  yctx.setTransform(dpr,0,0,dpr,0,0); yctx.clearRect(0,0,W,H);
  const pad={l:58,r:14,t:12,b:24}; const iw=W-pad.l-pad.r, ih=H-pad.t-pad.b;
  const rows = DATA.yearly.filter((d,i)=>yvisible[i]);
  let vmin=0,vmax=0;
  DATA.yearly.forEach(d=>d.vals.forEach(v=>{vmin=Math.min(vmin,v);vmax=Math.max(vmax,v);}));
  const padV=(vmax-vmin)*0.08||1; vmin-=padV; vmax+=padV;
  const nY=DATA.years.length; const gw=iw/nY;
  const bw=gw*0.8/Math.max(rows.length,1);
  const Y=v=>pad.t+(vmax-v)/(vmax-vmin)*ih;
  yctx.font='11px -apple-system,sans-serif'; yctx.fillStyle='#8a94a6';
  yctx.textAlign='right'; yctx.textBaseline='middle';
  for(let k=0;k<=4;k++){ const v=vmin+(vmax-vmin)*k/4,y=Y(v);
    yctx.strokeStyle='#232c39'; yctx.beginPath(); yctx.moveTo(pad.l,y); yctx.lineTo(W-pad.r,y); yctx.stroke();
    yctx.fillText('$'+Math.round(v),pad.l-8,y); }
  yctx.strokeStyle='#3a4557'; yctx.beginPath(); yctx.moveTo(pad.l,Y(0)); yctx.lineTo(W-pad.r,Y(0)); yctx.stroke();
  yctx.textAlign='center'; yctx.textBaseline='top';
  DATA.years.forEach((y,iy)=>{
    yctx.fillStyle='#8a94a6'; yctx.fillText(y, pad.l+gw*(iy+0.5), H-pad.b+5);
    rows.forEach((d,ri)=>{
      const v=d.vals[iy]; const x=pad.l+gw*iy+gw*0.1+ri*bw;
      yctx.fillStyle=d.color;
      const yy=Y(v), h=Y(0)-Y(v);
      yctx.fillRect(x, v>=0?yy:Y(0), bw-1.5, Math.abs(h)||1);
    });
  });
}

/* ---------- 图例/表格 ---------- */
function buildLegend(){
  const lg=document.getElementById('legend'); lg.innerHTML='';
  DATA.series.forEach((s,i)=>{
    const l=document.createElement('label');
    l.innerHTML=`<input type="checkbox" ${visible[i]?'checked':''}>
      <span class="dot" style="background:${s.color}"></span>${s.name}`;
    l.querySelector('input').onchange=e=>{visible[i]=e.target.checked; drawEquity();};
    lg.appendChild(l);
  });
  const yl=document.getElementById('yrlegend'); yl.innerHTML='';
  DATA.yearly.forEach((s,i)=>{
    const l=document.createElement('label');
    l.innerHTML=`<input type="checkbox" ${yvisible[i]?'checked':''}>
      <span class="dot" style="background:${s.color}"></span>${s.name}`;
    l.querySelector('input').onchange=e=>{yvisible[i]=e.target.checked; drawYearly();};
    yl.appendChild(l);
  });
  const tb=document.getElementById('tbody'); tb.innerHTML='';
  const bestTot=Math.max(...DATA.table.map(t=>t.tot));
  DATA.table.forEach(t=>{
    const tr=document.createElement('tr');
    tr.innerHTML=`<td><span class="pill" style="background:${t.color}"></span>${t.name}</td>
      <td>${t.n}</td><td>${t.win}%</td>
      <td class="${t.pf===2.11||t.pf===1.96?'best':''}">${t.pf}</td>
      <td>$${t.exp}</td>
      <td class="${t.tot===bestTot?'best':''}">$${t.tot.toLocaleString()}</td>
      <td>$${t.mdd.toLocaleString()}</td>`;
    tb.appendChild(tr);
  });
}

window.addEventListener('resize', ()=>{drawEquity(); drawYearly();});
buildLegend(); drawEquity(); drawYearly();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
