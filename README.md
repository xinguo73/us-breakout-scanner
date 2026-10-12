# 美股高波动启动扫描器

仿照 OKX crypto「启动信号」扫描器，抓美股高波动股票的 15m ATR 放大启动信号。

## 信号定义

- 最新已完成 15m 柱真实波幅 TR > 2.0 × ATR14（前 14 根 15m 平均真实波幅）
- 该柱收阳（close > open，剔除砸盘阴线）
- 之前 ATR14 占现价 < 0.5%（安静基线，剔除「已高波动」的票）
- 日成交额 ≥ 2000 万 USD（预筛，剔除微盘）
- 与 SPY 相关性 ≤ 0.8（近 4h 15m 收益率 Pearson，剔除跟随大盘的 beta）
- 跳过开盘第一根（9:30 ET，含隔夜跳空）
- 同一标的 6h 冷却期内不重复报

## 参数

| 参数 | 值 | 说明 |
|---|---|---|
| `ATR_MULT` | 2.0 | 放大倍数 |
| `ATR_PERIOD` | 14 | ATR 周期 |
| `MAX_ATR_PCT` | 0.5 | 之前 ATR14 占现价上限(%) |
| `MIN_DOLLAR_VOL` | 20M | 日成交额下限(USD) |
| `MIN_PCT_CHANGE` | 1.5 | 预筛当日涨跌幅下限(%) |
| `CORR_WINDOW` | 16 | 相关性窗口(15m柱=4h) |
| `CORR_MAX` | 0.8 | 相关性上限 |
| `COOLDOWN_HOURS` | 6 | 冷却期 |

## 架构（选项 C：实时预筛）

1. Nasdaq screener API 预筛当日放量异动（价格>$2、日成交额>$2000万、|涨跌|>1.5%），取前 150 只。
2. Yahoo chart API 并发拉 15m K 线（含 SPY 基准）。
3. 检测信号 → stdout 输出（空=不投递）+ 写台账。

## 文件

- `us_breakout_monitor.py` — 扫描器（cron 每 5 分钟跑，自判美股交易时段）
- `us_ledger.py` — 信号台账
- `us_dashboard_server.py` — 网页看板（端口 8788）
- `us_breakout_monitor.sh` — cron wrapper（代理 env + venv python）
- `deploy/us-breakout-dashboard.service` — systemd 用户服务

## 运行

```bash
# 手动跑（--force 跳过交易时段判断，用于测试）
python3 us_breakout_monitor.py --force

# 投递测试
python3 us_breakout_monitor.py --selftest

# 看板
systemctl --user start us-breakout-dashboard  # http://127.0.0.1:8788
```

## 数据源

- Nasdaq screener API（预筛，可靠）
- Yahoo chart API（15m/1h K 线，可靠）
- 生产脚本优先直连 Yahoo chart（不用 yfinance，避免 429/退市噪音）
