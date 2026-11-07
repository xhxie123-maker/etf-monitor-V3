# -*- coding: utf-8 -*-
"""
V4 交易决策引擎（etf-monitor-v4）
=================================
将判断拆为 趋势 / 估值 / 风险 三个维度，并经数据质量闸门后输出综合评分(0-100)、
数据完整度、六种操作状态、主要依据、分批建仓纪律与 ATR 参考价位。

设计原则（V4 核心，依据 GPT 评审 P0）：
1. **未知风险 ≠ 低风险**：风险指标（回撤/波动率/ADX）全部缺失时明确返回「数据不足」，
   绝不因为"字典存在但字段为空"而误判低风险；风险数据缺失时禁止主动加仓。
2. **前序闸门有否决权**：行情滞后 / 数据源异常 / 风险缺失 / 趋势缺失时，
   后续高分不能推翻前序风险警告，最高只输出「暂停新增」或「暂不判断」。
3. 估值只使用"积累的官方 PE 观测"：至少 200 条有效记录才计算历史分位并参与决策；
   样本不足时该维度明确标注"数据不足"，不参与综合评分。
4. 输出统一为六种操作状态（允许分批加仓/正常定投/持有观察/暂停新增/再平衡评估/暂不判断），
   综合评分仅作辅助排序，不代表上涨概率。
5. 本模块为纯函数（仅依赖 pandas/numpy），便于单元测试与复用。

所有输出均为量化参考，不构成投资建议。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

# 估值维度参与决策所需的最少官方 PE 有效记录数
PE_MIN_OFFICIAL = 200
# 官方 PE 分位计算窗口（交易日）：与主信号窗口一致
PE_WINDOWS = {"近1年": 250, "近3年": 750, "近5年": 1250}
PE_MAIN_WINDOW = "近3年"

# 估值分位阈值（%）
PE_LOW = 30.0
PE_HIGH = 70.0

# 风险阈值
RISK_DD120_HIGH = -25.0     # 距120日高点回撤 ≤ -25% → 高风险
RISK_DD120_MID = -15.0      # ≤ -15% → 中风险
RISK_VOL_HIGH = 35.0        # 20日年化波动率 ≥ 35% → 高风险
RISK_VOL_MID = 25.0         # ≥ 25% → 中风险
RISK_ADX_STRONG = 40.0      # ADX ≥ 40 且趋势下降 → 高风险

# V4 六种操作状态（GPT 评审：取消只盯单一综合评分）
STATE_LABELS = {
    "allow_add": "允许分批加仓",
    "normal_dca": "正常定投",
    "hold": "持有观察",
    "pause": "暂停新增",
    "rebalance": "再平衡评估",
    "skip": "暂不判断",
}
STATE_COLORS = {
    "allow_add": "#ef4444",
    "normal_dca": "#f97316",
    "hold": "#94a3b8",
    "pause": "#3b82f6",
    "rebalance": "#eab308",
    "skip": "#94a3b8",
}
# 状态按"操作强度"排序，用于组合层汇总
STATE_ORDER = ["skip", "pause", "hold", "rebalance", "normal_dca", "allow_add"]

# 数据质量闸门默认阈值（与 portfolio_config.json 的 data_quality 段一致）
GATE_PRICE_STALE_DAYS = 3
GATE_PE_STALE_DAYS = 10
GATE_MIN_PRICE_ROWS = 130


def trend_state(ma20: float | None, ma60: float | None, ma120: float | None,
                slope_ma20: float | None = None) -> dict:
    """
    趋势维度：由 MA20/MA60/MA120 结构与 MA20 斜率判断。
    返回 {label, score, desc}
    label: 上升 / 下降 / 震荡 / 数据不足
    score: 上升=1.0, 下降=-1.0, 震荡=0.0, 数据不足=None（不参与）
    """
    if ma20 is None or ma60 is None or ma120 is None or any(
            (x is not None and (isinstance(x, float) and math.isnan(x))) for x in (ma20, ma60, ma120)):
        # 数据不足（任一关键均线缺失）
        if (ma20 is None or (isinstance(ma20, float) and math.isnan(ma20))) or \
           (ma60 is None or (isinstance(ma60, float) and math.isnan(ma60))) or \
           (ma120 is None or (isinstance(ma120, float) and math.isnan(ma120))):
            return {"label": "数据不足", "score": None, "desc": "均线数据不足，趋势暂不判断"}
    # 显式结构判断（忽略 NaN 的参与）
    valid = [x for x in (ma20, ma60, ma120) if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if len(valid) < 2:
        return {"label": "数据不足", "score": None, "desc": "均线数据不足，趋势暂不判断"}
    ma20, ma60, ma120 = (float(x) if x is not None and not (isinstance(x, float) and math.isnan(x)) else None
                         for x in (ma20, ma60, ma120))
    score = 0.0
    desc_parts = []
    if ma20 is not None and ma60 is not None:
        if ma20 > ma60:
            score += 0.5
            desc_parts.append("MA20>MA60")
        else:
            score -= 0.5
            desc_parts.append("MA20<MA60")
    if ma60 is not None and ma120 is not None:
        if ma60 > ma120:
            score += 0.5
            desc_parts.append("MA60>MA120")
        else:
            score -= 0.5
            desc_parts.append("MA60<MA120")
    # MA20 斜率辅助（5 日变化）
    if slope_ma20 is not None and not (isinstance(slope_ma20, float) and math.isnan(slope_ma20)):
        if slope_ma20 > 0:
            score += 0.2 * min(abs(float(slope_ma20)) / 0.01, 1.0)
            desc_parts.append("MA20斜率向上")
        elif slope_ma20 < 0:
            score -= 0.2 * min(abs(float(slope_ma20)) / 0.01, 1.0)
            desc_parts.append("MA20斜率向下")
    score = max(-1.0, min(1.0, score))
    if score >= 0.6:
        label = "上升"
    elif score <= -0.6:
        label = "下降"
    else:
        label = "震荡"
    return {"label": label, "score": score, "desc": "、".join(desc_parts) if desc_parts else label}


def valuation_state(pe_pct: float | None, official_count: int) -> dict:
    """
    估值维度：仅官方 PE 积累 ≥ PE_MIN_OFFICIAL 才参与决策。
    返回 {label, score, desc, available}
    label: 低估 / 合理 / 高估 / 数据不足
    score: 低估=1.0, 高估=-1.0, 合理=0.0, 数据不足=None
    """
    if official_count is None or official_count < PE_MIN_OFFICIAL or pe_pct is None or \
       (isinstance(pe_pct, float) and math.isnan(pe_pct)):
        return {"label": "数据不足", "score": None, "desc": f"官方PE积累{0 if official_count is None else int(official_count)}条(<{PE_MIN_OFFICIAL})，估值不参与决策", "available": False}
    if pe_pct < PE_LOW:
        return {"label": "低估", "score": 1.0, "desc": f"官方PE分位{pe_pct:.0f}% 低估", "available": True}
    if pe_pct > PE_HIGH:
        return {"label": "高估", "score": -1.0, "desc": f"官方PE分位{pe_pct:.0f}% 高估", "available": True}
    return {"label": "合理", "score": 0.0, "desc": f"官方PE分位{pe_pct:.0f}% 中性", "available": True}


def _wilder_smooth(values: pd.Series, period: int) -> pd.Series:
    """Wilder 平滑：从首个有效观测开始，前 period 期均值初始化，之后逐点递推 (prev*(p-1)+cur)/p。
    输入允许含 NaN 前缀（如 DX 序列），自动跳过无效区间。"""
    out = pd.Series(np.nan, index=values.index, dtype=float)
    vals = values.to_numpy(dtype=float)
    n = len(vals)
    valid_idx = np.where(~np.isnan(vals))[0]
    if len(valid_idx) < period + 1:
        return out
    # 初始窗口：从第 period 个有效值位置开始，用其之前最近的 period 个有效值求均值
    for start in range(period, len(valid_idx)):
        anchor = valid_idx[start]
        prev = valid_idx[:start]
        if len(prev) >= period:
            out.iloc[anchor] = vals[prev[-period:]].mean()
            break
    # 递推：对每个有效观测逐点平滑（初始锚点之后；缺失值保持 NaN 不参与）
    for i in range(1, n):
        if np.isnan(vals[i]):
            continue
        prev_val = out.iloc[i - 1]
        if np.isnan(prev_val):
            seg = vals[valid_idx[valid_idx <= i]][-period:]
            if len(seg) >= period:
                out.iloc[i] = seg.mean()
        else:
            out.iloc[i] = (prev_val * (period - 1) + vals[i]) / period
    return out


def risk_metrics(closes: pd.Series, highs: pd.Series | None = None,
                 lows: pd.Series | None = None) -> dict:
    """
    风险维度原始指标（末值 + 序列内部使用）：
      atr(14)、adx(14)、vol20_ann（20日年化波动率%）、dd60/dd120（距60/120日高点回撤%）
    返回含最新值的 dict；数据不足的字段为 None。
    """
    closes = closes.dropna().astype(float)
    if len(closes) < 30:
        return {"atr": None, "adx": None, "vol20_ann": None, "dd60": None, "dd120": None,
                "atr_pct": None}
    highs = highs.dropna().astype(float) if highs is not None else closes
    lows = lows.dropna().astype(float) if lows is not None else closes
    # 对齐索引
    idx = closes.index
    h = highs.reindex(idx).fillna(closes)
    l = lows.reindex(idx).fillna(closes)
    prev_close = closes.shift(1)
    tr = pd.concat([h - l, (h - prev_close).abs(), (l - prev_close).abs()], axis=1).max(axis=1)
    atr = _wilder_smooth(tr, 14)
    # ADX(14)：+DM/-DM → Wilder 平滑 → DX → ADX
    up = h.diff()
    down = -l.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=idx)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=idx)
    atr_s = _wilder_smooth(tr, 14)
    plus_s = _wilder_smooth(plus_dm, 14)
    minus_s = _wilder_smooth(minus_dm, 14)
    di_plus = 100 * plus_s / atr_s.replace(0, np.nan)
    di_minus = 100 * minus_s / atr_s.replace(0, np.nan)
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    adx = _wilder_smooth(dx, 14)
    ret = closes.pct_change()
    vol20 = ret.rolling(20).std() * math.sqrt(252) * 100
    dd60 = (closes / closes.rolling(60).max() - 1) * 100
    dd120 = (closes / closes.rolling(120).max() - 1) * 100

    def _last(s):
        v = s.dropna()
        return float(v.iloc[-1]) if len(v) else None

    atr_v = _last(atr)
    return {
        "atr": atr_v,
        "atr_pct": round(atr_v / float(closes.iloc[-1]) * 100, 2) if atr_v else None,
        "adx": _last(adx),
        "vol20_ann": _last(vol20),
        "dd60": _last(dd60),
        "dd120": _last(dd120),
    }


def risk_state(r: dict, trend: dict | None = None) -> dict:
    """
    风险等级：低/中/高；核心风险字段缺失时明确返回"数据不足"。
    score: 低=1.0, 中=0.5, 高=0.0；数据不足=None（不参与决策，且禁止主动加仓）。

    V4 修复（GPT 评审 P0-2）：V3 只判断 r 是否为 None，而 risk_metrics() 在样本不足时
    返回的是"全 None 的字典"，导致 dd120/vol/adx 全部缺失时仍走到末尾返回「低风险 1.0」，
    把"没有足够数据证明风险"误判为"风险很低"。V4 改为按字段可用性判定：
      - 回撤(dd120)与波动率(vol20_ann)两个核心风险维度全部缺失 → 数据不足，绝不判低风险；
      - 判为低风险时，desc 必须列明实际依据字段与数值，便于复核。
    """
    if r is None:
        return {"label": "数据不足", "score": None, "desc": "风险数据缺失（无风险指标返回），未知风险不判低风险", "available": False}
    dd120 = r.get("dd120")
    vol = r.get("vol20_ann")
    adx = r.get("adx")
    t_label = (trend or {}).get("label")
    core = [v for v in (dd120, vol) if v is not None]
    if not core:
        return {"label": "数据不足", "score": None,
                "desc": "回撤/波动率核心风险指标均缺失（未知风险≠低风险），风险维度不参与决策",
                "available": False}
    parts = []
    if dd120 is not None and dd120 <= RISK_DD120_HIGH:
        parts.append(f"距120日高点回撤{dd120:.0f}%")
    if vol is not None and vol >= RISK_VOL_HIGH:
        parts.append(f"年化波动{vol:.0f}%")
    if adx is not None and adx >= RISK_ADX_STRONG and t_label == "下降":
        parts.append(f"ADX{adx:.0f}趋势下行")
    if parts:
        return {"label": "高风险", "score": 0.0, "desc": "；".join(parts), "available": True}
    mid = []
    if dd120 is not None and dd120 <= RISK_DD120_MID:
        mid.append(f"距120日高点回撤{dd120:.0f}%")
    if vol is not None and vol >= RISK_VOL_MID:
        mid.append(f"年化波动{vol:.0f}%")
    if mid:
        return {"label": "中风险", "score": 0.5, "desc": "；".join(mid), "available": True}
    basis = []
    basis.append(f"距120日高点回撤{dd120:+.0f}%" if dd120 is not None else "回撤数据缺失")
    basis.append(f"20日年化波动{vol:.0f}%" if vol is not None else "波动率数据缺失")
    return {"label": "低风险", "score": 1.0, "desc": "；".join(basis) + "，均处温和区间", "available": True}


def data_gate(price_last_date=None, run_date=None, price_rows: int | None = None,
              pe_last_date=None, official_count: int = 0,
              src_flags: dict | None = None, trend: dict | None = None,
              risk: dict | None = None, val: dict | None = None,
              price_stale_days: int = GATE_PRICE_STALE_DAYS,
              price_stale_hard_days: int = 7,
              pe_stale_days: int = GATE_PE_STALE_DAYS,
              min_price_rows: int = GATE_MIN_PRICE_ROWS,
              pe_min_official: int = PE_MIN_OFFICIAL) -> dict:
    """
    V4 数据质量闸门（GPT 评审 P0-2/P0-3）：在任何评分之前先校验数据可用性。

    规则（与 GPT 数据状态闸门表一一对应）：
      - 行情滞后超过 price_stale_hard_days → block：禁止用滞后行情生成当日操作信号，直接暂不判断；
      - 行情滞后在 (price_stale_days, hard] 之间 → warn+block_add：可能为周末/长假休市或数据延迟，
        当日不主动加仓（不直接判"暂不判断"，避免每个休市日都停掉全部判断）；
      - 行情样本不足（< min_price_rows）→ warn+block_add；
      - 趋势数据不足 → warn+block_add：暂不生成主动加仓判断；
      - 风险数据不足 → warn+block_add：未知风险不得当作低风险；
      - 估值数据不足（官方PE < pe_min_official）→ warn：不使用估值维度，降低决策可信度（不阻断加仓）；
      - PE 口径陈旧 / 数据源降级 → warn：标记异常、降级为观察值；
      - 多核心维度异常（趋势与风险同时缺失）→ fatal：直接输出暂不判断。

    返回：{passed, block_add, fatal, level, flags:[{code,level,label,detail}], summary, stale_days}
    """
    flags = []
    block_add = False
    fatal = False

    # --- 行情新鲜度 ---
    stale_days = None
    if price_last_date is not None and run_date is not None:
        try:
            stale_days = (pd.to_datetime(run_date).date() - pd.to_datetime(price_last_date).date()).days
        except Exception:
            stale_days = None
    if stale_days is not None and stale_days > price_stale_days:
        if stale_days > price_stale_hard_days:
            flags.append({"code": "price_stale_hard", "level": "block",
                          "label": f"行情断更 {stale_days} 天",
                          "detail": f"最新行情日 {price_last_date} 距运行日 {run_date} 超过 {price_stale_hard_days} 天：禁止用滞后行情生成当日操作信号"})
            fatal = True
        else:
            flags.append({"code": "price_stale", "level": "warn",
                          "label": f"行情滞后 {stale_days} 天",
                          "detail": f"最新行情日 {price_last_date} 距运行日 {run_date} 约 {stale_days} 天（可能为周末/休市或数据延迟）：当日不主动加仓"})
        block_add = True

    # --- 行情样本量 ---
    if price_rows is not None and price_rows < min_price_rows:
        flags.append({"code": "price_sample", "level": "warn",
                      "label": f"价格样本不足（{price_rows} 条）",
                      "detail": f"少于 {min_price_rows} 条，趋势/风险指标稳定性下降，禁止主动加仓"})
        block_add = True

    # --- 趋势维度 ---
    if trend is not None and trend.get("score") is None:
        flags.append({"code": "trend_missing", "level": "warn",
                      "label": "趋势数据不足", "detail": "均线不足：暂不生成主动加仓判断"})
        block_add = True

    # --- 风险维度 ---
    if risk is not None and risk.get("score") is None:
        flags.append({"code": "risk_missing", "level": "warn",
                      "label": "风险数据不足", "detail": "未知风险不得当作低风险：禁止主动加仓，仅允许观望/持有"})
        block_add = True

    # --- 估值维度（不阻断加仓，只降可信度）---
    if val is not None and not val.get("available", False):
        flags.append({"code": "val_missing", "level": "warn",
                      "label": f"估值数据不足（官方PE {official_count} 条）",
                      "detail": f"官方PE不足 {pe_min_official} 条：不使用估值维度，综合评分可信度降低"})

    # --- PE 口径新鲜度 ---
    if pe_last_date is not None and run_date is not None:
        try:
            pe_days = (pd.to_datetime(run_date).date() - pd.to_datetime(pe_last_date).date()).days
            if pe_days > pe_stale_days:
                flags.append({"code": "pe_stale", "level": "warn",
                              "label": f"估值口径陈旧 {pe_days} 天",
                              "detail": f"官方PE最新观测 {pe_last_date} 已过期（>{pe_stale_days} 天）：估值维度降级为观察值"})
        except Exception:
            pass

    # --- 数据源异常 ---
    if src_flags:
        failed = {k: v for k, v in src_flags.items() if k.startswith("fail") and v}
        if failed:
            flags.append({"code": "src_fail", "level": "warn",
                          "label": "数据源降级",
                          "detail": "；".join(f"{k}={v}" for k, v in failed.items()) + "：已降价到备用源，来源一致性需人工核对"})

    # --- 多核心维度异常 → 直接暂不判断 ---
    if (trend is not None and trend.get("score") is None) and (risk is not None and risk.get("score") is None):
        flags.append({"code": "multi_dim_fail", "level": "block",
                      "label": "核心维度同时缺失", "detail": "趋势与风险维度均不可用：直接输出暂不判断"})
        fatal = True

    if fatal:
        level = "block"
    elif flags:
        level = "warn"
    else:
        level = "ok"
    summary = "；".join(f["label"] for f in flags) if flags else "数据质量校验通过"
    return {"passed": not fatal, "block_add": block_add, "fatal": fatal,
            "level": level, "flags": flags, "summary": summary,
            "stale_days": stale_days}



def data_quality(trend: dict, val: dict, risk: dict, official_count: int) -> int:
    """数据完整度 0-100：估值按官方积累条数计，趋势/风险按是否可用计"""
    q = []
    q.append(100 if official_count is not None and official_count >= PE_MIN_OFFICIAL else
             (int(official_count / PE_MIN_OFFICIAL * 100) if official_count and official_count > 0 else 0))
    q.append(100 if (trend.get("score") is not None) else 0)
    q.append(100 if (risk.get("score") is not None) else 0)
    return int(round(sum(q) / len(q)))


def _score100(x: float | None):
    """-1..1 三维得分 → 0-100 展示分；None 保持 None（维度未参与）。"""
    if x is None:
        return None
    return int(round((float(x) + 1) / 2 * 100))


def decide(trend: dict, val: dict, risk: dict, official_count: int, gate: dict | None = None) -> dict:
    """
    V4 综合决策：数据质量闸门 → 趋势/估值/风险三维评分 → 六种操作状态。

    关键行为（GPT 评审 P0-2 修复）：
      - 缺失维度不再用 0.5 中性值填充；只在可用维度上归一化权重，并在 w/score_scope 中
        明确标出实际参与维度，避免"中性分数掩盖数据缺失"。
      - 闸门否决：gate.fatal（行情滞后/多维度缺失）→ 暂不判断；
        风险维度缺失（未知风险）→ 暂停新增；趋势缺失/趋势下降/高风险 → 暂停新增。
        后续高分不能推翻前序风险警告。

    返回字段：
      score(0-100, 仅可用维度) / quality(数据完整度%) / state(六状态码) / state_label /
      suggestion / sug_color / reasons / trend / risk / val / sub / w / score_alt /
      gate / score_scope / next_conditions / position_note
    """
    t = trend.get("score")
    v = val.get("score")
    r = risk.get("score")
    t_label = trend.get("label")
    r_label = risk.get("label")
    v_label = val.get("label")
    gate = gate or {}
    fatal = bool(gate.get("fatal"))
    gate_block_add = bool(gate.get("block_add"))
    quality = data_quality(trend, val, risk, official_count)
    base_sub = {"trend": _score100(t), "val": _score100(v), "risk": _score100(r)}
    gate_flags = gate.get("flags") or []

    def _out(state, reasons, score=None, score_alt=None, w=None, scope=None, extra_reasons=None):
        reasons = [x for x in reasons if x]
        if gate_flags:
            reasons.append("数据质量闸门：" + "；".join(f["label"] for f in gate_flags))
        return {
            "score": score,
            "quality": quality,
            "state": state,
            "state_label": STATE_LABELS[state],
            "suggestion": STATE_LABELS[state],
            "sug_color": STATE_COLORS[state],
            "reasons": reasons,
            "trend": t_label, "risk": r_label, "val": v_label,
            "sub": base_sub,
            "w": w or {"trend": 0.0, "val": 0.0, "risk": 0.0},
            "score_alt": score_alt,
            "score_scope": scope or [],
            "gate": {"level": gate.get("level", "ok"), "summary": gate.get("summary", "数据质量校验通过"),
                     "flags": gate_flags, "block_add": gate_block_add},
            "next_conditions": extra_reasons or [],
            "position_note": "V4 P0 阶段未录入实际持仓：仓位提示按基准权重/上限给出；实际偏离触发的「再平衡评估」需持仓数据（P1）。",
        }

    # --- 闸门一：致命（行情滞后/多维度缺失）---
    if fatal:
        return _out("skip", ["数据质量闸门未通过，暂停当日判断"] + [f"{f['label']}：{f['detail']}" for f in gate_flags],
                    extra_reasons=["恢复实时行情与核心指标后重新评估"])

    # --- 闸门二：趋势与风险同时缺失（无维度可评）---
    if t is None and r is None:
        return _out("skip", ["趋势/风险数据均不足，暂不生成交易建议"], scope=["val"] if v is not None else [])

    # --- 评分：仅可用维度归一化（不用中性值掩盖缺失）---
    # 三维子分数先映射为 0-100（_score100）再加权，避免 -1..1 直接相加后被钳制到 0/100
    # 造成分数堆积（V3 遗留缺陷：趋势下降+估值高估+低风险会得 0 分而非 30 分）。
    dims = []
    if t is not None:
        dims.append(("trend", _score100(t), 0.30))
    if v is not None:
        dims.append(("val", _score100(v), 0.40))
    if r is not None:
        dims.append(("risk", _score100(r), 0.30))
    wsum = sum(w for _, _, w in dims)
    score = int(round(sum(s * w for _, s, w in dims) / wsum)) if wsum > 0 else None
    w_used = {k: round(w / wsum, 2) for k, _, w in dims}
    scope = [k for k, _, _ in dims]
    # 敏感性：估值权重 40%→30%（趋势/风险各 35%），同样在可用维度内归一化
    sens = {"trend": 0.35, "val": 0.30, "risk": 0.35}
    walt_sum = sum(sens[k] for k in scope)
    score_alt = int(round(sum(s * sens[k] for k, s, _ in dims) / walt_sum)) if walt_sum > 0 else None

    reasons = []
    reasons.append(trend.get("desc") or f"趋势{t_label}")
    reasons.append(val.get("desc"))
    reasons.append(risk.get("desc") or f"风险{r_label}")
    next_conds = []

    # --- 否决层：风险缺失 / 高风险 / 趋势缺失 / 趋势下降 → 暂停新增 ---
    if r is None:
        return _out("pause", reasons + ["风险维度缺失：未知风险不得当作低风险，禁止主动加仓"],
                    score, score_alt, w_used, scope,
                    ["等回撤/波动率指标恢复可用后再评估主动加仓"])
    if t is None:
        return _out("pause", reasons + ["趋势维度缺失：暂不生成主动加仓判断"],
                    score, score_alt, w_used, scope,
                    ["补齐 MA20/60/120 所需样本后重新评估"])
    if r_label == "高风险":
        return _out("pause", reasons + ["风险过高：优先控制风险，暂停新增仓位"],
                    score, score_alt, w_used, scope,
                    ["风险等级回落至中/低风险", "趋势重新转为上升或震荡企稳"])
    if t_label == "下降":
        return _out("pause", reasons + ["长期趋势向下：即使估值偏低也不主动加仓"],
                    score, score_alt, w_used, scope,
                    ["MA20/MA60/MA120 结构转多", "风险指标不高于中风险"])

    # --- 主动加仓：高分 + 趋势在 + 估值非高估 + 闸门不加仓阻断 ---
    if score is not None and score >= 70:
        if v_label == "高估":
            return _out("hold", reasons + ["估值高估：趋势尚在则持有观察、分批止盈，不追高"],
                        score, score_alt, w_used, scope,
                        ["估值分位回落至 70% 以下", "回踩企稳不破关键均线"])
        if v is None:
            return _out("hold", reasons + ["估值数据不足：评分升级受限，仅持有不主动加仓"],
                        score, score_alt, w_used, scope,
                        ["官方PE积累达到 200 条后启用估值维度"])
        if gate_block_add:
            return _out("pause", reasons + ["数据质量闸门阻断：虽评分较高，但数据不满足加仓条件"],
                        score, score_alt, w_used, scope,
                        ["闸门告警项全部消除（行情/风险/样本）"])
        return _out("allow_add", reasons + ["三维共振：允许按 40/30/30 分批执行"],
                    score, score_alt, w_used, scope,
                    ["首批后回踩 MA20 站稳再执行第二笔", "突破近 20 日高点且有量能确认再执行第三笔"])

    if score is not None and score >= 55:
        if v_label == "高估":
            return _out("hold", reasons + ["估值偏高：持有底仓，不新增主动仓位"],
                        score, score_alt, w_used, scope,
                        ["估值分位回落", "风险等级不上升"])
        return _out("normal_dca", reasons + ["按长期定投纪律执行，不追高、不加速"],
                    score, score_alt, w_used, scope,
                    ["保持基准权重节奏", "出现回踩再考虑分批加仓"])

    if score is not None and score >= 40:
        return _out("hold", reasons + ["多空交织：持有观察，等待方向确认"],
                    score, score_alt, w_used, scope,
                    ["趋势转为上升", "估值分位进入低估区间"])

    if t_label == "上升":
        return _out("hold", reasons + ["估值偏高但趋势尚在：持有观察，反弹分批止盈"],
                    score, score_alt, w_used, scope,
                    ["不追高；跌破 MA20 且趋势转弱则减仓"])
    return _out("pause", reasons + ["估值与趋势均不利：暂停新增，控制风险"],
                score, score_alt, w_used, scope,
                ["估值分位回落至 40% 以下", "趋势结构转多"])



def batch_plan(trend: dict, risk: dict, val: dict, gate: dict | None = None) -> dict:
    """
    分批建仓纪律（参考执行计划，V4 增加数据质量闸门否决）：
      第一笔 40%：趋势条件成立（当前）；
      第二笔 30%：回踩 MA20 附近并重新站稳；
      第三笔 30%：突破近 20 日高点并有成交确认。
    数据质量闸门阻断 / 趋势下降 / 风险过高 / 风险数据不足 → 暂停新增主动仓位；估值高估 → 暂缓新增。
    """
    t_label = trend.get("label")
    r_label = risk.get("label")
    v_label = val.get("label")
    gate = gate or {}
    if gate.get("fatal") or gate.get("block_add"):
        return {"status": "暂停", "color": "#3b82f6",
                "text": "数据质量闸门未通过：暂停新增主动仓位，待数据可用性恢复"}
    if t_label == "下降" or r_label == "高风险":
        return {"status": "暂停", "color": "#3b82f6",
                "text": "趋势下降或风险过高：暂停新增主动仓位，不机械补仓"}
    if not risk.get("available", False):
        return {"status": "暂停", "color": "#3b82f6",
                "text": "风险数据不足（未知风险≠低风险）：暂停主动加仓，仅维持既定底仓"}
    if v_label == "高估":
        return {"status": "暂缓", "color": "#eab308",
                "text": "估值高估：暂缓新增，等待估值回落或趋势确认"}
    if v_label == "数据不足":
        return {"status": "暂缓", "color": "#eab308",
                "text": "估值数据不足：不机械新增主动仓位，待官方PE积累≥200条后按纪律分批"}
    steps = [
        {"phase": "第一笔", "ratio": "40%", "trigger": "趋势条件仍然成立（当前）"},
        {"phase": "第二笔", "ratio": "30%", "trigger": "回踩 MA20 附近并重新站稳"},
        {"phase": "第三笔", "ratio": "30%", "trigger": "突破近 20 日高点并有成交确认"},
    ]
    return {"status": "允许", "color": "#ef4444", "text": "允许分批建仓（参考执行计划）", "steps": steps}


def portfolio_check(etf_configs: list, decisions: list, actual_w: dict | None = None,
                    deviation_pp: float = 10.0) -> dict:
    """
    V4 组合配置体检（P0-4 一致性 + P1 集中度 + 实际持仓偏离）：
      - 基准权重合计是否等于 100；
      - 单品种基准权重是否超过单品种上限；
      - 红利家族等同类分组合计是否超过分组上限；
      - 强周期品种是否超过研究建议上限（仅提示，不强制）；
      - actual_w（实际持仓小数，可选）：实际权重相对基准的绝对百分点偏离，超 deviation_pp 标记。
    """
    flags = []
    base_sum = 0
    singles = {}
    for cfg in etf_configs:
        code = cfg.get("code")
        bw = float(cfg.get("base_w") or 0)
        base_sum += bw
        singles[code] = bw
        if cfg.get("max_w") is not None and bw > float(cfg["max_w"]):
            flags.append({"level": "warn", "label": f"{cfg.get('name', code)} 基准{bw}% 超上限{cfg['max_w']}%"})
        cap = cfg.get("research_cap_w")
        if cap is not None and bw > float(cap):
            flags.append({"level": "info",
                          "label": f"{cfg.get('name', code)} 基准{bw}% 高于稳健研究建议上限{cap}%（强周期，非强制）"})
    if abs(base_sum - 100) > 0.01:
        flags.append({"level": "warn", "label": f"基准权重合计 {base_sum:.0f}% ≠ 100%"})

    deviations = []
    if actual_w:
        for cfg in etf_configs:
            code = cfg.get("code")
            tgt = float(cfg.get("base_w") or 0) / 100.0
            act = float(actual_w.get(code, 0.0))
            dev_pp = (act - tgt) * 100.0
            if abs(dev_pp) >= deviation_pp:
                flags.append({"level": "warn",
                              "label": f"{cfg.get('name', code)} 实际{act*100:.0f}% 偏离基准{tgt*100:.0f}% 达 {dev_pp:+.0f}pp"})
            if act*100 > float(cfg.get("max_w") or 100):
                flags.append({"level": "warn",
                              "label": f"{cfg.get('name', code)} 实际{act*100:.0f}% 超上限{cfg.get('max_w')}%"})
            deviations.append({"code": code, "target_pct": round(tgt*100, 2),
                               "actual_pct": round(act*100, 2), "dev_pp": round(dev_pp, 2)})

    return {"base_sum": round(base_sum, 2), "ok": abs(base_sum - 100) < 0.01, "flags": flags,
            "deviations": deviations,
            "has_actual_holdings": bool(actual_w),
            "note": ("已按实际持仓计算偏离。" if actual_w else
                     "未录入实际持仓：集中度按基准权重静态校验，实际仓位偏离需持仓数据。")}


def market_regime(macro: dict | None, broad_trend_label: str | None = None) -> dict:
    """
    五步决策第二步：市场环境判断（利率/流动性/趋势/宏观）。
    macro: {available:int, items:[{label,status(ok/stale/fail),...}]}；
    broad_trend_label: 核心宽基（如沪深300）趋势标签。
    返回 regime: supportive（支持加仓）/ neutral / cautious（谨慎）/ unknown。
    规则：不凭单一宏观指标下结论；宽基趋势下降或宏观多数失效 → 谨慎/未知。
    """
    items = (macro or {}).get("items") or []
    ok_n = sum(1 for it in items if it.get("status") == "ok")
    fail_n = sum(1 for it in items if it.get("status") == "fail")
    total = len(items)
    if broad_trend_label == "下降":
        return {"regime": "cautious", "label": "谨慎（宽基趋势向下）",
                "basis": "核心宽基趋势下降，主动加仓受限", "macro_ok": ok_n, "macro_total": total}
    if total and fail_n / total > 0.5:
        return {"regime": "unknown", "label": "环境未知（宏观多数失效）",
                "basis": "宏观指标过半获取失败，不据此生成信号", "macro_ok": ok_n, "macro_total": total}
    if broad_trend_label == "上升" and ok_n >= max(1, total // 2):
        return {"regime": "supportive", "label": "环境支持（趋势向上、宏观可用）",
                "basis": "宽基趋势向上且宏观指标可用", "macro_ok": ok_n, "macro_total": total}
    return {"regime": "neutral", "label": "中性",
            "basis": "宏观与趋势无明显利空，按既定纪律执行", "macro_ok": ok_n, "macro_total": total}


def rebalance_plan(target_w: dict, actual_w: dict | None, run_date: str,
                   calendar_months: list, deviation_pp: float,
                   portfolio_dd_pct: float | None = None,
                   dd_warn_pct: float | None = None, dd_hard_pct: float | None = None,
                   has_cash_inflow: bool = False, fee_rate: float = 0.001,
                   concentration_flag: bool = False) -> dict:
    """
    再平衡评估（GPT P1-4）：四种触发任一满足即评估，结合成本与风险预算决定是否执行。
      1. calendar：运行月 ∈ calendar_months；
      2. deviation：任一实际权重相对基准绝对偏离 ≥ deviation_pp；
      3. cashflow：有新增资金（可借资金流入低冲击再平衡）；
      4. risk_budget：组合回撤触及 warn/hard，或集中度超限。
    target_w/actual_w: code -> 小数。
    返回 triggers、should_execute、目标/实际/Δpp、单边换手与预估成本（%）。
    """
    triggers = []
    try:
        month = pd.to_datetime(run_date).month
        if month in [int(m) for m in calendar_months]:
            triggers.append({"type": "calendar", "label": f"日历再平衡（{month}月）"})
    except Exception:
        pass

    deviations = []
    dev_hit = False
    if actual_w:
        for code, tgt in target_w.items():
            act = float(actual_w.get(code, 0.0))
            d_pp = (act - float(tgt)) * 100.0
            deviations.append({"code": code, "target_pct": round(float(tgt)*100, 2),
                               "actual_pct": round(act*100, 2), "delta_pp": round(d_pp, 2)})
            if abs(d_pp) >= deviation_pp:
                dev_hit = True
    if dev_hit:
        triggers.append({"type": "deviation",
                         "label": f"仓位偏离达 ±{deviation_pp:g}pp（绝对百分点）"})

    if has_cash_inflow:
        triggers.append({"type": "cashflow", "label": "资金流入再平衡（新增资金优先补低配）"})

    rb_hit = False
    if portfolio_dd_pct is not None and dd_warn_pct is not None:
        if portfolio_dd_pct <= dd_warn_pct:
            rb_hit = True
    if concentration_flag:
        rb_hit = True
    if rb_hit:
        triggers.append({"type": "risk_budget", "label": "风险预算/集中度触发，优先降风险"})

    # 单边换手 = 0.5*Σ|Δw|（小数）；成本 = 换手 × fee
    turnover = 0.0
    if deviations:
        turnover = sum(abs(d["delta_pp"]) for d in deviations) / 100.0 / 2.0
    est_cost_pct = round(turnover * fee_rate * 100, 4)

    return {
        "triggers": triggers, "should_evaluate": bool(triggers),
        "should_execute": bool(dev_hit or rb_hit),
        "execution_note": ("偏离或风险预算触发：建议执行，向基准权重收敛并优先降风险"
                           if (dev_hit or rb_hit) else
                           ("仅日历/资金流入：评估成本后决定，无明显偏离可不交易"
                            if triggers else "无再平衡触发，维持现状")),
        "deviations": deviations,
        "one_way_turnover_pct": round(turnover * 100, 3),
        "est_cost_pct": est_cost_pct,
        "deviation_mode": "absolute_pp",
    }


def discipline_layers(gate: dict, regime: dict, rebalance: dict) -> dict:
    """
    三层纪律（GPT P1-4）显式状态：
      layer1 长期定投：fatal 时暂停，否则维持战略定投；
      layer2 风险控制：闸门 block_add / 环境谨慎时限制主动加仓；
      layer3 估值再平衡：透传再平衡计划。
    """
    gate = gate or {}
    regime = regime or {}
    layer1 = {
        "name": "第一层 · 长期定投纪律",
        "status": "暂停" if gate.get("fatal") else "维持",
        "desc": "致命闸门未过，暂停当日定投" if gate.get("fatal")
                else "按战略目标持续定投，不因短期波动随意停止",
    }
    restrictions = []
    if gate.get("block_add"):
        restrictions.append("数据质量闸门阻断主动加仓")
    if regime.get("regime") in ("cautious", "unknown"):
        restrictions.append(regime.get("label", "市场环境不支持主动加仓"))
    layer2 = {
        "name": "第二层 · 风险控制纪律",
        "status": "限制加仓" if restrictions else "正常",
        "desc": "；".join(restrictions) if restrictions
                else "趋势/回撤/集中度未触限，可按纪律加仓",
    }
    layer3 = {
        "name": "第三层 · 估值与再平衡纪律",
        "status": "触发" if rebalance.get("should_evaluate") else "无触发",
        "desc": rebalance.get("execution_note", "维持现状"),
        "triggers": [t["type"] for t in rebalance.get("triggers", [])],
    }
    return {"layers": [layer1, layer2, layer3],
            "summary": " / ".join([layer1["status"], layer2["status"], layer3["status"]])}


def atr_ref(close: float | None, atr: float | None) -> dict:
    """ATR 波动率参考价位（非止损指令、非保证成交价）"""
    if close is None or atr is None or atr <= 0:
        return {"text": "ATR 数据不足，暂无参考价位"}
    return {
        "atr": round(atr, 4),
        "ref_low": round(close - atr, 4),
        "ref_high": round(close + atr, 4),
        "watch_low": round(close - 1.5 * atr, 4),
        "watch_high": round(close + 1.5 * atr, 4),
        "text": (f"ATR={atr:.4f}（{atr / close * 100:.1f}%），参考波动区间=现价±1×ATR：{close - atr:.4f}~{close + atr:.4f}；"
                 f"跌破 {close - 1.5 * atr:.4f}（-1.5×ATR）关注企稳，突破 {close + 1.5 * atr:.4f}（+1.5×ATR）关注趋势确认"),
    }


def clean_pe_series(pe_series: "pd.Series | None") -> "pd.Series | None":
    """
    清洗 PE 序列用于分位计算（国证980092等官方历史含负值/极端值时的必要前置）：
      1) 剔除 PE<=0（负 PE 无分位经济意义；980092 在 2019-04~2022 大段为负）
      2) 对正值序列按 1%/99% 分位 winsorize（公告日 TTM 基准切换的极端值如 ±数千不污染参考系）
    返回清洗后的 Series（index 保留原日期）；空/None 原样返回。
    """
    if pe_series is None:
        return None
    s = pd.Series(pe_series).dropna()
    s = s[s > 0]
    if len(s) < 2:
        return s
    lo = float(s.quantile(0.01))
    hi = float(s.quantile(0.99))
    if hi > lo:
        s = s.clip(lo, hi)
    return s


def official_pe_windows(pe_hist: pd.DataFrame | None) -> dict:
    """
    基于积累库官方 PE 序列计算三窗口分位（决策引擎估值维度使用）。
    pe_hist: 该指数官方 PE 历史（含 date, pe），升序。
    返回 {窗口名: {pct,n,start,end}} 或 None；样本不足的窗口为 None；总记录 < PE_MIN_OFFICIAL 返回 None。
    分位计算前先清洗（clean_pe_series：剔除 PE<=0、1%/99% winsorize），
    否则 980092 的负值段与公告日极端值会使近5年分位严重失真。
    """
    if pe_hist is None or pe_hist.empty:
        return None
    s = pe_hist.dropna(subset=["pe"]).sort_values("date")
    if len(s) < PE_MIN_OFFICIAL:
        return None
    s = s.set_index(pd.to_datetime(s["date"]))["pe"].astype(float)
    s = clean_pe_series(s)
    if s is None or len(s) < PE_MIN_OFFICIAL:
        return None
    out = {}
    for label, n in PE_WINDOWS.items():
        w = s.tail(n)
        if len(w) < max(int(n * 0.80), PE_MIN_OFFICIAL):
            out[label] = None
            continue
        reference = w.iloc[:-1] if len(w) > 1 else w
        pct = float((reference < float(w.iloc[-1])).sum() / len(reference) * 100)
        out[label] = {"pct": round(pct, 1), "n": int(len(w)),
                      "start": str(w.index[0].date()), "end": str(w.index[-1].date())}
    return out
