# -*- coding: utf-8 -*-
"""
etf_quality.py — V4 ETF 产品质量评估与穿透式持仓分析（GPT 评审 P1-2）

数据来源与降级原则：
- 腾讯实时（urllib 直连，本机可用）：现价、五档买卖价、成交额、换手率、振幅、量比
- 东方财富 fund_etf_spot_em（云端可用、本机常被拒）：基金规模、折溢价率；失败标 N/A，不编造
- 历史价格（本地 CSV / 新浪）：日均成交额
- 持仓 fund_portfolio_hold_em：股票型 ETF 季报持仓；黄金/债券/货币返回空，标注口径不适用
- 费率：portfolio_config.json 静态字段（以基金公告为准）
- 指数历史：跟踪误差 / 跟踪偏离度（可选，缺数据则 N/A）

缺失维度不参与评分，输出参与项数与可信度（与决策引擎同原则）。
"""
import json
import math
import urllib.request
from pathlib import Path

TX_URL = "https://qt.gtimg.cn/q="
TRADING_DAYS = 252


def _pref(code):
    return "sh" if code.startswith("5") else "sz"


# ---------------- 数据抓取 ----------------

def tx_quote(code, timeout=15):
    """腾讯实时行情，返回标准化字段 dict；失败返回 {'error': ...}。"""
    url = TX_URL + _pref(code) + code
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("gbk", "replace")
        p = raw.split("~")
        def f(i):
            try:
                return p[i]
            except Exception:
                return ""
        def num(i):
            try:
                return float(p[i])
            except Exception:
                return None
        return {
            "code": code, "name": f(1), "price": num(3),
            "bid1": num(9), "ask1": num(19),
            "volume_lots": num(36), "amount_wan": num(37),
            "turnover_pct": num(38), "amplitude_pct": num(43),
            "volume_ratio": num(49), "ts": f(30), "source": "tencent",
        }
    except Exception as e:
        return {"code": code, "error": type(e).__name__ + ": " + str(e)[:100]}


def em_spot(timeout=30):
    """东财全市场 ETF 快照（规模/折溢价）。失败返回 {}。"""
    try:
        import akshare as ak
        df = ak.fund_etf_spot_em()
        out = {}
        for _, r in df.iterrows():
            d = r.to_dict()
            code = str(d.get("代码", "")).zfill(6)
            if code:
                out[code] = d
        return out
    except Exception:
        return {}


def stock_holdings(code, date="2025"):
    """股票型 ETF 季报持仓。返回 (最新季度 dict[股票代码->权重%], 全部季度列表)。"""
    try:
        import akshare as ak
        df = ak.fund_portfolio_hold_em(symbol=code, date=str(date))
        if df is None or len(df) == 0:
            return None, []
        quarters = sorted(df["季度"].unique().tolist())
        latest_q = quarters[-1]
        sub = df[df["季度"] == latest_q]
        weights = {}
        rows = []
        for _, r in sub.iterrows():
            sc = str(r["股票代码"]).zfill(6)
            w = float(r["占净值比例"])
            weights[sc] = w
            rows.append({"code": sc, "name": r["股票名称"], "weight_pct": w})
        rows.sort(key=lambda x: -x["weight_pct"])
        return {"quarter": latest_q, "weights": weights, "rows": rows}, quarters
    except Exception:
        return None, []


def dividend_records(code):
    """ETF 分红记录，多候选函数名安全探测；失败返回 None。"""
    try:
        import akshare as ak
        for fn in ("fund_etf_div_em", "fund_openfund_div", "fund_dividend_em"):
            f = getattr(ak, fn, None)
            if f is None:
                continue
            try:
                if fn == "fund_etf_div_em":
                    df = f(symbol=code)
                else:
                    df = f(symbol=code)
                if df is not None and len(df) > 0:
                    return {"func": fn, "count": int(len(df)), "cols": list(df.columns)}
            except Exception:
                continue
        return None
    except Exception:
        return None


# ---------------- 指标计算 ----------------

def relative_spread_pct(q):
    """五档一价相对价差 %。"""
    b, a = q.get("bid1"), q.get("ask1")
    if b and a and b > 0 and a >= b:
        return round((a - b) / b * 100.0, 4)
    return None


def avg_amount_wan(hist_df, n=20):
    """近 n 日日均成交额（万元）。新浪 amount 单位为元，换算万元。"""
    if hist_df is None or len(hist_df) == 0 or "amount" not in hist_df.columns:
        return None
    tail = hist_df.tail(n)
    try:
        vals = tail["amount"].astype(float)
        return round(float(vals.mean()) / 1e4, 2)
    except Exception:
        return None


def _returns(series):
    out = []
    prev = None
    for v in series:
        v = float(v)
        if prev and prev > 0:
            out.append(v / prev - 1.0)
        prev = v
    return out


