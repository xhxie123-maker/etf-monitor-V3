# -*- coding: utf-8 -*-
"""
四策略封装（v5 strategies）
===========================
PRD 第 5 章要求四个可独立解释、可独立回测的策略，每个都带版本与规则文本：

  A 质量筛选  成立年限 / 规模 / 20 日均额 / 价差 / 跟踪误差 / 费率 / 折溢价 / 结构
              → 合格 / 观察 / 淘汰，淘汰必须给原因，并输出同类排名
  B 趋势跟随  收盘 > MA60 且 MA20 > MA60 且中期动量 > 0，且须连续 N 日确认
  C 多周期动量 月频换仓；横截面先转标准分；信号在下一交易日按可成交价执行
  D 稳健多资产 核心 50% / 防御 25% / 黄金 10% / 卫星 10% / 现金 5%（战略配置），
              战术层只用市场状态乘数调整，不改战略比例

输出仅供量化参考，不构成投资建议。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

STRATEGY_VERSION = "strategy-v5-2026-11-07"
A_VERSION = "A-quality-v5-2026-11-07"
B_VERSION = "B-trend-v5-2026-11-07"
C_VERSION = "C-momentum-v5-2026-11-07"
D_VERSION = "D-multiasset-v5-2026-11-07"

A_RULES = {
    "min_history_years": 2,
    "min_scale_yi": 2.0,
    "min_amount20_wan": 2000.0,
    "max_spread_pct": 0.5,
    "max_tracking_error_pct": 2.0,
    "max_expense_pct": 0.6,
    "max_premium_pct": 1.0,
    "verdict": {"pass": 75, "watch": 60},
}

B_RULES = {"confirm_days": 3, "ma_fast": 20, "ma_slow": 60,
           "note": "收盘>MA60 且 MA20>MA60 且 60 日动量>0，须连续 confirm_days 个交易日成立"}

C_RULES = {"lookbacks": [20, 60, 120], "top_n": 3, "rebalance": "monthly",
           "note": "月末最后一个交易日计算，次一交易日按可成交价执行；横截面先转标准分再合成"}

D_RULES = {
    "templates": {
        "保守": {"core": 30, "defensive": 40, "diversifier": 10, "satellite": 5, "cash": 15},
        "稳健": {"core": 50, "defensive": 25, "diversifier": 10, "satellite": 10, "cash": 5},
        "平衡": {"core": 60, "defensive": 15, "diversifier": 12, "satellite": 15, "cash": 8},
    },
    "default": "稳健",
    "note": "示例权重仅为测试示例、非个人建议；战略配置与战术择时分离，战术层只乘风险乘数",
}


def _num(x):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def _clamp(x, lo=0, hi=100):
    return max(lo, min(hi, float(x)))


# ------------------------------------------------------------------ A 质量筛选
def strategy_a(quality_results: list) -> dict:
    """把 etf_quality 的指标按 A 策略硬门槛重判，并给出淘汰原因与同类排名。"""
    rows = []
    for q in (quality_results or []):
        code = q.get("code")
        m = q.get("metrics") or {}
        score = _num(q.get("quality_score"))
        reasons, fails = [], []
        amt = _num(m.get("avg_amount_wan"))
        spread = _num(m.get("spread_pct"))
        te = _num(m.get("tracking_error_pct")) or _num((m.get("tracking") or {}).get("error_pct")) \
            if isinstance(m.get("tracking"), dict) else _num(m.get("tracking_error_pct"))
        fee = _num(m.get("expense_ratio_pct"))
        prem = _num(m.get("premium_pct"))
        scale = _num(m.get("scale_yi"))
        if amt is not None and amt < A_RULES["min_amount20_wan"]:
            fails.append(f"20 日均成交额 {amt:,.0f} 万元 < 门槛 {A_RULES['min_amount20_wan']:,.0f} 万元")
        if scale is not None and scale < A_RULES["min_scale_yi"]:
            fails.append(f"规模 {scale:.2f} 亿 < 门槛 {A_RULES['min_scale_yi']} 亿")
        if spread is not None and spread > A_RULES["max_spread_pct"]:
            fails.append(f"价差 {spread:.3f}% > 门槛 {A_RULES['max_spread_pct']}%")
        if te is not None and te > A_RULES["max_tracking_error_pct"]:
            fails.append(f"跟踪误差 {te:.2f}% > 门槛 {A_RULES['max_tracking_error_pct']}%")
        if fee is not None and fee > A_RULES["max_expense_pct"]:
            fails.append(f"费率 {fee:.3f}% > 门槛 {A_RULES['max_expense_pct']}%")
        if prem is not None and abs(prem) > A_RULES["max_premium_pct"]:
            fails.append(f"折溢价 {prem:+.2f}% 超 ±{A_RULES['max_premium_pct']}%")
        if score is None:
            verdict = "观察"
            reasons.append("质量分缺失，暂列观察")
        elif score >= A_RULES["verdict"]["pass"] and not fails:
            verdict = "合格"
        elif score >= A_RULES["verdict"]["watch"] and len(fails) <= 1:
            verdict = "观察"
        else:
            verdict = "淘汰"
        if fails:
            reasons.extend(fails)
        if not reasons:
            reasons.append(f"质量分 {score:.1f}，各项指标均在门槛内")
        rows.append({"code": code, "name": q.get("name"), "asset_class": q.get("asset_class"),
                     "quality_score": score, "verdict": verdict, "reasons": reasons,
                     "fail_count": len(fails), "confidence": q.get("confidence")})
    # 同类排名（同 asset_class 内按质量分降序）
    by_class = {}
    for r in rows:
        by_class.setdefault(r.get("asset_class") or "未分类", []).append(r)
    for cls, group in by_class.items():
        ranked = sorted([g for g in group if g["quality_score"] is not None],
                        key=lambda g: -g["quality_score"])
        for i, g in enumerate(ranked, 1):
            g["class_rank"] = i
            g["class_size"] = len(ranked)
        for g in group:
            g.setdefault("class_rank", None)
            g.setdefault("class_size", len(ranked))
    counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in ("合格", "观察", "淘汰")}
    return {"ok": True, "strategy_id": "A", "version_id": A_VERSION,
            "rules": A_RULES, "items": rows, "counts": counts,
            "n": len(rows),
            "note": "淘汰标的不得进入权重分配；合格/观察可供研究，非买入建议"}


# ------------------------------------------------------------------ B 趋势跟随
def strategy_b(price_map: dict, confirm_days: int = 3) -> dict:
    items = []
    for code, df in (price_map or {}).items():
        if df is None or len(df) < 130:
            items.append({"code": code, "state": "数据不足", "days_confirmed": 0,
                          "pass": False, "reasons": [f"历史仅 {0 if df is None else len(df)} 日"]})
            continue
        c = pd.Series(df["close"]).astype(float).reset_index(drop=True)
        ma20 = c.rolling(20).mean()
        ma60 = c.rolling(60).mean()
        cond_cross = (c > ma60) & (ma20 > ma60)
        mom60 = c / c.shift(60) - 1
        cond = cond_cross & (mom60 > 0)
        days = 0
        for v in reversed(cond.tolist()):
            if bool(v) and not (isinstance(v, float) and math.isnan(v)):
                days += 1
            else:
                break
        last_ok = bool(cond.iloc[-1]) if not pd.isna(cond.iloc[-1]) else False
        reasons = []
        if last_ok:
            reasons.append(f"收盘 {c.iloc[-1]:.4f} > MA60 {ma60.iloc[-1]:.4f}")
            reasons.append(f"MA20 {ma20.iloc[-1]:.4f} > MA60 {ma60.iloc[-1]:.4f}")
            reasons.append(f"60 日动量 {(mom60.iloc[-1] * 100):+.2f}% > 0")
        else:
            if not bool(cond_cross.iloc[-1]):
                reasons.append(f"均线条件不成立：收盘 {c.iloc[-1]:.4f} / MA20 {ma20.iloc[-1]:.4f} / MA60 {ma60.iloc[-1]:.4f}")
            if not (mom60.iloc[-1] > 0):
                reasons.append(f"60 日动量 {(mom60.iloc[-1] * 100):+.2f}% ≤ 0")
        items.append({"code": code,
                      "state": "确认成立" if (last_ok and days >= confirm_days) else ("刚成立待确认" if last_ok else "不成立"),
                      "days_confirmed": days, "pass": bool(last_ok and days >= confirm_days),
                      "mom60_pct": None if pd.isna(mom60.iloc[-1]) else round(float(mom60.iloc[-1]) * 100, 2),
                      "reasons": reasons})
    return {"ok": True, "strategy_id": "B", "version_id": B_VERSION, "rules": B_RULES,
            "items": items, "n_pass": sum(1 for i in items if i["pass"]), "n": len(items),
            "note": "趋势需持续确认，单日穿越不构成信号；不叠加估值判断，避免双重择时"}


# ------------------------------------------------------------------ C 多周期动量
def _zscore(vals):
    arr = np.asarray([v for v in vals if v is not None], dtype=float)
    if len(arr) < 2:
        return [None] * len(vals), None, None
    mu, sd = float(np.mean(arr)), float(np.std(arr, ddof=1))
    if sd == 0:
        return [0.0 if v is not None else None for v in vals], mu, sd
    return [None if v is None else float((v - mu) / sd) for v in vals], mu, sd


def strategy_c(price_map: dict, top_n: int = 3) -> dict:
    """月频多周期动量：20/60/120 日收益 → 横截面标准分 → 取前 top_n。"""
    codes, mom = [], {"20": [], "60": [], "120": []}
    for code, df in (price_map or {}).items():
        if df is None or len(df) < 130:
            continue
        c = pd.Series(df["close"]).astype(float).reset_index(drop=True)
        codes.append(code)
        for lb in ("20", "60", "120"):
            n = int(lb)
            mom[lb].append(float(c.iloc[-1] / c.iloc[-1 - n] - 1) * 100)
    if not codes:
        return {"ok": False, "strategy_id": "C", "version_id": C_VERSION,
                "rules": C_RULES, "items": [], "reason": "无足够历史数据"}
    z = {}
    for lb in ("20", "60", "120"):
        z[lb], _, _ = _zscore(mom[lb])
    items = []
    for i, code in enumerate(codes):
        parts = {lb: (None if z[lb][i] is None else round(z[lb][i], 2)) for lb in ("20", "60", "120")}
        avail = [parts[lb] for lb in parts if parts[lb] is not None]
        score = round(sum(avail) / len(avail), 2) if avail else None
        items.append({"code": code,
                      "mom20_pct": round(mom["20"][i], 2),
                      "mom60_pct": round(mom["60"][i], 2),
                      "mom120_pct": round(mom["120"][i], 2),
                      "z": parts, "score": score})
    ranked = sorted([i for i in items if i["score"] is not None], key=lambda x: -x["score"])
    for rank, it in enumerate(ranked, 1):
        it["rank"] = rank
        it["selected"] = rank <= top_n
    for it in items:
        it.setdefault("rank", None)
        it.setdefault("selected", False)
    # 下一交易日可成交价：主流程按次日收盘价成交（与回测口径一致）
    return {"ok": True, "strategy_id": "C", "version_id": C_VERSION, "rules": C_RULES,
            "items": items, "selected": [i["code"] for i in ranked[:top_n]],
            "n": len(items), "top_n": top_n,
            "rebalance": "monthly", "fill": "next_close",
            "note": "月末计算，次一交易日按可成交价执行；换手受成本惩罚约束"}


# ------------------------------------------------------------------ D 稳健多资产
def strategy_d(pool: list, template: str = "稳健", risk_multiplier: float = 1.0) -> dict:
    """
    战略配置（核心/防御/黄金/卫星/现金）+ 战术乘数。
    只在同一资产桶内按战略基准权重的相对比例分配，不改桶间比例。
    """
    tpl = D_RULES["templates"].get(template) or D_RULES["templates"][D_RULES["default"]]
    buckets = {}
    for e in (pool or []):
        ai = e.get("asset_class") or "style_satellite"
        key = {"core_equity": "core", "defensive": "defensive",
               "diversifier": "diversifier"}.get(ai, "satellite")
        buckets.setdefault(key, []).append(e)
    weights, expr = {}, []
    for key in ("core", "defensive", "diversifier", "satellite"):
        members = buckets.get(key) or []
        total_w = float(tpl.get(key, 0))
        base_sum = sum(float(m.get("base_w", 0) or 0) for m in members) or len(members) or 1
        for m in members:
            share = float(m.get("base_w", 0) or 0) / base_sum if base_sum else 1.0 / len(members)
            weights[m["code"]] = round(total_w * share, 2)
            expr.append(f"{m['code']} = {total_w:.0f}% × {share * 100:.1f}%")
    cash = float(tpl.get("cash", 0))
    # 战术层：乘数 < 1 时按比例收缩权益暴露，差额转现金
    equity_exposure = sum(weights.values())
    applied = risk_multiplier if risk_multiplier else 1.0
    if applied < 1.0:
        for k in weights:
            weights[k] = round(weights[k] * applied, 2)
        cash = round(100.0 - sum(weights.values()), 2)
        expr.append(f"战术层：风险乘数 {applied:.2f} → 权益暴露由 {equity_exposure:.1f}% 收缩，现金升至 {cash:.2f}%")
    return {"ok": True, "strategy_id": "D", "version_id": D_VERSION,
            "template": template, "templates": D_RULES["templates"],
            "weights": weights, "cash_pct": cash,
            "invested_pct": round(sum(weights.values()), 2),
            "risk_multiplier": applied, "expression": expr[:14],
            "rules": D_RULES,
            "note": "示例权重仅为测试示例、非个人建议；战略配置与战术择时分离"}


def evaluate_all(quality_results, price_map, pool, template="稳健", risk_multiplier=1.0) -> dict:
    return {"A": strategy_a(quality_results), "B": strategy_b(price_map),
            "C": strategy_c(price_map), "D": strategy_d(pool, template, risk_multiplier),
            "version": STRATEGY_VERSION,
            "note": "四策略各自独立，可单独回测；组合层只接受通过闸门的输出"}


def self_test() -> list:
    out = []

    def chk(name, cond, extra=""):
        out.append({"name": name, "pass": bool(cond), "extra": extra})

    n = 200
    up = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n).strftime("%Y-%m-%d"),
                       "close": np.linspace(1.0, 1.5, n)})
    down = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n).strftime("%Y-%m-%d"),
                         "close": np.linspace(1.5, 1.0, n)})
    b = strategy_b({"510300": up, "515180": down})
    chk("B 上涨→确认成立", any(i["code"] == "510300" and i["pass"] for i in b["items"]))
    chk("B 下跌→不成立", any(i["code"] == "515180" and not i["pass"] for i in b["items"]))
    c = strategy_c({"510300": up, "515180": down, "518880": up}, top_n=2)
    chk("C 取前 N 且含上涨标的", "510300" in c["selected"] and "515180" not in c["selected"],
        str(c["selected"]))
    chk("C 横截面标准分", all("z" in i for i in c["items"]))
    q = [{"code": "510300", "name": "沪深300ETF", "asset_class": "core_equity", "quality_score": 78,
          "metrics": {"avg_amount_wan": 50000, "spread_pct": 0.05, "premium_pct": 0.1, "scale_yi": 300, "expense_ratio_pct": 0.5}},
         {"code": "512050", "name": "中证A500ETF", "asset_class": "core_equity", "quality_score": 100,
          "metrics": {"avg_amount_wan": 60000, "spread_pct": 0.05, "premium_pct": 0.1, "scale_yi": 200, "expense_ratio_pct": 0.5}},
         {"code": "515220", "name": "煤炭ETF", "asset_class": "style_satellite", "quality_score": 55,
          "metrics": {"avg_amount_wan": 100, "spread_pct": 0.2, "premium_pct": 0.2, "scale_yi": 30, "expense_ratio_pct": 0.6}}]
    a = strategy_a(q)
    chk("A 低流动→淘汰", any(i["code"] == "515220" and i["verdict"] == "淘汰" for i in a["items"]),
        str(a["counts"]))
    chk("A 同类排名", all(i.get("class_rank") for i in a["items"] if i["quality_score"]),
        str([(i["code"], i.get("class_rank")) for i in a["items"]]))
    chk("A 淘汰有原因", all(i["reasons"] for i in a["items"] if i["verdict"] == "淘汰"))
    pool = [{"code": "510300", "asset_class": "core_equity", "base_w": 20},
            {"code": "512050", "asset_class": "core_equity", "base_w": 10},
            {"code": "511010", "asset_class": "defensive", "base_w": 15},
            {"code": "511880", "asset_class": "defensive", "base_w": 10},
            {"code": "518880", "asset_class": "diversifier", "base_w": 10},
            {"code": "515180", "asset_class": "style_satellite", "base_w": 15}]
    d = strategy_d(pool, "稳健", 1.0)
    chk("D 稳健模板投资合计 95%", abs(d["invested_pct"] - 95.0) < 0.01, str(d["invested_pct"]))
    chk("D 现金 5%", abs(d["cash_pct"] - 5.0) < 0.01, str(d["cash_pct"]))
    d2 = strategy_d(pool, "保守", 0.6)
    chk("D 保守+战术收缩→现金上升", d2["cash_pct"] > 15 and d2["invested_pct"] < 85,
        f"{d2['invested_pct']}/{d2['cash_pct']}")
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        r = self_test()
        for x in r:
            print(("PASS " if x["pass"] else "FAIL ") + x["name"] + (" | " + x["extra"] if x["extra"] else ""))
        print("%d/%d PASS" % (sum(1 for x in r if x["pass"]), len(r)))
        raise SystemExit(0 if all(x["pass"] for x in r) else 1)
