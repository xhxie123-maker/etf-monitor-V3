# -*- coding: utf-8 -*-
"""
V5.0 交易规则层（instrument_rule）
=================================
对应 Smart ETF Quant V4.0 PRD 第 5.2 节「表6 instrument_rule」与第 15.1 / 15.3 节：

  * 交易规则不得全市场无差别硬编码：T+0/T+1、最小申报数量、数量增量、
    价格最小变动单位、涨跌幅约束、交易时段、盘后固定价格交易均按标的登记；
  * 规则带生效区间（effective_from / effective_to）、来源与核验时间；
  * 小资金必须按整手取整：Q = floor(B / (P × L)) × L，并额外扣除费用与滑点。

本模块为纯函数 + 静态规则表，不联网。规则值分两类：
  1) 交易所层面的通用规则（端口/单位/tick/时段）—— 标注为「初始参考」；
  2) 单只产品的差异化属性（T+0/T+1、盘后固定价格资格）—— 标注 verified=False，
     必须在券商/交易所公告逐只核验后才可用于真实下单。

所有输出均为量化参考，不构成投资建议。
"""
from __future__ import annotations

import math

# 规则版本与依据
RULE_VERSION = "instrument-rule-v5-2026-11-07"
RULE_EFFECTIVE_FROM = "2026-07-06"   # 沪深 2026 年修订交易规则实施日（PRD 15.1）
RULE_SOURCE = "上交所/深交所 ETF 交易规则（2026-07-06 实施）；初始参考值，逐只待核验"

# 交易所默认参数（初始参考）
EXCHANGE_DEFAULTS = {
    "SH": {
        "exchange": "上海证券交易所",
        "min_order_qty": 100,          # 上交所 ETF 常见 100 份/手
        "order_step": 100,
        "price_tick": 0.001,           # 上交所 ETF 价格最小变动 0.001 元
        "price_limit_rule": "±10%（权益类 ETF 常规涨跌幅限制）",
        "trading_sessions": ["09:30-11:30", "13:00-15:00"],
        "after_hours_fixed_price_allowed": None,   # 需逐只核验
    },
    "SZ": {
        "exchange": "深圳证券交易所",
        "min_order_qty": 100,
        "order_step": 100,
        "price_tick": 0.001,
        "price_limit_rule": "±10%（权益类 ETF 常规涨跌幅限制）",
        "trading_sessions": ["09:30-11:30", "13:00-15:00"],
        "after_hours_fixed_price_allowed": None,   # 2026 新规扩大品种，需逐只核验
    },
}

# 单只产品规则（初始参考；verified=False 表示尚未逐只核验）
INSTRUMENT_RULES = {
    "510300": {"exchange": "SH", "settlement_type": "T+1", "category": "equity",
               "after_hours_fixed_price_allowed": False,
               "note": "沪深300ETF：股票 ETF，T+1"},
    "512050": {"exchange": "SH", "settlement_type": "T+1", "category": "equity",
               "after_hours_fixed_price_allowed": False,
               "note": "中证A500ETF：股票 ETF，T+1"},
    "511010": {"exchange": "SH", "settlement_type": "T+0", "category": "bond",
               "after_hours_fixed_price_allowed": False,
               "note": "国债ETF：债券 ETF，T+0"},
    "511880": {"exchange": "SH", "settlement_type": "T+0", "category": "money",
               "after_hours_fixed_price_allowed": False,
               "note": "货币ETF：货币 ETF，T+0，现金管理工具"},
    "518880": {"exchange": "SH", "settlement_type": "T+0", "category": "gold",
               "after_hours_fixed_price_allowed": False,
               "note": "黄金ETF：黄金 ETF，T+0"},
    "515180": {"exchange": "SH", "settlement_type": "T+1", "category": "equity",
               "after_hours_fixed_price_allowed": False,
               "note": "红利ETF：股票 ETF，T+1"},
    "516310": {"exchange": "SH", "settlement_type": "T+1", "category": "equity",
               "after_hours_fixed_price_allowed": False,
               "note": "银行ETF：股票 ETF，T+1"},
    "515220": {"exchange": "SH", "settlement_type": "T+1", "category": "equity",
               "after_hours_fixed_price_allowed": False,
               "note": "煤炭ETF：股票 ETF，T+1"},
    "159201": {"exchange": "SZ", "settlement_type": "T+1", "category": "equity",
               "after_hours_fixed_price_allowed": None,
               "note": "自由现金流ETF：深市股票 ETF，T+1；盘后固定价格资格需核验"},
    "563020": {"exchange": "SH", "settlement_type": "T+1", "category": "equity",
               "after_hours_fixed_price_allowed": False,
               "note": "红利低波ETF：股票 ETF，T+1"},
}


def rules_for(code: str) -> dict:
    """返回单只 ETF 的完整交易规则（交易所默认 + 产品覆盖）。"""
    code = str(code).zfill(6)
    prod = INSTRUMENT_RULES.get(code)
    if prod is None:
        return {"etf_code": code, "available": False,
                "reason": "instrument_rule 未登记，禁止生成交易计划（PRD 15.1/15.5）"}
    ex = EXCHANGE_DEFAULTS[prod["exchange"]]
    out = {
        "etf_code": code,
        "available": True,
        "exchange": ex["exchange"],
        "exchange_code": prod["exchange"],
        "settlement_type": prod["settlement_type"],
        "category": prod["category"],
        "min_order_qty": ex["min_order_qty"],
        "order_step": ex["order_step"],
        "price_tick": ex["price_tick"],
        "price_limit_rule": ex["price_limit_rule"],
        "trading_sessions": list(ex["trading_sessions"]),
        "after_hours_fixed_price_allowed": prod["after_hours_fixed_price_allowed"],
        "effective_from": RULE_EFFECTIVE_FROM,
        "effective_to": None,
        "source_url": RULE_SOURCE,
        "verified": False,
        "verified_at": None,
        "rule_version": RULE_VERSION,
        "note": prod["note"],
    }
    return out