def tracking_quality(etf_df, index_df, n=250):
    """跟踪误差（年化%）与跟踪偏离度（年化%）。需要 ETF 与指数对齐收盘价。"""
    if etf_df is None or index_df is None:
        return None
    try:
        a = etf_df[["date", "close"]].copy()
        b = index_df[["date", "close"]].copy()
        a["date"] = a["date"].astype(str)
        b["date"] = b["date"].astype(str)
        m = a.merge(b, on="date", suffixes=("_etf", "_idx"))
        m = m.tail(n)
        if len(m) < 30:
            return None
        re_ = _returns(m["close_etf"].tolist())
        ri = _returns(m["close_idx"].tolist())
        k = min(len(re_), len(ri))
        diff = [re_[i] - ri[i] for i in range(k)]
        mean = sum(diff) / len(diff)
        var = sum((x - mean) ** 2 for x in diff) / (len(diff) - 1)
        te = math.sqrt(var) * math.sqrt(TRADING_DAYS) * 100
        bias = mean * TRADING_DAYS * 100
        return {
            "paired_days": len(m), "tracking_error_ann_pct": round(te, 3),
            "tracking_bias_ann_pct": round(bias, 3),
        }
    except Exception:
        return None


def overlap_weight(hold_a, hold_b):
    """两只 ETF 持仓重合：共同股票数、共同权重占 A/B 各自净值比（%）。"""
    if not hold_a or not hold_b:
        return None
    wa, wb = hold_a["weights"], hold_b["weights"]
    common = set(wa.keys()) & set(wb.keys())
    return {
        "n_common": len(common),
        "common_w_in_a_pct": round(sum(wa[k] for k in common), 3),
        "common_w_in_b_pct": round(sum(wb[k] for k in common), 3),
        "common_codes": sorted(common),
    }


# ---------------- 子项评级 ----------------

def _band_score(value, bands):
    """bands: [(阈值含, 得分)] 从优到劣；返回得分或 None。"""
    if value is None:
        return None
    for thr, score in bands:
        if value >= thr:
            return score
    return bands[-1][1]


def liquidity_score(avg_amt_wan, spread_pct):
    """流动性综合：成交额与价差各半，0-100；缺失项不参与。"""
    s_amt = _band_score(avg_amt_wan, [(10000, 100), (5000, 80), (1000, 60), (0, 35)])
    s_spd = None
    if spread_pct is not None:
        s_spd = _band_score(-spread_pct, [(-0.1, 100), (-0.2, 80), (-0.5, 60), (-99, 35)])
    parts = [s for s in (s_amt, s_spd) if s is not None]
    return round(sum(parts) / len(parts), 1) if parts else None


def cost_score(expense_ratio):
    """费率得分（年化%）：越低越好。"""
    if expense_ratio is None:
        return None
    return _band_score(-float(expense_ratio), [(-0.2, 100), (-0.4, 85), (-0.6, 70), (-99, 50)])


def concentration_top10(hold):
    """前十大成分权重合计（集中度%）。"""
    if not hold:
        return None
    return round(sum(r["weight_pct"] for r in hold["rows"][:10]), 2)


# ---------------- 因子暴露（规则推断） ----------------

FACTOR_RULES = {
    "515180": {"value": "高", "dividend": "高", "cyclical": "中", "rate_sensitive": "中",
               "style": "红利/价值"},
    "563020": {"value": "高", "dividend": "高", "cyclical": "低", "rate_sensitive": "中",
               "style": "红利低波"},
    "516310": {"value": "高", "dividend": "中", "cyclical": "中", "rate_sensitive": "高",
               "style": "金融/银行"},
    "515220": {"value": "中", "dividend": "中", "cyclical": "高", "rate_sensitive": "低",
               "style": "强周期/煤炭"},
    "159201": {"value": "中", "dividend": "中", "cyclical": "中", "rate_sensitive": "低",
               "style": "现金流质量"},
    "510300": {"value": "中", "dividend": "低", "cyclical": "中", "rate_sensitive": "中",
               "style": "宽基核心"},
    "512050": {"value": "中", "dividend": "低", "cyclical": "中", "rate_sensitive": "中",
               "style": "宽基核心"},
}


def factor_exposure(code, asset_class):
    if asset_class == "defensive":
        return {"style": "利率/现金", "rate_sensitive": "高", "note": "非股票，因子框架不适用"}
    if asset_class == "diversifier":
        return {"style": "黄金", "rate_sensitive": "低", "note": "实物金，抗通胀分散"}
    return FACTOR_RULES.get(code, {"style": "未分类", "note": "规则待维护"})


# ---------------- 批量评估 ----------------

