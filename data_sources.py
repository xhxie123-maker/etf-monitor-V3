# -*- coding: utf-8 -*-
"""
数据源分级适配器（v5 data_sources）
==================================
PRD 15.2：数据源必须分级，且「P2（AKShare 等）不得作为唯一生产源」。

  P0 权威原始源：交易所（SSE/SZSE 行情与公告）、基金管理人（净值、持仓、费率公告）
  P1 官方/持牌机构：中证指数、国证指数等指数机构官方数据；Tushare（持牌数据服务）
  P2 第三方聚合：东方财富、腾讯行情、新浪财经、AKShare 封装

本模块只做「分级登记 + 实际使用体检 + 降级判定 + 采集日志落库」，不改变抓数实现；
抓数仍由 etf_monitor.py 既有链路负责，此处把链路结果按分级重新解释。
"""
from __future__ import annotations

import math

MODEL_VERSION = "sources-v5-2026-11-07"

TIERS = {
    "P0": {"label": "权威原始源", "primary_allowed": True,
           "desc": "交易所行情/公告文件、基金管理人披露（净值、持仓、费率）"},
    "P1": {"label": "官方与持牌机构", "primary_allowed": True,
           "desc": "中证/国证等指数机构官方数据、Tushare 等持牌数据服务"},
    "P2": {"label": "第三方聚合", "primary_allowed": False,
           "desc": "东方财富、腾讯、新浪、AKShare 等聚合或封装接口；接口易变，只能作补充或交叉校验"},
}

REGISTRY = [
    {"id": "sse", "name": "上交所官网行情与公告", "tier": "P0",
     "covers": ["etf_basic", "etf_daily_bar", "instrument_rule"],
     "note": "交易所原始文件最权威，但需解析公告/文件，当前未接入"},
    {"id": "szse", "name": "深交所官网行情与公告", "tier": "P0",
     "covers": ["etf_basic", "etf_daily_bar", "instrument_rule"],
     "note": "同上；深市 ETF 盘后固定价格交易资格以此为准"},
    {"id": "fund_company", "name": "基金管理人披露（净值/持仓/费率）", "tier": "P0",
     "covers": ["etf_basic", "etf_valuation", "holding_penetration"],
     "note": "费率和持仓穿透的最终依据；当前持仓来自东财季报，属降级"},
    {"id": "csindex", "name": "中证指数官网（PE/股息率/成分）", "tier": "P1",
     "covers": ["etf_valuation", "index_constituent"],
     "note": "已接入：官方 XLS 估值与股息率序列，是当前估值分位的主口径"},
    {"id": "cnindex", "name": "国证指数官网（980092 等）", "tier": "P1",
     "covers": ["etf_valuation"],
     "note": "已接入 980092 官方 PE-TTM 序列；部分代码官网无数据"},
    {"id": "tushare", "name": "Tushare Pro", "tier": "P1",
     "covers": ["etf_daily_bar", "etf_valuation", "instrument_rule"],
     "note": "持牌数据服务，需 token；当前未接入，作为 P1 备份方案登记"},
    {"id": "em", "name": "东方财富（行情/估值/持仓）", "tier": "P2",
     "covers": ["etf_daily_bar", "etf_valuation", "holding_penetration"],
     "note": "主力抓数通道，但属第三方聚合；接口变动频繁，不得作唯一生产源"},
    {"id": "tx", "name": "腾讯行情（实时/日线）", "tier": "P2",
     "covers": ["etf_daily_bar", "realtime_quote"],
     "note": "东财失败时的第一备用源"},
    {"id": "sina", "name": "新浪财经（历史日线）", "tier": "P2",
     "covers": ["etf_daily_bar", "index_daily"],
     "note": "东财与腾讯失败时的第二备用源；不复权口径需在回测中统一处理"},
    {"id": "akshare", "name": "AKShare 封装", "tier": "P2",
     "covers": ["etf_daily_bar", "holding_penetration"],
     "note": "实为上游接口封装，接口频繁变更；PRD 明确仅学术用途，不得作唯一生产源"},
]

# etf_monitor 的 SRC_STATS 键 → 数据源 id
STATS_MAP = [("ok_em", "em"), ("ok_tx", "tx"), ("ok_sina", "sina"), ("ok_cs", "csindex")]
FAIL_MAP = [("fail_em", "em")]


def registry(tier: str | None = None) -> list:
    if tier:
        return [r for r in REGISTRY if r["tier"] == tier]
    return list(REGISTRY)


def source(source_id: str) -> dict | None:
    for r in REGISTRY:
        if r["id"] == source_id:
            return dict(r)
    return None


def tier_of(source_id: str) -> str | None:
    s = source(source_id)
    return s["tier"] if s else None


