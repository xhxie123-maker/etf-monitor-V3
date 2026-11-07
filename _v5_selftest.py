# -*- coding: utf-8 -*-
"""v5_layer 离线自检：用 output/ 已落盘的历史行情跑通 build()+render()。
不发网络请求，仅验证分层编排、SQLite 落库、回测调用、HTML 渲染与 JSON 可序列化。
"""
import json
import os
import sys

import pandas as pd

import v5_layer
import smart_db as sdb

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")
cfg = json.load(open(os.path.join(HERE, "portfolio_config.json"), encoding="utf-8"))

price_map, rets = {}, {}
for e in cfg["etfs"]:
    code = e["code"]
    fp = os.path.join(OUT, f"price_history_{code}.csv")
    if not os.path.exists(fp):
        print(f"[skip] {code} 无历史文件")
        continue
    df = pd.read_csv(fp)
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    # 复刻主流程 calc_indicators 的均线列，供五维趋势与独立核算校验使用
    for n in (20, 60, 120, 250):
        df[f"ma{n}"] = df["close"].rolling(n).mean()
    price_map[code] = df
    rets[code] = df["close"].pct_change()

rets_df = pd.DataFrame(rets).dropna(how="all")
# 已知 V4.1 质量分，用于验证质量分层与流动性闸门
q_known = {"510300": 78.3, "512050": 100, "511010": 70, "511880": 100,
           "518880": 85, "515180": 70, "516310": 82.5, "515220": 70,
           "159201": 82.5, "563020": 82.5}
quality = [{"code": c, "quality_score": s, "metrics": {"avg_amount_wan": None,
           "spread_pct": None, "premium_pct": None}} for c, s in q_known.items()]

ctx = {
    "config": cfg,
    "etfs": [],
    "price_map_full": price_map,
    "price_rets": rets_df.to_dict(),
    "quality": quality,
    "regime": {"regime": "正常", "risk_multiplier": 1.0, "reasons": ["离线自检"]},
    "main_date": "2026-11-06",
    "fee": 0.001, "slippage": 0.001,
    "out_dir": OUT,
    "ok_em": False, "holdings": {},
    "src_stats": {"ok_em": 0, "ok_tx": 6, "ok_sina": 4, "ok_cs": 5, "fail_em": 2},
}

res = v5_layer.build(ctx)
print("== build ok ==")
print("version:", res.get("version"), "| trade_date:", res.get("trade_date"))
print("评分条数:", len(res.get("score_rows") or []))
for r in res.get("score_rows") or []:
    print("  %-7s %-14s total=%-6s %-6s rank=%-3s conf=%-4s 质量=%s 缺失=%s" % (
        r["code"], (r.get("name") or "")[:12], r.get("total"),
        (r.get("signal") or {}).get("label"), r.get("rank"), r.get("confidence"),
        r.get("quality"), ",".join(r.get("missing") or []) or "-"))
print("风险:", (res.get("risk") or {}).get("risk_level"), "| ok=", (res.get("risk") or {}).get("ok"),
      "| 波动=", (res.get("risk") or {}).get("volatility_pct"),
      "| MDD=", (res.get("risk") or {}).get("max_drawdown_pct"),
      "| 熔断=", ((res.get("risk") or {}).get("circuit_breaker") or {}).get("level"))
print("sizing ok=", (res.get("sizing") or {}).get("ok"), "weights=", (res.get("sizing") or {}).get("weights"))
print("plan:", (res.get("plan") or {}).get("reason") or (res.get("plan") or {}).get("ok"))
d = res.get("decisions") or {}
print("决策: total=%s add=%s reduce=%s paused=%s configured=%s" % (
    d.get("n_total"), d.get("n_add"), d.get("n_reduce"), d.get("n_paused"),
    d.get("risk_tolerance_configured")))
for it in (d.get("items") or [])[:4]:
    print("   ", it.get("etf_code"), it.get("action"), "|", (it.get("reasons") or [""])[0][:60])
print("db:", (res.get("db") or {}).get("ok"), (res.get("db") or {}).get("error"),
      (res.get("db") or {}).get("bt_error"),
      "| counts:", (res.get("db") or {}).get("counts"))
print("health:", (res.get("db") or {}).get("health"))
print("回测:", {k: (v.get("strategy_ann") if not v.get("error") else v["error"]) for k, v in (res.get("backtest") or {}).items()})
print("xcheck:", json.dumps(res.get("xcheck") or {}, ensure_ascii=False)[:400])
r3 = res.get("regime3") or {}
print("regime3:", r3.get("regime"), r3.get("total_score"), "mult", r3.get("risk_multiplier"),
      "| trend/breadth/vol", r3.get("trend_score"), r3.get("breadth_score"), r3.get("volatility_score"),
      "| 站上MA60", (r3.get("breadth_detail") or {}).get("above_ma60_pct"))
s = res.get("strategies") or {}
print("A:", (s.get("A") or {}).get("counts"), "| B pass", (s.get("B") or {}).get("n_pass"),
      "| C selected", (s.get("C") or {}).get("selected"),
      "| D", (s.get("D") or {}).get("invested_pct"), (s.get("D") or {}).get("cash_pct"))
sxr = res.get("stress") or {}
print("stress ok:", sxr.get("ok"), "| 极端最差:", (sxr.get("extreme") or {}).get("worst", {}).get("name"),
      (sxr.get("extreme") or {}).get("worst", {}).get("loss_pct"),
      "| 历史最差:", ((sxr.get("historical") or {}).get("worst") or [{}])[0])
g = (sxr.get("monte_carlo") or {}).get("generators") or {}
print("MC:", {k: (v["p05_pct"], v["cvar05_pct"]) for k, v in g.items()})
print("corr jump:", (sxr.get("correlation_jump") or {}).get("vol_now_pct"),
      "→", (sxr.get("correlation_jump") or {}).get("vol_jump_pct"))
ds = res.get("data_sources") or {}
print("sources ok:", ds.get("ok"), "only_p2", ds.get("only_p2"), "degraded", ds.get("degraded"),
      "| used", [u["id"] for u in ds.get("used") or []])
print("api snapshot:", res.get("api"))
ap = os.path.join(OUT, "api", "v1")
print("api files:", len(os.listdir(ap)) if os.path.isdir(ap) else 0,
      "| index.json", os.path.exists(os.path.join(ap, "index.json")))
print("验收:")
for a in res.get("acceptance") or []:
    print("  %2s %-22s %-8s %s" % (a["no"], a["item"], a["status"], a.get("evidence", "")[:80]))

# JSON 可序列化（看板 payload 用 json.dumps 不带 default）
s = json.dumps(res, ensure_ascii=False)
print("json.dumps OK, bytes:", len(s))

html = v5_layer.render(res)
print("html bytes:", len(html))
for k in ["投资驾驶舱", "市场状态三档", "五维评分", "四级风控", "压力测试", "风险预算",
          "四策略中心", "交易决策", "回测验证", "数据层", "数据源分级", "验收"]:
    print("  包含 %-8s :" % k, k in html)

dbp = os.path.join(OUT, "smart_etf.db")
print("db file:", os.path.exists(dbp), os.path.getsize(dbp) if os.path.exists(dbp) else 0)
print("ALL OK")
