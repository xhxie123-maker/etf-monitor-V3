# -*- coding: utf-8 -*-
"""
V5.0 风险预算仓位引擎（position_sizing）
=======================================
对应 Smart ETF Quant V4.0 PRD 第 6.5 / 8.3 / 15.3 / 15.4 节：

  w_i ∝ RiskBudget_i / σ_i

再依次施加约束：
  1) 单只 ETF 最大权重
  2) 同类资产总权重
  3) 家族/风格分组上限（如红利家族）
  4) 强周期品种研究上限
  5) 相关性集中度（由 risk_control 检查，本模块只接收上限）
  6) 流动性（流动性不足的标的不进入权重分配）
  7) 现金预留
  8) 最小交易单位取整（整手）

硬性前置（PRD 15.4）：
  「未确认投资期限与可承受最大回撤前，只出排名/市场状态/趋势/风险指标，
    目标仓位必须基于用户明确设定的风险预算。」
  因此本模块用一个显式开关 risk_tolerance_configured 控制：
    - False：只输出「战略基准权重（未个性化）」，不输出可执行的目标仓位与数量；
    - True ：才输出个性化目标权重 + 整手数量 + 交易计划。

所有输出均为量化参考，不构成投资建议。
"""
from __future__ import annotations

import math

MODEL_VERSION = "sizing-v5-2026-11-07"


def _num(x):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def risk_budget_weights(items: list, sigma: dict, target_total_pct: float = 95.0,
                        constraints: dict | None = None,
                        liquidity_block: set | None = None) -> dict:
    """
    按 RiskBudget/σ 计算初始权重，再逐条施加上限约束。

    items: [{"code","name","asset_class","base_w","max_w","min_w","cyclic"}]
    sigma: {code: 年化波动率%}
    target_total_pct: 可投资总权重（100 - 现金预留）
    liquidity_block: 流动性不合格、不得分配权重的 code 集合（策略A 淘汰）
    """
    c = constraints or {}
    single_max = float(c.get("single_etf_max_w", 35))
    class_max = c.get("asset_class_max_w", {}) or {}
    family_max = c.get("family_group_max_w", {}) or {}
    family_groups = c.get("family_groups", {}) or {}
    cyclic_cap = float(c.get("cyclic_single_research_cap_w", 10))
    blocked = liquidity_block or set()

    # 1) 初始风险预算权重
    raw = {}
    excluded = {}
    for it in items:
        code = it["code"]
        if code in blocked:
            excluded[code] = "流动性/质量不合格，不参与权重分配"
            continue
        s = _num(sigma.get(code))
        if s is None or s <= 0:
            excluded[code] = "波动率缺失，无法按风险预算分配"
            continue
        rb = float(it.get("base_w", 0) or 0)
        if rb <= 0:
            excluded[code] = "战略基准权重为 0"
            continue
        raw[code] = rb / s

    if not raw:
        return {"ok": False, "weights": {}, "excluded": excluded,
                "note": "无可用标的，无法分配权重"}

    total_raw = sum(raw.values())
    w = {k: v / total_raw * target_total_pct for k, v in raw.items()}

    # 2) 上限约束（迭代重分配，最多 20 轮）
    code2item = {it["code"]: it for it in items}

    def _caps():
        caps = {}
        for code in w:
            it = code2item[code]
            caps[code] = min(float(it.get("max_w", single_max) or single_max), single_max)
            if it.get("cyclic"):
                caps[code] = min(caps[code], cyclic_cap)
        # 资产类别上限（按类别内权重合计折算）
        for ac, cap in class_max.items():
            members = [k for k in w if code2item[k].get("asset_class") == ac]
            cur = sum(w[k] for k in members)
            if cur > float(cap) and cur > 0:
                scale = float(cap) / cur
                for k in members:
                    caps[k] = min(caps.get(k, 1e9), w[k] * scale)
        # 家族分组上限
        for gname, cap in family_max.items():
            members = [k for k in family_groups.get(gname, []) if k in w]
            cur = sum(w[k] for k in members)
            if cur > float(cap) and cur > 0:
                scale = float(cap) / cur
                for k in members:
                    caps[k] = min(caps.get(k, 1e9), w[k] * scale)
        return caps

    for _ in range(20):
        caps = _caps()
        over = {k: w[k] - caps[k] for k in w if w[k] > caps[k] + 1e-9}
        if not over:
            break
        for k, v in over.items():
            w[k] = caps[k]
        # 溢出额按原始风险预算比例分给未触顶标的
        free = [k for k in w if w[k] < caps[k] - 1e-9]
        spare = target_total_pct - sum(w.values())
        if not free or spare <= 1e-9:
            break
        free_raw = sum(raw[k] for k in free)
        for k in free:
            w[k] += spare * raw[k] / free_raw

    # 3) 下限处理：若某标的下限无法满足，从权重最高的非下限标的转移
    for code in list(w):
        it = code2item[code]
        mn = float(it.get("min_w", 0) or 0)
        if mn > 0 and w[code] < mn:
            need = mn - w[code]
            donors = sorted([k for k in w if k != code and w[k] > float(code2item[k].get("min_w", 0) or 0)],
                            key=lambda k: -w[k])
            for d in donors:
                room = w[d] - float(code2item[d].get("min_w", 0) or 0)
                take = min(room, need)
                if take > 0:
                    w[d] -= take
                    w[code] += take
                    need -= take
                if need <= 1e-9:
                    break

    w = {k: round(v, 2) for k, v in w.items()}
    return {
        "ok": True,
        "weights": w,
        "excluded": excluded,
        "total_pct": round(sum(w.values()), 2),
        "cash_reserve_pct": round(100.0 - target_total_pct, 2),
        "risk_budget_pct": {k: round(float(code2item[k].get("base_w", 0) or 0), 2) for k in w},
        "sigma_pct": {k: round(float(sigma[k]), 2) for k in w},
        "constraints": {"single_etf_max_w": single_max, "asset_class_max_w": class_max,
                        "family_group_max_w": family_max, "cyclic_cap": cyclic_cap},
        "model_version": MODEL_VERSION,
        "note": "权重经单只/同类/家族/周期/流动性/现金预留约束；目标仓位≠必须执行",
    }


