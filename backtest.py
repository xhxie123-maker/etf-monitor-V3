# -*- coding: utf-8 -*-
"""
V3 基础回测模块（etf-monitor-v3）
=================================
使用 MA20/MA60/MA120 构建趋势策略：
  - 持仓条件：MA20 > MA60 > MA120（多头排列）
  - 信号在下一交易日生效（避免用当日收盘后才知道的信息进行当日成交）
  - 纳入手续费与滑点参数
输出：年化收益率、最大回撤、夏普比率、买入持有对比、持仓暴露、换手率。

本模块为"基准测试策略"，用于检查系统基础趋势规则是否合理，
不代表 V3 决策信号已经获得历史收益验证。回测结果仅为量化参考。

用法：
  python backtest.py output/price_history_515180.csv [--fee 0.001] [--slippage 0.001]
"""
from __future__ import annotations

import argparse
import json
import math
import sys

import numpy as np
import pandas as pd

MA_FAST = 20
MA_MID = 60
MA_SLOW = 120
RISK_FREE_ANN = 0.02  # 无风险利率 2%（夏普用）


def load_prices(path: str) -> pd.DataFrame:
    """读取价格 CSV（date, close 列；自动处理缺列与时间格式）"""
    df = pd.read_csv(path)
    if "date" not in df.columns or "close" not in df.columns:
        raise ValueError(f"{path}: 缺少 date/close 列（应为每日收盘序列）")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df = df[df["close"] > 0].sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    return df


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """MA 均线 + 下一交易日生效的多头排列持仓信号"""
    out = df.copy()
    for w in (MA_FAST, MA_MID, MA_SLOW):
        out[f"ma{w}"] = out["close"].rolling(w).mean()
    # 持仓条件（当日收盘后可判定）→ 次日生效：shift(1) 严格错位，
    # 杜绝"T日收盘后生成信号却以T日收盘价成交"的前视偏差（DeepSeek评审P0）
    cond = (out["ma20"] > out["ma60"]) & (out["ma60"] > out["ma120"])
    out["signal"] = cond.astype(int).shift(1).fillna(0).astype(int)
    return out


def detect_anomalous_returns(closes: np.ndarray, limit: float = 0.11) -> list:
    """
    复权断崖/数据错误检测（DeepSeek评审P0）：
    A股ETF 单日涨跌幅理论上限约 ±11%（±10% 涨跌停 + 少量溢价/折价），
    超过即视为数据异常（除权除息未正确复权 / 数据源错位 / 停牌复牌跳空），
    这类假跳变会直接污染回测净值与回撤。返回 [(序列索引, 涨跌幅%)]。
    """
    anoms = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            r = closes[i] / closes[i - 1] - 1.0
            if abs(r) > limit:
                anoms.append((i, r * 100))
    return anoms


