#!/usr/bin/env python3
"""Local dashboard for the Binance bot.  Run:  python gui.py   ->  http://127.0.0.1:8000
Binds to localhost only. API keys stay in .env and are never sent to the browser."""
import json
import logging
import os
import re
import secrets
import threading
import webbrowser

from flask import Flask, Response, jsonify, request

import bot as B
from bot import CFG

SETTINGS_FILE = "settings.json"
TOKEN = secrets.token_hex(16)
app = Flask(__name__)
state = {"bot": None, "thread": None}


# ---------------------------------------------------------------- settings
def current_settings():
    return {
        "mode": CFG.mode, "symbols": ",".join(CFG.symbols), "timeframe": CFG.timeframe,
        "capital": CFG.capital, "risk_pct": round(CFG.risk_per_trade * 100, 3),
        "max_positions": CFG.max_positions, "max_trades": CFG.max_trades_per_day,
        "daily_loss_pct": round(CFG.daily_loss_limit * 100, 3), "rsi_entry": CFG.rsi_entry,
        "atr_stop": CFG.atr_stop, "atr_tp": CFG.atr_tp,
    }


def apply_settings(d, save=True):
    def num(key, lo, hi, cast=float):
        v = cast(d[key])
        if not lo <= v <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        return v

    mode = d["mode"]
    if mode not in ("paper", "testnet", "live"):
        raise ValueError("Invalid mode")
    syms = [x.strip().upper() for x in str(d["symbols"]).split(",") if x.strip()]
    if not syms or not all(re.fullmatch(r"[A-Z0-9]{2,10}/[A-Z]{3,5}", x) for x in syms):
        raise ValueError("Symbols must look like BTC/USDT, comma separated")
    if d["timeframe"] not in B.TF_SECONDS:
        raise ValueError("Unsupported timeframe")
    vals = dict(
        capital=num("capital", 20, 10_000_000), risk_per_trade=num("risk_pct", 0.05, 5) / 100,
        max_positions=num("max_positions", 1, 10, int), max_trades_per_day=num("max_trades", 1, 200, int),
        daily_loss_limit=num("daily_loss_pct", 0.5, 20) / 100, rsi_entry=num("rsi_entry", 20, 70),
        atr_stop=num("atr_stop", 0.5, 5), atr_tp=num("atr_tp", 0.5, 10))
    if vals["atr_tp"] < vals["atr_stop"]:
        raise ValueError("Take-profit multiple should be at least the stop multiple")
    CFG.mode, CFG.symbols, CFG.timeframe = mode, tuple(syms), d["timeframe"]
    for k, v in vals.items():
        setattr(CFG, k, v)
    if save:
        json.dump(current_settings(), open(SETTINGS_FILE, "w"), indent=2)


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            apply_settings(json.load(open(SETTINGS_FILE)), save=False)
        except Exception as e:
            logging.warning("Ignoring bad settings.json: %s", e)


# ---------------------------------------------------------------- helpers
def running():
    t = state["thread"]
    return bool(t and t.is_alive())


def offline_snapshot():
    f = f"state_{CFG.mode}.json"
    s = json.load(open(f)) if os.path.exists(f) else {
        "realized": 0, "day_pnl": 0, "day_trades": 0, "positions": {}, "history": []}
    h = s["history"]
    pos = [{**p, "sym": k, "price": p["entry"], "unrealized": 0, "pct": 0} for k, p in s["positions"].items()]
    return {"mode": CFG.mode, "equity": CFG.capital + s["realized"], "realized": s["realized"],
            "unrealized": 0, "day_pnl": s["day_pnl"], "day_trades": s["day_trades"],
            "deployed": sum(p["cost"] for p in s["positions"].values()), "capital": CFG.capital,
            "positions": pos, "prices": {}, "history": h[-200:], "wins": sum(x["pnl"] > 0 for x in h),
            "total_trades": len(h), "halted": False, "last_tick": None, "last_error": ""}


