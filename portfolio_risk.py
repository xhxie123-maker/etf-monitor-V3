# -*- coding: utf-8 -*-
"""
portfolio_risk.py — V4 组合级风险引擎与六情景压力测试（GPT 评审 P1-3）

输入：多只 ETF 对齐收盘价 + 权重 + 配置。
指标：
- 60/120/250 日滚动相关性（最新窗口矩阵 + 全期均值）
- 组合年化波动率、组合 MDD、下行波动率、CVaR95
- 行业/资产类别集中度（HHI）
- 单一资产风险贡献（基于协方差分解）
- 六情景压力测试（按资产类别/单代码冲击，组合损失 = Σ w·shock）

口径：风险基于日频收益率，年化乘 sqrt(252)；组合净值按固定权重（每日再平衡简化）。
所有数值可由输入复算；缺失数据不臆造。
"""
import math

TRADING_DAYS = 252


# ---------------- 数据对齐 ----------------

def align_prices(price_map):
    """price_map: code -> DataFrame(date,close)。返回对齐后的收盘价 DataFrame（inner join）。"""
    frames = []
    for code, df in price_map.items():
        d = df[["date", "close"]].copy()
        d["date"] = d["date"].astype(str)
        d = d.rename(columns={"close": code})[["date", code]]
        frames.append(d)
    if not frames:
        return None
    out = frames[0]
    for d in frames[1:]:
        out = out.merge(d, on="date", how="inner")
    return out.sort_values("date").reset_index(drop=True)


def returns_matrix(price_df):
    ret = price_df.set_index("date").pct_change().dropna()
    return ret


# ---------------- 相关性 ----------------

