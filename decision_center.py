# -*- coding: utf-8 -*-
"""
V5.0 决策中心（decision_center）
===============================
对应 Smart ETF Quant V4.0 PRD 第 6.6 / 9 / 15.4 / 15.5 节：

五道检查（任一「阻断」即不出扩大风险的建议）：
  1) 数据质量闸门：行情未更新 / 规则缺失 / 复权异常 / 估值与行情日期不一致 / IOPV 过期 / 指标失败
  2) 资格闸门    ：策略A 质量为「淘汰」的标的不得新增
  3) 市场风险闸门：市场状态 + 熔断等级（红色禁止新增）
  4) 组合风险闸门：单只/类别/相关性/回撤硬阈值
  5) 资金与最小单位闸门：现金缓冲、整手数量、费用与滑点

每条建议必须含 PRD 第 9 章要求的 12 个字段：
  代码 / 当前评分 / 当前状态 / 现有仓位 / 建议目标权重 / 调整方向 / 建议数量 /
  资金占用 / 交易成本 / 推荐理由 / 风险提示 / 信号日期 / 失效日期
且 review_status 默认 pending，必须人工确认后才算生效。

硬性前置（PRD 15.4）：
  未确认投资期限与可承受最大回撤 → 只输出排名/状态/指标，不给个性化目标仓位。

所有输出均为量化参考，不构成投资建议。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

MODEL_VERSION = "decision-v5-2026-11-07"

# 微幅换手抑制：调仓幅度低于该阈值时，交易成本不划算（PRD 6.6）
MIN_TRADE_DELTA_PCT = 2.0
COST_BENEFIT_RATIO_MIN = 3.0


def _num(x):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def next_trading_day(date_str: str, holidays: set | None = None) -> str:
    """简易下一交易日（跳过周六日与给定节假日）；精确日历以 trading_calendar 表为准。"""
    holidays = holidays or set()
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
    except ValueError:
        return str(date_str)
    for _ in range(10):
        d += timedelta(days=1)
        if d.weekday() < 5 and d.strftime("%Y-%m-%d") not in holidays:
            return d.strftime("%Y-%m-%d")
    return (d + timedelta(days=1)).strftime("%Y-%m-%d")


def gate_data(dq: dict | None, rule_gate: dict | None, extras: dict | None = None) -> dict:
    """第 1 道：数据质量闸门。"""
    dq = dq or {}
    rg = rule_gate or {}
    ex = extras or {}
    flags = []
    ok = True
    if dq.get("blocked"):
        ok = False
        flags.append({"level": "block", "label": "数据质量闸门阻断：" + (dq.get("reason") or "数据异常")})
    elif dq.get("level") == "warn":
        flags.append({"level": "warn", "label": "数据质量提示：" + (dq.get("reason") or "")})
    if rg.get("level") == "block":
        ok = False
        flags.append({"level": "block", "label": "交易规则缺失：" + (rg.get("summary") or "")})
    elif rg.get("level") == "warn":
        flags.append({"level": "warn", "label": "交易规则未逐只核验：" + (rg.get("summary") or "")})
    for k, v in ex.items():
        if v:
            flags.append({"level": "warn", "label": f"{k}：{v}"})
    return {"gate": "data", "ok": ok, "flags": flags}


def gate_quality(quality: str | None) -> dict:
    """第 2 道：资格闸门（策略A 淘汰即不得新增）。"""
    if quality == "淘汰":
        return {"gate": "quality", "ok": False,
                "flags": [{"level": "block", "label": "产品质量判定为淘汰，禁止新增"}]}
    if quality == "观察":
        return {"gate": "quality", "ok": True,
                "flags": [{"level": "warn", "label": "产品质量为观察级，仅可小额试探"}]}
    return {"gate": "quality", "ok": True, "flags": []}


def gate_market(regime: dict | None, breaker: dict | None, direction: str) -> dict:
    """第 3 道：市场风险闸门。只对「扩大风险」的方向（加仓）生效。"""
    reg = regime or {}
    cb = breaker or {}
    flags = []
    ok = True
    if direction == "加仓":
        if cb.get("level") == "red" or cb.get("allow_add") is False:
            ok = False
            flags.append({"level": "block", "label": "红色熔断：禁止生成扩大风险的新增建议"})
        elif cb.get("half_add"):
            flags.append({"level": "warn", "label": "熔断预警：新增仓位须分批减半"})
        if reg.get("regime") == "高风险":
            flags.append({"level": "warn", "label": "市场状态=高风险：新增仓位需降低风险预算"})
    return {"gate": "market", "ok": ok, "flags": flags,
            "regime": reg.get("regime"), "breaker": cb.get("level")}


def gate_portfolio(pchecks: dict | None, is_new_risk: bool) -> dict:
    """第 4 道：组合风险闸门。"""
    pc = pchecks or {}
    flags = list(pc.get("flags") or [])
    ok = True
    if is_new_risk and pc.get("ok") is False:
        ok = False
    return {"gate": "portfolio", "ok": ok, "flags": flags}


def gate_capital(qty: int, capital_ok: bool, cash_after: float | None) -> dict:
    """第 5 道：资金与最小单位闸门。"""
    flags = []
    ok = True
    if not capital_ok:
        ok = False
        flags.append({"level": "block", "label": "预算不足一手（含费用与滑点），无法执行"})
    if qty <= 0 and capital_ok:
        ok = False
        flags.append({"level": "block", "label": "整手计算结果为 0 份"})
    if cash_after is not None and cash_after < 0:
        ok = False
        flags.append({"level": "block", "label": "执行后现金为负，违反现金预留约束"})
    return {"gate": "capital", "ok": ok, "flags": flags}


def should_trade(delta_w_pct, amount_cny, cost_cny, reason_strength: str = "normal") -> dict:
    """
    微幅换手抑制（PRD 6.6）：调仓幅度过小、或成本相对预期收益不划算时，建议不交易。
    reason_strength: strong / normal / weak
    """
    if delta_w_pct is None:
        return {"trade": False, "why": "调仓幅度未知"}
    if abs(delta_w_pct) < MIN_TRADE_DELTA_PCT and reason_strength != "strong":
        return {"trade": False,
                "why": f"目标调整 {delta_w_pct:+.1f}% 低于 {MIN_TRADE_DELTA_PCT:.1f}% 阈值，交易成本不划算"}
    if amount_cny and cost_cny and amount_cny > 0:
        ratio = cost_cny / amount_cny
        if ratio > COST_BENEFIT_RATIO_MIN / 100.0 and reason_strength != "strong":
            return {"trade": False, "why": f"交易成本占成交额 {ratio*100:.2f}% 偏高，建议合并到下次再平衡"}
    return {"trade": True, "why": "达到可执行阈值"}


def build_decision(item: dict, score_row: dict, ctx: dict) -> dict:
    """
    生成单只标的的决策（12 字段 + 五道闸门）。
    item: {"code","name","asset_class","quality","price","target_weight_pct","current_weight_pct","qty","cash_used","capital_ok","cash_after","trade_cost"}
    score_row: factor_engine.composite 输出
    ctx: {"trade_date","regime","breaker","pchecks","dq","rule_gate","risk_tolerance_configured"}
    """
    code = item["code"]
    cur_w = _num(item.get("current_weight_pct")) or 0.0
    tgt_w = _num(item.get("target_weight_pct"))
    delta = None if tgt_w is None else tgt_w - cur_w
    if delta is None:
        direction = "仅观察"
    elif delta > 1.0:
        direction = "加仓"
    elif delta < -1.0:
        direction = "减仓"
    else:
        direction = "维持"

    sig = (score_row or {}).get("signal") or {}
    total = (score_row or {}).get("total")

    g1 = gate_data(ctx.get("dq"), ctx.get("rule_gate"), ctx.get("dq_extras"))
    g2 = gate_quality(item.get("quality"))
    g3 = gate_market(ctx.get("regime"), ctx.get("breaker"), direction)
    g4 = gate_portfolio(ctx.get("pchecks"), direction == "加仓")
    g5 = gate_capital(int(item.get("qty") or 0), bool(item.get("capital_ok")),
                      _num(item.get("cash_after")))
    gates = [g1, g2, g3, g4, g5]
    blocks = [f for g in gates for f in g["flags"] if f["level"] == "block"]
    warns = [f for g in gates for f in g["flags"] if f["level"] == "warn"]

    # 最终动作
    if blocks:
        action = "暂停"
    elif direction == "加仓":
        action = "加仓（需人工确认）"
        if any("减半" in w["label"] for w in warns):
            action = "加仓-减半（需人工确认）"
    elif direction == "减仓":
        action = "减仓（需人工确认）"
    elif not ctx.get("risk_tolerance_configured"):
        action = "仅观察（未确认投资期限与最大回撤）"
    else:
        action = "维持"

    # PRD 15.4 硬前置：未确认风险偏好时不给个性化目标仓位与数量
    if not ctx.get("risk_tolerance_configured"):
        tgt_w = None
        delta = None
        action = "仅观察（未确认投资期限与最大回撤）"

    reasons = []
    if total is not None:
        reasons.append(f"综合评分 {total:.1f}（{sig.get('label','')}），五维 "
                       f"{'/'.join(f'{k}:{v}' for k, v in (score_row.get('sub') or {}).items() if v is not None)}")
    if (score_row.get("missing") or []):
        reasons.append("缺失维度：" + "、".join(score_row["missing"]) + "（已按可用维度归一化，不记零分）")
    if item.get("quality"):
        reasons.append(f"产品质量：{item['quality']}")
    if (score_row.get("detail") or {}).get("trend", {}).get("parts"):
        reasons.append("趋势：" + "；".join(score_row["detail"]["trend"]["parts"][:3]))
    if direction in ("加仓", "减仓") and delta is not None:
        reasons.append(f"目标权重 {tgt_w:.1f}%，当前 {cur_w:.1f}%，调整 {delta:+.1f}%")
    if warns:
        reasons.append("提示：" + "；".join(w["label"] for w in warns[:3]))

    risk_warning = "；".join([w["label"] for w in warns] + [b["label"] for b in blocks]) or "无特殊风险提示"
    signal_date = ctx.get("trade_date")
    valid_until = next_trading_day(signal_date) if signal_date else None

    return {
        "etf_code": code,
        "etf_name": item.get("name", code),
        # 12 字段
        "current_score": None if total is None else round(total, 1),
        "current_state": sig.get("label", "数据不足"),
        "current_weight_pct": round(cur_w, 2),
        "target_weight_pct": None if tgt_w is None else round(tgt_w, 2),
        "direction": direction,
        "suggested_quantity": int(item.get("qty") or 0) if ctx.get("risk_tolerance_configured") else None,
        "capital_occupied": _num(item.get("cash_used")),
        "trade_cost": _num(item.get("trade_cost")),
        "reasons": reasons,
        "risk_warning": risk_warning,
        "signal_date": signal_date,
        "valid_until": valid_until,
        # 附加：闸门与元数据
        "action": action,
        "gates": gates,
        "n_block": len(blocks), "n_warn": len(warns),
        "confidence": (score_row or {}).get("confidence"),
        "review_status": "pending",
        "model_version": MODEL_VERSION,
    }


def build_plan(signal_rows: list, position_plan: dict, ctx: dict) -> dict:
    """
    把评分结果 + 仓位计划汇总成交易决策清单。
    signal_rows: [{"code","name","asset_class","quality","score_row", ...}]
    position_plan: position_sizing.position_plan 输出
    """
    items = []
    plan_map = {p["code"]: p for p in (position_plan.get("plan") or [])}
    for row in signal_rows:
        code = row["code"]
        p = plan_map.get(code, {})
        it = {
            "code": code, "name": row.get("name", code),
            "asset_class": row.get("asset_class"), "quality": row.get("quality"),
            "price": p.get("price") or row.get("price"),
            "target_weight_pct": (None if not ctx.get("risk_tolerance_configured")
                                  else (p.get("target_weight_pct") if p else row.get("strategic_w_pct"))),
            "current_weight_pct": (ctx.get("holdings") or {}).get(code, 0.0),
            "qty": p.get("qty"),
            "cash_used": p.get("cash_used"),
            "capital_ok": p.get("fill_ok", True),
            "cash_after": position_plan.get("cash_left"),
            "trade_cost": _trade_cost(p.get("cash_used"), ctx),
        }
        d = build_decision(it, row.get("score_row"), ctx)
        d["recommendation_id"] = f"{code}-{ctx.get('trade_date')}-{MODEL_VERSION.split('-')[1]}"
        items.append(d)
    # 排序：评分高、且非暂停的优先
    items.sort(key=lambda x: (
        0 if x["action"] != "暂停" else 1,
        -(x.get("current_score") or 0)))
    return {
        "ok": True, "trade_date": ctx.get("trade_date"),
        "items": items,
        "n_total": len(items),
        "n_add": sum(1 for i in items if i["direction"] == "加仓" and i["action"] != "暂停"),
        "n_reduce": sum(1 for i in items if i["direction"] == "减仓" and i["action"] != "暂停"),
        "n_paused": sum(1 for i in items if i["action"] == "暂停"),
        "regime": (ctx.get("regime") or {}).get("regime"),
        "breaker": (ctx.get("breaker") or {}).get("level"),
        "risk_tolerance_configured": bool(ctx.get("risk_tolerance_configured")),
        "model_version": MODEL_VERSION,
        "disclaimer": "本清单为量化辅助信息，所有交易需人工确认后执行；仅供参考，不构成投资建议。",
    }


def _trade_cost(amount, ctx: dict):
    a = _num(amount)
    if a is None or a <= 0:
        return None
    fee = float(ctx.get("fee", 0.001))
    slip = float(ctx.get("slippage", 0.001))
    min_comm = float(ctx.get("min_commission", 0.0) or 0.0)
    cost = a * (fee + slip)
    return round(max(cost, min_comm), 2)


def record_review(conn, recommendation_id: str, status: str, note: str = "") -> dict:
    """人工确认（PRD 第 9 章：全部交易建议需人工确认）。"""
    if status not in ("approved", "rejected", "pending"):
        return {"ok": False, "reason": "status 只能是 approved / rejected / pending"}
    cur = conn.execute("SELECT review_status FROM recommendation WHERE recommendation_id=?",
                       (recommendation_id,)).fetchone()
    if cur is None:
        return {"ok": False, "reason": "建议不存在"}
    conn.execute("UPDATE recommendation SET review_status=?, review_note=? WHERE recommendation_id=?",
                 (status, note, recommendation_id))
    conn.commit()
    return {"ok": True, "recommendation_id": recommendation_id, "review_status": status}


def expire_past(conn, trade_date: str) -> int:
    """把过期未确认的建议标记为 expired（PRD 表16 expired_at）。"""
    n = conn.execute(
        "UPDATE recommendation SET review_status='expired' "
        "WHERE review_status='pending' AND expired_at IS NOT NULL AND expired_at < ?",
        (trade_date,)).rowcount
    conn.commit()
    return n


def self_test() -> list:
    fails = []
    # 下一交易日（2026-11-06 周五 → 11-09 周一）
    if next_trading_day("2026-11-06") != "2026-11-09":
        fails.append("下一交易日计算错误：" + next_trading_day("2026-11-06"))
    # 微幅换手抑制
    if should_trade(1.0, 1000, 2.0)["trade"]:
        fails.append("1% 调整不应建议交易")
    if not should_trade(8.0, 1000, 2.0)["trade"]:
        fails.append("8% 调整应建议交易")
    # 红色熔断禁止加仓
    g3 = gate_market({"regime": "高风险"}, {"level": "red", "allow_add": False}, "加仓")
    if g3["ok"]:
        fails.append("红色熔断下加仓必须被阻断")
    g3b = gate_market({"regime": "高风险"}, {"level": "red", "allow_add": False}, "减仓")
    if not g3b["ok"]:
        fails.append("红色熔断不应阻断减仓")
    # 淘汰标的禁止新增
    if gate_quality("淘汰")["ok"]:
        fails.append("淘汰标的应被资格闸门阻断")
    # 未确认风险偏好 → 不给目标仓位
    score_row = {"total": 82.0, "signal": {"label": "优先关注", "code": "priority"},
                 "sub": {"trend": 80, "momentum": 70, "valuation": 60,
                         "liquidity": 85, "risk": 75}, "missing": [],
                 "confidence": "high", "detail": {"trend": {"parts": ["收盘高于MA60 2.0%"]}}}
    it = {"code": "510300", "name": "沪深300ETF", "quality": "合格", "qty": 100,
          "cash_used": 120.0, "capital_ok": True, "cash_after": 500.0,
          "target_weight_pct": 20, "current_weight_pct": 10}
    d1 = build_decision(it, score_row, {"trade_date": "2026-11-06",
                                        "risk_tolerance_configured": False, "fee": 0.001,
                                        "slippage": 0.001})
    if d1["target_weight_pct"] is not None or d1["suggested_quantity"] is not None:
        fails.append("未确认风险偏好时不应给出目标仓位与数量")
    if "仅观察" not in d1["action"]:
        fails.append("未确认风险偏好时动作应为仅观察")
    # 确认后给目标仓位
    d2 = build_decision(it, score_row, {"trade_date": "2026-11-06",
                                        "risk_tolerance_configured": True, "fee": 0.001,
                                        "slippage": 0.001})
    if d2["target_weight_pct"] != 20.0 or d2["direction"] != "加仓":
        fails.append("确认风险偏好后应给出目标权重与加仓方向")
    for f in ("current_score", "current_state", "current_weight_pct", "target_weight_pct",
              "direction", "suggested_quantity", "capital_occupied", "trade_cost",
              "reasons", "risk_warning", "signal_date", "valid_until"):
        if f not in d2:
            fails.append(f"缺少 PRD 12 字段：{f}")
    return fails


if __name__ == "__main__":
    import sys
    if "selftest" in sys.argv:
        f = self_test()
        print("decision_center self_test:", "PASS" if not f else "FAIL")
        for x in f:
            print(" -", x)
        sys.exit(0 if not f else 1)
    print("module ok")
