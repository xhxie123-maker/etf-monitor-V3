# -*- coding: utf-8 -*-
"""
REST API 形态层（PRD 第 10 章 13 个端点）
=========================================
PRD 要求 FastAPI 提供 13 个 REST 端点。本项目实际跑在 GitHub Pages（纯静态）
+ GitHub Actions（无常驻进程），因此用「同一套响应契约 + 两种载体」实现：

  载体 A（默认，CI 每日产出）：把每个端点的响应写成静态 JSON 快照
      output/api/v1/<endpoint>.json —— 前端页面直接 GET 静态文件；
  载体 B（本地可选）：api_server.py 用标准库 http.server 提供同路径的实时服务，
      零第三方依赖（本机未安装 FastAPI，不引入不可运行的依赖）。

13 个端点（PRD 10.2 原文路径）：
  GET  /api/v1/market/status
  GET  /api/v1/etfs
  GET  /api/v1/etfs/{code}
  GET  /api/v1/etfs/{code}/signals
  GET  /api/v1/rankings
  GET  /api/v1/portfolio
  GET  /api/v1/portfolio/risk
  GET  /api/v1/recommendations
  GET  /api/v1/backtests
  GET  /api/v1/data/health
  POST /api/v1/portfolio/positions
  POST /api/v1/recommendations/{id}/review
  POST /api/v1/backtests/run

每个响应统一包裹信封：data_date / updated_at / sources / model_versions / data。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

MODEL_VERSION = "api-v5-2026-11-07"

ENDPOINTS = [
    {"method": "GET", "path": "/api/v1/market/status", "file": "market_status.json", "key": "market_status"},
    {"method": "GET", "path": "/api/v1/etfs", "file": "etfs.json", "key": "etfs"},
    {"method": "GET", "path": "/api/v1/etfs/{code}", "file": "etf_detail.json", "key": "etf_detail"},
    {"method": "GET", "path": "/api/v1/etfs/{code}/signals", "file": "etf_signals.json", "key": "etf_signals"},
    {"method": "GET", "path": "/api/v1/rankings", "file": "rankings.json", "key": "rankings"},
    {"method": "GET", "path": "/api/v1/portfolio", "file": "portfolio.json", "key": "portfolio"},
    {"method": "GET", "path": "/api/v1/portfolio/risk", "file": "portfolio_risk.json", "key": "portfolio_risk"},
    {"method": "GET", "path": "/api/v1/recommendations", "file": "recommendations.json", "key": "recommendations"},
    {"method": "GET", "path": "/api/v1/backtests", "file": "backtests.json", "key": "backtests"},
    {"method": "GET", "path": "/api/v1/data/health", "file": "data_health.json", "key": "data_health"},
    {"method": "POST", "path": "/api/v1/portfolio/positions", "file": "_post_positions_schema.json",
     "key": "_post_positions_schema"},
    {"method": "POST", "path": "/api/v1/recommendations/{id}/review", "file": "_post_review_schema.json",
     "key": "_post_review_schema"},
    {"method": "POST", "path": "/api/v1/backtests/run", "file": "_post_backtest_schema.json",
     "key": "_post_backtest_schema"},
]


def _envelope(data, v5, sources=None):
    mv = (v5 or {}).get("model_versions") or {}
    return {
        "data_date": (v5 or {}).get("trade_date"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sources": sources or [],
        "model_versions": mv,
        "api_version": MODEL_VERSION,
        "disclaimer": "本接口输出为量化辅助信息，不构成投资建议。",
        "data": data,
    }


def build_snapshot(v5: dict, config: dict | None = None) -> dict:
    """把 v5_layer 的结果集映射成 13 个端点的响应。"""
    v5 = v5 or {}
    cfg = config or {}
    pool = {c["code"]: c for c in cfg.get("etfs", [])}
    rows = v5.get("score_rows") or []
    risk = v5.get("risk") or {}
    sizing = v5.get("sizing") or {}
    dec = v5.get("decisions") or {}
    db = v5.get("db") or {}
    src = v5.get("data_sources") or {}
    sources = [f"{u['tier']}:{u['id']}" for u in (src.get("used") or [])]

    # 1 market/status
    regime = v5.get("regime3") or {}
    market = {
        "regime": regime.get("regime"), "label": regime.get("label"),
        "total_score": regime.get("total_score"),
        "risk_multiplier": regime.get("risk_multiplier"),
        "trend_score": regime.get("trend_score"), "breadth_score": regime.get("breadth_score"),
        "volatility_score": regime.get("volatility_score"),
        "breadth_detail": regime.get("breadth_detail"),
        "vol20_pct": regime.get("vol20_pct"), "vol_pct_rank": regime.get("vol_pct_rank"),
        "action": regime.get("action"), "reasons": regime.get("reasons") or [],
        "risk_state": (risk.get("risk_level") or {}).get("label"),
        "circuit_breaker": (risk.get("circuit_breaker") or {}).get("level"),
        "ok": regime.get("ok"),
    }

    # 2 etfs
    etfs = []
    for r in rows:
        etfs.append({
            "code": r.get("code"), "name": r.get("name"),
            "asset_class": r.get("asset_class"), "total_score": r.get("total"),
            "sub_scores": r.get("sub"), "signal": (r.get("signal") or {}).get("label"),
            "confidence": r.get("confidence"), "missing_dims": r.get("missing"),
            "quality": r.get("quality"), "quality_score": r.get("quality_score"),
            "rank": r.get("rank"), "pct_rank": r.get("pct_rank"),
            "pe_pct": r.get("pe_pct"), "amount20_wan": r.get("amount20_wan"),
        })

    # 3 etfs/{code}
    detail = {}
    for r in rows:
        code = r.get("code")
        detail[code] = {
            "code": code, "name": r.get("name"), "asset_class": r.get("asset_class"),
            "role": (pool.get(code) or {}).get("role"),
            "score": r.get("total"), "sub_scores": r.get("sub"),
            "signal": (r.get("signal") or {}).get("label"),
            "confidence": r.get("confidence"), "missing_dims": r.get("missing"),
            "quality": r.get("quality"), "quality_score": r.get("quality_score"),
            "pe_pct": r.get("pe_pct"), "amount20_wan": r.get("amount20_wan"),
            "strategic_weight_pct": (sizing.get("weights") or {}).get(code),
            "risk_budget_pct": (sizing.get("risk_budget_pct") or {}).get(code),
            "sigma_pct": (sizing.get("sigma_pct") or {}).get(code),
            "rule": None,
        }

    # 4 etfs/{code}/signals（当前快照里的最新信号 + 失效条件）
    sigs = {}
    for d in (dec.get("items") or []):
        sigs.setdefault(d.get("etf_code"), []).append({
            "signal_date": d.get("signal_date"), "valid_until": d.get("valid_until"),
            "action": d.get("action"), "score": d.get("current_score"),
            "state": d.get("current_state"), "reasons": d.get("reasons"),
            "direction": d.get("direction"), "confidence": d.get("confidence"),
            "strategy_id": "five_factor_v5",
        })

    # 5 rankings
    rankings = [{"rank": r.get("rank"), "code": r.get("code"), "name": r.get("name"),
                 "total_score": r.get("total"), "signal": (r.get("signal") or {}).get("label"),
                 "pct_rank": r.get("pct_rank")}
                for r in sorted(rows, key=lambda x: (x.get("total") is None, -(x.get("total") or 0)))]

    # 6 portfolio
    portfolio = {
        "weights_pct": sizing.get("weights"), "risk_budget_pct": sizing.get("risk_budget_pct"),
        "sigma_pct": sizing.get("sigma_pct"), "cash_reserve_pct": sizing.get("cash_reserve_pct"),
        "state_multiplier": sizing.get("state_multiplier"),
        "target_total_pct": sizing.get("target_total_pct"),
        "excluded": sizing.get("excluded"), "constraints": sizing.get("constraints"),
        "order_plan": (v5.get("plan") or {}).get("plan") or [],
        "order_plan_reason": None if (v5.get("plan") or {}).get("ok") else (v5.get("plan") or {}).get("reason"),
        "configured": (v5.get("unlock") or {}).get("configured"),
        "unlock_need": (v5.get("unlock") or {}).get("need"),
        "strategies": v5.get("strategies"),
        "note": "strategies 为策略中心页所需的扩展字段（A/B/C/D 四策略最新的判定与排名）",
    }

    # 7 portfolio/risk
    risk_payload = {
        "volatility_pct": risk.get("volatility_pct"),
        "max_drawdown_pct": risk.get("max_drawdown_pct"),
        "var_95_daily_pct": risk.get("var_95_daily_pct"),
        "cvar_95_daily_pct": risk.get("cvar_95_daily_pct"),
        "downside_vol_pct": (risk.get("stats") or {}).get("downside_vol_pct"),
        "sharpe": (risk.get("stats") or {}).get("sharpe"),
        "calmar": (risk.get("stats") or {}).get("calmar"),
        "recovery_days": (risk.get("stats") or {}).get("recovery_days"),
        "concentration": risk.get("concentration"),
        "correlation_risk": risk.get("correlation_risk"),
        "risk_level": risk.get("risk_level"),
        "circuit_breaker": risk.get("circuit_breaker"),
        "single_checks": risk.get("single_checks"),
        "portfolio_checks": risk.get("portfolio_checks"),
        "warnings": risk.get("warnings"),
        "stress": {"historical": (v5.get("stress") or {}).get("historical"),
                   "extreme": (v5.get("stress") or {}).get("extreme"),
                   "monte_carlo": (v5.get("stress") or {}).get("monte_carlo"),
                   "correlation_jump": (v5.get("stress") or {}).get("correlation_jump")},
    }

    # 8 recommendations
    recs = [{"recommendation_id": d.get("recommendation_id"), "etf_code": d.get("etf_code"),
             "etf_name": d.get("etf_name"), "action": d.get("action"),
             "current_weight_pct": d.get("current_weight_pct"),
             "target_weight_pct": d.get("target_weight_pct"),
             "suggested_quantity": d.get("suggested_quantity"),
             "estimated_cost": d.get("capital_occupied"), "trade_cost": d.get("trade_cost"),
             "risk_warning": d.get("risk_warning"), "reasons": d.get("reasons"),
             "review_status": d.get("review_status"), "signal_date": d.get("signal_date"),
             "valid_until": d.get("valid_until"), "direction": d.get("direction")}
            for d in (dec.get("items") or [])]

    # 9 backtests
    backtests = []
    for code, b in (v5.get("backtest") or {}).items():
        if b.get("error"):
            backtests.append({"code": code, "error": b["error"]})
            continue
        backtests.append({"code": code, "n_days": b.get("n"), "years": b.get("years"),
                          "strategy_ann_pct": b.get("strategy_ann"),
                          "buyhold_ann_pct": b.get("buyhold_ann"),
                          "strategy_mdd_pct": b.get("strategy_mdd"),
                          "buyhold_mdd_pct": b.get("buyhold_mdd"),
                          "sharpe": b.get("sharpe"), "sortino": b.get("sortino"),
                          "calmar": b.get("calmar"), "cvar95_daily_pct": b.get("cvar95"),
                          "turnover_per_year": b.get("turnover"),
                          "cost_total_pct": b.get("cost_total_pct"),
                          "recovery": b.get("recovery"), "lookahead": b.get("lookahead"),
                          "rolling_oos": b.get("rolling_oos"),
                          "sensitivity": b.get("sensitivity")})

    # 10 data/health
    health = {"db": {"counts": db.get("counts"), "health": db.get("health"),
                     "error": db.get("error")},
              "sources": {"ok": src.get("ok"), "used": src.get("used"),
                          "by_tier": src.get("by_tier"), "failures": src.get("failures"),
                          "only_p2": src.get("only_p2"), "degraded": src.get("degraded"),
                          "notes": src.get("notes")},
              "acceptance": v5.get("acceptance")}

    # 11-13 POST schema（静态载体无法写库，给出契约与示例，供前端/本地服务调用）
    post_positions = {"schema": {"portfolio_id": "string", "holdings": [
        {"etf_code": "510300", "qty": 1000, "avg_cost": 4.10, "as_of": "YYYY-MM-DD"}],
        "cash_cny": 0}, "note": "持仓写入 portfolio_position 语义表；静态载体下仅提供契约，本地 api_server 可直接落库"}
    post_review = {"schema": {"recommendation_id": "string", "status": "approved|rejected|expired",
                              "note": "string"}, "note": "对应 decision_center.record_review()"}
    post_backtest = {"schema": {"code": "510300", "fee": 0.001, "slippage": 0.001,
                                "deep": False}, "note": "对应 backtest.run_backtest()"}

    return {
        "market_status": _envelope(market, v5, sources),
        "etfs": _envelope({"count": len(etfs), "items": etfs}, v5, sources),
        "etf_detail": _envelope({"count": len(detail), "items": detail}, v5, sources),
        "etf_signals": _envelope({"items": sigs}, v5, sources),
        "rankings": _envelope({"items": rankings}, v5, sources),
        "portfolio": _envelope(portfolio, v5, sources),
        "portfolio_risk": _envelope(risk_payload, v5, sources),
        "recommendations": _envelope({"count": len(recs), "items": recs}, v5, sources),
        "backtests": _envelope({"items": backtests}, v5, sources),
        "data_health": _envelope(health, v5, sources),
        "_post_positions_schema": _envelope(post_positions, v5, sources),
        "_post_review_schema": _envelope(post_review, v5, sources),
        "_post_backtest_schema": _envelope(post_backtest, v5, sources),
    }


def write_snapshot(snap: dict, out_dir: str) -> dict:
    """把 13 个响应写成静态 JSON（output/api/v1/），返回写入清单。"""
    api_dir = os.path.join(out_dir, "api", "v1")
    os.makedirs(api_dir, exist_ok=True)
    written = []
    for ep in ENDPOINTS:
        key = ep["key"]
        if key not in snap:
            continue
        path = os.path.join(api_dir, ep["file"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap[key], f, ensure_ascii=False, default=str)
        written.append({"path": "output/api/v1/" + key, "bytes": os.path.getsize(path),
                        "method": ep["method"], "endpoint": ep["path"]})
    # 索引文件，供前端列目录
    idx = {"api_version": MODEL_VERSION, "count": len(written),
           "endpoints": [{"method": e["method"], "path": e["path"], "file": e["file"]} for e in ENDPOINTS]}
    p = os.path.join(api_dir, "index.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)
    return {"dir": "output/api/v1", "files": written, "index": "output/api/v1/index.json"}


def self_test() -> list:
    out = []

    def chk(n, c, e=""):
        out.append({"name": n, "pass": bool(c), "extra": e})

    import tempfile
    chk("13 个端点登记", len(ENDPOINTS) == 13, str(len(ENDPOINTS)))
    chk("GET 10 + POST 3", sum(1 for e in ENDPOINTS if e["method"] == "GET") == 10
        and sum(1 for e in ENDPOINTS if e["method"] == "POST") == 3)
    fake_v5 = {"trade_date": "2026-11-07",
               "model_versions": {"factor": "f", "risk": "r"},
               "score_rows": [{"code": "510300", "name": "沪深300ETF", "total": 38.3,
                               "sub": {"trend": 10, "momentum": 20, "valuation": None,
                                       "liquidity": None, "risk": 30},
                               "signal": {"label": "谨慎持有"}, "confidence": "low",
                               "missing": ["liquidity"], "rank": 9, "quality": "合格",
                               "quality_score": 78.3, "pe_pct": 0.42, "amount20_wan": 30000}],
               "risk": {"ok": True, "volatility_pct": 9.7, "max_drawdown_pct": -7.8,
                        "risk_level": {"label": "低风险"}, "circuit_breaker": {"level": "green"}},
               "sizing": {"weights": {"510300": 10.0}, "risk_budget_pct": {"510300": 12},
                          "sigma_pct": {"510300": 16}, "cash_reserve_pct": 5,
                          "state_multiplier": 0.6, "target_total_pct": 57},
               "decisions": {"items": [{"etf_code": "510300", "etf_name": "沪深300ETF",
                                        "action": "仅观察", "valid_until": "2026-11-09",
                                        "signal_date": "2026-11-07", "reasons": ["r"],
                                        "review_status": "pending"}]},
               "db": {"counts": {"etf_daily_bar": 10}, "health": {"ok": True}},
               "data_sources": {"used": [{"id": "csindex", "tier": "P1", "calls": 5}],
                                "ok": True},
               "regime3": {"regime": "防守", "label": "防守", "total_score": 30.3,
                           "risk_multiplier": 0.6, "ok": True},
               "stress": {"historical": {"ok": True}, "extreme": {"ok": True}},
               "backtest": {"510300": {"n": 100, "strategy_ann": -0.93}},
               "acceptance": []}
    snap = build_snapshot(fake_v5, {"etfs": [{"code": "510300", "role": "core"}]})
    chk("快照覆盖 13 键", len(snap) == 13, str(len(snap)))
    chk("每个响应带信封字段",
        all(set(["data_date", "updated_at", "sources", "model_versions", "data"]) <= set(v)
            for v in snap.values()))
    chk("POST 契约在快照中可见",
        all(k in snap for k in ("_post_positions_schema", "_post_review_schema", "_post_backtest_schema")))
    with tempfile.TemporaryDirectory() as td:
        w = write_snapshot(snap, td)
        chk("写出 13 个 JSON 文件", len(w["files"]) == 13, str(len(w["files"])))
        chk("索引文件存在", os.path.exists(os.path.join(td, "api", "v1", "index.json")))
        p = os.path.join(td, "api", "v1", "etfs.json")
        with open(p, encoding="utf-8") as f:
            reloaded = json.load(f)
        chk("回读 JSON 与写入一致", reloaded["data"]["count"] == 1, str(reloaded["data"]["count"]))
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        r = self_test()
        for x in r:
            print(("PASS " if x["pass"] else "FAIL ") + x["name"] + (" | " + x["extra"] if x["extra"] else ""))
        print("%d/%d PASS" % (sum(1 for x in r if x["pass"]), len(r)))
        raise SystemExit(0 if all(x["pass"] for x in r) else 1)