def rules_table() -> list:
    """全池规则列表（供数据库/看板展示）。"""
    return [rules_for(c) for c in INSTRUMENT_RULES]


def lot_qty(budget: float, price: float, lot: int = 100,
            fee: float = 0.0, slippage: float = 0.0) -> dict:
    """
    按整手计算可买数量（PRD 15.3）。

      Q = floor(B / (P × L)) × L

    实际可用资金 B 需先扣除现金缓冲；每手实际成本还要计入滑点与手续费，
    因此用「每手全成本」做分母，避免按名义价算出买不起的数量。

    返回：qty / lots / cash_used / cash_left / per_lot_cost / effective_price
    """
    if budget is None or price is None or price <= 0 or lot <= 0:
        return {"qty": 0, "lots": 0, "cash_used": 0.0, "cash_left": round(float(budget or 0), 2),
                "per_lot_cost": None, "effective_price": None, "ok": False,
                "reason": "预算或价格无效"}
    eff_price = float(price) * (1.0 + float(slippage))
    per_lot = eff_price * lot * (1.0 + float(fee))
    lots = int(math.floor(float(budget) / per_lot)) if per_lot > 0 else 0
    if lots < 0:
        lots = 0
    qty = lots * lot
    used = round(per_lot * lots, 2)
    return {
        "qty": qty, "lots": lots,
        "cash_used": used,
        "cash_left": round(float(budget) - used, 2),
        "per_lot_cost": round(per_lot, 2),
        "effective_price": round(eff_price, 4),
        "ok": qty > 0,
        "reason": "" if qty > 0 else "预算不足一手（含费用与滑点）",
    }


def validate_order(qty: int, rule: dict) -> dict:
    """校验申报数量是否符合最小申报单位与增量。"""
    if not rule or not rule.get("available"):
        return {"ok": False, "reason": "交易规则未登记"}
    lot = int(rule["min_order_qty"])
    step = int(rule["order_step"])
    if qty <= 0:
        return {"ok": False, "reason": "数量必须为正"}
    if qty % step != 0:
        return {"ok": False, "reason": f"数量须为 {step} 的整数倍（当前 {qty}）"}
    if qty < lot:
        return {"ok": False, "reason": f"低于最小申报数量 {lot} 份"}
    return {"ok": True, "reason": "符合最小申报单位与增量"}


def rule_gate(codes_weights: dict | None = None) -> dict:
    """
    规则完整性闸门（PRD 15.5）：任何参与决策的标的规则缺失 → 暂停该标的的交易计划。
    codes_weights: {code: weight%}
    """
    codes = codes_weights or {c: 1.0 for c in INSTRUMENT_RULES}
    missing, unverified = [], []
    for code in codes:
        r = rules_for(code)
        if not r.get("available"):
            missing.append(code)
        elif not r.get("verified"):
            unverified.append(code)
    return {
        "ok": len(missing) == 0,
        "missing": missing,
        "unverified": unverified,
        "level": "block" if missing else ("warn" if unverified else "ok"),
        "summary": ("规则缺失，相关标的暂停生成交易计划" if missing
                    else ("规则已登记但未逐只核验，交易计划仅供研究" if unverified
                          else "交易规则完整")),
    }


def self_test() -> list:
    """自测：整手计算、规则校验、闸门。"""
    fails = []
    # 1) PRD 15.3 示例：100 份/手、1.25 元、3000 元、无费用 → 2400 份
    r = lot_qty(3000, 1.25, 100, 0.0, 0.0)
    if r["qty"] != 2400:
        fails.append(f"整手示例应为 2400 份，实际 {r['qty']}")
    # 2) 含费用滑点后每手成本上升，数量不应多于零成本情形
    r2 = lot_qty(3000, 1.25, 100, 0.001, 0.001)
    if r2["qty"] > 2400:
        fails.append("计入费用与滑点后数量反而增加")
    # 3) 预算不足一手
    r3 = lot_qty(100, 1.25, 100)
    if r3["qty"] != 0 or r3["ok"]:
        fails.append("预算不足一手时应返回 0")
    # 4) 规则校验
    rule = rules_for("510300")
    if not validate_order(200, rule)["ok"]:
        fails.append("200 份应通过校验")
    if validate_order(150, rule)["ok"]:
        fails.append("150 份不应通过校验（非 100 整数倍）")
    if validate_order(0, rule)["ok"]:
        fails.append("0 份不应通过校验")
    # 5) T+0/T+1 分类正确
    if rules_for("511010")["settlement_type"] != "T+0":
        fails.append("国债ETF 应为 T+0")
    if rules_for("510300")["settlement_type"] != "T+1":
        fails.append("沪深300ETF 应为 T+1")
    # 6) 未登记标的必须被闸门阻断
    g = rule_gate({"999999": 1.0})
    if g["ok"] or g["level"] != "block":
        fails.append("未登记标的应触发规则闸门阻断")
    return fails


if __name__ == "__main__":
    import sys
    if "selftest" in sys.argv:
        f = self_test()
        print("trading_rules self_test:", "PASS" if not f else "FAIL")
        for x in f:
            print(" -", x)
        sys.exit(0 if not f else 1)
    for r in rules_table():
        print(r["etf_code"], r["settlement_type"], r["min_order_qty"], r["price_tick"],
              r["after_hours_fixed_price_allowed"], r["verified"])
