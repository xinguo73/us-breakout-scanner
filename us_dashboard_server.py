#!/usr/bin/env python3
"""US breakout signal dashboard (mirror of crypto dashboard).

Serves the signal ledger as an auto-refreshing web page: weekly tabs, stats,
returns at +4h/+8h/+12h/+24h (via 1H bars), and a simple SVG K-line modal.
Pure stdlib + requests.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
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
        f'<td class="sym">{sig["symbol"]}</td>',
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
    return "<tr>" + "".join(cells) + "</tr>"


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
"""

JS = """
function show(i) {
  var tabs = document.querySelectorAll('.tab');
  var weeks = document.querySelectorAll('.week');
  tabs.forEach(function (t, n) { t.classList.toggle('active', n === i); });
  weeks.forEach(function (w, n) { w.style.display = (n === i) ? 'block' : 'none'; });
  try { localStorage.setItem('us_tab', String(i)); } catch (e) {}
}
try { var _t = parseInt(localStorage.getItem('us_tab') || '0', 10); show(_t); } catch (e) {}
setInterval(function () { location.reload(); }, 60000);
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
<script>{JS}</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._reply(200, "ok", "text/plain")
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
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"us-breakout dashboard on http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