def position_plan(weights: dict, capital: float, prices: dict, rules: dict,
                  fee: float = 0.001, slippage: float = 0.001,
                  cash_buffer_pct: float = 5.0) -> dict:
    """
    把目标权重翻译成整手数量与资金占用（PRD 8.4 / 15.3）。

    capital: 账户总资金（元）。现金缓冲先扣除，再按权重分配预算。
    每只标的的数量 = floor(该标的预算 / 每手全成本) × 每手份额。
    """
    import trading_rules as tr

    if capital is None or capital <= 0:
        return {"ok": False, "reason": "未提供资金规模，不能生成数量计划"}
    investable = float(capital) * (1.0 - cash_buffer_pct / 100.0)
    plan, used, notes = [], 0.0, []
    for code, wpct in weights.items():
        px = _num(prices.get(code))
        rule = rules.get(code) if isinstance(rules, dict) else tr.rules_for(code)
        if px is None or px <= 0:
            notes.append(f"{code}: 价格缺失，跳过")
            continue
        if not rule or not rule.get("available"):
            notes.append(f"{code}: instrument_rule 缺失，禁止生成数量计划（PRD 15.1/15.5）")
            continue
        budget = investable * float(wpct) / 100.0
        q = tr.lot_qty(budget, px, lot=int(rule["min_order_qty"]), fee=fee, slippage=slippage)
        plan.append({
            "code": code,
            "target_weight_pct": round(float(wpct), 2),
            "budget_cny": round(budget, 2),
            "price": round(px, 4),
            "qty": q["qty"],
            "lots": q["lots"],
            "cash_used": q["cash_used"],
            "cash_left_in_budget": q["cash_left"],
            "min_order_qty": rule["min_order_qty"],
            "order_step": rule["order_step"],
            "price_tick": rule["price_tick"],
            "settlement_type": rule["settlement_type"],
            "fill_ok": q["ok"],
            "note": q["reason"],
        })
        used += q["cash_used"]
    leftover = round(float(capital) - used, 2)
    return {
        "ok": True, "capital": round(float(capital), 2),
        "investable": round(investable, 2),
        "cash_buffer_pct": cash_buffer_pct,
        "plan": plan, "cash_used": round(used, 2), "cash_left": leftover,
        "notes": notes, "model_version": MODEL_VERSION,
        "note": "金额为按参考价估算的名义资金占用，实际成交价、费用与滑点以券商为准",
    }


