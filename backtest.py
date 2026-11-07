# -*- coding: utf-8 -*-
"""
V4 回测引擎（etf-monitor-v4）
=============================
修复 V3 回测的三处缺陷（GPT 评审 P0-1 逐条核对）：

1) 【收益结算顺序错误】V3 在换仓分支里先执行 `position = new_pos`，再执行
   `equity *= (1 + ret * position)`。卖出时 new_pos=0，导致当日持仓收益与已扣除的
   卖出成本都被乘以 0 静默丢失，净值虚高。
   V4：每日先用**昨日实际持仓**结算当日收益，再在成交时点（当日收盘）调仓，
   成交成本按成交金额（换手 |Δpos| × 净值）真实扣除。

2) 【买入成本近似不当】V3 把成本当收益比例（`ret = ret - cost`）混入味，且买卖不对称。
   V4：买卖双向统一按成交金额比例扣费（fee + slippage），费用无量纲、不随收益放大/缩小。

3) 【成本是否进入净值不可验证】V4 内置 `self_test()`：构造明确涨跌序列，
   断言每次买入/卖出成本与持仓收益都完整反映到期末净值（可复算）。

信号与成交假设（披露）：
  - 信号：T 日收盘计算 MA 多头排列，`shift(1)` 使其在 T+1 日生效，杜绝前视偏差；
  - 成交：T+1 日**收盘价**成交（仅有日收盘序列时可执行的最保守假设，fill="next_close"）；
  - 分红：价格序列为收盘价口径，未计分红再投资，策略与买入持有同为价格回报基准，
    真实总回报需另行加回分红（caveats 中显式标注）。

指标（GPT 回测验证标准）：CAGR / MDD / Sharpe / Sortino / Calmar / VaR95 / CVaR95 /
年度胜率 / 换手率 / 持仓暴露 / 期末净值，并输出样本内(70%) 与样本外(30%) 分段结果。

用法：
  python backtest.py output/price_history_515180.csv [--fee 0.001] [--slippage 0.001]
                      [--strict] [--json] [--oos 0.3] [--grid]
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
RISK_FREE_ANN = 0.02       # 无风险利率 2%（夏普/索提诺用）
FEE_DEFAULT = 0.001        # 单边手续费 0.1%
SLIP_DEFAULT = 0.001       # 单边滑点 0.1%
ANOMALY_LIMIT = 0.11       # |日涨跌幅| 超过 11% 视为数据异常
GRID = [(10, 30, 60), (20, 60, 120), (20, 40, 80), (30, 60, 120)]


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


def compute_signals(df: pd.DataFrame, fast: int = MA_FAST, mid: int = MA_MID,
                    slow: int = MA_SLOW) -> pd.DataFrame:
    """MA 均线 + 下一交易日生效的多头排列持仓信号（shift(1) 防前视）"""
    out = df.copy()
    out[f"ma{fast}"] = out["close"].rolling(fast).mean()
    out[f"ma{mid}"] = out["close"].rolling(mid).mean()
    out[f"ma{slow}"] = out["close"].rolling(slow).mean()
    cond = (out[f"ma{fast}"] > out[f"ma{mid}"]) & (out[f"ma{mid}"] > out[f"ma{slow}"])
    out["signal"] = cond.astype(int).shift(1).fillna(0).astype(int)
    return out


def detect_anomalous_returns(closes: np.ndarray, limit: float = ANOMALY_LIMIT) -> list:
    """
    复权断崖/数据错误检测：A股 ETF 单日涨跌幅理论上限约 ±11%，
    超过即视为数据异常（除权未复权 / 数据源错位 / 停牌复牌跳空）。返回 [(索引, 涨跌幅%)]。
    """
    anoms = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            r = closes[i] / closes[i - 1] - 1.0
            if abs(r) > limit:
                anoms.append((i, round(r * 100, 2)))
    return anoms


def _recovery(curve: np.ndarray) -> dict:
    """
    回撤持续时间与恢复时间（PRD 8.2 必含指标 Recovery Time）。
      mdd_days    ：从回撤前高点到最低点的交易日数
      recover_days：从最低点回到前高点所需交易日数（未恢复则 None）
    """
    n = len(curve)
    if n < 2:
        return {"mdd_days": 0, "recover_days": 0, "recovered": True}
    peak = np.maximum.accumulate(curve)
    dd = curve / peak - 1.0
    if float(np.min(dd)) >= 0:
        return {"mdd_days": 0, "recover_days": 0, "recovered": True}
    trough = int(np.argmin(dd))
    start = int(np.argmax(curve[:trough + 1]))
    rec = None
    for i in range(trough + 1, n):
        if curve[i] >= curve[start]:
            rec = i
            break
    return {"mdd_days": int(trough - start),
            "recover_days": None if rec is None else int(rec - trough),
            "recovered": rec is not None,
            "max_dd_pct": round(float(np.min(dd)) * 100, 2)}


def _perf(curve: np.ndarray, dates: pd.Series, rf: float = RISK_FREE_ANN) -> dict:
    """净值曲线指标：CAGR / MDD / Sharpe / Sortino / Calmar / VaR95 / CVaR95 / 年度胜率 / 恢复时间"""
    n = len(curve)
    if n < 2:
        return {}
    years = (pd.Timestamp(dates.iloc[-1]) - pd.Timestamp(dates.iloc[0])).days / 365.25
    if years <= 0:
        return {}
    cagr = float(curve[-1] ** (1.0 / years) - 1.0)
    peak = np.maximum.accumulate(curve)
    mdd = float(np.min(curve / peak - 1.0))
    dr = np.diff(curve) / curve[:-1]
    out = {"cagr": cagr, "mdd": mdd, "n": int(n), "years": round(years, 2)}
    if len(dr) > 1:
        vol = float(np.std(dr, ddof=1))
        mean = float(np.mean(dr))
        out["sharpe"] = float((mean - rf / 252) / vol * math.sqrt(252)) if vol > 0 else 0.0
        downside = dr[dr < 0]
        dvol = float(np.std(downside, ddof=1)) if len(downside) > 1 else 0.0
        out["sortino"] = float((mean - rf / 252) / dvol * math.sqrt(252)) if dvol > 0 else 0.0
        out["calmar"] = float(cagr / abs(mdd)) if mdd < 0 else 0.0
        var95 = float(np.percentile(dr, 5))
        tail = dr[dr <= var95]
        out["var95"] = round(var95 * 100, 3)
        out["cvar95"] = round(float(np.mean(tail)) * 100, 3) if len(tail) else round(var95 * 100, 3)
        out["daily_vol_ann"] = round(vol * math.sqrt(252) * 100, 2)
        # 年度胜率：按自然年聚合日收益
        yearly = pd.Series(dr, index=pd.to_datetime(dates.iloc[1:].values)).groupby(
            pd.to_datetime(dates.iloc[1:].values).year).apply(lambda x: float(np.prod(1.0 + x) - 1.0))
        out["annual_win_rate"] = round(float((yearly > 0).mean() * 100), 1) if len(yearly) else None
        out["annual_returns"] = {str(k): round(v * 100, 2) for k, v in yearly.items()}
    rec = _recovery(curve)
    out["recovery"] = rec
    out["recover_days"] = rec.get("recover_days")
    out["mdd_days"] = rec.get("mdd_days")
    return out


def _run_curve(closes: np.ndarray, signals: np.ndarray, fee: float, slippage: float) -> dict:
    """
    V4 核心净值引擎。

    每个交易日 i：
      1) 先用昨日持仓 pos 结算今日收益 ret（持仓期内收益不因调仓丢失）；
      2) 若今日目标仓位 target != pos：在今日收盘成交，按成交金额扣成本
         (fee + slippage) * |Δpos|（买卖双向同一口径，真实计入净值）；
      3) 持仓切换为 target，从次日起计收益。

    买入持有对照：首日建仓扣一次买入成本，期末按收盘价估值（不计清仓成本，与策略一致）。
    """
    n = len(closes)
    pos = 0
    eq = 1.0
    bh = 1.0 - (fee + slippage)      # 买入持有首日建仓成本
    eq_curve = np.empty(n)
    bh_curve = np.empty(n)
    eq_curve[0] = 1.0
    bh_curve[0] = 1.0
    trades = 0
    buy_trades = 0
    sell_trades = 0
    cost_total = 0.0
    for i in range(1, n):
        ret = closes[i] / closes[i - 1] - 1.0
        # 1) 旧持仓结算今日收益
        eq *= (1.0 + ret * pos)
        bh *= (1.0 + ret)
        # 2) 成交时点调仓 + 按成交金额扣成本
        target = int(signals[i])
        if target != pos:
            turn = abs(target - pos)
            cost = (fee + slippage) * turn
            cost_amount = eq * cost
            eq *= (1.0 - cost)
            cost_total += cost_amount
            trades += 1
            if target == 1:
                buy_trades += 1
            else:
                sell_trades += 1
            pos = target
        eq_curve[i] = eq
        bh_curve[i] = bh
    return {"eq_curve": eq_curve, "bh_curve": bh_curve, "trades": trades,
            "buy_trades": buy_trades, "sell_trades": sell_trades, "cost_total": cost_total}


def run_backtest(df: pd.DataFrame, fee: float = FEE_DEFAULT, slippage: float = SLIP_DEFAULT,
                 strict_anomalies: bool = False, oos_ratio: float = 0.3,
                 fast: int = MA_FAST, mid: int = MA_MID, slow: int = MA_SLOW,
                 deep: bool = False) -> dict:
    """
    执行回测。fee/slippage 均为单边比例，买卖双向按成交金额计入净值。
    oos_ratio: 样本外比例（默认 0.3，即前 70% 样本内、后 30% 样本外）。
    deep: 额外跑滚动样本外三段 + 参数敏感性 + 防前视校验（较慢，看板默认关闭）。
    """
    data = compute_signals(df, fast, mid, slow)
    closes = data["close"].to_numpy(dtype=float)
    signals = data["signal"].to_numpy(dtype=int)
    dates = data["date"]
    n = len(closes)
    if n < slow + 2:
        return {"ok": False, "error": f"样本不足（{n} 条，需 >{slow + 2}）"}

    anomalies = detect_anomalous_returns(closes)
    if anomalies and strict_anomalies:
        first = anomalies[0]
        return {"ok": False,
                "error": f"检测到 {len(anomalies)} 个 |日涨跌幅|>11% 的异常收益（首个 at 索引 {first[0]}，{first[1]:+.1f}%），"
                         f"疑似除权未复权或数据源错位，请先核对价格序列（--strict 模式）"}

    core = _run_curve(closes, signals, fee, slippage)
    eq_curve, bh_curve = core["eq_curve"], core["bh_curve"]

    perf_s = _perf(eq_curve, dates)
    perf_bh = _perf(bh_curve, dates)
    if not perf_s:
        return {"ok": False, "error": "时间跨度不足"}

    # 样本内 / 样本外分段（信号全长连续计算，仅分段评估净值，避免重算损失 warmup）
    split_idx = max(1, min(n - 1, int(round(n * (1.0 - oos_ratio))))
                    if oos_ratio > 0 else n - 1)
    seg = lambda curve, a, b: curve[a:b] / curve[a]

    in_s = _perf(seg(eq_curve, 0, split_idx), dates.iloc[0:split_idx]) if split_idx > 1 else {}
    in_bh = _perf(seg(bh_curve, 0, split_idx), dates.iloc[0:split_idx]) if split_idx > 1 else {}
    oos_s = _perf(seg(eq_curve, split_idx, n), dates.iloc[split_idx:n]) if split_idx < n - 1 else {}
    oos_bh = _perf(seg(bh_curve, split_idx, n), dates.iloc[split_idx:n]) if split_idx < n - 1 else {}

    deep_out = {}
    if deep:
        deep_out = {
            "rolling_oos": rolling_out_of_sample(df, fee, slippage, n_folds=3),
            "sensitivity": sensitivity(df, fee, slippage),
        }

    years = perf_s.get("years", 0) or 0
    exposure = float(np.mean(signals) * 100)
    turnover = core["trades"] / years if years > 0 else 0.0

    caveats = [
        "成交假设：信号次日收盘价成交（fill=next_close），非开盘价/VWAP，不含盘中滑点分布",
        "分红口径：价格为收盘价序列，未计分红再投资；真实总回报应高于本结果（策略与买入持有同为价格回报基准）",
        f"交易成本：买卖双向按成交金额各扣 {fee + slippage:.4f}（fee {fee} + slippage {slippage}），已真实进入净值",
        "样本内外划分：前 {:.0%} 样本内 / 后 {:.0%} 样本外（信号全长连续计算）".format(1 - oos_ratio, oos_ratio),
    ]

    return {
        "ok": True,
        "n": int(n),
        "years": round(years, 2),
        "params": {"fast": fast, "mid": mid, "slow": slow, "fee": fee, "slippage": slippage},
        "strategy_ann": round(perf_s.get("cagr", 0) * 100, 2),
        "buyhold_ann": round(perf_bh.get("cagr", 0) * 100, 2),
        "strategy_mdd": round(perf_s.get("mdd", 0) * 100, 2),
        "buyhold_mdd": round(perf_bh.get("mdd", 0) * 100, 2),
        "recovery": perf_s.get("recovery"),
        "lookahead": no_lookahead_check(df, fast, mid, slow),
        "sharpe": round(perf_s.get("sharpe", 0), 2),
        "sortino": round(perf_s.get("sortino", 0), 2),
        "calmar": round(perf_s.get("calmar", 0), 2),
        "var95": perf_s.get("var95"),
        "cvar95": perf_s.get("cvar95"),
        "annual_win_rate": perf_s.get("annual_win_rate"),
        "annual_returns": perf_s.get("annual_returns", {}),
        "exposure_pct": round(exposure, 1),
        "trades": int(core["trades"]),
        "buy_trades": int(core["buy_trades"]),
        "sell_trades": int(core["sell_trades"]),
        "cost_total_pct": round(core["cost_total"] * 100, 3),
        "turnover_per_year": round(turnover, 1),
        "final_equity": round(float(eq_curve[-1]), 4),
        "final_buyhold": round(float(bh_curve[-1]), 4),
        "in_sample": {"strategy_ann": round(in_s.get("cagr", 0) * 100, 2) if in_s else None,
                      "buyhold_ann": round(in_bh.get("cagr", 0) * 100, 2) if in_bh else None,
                      "strategy_mdd": round(in_s.get("mdd", 0) * 100, 2) if in_s else None,
                      "n": in_s.get("n")},
        "out_sample": {"strategy_ann": round(oos_s.get("cagr", 0) * 100, 2) if oos_s else None,
                       "buyhold_ann": round(oos_bh.get("cagr", 0) * 100, 2) if oos_bh else None,
                       "strategy_mdd": round(oos_s.get("mdd", 0) * 100, 2) if oos_s else None,
                       "sharpe": round(oos_s.get("sharpe", 0), 2) if oos_s else None,
                       "start": str(dates.iloc[split_idx].date()) if split_idx < n else None,
                       "n": oos_s.get("n")},
        "anomaly_count": len(anomalies),
        "anomalies": anomalies[:10],
        "caveats": caveats,
        "deep": deep_out,
    }


def no_lookahead_check(df: pd.DataFrame, fast: int = MA_FAST, mid: int = MA_MID,
                       slow: int = MA_SLOW) -> dict:
    """
    防未来函数校验（PRD 8.2 防作弊第 1 项）。
    独立实现一份「只用 i-1 及之前数据」的信号，与引擎实际使用的信号逐日比对；
    全部一致才算通过，任何不一致都说明信号里混入了当日或未来信息。
    """
    data = compute_signals(df, fast, mid, slow)
    closes = data["close"].to_numpy(dtype=float)
    sig = data["signal"].to_numpy(dtype=int)
    n = len(closes)
    if n <= max(fast, mid, slow) + 2:
        return {"ok": False, "checked": 0, "mismatches": 0,
                "reason": f"样本不足（需 >{max(fast, mid, slow) + 2} 条）"}
    bad = []
    checked = 0
    for i in range(max(fast, mid, slow) + 1, n):
        hist = closes[:i]          # 仅含 i-1 及之前
        f = float(np.mean(hist[-fast:]))
        m = float(np.mean(hist[-mid:]))
        s = float(np.mean(hist[-slow:]))
        expect = 1 if (f > m and m > s) else 0
        checked += 1
        if expect != int(sig[i]):
            bad.append(i)
    return {"ok": len(bad) == 0, "checked": checked, "mismatches": len(bad),
            "first_mismatch_index": bad[0] if bad else None,
            "method": "独立用 t-1 及之前收盘重算信号，与引擎 shift(1) 信号逐日比对"}


def rolling_out_of_sample(df: pd.DataFrame, fee: float = FEE_DEFAULT,
                          slippage: float = SLIP_DEFAULT, n_folds: int = 3,
                          min_train: int = 250) -> dict:
    """
    滚动样本外（PRD 8.2 防作弊第 3 项 + 第 8 章「滚动样本外」）。
    扩张窗口 walk-forward：每一折在训练段内用 Calmar 选参数，样本外只跑一次、不参与选参；
    共 n_folds（默认 3）段，每段从空仓起步（入场成本照扣），披露该保守假设。
    """
    n = len(df)
    if n < min_train + 20 * n_folds:
        return {"ok": False, "reason": f"样本 {n} 条，不足以做 {n_folds} 折滚动样本外（需 ≥{min_train + 20 * n_folds}）"}
    step = max(20, (n - min_train) // n_folds)
    folds = []
    for k in range(n_folds):
        tr_end = min(n - 1, min_train + k * step)
        te_end = min(n, tr_end + step)
        if te_end - tr_end < 20:
            break
        # 1) 训练段内选参（只看训练段）
        best = None
        for (f, m, s) in GRID:
            sub = df.iloc[:tr_end].reset_index(drop=True)
            sg = compute_signals(sub, f, m, s)["signal"].to_numpy(dtype=int)
            core = _run_curve(sub["close"].to_numpy(dtype=float), sg, fee, slippage)
            p = _perf(core["eq_curve"], sub["date"])
            if not p:
                continue
            score = float(p.get("calmar") or -999)
            if best is None or score > best["score"]:
                best = {"params": (f, m, s), "score": score,
                        "train_cagr": round(p.get("cagr", 0) * 100, 2),
                        "train_mdd": round(p.get("mdd", 0) * 100, 2)}
        if best is None:
            continue
        # 2) 用选定的参数在全序列上算信号（滚动均线本身不含未来信息），只评估样本外区间
        f, m, s = best["params"]
        full = compute_signals(df, f, m, s)
        sg = full["signal"].to_numpy(dtype=int)[tr_end:te_end]
        cl = full["close"].to_numpy(dtype=float)[tr_end:te_end]
        core = _run_curve(cl, sg, fee, slippage)
        p = _perf(core["eq_curve"], full["date"].iloc[tr_end:te_end])
        folds.append({
            "fold": k + 1,
            "train_start": str(df["date"].iloc[0].date()),
            "train_end": str(df["date"].iloc[tr_end - 1].date()),
            "test_start": str(df["date"].iloc[tr_end].date()),
            "test_end": str(df["date"].iloc[te_end - 1].date()),
            "params": f"{f}/{m}/{s}",
            "train_cagr": best["train_cagr"], "train_mdd": best["train_mdd"],
            "test_cagr": round(p.get("cagr", 0) * 100, 2) if p else None,
            "test_mdd": round(p.get("mdd", 0) * 100, 2) if p else None,
            "test_sharpe": round(p.get("sharpe", 0), 2) if p else None,
            "n_test": p.get("n") if p else None,
        })
    if not folds:
        return {"ok": False, "reason": "无可评估的样本外区间"}
    pos = sum(1 for f in folds if (f.get("test_cagr") or 0) > 0)
    return {
        "ok": True, "folds": folds, "n_folds": len(folds),
        "positive_folds": pos,
        "note": f"扩张窗口 walk-forward；每折样本外从空仓起步（入场成本照扣），"
                f"训练段只用 Calmar 选参，样本外不参与任何选择",
        "caveat": "样本外区间较短，单折结果噪声大，应看折间一致性而非单折最优",
    }


def sensitivity(df: pd.DataFrame, fee: float, slippage: float) -> dict:
    """
    参数敏感性（PRD 8.2 防作弊第 3 项）：同一策略在 4 组均线参数下的稳健性。
    关注离散度与「跑赢买入持有」的比例，而不是挑最好的一组。
    """
    rows = []
    for (f, m, s) in GRID:
        r = run_backtest(df, fee=fee, slippage=slippage, fast=f, mid=m, slow=s)
        if r.get("ok"):
            rows.append({"params": f"{f}/{m}/{s}", "ann": r["strategy_ann"],
                         "mdd": r["strategy_mdd"], "sharpe": r["sharpe"],
                         "trades": r["trades"],
                         "vs_bh": round(r["strategy_ann"] - r["buyhold_ann"], 2)})
    if not rows:
        return {"ok": False, "reason": "参数网格全部失败"}
    anns = [r["ann"] for r in rows]
    mdds = [r["mdd"] for r in rows]
    mean = float(np.mean(anns))
    std = float(np.std(anns, ddof=1)) if len(anns) > 1 else 0.0
    win = sum(1 for r in rows if r["vs_bh"] > 0) / len(rows) * 100
    # 稳健性判定：年化离散度小且多数参数组跑赢买入持有
    verdict = "稳健" if (std <= max(3.0, abs(mean) * 0.5) and win >= 50) else "不稳定"
    return {"ok": True, "grid": rows,
            "ann_mean": round(mean, 2), "ann_std": round(std, 2),
            "ann_min": round(min(anns), 2), "ann_max": round(max(anns), 2),
            "mdd_worst": round(min(mdds), 2), "beat_bh_pct": round(win, 1),
            "verdict": verdict,
            "note": "参数敏感性用于判断结论是否依赖特定参数，不作为挑参依据"}


def param_grid(df: pd.DataFrame, fee: float, slippage: float) -> list:
    """参数稳定性检查：不同均线组合下的年化与回撤（GPT 回测验证标准：参数稳定性）"""
    out = []
    for (f, m, s) in GRID:
        r = run_backtest(df, fee=fee, slippage=slippage, fast=f, mid=m, slow=s)
        if r.get("ok"):
            out.append({"params": f"{f}/{m}/{s}", "ann": r["strategy_ann"], "mdd": r["strategy_mdd"],
                        "trades": r["trades"], "vs_bh": round(r["strategy_ann"] - r["buyhold_ann"], 2)})
        else:
            out.append({"params": f"{f}/{m}/{s}", "error": r.get("error")})
    return out


def self_test() -> list:
    """
    确定性测试（GPT 要求：构造明确涨跌与买卖信号，验证每次买卖与成本都正确反映到净值）。
    场景：价格 1.00 → 1.00 → 1.10 → 1.00，信号在第 2 日买入、第 4 日卖出；fee=slip=0.001。
    期望净值推导：
      t1 收益 0，买入扣 0.002 → 0.998
      t2 持仓收益 +10% → 0.998 × 1.1 = 1.0978
      t3 持仓收益 1.0/1.1-1 = -9.0909% → 1.0978 × 0.909091 = 0.998
      t3 收盘卖出扣 0.002 → 0.998 × 0.998 = 0.996004
      期末净值应为 0.996004（两次成本各一次，持仓收益完整结算）
    """
    results = []
    closes = [1.00, 1.00, 1.10, 1.00, 1.00, 1.00]
    dates = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    df = pd.DataFrame({"date": dates, "close": closes})
    # 直接给定信号（绕过均线 warmup）：[0,1,1,0,0,0]
    sig = np.array([0, 1, 1, 0, 0, 0])
    core = _run_curve(np.array(closes, dtype=float), sig, 0.001, 0.001)
    expected = 0.998 * 1.1 * (1.0 / 1.1) * 0.998
    got = float(core["eq_curve"][-1])
    ok = abs(got - expected) < 1e-9
    results.append({"case": "买入+持仓收益+卖出成本完整结算", "expected": round(expected, 6),
                    "got": round(got, 6), "pass": bool(ok),
                    "detail": f"trades={core['trades']} buy={core['buy_trades']} sell={core['sell_trades']} cost_pct={core['cost_total']*100:.3f}"})
    # 场景2：全程空仓 → 净值恒为 1（无成本）
    core2 = _run_curve(np.array(closes, dtype=float), np.zeros(len(closes), dtype=int), 0.001, 0.001)
    results.append({"case": "全程空仓净值不变", "expected": 1.0, "got": round(float(core2["eq_curve"][-1]), 6),
                    "pass": abs(float(core2["eq_curve"][-1]) - 1.0) < 1e-9, "detail": f"trades={core2['trades']}"})
    # 场景3：全程满仓（首日后）→ 买入一次成本，收益按 1.1×0.909 结算
    sig3 = np.array([0, 1, 1, 1, 1, 1])
    core3 = _run_curve(np.array(closes, dtype=float), sig3, 0.001, 0.001)
    exp3 = 0.998 * (1.1 / 1.0) * (1.0 / 1.1) * 1.0 * 1.0
    results.append({"case": "买入一次后满仓、收益连续结算", "expected": round(exp3, 6),
                    "got": round(float(core3["eq_curve"][-1]), 6),
                    "pass": abs(float(core3["eq_curve"][-1]) - exp3) < 1e-9,
                    "detail": f"trades={core3['trades']}（应=1，仅买入）"})
    # 场景4：防前视校验 —— 用真实行情序列独立重算信号并逐日比对
    n = 400
    rng = np.random.default_rng(11)
    px = 1.0 + np.cumsum(rng.normal(0.0005, 0.01, n))
    px = np.maximum(px, 0.1)
    df4 = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=n, freq="B"), "close": px})
    lk = no_lookahead_check(df4)
    results.append({"case": "防前视校验（无未来函数）", "expected": "0 处不一致",
                    "got": f"{lk.get('mismatches')} 处",
                    "pass": bool(lk.get("ok")),
                    "detail": f"checked={lk.get('checked')} method={lk.get('method')}"})
    # 场景5：恢复时间计算
    curve = np.array([1.0, 1.2, 0.9, 1.1, 1.25])
    rec = _recovery(curve)
    ok5 = rec["mdd_days"] == 1 and rec["recover_days"] == 2
    results.append({"case": "回撤持续与恢复时间", "expected": "持续1天/恢复2天",
                    "got": f"持续{rec['mdd_days']}天/恢复{rec['recover_days']}天",
                    "pass": bool(ok5), "detail": f"max_dd={rec.get('max_dd_pct')}%"})
    return results


def summarize(r: dict, params: dict | None = None) -> str:
    if not r.get("ok"):
        return f"回测不可用：{r.get('error')}"
    p = r.get("params", {})
    lines = [
        f"样本 {r['n']} 个交易日（约 {r['years']} 年）｜均线 {p.get('fast')}/{p.get('mid')}/{p.get('slow')}"
        f"｜单边成本 {p.get('fee', 0) + p.get('slippage', 0):.4f}（买卖双向按成交金额计入净值）",
        f"策略年化 {r['strategy_ann']}% vs 买入持有 {r['buyhold_ann']}%",
        f"策略最大回撤 {r['strategy_mdd']}% vs 买入持有 {r['buyhold_mdd']}%",
        f"Sharpe {r['sharpe']} ｜ Sortino {r['sortino']} ｜ Calmar {r['calmar']}",
        f"CVaR95 {r['cvar95']}%/日 ｜ 年度胜率 {r['annual_win_rate']}% ｜ 持仓暴露 {r['exposure_pct']}%",
        f"换手 {r['turnover_per_year']} 次/年（买入 {r['buy_trades']} 次 / 卖出 {r['sell_trades']} 次，累计成本 {r['cost_total_pct']}%）",
        f"期末净值：策略 {r['final_equity']} / 买入持有 {r['final_buyhold']}",
        f"样本内(前) 年化 {r['in_sample']['strategy_ann']}% / MDD {r['in_sample']['strategy_mdd']}% ｜ "
        f"样本外 年化 {r['out_sample']['strategy_ann']}% / MDD {r['out_sample']['strategy_mdd']}% / Sharpe {r['out_sample']['sharpe']}",
    ]
    if r.get("recovery"):
        rec = r["recovery"]
        lines.append(f"回撤持续 {rec.get('mdd_days')} 个交易日 ｜ 恢复 "
                     f"{rec.get('recover_days') if rec.get('recover_days') is not None else '未恢复'} 个交易日")
    lk = r.get("lookahead") or {}
    if lk:
        lines.append("防前视校验：" + ("通过" if lk.get("ok") else "未通过") +
                     f"（比对 {lk.get('checked')} 日，不一致 {lk.get('mismatches')} 处）")
    deep = r.get("deep") or {}
    ro = deep.get("rolling_oos") or {}
    if ro.get("ok"):
        for f in ro["folds"]:
            lines.append(f"滚动样本外第 {f['fold']} 折：训练至 {f['train_end']}（参数 {f['params']}）"
                         f" → 样本外 {f['test_start']}~{f['test_end']} 年化 {f['test_cagr']}% / MDD {f['test_mdd']}%")
        lines.append(f"样本外为正的折数：{ro['positive_folds']}/{ro['n_folds']}")
    se = deep.get("sensitivity") or {}
    if se.get("ok"):
        lines.append(f"参数敏感性：年化 {se['ann_mean']}%（区间 {se['ann_min']}~{se['ann_max']}%，"
                     f"标准差 {se['ann_std']}pp）｜ 跑赢买入持有 {se['beat_bh_pct']}% ｜ 判定 {se['verdict']}")
    if r.get("anomaly_count"):
        lines.append(f"⚠ 数据健康检查：检出 {r['anomaly_count']} 个 |日涨跌幅|>11% 异常收益（可能为除权未复权/数据源错位，示例：{r['anomalies'][:3]}）")
    lines.append("披露：" + "；".join(r.get("caveats", [])))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="V4 回测引擎：MA 多头排列趋势策略（修复换仓结算与成本计入）")
    ap.add_argument("csv", nargs="?", help="价格 CSV 路径（date, close）；--selftest 时可省略")
    ap.add_argument("--fee", type=float, default=FEE_DEFAULT, help="单边手续费比例（默认 0.001=万10）")
    ap.add_argument("--slippage", type=float, default=SLIP_DEFAULT, help="单边滑点比例（默认 0.001）")
    ap.add_argument("--strict", action="store_true", help="存在 |日涨跌幅|>11% 异常收益时直接报错（默认仅警告）")
    ap.add_argument("--oos", type=float, default=0.3, help="样本外比例（默认 0.3）")
    ap.add_argument("--grid", action="store_true", help="参数稳定性扫描（多组均线组合）")
    ap.add_argument("--deep", action="store_true", help="附加滚动样本外三段 + 参数敏感性（较慢）")
    ap.add_argument("--selftest", action="store_true", help="运行确定性自测（验证成本与收益计入净值）")
    ap.add_argument("--json", action="store_true", help="额外输出 JSON 结果")
    args = ap.parse_args(argv)

    if args.selftest:
        tests = self_test()
        all_ok = all(t["pass"] for t in tests)
        for t in tests:
            print(("PASS " if t["pass"] else "FAIL ") + t["case"] +
                  f" ｜ expected {t['expected']} got {t['got']} ｜ {t['detail']}")
        print("自测结果：" + ("全部通过" if all_ok else "存在失败项"))
        if args.json:
            print(json.dumps(tests, ensure_ascii=False))
        return 0 if all_ok else 1

    if not args.csv:
        print("缺少价格 CSV 路径（或使用 --selftest）")
        return 1
    try:
        df = load_prices(args.csv)
    except Exception as e:
        print(f"读取失败: {e}")
        return 1
    r = run_backtest(df, fee=args.fee, slippage=args.slippage, strict_anomalies=args.strict,
                     oos_ratio=args.oos, deep=args.deep)
    print(summarize(r))
    if args.grid:
        print("参数稳定性扫描：")
        for g in param_grid(df, args.fee, args.slippage):
            if g.get("error"):
                print(f"  {g['params']}: {g['error']}")
            else:
                print(f"  {g['params']}: 年化 {g['ann']}% / MDD {g['mdd']}% / 换手 {g['trades']} 次 / vs 买入持有 {g['vs_bh']}pp")
    if args.json:
        print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
