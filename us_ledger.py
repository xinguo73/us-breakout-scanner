#!/usr/bin/env python3
"""US breakout signal ledger: persist signals + return tracking (mirror of crypto ledger)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path.home() / ".hermes" / "data" / "us_breakout"
LEDGER_PATH = STATE_DIR / "us_breakout_ledger.json"
CST = timezone(timedelta(hours=8))


def load_ledger() -> dict:
    if LEDGER_PATH.exists():
        try:
            data = json.loads(LEDGER_PATH.read_text())
            if isinstance(data, dict) and isinstance(data.get("signals"), list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"signals": []}


def save_ledger(ledger: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2))
    tmp.replace(LEDGER_PATH)


def append_signal(sig: dict) -> bool:
    """Append a signal. Dedup by (symbol, signal_ts). Returns True if added."""
    ledger = load_ledger()
    sym = sig["symbol"]
    for s in ledger["signals"]:
        if s.get("symbol") == sym and s.get("signal_ts") == sig["signal_ts"]:
            return False
    sig["signal_date"] = datetime.fromtimestamp(sig["signal_ts"], tz=CST).strftime("%Y-%m-%d")
    sig["week"] = week_key(sig["signal_ts"])
    for k in ("ret_4h", "ret_8h", "ret_12h", "ret_now", "price_4h", "price_8h",
              "price_12h", "last_price"):
        sig.setdefault(k, None)
    ledger["signals"].append(sig)
    ledger["signals"].sort(key=lambda s: s["signal_ts"])
    save_ledger(ledger)
    return True


def load_state(sym: str) -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    sf = STATE_DIR / f"us_breakout_{sym}.json"
    if sf.exists():
        try:
            return json.loads(sf.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(sym: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"us_breakout_{sym}.json").write_text(json.dumps(state))


def week_key(ts: int) -> str:
    iso = datetime.fromtimestamp(ts, tz=CST).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def week_label(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=CST)
    monday = dt - timedelta(days=dt.weekday())
    sunday = monday + timedelta(days=6)
    return f"{monday.strftime('%m-%d')} ~ {sunday.strftime('%m-%d')}"


def fmt_cst(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=CST).strftime("%m-%d %H:%M")
