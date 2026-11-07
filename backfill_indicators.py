# -*- coding: utf-8 -*-
"""
indicator_daily 逐日回填（补齐 PRD 表 10 的历史序列）。

背景：主流程 `etf_monitor.py --cloud` 每日只写当日一条 indicator_daily，
PRD 要求指标层保留可追溯的历史序列（用于事后复核、回放与参数敏感性）。
本脚本读取 output/price_history_<code>.csv 的日线收盘价，逐日重算
MA20/60/120/250、动量 20/60/120、波动率 20/60（年化 %）、60 日最大回撤、
RSI14，再通过 smart_db.sync_indicator 落库（ON CONFLICT 幂等更新）。

口径与限制：
- 数据源只有 date, close 两列（腾讯/新浪 ETF 日线，不复权），没有 high/low，
  因此 ATR 与 ADX 无法计算，字段留空并在 model_version 说明；不得用估算值填充。
- 波动率为日收益标准差 × √252 × 100；动量为区间涨跌幅 %；回撤为负值 %。
- 只用当日及之前数据，无未来函数。

用法：
    python backfill_indicators.py            # 全量回填
    python backfill_indicators.py --dry-run  # 只统计不写库
"""
import glob
import io
import os
import sys

import numpy as np
import pandas as pd

import smart_db

IND_VERSION = "indicator-v5-2026-11-07"
MIN_ROWS = 60


def _rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"].astype(float)
    out = pd.DataFrame({"date": df["date"].dt.strftime("%Y-%m-%d")})
    for w in (20, 60, 120, 250):
        out["ma%d" % w] = close.rolling(w).mean()
    ret = close.pct_change()
    for w in (20, 60, 120):
        out["momentum%d" % w] = (close / close.shift(w) - 1.0) * 100.0
    for w in (20, 60):
        out["volatility%d" % w] = ret.rolling(w).std() * np.sqrt(252.0) * 100.0
    roll_max = close.rolling(60).max()
    out["max_drawdown60"] = (close / roll_max - 1.0) * 100.0
    out["rsi"] = _rsi(close)
    return out


def main(argv):
    dry = "--dry-run" in argv
    rows_total = 0
    files = sorted(glob.glob(os.path.join("output", "price_history_*.csv")))
    if not files:
        print("no price history csv found")
        return 1
    conn = None if dry else smart_db.connect(os.path.join("output", "smart_etf.db"))
    for path in files:
        code = os.path.basename(path).replace("price_history_", "").replace(".csv", "")
        df = pd.read_csv(path, encoding="utf-8-sig")
        if "date" not in df.columns or "close" not in df.columns:
            print("skip (bad columns):", path)
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
        if len(df) < MIN_ROWS:
            print("skip (too short):", code, len(df))
            continue
        ind = indicators(df)
        n = 0
        for _, r in ind.iterrows():
            if pd.isna(r.get("ma20")):
                continue
            if dry:
                n += 1
                continue
            smart_db.sync_indicator(conn, code, r["date"], {
                "ma20": r["ma20"], "ma60": r["ma60"], "ma120": r["ma120"], "ma250": r["ma250"],
                "momentum20": r["momentum20"], "momentum60": r["momentum60"],
                "momentum120": r["momentum120"], "volatility20": r["volatility20"],
                "volatility60": r["volatility60"], "max_drawdown60": r["max_drawdown60"],
                "rsi": r["rsi"],
            }, IND_VERSION)
            n += 1
        if not dry:
            conn.commit()
        rows_total += n
        print("%s rows=%d first=%s last=%s" % (code, n, ind["date"].iloc[0], ind["date"].iloc[-1]))
    if conn is not None:
        counts = smart_db.counts(conn)
        print("indicator_daily total =", counts.get("indicator_daily"))
        conn.close()
    print("backfill rows =", rows_total, "(dry)" if dry else "")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