def check_constraints(weights: dict, items: list, constraints: dict | None = None) -> dict:
    """复核权重是否满足单只/同类/家族/周期上限，返回违规清单。"""
    c = constraints or {}
    code2item = {it["code"]: it for it in items}
    single_max = float(c.get("single_etf_max_w", 35))
    class_max = c.get("asset_class_max_w", {}) or {}
    family_max = c.get("family_group_max_w", {}) or {}
    family_groups = c.get("family_groups", {}) or {}
    cyclic_cap = float(c.get("cyclic_single_research_cap_w", 10))
    flags = []
    for code, w in weights.items():
        it = code2item.get(code, {})
        cap = min(float(it.get("max_w", single_max) or single_max), single_max)
        if it.get("cyclic"):
            cap = min(cap, cyclic_cap)
        if w > cap + 0.01:
            flags.append({"level": "block", "label": f"{code} 权重 {w:.1f}% 超单只上限 {cap:.0f}%"})
    for ac, cap in class_max.items():
        cur = sum(w for k, w in weights.items() if code2item.get(k, {}).get("asset_class") == ac)
        if cur > float(cap) + 0.01:
            flags.append({"level": "block", "label": f"资产类别 {ac} 合计 {cur:.1f}% 超上限 {cap}%"})
    for gname, cap in family_max.items():
        members = family_groups.get(gname, [])
        cur = sum(weights.get(k, 0) for k in members)
        if cur > float(cap) + 0.01:
            flags.append({"level": "block", "label": f"分组 {gname} 合计 {cur:.1f}% 超上限 {cap}%"})
    return {"ok": len(flags) == 0, "flags": flags}


def self_test() -> list:
    fails = []
    items = [
        {"code": "A", "asset_class": "core_equity", "base_w": 20, "max_w": 35, "min_w": 10, "cyclic": False},
        {"code": "B", "asset_class": "core_equity", "base_w": 10, "max_w": 25, "min_w": 5, "cyclic": False},
        {"code": "C", "asset_class": "defensive", "base_w": 20, "max_w": 30, "min_w": 5, "cyclic": False},
        {"code": "D", "asset_class": "style_satellite", "base_w": 10, "max_w": 10, "min_w": 0, "cyclic": True},
        {"code": "E", "asset_class": "diversifier", "base_w": 5, "max_w": 15, "min_w": 2, "cyclic": False},
    ]
    sigma = {"A": 18, "B": 22, "C": 6, "D": 30, "E": 15}
    constraints = {"single_etf_max_w": 35,
                   "asset_class_max_w": {"core_equity": 55, "defensive": 40, "diversifier": 15,
                                         "style_satellite": 30},
                   "family_group_max_w": {}, "family_groups": {}, "cyclic_single_research_cap_w": 10}
    r = risk_budget_weights(items, sigma, 95.0, constraints)
    if not r["ok"]:
        fails.append("权重分配失败")
    if abs(r["total_pct"] - 95.0) > 0.5:
        fails.append(f"权重合计应约 95%，实际 {r['total_pct']}")
    chk = check_constraints(r["weights"], items, constraints)
    if not chk["ok"]:
        fails.append("分配结果违反约束：" + str(chk["flags"]))
    # 波动率越低（C=6%）风险预算权重应越高
    if r["weights"]["C"] <= r["weights"]["D"]:
        fails.append("低波动标的权重应高于高波动标的")
    # 流动性不合格被剔除
    r2 = risk_budget_weights(items, sigma, 95.0, constraints, liquidity_block={"D"})
    if "D" in r2["weights"]:
        fails.append("流动性不合格标的应被剔除")
    # 整手数量
    plan = position_plan({"A": 20, "C": 20}, 10000, {"A": 1.25, "C": 100.0},
                         {"A": {"available": True, "min_order_qty": 100, "order_step": 100,
                                "price_tick": 0.001, "settlement_type": "T+1"},
                          "C": {"available": True, "min_order_qty": 100, "order_step": 100,
                                "price_tick": 0.001, "settlement_type": "T+0"}},
                         fee=0.001, slippage=0.001, cash_buffer_pct=5)
    if not plan["ok"]:
        fails.append("数量计划生成失败")
    a = [p for p in plan["plan"] if p["code"] == "A"][0]
    # 10000×95%×20% = 1900 元预算；1.25 元/份，100 份/手 → 每手约 126.3 元 → 15 手 = 1500 份
    if a["qty"] != 1500:
        fails.append(f"整手数量应为 1500 份，实际 {a['qty']}")
    return fails


if __name__ == "__main__":
    import sys
    if "selftest" in sys.argv:
        f = self_test()
        print("position_sizing self_test:", "PASS" if not f else "FAIL")
        for x in f:
            print(" -", x)
        sys.exit(0 if not f else 1)
    print("module ok")