def evaluate_one(meta, q, spot_row, hist_df, index_df, hold):
    code = meta["code"]
    spread = relative_spread_pct(q) if "error" not in q else None
    avg_amt = avg_amount_wan(hist_df, 20)
    amt_basis = "hist20d"
    if avg_amt is None and "error" not in q and q.get("amount_wan"):
        avg_amt = q.get("amount_wan")  # 降级：腾讯当日成交额（单日口径）
        amt_basis = "tx_single_day"
    tq = tracking_quality(etf_df=hist_df, index_df=index_df)
    top10 = concentration_top10(hold)
    exp = meta.get("expense_ratio")
    div = dividend_records(code)

    scale_yi = None
    premium_pct = None
    if spot_row:
        for k in ("基金规模", "资产净值"):
            if k in spot_row and spot_row[k] == spot_row[k]:
                try:
                    scale_yi = round(float(spot_row[k]), 2)
                    break
                except Exception:
                    pass
        for k in ("折价率", "溢价率", "折溢价率"):
            if k in spot_row and spot_row[k] == spot_row[k]:
                try:
                    premium_pct = float(spot_row[k])
                    break
                except Exception:
                    pass

    s_liq = liquidity_score(avg_amt, spread)
    s_cost = cost_score(exp)
    s_scale = _band_score(scale_yi, [(20, 100), (5, 85), (2, 70), (0, 45)])
    s_te = None
    if tq:
        s_te = _band_score(-tq["tracking_error_ann_pct"], [(-1, 100), (-2, 85), (-4, 65), (-99, 40)])
    s_prem = None
    if premium_pct is not None:
        s_prem = 100 if abs(premium_pct) <= 0.3 else (80 if abs(premium_pct) <= 0.7 else 50)

    sub_scores = {"liquidity": s_liq, "cost": s_cost, "scale": s_scale,
                  "tracking": s_te, "premium": s_prem}
    used = {k: v for k, v in sub_scores.items() if v is not None}
    quality = round(sum(used.values()) / len(used), 1) if used else None

    return {
        "code": code, "name": meta["name"], "asset_class": meta["asset_class"],
        "metrics": {
            "scale_yi": scale_yi, "avg_amount_wan": avg_amt, "amount_basis": amt_basis,
            "spread_pct": spread, "premium_pct": premium_pct, "expense_ratio_pct": exp,
            "top10_concentration_pct": top10, "dividend": div,
            "tracking": tq,
        },
        "holdings": ({"quarter": hold["quarter"], "top10": hold["rows"][:10],
                      "n_total": len(hold["rows"])} if hold else
                     {"note": "非股票型或接口未返回，股票穿透口径不适用"}),
        "factor_exposure": factor_exposure(code, meta["asset_class"]),
        "sub_scores": sub_scores, "quality_score": quality,
        "score_dims_used": sorted(used.keys()),
        "confidence": "high" if len(used) >= 4 else ("mid" if len(used) >= 2 else "low"),
    }


def evaluate_all(config, hist_map=None, index_map=None, use_em=True):
    """hist_map/index_map: code/dataFrame。返回 (results, overlap_pairs)。"""
    hist_map = hist_map or {}
    index_map = index_map or {}
    spot = em_spot() if use_em else {}
    holds = {}
    results = []
    for meta in config["etfs"]:
        code = meta["code"]
        q = tx_quote(code)
        hold, _q = stock_holdings(code)
        if hold:
            holds[code] = hold
        idx = None
        if meta.get("index_code") and meta["index_code"] in index_map:
            idx = index_map[meta["index_code"]]
        results.append(evaluate_one(meta, q, spot.get(code), hist_map.get(code), idx, hold))

    pairs = []
    codes = sorted(holds.keys())
    for i in range(len(codes)):
        for j in range(i + 1, len(codes)):
            ov = overlap_weight(holds[codes[i]], holds[codes[j]])
            if ov and ov["n_common"] > 0:
                pairs.append({"a": codes[i], "b": codes[j], **ov})
    pairs.sort(key=lambda x: -x["common_w_in_a_pct"])
    return results, pairs


# ---------------- 自测 ----------------

def self_test():
    # 1) 重合率
    ha = {"quarter": "q", "weights": {"601": 10, "602": 8, "603": 5}, "rows": []}
    hb = {"quarter": "q", "weights": {"602": 9, "603": 4, "604": 3}, "rows": []}
    ov = overlap_weight(ha, hb)
    assert ov["n_common"] == 2 and ov["common_w_in_a_pct"] == 13 and ov["common_w_in_b_pct"] == 13

    # 2) 价差
    q = {"bid1": 4.124, "ask1": 4.125}
    assert abs(relative_spread_pct(q) - 0.02425) < 0.001

    # 3) 流动性得分边界
    assert liquidity_score(10000, 0.05) == 100
    assert liquidity_score(None, 0.6) == 35  # 价差>0.5% 落最差档

    # 4) 费率
    assert cost_score(0.6) == 70 and cost_score(None) is None
    print("etf_quality self_test: 4/4 PASS")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        self_test()
    else:
        base = Path(__file__).resolve().parent
        cfg = json.load(open(base / "portfolio_config.json", encoding="utf-8"))
        res, pairs = evaluate_all(cfg, use_em=False)
        print(json.dumps({"results": res, "overlap_pairs": pairs},
                         ensure_ascii=False, indent=2)[:6000])