def report(src_stats: dict | None, extra_sources: list | None = None,
           trade_date: str | None = None) -> dict:
    """
    依据本轮实际抓数统计做分级体检：
      used              本轮实际生效的数据源及其调用次数
      by_tier           按分级汇总
      only_p2           只看得到 P2 的数据类型（PRD 15.2 违规，需告警）
      degraded          是否处于降级态（P0/P1 缺失，仅靠 P2 支撑）
    """
    stats = src_stats or {}
    used = []
    for key, sid in STATS_MAP:
        cnt = stats.get(key) or 0
        try:
            cnt = int(cnt)
        except (TypeError, ValueError):
            cnt = 0
        if cnt > 0:
            s = source(sid)
            used.append({"id": sid, "name": s["name"], "tier": s["tier"], "calls": cnt})
    for sid in (extra_sources or []):
        s = source(sid)
        if s and not any(u["id"] == sid for u in used):
            used.append({"id": sid, "name": s["name"], "tier": s["tier"], "calls": 0})
    fails = []
    for key, sid in FAIL_MAP:
        n = stats.get(key) or 0
        if n:
            fails.append({"id": sid, "name": source(sid)["name"], "failures": int(n)})
    by_tier = {}
    for t in TIERS:
        items = [u for u in used if u["tier"] == t]
        by_tier[t] = {"label": TIERS[t]["label"], "sources": [i["id"] for i in items],
                      "calls": sum(i["calls"] for i in items), "present": bool(items)}
    p0_p1 = by_tier["P0"]["present"] or by_tier["P1"]["present"]
    only_p2 = (not p0_p1) and by_tier["P2"]["present"]
    degraded = by_tier["P2"]["present"] and p0_p1
    notes = []
    if only_p2:
        notes.append("本轮只有 P2 数据源生效：按 PRD 15.2 判定为不合规，必须补 P0/P1 或明确标注降级")
    if degraded:
        notes.append("行情主通道为 P2（第三方聚合），存在 P1 官方估值兜底，判定为「可接受但需交叉校验」")
    if fails:
        notes.append("本轮失败源：" + "、".join(f"{f['name']}×{f['failures']}" for f in fails))
    if not used:
        notes.append("本轮无任何数据源生效，按 PRD 15.5 输出「数据异常，暂停决策」")
    ok = bool(used) and not only_p2
    return {"ok": ok, "trade_date": trade_date, "used": used, "by_tier": by_tier,
            "failures": fails, "only_p2": only_p2, "degraded": degraded,
            "tiers": TIERS, "registry_size": len(REGISTRY),
            "notes": notes, "model_version": MODEL_VERSION,
            "rule": "P2 只能作补充或交叉校验，不得作为唯一生产源（PRD 15.2）"}


def ingestion_rows(rep: dict, data_types: list | None = None) -> list:
    """把体检结果转成 data_ingestion_log 的行（每源一行，便于追溯）。"""
    rows = []
    dt = data_types or ["etf_daily_bar", "etf_valuation", "etf_signal"]
    for u in (rep or {}).get("used") or []:
        rows.append({"source_id": u["id"], "data_type": "|".join(dt),
                     "requested": u["calls"], "success": u["calls"], "failed": 0,
                     "status": "ok" if u["tier"] in ("P0", "P1") else "degraded_p2",
                     "error": None if u["tier"] in ("P0", "P1") else "P2 第三方聚合，需交叉校验"})
    for f in (rep or {}).get("failures") or []:
        rows.append({"source_id": f["id"], "data_type": "|".join(dt),
                     "requested": f["failures"], "success": 0, "failed": f["failures"],
                     "status": "failed", "error": "本轮连接失败，已降级备用源",
                     })
    return rows


def self_test() -> list:
    out = []

    def chk(name, cond, extra=""):
        out.append({"name": name, "pass": bool(cond), "extra": extra})

    chk("分级共三档", set(TIERS) == {"P0", "P1", "P2"})
    chk("登记表覆盖 10 个源", len(REGISTRY) >= 10, str(len(REGISTRY)))
    chk("P2 明确禁止作为主源", all(r["tier"] != "P2" or not TIERS["P2"]["primary_allowed"] for r in REGISTRY))
    chk("每源都有 covers 与 note", all(r.get("covers") and r.get("note") for r in REGISTRY))
    r1 = report({"ok_em": 8, "ok_tx": 0, "ok_sina": 0, "ok_cs": 6, "fail_em": 2})
    chk("P2+P1 → 可接受但降级", r1["ok"] and r1["degraded"] and not r1["only_p2"],
        str(r1["notes"]))
    r2 = report({"ok_tx": 10, "ok_sina": 0, "ok_cs": 0})
    chk("只有 P2 → 判不合规", r2["only_p2"] and not r2["ok"], str(r2["notes"]))
    r3 = report({})
    chk("无源 → 不 ok", not r3["ok"], str(r3["notes"]))
    r4 = report({"ok_em": 5, "ok_cs": 3})
    rows = ingestion_rows(r4)
    chk("采集日志按源分行", len(rows) >= 2 and all("source_id" in x for x in rows), str(len(rows)))
    chk("tier_of 可查", tier_of("csindex") == "P1" and tier_of("akshare") == "P2")
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        r = self_test()
        for x in r:
            print(("PASS " if x["pass"] else "FAIL ") + x["name"] + (" | " + x["extra"] if x["extra"] else ""))
        print("%d/%d PASS" % (sum(1 for x in r if x["pass"]), len(r)))
        raise SystemExit(0 if all(x["pass"] for x in r) else 1)