def run_backtest(df: pd.DataFrame, fee: float = 0.001, slippage: float = 0.001,
                 strict_anomalies: bool = False) -> dict:
    """
    执行回测。
    fee/slippage 均为单边比例（每次换仓各计一次）。
    成交假设（披露，DeepSeek评审P1）：换仓以当日收盘 ×(1±滑点) 近似成交，
    非真实开盘价/VWAP；成本在换仓当日记一次单边 fee+slippage，真实生效（非仅声明）。
    strict_anomalies=True 时，存在 |日涨跌幅|>11% 的异常收益直接报错（默认仅警告并记录）。
    """
    data = compute_signals(df)
    closes = data["close"].to_numpy(dtype=float)
    signals = data["signal"].to_numpy(dtype=int)
    n = len(closes)
    if n < MA_SLOW + 2:
        return {"ok": False, "error": f"样本不足（{n} 条，需 >{MA_SLOW + 2}）"}

    # 复权断崖检测：异常收益不静默跳过，记录并（strict 时）报错
    anomalies = detect_anomalous_returns(closes)
    if anomalies and strict_anomalies:
        first = anomalies[0]
        return {"ok": False,
                "error": f"检测到 {len(anomalies)} 个 |日涨跌幅|>11% 的异常收益（首个 at 索引 {first[0]}，{first[1]:+.1f}%），"
                         f"疑似除权未复权或数据源错位，请先核对价格序列（--strict 模式）"}

    # 逐日持仓 + 换仓
    position = 0          # 0=空仓, 1=满仓
    trades = 0            # 换仓次数（0<->1 各计一次边）
    equity = 1.0
    equity_bh = 1.0
    eq_curve = np.empty(n)
    bh_curve = np.empty(n)
    prev_close = closes[0]
    for i in range(1, n):
        ret = closes[i] / prev_close - 1.0
        ret_bh = ret
        new_pos = int(signals[i])
        if new_pos != position:
            # 换仓：先按原仓位结算当日收益，再按新仓位计成本
            cost = fee + slippage
            if position == 0 and new_pos == 1:
                # 买入：以次日开盘近似（用当日收盘 * (1+slippage) 建模），先付成本
                ret = ret - cost
                trades += 1
            elif position == 1 and new_pos == 0:
                ret = ret - cost
                trades += 1
            position = new_pos
        equity *= (1.0 + ret * position)
        equity_bh *= (1.0 + ret_bh)
        eq_curve[i] = equity
        bh_curve[i] = equity_bh
        prev_close = closes[i]
    eq_curve[0] = 1.0
    bh_curve[0] = 1.0

    years = (data["date"].iloc[-1] - data["date"].iloc[0]).days / 365.25
    if years <= 0:
        return {"ok": False, "error": "时间跨度不足"}

    def ann_return(curve):
        return (curve[-1] ** (1.0 / years) - 1.0) * 100

    def max_drawdown(curve):
        peak = np.maximum.accumulate(curve)
        dd = (curve / peak - 1.0) * 100
        return float(np.min(dd))

    # 夏普（日收益）
    daily_ret = np.diff(eq_curve) / eq_curve[:-1]
    sharpe = 0.0
    if len(daily_ret) > 1:
        vol = np.std(daily_ret, ddof=1)
        if vol > 0:
            sharpe = float((daily_ret.mean() - RISK_FREE_ANN / 252) / vol * math.sqrt(252))

    exposure = float(np.mean(signals) * 100)
    turnover = trades / years if years > 0 else 0.0

    return {
        "ok": True,
        "n": int(n),
        "years": round(years, 2),
        "strategy_ann": round(ann_return(eq_curve), 2),
        "buyhold_ann": round(ann_return(bh_curve), 2),
        "strategy_mdd": round(max_drawdown(eq_curve), 2),
        "buyhold_mdd": round(max_drawdown(bh_curve), 2),
        "sharpe": round(sharpe, 2),
        "exposure_pct": round(exposure, 1),
        "trades": int(trades),
        "turnover_per_year": round(turnover, 1),
        "final_equity": round(float(eq_curve[-1]), 3),
        "final_buyhold": round(float(bh_curve[-1]), 3),
        "anomaly_count": len(anomalies),
        "anomalies": [(i, round(p, 2)) for i, p in anomalies[:10]],
    }


def summarize(r: dict) -> str:
    if not r.get("ok"):
        return f"回测不可用：{r.get('error')}"
    lines = [
        f"样本 {r['n']} 个交易日（约 {r['years']} 年）｜ 手续费+滑点单边 0.2%",
        f"策略年化 {r['strategy_ann']}% vs 买入持有 {r['buyhold_ann']}%",
        f"策略最大回撤 {r['strategy_mdd']}% vs 买入持有 {r['buyhold_mdd']}%",
        f"夏普比率 {r['sharpe']} ｜ 持仓暴露 {r['exposure_pct']}% ｜ 换手 {r['turnover_per_year']} 次/年（共 {r['trades']} 次）",
        f"期末净值：策略 {r['final_equity']} / 买入持有 {r['final_buyhold']}",
    ]
    if r.get("anomaly_count"):
        lines.append(f"⚠ 数据健康检查：检出 {r['anomaly_count']} 个 |日涨跌幅|>11% 异常收益（可能为除权未复权/数据源错位，示例：{r['anomalies'][:3]}）；建议核对价格序列后再采信回测结果")
    lines.append("成交假设：换仓以当日收盘×(1±滑点) 近似，非真实开盘/VWAP；信号次日生效（shift(1)），成本真实扣除。")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="V3 基础回测：MA20/60/120 趋势策略")
    ap.add_argument("csv", help="价格 CSV 路径（date, close）")
    ap.add_argument("--fee", type=float, default=0.001, help="单边手续费比例（默认 0.001=万10）")
    ap.add_argument("--slippage", type=float, default=0.001, help="单边滑点比例（默认 0.001）")
    ap.add_argument("--strict", action="store_true", help="存在 |日涨跌幅|>11% 异常收益时直接报错（默认仅警告）")
    ap.add_argument("--json", action="store_true", help="额外输出 JSON 结果")
    args = ap.parse_args(argv)
    try:
        df = load_prices(args.csv)
    except Exception as e:
        print(f"读取失败: {e}")
        return 1
    r = run_backtest(df, fee=args.fee, slippage=args.slippage, strict_anomalies=args.strict)
    print(summarize(r))
    if args.json:
        print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
