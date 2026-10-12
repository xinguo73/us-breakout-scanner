#!/usr/bin/env python3
"""US breakout signal dashboard (mirror of crypto dashboard).

Serves the signal ledger as an auto-refreshing web page: weekly tabs, stats,
returns at +4h/+8h/+12h/+24h (via 1H bars), and a simple SVG K-line modal.
Pure stdlib + requests.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

from us_ledger import LEDGER_PATH, load_ledger, save_ledger, week_key, week_label, fmt_cst

PORT = 8788
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
REFRESH_INTERVAL = 60
STRONG_RS = 0.7

_SESSION = requests.Session()
_CACHE = {"html": None, "ts": 0.0}


def fetch_bars(symbol, interval, rng):
    try:
        r = requests.get(f"{YAHOO_CHART}/{symbol}",
                         params={"range": rng, "interval": interval}, headers=UA, timeout=15)
        res = r.json()["chart"]["result"][0]
        ts = res.get("timestamp") or []
        q = (res.get("indicators", {}).get("quote") or [{}])[0]
        out = {}
        for i, t in enumerate(ts):
            c = q["close"][i]
            if c is not None:
                out[int(t)] = float(c)
        return out
    except Exception:
        return {}


def fetch_quote(symbol):
    try:
        r = requests.get(f"{YAHOO_CHART}/{symbol}",
                         params={"range": "1d", "interval": "15m"}, headers=UA, timeout=15)
        res = r.json()["chart"]["result"][0]
        meta = res.get("meta", {})
        return meta.get("regularMarketPrice")
    except Exception:
        return None


_INTERVAL_MAP = {"15m": ("5d", "15m"), "1H": ("1mo", "60m"), "1D": ("1y", "1d")}


def fetch_ohlcv(symbol, bar, limit=200):
    """OHLCV list oldest-first, for the K-line chart (Yahoo chart API)."""
    rng, interval = _INTERVAL_MAP.get(bar, ("1mo", "60m"))
    try:
        r = requests.get(f"{YAHOO_CHART}/{symbol}",
                         params={"range": rng, "interval": interval}, headers=UA, timeout=15)
        res = r.json()["chart"]["result"][0]
        ts = res.get("timestamp") or []
        q = (res.get("indicators", {}).get("quote") or [{}])[0]
        out = []
        for i, t in enumerate(ts):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if o is None or h is None or l is None or c is None:
                continue
            out.append({"time": int(t), "open": float(o), "high": float(h),
                        "low": float(l), "close": float(c)})
        return out[-limit:]
    except Exception:
        return []


def refresh_ledger(ledger):
    changed = False
    for sig in ledger["signals"]:
        if sig.get("ret_24h") is not None:
            continue
        closes = fetch_bars(sig["symbol"], "1h", "5d")
        t0 = sig["signal_ts"]
        for label, oh in (("4h", 4), ("8h", 8), ("12h", 12), ("24h", 24)):
            if sig.get(f"ret_{label}") is not None:
                continue
            target = ((t0 + oh * 3600) // 3600) * 3600
            c = closes.get(target)
            if c is None:
                continue
            sig[f"price_{label}"] = c
            sig[f"ret_{label}"] = (c - sig["entry_price"]) / sig["entry_price"]
            changed = True
        last = fetch_quote(sig["symbol"])
        if last and last > 0 and sig.get("last_price") != last:
            sig["last_price"] = last
            sig["ret_now"] = (last - sig["entry_price"]) / sig["entry_price"]
            changed = True
    return changed


# ── rendering ──────────────────────────────────────────────────────────────
def fmt_price(p):
    if p is None:
        return "-"
    if p >= 1000:
        return f"{p:,.2f}"
    return f"{p:.2f}"


def fmt_ret(r):
    if r is None:
        return '<span class="na">待定</span>'
    pct = r * 100
    cls = "pos" if pct >= 0 else "neg"
    return f'<span class="{cls}">{pct:+.2f}%</span>'


def fmt_corr(corr):
    if corr is None:
        return '<span class="na">-</span>'
    cls = "pos" if corr < 0 else ("rs-mid" if corr >= 0.6 else "")
    return f'<span class="{cls}">{corr:.2f}</span>'


def _row(sig):
    corr = sig.get("corr")
    cells = [
        f'<td class="sym">{sig["symbol"]} <span class="kl">K线▸</span></td>',
        f'<td>{fmt_cst(sig["signal_ts"])}</td>',
        f'<td class="num">{fmt_price(sig["entry_price"])}</td>',
        f'<td class="num">{fmt_price(sig.get("last_price"))}</td>',
        f'<td class="num">{sig.get("atr_ratio", 0):.2f}×</td>',
        f'<td class="num">{fmt_corr(corr)}</td>',
        f'<td class="num">{fmt_ret(sig.get("ret_4h"))}</td>',
        f'<td class="num">{fmt_ret(sig.get("ret_8h"))}</td>',
        f'<td class="num">{fmt_ret(sig.get("ret_12h"))}</td>',
        f'<td class="num">{fmt_ret(sig.get("ret_now"))}</td>',
    ]
    onclick = (f'onclick="openKline(\'{sig["symbol"]}\', {sig["signal_ts"]}, '
               f'{sig["entry_price"]})"')
    return f'<tr class="clickable" {onclick}>' + "".join(cells) + "</tr>"


def _stats(signals):
    parts = [f'<span class="stat total">总信号数 <b>{len(signals)}</b></span>']
    for label, key in (("4h", "ret_4h"), ("8h", "ret_8h"), ("12h", "ret_12h"), ("当下", "ret_now")):
        vals = [s[key] for s in signals if s.get(key) is not None]
        n = len(vals)
        if n == 0:
            parts.append(f'<span class="stat"><b>{label}胜率</b> -</span>')
            continue
        wins = sum(1 for v in vals if v > 0)
        parts.append(f'<span class="stat"><b>{label}胜率</b> {wins / n * 100:.0f}% ({wins}/{n})</span>')
    return '<div class="stats">' + "".join(parts) + "</div>"


def _week_section(wk, signals, idx, active):
    rows = "\n".join(_row(s) for s in signals)
    disp = "block" if active else "none"
    return f"""