@app.before_request
def guard():
    # localhost only (blocks DNS-rebinding) + secret token on every API call (blocks CSRF from other sites)
    if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
        return jsonify(error="forbidden"), 403
    if request.path.startswith("/api/") and request.headers.get("X-Token") != TOKEN:
        return jsonify(error="forbidden"), 403


# -------------------------------------------------------------------- API
@app.get("/api/status")
def status():
    run = running()
    snap = state["bot"].snapshot() if run else offline_snapshot()
    return jsonify(running=run, settings=current_settings(), snapshot=snap,
                   keys_loaded=bool(CFG.api_key and CFG.api_secret), logs=list(B.LOG_BUFFER)[-80:])


@app.post("/api/settings")
def save_settings():
    if running():
        return jsonify(error="Stop the bot before changing settings"), 400
    try:
        apply_settings(request.get_json())
    except (ValueError, KeyError, TypeError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.post("/api/start")
def start():
    if running():
        return jsonify(error="Already running"), 400
    try:
        b = B.Bot(confirm_live=(request.get_json(silent=True) or {}).get("confirm") == "LIVE")
    except Exception as e:
        return jsonify(error=str(e)), 400
    t = threading.Thread(target=b.run, daemon=True)
    state.update(bot=b, thread=t)
    t.start()
    return jsonify(ok=True)


@app.post("/api/stop")
def stop():
    b = state["bot"]
    if not running():
        return jsonify(ok=True)
    try:
        if (request.get_json(silent=True) or {}).get("flatten"):
            with b.lock:
                b.flatten()
        b.stop_event.set()
        state["thread"].join(timeout=20)
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(ok=True)


@app.post("/api/close")
def close_position():
    if not running():
        return jsonify(error="Bot is not running"), 400
    try:
        state["bot"].close_one(request.get_json()["symbol"])
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(ok=True)


@app.post("/api/backtest")
def backtest():
    d = request.get_json()
    sym = str(d.get("symbol", "")).upper()
    if not re.fullmatch(r"[A-Z0-9]{2,10}/[A-Z]{3,5}", sym):
        return jsonify(error="Bad symbol"), 400
    try:
        return jsonify(B.run_backtest(sym, max(7, min(int(d.get("days", 60)), 365))))
    except Exception as e:
        return jsonify(error=f"Backtest failed: {e}"), 500


@app.get("/")
def index():
    return Response(PAGE.replace("__TOKEN__", TOKEN), mimetype="text/html")


# ------------------------------------------------------------------- page
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Binance Bot</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#14171a;--muted:#66707a;--line:#e3e6ea;--accent:#2563eb;--pos:#0a8a4a;--neg:#d12c3a;--warn:#b7791f}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#181c22;--text:#e8ebef;--muted:#8b95a1;--line:#2a3038;--accent:#5b8dff;--pos:#33c47a;--neg:#ff6370;--warn:#e0a84a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:14px 20px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
h1{font-size:17px;margin:0;flex:1}
.badge{padding:3px 10px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid var(--line)}
.badge.live{background:var(--neg);color:#fff;border-color:var(--neg)}.badge.paper{color:var(--accent)}.badge.testnet{color:var(--warn)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted);display:inline-block;margin-right:6px}.dot.on{background:var(--pos)}
main{max-width:1150px;margin:0 auto;padding:18px;display:grid;gap:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.kpi .l{color:var(--muted);font-size:12px}.kpi .v{font-size:21px;font-weight:650;margin-top:2px;font-variant-numeric:tabular-nums}
.pos{color:var(--pos)}.neg{color:var(--neg)}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 10px}
.cols{display:grid;grid-template-columns:2fr 1fr;gap:16px}@media(max-width:850px){.cols{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:500;font-size:12px}
.scroll{overflow-x:auto;max-height:300px;overflow-y:auto}
button{font:inherit;padding:7px 14px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.danger{background:var(--neg);border-color:var(--neg);color:#fff}
button:disabled{opacity:.45;cursor:not-allowed}button.sm{padding:2px 9px;font-size:12px}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
label{display:grid;gap:3px;font-size:12px;color:var(--muted)}
input,select{font:inherit;padding:7px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--text);width:100%}
pre{margin:0;font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;max-height:230px;overflow:auto;white-space:pre-wrap;color:var(--muted)}
.note{color:var(--muted);font-size:12px}.alert{color:var(--neg);font-size:13px;margin-top:6px}
#toast{position:fixed;bottom:18px;right:18px;background:var(--text);color:var(--bg);padding:10px 14px;border-radius:8px;display:none;max-width:340px;z-index:9}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:end}
</style></head><body>
<header>
  <h1>Binance Spot Bot</h1>
  <span id="modeBadge" class="badge">paper</span>
  <span><span id="dot" class="dot"></span><span id="state">Stopped</span></span>
  <button id="startBtn" class="primary">Start</button>
  <button id="stopBtn">Stop</button>
  <button id="flatBtn" class="danger">Stop &amp; close all</button>
</header>
<main>
  <div id="warn" class="alert"></div>
  <div class="kpis" id="kpis"></div>
  <div class="cols">
    <div class="card"><h2>Equity curve (closed trades)</h2><svg id="chart" viewBox="0 0 600 170" width="100%" height="170" preserveAspectRatio="none"></svg><div id="chartNote" class="note"></div></div>
    <div class="card"><h2>Market prices</h2><div id="prices" class="note">Start the bot to see live prices.</div></div>
  </div>
  <div class="card"><h2>Open positions</h2><div class="scroll"><table id="posTbl"></table></div></div>
  <div class="card"><h2>Trade history</h2><div class="scroll"><table id="histTbl"></table></div></div>
  <div class="card"><h2>Settings <span class="note">(editable while stopped)</span></h2>
    <div class="form">
      <label>Mode<select id="f_mode"><option value="paper">Paper (simulated)</option><option value="testnet">Testnet (fake money)</option><option value="live">Live (REAL money)</option></select></label>
      <label>Symbols<input id="f_symbols"></label>
      <label>Timeframe<select id="f_timeframe"><option>1m</option><option>5m</option><option>15m</option><option>30m</option><option>1h</option><option>4h</option></select></label>
      <label>Capital cap (USDT)<input id="f_capital" type="number" step="any"></label>
      <label>Risk per trade (%)<input id="f_risk_pct" type="number" step="any"></label>
      <label>Max open positions<input id="f_max_positions" type="number"></label>
      <label>Max trades / day<input id="f_max_trades" type="number"></label>
      <label>Daily loss limit (%)<input id="f_daily_loss_pct" type="number" step="any"></label>
      <label>RSI entry below<input id="f_rsi_entry" type="number" step="any"></label>
      <label>Stop (x ATR)<input id="f_atr_stop" type="number" step="any"></label>
      <label>Take profit (x ATR)<input id="f_atr_tp" type="number" step="any"></label>
    </div>
    <div class="row" style="margin-top:12px"><button id="saveBtn" class="primary">Save settings</button><span id="keys" class="note"></span></div>
  </div>
  <div class="card"><h2>Backtest (real history, fees + slippage included)</h2>
    <div class="row"><label>Symbol<input id="b_sym" value="BTC/USDT"></label><label>Days<input id="b_days" type="number" value="60"></label><button id="btBtn">Run backtest</button></div>
    <div id="btOut" class="note" style="margin-top:10px"></div>
  </div>
  <div class="card"><h2>Log</h2><pre id="log"></pre></div>
  <p class="note">Trading involves risk of loss and past results do not predict future results. This tool is not financial advice.</p>
</main>
<div id="toast"></div>
<script>
const TOKEN="__TOKEN__", $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=(n,d=2)=>(n<0?'-':'')+Math.abs(n).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const sgn=n=>n>0?'pos':n<0?'neg':'';
function toast(m){const t=$('toast');t.textContent=m;t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',4500);}
async function api(p,body){const r=await fetch(p,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:body?JSON.stringify(body):undefined});
  const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j;}
const F=['mode','symbols','timeframe','capital','risk_pct','max_positions','max_trades','daily_loss_pct','rsi_entry','atr_stop','atr_tp'];
let formLoaded=false,isRunning=false;

function fillForm(s){F.forEach(k=>$('f_'+k).value=s[k]);formLoaded=true;}
function kpi(l,v,c=''){return `<div class="card kpi"><div class="l">${l}</div><div class="v ${c}">${v}</div></div>`;}

function chart(h,cap){
  const svg=$('chart');
  if(h.length<1){svg.innerHTML='';$('chartNote').textContent='No closed trades yet.';return;}
  let v=cap,pts=[cap];h.forEach(t=>{v+=t.pnl;pts.push(v);});
  const mn=Math.min(...pts),mx=Math.max(...pts),rg=(mx-mn)||1;
  const xy=pts.map((p,i)=>`${(i/(pts.length-1||1))*600},${160-((p-mn)/rg)*150}`).join(' ');
  const base=160-((cap-mn)/rg)*150, up=pts[pts.length-1]>=cap;
  svg.innerHTML=`<line x1="0" x2="600" y1="${base}" y2="${base}" stroke="currentColor" stroke-opacity=".2" stroke-dasharray="4"/>
   <polyline fill="none" stroke="${up?'#22a861':'#e5484d'}" stroke-width="2" vector-effect="non-scaling-stroke" points="${xy}"/>`;
  $('chartNote').textContent=`${h.length} trades · ${money(mn)} to ${money(mx)} USDT`;
}

function render(d){
  const s=d.snapshot,m=d.settings.mode;isRunning=d.running;
  const b=$('modeBadge');b.textContent=(d.running?s.mode:m).toUpperCase();b.className='badge '+(d.running?s.mode:m);
  $('dot').className='dot'+(d.running?' on':'');$('state').textContent=d.running?(s.halted?'Running (daily loss limit hit, no new entries)':'Running'):'Stopped';
  $('startBtn').disabled=d.running;$('stopBtn').disabled=!d.running;$('flatBtn').disabled=!d.running;
  F.forEach(k=>$('f_'+k).disabled=d.running);$('saveBtn').disabled=d.running;
  if(!formLoaded)fillForm(d.settings);
  $('keys').textContent=d.keys_loaded?'API keys found in .env':'No API keys in .env (paper mode only)';
  $('warn').textContent=s.last_error||'';
  const wr=s.total_trades?Math.round(s.wins/s.total_trades*100)+'%':'–';
  $('kpis').innerHTML=kpi('Equity (USDT)',money(s.equity))+kpi('Realized PnL',money(s.realized),sgn(s.realized))
    +kpi('Unrealized PnL',money(s.unrealized),sgn(s.unrealized))+kpi('Today PnL',money(s.day_pnl),sgn(s.day_pnl))
    +kpi('Win rate',wr+` <span class="note">(${s.total_trades})</span>`)+kpi('Deployed',`${money(s.deployed,0)} / ${money(s.capital,0)}`)
    +kpi('Trades today',s.day_trades);
  chart(s.history,s.capital);
  const pr=Object.entries(s.prices);
  $('prices').innerHTML=pr.length?`<table>${pr.map(([k,v])=>`<tr><td>${esc(k)}</td><td>${money(v,v<10?4:2)}</td></tr>`).join('')}</table>`:'Start the bot to see live prices.';
  $('posTbl').innerHTML=s.positions.length?`<tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Price</th><th>Stop</th><th>Target</th><th>PnL</th><th></th></tr>`
    +s.positions.map(p=>`<tr><td>${esc(p.sym)}</td><td>${p.qty}</td><td>${money(p.entry,4)}</td><td>${money(p.price,4)}</td><td>${money(p.stop,4)}</td><td>${money(p.tp,4)}</td>
     <td class="${sgn(p.unrealized)}">${money(p.unrealized)} (${p.pct.toFixed(2)}%)</td><td><button class="sm" ${d.running?'':'disabled'} onclick="closePos('${esc(p.sym)}')">Close</button></td></tr>`).join('')
    :'<tr><td class="note">No open positions</td></tr>';
  const h=[...s.history].reverse().slice(0,60);
  $('histTbl').innerHTML=h.length?`<tr><th>Time (UTC)</th><th>Symbol</th><th>Exit</th><th>PnL</th></tr>`
    +h.map(t=>`<tr><td>${esc(t.t.slice(0,19).replace('T',' '))}</td><td>${esc(t.sym)}</td><td>${esc(t.why)}</td><td class="${sgn(t.pnl)}">${money(t.pnl,3)}</td></tr>`).join('')
    :'<tr><td class="note">No trades yet</td></tr>';
  const lg=$('log'),atBottom=lg.scrollTop+lg.clientHeight>=lg.scrollHeight-20;
  lg.textContent=d.logs.join('\n');if(atBottom)lg.scrollTop=lg.scrollHeight;
}
async function refresh(){try{render(await api('/api/status'));}catch(e){$('state').textContent='Server unreachable';}}

$('startBtn').onclick=async()=>{
  let body={};
  if($('f_mode').value==='live'){const t=prompt('LIVE mode trades REAL money on your Binance account.\nYou can lose money. Type LIVE to confirm.');if(t!=='LIVE')return;body.confirm='LIVE';}
  $('startBtn').disabled=true;toast('Starting…');
  try{await api('/api/start',body);toast('Bot started');}catch(e){toast(e.message);}refresh();};
$('stopBtn').onclick=async()=>{
  if(!confirm('Stop the bot? Open positions stay open and will have NO stop-loss until you restart it.'))return;
  try{await api('/api/stop',{});}catch(e){toast(e.message);}refresh();};
$('flatBtn').onclick=async()=>{
  if(!confirm('Sell all open positions at market and stop the bot?'))return;
  try{await api('/api/stop',{flatten:true});}catch(e){toast(e.message);}refresh();};
window.closePos=async sym=>{if(!confirm('Sell '+sym+' at market?'))return;try{await api('/api/close',{symbol:sym});}catch(e){toast(e.message);}refresh();};
$('saveBtn').onclick=async()=>{
  const b={};F.forEach(k=>b[k]=$('f_'+k).value);
  try{await api('/api/settings',b);toast('Settings saved');refresh();}catch(e){toast(e.message);}};
$('f_mode').onchange=async()=>{ // switching mode loads that mode's own history
  if(isRunning)return;const b={};F.forEach(k=>b[k]=$('f_'+k).value);try{await api('/api/settings',b);refresh();}catch(e){}};
$('btBtn').onclick=async()=>{
  $('btOut').textContent='Downloading history and simulating… (can take up to a minute)';$('btBtn').disabled=true;
  try{const r=await api('/api/backtest',{symbol:$('b_sym').value,days:+$('b_days').value});
    $('btOut').innerHTML=`<b>${esc(r.symbol)}</b> · ${r.timeframe} · ${r.days} days · ${r.trades} trades · win rate ${r.win_rate}%<br>
      ${money(r.start)} → ${money(r.end)} USDT (<span class="${sgn(r.return_pct)}">${r.return_pct>0?'+':''}${r.return_pct}%</span>) after fees and slippage.
      ${r.return_pct<=0?'<br>Not profitable on this history. Do not trade it live as is.':''}`;
  }catch(e){$('btOut').textContent=e.message;}$('btBtn').disabled=false;};
refresh();setInterval(refresh,2000);
</script></body></html>"""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("bot.log"), B.BufferHandler()])
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    load_settings()
    url = "http://127.0.0.1:8000"
    print(f"Dashboard: {url}  (Ctrl+C to quit)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=8000, threaded=True)