def corr_windows(ret_df, windows=(60, 120, 250)):
    out = {}
    for w in windows:
        sub = ret_df.tail(w)
        if len(sub) >= max(20, w // 3):
            c = sub.corr()
            out[str(w)] = {
                "days": len(sub),
                "matrix": _round_df(c),
                "avg_offdiag": _avg_offdiag(c),
            }
    return out


def _round_df(df, n=3):
    return {a: {b: (None if math.isnan(df.loc[a, b]) else round(float(df.loc[a, b]), n))
                for b in df.columns} for a in df.index}


def _avg_offdiag(corr):
    vals = []
    cols = list(corr.columns)
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = corr.loc[cols[i], cols[j]]
            if not math.isnan(v):
                vals.append(float(v))
    return round(sum(vals) / len(vals), 3) if vals else None


# ---------------- 组合统计 ----------------

def weighted_returns(ret_df, weights):
    """weights: code -> 权重(小数)。仅用 ret_df 中存在且权重>0 的列。"""
    codes = [c for c in ret_df.columns if weights.get(c, 0) > 0]
    w = {c: weights[c] for c in codes}
    total = sum(w.values())
    rp = []
    for _, row in ret_df[codes].iterrows():
        r = 0.0
        for c in codes:
            v = row[c]
            if v == v:
                r += float(v) * (w[c] / total)
        rp.append(r)
    return rp, codes


def ann_vol(rp):
    if len(rp) < 2:
        return None
    mean = sum(rp) / len(rp)
    var = sum((x - mean) ** 2 for x in rp) / (len(rp) - 1)
    return round(math.sqrt(var) * math.sqrt(TRADING_DAYS) * 100, 3)


def downside_vol(rp, target=0.0):
    neg = [(x - target) ** 2 for x in rp if x < target]
    if not neg:
        return 0.0
    return round(math.sqrt(sum(neg) / len(neg)) * math.sqrt(TRADING_DAYS) * 100, 3)


def equity_curve_mdd(rp):
    nav = 1.0
    peak = 1.0
    mdd = 0.0
    curve = []
    for r in rp:
        nav *= (1 + r)
        curve.append(nav)
        peak = max(peak, nav)
        dd = nav / peak - 1
        mdd = min(mdd, dd)
    return round(mdd * 100, 3), curve


def cvar(rp, alpha=0.05):
    """VaR 与 CVaR（日频，%，正数表示损失幅度）。"""
    if len(rp) < 20:
        return None
    s = sorted(rp)
    k = max(1, int(math.ceil(alpha * len(s))))
    tail = s[:k]
    var = round(-s[k - 1] * 100, 3)
    cvar95 = round(-(sum(tail) / len(tail)) * 100, 3)
    return {"var95_pct": var, "cvar95_pct": cvar95, "tail_n": k}


# ---------------- 集中度与风险贡献 ----------------

def class_hhi(weights, class_by_code):
    """资产类别权重的 HHI（0-10000 口径）。"""
    cw = {}
    for code, w in weights.items():
        c = class_by_code.get(code, "unknown")
        cw[c] = cw.get(c, 0) + w
    total = sum(cw.values())
    hhi = sum((v / total) ** 2 for v in cw.values()) * 10000
    return {"class_weights_pct": {k: round(v * 100, 2) for k, v in cw.items()},
            "hhi": round(hhi, 1)}


def risk_contributions(ret_df, weights):
    """基于协方差的单一资产风险贡献（%）。"""
    codes = [c for c in ret_df.columns if weights.get(c, 0) > 0]
    sub = ret_df[codes]
    cov = sub.cov().values
    w = [weights[c] for c in codes]
    sigma_w = [sum(cov[i][j] * w[j] for j in range(len(codes))) for i in range(len(codes))]
    port_var = sum(w[i] * sigma_w[i] for i in range(len(codes)))
    if port_var <= 0:
        return None
    contrib = []
    for i, c in enumerate(codes):
        pct = w[i] * sigma_w[i] / port_var * 100
        contrib.append({"code": c, "weight_pct": round(w[i] * 100, 2),
                        "risk_contrib_pct": round(pct, 2)})
    contrib.sort(key=lambda x: -x["risk_contrib_pct"])
    return {"contributions": contrib,
            "port_ann_vol_pct": round(math.sqrt(port_var) * math.sqrt(TRADING_DAYS) * 100, 3)}


# ---------------- 压力测试 ----------------

def _shock_for(code, asset_class, shocks):
    if code in shocks:
        return float(shocks[code])
    if asset_class in shocks:
        return float(shocks[asset_class])
    return 0.0


def stress_test(weights, class_by_code, scenarios):
    """weights: code->小数。返回每情景组合损失% 与分资产贡献。"""
    out = []
    for sc in scenarios:
        shocks = sc["shocks"]
        total = 0.0
        detail = []
        for code, w in weights.items():
            sh = _shock_for(code, class_by_code.get(code), shocks)
            impact = w * sh
            total += impact
            if abs(impact) > 0.05:
                detail.append({"code": code, "shock_pct": sh,
                               "portfolio_impact_pp": round(impact, 3)})
        detail.sort(key=lambda x: x["portfolio_impact_pp"])
        out.append({"code": sc["code"], "name": sc["name"],
                    "portfolio_loss_pct": round(total, 3),
                    "assumption": sc.get("assumption", ""), "detail": detail})
    return out


# ---------------- 汇总 ----------------

def evaluate(price_map, weights, class_by_code, scenarios):
    """weights: code->权重小数（应已归一）。返回完整风险报告 dict。"""
    pdf = align_prices(price_map)
    if pdf is None:
        return {"error": "no_price_data"}
    ret = returns_matrix(pdf)
    used_w = {c: weights.get(c, 0) for c in ret.columns if weights.get(c, 0) > 0}
    rp, codes = weighted_returns(ret, weights)
    mdd, curve = equity_curve_mdd(rp)
    tail = cvar(rp)
    return {
        "n_days": len(ret), "codes_used": codes,
        "correlation": corr_windows(ret),
        "portfolio_ann_vol_pct": ann_vol(rp),
        "portfolio_mdd_pct": mdd,
        "downside_vol_pct": downside_vol(rp),
        "tail_risk": tail,
        "concentration": class_hhi(used_w, class_by_code),
        "risk_contributions": risk_contributions(ret, weights),
        "stress_test": stress_test(used_w, class_by_code, scenarios),
    }


# ---------------- 自测 ----------------

def self_test():
    # 构造两只资产：A 波动 ±2%，B 波动 ±1%，日期对齐
    import pandas as pd
    rows = []
    base_a, base_b = 100.0, 50.0
    seq = [0.02, -0.02, 0.02, -0.02, 0.02, -0.02, 0.02, -0.02,
           0.02, -0.02, 0.02, -0.02, 0.02, -0.02, 0.02, -0.02,
           0.02, -0.02, 0.02, -0.02, 0.02, -0.02, 0.02, -0.02]
    dates, pa, pb = [], [], []
    for i, r in enumerate(seq):
        base_a *= (1 + r)
        base_b *= (1 + r / 2)
        dates.append("2026-01-%02d" % (i + 1))
        pa.append(round(base_a, 4))
        pb.append(round(base_b, 4))
    da = pd.DataFrame({"date": dates, "close": pa})
    db = pd.DataFrame({"date": dates, "close": pb})
    pm = {"AAA": da, "BBB": db}
    weights = {"AAA": 0.5, "BBB": 0.5}
    cls = {"AAA": "core_equity", "BBB": "defensive"}
    scenarios = [{"code": "t1", "name": "测试情景",
                  "shocks": {"core_equity": -20, "defensive": -4}}]

    pdf = align_prices(pm)
    assert len(pdf) == 24
    ret = returns_matrix(pdf)
    assert len(ret) == 23
    rp, codes = weighted_returns(ret, weights)
    assert codes == ["AAA", "BBB"] and len(rp) == 23
    vol = ann_vol(rp)
    assert vol is not None and vol > 0
    mdd, curve = equity_curve_mdd(rp)
    assert mdd < 0 and len(curve) == 23
    assert downside_vol(rp) > 0
    rc = risk_contributions(ret, weights)
    s = sum(x["risk_contrib_pct"] for x in rc["contributions"])
    assert abs(s - 100) < 0.01  # 风险贡献合计 100%
    hhi = class_hhi(weights, cls)
    assert abs(hhi["hhi"] - 5000) < 1  # 两类各50% → HHI=5000
    st = stress_test(weights, cls, scenarios)
    assert abs(st[0]["portfolio_loss_pct"] - (-12)) < 0.01  # 0.5*-20+0.5*-4
    print("portfolio_risk self_test: 8/8 PASS")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        self_test()
    else:
        import json
        from pathlib import Path
        import pandas as pd
        base = Path(__file__).resolve().parent
        cfg = json.load(open(base / "portfolio_config.json", encoding="utf-8"))
        pm = {}
        for meta in cfg["etfs"]:
            f = base / "output" / ("price_history_" + meta["code"] + ".csv")
            if f.exists():
                pm[meta["code"]] = pd.read_csv(f)
        weights = {e["code"]: e["base_w"] / 100 for e in cfg["etfs"]}
        cls = {e["code"]: e["asset_class"] for e in cfg["etfs"]}
        rep = evaluate(pm, weights, cls, cfg["portfolio"]["stress_scenarios"])
        print(json.dumps(rep, ensure_ascii=False, indent=2)[:6000])
