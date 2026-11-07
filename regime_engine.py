# -*- coding: utf-8 -*-
"""
市场状态引擎（v5 regime_engine）
================================
PRD 第 6 章：市场状态三档（进攻 / 中性 / 防守），输出 risk_multiplier 供风控层
调整风险预算，不直接决定买卖。

三个子分（0-100）：
  trend_score      指数收盘相对 MA20 / MA60 / MA120 的位置与斜率
  breadth_score    监测池中站上 MA60（及 MA20）的标的占比
  volatility_score 指数 20 日年化波动的历史分位（越高分越低，反向计分）

综合分 = 0.40 × trend + 0.30 × breadth + 0.30 × volatility
  ≥ 65 → 进攻（risk_multiplier 1.00）
  40-64 → 中性（0.80）
  < 40 → 防守（0.60）

输出仅供量化参考，不构成投资建议。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

MODEL_VERSION = "regime-v5-2026-11-07"
WEIGHTS = {"trend": 0.40, "breadth": 0.30, "volatility": 0.30}
BANDS = [(65, "进攻", 1.00, "趋势与广度同步走强，可按战略权重执行加仓"),
         (40, "中性", 0.80, "方向不明，维持战略权重，新增仓位减半"),
         (0, "防守", 0.60, "趋势走弱或波动抬升，只减不加，优先保留现金与防御资产")]


def _num(x):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(x)))


def _ma(closes, n):
    if closes is None or len(closes) < n:
        return None
    return float(np.mean(np.asarray(closes[-n:], dtype=float)))


def trend_part(index_close: pd.Series) -> dict:
    c = pd.Series(index_close).astype(float).dropna()
    if len(c) < 60:
        return {"score": None, "label": "数据不足", "parts": [f"指数仅 {len(c)} 条，不足 60 日"]}
    last = float(c.iloc[-1])
    ma20, ma60, ma120 = _ma(c, 20), _ma(c, 60), _ma(c, 120)
    parts, pts = [], 0.0
    # 站上均线（各 20 分）
    for name, ma in (("MA20", ma20), ("MA60", ma60), ("MA120", ma120)):
        if ma is None:
            continue
        if last > ma:
            pts += 20
            parts.append(f"收盘 {last:.4f} 站上 {name} {ma:.4f}")
        else:
            parts.append(f"收盘 {last:.4f} 跌破 {name} {ma:.4f}")
    # 均线多头排列（20 分）
    if ma20 and ma60 and ma120 and ma20 > ma60 > ma120:
        pts += 20
        parts.append("MA20 > MA60 > MA120 多头排列")
    elif ma20 and ma60 and ma20 < ma60:
        parts.append("MA20 低于 MA60，中期趋势偏弱")
    # MA20 斜率（20 分）
    if len(c) >= 26:
        prev = float(c.iloc[-6])
        slope_pct = (last - prev) / prev * 100 if prev else 0.0
        if slope_pct > 0:
            pts += min(20, 10 + slope_pct * 5)
        parts.append(f"近 5 日涨跌 {slope_pct:+.2f}%")
    return {"score": round(_clamp(pts), 1),
            "label": "强" if pts >= 70 else ("中性" if pts >= 40 else "弱"),
            "parts": parts}


def breadth_part(pool_closes: dict) -> dict:
    """监测池广度：站上 MA60 的占比 + 站上 MA20 的占比。"""
    above60 = above20 = total = 0
    detail = []
    for code, closes in (pool_closes or {}).items():
        c = pd.Series(closes).astype(float).dropna()
        if len(c) < 60:
            continue
        total += 1
        last = float(c.iloc[-1])
        ma60, ma20 = _ma(c, 60), _ma(c, 20)
        if ma60 and last > ma60:
            above60 += 1
        if ma20 and last > ma20:
            above20 += 1
        detail.append({"code": code, "above_ma60": bool(ma60 and last > ma60)})
    if not total:
        return {"score": None, "label": "数据不足", "above_ma60_pct": None,
                "above_ma20_pct": None, "n": 0, "parts": ["监测池无足够历史数据"]}
    p60 = above60 / total * 100
    p20 = above20 / total * 100
    score = _clamp(0.6 * p60 + 0.4 * p20)
    return {"score": round(score, 1),
            "label": "广" if score >= 65 else ("中性" if score >= 40 else "窄"),
            "above_ma60_pct": round(p60, 1), "above_ma20_pct": round(p20, 1),
            "n": total,
            "parts": [f"{above60}/{total} 只站上 MA60（{p60:.1f}%）",
                      f"{above20}/{total} 只站上 MA20（{p20:.1f}%）"]}


def volatility_part(index_close: pd.Series, lookback: int = 250) -> dict:
    """指数 20 日年化波动在近 1 年内的分位，越高得分越低（反向计分）。"""
    c = pd.Series(index_close).astype(float).dropna()
    if len(c) < 80:
        return {"score": None, "label": "数据不足", "vol20_pct": None,
                "vol_pct_rank": None, "parts": ["指数历史不足 80 日"]}
    ret = c.pct_change().dropna()
    vols = ret.rolling(20).std().dropna() * math.sqrt(252) * 100
    if len(vols) < 20:
        return {"score": None, "label": "数据不足", "vol20_pct": None,
                "vol_pct_rank": None, "parts": ["波动序列不足"]}
    cur = float(vols.iloc[-1])
    window = vols.tail(lookback)
    rank = float((window <= cur).mean() * 100)
    score = _clamp(100 - rank)
    return {"score": round(score, 1),
            "label": "平稳" if score >= 60 else ("偏高" if score >= 35 else "极端"),
            "vol20_pct": round(cur, 2), "vol_pct_rank": round(rank, 1),
            "parts": [f"指数 20 日年化波动 {cur:.2f}%，位于近 {len(window)} 日 {rank:.0f}% 分位"]}


def band_of(score):
    s = _num(score)
    if s is None:
        return {"regime": "数据不足", "label": "数据不足", "risk_multiplier": 0.6,
                "action": "数据异常，暂停决策（PRD 15.5）"}
    for thr, label, mult, action in BANDS:
        if s >= thr:
            return {"regime": label, "label": label, "risk_multiplier": mult, "action": action}
    return {"regime": "防守", "label": "防守", "risk_multiplier": 0.6, "action": BANDS[-1][3]}


def evaluate(index_close: pd.Series, pool_closes: dict | None = None,
             index_code: str = "000300", trade_date: str | None = None) -> dict:
    t = trend_part(index_close)
    b = breadth_part(pool_closes or {})
    v = volatility_part(index_close)
    parts = {"trend": t, "breadth": b, "volatility": v}
    dims = [(k, parts[k]["score"], WEIGHTS[k]) for k in WEIGHTS if parts[k]["score"] is not None]
    if dims:
        wsum = sum(w for _, _, w in dims)
        total = sum(s * w for _, s, w in dims) / wsum
    else:
        total = None
    band = band_of(total)
    reasons = []
    for k in ("trend", "breadth", "volatility"):
        if parts[k]["score"] is not None:
            reasons.append(f"{k} {parts[k]['score']:.0f} 分（{parts[k]['label']}）")
        reasons.extend(parts[k].get("parts") or [])
    if band["regime"] == "数据不足":
        reasons.append("指数行情缺失或不足，按 PRD 15.5 输出数据异常，不出有效信号")
    return {
        "ok": band["regime"] != "数据不足",
        "trade_date": trade_date, "index_code": index_code,
        "trend_score": t["score"], "breadth_score": b["score"], "volatility_score": v["score"],
        "total_score": None if total is None else round(total, 1),
        "regime": band["regime"], "label": band["label"],
        "risk_multiplier": band["risk_multiplier"], "action": band["action"],
        "breadth_detail": {"above_ma60_pct": b.get("above_ma60_pct"),
                           "above_ma20_pct": b.get("above_ma20_pct"), "n": b.get("n")},
        "vol20_pct": v.get("vol20_pct"), "vol_pct_rank": v.get("vol_pct_rank"),
        "reasons": reasons[:12],
        "parts": parts,
        "model_version": MODEL_VERSION,
        "note": "市场状态用于调整风险预算乘数，不等于买卖信号；数据不足时不给有效信号",
    }


def self_test() -> list:
    out = []

    def chk(name, cond, extra=""):
        out.append({"name": name, "pass": bool(cond), "extra": extra})

    n = 260
    up = pd.Series(np.linspace(1.0, 1.6, n))
    down = pd.Series(np.linspace(1.6, 1.0, n))
    flat = pd.Series(np.ones(n))
    a = evaluate(up, {"510300": up, "515180": up}, trade_date="2026-11-06")
    chk("上涨趋势→进攻", a["regime"] == "进攻" and a["risk_multiplier"] == 1.0, a["regime"])
    b = evaluate(down, {"510300": down, "515180": down})
    chk("下跌趋势→防守", b["regime"] == "防守" and b["risk_multiplier"] == 0.6, b["regime"])
    c = evaluate(flat, {"510300": flat})
    chk("横盘→中性或防守", c["regime"] in ("中性", "防守"), c["regime"])
    d = evaluate(pd.Series([1.0]), {})
    chk("数据不足→不 ok", d["ok"] is False and d["regime"] == "数据不足", d["regime"])
    chk("三档乘数唯一", {x["risk_multiplier"] for x in [a, b]} == {1.0, 0.6})
    chk("理由可追溯", len(a["reasons"]) >= 3, str(len(a["reasons"])))
    return out


if __name__ == "__main__":
    if len(__import__("sys").argv) > 1 and __import__("sys").argv[1] == "selftest":
        r = self_test()
        for x in r:
            print(("PASS " if x["pass"] else "FAIL ") + x["name"] + (" | " + x["extra"] if x["extra"] else ""))
        print("%d/%d PASS" % (sum(1 for x in r if x["pass"]), len(r)))
        raise SystemExit(0 if all(x["pass"] for x in r) else 1)
