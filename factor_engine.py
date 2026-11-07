# -*- coding: utf-8 -*-
"""
V5.0 五维评分引擎（factor_engine）
=================================
严格对应 Smart ETF Quant V4.0 PRD 第 6.2 / 6.3 节：

    趋势 25% ｜ 动量 20% ｜ 估值 15% ｜ 流动性 15% ｜ 风险 25%
    S = 0.25T + 0.20M + 0.15V + 0.15L + 0.25R

五级信号（PRD 6.3）：
    优先关注 80-100 / 买入观察 70-79 / 持有 50-69 / 谨慎持有 35-49 / 减仓观察 0-34

关键约束（PRD 明确要求）：
1. 所有子项先转成 0-100 分再合成，不允许把 -1..1 的原始分直接加权。
2. 黄金 / 债券 / 货币类不适用 PE/PB，必须使用专门的资产类别模型，
   缺失估值**不得简单记为零分**；确实不可得时该维度不参与，权重在可用维度内归一化，
   并在 confidence / missing 中显式标注。
3. 动量必须做波动率调整，避免高波动 ETF 仅因振幅大而排名靠前。
4. 分数只用于候选分层，不能仅凭分数下单 —— 还要过数据质量、资格、市场风险、
   组合风险、资金与最小交易单位五道检查（见 decision_center / risk_control）。

所有公式与阈值均为模型规则，带 model_version，可复核、可回归测试。
本模块输出仅为量化参考，不构成投资建议。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

MODEL_VERSION = "factor-v5-2026-11-07"

# PRD 6.2 权重
WEIGHTS = {"trend": 0.25, "momentum": 0.20, "valuation": 0.15,
           "liquidity": 0.15, "risk": 0.25}
W_SUM = sum(WEIGHTS.values())

# PRD 6.3 五级信号
SIGNAL_BANDS = [
    (80.0, 100.0, "优先关注", "priority", "进入重点候选，仍需过五道检查"),
    (70.0, 79.999, "买入观察", "watch", "检查仓位、资金与交易条件后生成计划"),
    (50.0, 69.999, "持有", "hold", "维持现有仓位或等待方向确认"),
    (35.0, 49.999, "谨慎持有", "caution", "限制新增仓位，只做减不加"),
    (0.0, 34.999, "减仓观察", "reduce", "检查退出条件，评估降低仓位"),
]


def _num(x):
    """None / NaN / 非数值统一返回 None。"""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _soft(ratio, scale):
    """把相对偏离 ratio 映射到 0-1 的软判定：0 偏离=0.5，+scale=1，-scale=0。"""
    return _clamp(0.5 + ratio / (2.0 * scale), 0.0, 1.0)


def signal_band(total) -> dict:
    """按 PRD 6.3 把综合分映射为五级信号。"""
    v = _num(total)
    if v is None:
        return {"label": "数据不足", "code": "nodata", "score": None,
                "action": "数据异常，暂停决策（PRD 15.5）"}
    for lo, hi, label, code, action in SIGNAL_BANDS:
        if lo <= v <= hi:
            return {"label": label, "code": code, "score": round(v, 1),
                    "band": f"{lo:g}-{hi:g}", "action": action}
    return {"label": "减仓观察", "code": "reduce", "score": round(v, 1),
            "band": "0-34.999", "action": "检查退出条件，评估降低仓位"}


# ---------------------------------------------------------------- 趋势 T
def trend_score(price: pd.DataFrame) -> dict:
    """
    PRD 6.1 策略B：强势 = 收盘>MA60 且 MA20>MA60 且中期动量为正；
    弱势 = 收盘<MA60 且 MA20<MA60 且多周期动量转负。

    为使分数连续（避免单日交叉造成跳变），每个条件用相对偏离做软判定，
    权重：站上 MA60 30% / MA20>MA60 22% / MA60>MA120 18% / MA120 上行 15% / 站上 MA250 15%。
    ADX 只做 ±5 分强度修正。
    """
    if price is None or len(price) < 60:
        return {"score": None, "label": "数据不足", "parts": [],
                "reason": "行情样本不足 60 条"}
    close = price["close"].astype(float)
    c = float(close.iloc[-1])
    ma20 = _num(price.get("ma20", pd.Series(dtype=float)).iloc[-1]) if "ma20" in price else None
    ma60 = _num(price.get("ma60", pd.Series(dtype=float)).iloc[-1]) if "ma60" in price else None
    ma120 = _num(price.get("ma120", pd.Series(dtype=float)).iloc[-1]) if "ma120" in price else None
    ma250_s = close.rolling(250).mean()
    ma250 = _num(ma250_s.iloc[-1])
    if ma20 is None or ma60 is None:
        return {"score": None, "label": "数据不足", "parts": [],
                "reason": "MA20/MA60 缺失"}

    parts, detail, w_used = [], [], {}
    # 1) 收盘 vs MA60
    s1 = _soft((c / ma60 - 1.0), 0.05)
    parts.append(s1 * 0.30); w_used["above_ma60"] = 0.30
    detail.append(f"收盘{'高于' if c > ma60 else '低于'}MA60 {abs(c/ma60-1)*100:.1f}%")
    # 2) MA20 vs MA60
    s2 = _soft((ma20 / ma60 - 1.0), 0.02)
    parts.append(s2 * 0.22); w_used["ma20_gt_ma60"] = 0.22
    detail.append(f"MA20{'高于' if ma20 > ma60 else '低于'}MA60 {abs(ma20/ma60-1)*100:.1f}%")
    # 3) MA60 vs MA120
    if ma120 is not None:
        s3 = _soft((ma60 / ma120 - 1.0), 0.02)
        parts.append(s3 * 0.18); w_used["ma60_gt_ma120"] = 0.18
        detail.append(f"MA60{'高于' if ma60 > ma120 else '低于'}MA120")
    # 4) MA120 斜率（20 日变化）
    if ma120 is not None and len(price) >= 21:
        ma120_prev = _num(price["ma120"].iloc[-21]) if "ma120" in price else None
        if ma120_prev is not None and ma120_prev > 0:
            s4 = _soft((ma120 / ma120_prev - 1.0), 0.01)
            parts.append(s4 * 0.15); w_used["ma120_slope"] = 0.15
            detail.append(f"MA120 20日{'上行' if ma120 > ma120_prev else '下行'} {abs(ma120/ma120_prev-1)*100:.2f}%")
    # 5) 长期均线 MA250
    if ma250 is not None and len(close) >= 250:
        s5 = _soft((c / ma250 - 1.0), 0.08)
        parts.append(s5 * 0.15); w_used["above_ma250"] = 0.15
        detail.append(f"收盘{'高于' if c > ma250 else '低于'}MA250 {abs(c/ma250-1)*100:.1f}%")

    wsum = sum(w_used.values())
    base = sum(parts) / wsum * 100 if wsum > 0 else None
    if base is None:
        return {"score": None, "label": "数据不足", "parts": detail, "reason": "无可用趋势条件"}

    # ADX 强度修正
    adx = None
    try:
        import decision_engine as _de
        adx = _de.risk_metrics(price["close"], price.get("high"), price.get("low")).get("adx")
    except Exception:
        adx = None
    adj = 0.0
    if adx is not None:
        if adx >= 25:
            adj = 5.0 if base >= 50 else -5.0   # 有趋势时放大方向
        elif adx < 15:
            adj = -5.0                          # 无趋势，弱化
    score = _clamp(base + adj)

    if score >= 65:
        label = "强势"
    elif score <= 40:
        label = "弱势"
    else:
        label = "中性"
    # PRD 策略B 的三条件严格判定（用于文字说明，不直接改分数）
    strict_strong = bool(c > ma60 and ma20 > ma60 and _momentum_raw(close)["composite"] is not None
                         and _momentum_raw(close)["composite"] > 0)
    return {"score": round(score, 1), "label": label, "parts": detail,
            "strict_strong": strict_strong, "adx": None if adx is None else round(adx, 1),
            "model_version": MODEL_VERSION,
            "note": "趋势须持续确认，不因单日均线交叉立刻生成大额交易计划（PRD 6.1-B）"}


# ---------------------------------------------------------------- 动量 M
def _momentum_raw(closes: pd.Series) -> dict:
    """PRD 6.1-C：M20=P(t)/P(t-20)-1，M60、M120；M=0.2M20+0.3M60+0.5M120。"""
    s = closes.dropna().astype(float)
    out = {"m20": None, "m60": None, "m120": None, "composite": None}
    if len(s) < 2:
        return out
    for n, key in ((20, "m20"), (60, "m60"), (120, "m120")):
        if len(s) > n:
            out[key] = float(s.iloc[-1] / s.iloc[-1 - n] - 1.0)
    avail = [(out[k], w) for k, w in (("m20", 0.2), ("m60", 0.3), ("m120", 0.5))
             if out[k] is not None]
    if avail:
        wsum = sum(w for _, w in avail)
        out["composite"] = sum(v * w for v, w in avail) / wsum
    return out


def momentum_score(closes: pd.Series) -> dict:
    """
    波动率调整后的动量分：
      adj = 年化综合动量 / 年化波动率（信息比率式），score = 50 + 50·tanh(adj/0.5)。
    这样同样 10% 的涨幅，低波动标的得分更高，符合 PRD「避免高风险 ETF 仅因波动剧烈获得高排名」。
    """
    m = _momentum_raw(closes)
    if m["composite"] is None:
        return {"score": None, "label": "数据不足", "raw": m,
                "reason": "动量样本不足 20 条"}
    s = closes.dropna().astype(float)
    ret = s.pct_change().dropna().tail(120)
    vol = float(ret.std(ddof=1)) * math.sqrt(252) if len(ret) > 5 else None
    comp = m["composite"]
    # 综合动量覆盖约 120 个交易日，年化后与波动率同口径比较
    comp_ann = (1.0 + comp) ** (252.0 / 120.0) - 1.0 if comp > -1 else -1.0
    if vol and vol > 0:
        adj = comp_ann / vol
    else:
        adj = comp_ann * 5.0     # 波动率缺失时退化为原始动量，并标注
    score = _clamp(50.0 + 50.0 * math.tanh(adj / 0.5))
    label = "正值" if comp > 0 else ("负值" if comp < 0 else "持平")
    return {"score": round(score, 1), "label": label, "raw": {k: (None if v is None else round(v * 100, 2))
                                                               for k, v in m.items()},
            "vol_ann_pct": None if vol is None else round(vol * 100, 2),
            "adj": round(adj, 3), "model_version": MODEL_VERSION,
            "note": "M=0.2·M20+0.3·M60+0.5·M120；分数经波动率调整"}


# ---------------------------------------------------------------- 估值 V
def valuation_score(asset_class: str, price: pd.DataFrame, pe_pct=None,
                    official_count: int = 0, pe_min: int = 200) -> dict:
    """
    分资产类别的估值模型（PRD 6.2 末尾）：
      equity        ：官方 PE 分位（需 ≥pe_min 条官方记录），越低越便宜 → 分越高
      bond          ：价格相对 MA250 偏离（低偏离=便宜）——利率/久期代理，标注「代理口径」
      gold          ：价格相对 MA250 偏离 —— 实际利率不可得时的均值回归代理，标注「代理口径」
      money         ：现金工具无估值概念，固定中性 60，标注「不适用」
      real_estate等：同 equity 处理
    任何情况下缺失估值都不记 0 分，而是返回 score=None 并从权重中剔除。
    """
    ac = (asset_class or "").lower()
    if ac == "defensive":
        # 债券 / 货币需区分：货币无估值
        ac_hint = (asset_class or "")
        pass
    cat = _asset_category(asset_class)
    if cat == "money":
        return {"score": 60.0, "label": "不适用", "basis": "货币 ETF 为现金管理工具，无 PE/PB 估值概念",
                "available": True, "model": "asset_class:money", "model_version": MODEL_VERSION}
    if cat in ("bond", "gold"):
        if price is None or len(price) < 250:
            return {"score": None, "label": "数据不足", "available": False,
                    "basis": "价格样本不足 250 条，无法构建资产类别代理估值"}
        close = price["close"].astype(float)
        ma250 = float(close.rolling(250).mean().iloc[-1])
        c = float(close.iloc[-1])
        dev = c / ma250 - 1.0
        # 债券价格波动区间小，用 2% 作为半程尺度；黄金用 8%
        scale = 0.02 if cat == "bond" else 0.08
        score = _clamp(50.0 - (dev / scale) * 25.0, 10.0, 90.0)
        basis = ("债券 ETF 以价格相对 MA250 的偏离作久期/利率代理（非真实到期收益率）"
                 if cat == "bond" else
                 "黄金 ETF 以价格相对 MA250 的偏离作均值回归代理（实际利率数据不可得）")
        return {"score": round(score, 1), "label": "便宜" if dev < 0 else "偏贵",
                "basis": basis + f"；当前偏离 {dev*100:+.2f}%", "available": True,
                "model": f"asset_class:{cat}", "proxy": True, "model_version": MODEL_VERSION}
    # 权益类：官方 PE 分位
    pct = _num(pe_pct)
    if official_count < pe_min or pct is None:
        return {"score": None, "label": "数据不足", "available": False,
                "basis": f"官方 PE 记录 {official_count} 条（需 ≥{pe_min} 条），估值维度不参与评分",
                "model": "equity:pe_percentile", "model_version": MODEL_VERSION}
    # 分位 → 分数：≤10% 95 分，30% 80 分，70% 40 分，≥90% 15 分（分段线性）
    if pct <= 10:
        score = 95.0
    elif pct <= 30:
        score = 95.0 - (pct - 10) / 20.0 * 15.0
    elif pct <= 70:
        score = 80.0 - (pct - 30) / 40.0 * 40.0
    elif pct <= 90:
        score = 40.0 - (pct - 70) / 20.0 * 25.0
    else:
        score = 15.0
    return {"score": round(_clamp(score), 1),
            "label": "低估" if pct < 30 else ("高估" if pct > 70 else "中性"),
            "basis": f"官方 PE 历史分位 {pct:.1f}%（{official_count} 条记录）",
            "available": True, "model": "equity:pe_percentile", "model_version": MODEL_VERSION}


def _asset_category(asset_class: str) -> str:
    """资产类别归一：核心权益/风格卫星→equity；防御性→bond/money；分散化→gold。"""
    ac = (asset_class or "").lower()
    if ac in ("core_equity", "style_satellite", "equity"):
        return "equity"
    if ac == "diversifier":
        return "gold"
    if ac == "defensive":
        return "bond"          # 货币 ETF 由 code 覆盖（511880 单独判 money）
    return "equity"


# ---------------------------------------------------------------- 流动性 L
def liquidity_score(amount20_wan=None, spread_bp=None, premium_pct=None,
                    premium_limit: float = 1.0) -> dict:
    """
    流动性（PRD 5.2 表6/策略A）：20 日均成交额为主，买卖价差与折溢价做扣分。
    成交额单位：万元。阈值按稳健型小资金（<10 万元）实际可执行性设定。
    """
    a = _num(amount20_wan)
    if a is None:
        return {"score": None, "label": "数据不足", "available": False,
                "basis": "20 日均成交额缺失"}
    if a >= 30000:
        base = 95.0
    elif a >= 10000:
        base = 85.0
    elif a >= 5000:
        base = 72.0
    elif a >= 2000:
        base = 60.0
    else:
        base = 40.0
    parts = [f"20日均成交额 {a:,.0f} 万元"]
    sp = _num(spread_bp)
    if sp is not None:
        parts.append(f"买卖价差 {sp:.1f}bp")
        if sp > 30:
            base -= 12
        elif sp > 15:
            base -= 6
        elif sp <= 5:
            base += 3
    pm = _num(premium_pct)
    if pm is not None:
        parts.append(f"折溢价 {pm:+.2f}%")
        if abs(pm) > premium_limit:
            base -= 15
        elif abs(pm) > premium_limit * 0.5:
            base -= 6
    return {"score": round(_clamp(base), 1), "label": "充足" if base >= 70 else "一般",
            "available": True, "basis": "；".join(parts), "model_version": MODEL_VERSION}


# ---------------------------------------------------------------- 风险 R
def risk_score(closes: pd.Series) -> dict:
    """
    风险分（越高越安全）：波动率 + 回撤 + 下行波动三部分扣分。
      基准 100 分；20日年化波动超过 10% 开始扣，每 10pp 扣 12 分（上限 45）；
      距 120 日高点回撤超过 5% 开始扣，每 10pp 扣 14 分（上限 40）；
      下行波动率高于上行时额外扣 5-10 分。
    """
    if closes is None:
        return {"score": None, "label": "数据不足", "available": False}
    s = closes.dropna().astype(float)
    if len(s) < 30:
        return {"score": None, "label": "数据不足", "available": False,
                "basis": "样本不足 30 条"}
    ret = s.pct_change().dropna()
    vol20 = float(ret.tail(20).std(ddof=1)) * math.sqrt(252) * 100
    vol60 = float(ret.tail(60).std(ddof=1)) * math.sqrt(252) * 100 if len(ret) >= 60 else vol20
    dd120 = float(s.iloc[-1] / s.tail(120).max() - 1) * 100
    down = ret[ret < 0]
    dvol = float(down.tail(60).std(ddof=1)) * math.sqrt(252) * 100 if len(down) > 5 else vol20
    up = ret[ret > 0]
    uvol = float(up.tail(60).std(ddof=1)) * math.sqrt(252) * 100 if len(up) > 5 else vol20

    vol_pen = _clamp((vol20 - 10.0) / 10.0 * 12.0, 0, 45)
    dd_pen = _clamp((-dd120 - 5.0) / 10.0 * 14.0, 0, 40)
    skew_pen = 8.0 if dvol > uvol * 1.15 else (3.0 if dvol > uvol else 0.0)
    score = _clamp(100.0 - vol_pen - dd_pen - skew_pen)
    label = "低" if score >= 72 else ("中" if score >= 45 else "高")
    return {"score": round(score, 1), "label": label, "available": True,
            "vol20_pct": round(vol20, 2), "vol60_pct": round(vol60, 2),
            "dd120_pct": round(dd120, 2), "downside_pct": round(dvol, 2),
            "basis": f"20日年化波动 {vol20:.1f}% / 60日 {vol60:.1f}%；距120日高点 {dd120:+.1f}%；下行波动 {dvol:.1f}%",
            "model_version": MODEL_VERSION}


# ---------------------------------------------------------------- 合成
def composite(asset_class: str, price: pd.DataFrame, pe_pct=None,
              official_count: int = 0, pe_min: int = 200,
              amount20_wan=None, spread_bp=None, premium_pct=None,
              is_money: bool = False) -> dict:
    """五维合成。返回 sub / total / band / missing / weights_used / confidence。"""
    t = trend_score(price)
    m = momentum_score(price["close"] if price is not None else None)
    if is_money:
        v = {"score": 60.0, "label": "不适用", "available": True,
             "basis": "货币 ETF 无估值概念，中性处理", "model": "asset_class:money"}
    else:
        v = valuation_score(asset_class, price, pe_pct, official_count, pe_min)
    l = liquidity_score(amount20_wan, spread_bp, premium_pct)
    r = risk_score(price["close"] if price is not None else None)

    subs = {
        "trend": t.get("score"),
        "momentum": m.get("score"),
        "valuation": v.get("score"),
        "liquidity": l.get("score"),
        "risk": r.get("score"),
    }
    missing = [k for k, val in subs.items() if val is None]
    dims = [(k, float(subs[k]), WEIGHTS[k]) for k in WEIGHTS if subs[k] is not None]
    if not dims:
        total, w_used = None, {}
    else:
        wsum = sum(w for _, _, w in dims)
        total = sum(s * w for _, s, w in dims) / wsum
        w_used = {k: round(w / wsum, 3) for k, _, w in dims}
    band = signal_band(total)

    # 置信度：缺失维度数量 + 估值是否为代理/不适用
    n_present = len(dims)
    if n_present <= 3:
        conf = "low"
    elif missing:
        conf = "mid"
    elif v.get("proxy") or v.get("model", "").endswith("money"):
        conf = "mid"
    else:
        conf = "high"

    detail = {"trend": t, "momentum": m, "valuation": v, "liquidity": l, "risk": r}
    return {
        "sub": {k: (None if s is None else round(s, 1)) for k, s in subs.items()},
        "total": None if total is None else round(total, 1),
        "weights": WEIGHTS, "w_used": w_used,
        "signal": band, "missing": missing, "confidence": conf,
        "detail": detail, "model_version": MODEL_VERSION,
        "formula": "S = 0.25T + 0.20M + 0.15V + 0.15L + 0.25R（缺失维度按可用维度归一化）",
    }


def cross_section(rows: list) -> list:
    """
    横截面分位（PRD 5.3：转标准分/百分位再合成）。
    rows: [{"code","name","total"}] → 追加 rank / pct_rank（0-100，越高越强）。
    """
    vals = [r["total"] for r in rows if r.get("total") is not None]
    out = []
    for r in rows:
        x = r.get("total")
        if x is None or not vals:
            out.append({**r, "rank": None, "pct_rank": None})
            continue
        pct = sum(1 for v in vals if v <= x) / len(vals) * 100.0
        rank = 1 + sum(1 for v in vals if v > x)
        out.append({**r, "rank": rank, "pct_rank": round(pct, 1)})
    return out


def self_test() -> list:
    """自测：权重、分档、缺失处理、非权益估值模型、横截面。"""
    fails = []
    # 1) 权重合计 1.0
    if abs(sum(WEIGHTS.values()) - 1.0) > 1e-9:
        fails.append("五维权重合计不等于 1")
    # 2) 分档边界
    cases = [(95, "优先关注"), (80, "优先关注"), (79.9, "买入观察"), (70, "买入观察"),
             (69, "持有"), (50, "持有"), (49, "谨慎持有"), (35, "谨慎持有"),
             (34, "减仓观察"), (0, "减仓观察")]
    for v, want in cases:
        if signal_band(v)["label"] != want:
            fails.append(f"分档错误：{v} 应为 {want}，实际 {signal_band(v)['label']}")
    # 3) 缺失估值不得记 0 分
    n = 300
    closes = pd.Series(np.linspace(1.0, 1.3, n))
    price = pd.DataFrame({"close": closes, "ma20": closes.rolling(20).mean(),
                          "ma60": closes.rolling(60).mean(),
                          "ma120": closes.rolling(120).mean()})
    res = composite("core_equity", price, pe_pct=None, official_count=10)
    if res["sub"]["valuation"] is not None:
        fails.append("估值缺失时不应给出分数")
    if res["total"] is None:
        fails.append("其余维度可用时应能算出综合分")
    # 4) 货币 ETF 估值中性且计入
    res_m = composite("defensive", price, is_money=True)
    if res_m["sub"]["valuation"] != 60.0:
        fails.append("货币 ETF 估值应为中性 60")
    # 5) 权益有 200+ 官方记录时估值可用
    res_e = composite("core_equity", price, pe_pct=20.0, official_count=300)
    if res_e["sub"]["valuation"] is None or res_e["sub"]["valuation"] < 70:
        fails.append("PE 分位 20% 应给出偏低估的高分")
    # 6) 单调性：上涨序列趋势分应显著高于下跌序列
    up = pd.Series(np.linspace(1.0, 1.6, n))
    dn = pd.Series(np.linspace(1.6, 1.0, n))
    t_up = trend_score(pd.DataFrame({"close": up, "ma20": up.rolling(20).mean(),
                                     "ma60": up.rolling(60).mean(),
                                     "ma120": up.rolling(120).mean()}))["score"]
    t_dn = trend_score(pd.DataFrame({"close": dn, "ma20": dn.rolling(20).mean(),
                                     "ma60": dn.rolling(60).mean(),
                                     "ma120": dn.rolling(120).mean()}))["score"]
    if not (t_up > t_dn):
        fails.append("上涨序列趋势分应高于下跌序列")
    # 7) 横截面排名
    rows = cross_section([{"code": "a", "total": 80}, {"code": "b", "total": 40},
                          {"code": "c", "total": 60}])
    if rows[0]["rank"] != 1 or rows[1]["rank"] != 3:
        fails.append("横截面排名错误")
    return fails


if __name__ == "__main__":
    import sys
    if "selftest" in sys.argv:
        f = self_test()
        print("factor_engine self_test:", "PASS" if not f else "FAIL")
        for x in f:
            print(" -", x)
        sys.exit(0 if not f else 1)
    print(signal_band(82))