<div class="week" id="wk{idx}" style="display:{disp}">
  <h2>{week_label(signals[0]["signal_ts"])} <span class="wkid">{wk}</span>
      <span class="cnt">{len(signals)} 个信号</span></h2>
  <div class="table-wrap"><table>
    <thead><tr>
      <th>币</th><th>启动时间</th><th class="num">启动价</th><th class="num">现价</th>
      <th class="num">ATR</th><th class="num">相关性</th>
      <th class="num">+4h</th><th class="num">+8h</th><th class="num">+12h</th><th class="num">当下</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
</div>"""


CSS = """
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
          margin: 0; background: #0f1420; color: #e6e9ef; }
  header { padding: 20px 24px 8px; }
  header h1 { margin: 0 0 4px; font-size: 20px; }
  header .sub { color: #8b93a7; font-size: 13px; }
  .stats { padding: 12px 24px 0; display: flex; flex-wrap: wrap; gap: 18px;
           font-size: 13px; color: #aab2c5; }
  .stats .stat b { color: #e6e9ef; }
  .tabs { padding: 12px 24px 0; display: flex; flex-wrap: wrap; gap: 8px; }
  .tab { background: #1b2233; color: #aab2c5; border: 1px solid #2a3247;
          padding: 7px 14px; border-radius: 8px; cursor: pointer; font-size: 13px; }
  .tab.active { background: #2f6fed; color: #fff; border-color: #2f6fed; }
  .week { padding: 16px 24px 24px; }
  .week h2 { font-size: 16px; margin: 0 0 10px; color: #dbe1ec; }
  .wkid, .cnt { color: #6b7490; font-size: 12px; font-weight: normal; margin-left: 8px; }
  .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #232c40; }
  th { color: #8b93a7; font-weight: 600; background: #141b2b; }
  .sym { font-weight: 600; color: #fff; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .pos { color: #2ecc71; }
  .neg { color: #ff5c5c; }
  .na { color: #5b6377; }
  .rs-mid { color: #d9b64a; }
  .empty { color: #8b93a7; padding: 24px; }
  @media (max-width: 640px) {
    header { padding: 14px 12px 6px; }
    .stats { padding: 8px 12px 0; gap: 10px; font-size: 12px; }
    .week { padding: 12px 12px 20px; }
    table { font-size: 12px; }
    th, td { padding: 6px 6px; }
  }
  tr.clickable { cursor: pointer; }
  .kl { color: #2f6fed; font-size: 11px; font-weight: normal; }
  .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.62);
           z-index: 50; align-items: center; justify-content: center; }
  .modal-box { background: #0f1420; border: 1px solid #2a3247; border-radius: 10px;
               width: min(1000px, 94vw); max-height: 90vh; display: flex;
               flex-direction: column; }
  .modal-head { display: flex; align-items: center; gap: 12px;
                padding: 10px 14px; border-bottom: 1px solid #232c40; }
  #modal-title { font-weight: 600; flex: 1; }
  .bar-btn { background: #1b2233; color: #aab2c5; border: 1px solid #2a3247;
             padding: 4px 10px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  .bar-btn.active { background: #2f6fed; color: #fff; border-color: #2f6fed; }
  .close { background: none; border: none; color: #8b93a7; font-size: 16px; cursor: pointer; }
  .chart-body { height: 480px; padding: 8px; }
  .loading { color: #8b93a7; padding: 20px; }
"""

JS = """
var curSym = null, curBar = '1H', curTs = null, curPrice = null;
function show(i) {
  var tabs = document.querySelectorAll('.tab');
  var weeks = document.querySelectorAll('.week');
  tabs.forEach(function (t, n) { t.classList.toggle('active', n === i); });
  weeks.forEach(function (w, n) { w.style.display = (n === i) ? 'block' : 'none'; });
  try { localStorage.setItem('us_tab', String(i)); } catch (e) {}
}
function openKline(sym, ts, price) {
  curSym = sym; curBar = '1H'; curTs = ts; curPrice = price;
  document.getElementById('modal').style.display = 'flex';
  var sig = (ts != null) ? ('信号 ' + fmtT(ts) + ' @ $' + fmtP(price)) : '';
  document.getElementById('modal-title').textContent = sym + ' ' + sig;
  syncBarButtons(); loadKline();
}
function setBar(bar) { curBar = bar; syncBarButtons(); loadKline(); }
function syncBarButtons() {
  var btns = document.querySelectorAll('.bar-btn');
  for (var i = 0; i < btns.length; i++)
    btns[i].classList.toggle('active', btns[i].getAttribute('data-bar') === curBar);
}
function loadKline() {
  var body = document.getElementById('chart-body');
  body.innerHTML = '<div class="loading">加载中…</div>';
  fetch('/kline?symbol=' + encodeURIComponent(curSym) + '&bar=' + curBar + '&limit=200')
    .then(function (r) { return r.json(); })
    .then(function (d) { renderKline(d); })
    .catch(function (e) { body.textContent = '加载失败: ' + e; });
}
function closeModal() { document.getElementById('modal').style.display = 'none'; }
function fmtP(p) {
  if (p >= 1000) return p.toFixed(2);
  return p.toFixed(2);
}
function fmtT(ts) {
  var d = new Date(ts * 1000);
  var M = ('0' + (d.getMonth() + 1)).slice(-2), D = ('0' + d.getDate()).slice(-2);
  var h = ('0' + d.getHours()).slice(-2), m = ('0' + d.getMinutes()).slice(-2);
  return M + '-' + D + ' ' + h + ':' + m;
}
function renderKline(data) {
  var body = document.getElementById('chart-body');
  body.innerHTML = '';
  if (!data || !data.length) { body.textContent = '无数据'; return; }
  var W = 1000, H = 460, padL = 8, padR = 74, padT = 10, padB = 22;
  var pw = W - padL - padR, ph = H - padT - padB;
  var lo = Infinity, hi = -Infinity;
  for (var i = 0; i < data.length; i++) {
    if (data[i].low < lo) lo = data[i].low;
    if (data[i].high > hi) hi = data[i].high;
  }
  var range = (hi - lo) || 1;
  var n = data.length, step = pw / n, bw = Math.max(1, step * 0.62);
  function y(p) { return padT + (hi - p) / range * ph; }
  function x(i) { return padL + i * step + step / 2; }
  var NS = 'http://www.w3.org/2000/svg';
  var svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  svg.setAttribute('width', '100%'); svg.setAttribute('height', '100%');
  svg.style.display = 'block';
  for (var g = 0; g <= 4; g++) {
    var p = lo + range * g / 4, gy = y(p);
    var ln = document.createElementNS(NS, 'line');
    ln.setAttribute('x1', padL); ln.setAttribute('x2', padL + pw);
    ln.setAttribute('y1', gy); ln.setAttribute('y2', gy);
    ln.setAttribute('stroke', '#1f2737'); ln.setAttribute('stroke-width', '1');
    svg.appendChild(ln);
    var tx = document.createElementNS(NS, 'text');
    tx.setAttribute('x', padL + pw + 6); tx.setAttribute('y', gy + 4);
    tx.setAttribute('fill', '#6b7490'); tx.setAttribute('font-size', '11');
    tx.textContent = fmtP(p);
    svg.appendChild(tx);
  }
  for (var i = 0; i < n; i++) {
    var d = data[i], up = d.close >= d.open;
    var color = up ? '#2ecc71' : '#ff5c5c';
    var cx = x(i), oy = y(d.open), cy = y(d.close), hy = y(d.high), ly = y(d.low);
    var wk = document.createElementNS(NS, 'line');
    wk.setAttribute('x1', cx); wk.setAttribute('x2', cx);
    wk.setAttribute('y1', hy); wk.setAttribute('y2', ly);
    wk.setAttribute('stroke', color); wk.setAttribute('stroke-width', '1');
    svg.appendChild(wk);
    var top = Math.min(oy, cy), bh = Math.max(1, Math.abs(cy - oy));
    var rc = document.createElementNS(NS, 'rect');
    rc.setAttribute('x', cx - bw / 2); rc.setAttribute('y', top);
    rc.setAttribute('width', bw); rc.setAttribute('height', bh);
    rc.setAttribute('fill', color);
    svg.appendChild(rc);
  }
  if (curTs != null && curPrice != null && n >= 2) {
    var t0 = data[0].time, interval = data[1].time - data[0].time;
    var idx = interval > 0 ? (curTs - t0) / interval : 0;
    if (idx >= -0.5 && idx <= n + 0.5) {
      var sx = padL + idx * step + step / 2;
      sx = Math.max(padL, Math.min(padL + pw, sx));
      var sy = y(curPrice);
      var vl = document.createElementNS(NS, 'line');
      vl.setAttribute('x1', sx); vl.setAttribute('x2', sx);
      vl.setAttribute('y1', padT); vl.setAttribute('y2', padT + ph);
      vl.setAttribute('stroke', '#f5c542'); vl.setAttribute('stroke-width', '1.2');
      vl.setAttribute('stroke-dasharray', '4,4');
      svg.appendChild(vl);
      var hl = document.createElementNS(NS, 'line');
      hl.setAttribute('x1', padL); hl.setAttribute('x2', padL + pw);
      hl.setAttribute('y1', sy); hl.setAttribute('y2', sy);
      hl.setAttribute('stroke', '#f5c542'); hl.setAttribute('stroke-width', '1.2');
      hl.setAttribute('stroke-dasharray', '4,4');
      svg.appendChild(hl);
      var dot = document.createElementNS(NS, 'circle');
      dot.setAttribute('cx', sx); dot.setAttribute('cy', sy);
      dot.setAttribute('r', '4'); dot.setAttribute('fill', '#f5c542');
      svg.appendChild(dot);
      var lb = document.createElementNS(NS, 'text');
      lb.setAttribute('x', sx + 7); lb.setAttribute('y', sy - 7);
      lb.setAttribute('fill', '#f5c542'); lb.setAttribute('font-size', '12');
      lb.setAttribute('font-weight', '600');
      lb.textContent = '启动信号';
      svg.appendChild(lb);
    }
  }
  var idxs = [0, Math.floor(n / 2), n - 1];
  for (var k = 0; k < idxs.length; k++) {
    var ti = idxs[k];
    var tt = document.createElementNS(NS, 'text');
    tt.setAttribute('x', x(ti)); tt.setAttribute('y', H - 6);
    tt.setAttribute('fill', '#6b7490'); tt.setAttribute('font-size', '11');
    tt.setAttribute('text-anchor', ti === 0 ? 'start' : (ti === n - 1 ? 'end' : 'middle'));
    tt.textContent = fmtT(data[ti].time);
    svg.appendChild(tt);
  }
  body.appendChild(svg);
}
try { var _t = parseInt(localStorage.getItem('us_tab') || '0', 10); show(_t); } catch (e) {}
setInterval(function () {
  var m = document.getElementById('modal');
  if (m && m.style.display === 'flex') return;
  location.reload();
}, 60000);
"""


def render_html(ledger):
    signals = ledger["signals"]
    weeks = {}
    for s in signals:
        weeks.setdefault(s.get("week") or week_key(s["signal_ts"]), []).append(s)
    for k in weeks:
        weeks[k].sort(key=lambda s: s["signal_ts"], reverse=True)
    week_keys = sorted(weeks.keys(), reverse=True)

    stats = _stats(signals)
    if not week_keys:
        tabs = ""
        sections = '<p class="empty">还没有信号。等第一个「启动信号」出现。</p>'
    else:
        tabs = "\n".join(
            f'<button class="tab{" active" if i == 0 else ""}" onclick="show({i})">'
            f'{week_label(weeks[k][0]["signal_ts"])}</button>'
            for i, k in enumerate(week_keys))
        sections = "\n".join(
            _week_section(k, weeks[k], i, active=(i == 0))
            for i, k in enumerate(week_keys))

    updated = time.strftime("%m-%d %H:%M:%S")
    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="美股启动信号">
<meta name="theme-color" content="#0f1420">
<title>美股启动信号看板</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>美股启动信号看板</h1>
  <div class="sub">US 股票 · 15m波动放大(TR&gt;2×ATR14, 之前波动&lt;0.5%) · 日成交额≥2000万 · 相关性≤0.8(vs SPY) · 每60秒刷新 · 数据更新 {updated}</div>
</header>
{stats}
{tabs}
{sections}
<div class="modal" id="modal">
  <div class="modal-box">
    <div class="modal-head">
      <span id="modal-title"></span>
      <button class="bar-btn active" data-bar="1H" onclick="setBar('1H')">1H</button>
      <button class="bar-btn" data-bar="15m" onclick="setBar('15m')">15m</button>
      <button class="bar-btn" data-bar="1D" onclick="setBar('1D')">1D</button>
      <button class="close" onclick="closeModal()">✕</button>
    </div>
    <div class="chart-body" id="chart-body"></div>
  </div>
</div>
<script>{JS}</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._reply(200, "ok", "text/plain")
            return
        if path == "/kline":
            self._serve_kline()
            return
        ledger = load_ledger()
        now = time.time()
        if _CACHE["html"] is None or now - _CACHE["ts"] > REFRESH_INTERVAL:
            changed = refresh_ledger(ledger)
            if changed:
                save_ledger(ledger)
            _CACHE["html"] = render_html(ledger)
            _CACHE["ts"] = now
        self._reply(200, _CACHE["html"], "text/html; charset=utf-8")

    def _serve_kline(self):
        q = parse_qs(urlparse(self.path).query)
        symbol = (q.get("symbol") or [""])[0].strip().upper()
        bar = (q.get("bar") or ["1H"])[0]
        try:
            limit = min(int((q.get("limit") or ["200"])[0]), 500)
        except ValueError:
            limit = 200
        if not symbol or not symbol.replace("-", "").replace(".", "").isalnum():
            self._reply(400, "bad symbol", "text/plain")
            return
        if bar not in ("15m", "1H", "1D"):
            bar = "1H"
        data = fetch_ohlcv(symbol, bar, limit)
        self._reply(200, json.dumps(data), "application/json; charset=utf-8")

    def _reply(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def main():
    host = os.environ.get("US_DASHBOARD_HOST", "0.0.0.0")
    srv = ThreadingHTTPServer((host, PORT), Handler)
    print(f"us-breakout dashboard on http://{host}:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
