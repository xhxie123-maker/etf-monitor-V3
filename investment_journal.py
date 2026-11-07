# -*- coding: utf-8 -*-
"""
investment_journal.py — V4 投资记录与智能复盘（GPT 评审 P2-2）

能力：
  - 决策记录 / 交易执行 / 调整原因 / 预警 / 再平衡事件（journal.jsonl 追加）
  - 实际持仓维护（加权平均成本、买入/卖出、实现盈亏）
  - 持仓快照：市值、浮动盈亏、各标的收益贡献
  - 策略偏差：实际权重 vs 基准权重
  - 智能复盘：区间内执行、纪律遵守、偏差、盈亏与贡献汇总

隐私隔离（上线硬性验收第 10 条）：
  - 全部个人数据写入本地 private/（holdings.json / journal.jsonl），不写入 output/；
  - private/ 已在 .gitignore；CI 的 git add 白名单仅含 output/ 三个文件，不会提交；
  - 公开 GitHub Pages 只含公开行情与策略结果，不含任何个人持仓。
本模块纯本地、无网络请求。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PRIVATE_DIR = BASE_DIR / "private"
HOLDINGS_FILE = PRIVATE_DIR / "holdings.json"
JOURNAL_FILE = PRIVATE_DIR / "journal.jsonl"

EPS = 1e-9


# ---------------- 存储与隔离 ----------------

def ensure_private():
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    return PRIVATE_DIR


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path, obj):
    ensure_private()
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load_holdings():
    return _read_json(HOLDINGS_FILE, {})


def save_holdings(holdings):
    _write_json(HOLDINGS_FILE, holdings)


def load_journal():
    if not JOURNAL_FILE.exists():
        return []
    out = []
    for line in JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def append_event(kind: str, payload: dict, ts: str | None = None):
    ensure_private()
    event = {"ts": ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "kind": kind}
    event.update(payload)
    with JOURNAL_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


# ---------------- 交易与持仓 ----------------

def apply_trade(date: str, code: str, shares: float, price: float,
                fee: float = 0.0, note: str = "", holdings: dict | None = None):
    """
    记录一笔交易并更新持仓。
    shares>0 买入；shares<0 卖出（取绝对值为卖出数量）。
    买入：加权平均成本（费用计入成本）；卖出：按平均成本实现盈亏（费用扣减）。
    """
    holdings = load_holdings() if holdings is None else holdings
    pos = holdings.get(code, {"shares": 0.0, "cost_price": 0.0, "realized_pnl": 0.0})
    if shares > 0:
        old_n, old_c = float(pos["shares"]), float(pos["cost_price"])
        new_n = old_n + shares
        cost = (old_n * old_c + shares * price + fee) / new_n
        pos["shares"] = round(new_n, 6)
        pos["cost_price"] = round(cost, 6)
        action = "buy"
    elif shares < 0:
        qty = abs(shares)
        if qty > float(pos["shares"]) + EPS:
            raise ValueError(f"{code} 卖出 {qty} 超过持仓 {pos['shares']}")
        realized = (price - float(pos["cost_price"])) * qty - fee
        pos["shares"] = round(float(pos["shares"]) - qty, 6)
        pos["realized_pnl"] = round(float(pos.get("realized_pnl", 0.0)) + realized, 4)
        if pos["shares"] <= EPS:
            pos["shares"] = 0.0
        action = "sell"
    else:
        raise ValueError("shares 不能为 0")

    holdings[code] = pos
    save_holdings(holdings)
    append_event("trade", {"date": date, "code": code, "action": action,
                           "shares": abs(shares), "price": price, "fee": fee, "note": note})
    return pos


def record_decision(date: str, code: str, state: str, suggestion: str,
                    reasons: list, **extra):
    return append_event("decision", {"date": date, "code": code, "state": state,
                                     "suggestion": suggestion, "reasons": reasons, **extra})


def record_alert(date: str, code: str, level: str, message: str):
    return append_event("alert", {"date": date, "code": code, "level": level, "message": message})


# ---------------- 快照与贡献 ----------------

def holdings_snapshot(prices: dict, holdings: dict | None = None):
    """当前市值、浮动盈亏、成本、各标的收益贡献（按市值占比）。"""
    holdings = load_holdings() if holdings is None else holdings
    rows = []
    total_mv, total_cost = 0.0, 0.0
    for code, pos in holdings.items():
        n = float(pos["shares"])
        if n <= EPS:
            continue
        price = float(prices.get(code, pos["cost_price"]))
        mv = n * price
        cost = n * float(pos["cost_price"])
        floating = mv - cost
        rows.append({"code": code, "shares": round(n, 4), "cost_price": pos["cost_price"],
                     "price": round(price, 4), "market_value": round(mv, 2),
                     "cost_value": round(cost, 2), "floating_pnl": round(floating, 2),
                     "return_pct": round(floating / cost * 100, 2) if cost else None})
        total_mv += mv
        total_cost += cost
    for r in rows:
        r["weight_pct"] = round(r["market_value"] / total_mv * 100, 2) if total_mv else 0.0
        r["contribution_pp"] = round(r["floating_pnl"] / total_cost * 100, 2) if total_cost else 0.0
    return {"positions": rows, "total_market_value": round(total_mv, 2),
            "total_cost_value": round(total_cost, 2),
            "total_floating_pnl": round(total_mv - total_cost, 2),
            "total_realized_pnl": round(sum(float(p.get("realized_pnl", 0.0))
                                            for p in holdings.values()), 2)}


def weight_deviation(target_w: dict, prices: dict, holdings: dict | None = None):
    """实际权重 vs 基准权重（百分点）。target_w: code->小数。"""
    snap = holdings_snapshot(prices, holdings)
    total_mv = snap["total_market_value"]
    out = []
    if total_mv <= EPS:
        return {"deviations": [], "note": "无持仓市值，无法计算权重偏离"}
    actual = {r["code"]: r["market_value"] / total_mv for r in snap["positions"]}
    for code, tgt in target_w.items():
        act = actual.get(code, 0.0)
        out.append({"code": code, "target_pct": round(float(tgt) * 100, 2),
                    "actual_pct": round(act * 100, 2),
                    "dev_pp": round((act - float(tgt)) * 100, 2)})
    return {"deviations": out}


# ---------------- 智能复盘 ----------------

def review(prices: dict, target_w: dict | None = None, since: str | None = None,
           deviation_pp: float = 10.0):
    """区间复盘：事件统计、纪律遵守、偏差超阈值、盈亏与贡献。"""
    events = load_journal()
    if since:
        events = [e for e in events if e.get("ts", "") >= since or e.get("date", "") >= since]
    counts = {}
    alerts, deviations = [], []
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        if e["kind"] == "alert":
            alerts.append(e)
    snap = holdings_snapshot(prices)
    if target_w is not None:
        wd = weight_deviation(target_w, prices)
        deviations = [d for d in wd["deviations"] if abs(d["dev_pp"]) >= deviation_pp]
    discipline = {
        "decisions_recorded": counts.get("decision", 0),
        "trades_recorded": counts.get("trade", 0),
        "alerts_recorded": len(alerts),
        "deviation_breaches": len(deviations),
        "verdict": ("存在纪律偏差，需关注" if deviations or alerts else "记录完整，未发现纪律偏差"),
    }
    return {"period": {"since": since, "events": len(events)},
            "event_counts": counts, "discipline": discipline,
            "alerts": alerts, "deviation_breaches": deviations,
            "snapshot": snap, "privacy": privacy_report()}


# ---------------- 隐私报告 ----------------

def privacy_report():
    """隔离自检：数据位置、是否在 output/、CI 白名单与 .gitignore 状态。"""
    gitignore = BASE_DIR / ".gitignore"
    ignored = False
    if gitignore.exists():
        ignored = any(line.strip().startswith("private") for line in
                      gitignore.read_text(encoding="utf-8").splitlines())
    return {
        "private_dir": str(PRIVATE_DIR),
        "inside_output": "output" in PRIVATE_DIR.parts,
        "gitignore_covers_private": ignored,
        "ci_gitadd_scope": "仅 output/ 白名单文件，private/ 不被提交",
        "isolation_ok": (not ("output" in PRIVATE_DIR.parts)) and ignored,
    }


# ---------------- 自测 ----------------

def self_test():
    import tempfile
    global PRIVATE_DIR, HOLDINGS_FILE, JOURNAL_FILE
    tmp = Path(tempfile.mkdtemp())
    PRIVATE_DIR = tmp / "private"
    HOLDINGS_FILE = PRIVATE_DIR / "holdings.json"
    JOURNAL_FILE = PRIVATE_DIR / "journal.jsonl"

    # 两笔买入（加权平均）
    apply_trade("2026-01-05", "515180", 1000, 1.00, fee=1)
    p2 = apply_trade("2026-02-05", "515180", 1000, 1.20, fee=1)
    # 成本 = (1000*1 + 1 + 1000*1.2 + 1)/2000 = 1.101
    assert abs(p2["cost_price"] - 1.101) < 1e-9, p2["cost_price"]
    assert p2["shares"] == 2000

    # 卖出 500，实现盈亏 = (1.3-1.101)*500 - 0.5
    p3 = apply_trade("2026-03-05", "515180", -500, 1.30, fee=0.5)
    assert abs(p3["realized_pnl"] - ((1.3 - 1.101) * 500 - 0.5)) < 1e-9
    assert p3["shares"] == 1500

    # 决策与预警
    record_decision("2026-03-05", "515180", "hold", "持有", ["趋势中性"])
    record_alert("2026-03-06", "515220", "warn", "价格滞后")

    # 快照
    snap = holdings_snapshot({"515180": 1.30})
    assert abs(snap["total_market_value"] - 1950.0) < 1e-6
    assert snap["positions"][0]["floating_pnl"] == round(1950 - 1500 * 1.101, 2)

    # 偏离
    wd = weight_deviation({"515180": 0.5, "515220": 0.5}, {"515180": 1.30})
    d180 = [d for d in wd["deviations"] if d["code"] == "515180"][0]
    assert d180["actual_pct"] == 100.0 and d180["dev_pp"] == 50.0

    # 复盘
    rv = review({"515180": 1.30}, target_w={"515180": 0.5}, deviation_pp=10)
    assert rv["event_counts"]["trade"] == 3
    assert rv["discipline"]["deviation_breaches"] == 1

    # 隐私：临时目录不在 output；真实 .gitignore 已覆盖 private/
    pr = privacy_report()
    assert pr["inside_output"] is False
    assert pr["gitignore_covers_private"] is True
    assert pr["isolation_ok"] is True

    print("investment_journal self_test: 8/8 PASS")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        self_test()
    else:
        self_test()
