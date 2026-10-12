#!/usr/bin/env python3
"""美股高波动启动扫描（15m ATR 放大 + 成交额 + 相关性，仿 OKX crypto 扫描器）。

流程：
  1. Nasdaq screener 预筛当日放量异动的美股（价格>$2、日成交额>$2000万、|涨跌|>1.5%）。
  2. Yahoo chart 拉 15m K 线（含 SPY 基准），并发。
  3. 信号：最新已完成 15m 柱 TR > 2×ATR14，收阳，之前 ATR14 < 0.5% 现价，
     与 SPY 相关性 ≤ 0.8，跳过开盘第一根（跳空）。
  4. stdout 输出信号行（空输出=不投递），并写台账供看板。

纯标准库 requests + json + 并发；yfinance 仅用于分析脚本，本扫描器直接打 Yahoo chart。
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from us_ledger import append_signal, load_state, save_state  # noqa: E402

# ── 参数 ──────────────────────────────────────────────────────────────────
MIN_PRICE = 2.0               # 价格下限（剔毛票）
MIN_DOLLAR_VOL = 20_000_000   # 日成交额下限（USD）
MIN_PCT_CHANGE = 1.5          # 预筛：当日涨跌幅绝对值下限（%）
MAX_PRE_LIST = 150            # 预筛后最多拉多少只细看
ATR_PERIOD = 14               # ATR 周期
ATR_MULT = 2.0                # 放大倍数（TR > 2×ATR14）
MAX_ATR_PCT = 0.5             # 之前 ATR14 占现价上限(%)：安静基线
CORR_WINDOW = 16              # 相关性窗口（15m 柱 = 4h）
CORR_MAX = 0.8                # 相关性上限：>此值 = 跟随大盘
COOLDOWN_HOURS = 6            # 同一标的冷却期（小时）
BENCHMARK = "SPY"             # 大盘基准

NASDAQ_URL = "https://api.nasdaq.com/api/screener/stocks"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

NY = ZoneInfo("America/New_York")
CST = timezone(timedelta(hours=8))

_SESSION = requests.Session()


# ── 预筛：Nasdaq screener ─────────────────────────────────────────────────
def fetch_universe() -> list[dict]:
    r = _SESSION.get(NASDAQ_URL, params={"tableonly": "true", "download": "true"},
                     headers={**UA, "Accept": "application/json",
                              "Origin": "https://www.nasdaq.com"}, timeout=30)
    r.raise_for_status()
    rows = r.json()["data"]["rows"]
    out = []
    for t in rows:
        sym = t.get("symbol")
        if not sym or "-" in sym or "." in sym:   # 只要普通股
            continue
        try:
            price = float(t["lastsale"].replace("$", "").replace(",", ""))
            pct = float(t["pctchange"].replace("%", ""))
            vol = float(t["volume"].replace(",", ""))
        except (ValueError, TypeError, AttributeError):
            continue
        if price <= 0:
            continue
        dollar_vol = price * vol
        if price < MIN_PRICE or dollar_vol < MIN_DOLLAR_VOL:
            continue
        if abs(pct) < MIN_PCT_CHANGE:
            continue
        out.append({"symbol": sym, "price": price, "pct": pct,
                    "dollar_vol": dollar_vol})
    out.sort(key=lambda x: -x["dollar_vol"])
    return out[:MAX_PRE_LIST]


# ── 15m K 线：Yahoo chart ─────────────────────────────────────────────────
def fetch_15m(symbol: str) -> list[dict]:
    for attempt in range(3):
        try:
            r = requests.get(f"{YAHOO_CHART}/{symbol}",
                             params={"range": "5d", "interval": "15m"},
                             headers=UA, timeout=20)
            res = r.json()["chart"]["result"][0]
            ts = res.get("timestamp") or []
            q = (res.get("indicators", {}).get("quote") or [{}])[0]
            if not ts or not q:
                return []
            bars = []
            for i, t in enumerate(ts):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if o is None or h is None or l is None or c is None:
                    continue
                bars.append({"ts": int(t), "open": float(o), "high": float(h),
                             "low": float(l), "close": float(c)})
            return bars
        except Exception:
            if attempt < 2:
                time.sleep(0.6)
                continue
            return []
    return []


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy) ** 0.5


def corr_with_bench(bars, bench_closes, sig_idx):
    xs, ys = [], []
    for j in range(sig_idx - CORR_WINDOW + 1, sig_idx + 1):
        if j <= 0:
            continue
        c0, c1 = bars[j - 1]["close"], bars[j]["close"]
        t0, t1 = bars[j - 1]["ts"], bars[j]["ts"]
        b0, b1 = bench_closes.get(t0), bench_closes.get(t1)
        if c0 and c1 and b0 and b1:
            xs.append(c1 / c0 - 1.0)
            ys.append(b1 / b0 - 1.0)
    if len(xs) < 8:
        return None
    return pearson(xs, ys)


# ── 信号检测 ──────────────────────────────────────────────────────────────
def compute_signal(meta, bars, bench_closes, now_ts):
    n = len(bars)
    if n < ATR_PERIOD + 3:
        return None
    # 最新已完成柱：柱收盘时间 <= now
    sig_idx = None
    for i in range(n - 1, -1, -1):
        if bars[i]["ts"] + 900 <= now_ts:
            sig_idx = i
            break
    if sig_idx is None or sig_idx < ATR_PERIOD:
        return None
    bar = bars[sig_idx]
    # 跳过开盘第一根（含隔夜跳空）
    dt = datetime.fromtimestamp(bar["ts"], tz=NY)
    if dt.hour == 9 and dt.minute == 30:
        return None
    # 只报当前交易日的柱（跳过隔夜/周末的陈旧柱）
    if dt.date() != datetime.now(NY).date():
        return None
    # 收阳
    if bar["close"] <= bar["open"]:
        return None
    # TR 序列 + ATR14（信号柱之前 14 根）
    trs = [None] * n
    for i in range(1, n):
        hi, lo = bars[i]["high"], bars[i]["low"]
        pc = bars[i - 1]["close"]
        trs[i] = max(hi - lo, abs(hi - pc), abs(lo - pc))
    latest_tr = trs[sig_idx]
    if latest_tr is None:
        return None
    window = [x for x in trs[sig_idx - ATR_PERIOD:sig_idx] if x is not None]
    if not window:
        return None
    atr14 = sum(window) / len(window)
    if atr14 <= 0:
        return None
    ratio = latest_tr / atr14
    atr_pct = atr14 / bar["close"] * 100
    if ratio <= ATR_MULT:
        return None
    if atr_pct >= MAX_ATR_PCT:
        return None
    corr = corr_with_bench(bars, bench_closes, sig_idx)
    if corr is not None and corr > CORR_MAX:
        return None
    return {
        "symbol": meta["symbol"], "price": bar["close"],
        "pct": meta["pct"], "dollar_vol": meta["dollar_vol"],
        "atr_ratio": ratio, "atr_pct": atr_pct, "corr": corr,
        "bar_ts": bar["ts"], "bar_et": dt.strftime("%m-%d %H:%M"),
    }


def market_open() -> bool:
    now = datetime.now(NY)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def fmt_price(p):
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.2f}"
    return f"{p:.4f}"


def fmt_vol(v):
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    return f"${v / 1e6:.1f}M"


def format_signal(sig, cst_time):
    lines = [f"【启动信号，ATR放大】{sig['symbol']}"]
    lines.append(f"现价 ${fmt_price(sig['price'])}（当日 {sig['pct']:+.2f}%）")
    lines.append(f"ATR放大 {sig['atr_ratio']:.2f}× · 之前波动 {sig['atr_pct']:.2f}%")
    if sig.get("corr") is not None:
        lines.append(f"相关性 {sig['corr']:.2f}（vs {BENCHMARK} 4h）")
    lines.append(f"成交额 {fmt_vol(sig['dollar_vol'])}")
    lines.append(f"时间 {sig['bar_et']} ET / {cst_time}（北京）")
    return "\n".join(lines)


def main() -> int:
    if "--selftest" in sys.argv:
        print("【美股启动扫描·投递测试】收到此消息即表示飞书群投递正常。"
              f"策略：美股，15m当前TR>{ATR_MULT}xATR{ATR_PERIOD}且之前波动<{MAX_ATR_PCT}%"
              f"且收阳且日成交额≥{MIN_DOLLAR_VOL / 1e6:.0f}M且相关性≤{CORR_MAX}(vs {BENCHMARK})。")
        return 0

    force = "--force" in sys.argv
    if not force and not market_open():
        return 0   # 非美股交易时段，静默

    now_ts = int(time.time())
    cst_time = datetime.now(CST).strftime("%m-%d %H:%M")

    # 1) 预筛
    try:
        universe = fetch_universe()
    except Exception as e:
        print(json.dumps({"status": "universe_fetch_failed", "error": str(e)},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    if not universe:
        return 0

    # 2) 拉 15m（含 SPY 基准）
    symbols = [u["symbol"] for u in universe] + [BENCHMARK]
    bars_map = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for sym, bars in zip(symbols, ex.map(fetch_15m, symbols)):
            bars_map[sym] = bars
    bench_closes = {b["ts"]: b["close"] for b in bars_map.get(BENCHMARK, [])}
    if not bench_closes:
        return 0

    # 3) 信号检测
    emitted = 0
    for meta in universe:
        bars = bars_map.get(meta["symbol"]) or []
        sig = compute_signal(meta, bars, bench_closes, now_ts)
        if sig is None:
            continue
        state = load_state(meta["symbol"])
        if state.get("last_alert_bar_ts") == sig["bar_ts"]:
            continue
        last_ts = state.get("last_alert_ts")
        if last_ts and (now_ts - last_ts) < COOLDOWN_HOURS * 3600:
            continue
        print(format_signal(sig, cst_time))
        emitted += 1
        save_state(meta["symbol"], {"last_alert_bar_ts": sig["bar_ts"],
                                    "last_alert_ts": now_ts})
        append_signal({
            "symbol": sig["symbol"], "signal_ts": sig["bar_ts"],
            "entry_price": sig["price"], "atr_ratio": round(sig["atr_ratio"], 3),
            "atr_pct": round(sig["atr_pct"], 3),
            "corr": round(sig["corr"], 3) if sig["corr"] is not None else None,
            "dollar_vol": round(sig["dollar_vol"], 2),
            "pct": round(sig["pct"], 2),
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
