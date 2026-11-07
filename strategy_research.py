# -*- coding: utf-8 -*-
"""
strategy_research.py — V4 综合策略回测与滚动验证（GPT 评审 P2-1）

六基准（GPT 五.策略验证体系）：
  1. buy_hold        买入持有（一次性，验证择时是否创造价值）
  2. fixed_dca       固定周期定投（现金流，验证长期定投）
  3. periodic_rebal  定期再平衡（单资产退化为持有，组合再平衡由 decision/portfolio 覆盖）
  4. pe_dca          PE 分位定投（现金流，估值择时）
  5. trend_filter    趋势过滤（一次性，风险过滤）
  6. v4_combined     V4 综合（趋势闸门 + PE 调整，现金流+一次性双口径）

口径区分（关键，避免不可比）：
  - 一次性策略：初始 1 单位，输出 _perf 全套（CAGR/MDD/Sharpe/Sortino/Calmar/CVaR...）
  - 现金流策略：定期投入，输出累计投入/期末市值/资金加权 IRR（年化）
  - 成本：买入按 fee 扣减份额；趋势类买卖双向成本走 backtest._run_curve
滚动验证：固定长度窗口滚动，输出各策略年化/MDD 分布（参数/窗口稳定性）。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

import backtest as bt

TRADING_DAYS = 252
DEFAULT_PERIOD = 20          # 每 20 交易日一笔定投
DEFAULT_BASE_C = 1.0


# ---------------- 现金流 IRR ----------------

def xirr(cashflows, day_index):
    """牛顿法求日频 IRR。cashflows: 金额（负投入/正回收），day_index: 对应交易日位置。"""
    def npv(r):
        return sum(cf / (1 + r) ** t for cf, t in zip(cashflows, day_index))

    def dnpv(r):
        return sum(-t * cf / (1 + r) ** (t + 1) for cf, t in zip(cashflows, day_index))

    r = 0.0005
    for _ in range(200):
        f = npv(r)
        df = dnpv(r)
        if abs(df) < 1e-12:
            break
        r_new = r - f / df
        if r_new <= -0.999:
            r_new = -0.5
        if abs(r_new - r) < 1e-10:
            r = r_new
            break
        r = r_new
    return r


def _ann_from_daily(r):
    return (1 + r) ** TRADING_DAYS - 1.0


# ---------------- 定投乘数规则 ----------------

def pe_multiplier(pe_pct, cfg_dca):
    """PE 分位 → 投入乘数。"""
    if pe_pct is None or pe_pct != pe_pct:
        return 0.0
    if pe_pct < cfg_dca["double_below_pct"]:
        return 2.0
    if pe_pct < cfg_dca["normal_below_pct"]:
        return 1.0
    if pe_pct < cfg_dca["half_below_pct"]:
        return 0.5
    return 0.0


# ---------------- 现金流模拟 ----------------

def dca_simulate(close, period, base_c, fee, multiplier_seq=None,
                 allow_seq=None):
    """
    份额账户定投模拟。
    multiplier_seq: 每投入日的乘数（缺省恒 1）；allow_seq: 该投入日是否允许（0/1，趋势闸门）。
    返回 dict：投入现金流、累计投入、份额、期末市值、IRR 年化。
    """
    close = np.asarray(close, dtype=float)
    n = len(close)
    invest_idx = list(range(period, n, period))
    cashflows, day_index = [], []
    total_in = 0.0
    units = 0.0
    for k, i in enumerate(invest_idx):
        mult = multiplier_seq[i] if multiplier_seq is not None else 1.0
        allowed = allow_seq[i] if allow_seq is not None else 1
        c = base_c * mult * allowed
        if c <= 0:
            continue
        price = float(close[i])
        units += c * (1 - fee) / price
        total_in += c
        cashflows.append(-c)
        day_index.append(i)
    end_value = units * float(close[-1])
    irr_ann = None
    if cashflows:
        cf = cashflows + [end_value]
        di = day_index + [n - 1]
        irr_ann = _ann_from_daily(xirr(cf, di))
    return {
        "n_contributions": len(cashflows), "total_invested": round(total_in, 4),
        "units": round(units, 6), "end_value": round(end_value, 4),
        "total_multiple": round(end_value / total_in, 4) if total_in else None,
        "irr_ann_pct": round(irr_ann * 100, 2) if irr_ann is not None else None,
    }


# ---------------- 一次性策略 ----------------

def lump_simulate(close, signal_seq, fee, slip, dates):
    """0/1 目标仓位 → backtest._run_curve + _perf。"""
    closes = np.asarray(close, dtype=float)
    sig = np.asarray(signal_seq, dtype=int)
    core = bt._run_curve(closes, sig, fee, slip)
    perf = bt._perf(core["eq_curve"], dates)
    return {
        "final_equity": round(float(core["eq_curve"][-1]), 4),
        "trades": core["trades"], "cost_total_pct": round(core["cost_total"] * 100, 3),
        "cagr_pct": round(perf.get("cagr", 0) * 100, 2),
        "mdd_pct": round(perf.get("mdd", 0) * 100, 2),
        "sharpe": round(perf.get("sharpe", 0), 2),
        "sortino": round(perf.get("sortino", 0), 2),
        "calmar": round(perf.get("calmar", 0), 2),
        "cvar95_pct": perf.get("cvar95"),
        "annual_win_rate_pct": perf.get("annual_win_rate"),
        "exposure_pct": round(float(np.mean(sig)) * 100, 1),
    }


# ---------------- 六策略主流程 ----------------

def run_strategies(df, dca_cfg, fee=0.001, slip=0.001, period=DEFAULT_PERIOD,
                   pe_col="pe_pct", trend_col="trend_pos"):
    """
    df: 含 date、close；可选 pe_pct（当日PE分位）、trend_pos（0/1 趋势仓位）。
    返回六策略结果 dict。
    """
    df = df.reset_index(drop=True)
    dates = df["date"]
    close = df["close"].astype(float)
    n = len(df)
    has_pe = pe_col in df.columns
    has_trend = trend_col in df.columns

    # 序列准备
    pe_seq = df[pe_col].tolist() if has_pe else [None] * n
    trend_seq = df[trend_col].astype(int).tolist() if has_trend else [1] * n

    # 1 买入持有：信号全 1（首日建仓）
    sig_hold = [0] + [1] * (n - 1)
    r_bh = lump_simulate(close, sig_hold, fee, slip, dates)

    # 2 固定定投
    r_fixed = dca_simulate(close, period, DEFAULT_BASE_C, fee)

    # 3 定期再平衡（单资产 = 持有）
    r_rebal = {"note": "单资产口径等同买入持有；多资产再平衡见 decision.rebalance_plan",
               "cagr_pct": r_bh["cagr_pct"], "mdd_pct": r_bh["mdd_pct"]}

    # 4 PE 分位定投
    if has_pe:
        mult = [pe_multiplier(pe_seq[i], dca_cfg) for i in range(n)]
        r_pe = dca_simulate(close, period, DEFAULT_BASE_C, fee, multiplier_seq=mult)
    else:
        r_pe = {"note": "无PE分位数据，PE定投不可评估"}

    # 5 趋势过滤
    r_trend = lump_simulate(close, trend_seq, fee, slip, dates)

    # 6 V4 综合：趋势闸门 + PE 乘数（无PE则乘数恒1）
    if has_pe:
        mult_v4 = [pe_multiplier(pe_seq[i], dca_cfg) for i in range(n)]
    else:
        mult_v4 = [1.0] * n
    r_v4_cf = dca_simulate(close, period, DEFAULT_BASE_C, fee,
                           multiplier_seq=mult_v4, allow_seq=trend_seq)
    # V4 一次性口径：趋势仓位（与 trend_filter 同骨架，综合规则在现金流口径体现）
    r_v4_lump = lump_simulate(close, trend_seq, fee, slip, dates)

    return {
        "buy_hold": r_bh, "fixed_dca": r_fixed, "periodic_rebal": r_rebal,
        "pe_dca": r_pe, "trend_filter": r_trend,
        "v4_combined": {"cashflow": r_v4_cf, "lump": r_v4_lump},
    }


# ---------------- 滚动窗口验证 ----------------

def rolling_validate(df, dca_cfg, window=500, step=250, **kw):
    """固定长度窗口滚动，输出每窗口各一次性策略 CAGR/MDD，用于稳定性判断。"""
    n = len(df)
    out = []
    start = 0
    while start + window <= n:
        sub = df.iloc[start:start + window].reset_index(drop=True)
        r = run_strategies(sub, dca_cfg, **kw)
        out.append({
            "window": start, "start": str(sub["date"].iloc[0].date()),
            "end": str(sub["date"].iloc[-1].date()),
            "buy_hold_cagr": r["buy_hold"]["cagr_pct"],
            "buy_hold_mdd": r["buy_hold"]["mdd_pct"],
            "trend_cagr": r["trend_filter"]["cagr_pct"],
            "trend_mdd": r["trend_filter"]["mdd_pct"],
            "v4_cagr": r["v4_combined"]["lump"]["cagr_pct"],
        })
        start += step
    return out


def stability_summary(rolling):
    """滚动窗口统计：各策略 CAGR 均值/标准差/胜率（相对买入持有）。"""
    def stats(key):
        vals = [w[key] for w in rolling if w[key] is not None]
        if not vals:
            return None
        return {"mean_pct": round(float(np.mean(vals)), 2),
                "std_pct": round(float(np.std(vals, ddof=1)), 2) if len(vals) > 1 else 0.0,
                "min_pct": round(float(np.min(vals)), 2), "max_pct": round(float(np.max(vals)), 2)}
    wins = sum(1 for w in rolling if w["trend_cagr"] is not None and
               w["trend_cagr"] > w["buy_hold_cagr"])
    return {
        "buy_hold": stats("buy_hold_cagr"), "trend": stats("trend_cagr"),
        "v4": stats("v4_cagr"), "n_windows": len(rolling),
        "trend_beat_bh_windows": wins,
        "trend_beat_bh_rate_pct": round(wins / len(rolling) * 100, 1) if rolling else None,
    }


# ---------------- 自测 ----------------

def self_test():
    # 构造 100 日：前 60 日较快上涨、后 40 日温和调整但期末仍高于早期买入价
    n = 100
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    close = []
    p = 1.0
    for i in range(n):
        p *= 1.006 if i < 60 else 0.998
        close.append(round(p, 4))
    pe_pct = [25.0] * 40 + [80.0] * 30 + [15.0] * 30
    trend_pos = [1] * 65 + [0] * 35
    df = pd.DataFrame({"date": dates, "close": close,
                       "pe_pct": pe_pct, "trend_pos": trend_pos})
    dca_cfg = {"double_below_pct": 20, "normal_below_pct": 40, "half_below_pct": 70}

    # PE 乘数规则
    assert pe_multiplier(10, dca_cfg) == 2.0
    assert pe_multiplier(35, dca_cfg) == 1.0
    assert pe_multiplier(60, dca_cfg) == 0.5
    assert pe_multiplier(85, dca_cfg) == 0.0
    assert pe_multiplier(None, dca_cfg) == 0.0

    # 固定定投（整体净上涨序列，资金加权 IRR 应为正）
    fixed = dca_simulate(close, 20, 1.0, 0.001)
    assert fixed["n_contributions"] == 4 and fixed["total_invested"] == 4.0
    assert fixed["end_value"] > 0 and fixed["irr_ann_pct"] is not None
    assert fixed["irr_ann_pct"] > 0

    # 持续下跌序列：IRR 符号方向应为负（验证 IRR 不是恒为正）
    down = [round(0.998 ** i, 4) for i in range(1, n + 1)]
    r_down = dca_simulate(down, 20, 1.0, 0.001)
    assert r_down["irr_ann_pct"] < 0

    # 六策略
    r = run_strategies(df, dca_cfg)
    assert set(["buy_hold", "fixed_dca", "periodic_rebal", "pe_dca",
                "trend_filter", "v4_combined"]).issubset(r.keys())
    assert r["buy_hold"]["cagr_pct"] is not None
    assert r["pe_dca"]["n_contributions"] >= 1

    # 滚动
    rl = rolling_validate(df, dca_cfg, window=60, step=40)
    assert len(rl) >= 1
    ss = stability_summary(rl)
    assert ss["n_windows"] == len(rl)

    print("strategy_research self_test: 6/6 PASS")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        self_test()
    else:
        self_test()
