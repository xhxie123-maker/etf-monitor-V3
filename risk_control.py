# -*- coding: utf-8 -*-
"""
V5.0 四级风险控制（risk_control）
=================================
严格对应 Smart ETF Quant V4.0 PRD 第 7 章：

  一级 单 ETF 风险：最大持仓 / 可接受回撤 / 波动上限 / 最低流动性 / 折溢价限制
  二级 组合风险  ：σ_p=√(wᵗΣw)、VaR、CVaR、最大单一暴露、行业/类别集中、相关性集中
  三级 动态仓位  ：低/中/高/极端风险四状态 → 风险预算乘数（不是机械百分比）
  四级 熔断      ：黄/橙/红三级；红色只禁止「扩大风险的新增建议」+ 暂停策略升级 + 出重评报告
                   —— 熔断 ≠ 无条件清仓（PRD 7.3 明确）

另外实现压力测试的集中度口径与风险贡献，并把结果整理成 risk_snapshot（PRD 表20）。

所有输出均为量化参考，不构成投资建议。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

MODEL_VERSION = "risk-v5-2026-11-07"

# 动态仓位四状态（PRD 7.2 / 6.4）
RISK_STATES = {
    "low":    {"label": "低风险", "multiplier": 1.00, "desc": "趋势与波动均在正常区间，按战略权重执行"},
    "mid":    {"label": "中风险", "multiplier": 0.75, "desc": "波动抬升或趋势分化，新增仓位打折"},
    "high":   {"label": "高风险", "multiplier": 0.40, "desc": "主要指数趋势破坏，只保留核心仓位"},
    "extreme": {"label": "极端风险", "multiplier": 0.20, "desc": "流动性/相关性异常，仅保留现金与防御资产"},
}


def _num(x):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def portfolio_stats(returns: pd.DataFrame, weights: dict, rf: float = 0.02) -> dict:
    """
    组合级风险指标。returns 为各标的日收益 DataFrame（列=code）。
    σ_p = √(wᵗΣw)；同时给出 VaR95 / CVaR95 / MDD / 集中度 / 相关性 / 风险贡献。
    """
    codes = [c for c in weights if c in returns.columns]
    if len(codes) < 1:
        return {"ok": False, "reason": "无可用收益序列"}
    w = np.array([float(weights[c]) for c in codes], dtype=float)
    if w.sum() <= 0:
        return {"ok": False, "reason": "权重合计为 0"}
    w = w / w.sum()
    r = returns[codes].dropna()
    if len(r) < 60:
        return {"ok": False, "reason": f"共同交易日仅 {len(r)} 天，不足以计算组合风险"}
    arr = r.values
    port = arr @ w
    vol = float(np.std(port, ddof=1)) * math.sqrt(252) * 100
    eq = np.cumprod(1.0 + port)
    peak = np.maximum.accumulate(eq)
    mdd = float(np.min(eq / peak - 1.0)) * 100
    var95 = float(np.percentile(port, 5)) * 100
    tail = port[port <= np.percentile(port, 5)]
    cvar95 = float(np.mean(tail)) * 100 if len(tail) else var95
    downside = port[port < 0]
    dvol = float(np.std(downside, ddof=1)) * math.sqrt(252) * 100 if len(downside) > 1 else vol
    hhi = float(np.sum(w ** 2)) * 10000
    cov = np.cov(arr, rowvar=False) if arr.shape[1] > 1 else np.array([[np.var(arr[:, 0])]])
    port_var = float(w @ cov @ w) if np.ndim(cov) == 2 else float(cov) * float(w[0] ** 2)
    rc = (w * (cov @ w)) / port_var * 100 if port_var > 0 else np.zeros_like(w)
    corr = r.corr()
    off = corr.values[np.triu_indices_from(corr.values, k=1)] if len(codes) > 1 else np.array([])
    max_corr = float(np.nanmax(off)) if len(off) else None
    avg_corr = float(np.nanmean(off)) if len(off) else None
    # 相关性集中度：高相关对（>0.8）占全部对数比例
    high_pairs = [(codes[i], codes[j], round(float(corr.iloc[i, j]), 3))
                  for i in range(len(codes)) for j in range(i + 1, len(codes))
                  if pd.notna(corr.iloc[i, j]) and float(corr.iloc[i, j]) >= 0.8]
    # 回撤恢复时间
    recovery_days = _recovery_days(eq)

    return {
        "ok": True, "n_days": int(len(r)),
        "ann_vol_pct": round(vol, 2),
        "downside_vol_pct": round(dvol, 2),
        "mdd_pct": round(mdd, 2),
        "var95_daily_pct": round(var95, 3),
        "cvar95_daily_pct": round(cvar95, 3),
        "sharpe": round((float(np.mean(port)) - rf / 252) / float(np.std(port, ddof=1)) * math.sqrt(252), 2)
                    if float(np.std(port, ddof=1)) > 0 else None,
        "calmar": round((float(eq[-1] ** (252.0 / len(eq)) - 1) * 100) / abs(mdd), 2) if mdd < 0 else None,
        "hhi": round(hhi, 0),
        "single_max_w_pct": round(float(np.max(w)) * 100, 2),
        "max_corr": max_corr, "avg_corr": avg_corr,
        "high_corr_pairs": high_pairs[:6],
        "recovery_days": recovery_days,
        "risk_contrib_pct": {codes[i]: round(float(rc[i]), 2) for i in range(len(codes))},
        "weights_pct": {codes[i]: round(float(w[i]) * 100, 2) for i in range(len(codes))},
        "model_version": MODEL_VERSION,
    }


def _recovery_days(eq: np.ndarray) -> dict:
    """最大回撤的持续天数与恢复天数（PRD 8.2 Recovery Time）。"""
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0
    if len(dd) == 0 or dd.min() >= 0:
        return {"mdd_days": 0, "recover_days": 0, "recovered": True}
    trough = int(np.argmin(dd))
    start = int(np.argmax(eq[:trough + 1]))
    recovered_idx = None
    for i in range(trough + 1, len(eq)):
        if eq[i] >= eq[start]:
            recovered_idx = i
            break
    return {
        "mdd_days": int(trough - start),
        "recover_days": None if recovered_idx is None else int(recovered_idx - trough),
        "recovered": recovered_idx is not None,
    }


def risk_state_of(regime: str, stats: dict, thresholds: dict | None = None) -> dict:
    """
    三级动态仓位：把市场状态（正常/谨慎/高风险）与组合实测风险合成四状态。
    """
    th = thresholds or {}
    vol = _num(stats.get("ann_vol_pct"))
    dd = _num(stats.get("mdd_pct"))
    reg = (regime or "正常").strip()
    state = "low"
    reasons = []
    if reg == "高风险":
        state = "high"; reasons.append("市场状态=高风险")
    elif reg == "谨慎":
        state = "mid"; reasons.append("市场状态=谨慎")
    if vol is not None:
        if vol >= float(th.get("extreme_vol_pct", 25)):
            state = "extreme"; reasons.append(f"组合年化波动 {vol:.1f}% ≥ 极端阈值")
        elif vol >= float(th.get("high_vol_pct", 20)):
            state = max(state, "high", key=lambda s: ["low", "mid", "high", "extreme"].index(s))
            reasons.append(f"组合年化波动 {vol:.1f}% 偏高")
        elif vol >= float(th.get("mid_vol_pct", 13)):
            state = max(state, "mid", key=lambda s: ["low", "mid", "high", "extreme"].index(s))
            reasons.append(f"组合年化波动 {vol:.1f}% 抬升")
    if dd is not None:
        if dd <= float(th.get("extreme_dd_pct", -25)):
            state = "extreme"; reasons.append(f"组合回撤 {dd:.1f}% 达极端阈值")
        elif dd <= float(th.get("high_dd_pct", -15)):
            state = max(state, "high", key=lambda s: ["low", "mid", "high", "extreme"].index(s))
            reasons.append(f"组合回撤 {dd:.1f}% 偏大")
    info = RISK_STATES[state]
    return {"state": state, "label": info["label"], "multiplier": info["multiplier"],
            "desc": info["desc"], "reasons": reasons or ["各风险指标处于正常区间"],
            "model_version": MODEL_VERSION}


def single_etf_checks(items: list, weights: dict, metrics: dict, limits: dict | None = None) -> dict:
    """
    一级：单 ETF 风险检查。
    metrics[code] 可含 vol20_pct / dd120_pct / amount20_wan / premium_pct / quality(合格/观察/淘汰)
    """
    lim = limits or {}
    max_w = float(lim.get("single_etf_max_w", 35))
    max_vol = float(lim.get("single_vol_cap_pct", 30))
    max_dd = float(lim.get("single_dd_cap_pct", -25))
    min_amount = float(lim.get("min_amount20_wan", 2000))
    max_premium = float(lim.get("max_premium_pct", 1.0))
    flags = []
    for it in items:
        code, name = it["code"], it.get("name", it["code"])
        w = float(weights.get(code, 0) or 0)
        m = metrics.get(code, {}) or {}
        if w > max_w + 0.01:
            flags.append({"level": "block", "code": code, "label": f"{name} 目标权重 {w:.1f}% 超单只上限 {max_w:.0f}%"})
        vol = _num(m.get("vol20_pct"))
        if vol is not None and vol > max_vol:
            flags.append({"level": "warn", "code": code, "label": f"{name} 20日年化波动 {vol:.1f}% 超上限 {max_vol:.0f}%"})
        dd = _num(m.get("dd120_pct"))
        if dd is not None and dd < max_dd:
            flags.append({"level": "warn", "code": code, "label": f"{name} 距120日高点回撤 {dd:.1f}% 超可接受回撤 {max_dd:.0f}%"})
        amt = _num(m.get("amount20_wan"))
        if amt is not None and amt < min_amount:
            flags.append({"level": "block", "code": code, "label": f"{name} 20日均成交额 {amt:,.0f} 万元 低于流动性下限 {min_amount:,.0f} 万元"})
        pm = _num(m.get("premium_pct"))
        if pm is not None and abs(pm) > max_premium:
            flags.append({"level": "warn", "code": code, "label": f"{name} 折溢价 {pm:+.2f}% 超限制 ±{max_premium:.1f}%"})
        q = m.get("quality")
        if q == "淘汰":
            flags.append({"level": "block", "code": code, "label": f"{name} 产品质量判定为淘汰，不得纳入配置"})
    return {"ok": len([f for f in flags if f["level"] == "block"]) == 0,
            "flags": flags, "n_block": sum(1 for f in flags if f["level"] == "block"),
            "n_warn": sum(1 for f in flags if f["level"] == "warn")}


def portfolio_checks(stats: dict, items: list, weights: dict, limits: dict | None = None) -> dict:
    """二级：组合风险检查（集中度 / 相关性 / 类别 / 风险贡献）。"""
    lim = limits or {}
    flags = []
    if not stats.get("ok"):
        return {"ok": False, "flags": [{"level": "warn", "label": stats.get("reason", "组合风险不可计算")}]}
    max_vol = float(lim.get("portfolio_vol_cap_pct", 15))
    max_dd = float(lim.get("portfolio_dd_hard_pct", -25))
    max_w = float(lim.get("single_etf_max_w", 35))
    max_rc = float(lim.get("single_asset_risk_contribution_cap_pct", 40))
    max_avg_corr = float(lim.get("avg_corr_cap", 0.75))
    class_cap = lim.get("asset_class_max_w", {}) or {}

    vol = _num(stats.get("ann_vol_pct"))
    if vol is not None and vol > max_vol:
        flags.append({"level": "warn", "label": f"组合年化波动 {vol:.2f}% 超目标 {max_vol:.0f}%"})
    dd = _num(stats.get("mdd_pct"))
    if dd is not None and dd < max_dd:
        flags.append({"level": "block", "label": f"组合历史最大回撤 {dd:.2f}% 超硬阈值 {max_dd:.0f}%"})
    sw = _num(stats.get("single_max_w_pct"))
    if sw is not None and sw > max_w:
        flags.append({"level": "block", "label": f"最大单一暴露 {sw:.1f}% 超上限 {max_w:.0f}%"})
    for code, rc in (stats.get("risk_contrib_pct") or {}).items():
        if rc is not None and rc > max_rc:
            flags.append({"level": "warn", "label": f"{code} 风险贡献 {rc:.1f}% 超上限 {max_rc:.0f}%"})
    ac = _num(stats.get("avg_corr"))
    if ac is not None and ac > max_avg_corr:
        flags.append({"level": "warn", "label": f"平均相关性 {ac:.2f} 偏高（上限 {max_avg_corr}），分散效果下降"})
    for cls, cap in class_cap.items():
        cur = sum(float(weights.get(it["code"], 0) or 0) for it in items if it.get("asset_class") == cls)
        if cur > float(cap) + 0.01:
            flags.append({"level": "block", "label": f"资产类别 {cls} 合计 {cur:.1f}% 超上限 {cap}%"})
    return {"ok": len([f for f in flags if f["level"] == "block"]) == 0,
            "flags": flags, "n_block": sum(1 for f in flags if f["level"] == "block"),
            "n_warn": sum(1 for f in flags if f["level"] == "warn")}


def circuit_breaker(dd_pct, vol_pct, thresholds: dict | None = None) -> dict:
    """
    四级：熔断（黄/橙/红）。
    红色：禁止生成扩大风险的新增仓位建议、暂停策略升级、生成组合重评报告；
          不是无条件清仓，须提示流动性/跳空/停牌成交风险。
    """
    th = thresholds or {}
    y_dd = float(th.get("yellow_dd_pct", -8))
    o_dd = float(th.get("orange_dd_pct", -15))
    r_dd = float(th.get("red_dd_pct", -25))
    y_vol = float(th.get("yellow_vol_pct", 12))
    o_vol = float(th.get("orange_vol_pct", 18))
    r_vol = float(th.get("red_vol_pct", 25))
    dd = _num(dd_pct)
    vol = _num(vol_pct)
    level, why = "green", []
    if dd is not None:
        if dd <= r_dd:
            level = "red"; why.append(f"组合回撤 {dd:.1f}% ≤ 红色阈值 {r_dd:.0f}%")
        elif dd <= o_dd and level != "red":
            level = "orange"; why.append(f"组合回撤 {dd:.1f}% ≤ 橙色阈值 {o_dd:.0f}%")
        elif dd <= y_dd and level == "green":
            level = "yellow"; why.append(f"组合回撤 {dd:.1f}% ≤ 黄色阈值 {y_dd:.0f}%")
    if vol is not None:
        if vol >= r_vol:
            level = "red"; why.append(f"组合年化波动 {vol:.1f}% ≥ 红色阈值 {r_vol:.0f}%")
        elif vol >= o_vol and level in ("green", "yellow"):
            level = "orange"; why.append(f"组合年化波动 {vol:.1f}% ≥ 橙色阈值 {o_vol:.0f}%")
        elif vol >= y_vol and level == "green":
            level = "yellow"; why.append(f"组合年化波动 {vol:.1f}% ≥ 黄色阈值 {y_vol:.0f}%")
    actions = {
        "green": {"label": "正常", "allow_add": True, "half_add": False,
                  "actions": ["按战略权重与信号正常执行"]},
        "yellow": {"label": "黄色预警", "allow_add": True, "half_add": True,
                   "actions": ["记录预警原因与数据日期", "新增仓位分批减半", "复核高波动与高相关品种"]},
        "orange": {"label": "橙色预警", "allow_add": True, "half_add": True,
                   "actions": ["新增仓位减半并优先核心资产", "暂停卫星品种加仓", "准备再平衡评估"]},
        "red": {"label": "红色熔断", "allow_add": False, "half_add": False,
                "actions": ["禁止生成扩大风险的新增仓位建议", "暂停策略升级", "生成组合重新评估报告",
                            "提示人工判断；熔断≠无条件清仓，清仓需考虑流动性、跳空与停牌无法成交的风险"]},
    }[level]
    return {"level": level, "label": actions["label"], "allow_add": actions["allow_add"],
            "half_add": actions["half_add"], "actions": actions["actions"],
            "reasons": why or ["未触发任何熔断阈值"], "model_version": MODEL_VERSION}


def evaluate(returns: pd.DataFrame, weights: dict, items: list, class_by_code: dict,
             regime: str = "正常", metrics: dict | None = None,
             limits: dict | None = None, breaker_th: dict | None = None,
             trade_date: str | None = None) -> dict:
    """汇总四级风控，输出 risk_snapshot（PRD 表20 字段）。"""
    stats = portfolio_stats(returns, weights)
    l1 = single_etf_checks(items, weights, metrics or {}, limits)
    l2 = portfolio_checks(stats, items, weights, limits)
    st = risk_state_of(regime, stats, breaker_th)
    cb = circuit_breaker(stats.get("mdd_pct"), stats.get("ann_vol_pct"), breaker_th)
    warnings = []
    for f in (l1.get("flags") or []) + (l2.get("flags") or []):
        warnings.append({"level": f.get("level"), "label": f.get("label")})
    for r in cb.get("reasons") if cb["level"] != "green" else []:
        warnings.append({"level": "warn", "label": "熔断：" + r})
    overall = "extreme" if cb["level"] == "red" else st["state"]
    return {
        "ok": stats.get("ok", False),
        "trade_date": trade_date,
        "volatility_pct": stats.get("ann_vol_pct"),
        "max_drawdown_pct": stats.get("mdd_pct"),
        "var_95_daily_pct": stats.get("var95_daily_pct"),
        "cvar_95_daily_pct": stats.get("cvar95_daily_pct"),
        "concentration": {"hhi": stats.get("hhi"), "single_max_w_pct": stats.get("single_max_w_pct"),
                          "class_weights_pct": {cls: round(sum(float(weights.get(it["code"], 0) or 0)
                                                               for it in items
                                                               if it.get("asset_class") == cls), 2)
                                                for cls in sorted(set(class_by_code.values()))}},
        "correlation_risk": {"max_corr": stats.get("max_corr"), "avg_corr": stats.get("avg_corr"),
                             "high_corr_pairs": stats.get("high_corr_pairs")},
        "risk_level": {"state": st["state"], "label": st["label"],
                       "multiplier": st["multiplier"], "reasons": st["reasons"]},
        "circuit_breaker": cb,
        "single_checks": l1,
        "portfolio_checks": l2,
        "stats": stats,
        "warnings": warnings[:12],
        "overall_state": overall,
        "model_version": MODEL_VERSION,
        "note": "风险等级用于调整风险预算，不直接决定买卖；熔断不自动清仓",
    }


def self_test() -> list:
    fails = []
    rng = np.random.default_rng(7)
    n = 400
    rets = pd.DataFrame({
        "A": rng.normal(0.0004, 0.015, n),
        "B": rng.normal(0.0003, 0.011, n),
        "C": rng.normal(0.0001, 0.002, n),
    })
    w = {"A": 40, "B": 30, "C": 30}
    s = portfolio_stats(rets, w)
    if not s.get("ok"):
        fails.append("组合统计失败")
    if not (0 < s["ann_vol_pct"] < 100):
        fails.append("组合波动率异常")
    if abs(sum(s["risk_contrib_pct"].values()) - 100) > 5:
        fails.append("风险贡献合计应约 100%")
    if s["hhi"] <= 0:
        fails.append("HHI 计算异常")
    # 熔断分级
    if circuit_breaker(-3, 8)["level"] != "green":
        fails.append("温和回撤不应触发熔断")
    if circuit_breaker(-18, 10)["level"] != "orange":
        fails.append("-18% 回撤应触发橙色")
    if circuit_breaker(-30, 10)["level"] != "red":
        fails.append("-30% 回撤应触发红色")
    if circuit_breaker(-5, 26)["level"] != "red":
        fails.append("波动 26% 应触发红色")
    # 红色禁止新增但不清仓
    cb = circuit_breaker(-30, 10)
    if cb["allow_add"] is not False:
        fails.append("红色熔断必须禁止新增")
    if "清仓" not in " ".join(cb["actions"]):
        fails.append("红色熔断应提示熔断≠无条件清仓")
    # 单 ETF 超限
    l1 = single_etf_checks([{"code": "A", "name": "甲"}, {"code": "C", "name": "丙"}],
                           {"A": 45, "C": 55}, {"A": {"vol20_pct": 20}, "C": {"vol20_pct": 5}},
                           {"single_etf_max_w": 35})
    if l1["ok"]:
        fails.append("单只 55% 应触发超限阻断")
    # 集中度
    l2 = portfolio_checks(s, [{"code": "A", "asset_class": "equity"},
                              {"code": "B", "asset_class": "equity"},
                              {"code": "C", "asset_class": "bond"}],
                          w, {"single_etf_max_w": 35, "asset_class_max_w": {"equity": 60}})
    if l2["ok"]:
        fails.append("权益合计 70% 应触发类别超限")
    return fails


if __name__ == "__main__":
    import sys
    if "selftest" in sys.argv:
        f = self_test()
        print("risk_control self_test:", "PASS" if not f else "FAIL")
        for x in f:
            print(" -", x)
        sys.exit(0 if not f else 1)
    print("module ok")
