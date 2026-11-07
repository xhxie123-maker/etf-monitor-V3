# -*- coding: utf-8 -*-
"""
压力测试引擎（v5 stress）
=========================
PRD 第 7 章要求六类压力情景，且必须同时覆盖「历史情景 + 极端情景 + 蒙特卡洛」，
并明确不得只依赖正态假设。

六类情景（code）：
  equity_crash            股市快速下跌
  rate_shock              利率大幅上升
  fx_shock                汇率剧烈波动
  gold_equity_down        黄金与股票同跌
  liquidity_dry           流动性枯竭
  correlation_jump        多资产相关性突升

三层方法：
  1) 历史情景：在真实联合收益序列上滑动窗口，取组合最差的若干 20 日窗口（含日期，可复核）
  2) 极端情景：按资产类别给定冲击（可配置），叠加流动性折价与相关性突升
  3) 蒙特卡洛：三套生成器并排 —— 多元正态、多元 t（厚尾）、历史分块 bootstrap（保留厚尾与自相关），
     输出 20 日损益的 5% / 1% 分位与 CVaR，用于对照正态假设会低估多少尾部风险

输出仅为风险度量，不构成投资建议。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

MODEL_VERSION = "stress-v5-2026-11-07"
HORIZON = 20

SCENARIOS = [
    {"code": "equity_crash", "name": "股市快速下跌",
     "desc": "权益类 20 日内急跌，防御资产小幅正收益", "shock": {"core_equity": -22, "style_satellite": -26, "diversifier": 4, "defensive": 1}},
    {"code": "rate_shock", "name": "利率大幅上升",
     "desc": "长端利率快速上行，债券与高股息承压，权益估值收缩", "shock": {"core_equity": -12, "defensive": -6, "style_satellite": -14, "diversifier": -8}},
    {"code": "fx_shock", "name": "汇率剧烈波动",
     "desc": "人民币快速波动，外资流出，出口链与黄金分化", "shock": {"core_equity": -10, "style_satellite": -12, "defensive": -2, "diversifier": 6}},
    {"code": "gold_equity_down", "name": "黄金与股票同跌",
     "desc": "流动性收缩下避险资产同跌，传统对冲失效", "shock": {"core_equity": -15, "style_satellite": -18, "diversifier": -12, "defensive": -3}},
    {"code": "liquidity_dry", "name": "流动性枯竭",
     "desc": "成交萎缩、买卖价差走阔，冲击成本放大", "shock": {"core_equity": -8, "style_satellite": -18, "diversifier": -10, "defensive": -4},
     "extra_cost_pct": 0.8},
    {"code": "correlation_jump", "name": "多资产相关性突升",
     "desc": "相关性趋近 1，分散化失效，按等风险预算重叠计", "shock": {"core_equity": -13, "style_satellite": -15, "diversifier": -6, "defensive": -2},
     "corr_to": 0.9},
]


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def _weights_map(weights: dict) -> dict:
    return {k: float(v) for k, v in (weights or {}).items() if _num(v) is not None}


def _class_of(pool: list) -> dict:
    m = {}
    for e in (pool or []):
        m[e.get("code")] = e.get("asset_class") or "style_satellite"
    return m


# ---------------------------------------------------------------- 历史情景
def historical(port_ret: pd.Series, horizon: int = HORIZON, top: int = 3) -> dict:
    s = pd.Series(port_ret).dropna()
    if len(s) < horizon + 5:
        return {"ok": False, "reason": f"组合收益仅 {len(s)} 日，不足 {horizon} 日窗口"}
    roll = (1 + s).rolling(horizon).apply(np.prod, raw=True) - 1
    roll = roll.dropna()
    worst = roll.nsmallest(top)
    out = []
    for idx, val in worst.items():
        start = s.index[max(0, len(s) - len(s.loc[:idx]) + 0)]
        out.append({"end_date": str(idx)[:10],
                    "loss_pct": round(float(val) * 100, 2),
                    "window_days": horizon})
    return {"ok": True, "horizon": horizon, "worst": out,
            "n_windows": int(len(roll)),
            "note": f"在 {len(s)} 个交易日上滑动 {horizon} 日窗口取最差 {top} 段（真实联合收益，非假设）"}


# ---------------------------------------------------------------- 极端情景
def extreme(weights: dict, class_map: dict, scenarios: list | None = None) -> dict:
    w = _weights_map(weights)
    total = sum(w.values()) or 1.0
    rows = []
    for sc in (scenarios or SCENARIOS):
        loss = 0.0
        detail = []
        for code, weight in w.items():
            cls = class_map.get(code, "style_satellite")
            shock = float(sc["shock"].get(cls, sc["shock"].get("style_satellite", 0)))
            contrib = weight / total * shock
            loss += contrib
            detail.append({"code": code, "asset_class": cls, "weight_pct": round(weight / total * 100, 2),
                           "shock_pct": shock, "contrib_pct": round(contrib, 2)})
        extra = float(sc.get("extra_cost_pct", 0) or 0)
        total_loss = loss - extra
        rows.append({"code": sc["code"], "name": sc["name"], "desc": sc["desc"],
                     "loss_pct": round(total_loss, 2),
                     "shock_loss_pct": round(loss, 2),
                     "extra_cost_pct": extra,
                     "detail": detail})
    worst = min(rows, key=lambda r: r["loss_pct"]) if rows else None
    return {"ok": True, "items": rows, "worst": worst,
            "note": "极端情景为一次性冲击假设，用于比较相对暴露，不是预测"}


# ---------------------------------------------------------------- 蒙特卡洛
def _simulate(gen: str, rets: np.ndarray, n_sims: int, horizon: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n, k = rets.shape
    mu = rets.mean(axis=0)
    cov = np.cov(rets, rowvar=False)
    if k == 1:
        cov = np.array([[float(np.var(rets, ddof=1))]])
    cov = np.atleast_2d(cov)
    # 数值稳定：加微小对角，避免奇异
    cov = cov + np.eye(k) * 1e-10
    if gen == "normal":
        draws = rng.multivariate_normal(mu, cov, size=(n_sims, horizon))
        return draws.sum(axis=1)
    if gen == "t":
        # 多元 t：df=4，尺度矩阵按 df/(df-2) 校正，保证协方差一致
        df = 4.0
        L = np.linalg.cholesky(cov * (df - 2) / df)
        z = rng.standard_normal((n_sims, horizon, k))
        chi = rng.chisquare(df, size=(n_sims, horizon, 1))
        t = z / np.sqrt(chi / df)
        draws = mu + t @ L.T
        return draws.sum(axis=1)
    if gen == "bootstrap":
        # 历史分块 bootstrap：块长 5 日，保留厚尾与短期自相关
        block = 5
        n_blocks = int(math.ceil(horizon / block))
        starts = rng.integers(0, max(1, n - block), size=(n_sims, n_blocks))
        idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n_sims, -1)[:, :horizon]
        idx = np.clip(idx, 0, n - 1)
        draws = rets[idx]
        return draws.sum(axis=1)
    raise ValueError(gen)


def monte_carlo(returns: pd.DataFrame, weights: dict, n_sims: int = 5000,
                horizon: int = HORIZON, seed: int = 42) -> dict:
    w = _weights_map(weights)
    cols = [c for c in (returns.columns if returns is not None else []) if c in w]
    if not cols or len(returns) < 60:
        return {"ok": False, "reason": "共同交易日不足 60 或权重无交集"}
    sub = returns[cols].dropna()
    if len(sub) < 60:
        return {"ok": False, "reason": f"共同交易日仅 {len(sub)} 日"}
    wv = np.array([w[c] for c in cols], dtype=float)
    wv = wv / wv.sum()
    rets = sub.values
    port = rets @ wv
    out = {"ok": True, "n_days": int(len(sub)), "horizon": horizon, "n_sims": n_sims,
           "weight_used": {c: round(float(x) * 100, 2) for c, x in zip(cols, wv)}}
    gens = {}
    for g in ("normal", "t", "bootstrap"):
        sims = _simulate(g, rets, n_sims, horizon, seed + {"normal": 0, "t": 1, "bootstrap": 2}[g])
        q05, q01 = np.percentile(sims, 5), np.percentile(sims, 1)
        cvar05 = float(sims[sims <= q05].mean()) if (sims <= q05).any() else q05
        gens[g] = {"p05_pct": round(float(q05) * 100, 2),
                   "p01_pct": round(float(q01) * 100, 2),
                   "cvar05_pct": round(cvar05 * 100, 2),
                   "median_pct": round(float(np.median(sims)) * 100, 2),
                   "p95_pct": round(float(np.percentile(sims, 95)) * 100, 2),
                   "p_loss_gt_10_pct": round(float((sims <= -0.10).mean()) * 100, 2)}
    hist_vol = float(np.std(port, ddof=1) * math.sqrt(252) * 100)
    normal_05 = gens["normal"]["p05_pct"]
    tail_gap = round(gens["bootstrap"]["p05_pct"] - normal_05, 2)
    out["generators"] = gens
    out["hist_vol_ann_pct"] = round(hist_vol, 2)
    out["tail_gap_pct"] = tail_gap
    out["verdict"] = ("历史 bootstrap 5% 分位比正态低 " + f"{abs(tail_gap):.2f} 个百分点，正态假设低估尾部"
                      if tail_gap < 0 else "本次样本下正态未明显低估尾部，但仍需以 bootstrap 为准")
    out["note"] = "三种生成器并排对照；正态假设仅作基准，结论以 bootstrap 与 t 分布为准（PRD 第 7 章）"
    return out


# ---------------------------------------------------------------- 相关性突升
def correlation_jump(returns: pd.DataFrame, weights: dict, target_corr: float = 0.9) -> dict:
    w = _weights_map(weights)
    cols = [c for c in (returns.columns if returns is not None else []) if c in w]
    if len(cols) < 2 or len(returns) < 60:
        return {"ok": False, "reason": "标的不足或样本不足"}
    sub = returns[cols].dropna()
    corr = sub.corr()
    vols = sub.std() * math.sqrt(252) * 100
    wv = np.array([w[c] for c in cols], dtype=float)
    wv = wv / wv.sum()
    # 原相关下的组合波动 vs 相关系数全部提到 target 后的组合波动
    sig = vols.values / 100.0
    cov_now = np.outer(sig, sig) * corr.values
    cov_jump = np.outer(sig, sig) * np.full((len(cols), len(cols)), target_corr)
    np.fill_diagonal(cov_jump, sig ** 2)
    vol_now = float(math.sqrt(wv @ cov_now @ wv) * 100)
    vol_jump = float(math.sqrt(wv @ cov_jump @ wv) * 100)
    return {"ok": True, "avg_corr": round(float(corr.values[np.triu_indices(len(cols), 1)].mean()), 3),
            "vol_now_pct": round(vol_now, 2), "vol_jump_pct": round(vol_jump, 2),
            "vol_inflation_pct": round((vol_jump / vol_now - 1) * 100, 1) if vol_now else None,
            "target_corr": target_corr,
            "note": "把两两相关系数统一提到目标水平后的组合波动膨胀，用于检验分散化失效"}


def run(returns: pd.DataFrame, weights: dict, pool: list,
        scenarios: list | None = None, n_sims: int = 5000, seed: int = 42) -> dict:
    w = _weights_map(weights)
    class_map = _class_of(pool)
    cols = [c for c in (returns.columns if returns is not None else []) if c in w]
    port = None
    if cols and len(returns) >= 30:
        wv = np.array([w[c] for c in cols], dtype=float)
        sub = returns[cols].dropna()
        if len(sub) >= 30:
            port = pd.Series(sub.values @ (wv / wv.sum()), index=sub.index)
    hist = historical(port) if port is not None else {"ok": False, "reason": "无组合收益序列"}
    ext = extreme(w, class_map, scenarios)
    mc = monte_carlo(returns, w, n_sims=n_sims)
    cj = correlation_jump(returns, w, target_corr=0.9)
    return {"ok": True, "historical": hist, "extreme": ext, "monte_carlo": mc,
            "correlation_jump": cj, "horizon": HORIZON,
            "n_scenarios": len(ext.get("items") or []),
            "model_version": MODEL_VERSION,
            "disclaimer": "压力测试结果是情景假设下的损失估计，不是对未来收益的预测，仅供参考，不构成投资建议"}


def self_test() -> list:
    out = []

    def chk(name, cond, extra=""):
        out.append({"name": name, "pass": bool(cond), "extra": extra})

    rng = np.random.default_rng(7)
    n = 500
    a = rng.normal(0.0004, 0.012, n)
    b = 0.5 * a + rng.normal(0.0002, 0.010, n)
    c = rng.normal(0.0003, 0.006, n)
    # 制造一段剧烈下跌，验证历史最差窗口能抓到
    a[300:320] -= 0.012
    rets = pd.DataFrame({"510300": a, "515180": b, "511010": c},
                        index=pd.date_range("2024-01-01", periods=n))
    w = {"510300": 50, "515180": 30, "511010": 20}
    pool = [{"code": "510300", "asset_class": "core_equity"},
            {"code": "515180", "asset_class": "style_satellite"},
            {"code": "511010", "asset_class": "defensive"}]
    r = run(rets, w, pool, n_sims=2000)
    chk("历史情景有最差窗口", r["historical"]["ok"] and len(r["historical"]["worst"]) >= 1,
        str(r["historical"].get("worst", [])[:1]))
    chk("极端情景 6 类", len(r["extreme"]["items"]) == 6, str(len(r["extreme"]["items"])))
    chk("极端情景含损失明细", all(i["detail"] for i in r["extreme"]["items"]))
    g = r["monte_carlo"]["generators"]
    chk("三种生成器齐备", set(g) == {"normal", "t", "bootstrap"}, str(sorted(g)))
    chk("bootstrap 5% 分位不高于正态", g["bootstrap"]["p05_pct"] <= g["normal"]["p05_pct"] + 0.5,
        f"{g['bootstrap']['p05_pct']} vs {g['normal']['p05_pct']}")
    chk("CVaR 比 VaR 更差", g["bootstrap"]["cvar05_pct"] <= g["bootstrap"]["p05_pct"],
        f"{g['bootstrap']['cvar05_pct']} / {g['bootstrap']['p05_pct']}")
    chk("相关性突升使波动上升", r["correlation_jump"]["vol_jump_pct"] > r["correlation_jump"]["vol_now_pct"],
        f"{r['correlation_jump']['vol_now_pct']} → {r['correlation_jump']['vol_jump_pct']}")
    chk("情景损失均为负", all(i["loss_pct"] < 0 for i in r["extreme"]["items"]))
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        r = self_test()
        for x in r:
            print(("PASS " if x["pass"] else "FAIL ") + x["name"] + (" | " + x["extra"] if x["extra"] else ""))
        print("%d/%d PASS" % (sum(1 for x in r if x["pass"]), len(r)))
        raise SystemExit(0 if all(x["pass"] for x in r) else 1)
