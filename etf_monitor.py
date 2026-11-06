# -*- coding: utf-8 -*-
"""
ETF 智能监测与辅助决策看板（V4）
================================
监测标的：515180 红利ETF / 516310 银行ETF / 515220 煤炭ETF / 159201 自由现金流ETF / 563020 红利低波ETF
核心指标：价格趋势(MA20/MA60/MA120/BOLL/RSI)、指数估值分位、创业板红利比、
         风险指标(ATR/ADX/波动率/回撤)、交易决策简报(趋势×估值×风险三维)
输出     ：单窗口可视化看板 output/etf_dashboard.html（浏览器打开即看全部）
用法     ：python etf_monitor.py [--open]
数据源   ：东方财富/腾讯/新浪(行情回退链)、中证指数官网(官方估值+中证系指数官方日线)、日频PE序列(官方最新PE×价格比估算)
分位口径 ：近1年/近3年/近5年三窗口滚动分位，主信号取近3年；分位旁标注样本数(交易日)与时间跨度

V4 升级（依据 GPT 代码评审 P0，第一阶段四项）：
  1) 数据质量闸门（decision_engine.data_gate）：行情滞后/样本不足/趋势缺失/风险缺失/
     估值不足/来源降级/PE口径陈旧逐项校验，前序闸门对最终建议有否决权；
  2) 风险引擎修复：风险指标全缺失时明确判"数据不足"，不再误判"低风险"；缺失维度
     不再用 0.5 中性值填充，只在可用维度上归一化权重；
  3) 决策输出统一为六种操作状态（允许分批加仓/正常定投/持有观察/暂停新增/再平衡评估/暂不判断），
     每条附理由、数据日期、仓位上限与"下一步条件"，综合评分仅作辅助排序；
  4) 中央配置 portfolio_config.json：ETF池/基准权重/仓位上下限/资产类别/集中度/调仓阈值/
     引擎阈值/回测成本/数据质量阈值唯一来源（页面、文档、回测同一口径）。

说明     ：本工具仅为数据监测与量化参考，不构成投资建议。
"""
import os
import sys
import json
import time
import re
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import akshare as ak

import decision_engine

# ============================================================
# 0. 配置区
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "output"
LIBS_DIR = BASE_DIR / "libs"
OUT_DIR.mkdir(exist_ok=True)

DASHBOARD_PATH = OUT_DIR / "etf_dashboard.html"
HISTORY_PATH = OUT_DIR / "valuation_history.csv"
LOG_PATH = OUT_DIR / "run_log.txt"

# 行情数据源生效统计（千问评审二轮D-11：看板展示实际生效源，避免“标识ok但实际频繁降级”）
SRC_STATS = {"ok_em": 0, "ok_tx": 0, "ok_sina": 0, "ok_cs": 0, "fail_em": 0}

# ------------------------------------------------------------
# V4 中央配置：portfolio_config.json 为唯一来源
# （ETF池/基准权重/仓位上下限/资产类别/集中度/调仓阈值/引擎阈值/回测成本/数据质量阈值）
# 配置文件缺失或损坏时回退内置默认，保证云端仍可运行（日志与页面会标注回退状态）。
# ------------------------------------------------------------
CONFIG_PATH = BASE_DIR / "portfolio_config.json"

_BUILTIN_ETFS = [
    {"code": "515180", "name": "红利ETF", "index_code": "000922", "index_name": "中证红利",
     "asset_class": "style_satellite", "role": "高股息压舱石", "color": "#e6b31e",
     "base_w": 25, "max_w": 40, "min_w": 12, "cyclic": False},
    {"code": "516310", "name": "银行ETF", "index_code": "399986", "index_name": "中证银行",
     "asset_class": "style_satellite", "role": "低估值稳息品种", "color": "#3b82f6",
     "base_w": 20, "max_w": 30, "min_w": 8, "cyclic": False},
    {"code": "515220", "name": "煤炭ETF", "index_code": "000820", "index_name": "中证煤炭",
     "asset_class": "style_satellite", "role": "强周期高波动品种", "color": "#f97316",
     "base_w": 20, "max_w": 30, "min_w": 8, "cyclic": True, "research_cap_w": 10,
     "note": "强周期行业：盈利下行期 PE 被动抬升，高分位≠高估，需结合煤价位置/盈利与股息率判断；参考做T与仓位纪律而非单纯PE分位"},
    {"code": "159201", "name": "自由现金流ETF", "index_code": "980092", "index_name": "国证自由现金流",
     "asset_class": "style_satellite", "role": "现金流质量成长品种", "color": "#14b8a6",
     "base_w": 15, "max_w": 25, "min_w": 8, "cyclic": False,
     "note": "2025-02上市、历史<3年：官方PE已接入（iFinD全历史播种2016-01起+腾讯日频增量，国证980092指数口径，PE-TTM），估值参与决策评分；另保留十大权重加权参考估值(腾讯)与IOPV溢价率作观测（非官方指数估值、仅参考）；以价格/趋势/技术面与组合相关性为主要追踪维度"},
    {"code": "563020", "name": "红利低波ETF", "index_code": "H30269", "index_name": "中证红利低波动",
     "asset_class": "style_satellite", "role": "低波红利压舱石", "color": "#a78bfa",
     "base_w": 20, "max_w": 30, "min_w": 10, "cyclic": False,
     "note": "2023-12上市（约3年）；指数基日2005-12-30、官方PE全历史可用（中证官网indexCsiDsPe，2013-12起），估值分位完整；以低波动+高股息为特征，年化波动率与相对红利超额（vs红利ETF 515180）为专属观察维度"},
]


def _load_portfolio_config():
    """读取中央配置；失败返回 None（由调用链回退内置默认）。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[配置] portfolio_config.json 读取失败，使用内置默认: {e}")
        return None


_CFG = _load_portfolio_config()
V4_VERSION = (_CFG or {}).get("version", "V4.0")
STRATEGY_VERSION = (_CFG or {}).get("strategy_version", "v4-p0-builtin")
CONFIG_SOURCE = "portfolio_config.json" if _CFG else "内置默认（配置文件缺失）"

ETF_POOL = {}
for _e in ((_CFG or {}).get("etfs") or _BUILTIN_ETFS):
    ETF_POOL[_e["name"]] = {
        "code": _e["code"], "index_code": _e["index_code"], "index_name": _e["index_name"],
        "color": _e.get("color", "#94a3b8"), "role": _e.get("role", ""),
        "base_w": _e.get("base_w", 0), "max_w": _e.get("max_w", 0), "min_w": _e.get("min_w", 0),
        "cyclic": bool(_e.get("cyclic", False)),
        "asset_class": _e.get("asset_class", "style_satellite"),
        "research_cap_w": _e.get("research_cap_w"),
        "note": _e.get("note"),
    }

# 组合/引擎/回测/数据质量参数（配置缺失时用内置默认）
PORTFOLIO_CFG = (_CFG or {}).get("portfolio", {}) or {}
ENGINE_CFG = (_CFG or {}).get("engine", {}) or {}
BT_CFG = (_CFG or {}).get("backtest", {}) or {}
DQ_CFG = (_CFG or {}).get("data_quality", {}) or {}
GATE_PRICE_STALE = int(DQ_CFG.get("price_stale_days", 3))
GATE_PRICE_HARD = 7          # 超过 7 天视为行情断更（3~7 天可能为休市，仅告警不加仓）
GATE_MIN_ROWS = int(DQ_CFG.get("min_price_rows", 130))
GATE_PE_STALE = int(DQ_CFG.get("pe_stale_days", 10))

# 创业板红利比 = 创业板指(399006) / 中证红利(000922)
GEM_INDEX = "399006"
DIV_INDEX = "000922"

# 历史回看年数（价格类指标分位/均线用）
HISTORY_YEARS = 3
# 价格获取窗口（年）：需覆盖最长 PE 分位窗口（近5年≈1250 交易日）
PRICE_YEARS = 5
# PE 分位滚动窗口（交易日）：近1年/近3年/近5年
PE_WINDOWS = {"近1年": 250, "近3年": 750, "近5年": 1250}
PE_MAIN_WINDOW = "近3年"   # 主信号（估值打分/定投档位/长期仓位）使用的窗口
# 指数估值来源代码（中证指数官网）
CSINDEX_MAP = {"000922": "000922", "399986": "399986", "000820": "000820", "H30269": "H30269"}

# 信号阈值
TH = {
    "pe_low": 30, "pe_high": 70,          # PE分位 低估/高估
    "rsi_low": 35, "rsi_high": 70,        # RSI 超卖/超买
    "dev": 5.0,                           # 偏离20日线阈值%
    "ratio_low": 0.35, "ratio_high": 0.60 # （V2 起弃用：创业板红利比改用归一化相对强弱 + 分位30/70解释，此阈值仅保留）
}

# 宏观环境阈值（可自行调整）
MACRO_TH = {
    "y10_low": 2.0, "y10_high": 2.8,   # 10Y国债收益率%：<2.0 宽松支撑红利 / >2.8 偏紧
    "mkt_hi": 1.2, "mkt_lo": 0.8,      # 两市成交额较20日均量：>1.2 放量 / <0.8 缩量
}
# 政策风向：无法自动抓取定性政策，每周手动更新此条目（看板原样展示）
MACRO_POLICY = {
    "title": "政策风向（手动维护）",
    "text": "当前无新增重大政策信号；关注点：LPR 利率走向、财政发力节奏、行业监管动态。",
    "date": "2026-11-04",   # 条目最后维护日（DeepSeek评审P3：手动条目需显式时间戳，与自动采集同一新鲜度逻辑）
}

REQUEST_TIMEOUT = 20

# ============================================================
# 1. 工具函数
# ============================================================
def log(msg: str):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def safe_call(name, fn, *args, **kwargs):
    """带超时与异常捕获的统一调用，失败返回 None；attempts 控制重试次数。
    千问评审二轮D-10：重试过程不逐条刷屏，最终失败仅记一条汇总日志（含错误类型与次数）。"""
    attempts = int(kwargs.pop("attempts", 1))
    last = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(2)
    if last is not None:
        log(f"[数据源] {name} {attempts}次尝试均失败: {type(last).__name__}: {str(last)[:120]}")
    return None


def normalize_daily(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """统一行情字段并检查日期、价格、重复记录。"""
    if df is None or df.empty:
        raise ValueError(f"{source}: 空行情")
    aliases = {"日期":"date", "开盘":"open", "收盘":"close", "最高":"high", "最低":"low",
               "成交量":"volume", "成交额":"amount", "涨跌幅":"pct_chg"}
    df = df.rename(columns=aliases).copy()
    if not {"date", "close"}.issubset(df.columns):
        raise ValueError(f"{source}: 缺少 date/close 字段")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df = df[df["close"] > 0].sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    if df.empty: raise ValueError(f"{source}: 清洗后无有效行情")
    if "pct_chg" not in df.columns: df["pct_chg"] = df["close"].pct_change() * 100
    return df


# ============================================================
# 2. 数据获取（东财优先，新浪兜底——海外服务器(如GitHub Actions)访问东财会被拒绝）
# ============================================================
def get_etf_daily_em(code: str):
    """ETF 前复权日线（东方财富）"""
    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=int(PRICE_YEARS * 365))).strftime("%Y%m%d")
    df = ak.fund_etf_hist_em(symbol=code, period="daily",
                             start_date=start, end_date=end, adjust="qfq")
    return normalize_daily(df, "东方财富ETF")


def get_etf_daily_sina(code: str):
    """ETF 日线（新浪，海外可访问；新浪接口为不复权数据，仅作东财失败时的兜底）"""
    prefix = "sh" if str(code).startswith("5") else "sz"
    df = ak.fund_etf_hist_sina(symbol=f"{prefix}{code}")
    df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                            "最高": "high", "最低": "low"})
    return normalize_daily(df, "备用行情")


def get_etf_daily_tx(code: str):
    """ETF 日线（腾讯，海外可访问、数据新鲜；接口为不复权数据，仅作东财失败时的兜底）"""
    prefix = "sh" if str(code).startswith("5") else "sz"
    df = ak.stock_zh_a_hist_tx(symbol=f"{prefix}{code}")
    df = df.rename(columns={"date": "date", "open": "open", "close": "close",
                            "high": "high", "low": "low"})
    return normalize_daily(df, "备用行情")


def get_etf_daily(code: str):
    """ETF 日线：东财优先（重试2次），失败依次切腾讯、新浪（腾讯带新鲜度门槛）。
    同时累计实际生效源（SRC_STATS）供看板展示数据源真实性。"""
    df = safe_call("东财ETF行情", get_etf_daily_em, code, attempts=2)
    if df is None:
        SRC_STATS["fail_em"] += 2
        log("东财ETF行情不可用，尝试腾讯源")
        df = safe_call("腾讯ETF行情", get_etf_daily_tx, code)
        if df is not None:
            SRC_STATS["ok_tx"] += 1
    else:
        SRC_STATS["ok_em"] += 1
    if df is not None and df["date"].max() < pd.Timestamp(dt.date.today()) - pd.Timedelta(days=10):
        log(f"腾讯ETF行情数据过期(末日期 {df['date'].max().date()})，跳过")
        df = None
    if df is None:
        log("腾讯ETF行情不可用，尝试新浪源")
        df = safe_call("新浪ETF行情", get_etf_daily_sina, code)
        if df is not None:
            SRC_STATS["ok_sina"] += 1
    return df


# ------------------------------------------------------------
# 159201 自由现金流ETF 参考估值（官方估值已接入后的补充观测）
# ------------------------------------------------------------
# 背景：159201 跟踪国证自由现金流指数 980092。官方 PE 已接入决策链路
#       （iFinD 全历史播种2016-01起 + 腾讯官方指数行情日频增量，见
#       get_valuation 国证分支/seed_valuation_980092.py）。
#       下方为补充观测：基金季报前十大权重 × 腾讯个股 PE(TTM)/PB 做加权估算，
#       并附 IOPV 溢价率。口径：非官方指数估值、仅参考展示，不参与决策评分。
HOLDINGS_F10_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code=159201&topline=10"


def get_holdings_159201(max_n: int = 10):
    """天天基金 F10 持仓明细 → 最近季度前十大 [(code, name, weight_pct)]。
    返回空列表表示失败/无可解析数据（调用方 safe_call 降级）。"""
    hdrs = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Referer": "https://fundf10.eastmoney.com/",
    }
    r = requests.get(HOLDINGS_F10_URL, headers=hdrs, timeout=15)
    r.raise_for_status()
    m = re.search(r'var apidata=\s*\{ content:"(.*?)"\s*\}', r.text, re.S)
    html = m.group(1) if m else r.text
    html = (html.replace('\\"', '"').replace('\\/', '/')
                .replace('\\n', ' ').replace('\\t', ' '))
    out = []
    for row in re.findall(r'<tr>(.*?)</tr>', html, re.S):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
        if len(cells) < 7:
            continue
        code_m = re.search(r'>(\d{6})<', cells[1])
        name = re.sub(r'<[^>]+>', '', cells[2]).strip()
        w_m = re.search(r'([\d.]+)%', cells[6])
        if not code_m or not w_m:
            continue
        out.append((code_m.group(1), name, float(w_m.group(1))))
        if len(out) >= max_n:
            break
    return out


def get_weighted_val_159201():
    """159201 补充观测估值：前十大权重（归一化）× 腾讯个股 PE(TTM)/PB + IOPV 溢价率。
    非官方指数估值、仅参考展示（官方 PE 已由国证980092指数口径接入）。
    返回 {"pe":, "pb":, "n":, "total_w":, "premium":, "iopv":} 或 None。"""
    holdings = get_holdings_159201(10)
    if len(holdings) < 5:
        return None
    total_w = sum(w for _, _, w in holdings)
    pe_num = pb_num = 0.0
    n_pe = n_pb = 0
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120"}
    for code, _name, w in holdings:
        prefix = "sh" if str(code).startswith("6") else "sz"
        try:
            rr = requests.get(f"http://qt.gtimg.cn/q={prefix}{code}", headers=ua, timeout=8)
            f = rr.content.decode("gbk", errors="replace").split('="', 1)[1].rstrip('";\n').split('~')
        except Exception:
            continue
        try:
            pe = float(f[39]) if len(f) > 39 and f[39] not in ("", "-") else None
            pb = float(f[43]) if len(f) > 43 and f[43] not in ("", "-") else None
        except Exception:
            continue
        wn = w / total_w
        if pe is not None:
            pe_num += wn * pe; n_pe += 1
        if pb is not None:
            pb_num += wn * pb; n_pb += 1
    if n_pe == 0 or n_pb == 0:
        return None
    # IOPV 溢价率：159201 场内现价(f3) vs 参考净值/IOPV(f51)
    premium = iopv = None
    try:
        rr = requests.get("http://qt.gtimg.cn/q=sz159201", headers=ua, timeout=8)
        f = rr.content.decode("gbk", errors="replace").split('="', 1)[1].rstrip('";\n').split('~')
        price = float(f[3])
        iopv = float(f[51]) if len(f) > 51 and f[51] not in ("", "-") else None
        if iopv:
            premium = (price / iopv - 1) * 100
    except Exception:
        premium = iopv = None
    return {"pe": pe_num, "pb": pb_num, "n": n_pe, "total_w": total_w,
            "premium": premium, "iopv": iopv}


def get_index_daily_csindex(code: str):
    """指数日线（中证官网官方，中证系指数专用）。
    腾讯/新浪/东财对 H30269 等中证指数代码均无日线数据，中证官网 index-perf 接口提供官方全历史。
    返回 DataFrame: date/open/high/low/close 按日期升序。"""
    try:
        import requests as _rq
        hdrs = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            "Referer": f"https://www.csindex.com.cn/zh-CN/indices/index-detail/{code}",
        }
        url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
               f"?indexCode={code}&startDate=20050101&endDate={dt.date.today().strftime('%Y%m%d')}")
        r = _rq.get(url, headers=hdrs, timeout=20)
        r.raise_for_status()
        rows = (r.json() or {}).get("data") or []
        if not rows:
            return None
        df = pd.DataFrame(rows).rename(columns={"tradeDate": "date", "open": "open",
                                                "high": "high", "low": "low", "close": "close"})
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
        for c in ["open", "high", "low", "close"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["date", "close"]).sort_values("date")
        df = df.drop_duplicates("date", keep="last").reset_index(drop=True)
        return df[["date", "open", "high", "low", "close"]]
    except Exception as e:
        log(f"中证官网指数行情获取失败: {e}")
        return None


def get_index_daily_em(code: str):
    """指数日线（东方财富）"""
    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=int(PRICE_YEARS * 365))).strftime("%Y%m%d")
    df = ak.index_zh_a_hist(symbol=code, period="daily",
                            start_date=start, end_date=end)
    df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                            "最高": "high", "最低": "low", "涨跌幅": "pct_chg"})
    return normalize_daily(df, "指数行情")


def get_index_daily_sina(code: str):
    """指数日线（新浪，海外可访问）"""
    prefix = "sh" if str(code).startswith("000") else "sz"
    df = ak.stock_zh_index_daily(symbol=f"{prefix}{code}")
    df = df.rename(columns={"date": "date", "open": "open", "close": "close",
                            "high": "high", "low": "low"})
    return normalize_daily(df, "备用行情")


def get_index_daily_tx(code: str):
    """指数日线（腾讯，海外可访问，数据新鲜）"""
    prefix = "sh" if str(code).startswith("000") else "sz"
    df = ak.stock_zh_index_daily_tx(symbol=f"{prefix}{code}")
    df = df.rename(columns={"date": "date", "open": "open", "close": "close",
                            "high": "high", "low": "low"})
    return normalize_daily(df, "备用行情")


def get_index_daily(code: str):
    """指数日线：中证系指数（CSINDEX_MAP 内）优先中证官网官方日线（H30269 等腾讯/新浪/东财均无），
    失败回退常规链；常规链东财优先，失败依次切新浪、腾讯；
    新浪源部分指数停更（如 000922 只到 2019），过期数据视为失败继续下探"""
    if str(code) in CSINDEX_MAP:
        df = safe_call("中证官网指数行情", get_index_daily_csindex, code, attempts=1)
        if df is not None and not df.empty:
            SRC_STATS["ok_cs"] += 1
            return df
        log("中证官网指数行情不可用，回退常规通道")
    df = safe_call("东财指数行情", get_index_daily_em, code, attempts=2)
    if df is None:
        SRC_STATS["fail_em"] += 2
        log("东财指数行情不可用，尝试新浪源")
        df = safe_call("新浪指数行情", get_index_daily_sina, code)
        if df is not None:
            SRC_STATS["ok_sina"] += 1
    else:
        SRC_STATS["ok_em"] += 1
    if df is not None and df["date"].max() < pd.Timestamp(dt.date.today()) - pd.Timedelta(days=10):
        log(f"新浪指数行情数据过期(末日期 {df['date'].max().date()})，跳过")
        df = None
    if df is None:
        log("新浪指数行情不可用，尝试腾讯源")
        df = safe_call("腾讯指数行情", get_index_daily_tx, code)
        if df is not None:
            SRC_STATS["ok_tx"] += 1
    return df


def get_index_valuation_csindex(index_code: str, start_date: str = "20050101"):
    """中证指数官网估值（官方）。
    优先官网估值图 JSON 接口 indexCsiDsPe：全历史日频 PE（字段 peg/tradeDate，startDate 可指定），
    一次拉取即可补齐指数上市以来全部官方 PE 观测；失败回退 XLS 静态文件（近20期，市盈率1口径）。
    返回 DataFrame: date, pe, pb, dy 已按日期升序。
    """
    try:
        import requests as _rq
        hdrs = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            "Referer": f"https://www.csindex.com.cn/zh-CN/indices/index-detail/{index_code}",
        }
        url = ("https://www.csindex.com.cn/csindex-home/perf/indexCsiDsPe"
               f"?indexCode={index_code}&startDate={start_date}&endDate={dt.date.today().strftime('%Y%m%d')}")
        r = _rq.get(url, headers=hdrs, timeout=20)
        r.raise_for_status()
        rows = (r.json() or {}).get("data") or []
        if rows:
            df = pd.DataFrame(rows).rename(columns={"tradeDate": "date", "peg": "pe"})
            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
            df["pe"] = pd.to_numeric(df["pe"], errors="coerce")
            df = df.dropna(subset=["date", "pe"])
            df = df[df["pe"] > 0].sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
            if not df.empty:
                df["pb"] = np.nan
                df["dy"] = np.nan
                return df[["date", "pe", "pb", "dy"]]
    except Exception:
        pass
    # 回退：XLS 静态文件（近约20期，市盈率1口径）。不写积累库——
    # 避免与 peg 全历史口径按日期冲突覆盖（详见 README「官方 PE 口径」）。
    # 返回 None 时由 get_valuation 走积累库兜底展示最近值。
    try:
        df = ak.stock_zh_index_value_csindex(symbol=CSINDEX_MAP.get(index_code, index_code))
        df = df.rename(columns={"日期": "date", "市盈率1": "pe", "市净率": "pb",
                                "股息率1": "dy"})
        if "pb" not in df.columns:
            df["pb"] = np.nan
        if "dy" not in df.columns:
            df["dy"] = np.nan
        df = normalize_daily(df, "指数行情")[["date", "pe", "pb", "dy"]]
        if not df.empty:
            log(f"中证估值接口不可用，XLS 回退 {index_code} {len(df)} 期（仅展示，不写积累库）")
            return None
    except Exception:
        pass
    return None


# 国证体系指数（中证官网 indexCsiDsPe 对国证指数必 404）：官方估值走独立源分支
GUOZHENG_TX = {"980092"}


def get_index_valuation_guozheng(index_code: str):
    """国证体系指数官方估值日频增量（腾讯行情 qt.gtimg.cn，GBK）。
    980092 专用：腾讯该指数快照即官方口径估值（PE-TTM / PB，时间戳 YYYYMMDDHHMMSS），
    与 iFinD 全历史最新（2026-11-04 PE=12.9668/PB=1.401）互证。
    返回 DataFrame(date, pe, pb, dy=NaN) 仅最新 1 行；失败返回 None。
    说明：仅作每日增量接入积累库；已由 iFinD 播种的同一交易日行不会被覆盖
    （get_valuation 国证分支用 update_history(skip_existing_dates=True)）。
    """
    try:
        import requests as _rq
        hdrs = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            "Referer": "http://gu.qq.com/",
        }
        r = _rq.get("http://qt.gtimg.cn/q=sz980092", headers=hdrs, timeout=8)
        r.raise_for_status()
        r.encoding = "gbk"
        text = r.text
        body = text.split('="', 1)[1].rsplit('"', 1)[0]
        f = body.split("~")
        # 时间戳字段：14 位数字 YYYYMMDDHHMMSS（实测 sz980092 为 20261104161428）
        ts_idx = next((i for i, x in enumerate(f) if len(x) == 14 and x.isdigit()), None)
        if ts_idx is None:
            return None
        ts = f[ts_idx]
        d = pd.Timestamp(ts[:8], format="%Y%m%d")
        # PE-TTM 在时间戳后第 9 个字段、PB 在其后第 4 个（腾讯指数行情快照固定顺序，实测偏移）
        pe = pd.to_numeric(f[ts_idx + 9], errors="coerce") if ts_idx + 9 < len(f) else np.nan
        pb = pd.to_numeric(f[ts_idx + 13], errors="coerce") if ts_idx + 13 < len(f) else np.nan
        if np.isnan(pe) or pe <= 0 or np.isnan(pb) or pb <= 0:
            return None
        ratio = pe / pb
        if not (3 <= ratio <= 30):   # 现金流指数 PE/PB 比值通常在 5~20，异常即弃（防字段错位）
            return None
        df = pd.DataFrame({"date": [d], "pe": [float(pe)], "pb": [float(pb)], "dy": [np.nan]})
        log(f"腾讯官方指数估值增量 {index_code} {ts[:8]} PE={pe:.4f} PB={pb:.4f}")
        return df
    except Exception:
        return None


def get_index_dy_csindex(index_code: str):
    """
    中证官网 XLS 静态文件（近约20期）中的官方股息率（股息率1口径）。
    仅取股息率字段，不参与 PE 决策口径（避免与 peg 全历史冲突）。
    返回 DataFrame: date, dy（升序）；失败返回 None。
    """
    try:
        df = ak.stock_zh_index_value_csindex(symbol=CSINDEX_MAP.get(index_code, index_code))
        df = df.rename(columns={"日期": "date", "股息率1": "dy"})
        if "dy" not in df.columns or df["dy"].isna().all():
            return None
        # 手动归一化（该接口无 close 列，不走 normalize_daily）
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["dy"] = pd.to_numeric(df["dy"], errors="coerce")
        df = df.dropna(subset=["date", "dy"])
        df = df[["date", "dy"]].sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
        if not df.empty:
            log(f"官方股息率 XLS {index_code} {len(df)} 期（截至 {df['date'].iloc[-1].date()}，不写积累库PE）")
            return df
    except Exception:
        pass
    return None


def patch_dy(hist: pd.DataFrame, code: str, name: str, dy_df: "pd.DataFrame | None") -> pd.DataFrame:
    """
    把官方 XLS 股息率并入积累库 dy 字段。
    只填充已有日期行（date+index_code 命中），不插入新行、不覆盖 pe/pb——
    避免 XLS「市盈率1」口径与 peg 全历史冲突、避免新增 pe=NaN 行干扰决策。
    """
    if dy_df is None or dy_df.empty or hist is None or hist.empty:
        return hist
    c6 = str(code).zfill(6)
    patch = dy_df.copy()
    patch["index_code"] = c6
    patch["index_name"] = name
    patch["source"] = "csindex"
    for _, row in patch.iterrows():
        d = pd.to_datetime(row["date"])
        m = (hist["date"] == d) & (hist["index_code"] == c6)
        if m.any():
            hist.loc[m, "dy"] = row["dy"]
    hist = hist.sort_values("date").reset_index(drop=True)
    return hist


# ============================================================
# 3. 指标计算
# ============================================================
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """均线 / RSI / BOLL / MACD / 偏离度"""
    df = df.copy()
    for w in (5, 20, 60, 120):
        df[f"ma{w}"] = df["close"].rolling(w).mean()
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    # RSI(14) Wilder：前14期均值初始化，之后按 Wilder 递推。
    period = 14
    avg_gain = pd.Series(np.nan, index=df.index, dtype=float)
    avg_loss = pd.Series(np.nan, index=df.index, dtype=float)
    if len(df) > period:
        avg_gain.iloc[period] = gain.iloc[1:period+1].mean()
        avg_loss.iloc[period] = loss.iloc[1:period+1].mean()
        for i in range(period+1, len(df)):
            avg_gain.iloc[i] = (avg_gain.iloc[i-1]*(period-1)+gain.iloc[i])/period
            avg_loss.iloc[i] = (avg_loss.iloc[i-1]*(period-1)+loss.iloc[i])/period
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - 100/(1+rs)
    df.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100
    df.loc[(avg_loss == 0) & (avg_gain == 0), "rsi"] = 50
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    df["macd"] = 2 * (df["dif"] - df["dea"])
    mid = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    df["boll_mid"], df["boll_std"] = mid, std
    df["boll_up"] = mid + 2 * std
    df["boll_low"] = mid - 2 * std
    df["dev20"] = (df["close"] - mid) / mid * 100
    return df


def percentile_of(series: pd.Series, value: float) -> float:
    s = series.dropna()
    if len(s) == 0:
        return np.nan
    return float((s < value).sum() / len(s) * 100)


def build_pe_series(price_df, val_latest, acc_hist) -> "pd.Series | None":
    """
    构建日频 PE 序列（估算口径）：
      官方最新 PE × 指数点位/收盘价比例回推全历史（假设 EPS 近似恒定），
      官方积累库中的真实 PE 覆盖对应日期（近约20个交易日为官方值），
      末端强制对齐官方最新 PE。
    注：price_df 应传指数日线（真实点位，无复权问题）；仅指数日线缺失时
        才允许用 ETF 行情兜底（分位口径会略差）。
    返回 pd.Series(index=日期, name='pe') 升序；数据不足返回 None。
    """
    if price_df is None or price_df.empty or not val_latest or pd.isna(val_latest.get("pe")):
        return None
    pe0 = float(val_latest["pe"])
    p = price_df[["date", "close"]].dropna().sort_values("date")
    if len(p) < 10:
        return None
    # 锚点：估值日期（官方/积累库最近一期）对应的收盘价；缺失时用最新收盘价
    try:
        anchor = pd.to_datetime(val_latest.get("date") or p["date"].iloc[-1])
    except Exception:
        anchor = p["date"].iloc[-1]
    sub = p[p["date"] <= anchor]
    anchor_close = float(sub["close"].iloc[-1]) if not sub.empty else float(p["close"].iloc[-1])
    if not anchor_close or anchor_close <= 0:
        return None
    pe_s = pd.Series((pe0 * p["close"] / anchor_close).values,
                     index=pd.DatetimeIndex(p["date"]), name="pe")
    # 官方真实 PE 覆盖（积累库含中证官网近20期）
    if acc_hist is not None and not acc_hist.empty:
        acc = acc_hist.dropna(subset=["pe"])
        for _, row in acc.iterrows():
            d = pd.to_datetime(row["date"])
            if d in pe_s.index:
                pe_s.loc[d] = float(row["pe"])
    if pd.to_datetime(anchor).date() == pd.to_datetime(pe_s.index[-1]).date():
        pe_s.iloc[-1] = pe0
    return pe_s


def calc_pe_windows(pe_s: "pd.Series | None"):
    """
    三窗口（近1年/近3年/近5年）PE 分位（展示口径）。
    分位计算前先清洗（clean_pe_series：剔除 PE<=0、1%/99% winsorize），
    避免 980092 负值段（2019-04~2022）与公告日极端值使分位失真。
    返回 {窗口名: {pct, n, start, end}}；窗口样本不足返回 None；无数据返回 None。
    """
    if pe_s is None or len(pe_s) == 0:
        return None
    pe_s = decision_engine.clean_pe_series(pe_s)
    if pe_s is None or len(pe_s) == 0:
        return None
    out = {}
    for label, n in PE_WINDOWS.items():
        w = pe_s.tail(n)
        if len(w) < int(n * 0.80):
            out[label] = None
            continue
        reference = w.iloc[:-1] if len(w) > 1 else w
        out[label] = {
            "pct": round(percentile_of(reference, float(w.iloc[-1])), 1),
            "n": int(len(w)),
            "start": str(w.index[0].date()),
            "end": str(w.index[-1].date()),
        }
    return out


# ============================================================
# 4. 估值历史积累库
# ============================================================
def load_history() -> pd.DataFrame:
    if HISTORY_PATH.exists():
        try:
            df = pd.read_csv(HISTORY_PATH, parse_dates=["date"])
            if "index_code" in df.columns:
                # 统一为6位字符串代码，避免 "922" 与 "000922" 并存导致去重失效
                df["index_code"] = df["index_code"].astype(str).str.zfill(6)
            return df
        except Exception as e:
            log(f"读取估值历史失败: {e}")
    return pd.DataFrame(columns=["date", "index_code", "index_name", "pe", "pb", "dy", "source"])


def update_history(hist: pd.DataFrame, code: str, name: str, val_df: pd.DataFrame,
                   skip_existing_dates: bool = False, source_tag: str = "csindex") -> pd.DataFrame:
    """把本次拉到的估值并入历史库（按 date+index_code 去重）。
    skip_existing_dates=True：跳过该代码已存在的日期（国证腾讯增量不覆盖 iFinD 播种口径）；
    source_tag：写入 source 字段的标签。"""
    if val_df is None or val_df.empty:
        return hist
    new = val_df.copy()
    new["index_code"] = str(code).zfill(6)
    new["index_name"] = name
    new["source"] = source_tag
    if skip_existing_dates and hist is not None and not hist.empty:
        c6 = str(code).zfill(6)
        existing = set(hist.loc[hist["index_code"] == c6, "date"])
        new = new[~new["date"].isin(existing)]
        if new.empty:
            return hist
    merged = pd.concat([hist, new], ignore_index=True)
    merged["index_code"] = merged["index_code"].astype(str).str.zfill(6)
    merged = merged.drop_duplicates(subset=["date", "index_code"], keep="last")
    merged = merged.sort_values("date").reset_index(drop=True)
    try:
        merged.to_csv(HISTORY_PATH, index=False, encoding="utf-8-sig")
    except Exception as e:
        log(f"估值历史写盘失败: {e}")
    return merged


def get_valuation(code: str, name: str, hist: pd.DataFrame):
    """
    返回 (latest_dict, history_df, source_desc)
    latest_dict: {date, pe, pb, dy}
    history_df: 该指数 PE 历史(升序)
    国证体系（980092）走独立官方源分支：iFinD 全历史已播种入积累库
    （seed_valuation_980092.py，2016-01 起），每日以腾讯官方指数行情
    （PE-TTM/PB）增量接入；腾讯行不覆盖已播种日期。
    """
    c6 = str(code).zfill(6)
    if c6 in GUOZHENG_TX:
        val = safe_call("腾讯官方指数估值增量", get_index_valuation_guozheng, code)
        if val is not None and not val.empty:
            hist = update_history(hist, code, name, val, skip_existing_dates=True, source_tag="tx")
            src = "腾讯官方指数行情(官方增量)"
        elif hist is not None and not hist[hist["index_code"] == c6].empty:
            src = "本地积累库(官方PE全历史)"
        else:
            return None, hist, "暂无估值数据"
        sub = hist[hist["index_code"] == c6].sort_values("date")
        if sub.empty:
            return None, hist, src
        latest = sub.iloc[-1].to_dict()
        latest["date"] = str(latest["date"])[:10]
        return latest, hist, src
    val = safe_call("中证指数官网估值", get_index_valuation_csindex, code)
    if val is not None and not val.empty:
        hist = update_history(hist, code, name, val)
        src = "中证指数官网(官方)"
    elif hist is not None and not hist[hist["index_code"] == str(code).zfill(6)].empty:
        src = "本地积累库(上次数据)"
    else:
        return None, hist, "暂无估值数据"
    sub = hist[hist["index_code"] == str(code).zfill(6)].sort_values("date")
    if sub.empty:
        return None, hist, src
    latest = sub.iloc[-1].to_dict()
    latest["date"] = str(latest["date"])[:10]
    return latest, hist, src


# ============================================================
# 5. 信号引擎
# ============================================================
def build_signal(name: str, cfg: dict, price: pd.DataFrame, val_latest: dict,
                 pe_series: "pd.Series | None", pe_hist_official: "pd.DataFrame | None" = None,
                 run_date=None, src_flags: dict | None = None):
    """
    返回该 ETF 的全部指标、信号、建议。
    pe_series: 日频 PE 序列（估算口径，仅展示）
    pe_hist_official: 该指数积累库官方 PE 历史（决策引擎估值维度使用，≥200 条才参与）
    run_date/src_flags: V4 数据质量闸门所需的运行日与本轮数据源状态
    """
    last = price.iloc[-1]
    pe = val_latest.get("pe") if val_latest else np.nan
    pb = val_latest.get("pb") if val_latest else np.nan
    dy = val_latest.get("dy") if val_latest else np.nan
    pe_windows = calc_pe_windows(pe_series)
    w_main = pe_windows.get(PE_MAIN_WINDOW) if pe_windows else None
    pe_pct = w_main["pct"] if w_main else np.nan  # 展示用主分位（近3年，估算口径）
    pe_pcts, pe_dates = [], []
    if pe_series is not None and len(pe_series) > 1:
        # 近3年滚动窗口分位走势（先清洗负值/极端值，避免 980092 2022 负值段污染分位图）
        v = decision_engine.clean_pe_series(pe_series.tail(1250))
        if v is not None and len(v) > 1:
            idx = v.index
            for i in range(len(v)):
                w = v.iloc[max(0, i - (PE_WINDOWS[PE_MAIN_WINDOW] - 1)): i + 1]
                pe_pcts.append(round(percentile_of(w, float(v.iloc[i])), 1))
                pe_dates.append(str(idx[i].date()))

    # ---- V4 决策引擎：数据质量闸门 → 趋势 × 估值 × 风险 → 六种操作状态 ----
    close = float(last["close"])
    slope_ma20 = None
    if not np.isnan(last.get("ma20")) and len(price) >= 6 and not np.isnan(price["ma20"].iloc[-6]):
        slope_ma20 = float(last["ma20"] - price["ma20"].iloc[-6])
    trend = decision_engine.trend_state(
        None if np.isnan(last.get("ma20")) else float(last["ma20"]),
        None if np.isnan(last.get("ma60")) else float(last["ma60"]),
        None if np.isnan(last.get("ma120")) else float(last["ma120"]),
        slope_ma20)
    # 官方 PE 积累数与分位（决策用）
    official_count = 0
    if pe_hist_official is not None and not pe_hist_official.empty:
        official_count = int(pe_hist_official["pe"].dropna().shape[0])
    official_windows = decision_engine.official_pe_windows(pe_hist_official)
    ow_main = official_windows.get(PE_MAIN_WINDOW) if official_windows else None
    val = decision_engine.valuation_state(ow_main["pct"] if ow_main else None, official_count)
    risk_raw = decision_engine.risk_metrics(price["close"], price["high"], price["low"])
    risk = decision_engine.risk_state(risk_raw, trend)
    # V4 数据质量闸门（评分之前，前序闸门对最终建议有否决权）
    price_last_date = str(pd.to_datetime(price["date"].iloc[-1]).date())
    pe_last_date = None
    if pe_hist_official is not None and not pe_hist_official.empty:
        try:
            pe_last_date = str(pd.to_datetime(pe_hist_official["date"]).max().date())
        except Exception:
            pe_last_date = None
    gate = decision_engine.data_gate(
        price_last_date=price_last_date,
        run_date=(run_date or dt.date.today()),
        price_rows=len(price),
        pe_last_date=pe_last_date,
        official_count=official_count,
        src_flags=src_flags or {},
        trend=trend, risk=risk, val=val,
        price_stale_days=GATE_PRICE_STALE,
        price_stale_hard_days=GATE_PRICE_HARD,
        pe_stale_days=GATE_PE_STALE,
        min_price_rows=GATE_MIN_ROWS)
    decision = decision_engine.decide(trend, val, risk, official_count, gate=gate)
    batch = decision_engine.batch_plan(trend, risk, val, gate=gate)
    atr = decision_engine.atr_ref(close, risk_raw.get("atr"))

    score = 0.0
    signals = []

    # ---- 估值（展示）----
    if not np.isnan(pe_pct):
        if pe_pct < TH["pe_low"]:
            score += 2; signals.append(f"估值低估·3年分位{pe_pct:.0f}%")
        elif pe_pct > TH["pe_high"]:
            score -= 2; signals.append(f"估值高估·3年分位{pe_pct:.0f}%")
        else:
            signals.append(f"估值中性·3年分位{pe_pct:.0f}%")
    if official_count < decision_engine.PE_MIN_OFFICIAL:
        signals.append(f"官方PE积累中({official_count}条)")

    # ---- 技术面 ----
    rsi = float(last.get("rsi")) if not np.isnan(last.get("rsi")) else np.nan
    if not np.isnan(rsi):
        if rsi < TH["rsi_low"]:
            score += 1; signals.append(f"RSI超卖({rsi:.0f})")
        elif rsi > TH["rsi_high"]:
            score -= 1; signals.append(f"RSI超买({rsi:.0f})")
        else:
            signals.append(f"RSI中性({rsi:.0f})")

    if not np.isnan(last.get("boll_low")) and close < last["boll_low"]:
        score += 1; signals.append("跌破布林下轨")
    elif not np.isnan(last.get("boll_up")) and close > last["boll_up"]:
        score -= 1; signals.append("突破布林上轨")

    ma_bull = False
    if not np.isnan(last.get("ma20")) and not np.isnan(last.get("ma60")):
        if last["ma20"] > last["ma60"]:
            score += 0.5; signals.append("中期均线多头"); ma_bull = True
        else:
            score -= 0.5; signals.append("中期均线空头")

    dev20 = float(last.get("dev20")) if not np.isnan(last.get("dev20")) else 0.0
    if dev20 < -TH["dev"]:
        score += 0.5; signals.append(f"偏离20日线{dev20:.1f}%(超跌)")
    elif dev20 > TH["dev"]:
        score -= 0.5; signals.append(f"偏离20日线{dev20:.1f}%(超涨)")

    # ---- 长期趋势过滤（V2 保留）----
    ma60, ma120 = last.get("ma60", np.nan), last.get("ma120", np.nan)
    downtrend = pd.notna(ma60) and pd.notna(ma120) and ma60 < ma120
    if downtrend and score >= 0.5:
        signals.append("长期趋势过滤：MA60低于MA120，限制买入信号")

    # ---- 结论（V2 保留作为信号标签；V3 主建议见 decision.suggestion）----
    # 趋势差异化：均线多头时减仓类结论改为"分批止盈"（趋势尚在），空头时强化"逢高减仓"
    if score >= 2.5 and not downtrend:
        conclusion, concl_color, concl_txt = "强烈分批买入", "#ef4444", "估值与技术双低区域，分批建仓/加仓，严控单笔仓位"
    elif score >= 0.5:
        conclusion, concl_color, concl_txt = "逢低分批买入", "#f97316", "偏低估区域，可分批买入或维持定投"
    elif score > -1.0:
        if ma_bull:
            conclusion, concl_color, concl_txt = "持有观察", "#94a3b8", "趋势向上但估值不低：持有为主，回踩不破位可低吸，不追高"
        else:
            conclusion, concl_color, concl_txt = "谨慎观望", "#94a3b8", "趋势偏弱：观望或轻仓试探，等待企稳信号再介入"
    else:
        if ma_bull:
            conclusion, concl_color, concl_txt = "分批止盈", "#3b82f6", "估值偏高但趋势尚在：分批止盈/减仓，保留底仓跟踪，不一次清仓"
        else:
            conclusion, concl_color, concl_txt = "逢高减仓", "#3b82f6", "估值高位+趋势走弱：反弹分批减仓，控制仓位等待估值修复"

    # 周期股提示（煤炭等强周期）：PE 高分位≠高估，避免单看分位误判
    if cfg.get("cyclic") and not np.isnan(pe_pct) and pe_pct > 70:
        signals.append("周期股高位·PE分位参考盈利位置")

    # ---- 定投档位（V3：优先官方PE分位；官方积累不足时按正常定投）----
    ow_pct = ow_main["pct"] if ow_main else None
    dca_ref = ow_pct if ow_pct is not None else (pe_pct if not np.isnan(pe_pct) else None)
    if official_count < decision_engine.PE_MIN_OFFICIAL:
        dca = "数据积累中，先按正常定投"
        dca_color = "#94a3b8"
    elif dca_ref is None:
        dca = "数据积累中，先按正常定投"
        dca_color = "#94a3b8"
    elif dca_ref < 20:
        dca, dca_color = "双倍定投(2×)", "#ef4444"
    elif dca_ref < 40:
        dca, dca_color = "正常定投(1×)", "#f97316"
    elif dca_ref < 70:
        dca, dca_color = "减半定投(0.5×)", "#eab308"
    else:
        dca, dca_color = "暂停定投·仅持有", "#3b82f6"

    # ---- 做T参考（短周期支撑/压力，带来源标签；近→远排序）----
    # MA20 归属互斥：价格在 MA20 上方→MA20 仅作支撑；下方→仅作压力（避免同价位双标歧义）
    support = []
    pressure = []
    if not np.isnan(last.get("ma20")) and close >= last["ma20"]:
        support.append({"v": round(float(last["ma20"]), 4), "tag": "MA20"})
    if not np.isnan(last.get("boll_low")):
        support.append({"v": round(float(last["boll_low"]), 4), "tag": "布林下轨"})
    if not np.isnan(last.get("boll_up")):
        pressure.append({"v": round(float(last["boll_up"]), 4), "tag": "布林上轨"})
    recent_high = float(price["high"].tail(5).max())
    pressure.append({"v": round(recent_high, 4), "tag": "5日高"})
    if not np.isnan(last.get("ma20")) and close < last["ma20"]:
        pressure.append({"v": round(float(last["ma20"]), 4), "tag": "MA20压力"})
    support.sort(key=lambda x: x["v"], reverse=True)
    pressure.sort(key=lambda x: x["v"])
    t_text = ""
    if not np.isnan(rsi):
        # 阈值与信号段统一（TH["rsi_low"]/TH["rsi_high"]），避免两处口径不一致
        if rsi > TH["rsi_high"]:
            t_text = "高位慎追，做T以减仓为主"
        elif rsi < TH["rsi_low"]:
            t_text = "超跌区，做T可留意低吸反抽"
        else:
            t_text = "中性区，箱体内高抛低吸"

    # ---- 长期提示 ----
    long_txt = (f"{cfg['role']}。基准仓位{cfg['base_w']}%，"
                f"估值低估可加至{cfg['max_w']}%，高估减至{cfg['min_w']}%。"
                f"当前股息率 {dy:.2f}%" if not np.isnan(dy) else
                f"{cfg['role']}。基准仓位{cfg['base_w']}%，低估加至{cfg['max_w']}%，高估减至{cfg['min_w']}%。")

    return {
        "name": name, "code": cfg["code"], "color": cfg["color"],
        "close": close,
        "pct_chg": float(last.get("pct_chg", 0.0)) if not np.isnan(last.get("pct_chg", 0.0)) else 0.0,
        "ma20": float(last["ma20"]) if not np.isnan(last.get("ma20")) else None,
        "ma60": float(last["ma60"]) if not np.isnan(last.get("ma60")) else None,
        "ma120": float(last["ma120"]) if not np.isnan(last.get("ma120")) else None,
        "rsi": None if np.isnan(rsi) else round(rsi, 1),
        "dev20": round(dev20, 2),
        "pe": None if np.isnan(pe) else round(float(pe), 2),
        "pb": None if np.isnan(pb) else round(float(pb), 2),
        "dy": None if np.isnan(dy) else round(float(dy), 2),
        "pe_pct": None if np.isnan(pe_pct) else round(pe_pct, 1),
        "pe_windows": pe_windows,   # {近1年/近3年/近5年: {pct,n,start,end}}（估算口径，展示）
        "pe_pcts": pe_pcts, "pe_dates": pe_dates,
        "signals": signals,
        "score": round(score, 1),
        "note": cfg.get("note"),   # 周期股等特殊提示（原样展示在卡片）
        "conclusion": conclusion, "concl_color": concl_color, "concl_txt": concl_txt,
        "dca": dca, "dca_color": dca_color,
        "support": support, "pressure": pressure, "t_text": t_text,
        "long_txt": long_txt,
        "latest_date": str(price["date"].iloc[-1].date()),
        "price_rows": int(len(price)),
        "price_start": str(price["date"].iloc[0].date()),
        # ---- V4 决策简报：三维评分 + 数据质量闸门 + 六状态 ----
        "official_pe_count": official_count,
        "official_pe_windows": official_windows,   # 官方积累 PE 分位（决策用）
        "decision": decision,   # {score(0-100), state(六状态), quality, reasons, next_conditions, gate...}
        "gate": gate,           # {passed, block_add, fatal, level, flags, summary}
        "pos": {"base_w": cfg.get("base_w"), "max_w": cfg.get("max_w"), "min_w": cfg.get("min_w")},
        "asset_class": cfg.get("asset_class"),
        "research_cap_w": cfg.get("research_cap_w"),
        "pe_last_date": pe_last_date,
        "strategy_version": STRATEGY_VERSION,
        "trend": trend,         # {label, score, desc}
        "risk": risk,           # {label, score, desc, available}
        "risk_raw": {"atr": risk_raw.get("atr"), "atr_pct": risk_raw.get("atr_pct"),
                     "adx": risk_raw.get("adx"), "vol20_ann": risk_raw.get("vol20_ann"),
                     "dd60": risk_raw.get("dd60"), "dd120": risk_raw.get("dd120")},
        "batch": batch,         # {status, color, text, steps?}
        "atr_ref": atr,         # {atr, ref_low, ref_high, watch_low, watch_high, text}
    }


# ============================================================
# 4b. 每标的针对性监测指标（专属维度，随标的角色差异化）
# ============================================================
def get_coal_futures():
    """焦煤主力连续(JM0)日线（新浪期货）——煤炭行业景气 proxy。
    焦煤/焦炭价格是煤炭板块盈利与估值的核心驱动，用于强周期标的的专属观察。"""
    df = safe_call("焦煤主力行情", ak.futures_main_sina, "JM0", attempts=1)
    if df is None or df.empty:
        return None
    df = df.rename(columns={"日期": "date", "收盘价": "close"})
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    return df[["date", "close"]]


def build_special(name: str, cfg: dict, price: pd.DataFrame, val_latest: dict,
                  macro: dict, price_rets: dict, official_ctx: dict = None):
    """按标的角色构建针对性监测指标（区别于通用 M1-M5 的专属维度）。
    返回 {"rows": [{"k": ..., "v": ..., "note": ...}]} 或 None。
    数据源均已实测：中证官网股息率XLS / 宏观10Y国债 / 新浪焦煤期货 / 本地价格序列。
    official_ctx: 决策侧官方估值上下文 {"ow_main": 官方主窗口分位, "official_count": 官方PE积累条数}，
    仅用于 159201 专属行的官方 PE 展示。"""
    rows = []
    dy = (val_latest or {}).get("dy")
    y10 = (macro or {}).get("y10")

    # 通用维度1：股息率-10Y国债利差（红利/银行/煤炭/红利低波 均适用，衡量股息相对无风险利率吸引力）
    if dy is not None and not np.isnan(dy) and y10 is not None:
        spread = float(dy) - float(y10)
        if spread > 2.0:
            note = ">2pp，股息吸引力强"
        elif spread > 0:
            note = "股息仍高于无风险利率"
        else:
            note = "股息吸引力弱于无风险利率"
        rows.append({"k": "股息率-10Y利差", "v": f"{dy:.2f}% - {y10:.2f}% = {spread:+.2f}pp", "note": note})

    if name == "银行ETF":
        # 银行估值惯例看 PB，但中证官网仅提供 PE 口径（indexCsiDsPe 无 PB 字段），如实标注
        rows.append({"k": "PB 估值分位", "v": "数据源受限",
                     "note": "中证官网无PB历史接口，银行估值暂以PE分位+股息率替代，勿混用口径"})

    elif name == "煤炭ETF":
        # 焦煤主力连续：煤价位置是周期股估值的锚（周期行业盈利/PE 分位随煤价摆动）
        jm = get_coal_futures()
        if jm is not None and len(jm) >= 21:
            last_jm = float(jm["close"].iloc[-1])
            jm_d = jm.set_index("date")["close"]
            chg_m = (last_jm / float(jm_d.iloc[-21]) - 1) * 100      # 近21交易日(约1月)
            chg_3m = (last_jm / float(jm_d.iloc[-63]) - 1) * 100 if len(jm) >= 63 else None
            hi60 = float(jm_d.tail(60).max()); lo60 = float(jm_d.tail(60).min())
            pos60 = (last_jm - lo60) / (hi60 - lo60) * 100 if hi60 > lo60 else None
            v = f"主力{last_jm:.0f}（月{chg_m:+.1f}%）"
            if chg_3m is not None:
                v += f" · 3月{chg_3m:+.1f}%"
            note = f"景气proxy：煤价月涨跌{chg_m:+.1f}%"
            if pos60 is not None:
                note += f"，60日区间位置{pos60:.0f}%"
            note += "；煤价下行期PE分位参考意义下降"
            rows.append({"k": "焦煤主力(JM0)", "v": v, "note": note})

    elif name == "自由现金流ETF":
        # 官方估值已接入（iFinD 全历史播种 + 腾讯日频增量，国证980092指数口径，参与决策评分）：
        # ① 官方 PE（指数口径）展示（含主窗口分位）
        # ② 十大权重加权参考估值（腾讯个股 PE/PB）——保留但明确标注非官方、仅参考
        # ③ IOPV 溢价率（场内现价 vs 参考净值）
        # ④ 价格结构统计（原逻辑保留）
        octx = official_ctx or {}
        if val_latest and not pd.isna(val_latest.get("pe")):
            ov = f"PE-TTM {float(val_latest['pe']):.2f}"
            if not pd.isna(val_latest.get("pb")):
                ov += f" · PB {float(val_latest['pb']):.2f}"
            ow_main = octx.get("ow_main")
            if ow_main and ow_main.get("pct") is not None:
                ov += f" · {PE_MAIN_WINDOW}分位{ow_main['pct']:.0f}%"
            ocnt = int(octx.get("official_count") or 0)
            ov_note = ("国证980092指数官方口径 PE-TTM（iFinD 全历史播种2016-01起 + 腾讯日频增量），"
                       f"官方PE积累{ocnt}条≥200，估值维度已参与决策评分")
            rows.append({"k": "官方PE(指数口径)", "v": ov, "note": ov_note})
        vref = safe_call(f"{name}加权估值", get_weighted_val_159201)
        if vref is not None:
            SRC_STATS["ok_tx"] += 1
            rows.append({"k": "估值参考(十大权重加权)", "v": f"PE≈{vref['pe']:.1f} · PB≈{vref['pb']:.1f}",
                         "note": f"前十大权重{vref['n']}只（占净值{vref['total_w']:.1f}%，基金季报口径）加权估算，非官方指数估值、仅参考"})
            if vref.get("premium") is not None:
                p = vref["premium"]
                if p > 0.5:
                    pnote = "现价相对IOPV溢价，场内买入成本偏高"
                elif p < -0.2:
                    pnote = "轻微折价，利于场内买入"
                else:
                    pnote = "贴近净值，折溢平稳"
                rows.append({"k": "IOPV溢价率", "v": f"{p:+.2f}%", "note": pnote})
        try:
            c = price["close"].dropna()
            dd = float((c / c.cummax() - 1).min()) * 100                     # 最大回撤
            dist_high = (float(c.iloc[-1]) / float(c.max()) - 1) * 100       # 距上市高点
            tail20 = c.tail(20)
            n_high20 = int((tail20 >= tail20.cummax()).sum())                 # 20日内触及新高的天数
            rows.append({"k": "价格结构", "v": f"最大回撤{dd:.1f}% · 距上市高点{dist_high:+.1f}% · 近20日新高{n_high20}天",
                         "note": "上市时间短（2025-02），以价格趋势结构补充追踪"})
        except Exception as e:
            log(f"{name} 价格结构统计失败: {e}")

    elif name == "红利低波ETF":
        # 低波红利：验证"低波动"特征（年化波动率 vs 中证红利）+ 相对红利超额
        try:
            vol_low = float(price["close"].pct_change().tail(60).std() * np.sqrt(252) * 100)
            red_ret = price_rets.get("红利ETF")
            if red_ret is not None and len(red_ret) >= 60:
                vol_red = float(red_ret.tail(60).std() * np.sqrt(252) * 100)
                v = f"年化波动率{vol_low:.1f}%（中证红利{vol_red:.1f}%）"
                note = "低波红利核心特征：波动率低于中证红利即具备低波属性"
                if vol_low < vol_red:
                    note += "，当前符合"
                else:
                    note += "，当前波动偏高，低波属性阶段性失效"
            else:
                v = f"年化波动率{vol_low:.1f}%（中证红利--）"
                note = "红利ETF 行情缺失，暂无法对比低波属性"
            rows.append({"k": "低波属性", "v": v, "note": note})
            # 相对红利超额：近250交易日累计收益差（vs 红利ETF 515180）
            ret_low = float(price["close"].pct_change().tail(250).add(1).prod() - 1) * 100
            if red_ret is not None and len(red_ret) >= 250:
                ret_red = float(red_ret.tail(250).add(1).prod() - 1) * 100
                excess = ret_low - ret_red
                rows.append({"k": "相对红利超额(250日)", "v": f"{excess:+.1f}pp（{ret_low:+.2f}% vs {ret_red:+.2f}%）",
                             "note": ">0 跑赢中证红利ETF，低波+高股息在震荡市占优" if excess > 0 else "<0 跑输中证红利ETF，注意风格差异"})
        except Exception as e:
            log(f"{name} 低波/超额统计失败: {e}")

    elif name == "红利ETF":
        # 高股息压舱石：利差已覆盖；补一句股息覆盖口径说明即可
        rows.append({"k": "股息覆盖", "v": "官网XLS近20期口径",
                     "note": "股息率为官方近20期快照，非年化预测值；利差随10Y国债每日变动"})

    return {"rows": rows} if rows else None


# ============================================================
# 5. 宏观环境（利率 / 政策 / 情绪）
# ============================================================
def get_mkt_amount():
    """两市成交额序列（上证000001 + 深证成指399001，腾讯指数日线 amount，单位：千元）。
    海外环境（GitHub Actions）腾讯通道可用；失败返回 None 不阻塞主流程。"""
    try:
        a = ak.stock_zh_index_daily_tx(symbol="sh000001")
        b = ak.stock_zh_index_daily_tx(symbol="sz399001")
        da, db = a.sort_values("date"), b.sort_values("date")
        m = pd.merge(da[["date", "amount"]], db[["date", "amount"]],
                     on="date", suffixes=("_a", "_b"))
        m["total"] = m["amount_a"] + m["amount_b"]
        return m[["date", "total"]]
    except Exception as e:
        log(f"[数据源] 两市成交额获取失败: {type(e).__name__}: {str(e)[:120]}")
        return None


def _macro_status(last_date, run_date, hard_days):
    """宏观口径新鲜度：返回 (status, days)。status ∈ ok / stale / fail。"""
    if not last_date:
        return "fail", None
    try:
        days = (pd.to_datetime(run_date).date() - pd.to_datetime(last_date).date()).days
    except Exception:
        return "fail", None
    if days > hard_days:
        return "stale", days
    return "ok", days


def get_macro() -> dict:
    """宏观环境采集：
    利率 = 中国10Y国债收益率（英为财情）+ 较约20日前变化(bp)
    政策 = LPR(1Y/5Y，央行) + 手动维护条目 MACRO_POLICY
    情绪 = 两市成交额（上证+深证，腾讯）及其较20日均量比

    V4 P0-5：每项都带「发布日期 + 更新状态」（ok / stale / fail）。
    采集失败标 fail（页面显示「获取失败」而非裸 '--'）；超过新鲜度阈值标 stale，
    并明确「过期数据不参与判断」，不再把它当作可用环境信号。
    """
    run_date = dt.date.today()
    m = {"y10": None, "y10_chg": None, "y10_label": None, "y10_date": None,
         "lpr1": None, "lpr5": None, "lpr_date": None,
         "mkt": None, "mkt_ratio": None, "mkt_date": None,
         "policy_date": MACRO_POLICY.get("date"),
         "status": {"y10": "fail", "lpr": "fail", "mkt": "fail"},
         "stale_days": {"y10": None, "lpr": None, "mkt": None},
         "thresholds": {"y10": 10, "lpr": 45, "mkt": 5},
         "ok": False}

    # ---- 利率环境：10Y 国债收益率（阈值 10 个自然日）----
    try:
        d = ak.bond_zh_us_rate(start_date=(dt.date.today() - dt.timedelta(days=120)).strftime("%Y%m%d"))
        d = d.dropna(subset=["中国国债收益率10年"]).sort_values("日期").reset_index(drop=True)
        if len(d) >= 2:
            y = float(d["中国国债收益率10年"].iloc[-1])
            base = float(d["中国国债收益率10年"].iloc[-21]) if len(d) > 21 else float(d["中国国债收益率10年"].iloc[0])
            m["y10"], m["y10_chg"] = y, (y - base) * 100  # bp
            m["y10_date"] = str(d["日期"].iloc[-1])[:10]
            if y < MACRO_TH["y10_low"]:
                m["y10_label"] = "低利率·宽松（利好红利/高股息）"
            elif y > MACRO_TH["y10_high"]:
                m["y10_label"] = "高利率·偏紧（压制估值）"
            else:
                m["y10_label"] = "利率中性"
            m["status"]["y10"], m["stale_days"]["y10"] = _macro_status(
                m["y10_date"], run_date, m["thresholds"]["y10"])
        else:
            m["status"]["y10"] = "fail"
    except Exception as e:
        m["status"]["y10"] = "fail"
        log(f"[数据源] 10Y国债收益率获取失败: {type(e).__name__}: {str(e)[:120]}")

    # ---- 政策风向：LPR（自动硬指标，月度发布，阈值 45 天）+ 手动条目 ----
    try:
        d = ak.macro_china_lpr()
        d = d.sort_values("TRADE_DATE")
        m["lpr1"] = float(d["LPR1Y"].iloc[-1])
        m["lpr5"] = float(d["LPR5Y"].iloc[-1])
        m["lpr_date"] = str(d["TRADE_DATE"].iloc[-1])[:10]
        m["status"]["lpr"], m["stale_days"]["lpr"] = _macro_status(
            m["lpr_date"], run_date, m["thresholds"]["lpr"])
    except Exception as e:
        m["status"]["lpr"] = "fail"
        log(f"[数据源] LPR获取失败: {type(e).__name__}: {str(e)[:120]}")

    # ---- 市场情绪：两市成交额（阈值 5 个自然日）----
    try:
        mm = get_mkt_amount()
        if mm is not None and len(mm) >= 21:
            last = float(mm["total"].iloc[-1])
            avg20 = float(mm["total"].iloc[-21:-1].mean())
            m["mkt"] = last
            m["mkt_ratio"] = last / avg20 if avg20 > 0 else None
            m["mkt_date"] = str(mm["date"].iloc[-1])[:10]
            m["status"]["mkt"], m["stale_days"]["mkt"] = _macro_status(
                m["mkt_date"], run_date, m["thresholds"]["mkt"])
        else:
            m["status"]["mkt"] = "fail"
    except Exception as e:
        m["status"]["mkt"] = "fail"
        log(f"[数据源] 市场情绪采集失败: {type(e).__name__}: {str(e)[:120]}")

    m["ok"] = all(v == "ok" for v in m["status"].values())
    return m



def build_ratio():
    """创业板红利比 = 归一化相对强弱（创业板指/中证红利 各自相对基期涨幅之比），
    返回最新值+分位+建议+3年序列。
    口径说明：直接收盘比值因两指数基日/基点不同（399006基日2010-05-31、000922基日2004-12-31）
    无量纲意义，故取 各自相对合并序列首日(基期) 的涨幅之比；该比值仅反映相对走势强弱，
    绝对值随基期变动，分位不受基期影响（已实测：不同数据源基期下分位一致）。
    分位为近3年历史百分位（tail 750），与看板"近3年分位"口径一致。"""
    gem = safe_call("创业板指行情", get_index_daily, GEM_INDEX)
    div = safe_call("中证红利行情", get_index_daily, DIV_INDEX)
    if gem is None or div is None:
        return None
    m = pd.merge(gem[["date", "close"]], div[["date", "close"]],
                 on="date", suffixes=("_gem", "_div"))
    m = m.dropna()
    m = m[(m["close_gem"] > 0) & (m["close_div"] > 0)].copy()
    if m.empty:
        return None
    # 基期 = 两指数合并序列的首个完整交易日（随数据源起点变化，分位不受影响）
    m["ratio"] = (m["close_gem"] / m["close_gem"].iloc[0]) / (m["close_div"] / m["close_div"].iloc[0])
    # 仅取近3年样本（与看板“近3年分位”口径一致；腾讯/新浪源返回全量历史）
    m = m.tail(int(HISTORY_YEARS * 250)).reset_index(drop=True)
    if len(m) < 20:
        return None
    latest = float(m["ratio"].iloc[-1])
    pct = percentile_of(m["ratio"].iloc[:-1], latest)
    if pct < 30:
        sug, sug_color = "成长相对表现处于历史低位：观察相对表现，不据此单独交易", "#f97316"
    elif pct > 70:
        sug, sug_color = "成长相对表现处于历史高位：观察红利相对表现，不据此单独交易", "#ef4444"
    else:
        sug, sug_color = "相对表现处于历史中段：维持配置纪律", "#94a3b8"
    return {
        "latest": latest, "pct": round(pct, 1), "sug": sug, "sug_color": sug_color,
        "dates": [str(d.date()) for d in m["date"]],
        "ratios": [round(x, 4) for x in m["ratio"]],
        "gem_close": [round(x, 2) for x in m["close_gem"]],
        "div_close": [round(x, 2) for x in m["close_div"]],
        "base_date": str(m["date"].iloc[0].date()),
        "base_gem": round(float(m["close_gem"].iloc[0]), 2),
        "base_div": round(float(m["close_div"].iloc[0]), 2),
        "n": len(m),
    }


# ============================================================
# 6. 看板生成（单窗口 HTML）
# ============================================================
def render_dashboard(meta: dict, ratio: dict, etfs: list, macro: dict = None, corr: dict = None):
    # ECharts 内联（自包含，任何入口均可渲染）：优先读取本地 libs/echarts.min.js，
    # 缺失时回退 jsDelivr CDN（不再依赖仓库内相对路径，避免图表 404）
    _here = os.path.dirname(os.path.abspath(__file__))
    _echarts_path = os.path.join(_here, "libs", "echarts.min.js")
    if os.path.exists(_echarts_path):
        with open(_echarts_path, "r", encoding="utf-8") as _f:
            echarts_tag = "<script>\n" + _f.read() + "\n</script>"
    else:
        echarts_tag = '<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>'
    payload = {
        "meta": meta,
        "ratio": ratio,
        "etfs": etfs,
        "macro": macro if macro else {},
        "corr": corr if corr else {},
        "th": TH,
    }
    payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    # 实际生效行情源摘要（千问评审二轮D-11）：东方财富/腾讯/新浪/中证官网各自成功次数 + 东财失败降级次数
    _ss = meta.get("src_stats") or {}
    _parts = []
    for _lab, _key in (("东方财富", "ok_em"), ("腾讯", "ok_tx"), ("新浪", "ok_sina"), ("中证官网", "ok_cs")):
        if _ss.get(_key):
            _parts.append(f"{_lab}×{_ss[_key]}")
    _src_txt = " · ".join(_parts) if _parts else "未采集"
    _fail_n = _ss.get("fail_em", 0)
    src_line = (f'<div class="sub" style="font-size:11px;color:#7c8aa8;margin-top:2px">'
                f'行情来源(实际生效)：{_src_txt} ｜ 东财本轮失败 {_fail_n} 次已自动降级备用源</div>') if _ss else ""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ETF 智能监测看板</title>
{echarts_tag}
<style>
  :root {{ --bg:#0b1020; --card:#141a2e; --card2:#1a2340; --line:#2a3554; --text:#e8edf7; --sub:#93a0b8;
           --green:#22c55e; --red:#ef4444; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:radial-gradient(1200px 560px at 18% -8%, #182241 0%, #0b1020 55%), var(--bg); color:var(--text);
         font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif; padding:18px; }}
  .wrap {{ max-width:1280px; margin:0 auto; }}
  h1 {{ font-size:23px; font-weight:800; letter-spacing:.5px;
        background:linear-gradient(90deg,#ffffff 0%,#9db8ff 100%);
        -webkit-background-clip:text; background-clip:text; color:transparent; }}
  .header {{ display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap;
             gap:10px; margin-bottom:14px; }}
  .header .sub {{ color:var(--sub); font-size:13px; }}
  .badges {{ display:flex; gap:8px; flex-wrap:wrap; }}
  .badge {{ font-size:12px; padding:4px 12px; border-radius:14px; border:1px solid var(--line); color:var(--sub);
            background:rgba(255,255,255,.03); }}
  .badge.ok {{ color:#4ade80; border-color:#166534; background:rgba(34,197,94,.08); }}
  .badge.warn {{ color:#fbbf24; border-color:#854d0e; background:rgba(251,191,36,.08); }}
  .grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }}
  .card {{ background:linear-gradient(180deg,#1a2340 0%,#141a2e 100%); border:1px solid var(--line);
           border-radius:14px; padding:16px; box-shadow:0 2px 8px rgba(0,0,0,.25);
           transition:border-color .2s ease, box-shadow .2s ease; }}
  .card:hover {{ border-color:#3a4a78; box-shadow:0 6px 18px rgba(0,0,0,.35); }}
  .card h3 {{ font-size:15px; margin-bottom:10px; display:flex; justify-content:space-between; align-items:center; }}
  .card h3 .code {{ color:var(--sub); font-weight:400; font-size:12px; }}
  .num-row {{ display:flex; align-items:baseline; gap:10px; margin-bottom:6px; }}
  .big {{ font-size:28px; font-weight:700; }}
  .up {{ color:var(--red); }} .down {{ color:var(--green); }}
  .kv {{ display:grid; grid-template-columns:1fr 1fr; gap:6px 14px; margin:10px 0; font-size:13px; }}
  .kv .k {{ color:var(--sub); }} .kv .v {{ font-weight:600; }}
  .wins {{ font-size:11px; color:var(--sub); font-weight:400; line-height:1.55; margin-top:2px; }}
  .wins b {{ color:var(--text); font-weight:600; }}
  .chips {{ display:flex; flex-wrap:wrap; gap:6px; margin:8px 0; }}
  .chip {{ font-size:12px; padding:2px 8px; border-radius:10px; border:1px solid var(--line); color:var(--sub); }}
  .banner {{ border-radius:8px; padding:8px 10px; font-size:13px; font-weight:700; margin:6px 0; }}
  .mini {{ font-size:12px; color:var(--sub); line-height:1.7; }}
  .chart {{ width:100%; }}
  .ratio-card {{ grid-column:span 3; }}
  .ratio-main {{ display:flex; gap:20px; align-items:stretch; flex-wrap:wrap; }}
  .ratio-nums {{ min-width:260px; }}
  .ratio-chart {{ flex:1; min-width:420px; height:240px; }}
  .ratio-bar {{ height:8px; border-radius:4px; margin:8px 0;
                background:linear-gradient(90deg,#22c55e 0%,#22c55e 30%,#eab308 30%,#eab308 60%,#ef4444 60%,#ef4444 100%); }}
  .ratio-mark {{ display:flex; justify-content:space-between; font-size:11px; color:var(--sub); }}
  .macro-card {{ grid-column:span 3; }}
  .macro-row {{ display:flex; gap:14px; flex-wrap:wrap; }}
  .macro-item {{ flex:1; min-width:200px; background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.02)); border:1px solid var(--line);
                 border-radius:10px; padding:10px 12px; }}
  .macro-item .k {{ display:block; color:var(--sub); font-size:11px; margin-bottom:5px; }}
  .macro-item .v {{ font-weight:600; font-size:13px; line-height:1.6; }}
  .macro-item .v.ok {{ color:#4ade80; }} .macro-item .v.warn {{ color:#fbbf24; }}
  .macro-note {{ margin-top:8px; }}
  .decision-card {{ grid-column:span 3; }}
  .corr-card {{ grid-column:span 3; }}
  .corr-grid {{ display:flex; gap:14px; flex-wrap:wrap; align-items:stretch; }}
  .corr-item {{ flex:1; min-width:220px; background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.02)); border:1px solid var(--line);
                border-radius:10px; padding:10px 12px; }}
  .corr-item .k {{ display:block; color:var(--sub); font-size:11px; margin-bottom:5px; }}
  .corr-item .v {{ font-weight:700; font-size:16px; line-height:1.4; }}
  .corr-item .note {{ font-size:11px; color:var(--sub); margin-top:4px; line-height:1.5; }}
  .corr-bar {{ height:6px; border-radius:3px; margin-top:6px; background:#232c46; overflow:hidden; }}
  .corr-bar > div {{ height:100%; border-radius:3px; }}
  .dc-row {{ display:flex; gap:12px; flex-wrap:wrap; margin:10px 0 4px; }}
  .dc-block {{ flex:1; min-width:210px; background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.02)); border:1px solid var(--line);
               border-radius:10px; padding:10px 12px; }}
  .dc-block .k {{ display:block; color:var(--sub); font-size:11px; margin-bottom:5px; }}
  .dc-block .v {{ font-weight:600; font-size:13px; line-height:1.6; }}
  .scorebar {{ height:8px; border-radius:4px; background:#232c46; margin:6px 0; overflow:hidden; }}
  .scorebar > div {{ height:100%; border-radius:4px; }}
  .etf-grid {{ grid-template-columns:repeat(3,1fr); }}
  .price-charts {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-top:14px; }}
  .tall {{ height:300px; }}
  .pe-charts {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-top:14px; }}
  .v4tag {{ display:inline-block; font-size:11px; font-weight:700; color:#fff; background:#0f766e;
           border-radius:5px; padding:2px 7px; margin-left:8px; vertical-align:middle; letter-spacing:.5px; }}
  .st-badge {{ display:inline-block; font-size:12px; font-weight:700; padding:2px 9px; border-radius:6px;
              border:1px solid transparent; white-space:nowrap; }}
  .gate-row {{ display:flex; flex-wrap:wrap; gap:6px; margin:6px 0 0; }}
  .gate-chip {{ font-size:11px; padding:2px 7px; border-radius:5px; border:1px solid rgba(148,163,184,.35); color:var(--sub); }}
  .gate-chip.gate-warn {{ color:#eab308; border-color:rgba(234,179,8,.45); background:rgba(234,179,8,.10); }}
  .gate-chip.gate-block {{ color:#ef4444; border-color:rgba(239,68,68,.45); background:rgba(239,68,68,.10); }}
  .gate-chip.gate-ok {{ color:#22c55e; border-color:rgba(34,197,94,.4); background:rgba(34,197,94,.08); }}
  .next-cond {{ font-size:11.5px; color:var(--sub); margin-top:4px; line-height:1.6; }}
  .foot {{ margin-top:16px; color:var(--sub); font-size:12px; text-align:center; line-height:1.8; }}
  .rules {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-top:14px; }}
  .rules li {{ margin-left:16px; }}
  @media (max-width:900px) {{ .grid,.etf-grid,.price-charts,.pe-charts,.rules {{ grid-template-columns:1fr; }}
      .ratio-card,.macro-card {{ grid-column:span 1; }} }}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div>
      <h1>ETF 辅助决策看板<span class="v4tag">V4</span></h1>
      <div class="sub">{(' · '.join(f'{n} {c["code"]}' for n, c in ETF_POOL.items()))} ｜ 数据日期 {meta['data_date']} ｜ 生成 {meta['gen_time']} ｜ 策略版本 {meta.get('strategy_version', '')} ｜ 配置 {meta.get('config_source', '')}</div>
      {src_line}
    </div>
    <div class="badges" id="badges"></div>
  </div>

  <!-- 宏观环境：利率 / 政策 / 情绪 -->
  <div class="grid">
  <div class="card macro-card">
    <h3>宏观环境 <span class="code">利率环境 · 政策风向 · 市场情绪 ｜ 每日自动更新</span></h3>
    <div class="macro-row">
      <div class="macro-item"><span class="k">利率环境</span><span class="v" id="macro-rate">--</span></div>
      <div class="macro-item"><span class="k">政策风向</span><span class="v" id="macro-policy">--</span></div>
      <div class="macro-item"><span class="k">市场情绪</span><span class="v" id="macro-mkt">--</span></div>
    </div>
    <div class="mini macro-note" id="macro-note"></div>
  </div>
  </div>

  <!-- 创业板红利比 -->
  <div class="grid">
  <div class="card ratio-card">
    <h3>风格轮动 · 创业板红利比 <span class="code">创业板指 {GEM_INDEX} ÷ 中证红利 {DIV_INDEX}</span></h3>
    <div class="ratio-main">
      <div class="ratio-nums">
        <div class="num-row"><span class="big" id="ratio-val">--</span>
          <span style="font-size:13px;color:var(--sub)" id="ratio-pct">--</span></div>
        <div class="ratio-bar"></div>
        <div class="ratio-mark"><span>&lt;30% 成长相对低位</span><span>中段</span><span>&gt;70% 相对高位</span></div>
        <div class="banner" id="ratio-sug" style="margin-top:10px">--</div>
        <div class="mini">相对强弱=创业板指÷中证红利（各自相对基期涨幅之比）；分位为近{HISTORY_YEARS}年历史百分位（基期 <span id="ratio-base">--</span>）。相对强弱走高=创业板相对中证红利表现更强；分位不代表估值便宜度。</div>
      </div>
      <div class="ratio-chart" id="ratio-chart"></div>
    </div>
  </div>
  </div>

  <!-- V4 辅助决策简报：数据质量闸门 → 三维评分 → 六种操作状态 -->
  <div class="grid">
  <div class="card decision-card">
    <h3>辅助决策简报 <span class="code">数据质量闸门 · 趋势×估值×风险 · 六种操作状态 ｜ 综合评分仅作辅助排序</span></h3>
    <div id="combo-banner"></div>
    <div id="decision-brief"></div>
    <div class="next-cond">六状态＝允许分批加仓 / 正常定投 / 持有观察 / 暂停新增 / 再平衡评估 / 暂不判断。前序数据质量闸门与风险警告具有否决权：数据缺失或风险未知时，再高的综合评分也不会升级为主动加仓。</div>
  </div>
  </div>

  <!-- V4 组合配置体检（基准权重合计 / 单品种上限 / 同类分组集中度） -->
  <div class="grid">
  <div class="card">
    <h3>组合配置体检 <span class="code">基准权重合计 · 单品种上限 · 同类分组集中度 ｜ 来源 portfolio_config.json</span></h3>
    <div id="portfolio-check"></div>
  </div>
  </div>

  <!-- V4 数据口径说明（P0-3：来源 / 字段类型 / 计算口径 / 统计期间 / 最新日期） -->
  <div class="grid">
  <div class="card">
    <h3>数据口径说明 <span class="code">来源 · 字段类型 · 计算口径 · 统计期间 · 最新日期 ｜ 每项数据可追溯</span></h3>
    <div id="provenance"></div>
  </div>
  </div>

  <!-- 组合相关性（近250交易日日收益） -->
  <div class="grid">
  <div class="card corr-card">
    <h3>组合相关性 <span class="code">近250交易日日收益两两相关系数 · 判断组合分散度</span></h3>
    <div class="corr-grid" id="corr-brief"></div>
  </div>
  </div>

  <!-- ETF 卡片（由 ETF_POOL 循环生成） -->
  <div class="grid etf-grid" id="etf-cards"></div>

  <!-- 价格走势 -->
  <div class="price-charts" id="price-charts"></div>

  <!-- 估值分位走势（近3年滚动窗口） -->
  <div class="pe-charts" id="pe-charts"></div>

  <!-- 操作纪律 -->
  <div class="rules">
    <div class="card"><h3>定投纪律 <span style="font-size:11px;color:var(--sub);font-weight:400">（按近3年PE分位）</span></h3><ul class="mini" style="list-style:none">
      <li>· PE分位&lt;20%：双倍定投</li>
      <li>· PE分位20%~40%：正常定投</li>
      <li>· PE分位40%~70%：减半定投</li>
      <li>· PE分位&gt;70%：暂停定投，仅持底仓</li>
      <li>· 单日大跌&gt;3%可额外加投一份</li>
      <li>· 引擎估值阈值：低估&lt;30% / 高估&gt;70%（decision_engine PE_LOW/PE_HIGH，用于综合评分估值维度）</li>
    </ul></div>
    <div class="card"><h3>再平衡</h3><ul class="mini" style="list-style:none">
      <li>· 每半年（6月/12月）强制再平衡一次</li>
      <li>· 单只仓位偏离基准 <b>±10 绝对百分点（±10pp）</b> 触发调整（非相对偏离）</li>
      <li>· 单品种上限 40%、红利家族（红利ETF+红利低波ETF）合计上限 45%</li>
      <li>· 创业板/红利相对强弱分位&lt;30%或&gt;70%时关注风格切换，不据此单独交易</li>
      <li>· 实际仓位偏离需录入持仓数据后判定（P1）；当前为基准权重静态校验</li>
    </ul></div>
    <div class="card"><h3>长期纪律</h3><ul class="mini" style="list-style:none">
      <li>· 煤炭为强周期品种，硬上限 {ETF_POOL.get('煤炭ETF', {}).get('max_w', 30)}%；稳健配置研究建议上限 5%~10%（配置 research_cap_w），超配需自担波动</li>
      <li>· 银行低估值弹性小，作为组合压舱石</li>
      <li>· 红利在成长牛市中会跑输大盘，保持组合思维</li>
      <li>· 红利低波ETF（563020）与红利ETF同属红利家族，两者合计基准仓位上限 45%，避免同类过度集中</li>
      <li>· 自由现金流ETF（159201）为现金流质量成长品种，上市历史短但官方PE已接入（国证980092指数口径），按 {ETF_POOL.get('自由现金流ETF', {}).get('base_w', 15)}% 基准仓位执行</li>
      <li>· 「银行≠债券、红利≠固收、煤炭≠稳健底仓」：本组合无防御性资产（国债/货币ETF），权益暴露 100%，需自备现金缓冲</li>
      <li>· 所有信号为量化参考，须结合宏观与自身资金状况</li>
    </ul></div>
  </div>

  <div class="foot">
    数据来源：东方财富/腾讯/新浪（行情）· 中证指数官网（官方估值）· 国证指数官网（指数概览）· 英为财情（国债利率）· 央行（LPR）· 腾讯指数（两市成交）<br>
    159201/980092（国证自由现金流）：官方PE已接入——iFinD全历史播种（2016-01起，2829个交易日）＋腾讯官方指数行情日频增量（PE-TTM/PB），估值维度已参与决策评分（官方PE积累≥200条）；基金季报前十大权重 × 腾讯个股 PE(TTM)/PB 加权估算参考估值 + IOPV 溢价率保留为补充观测（非官方指数估值、仅参考）；价格样本为上市以来客观上限，技术面/相关性窗口已用满。<br>
    563020/H30269（中证红利低波动）：2023-12 上市，指数基日 2005-12-30、官方 PE 全历史可用（中证官网 indexCsiDsPe 自 2013-12 起）；价格样本约3年（指数同步积累），估值分位完整、价格 3Y 窗口基本满。<br>
    创业板红利比为归一化相对强弱（各自相对基期涨幅之比），基期随数据源起点变化、分位不受影响（实测跨源分位一致）；不构成估值信号。<br>
    PE分位口径：近1年250交易日 / 近3年750 / 近5年1250，样本数为窗口内实际交易日；主信号取近3年。<br>
    PE 历史序列以积累库中证官网官方PE（peg口径，指数上市以来全历史）为主，缺失区间按指数点位比例回推估算，分位仅供相对估值参考。<br>
    V4 决策（依据 GPT 代码评审 P0）：先过<b>数据质量闸门</b>——行情滞后/样本不足/趋势缺失/风险缺失/估值不足/来源降级逐项标注，前序闸门与风险警告具有否决权，后面的高分不能推翻前面的风险提示；<br>
    缺失维度不再用 0.5 中性值掩盖：只在可用维度上归一化权重（w 与 score_scope 明示参与维度），风险指标全缺失时判「数据不足」而非「低风险」；<br>
    输出六种操作状态（允许分批加仓 / 正常定投 / 持有观察 / 暂停新增 / 再平衡评估 / 暂不判断），综合评分仅作辅助排序；每条给出理由、数据日期与来源、仓位参照、下一步条件；<br>
    综合评分 = 趋势30% × 估值40% × 风险30%（缺维度时按可用维度归一化，不填中性值）；仓位上下限与集中度来自 portfolio_config.json（唯一配置来源，页面/文档/回测同口径）；<br>
    回测 backtest.py（V4）：修复「换仓当日收益与卖出成本被乘零」的缺陷，成本按成交金额买卖双向计入净值；结果为价格回报口径（未计分红再投资），须与买入持有基准对照阅读。<br>
    综合评分=做多友好度（0-100）：越高越适合买入（红色=高分位关注机会，蓝=低分位谨慎）；股息率来自中证官网XLS（近约20期官方口径），与PE数据源分离。<br>
    评分归因透明化：决策简报展示三维子分数（趋势/估值/风险各0-100）与权重敏感性（估值权重40%→30%的评分变化）；估值缺失标的（官方PE&lt;200条）评分与完整数据标的不可直接横向比较。<br>
    数据新鲜度对账：卡片"行情新/行情滞后"徽章与数据主日期强制对账，行情静默断更时滞后标的会标黄提示，防止旧数据冒充新数据参与判断。<br>
    高分位集群说明：红利/银行/红利低波同处近5年高分位，系2024-2026红利资产持续走强的真实估值状态而非数据异常；分位仅反映相对估值位置，需结合股息率与利差（专属指标）综合判断。<br>
    分批纪律（40/30/30）与 ATR 参考价位为执行参考，非自动止损指令、非保证成交价；做T支撑/压力（MA20/布林/近5日高）为统计参考，非保证成交位；回测结果见 backtest.py（信号次日生效，含手续费+滑点）。<br>
    宏观指标为每日自动采集（利率/情绪）与手动维护条目（政策风向）的组合，仅供环境参考。<br>
    本工具仅作数据监测与量化参考，不构成投资建议。市场有风险，投资需谨慎。
  </div>
</div>

<script>
const DATA = {payload_json};

/* 徽章：数据源状态 */
const badges = DATA.meta.badges.map(b => `<span class="badge ${{b.ok?'ok':'warn'}}">${{b.label}}</span>`).join('');
document.getElementById('badges').innerHTML = badges;

/* 宏观环境：利率 / 政策 / 情绪（V4 P0-5：每项都带「发布日期 + 更新状态」） */
(function () {{
  const M = DATA.macro || {{}};
  const el = (id) => document.getElementById(id);
  const ST = M.status || {{}};
  const SD = M.stale_days || {{}};
  const TH = M.thresholds || {{}};
  const stChip = (k) => {{
    const s = ST[k];
    if (s === 'fail') return '<span class="gate-chip gate-block">获取失败</span>';
    if (s === 'stale') {{
      const d = (SD[k] === null || SD[k] === undefined) ? '' : ' ' + SD[k] + '天';
      return '<span class="gate-chip gate-warn">陈旧' + d + ' · 不参与判断</span>';
    }}
    return '<span class="gate-chip gate-ok">正常</span>';
  }};
  const y10 = M.y10 !== null && M.y10 !== undefined;
  if (y10) {{
    el('macro-rate').innerHTML = '10Y国债 <b>' + M.y10.toFixed(2) + '%</b>（' + (M.y10_chg >= 0 ? '+' : '') + M.y10_chg.toFixed(0) + 'bp 较20日前 · 数据日 ' + M.y10_date + '）' + stChip('y10');
    const r = el('macro-rate');
    r.classList.add(M.y10_label && M.y10_label.indexOf('宽松') >= 0 ? 'ok' : (M.y10_label && M.y10_label.indexOf('偏紧') >= 0 ? 'warn' : ''));
  }} else {{
    el('macro-rate').innerHTML = '10Y国债 获取失败（接口未返回有效数据）' + stChip('y10');
  }}
  const pol = [];
  if (M.lpr1 !== null && M.lpr1 !== undefined) pol.push('LPR 1Y <b>' + M.lpr1.toFixed(1) + '%</b> / 5Y <b>' + M.lpr5.toFixed(1) + '%</b>（发布日 ' + (M.lpr_date || '--') + '）');
  let polHtml = pol.length ? pol.join(' ｜ ') : 'LPR 获取失败';
  if (M.policy_date) polHtml += '<span style="font-size:10px;color:var(--sub)">（政策条目维护于 ' + M.policy_date + '；自动指标与手动条目分列，手动条目不参与评分）</span>';
  el('macro-policy').innerHTML = polHtml + stChip('lpr');
  const mk = M.mkt !== null && M.mkt !== undefined;
  if (mk) {{
    const yiyuan = (M.mkt / 1e5).toFixed(0);          // 千元→亿元
    const wanYi = (M.mkt / 1e9).toFixed(2);           // 千元→万亿元
    const rt = M.mkt_ratio;
    let lab = '平稳', cls = '';
    if (rt !== null && rt !== undefined) {{
      if (rt >= 1.2) {{ lab = '放量(' + rt.toFixed(2) + 'x20日均)'; cls = 'warn'; }}
      else if (rt <= 0.8) {{ lab = '缩量(' + rt.toFixed(2) + 'x20日均)'; cls = 'ok'; }}
      else lab = '平稳(' + rt.toFixed(2) + 'x20日均)';
    }}
    el('macro-mkt').innerHTML = '两市成交约 <b>' + wanYi + '万亿</b>（' + yiyuan + '亿 · 数据日 ' + M.mkt_date + '）· ' + lab + stChip('mkt');
    if (cls) el('macro-mkt').classList.add(cls);
  }} else {{
    el('macro-mkt').innerHTML = '两市成交 获取失败（接口未返回有效数据）' + stChip('mkt');
  }}
  const bad = ['y10', 'lpr', 'mkt'].filter(k => ST[k] === 'fail').length;
  const stale = ['y10', 'lpr', 'mkt'].filter(k => ST[k] === 'stale').length;
  el('macro-note').textContent = '采集状态：失败 ' + bad + ' 项 / 陈旧 ' + stale + ' 项；陈旧或获取失败的数据只作历史参考，不进入环境判断'
    + '（新鲜度阈值：国债' + (TH.y10 || 10) + '天 / LPR' + (TH.lpr || 45) + '天 / 成交额' + (TH.mkt || 5) + '天）。'
    + '政策风向为手动维护条目，维护日见上，每周更新——详见操作纪律与README。';
}})();

/* 创业板红利比 */
const R = DATA.ratio;
if (R) {{
  document.getElementById('ratio-val').textContent = R.latest.toFixed(4);
  document.getElementById('ratio-pct').textContent = '历史分位 ' + R.pct + '% (' + R.n + ' 个交易日)';
  const rb = document.getElementById('ratio-base');
  if (rb) rb.textContent = R.base_date + '（创业板指 ' + R.base_gem + ' / 中证红利 ' + R.base_div + '）';
  const sug = document.getElementById('ratio-sug');
  sug.textContent = R.sug;
  sug.style.color = R.sug_color;
  const rcBox = document.getElementById('ratio-chart');
  try {{
    const rc = echarts.init(rcBox);
    rc.setOption({{
      backgroundColor:'transparent',
      grid:{{left:50,right:16,top:20,bottom:30}},
      tooltip:{{trigger:'axis'}},
      xAxis:{{type:'category',data:R.dates,axisLabel:{{color:'#8a93ab',fontSize:11}}}},
      yAxis:{{type:'value',scale:true,axisLabel:{{color:'#8a93ab'}},splitLine:{{lineStyle:{{color:'#232c46'}}}}}},
      series:[{{
        type:'line',data:R.ratios,smooth:true,symbol:'none',
        lineStyle:{{color:'#a78bfa',width:2}},
        areaStyle:{{color:'rgba(167,139,250,0.12)'}},
        markLine:{{silent:true,symbol:'none',
          data:[{{yAxis:1.0,label:{{formatter:'相对基准 1.0',color:'#8a93ab'}},lineStyle:{{color:'#8a93ab',type:'dashed'}}}}]}}
      }}]
    }});
    window.addEventListener('resize', () => rc.resize());
  }} catch (err) {{
    rcBox.innerHTML = '<div style="font-size:11px;color:#fbbf24;padding:10px">图表渲染失败（ECharts未加载），其余内容不受影响</div>';
  }}
}}

/* V4 辅助决策简报：数据质量闸门 → 三维评分 → 六种操作状态 */
function briefColor(score) {{
  if (score === null || score === undefined) return '#94a3b8';
  if (score >= 70) return '#ef4444';
  if (score >= 55) return '#f97316';
  if (score >= 40) return '#eab308';
  return '#3b82f6';
}}
function stateBadge(d) {{
  const ST = {{
    allow_add:  ['#ef4444', 'rgba(239,68,68,.14)'],
    normal_dca: ['#f97316', 'rgba(249,115,22,.14)'],
    hold:       ['#eab308', 'rgba(234,179,8,.14)'],
    pause:      ['#3b82f6', 'rgba(59,130,246,.14)'],
    rebalance:  ['#8b5cf6', 'rgba(139,92,246,.14)'],
    skip:       ['#94a3b8', 'rgba(148,163,184,.14)']
  }};
  const st = d.state || 'skip';
  const t = ST[st] || ST.skip;
  return '<span class="st-badge" style="color:' + t[0] + ';background:' + t[1] + ';border-color:' + t[0] + '66">'
       + (d.state_label || '暂不判断') + '</span>';
}}
function gateChips(g) {{
  const flags = (g && g.flags) || [];
  if (!flags.length) return '<span class="gate-chip gate-ok">数据质量闸门通过</span>';
  return flags.map(f => '<span class="gate-chip gate-' + (f.level === 'block' ? 'block' : (f.level === 'warn' ? 'warn' : 'ok')) + '">'
         + (f.label || '') + '</span>').join('');
}}
(function () {{
  const wrap = document.getElementById('decision-brief');
  if (!wrap) return;
  const combo = document.getElementById('combo-banner');
  if (combo) {{
    const st = DATA.etfs.map(e => (e.decision || {{}}).state || 'skip');
    const nAdd = st.filter(s => s === 'allow_add').length;
    const nHold = st.filter(s => s === 'hold' || s === 'normal_dca').length;
    const nStop = st.filter(s => s === 'pause' || s === 'skip').length;
    let txt = '';
    if (nAdd > 0) {{
      txt = '组合状态：' + nAdd + ' 只标的通过闸门并满足分批加仓条件（' + nHold + ' 只持有/定投、' + nStop + ' 只暂停/暂不判断）。加仓按 40/30/30 三笔执行，前一笔未站稳不加下一笔；单只不超过配置上限。';
    }} else if (nStop >= st.length && st.length > 0) {{
      txt = '组合状态：全部标的处于「暂停新增/暂不判断」→ 保留现金弹药，等待估值回落与趋势确认；闲置资金可配置货币基金/国债逆回购（1-7天）获取流动收益，按再平衡纪律维持底仓。';
    }} else {{
      txt = '组合状态：无标的同时满足主动加仓条件（' + nHold + ' 只持有/正常定投、' + nStop + ' 只暂停或数据不足）。以持有与纪律定投为主，不追高、不机械补仓。';
    }}
    const gs = DATA.data_gate || {{}};
    const gTxt = '数据质量闸门：通过 ' + (gs.ok || 0) + ' 只 ｜ 告警 ' + (gs.warn || 0) + ' 只 ｜ 阻断 ' + (gs.block || 0) + ' 只';
    combo.innerHTML = '<div style="font-size:12px;line-height:1.6;padding:6px 10px;border-radius:6px;background:#1b2438;border:1px solid #2a3654;color:var(--sub)">'
      + txt + '<br><span style="font-size:11px">' + gTxt + '</span></div>';
  }}
  DATA.etfs.forEach(e => {{
    const d = e.decision || {{}};
    const sc = d.score;
    const barColor = briefColor(sc);
    const g = e.gate || {{}};
    const trendTxt = e.trend && e.trend.label ? e.trend.label : '--';
    const riskTxt = e.risk && e.risk.label ? e.risk.label : '--';
    const valTxt = d.val || '--';
    const batchTxt = e.batch ? e.batch.text : '--';
    const reasons = (d.reasons || []).join(' ｜ ');
    const sub = d.sub || {{}};
    const subParts = ['trend','val','risk'].map(k => {{
      const lab = k==='trend' ? '趋势' : (k==='val' ? '估值' : '风险');
      const sv = sub[k];
      return lab + ' <b>' + (sv===null||sv===undefined ? '--' : sv) + '</b>';
    }}).join(' · ');
    const w = d.w || {{}};
    const scopeTxt = (d.score_scope && d.score_scope.length)
      ? '参与维度 ' + d.score_scope.map(k => k==='trend'?'趋势':(k==='val'?'估值':'风险')).join('+') + '（缺失维度权重为0，不填中性值）'
      : '无维度可参与';
    const sensTxt = (w.val && w.val > 0)
      ? '估值权重40%→30%时评分 ' + (sc===null||sc===undefined?'--':sc) + ' → ' + (d.score_alt===null||d.score_alt===undefined?'--':d.score_alt)
      : '估值未参与，无敏感性';
    const valMissWarn = (w.val === 0)
      ? '<span style="display:block;font-size:11px;color:#fbbf24;margin-top:4px">⚠ 估值缺失：不使用估值维度，本评分与官方PE完整标的不可直接横向比较</span>'
      : '';
    const qualWarn = (d.quality !== undefined && d.quality !== null && d.quality < 100)
      ? '<span style="display:block;font-size:11px;color:#fbbf24;margin-top:4px">⚠ 数据完整度 ' + d.quality + '%，缺失维度未参与评分，可信度降低</span>'
      : '';
    const stepsHtml = e.batch && e.batch.steps
      ? e.batch.steps.map(s => `<span class="chip">${{s.phase}} ${{s.ratio}}：${{s.trigger}}</span>`).join('')
      : '';
    const atrTxt = e.atr_ref && e.atr_ref.text ? e.atr_ref.text : '--';
    const pos = e.pos || {{}};
    const posTxt = '基准 ' + (pos.base_w===null||pos.base_w===undefined?'--':pos.base_w+'%')
                 + ' ｜ 上限 ' + (pos.max_w===null||pos.max_w===undefined?'--':pos.max_w+'%')
                 + ' ｜ 下限 ' + (pos.min_w===null||pos.min_w===undefined?'--':pos.min_w+'%');
    const nextHtml = (d.next_conditions && d.next_conditions.length)
      ? '<span class="next-cond">下一步条件：' + d.next_conditions.slice(0,3).join('；') + '</span>' : '';
    wrap.insertAdjacentHTML('beforeend', `
      <div class="dc-row" style="border-top:3px solid ${{e.color}};padding-top:8px">
        <div class="dc-block" style="flex:0 0 170px">
          <span class="k">${{e.name}} ${{e.code}}</span>
          <div style="margin:2px 0 6px">${{stateBadge(d)}}</div>
          <span class="v">综合评分 <b style="font-size:20px;color:${{barColor}}">${{sc===null||sc===undefined?'--':sc}}</b><span style="font-size:11px;color:var(--sub)">/100（仅辅助排序）</span></span>
          <div class="scorebar"><div style="width:${{sc===null||sc===undefined?0:sc}}%;background:${{barColor}}"></div></div>
          <span style="font-size:11px;color:var(--sub)">数据完整度 ${{d.quality===undefined||d.quality===null?'--':d.quality+'%'}} · 官方PE ${{e.official_pe_count}}条</span>
          <span class="k" style="margin-top:5px">三维子分数</span>
          <span class="v" style="font-weight:400;font-size:12px;line-height:1.5">${{subParts}}</span>
          <span class="k" style="margin-top:4px">权重敏感性</span>
          <span class="v" style="font-weight:400;font-size:11px;color:var(--sub)">${{sensTxt}}<br>${{scopeTxt}}</span>
          ${{valMissWarn}}
          ${{qualWarn}}
        </div>
        <div class="dc-block">
          <span class="k">趋势 / 估值 / 风险</span>
          <span class="v">${{trendTxt}} · ${{valTxt}} · ${{riskTxt}}</span>
          <span class="k" style="margin-top:4px">主要判断依据</span>
          <span class="v" style="font-weight:400;font-size:12px;line-height:1.6">${{reasons||'--'}}</span>
          <span class="k" style="margin-top:4px">数据质量闸门</span>
          <div class="gate-row">${{gateChips(g)}}</div>
        </div>
        <div class="dc-block">
          <span class="k">当前建议（六状态）</span>
          <span class="v" style="color:${{d.sug_color||'#94a3b8'}}">${{d.state_label||d.suggestion||'--'}}</span>
          <span class="k" style="margin-top:4px">仓位参照</span>
          <span class="v" style="font-weight:400;font-size:12px;color:var(--sub)">${{posTxt}}</span>
          <span class="k" style="margin-top:4px">分批纪律</span>
          <span class="v" style="font-weight:400;font-size:12px">${{batchTxt}}</span>
          ${{stepsHtml?`<div class="chips" style="margin-top:4px">${{stepsHtml}}</div>`:''}}
          ${{nextHtml}}
        </div>
        <div class="dc-block" style="flex:0 0 200px">
          <span class="k">ATR 参考价位</span>
          <span class="v" style="font-weight:400;font-size:12px;line-height:1.6">${{atrTxt}}</span>
          <span class="k" style="margin-top:4px">数据日期 / 来源</span>
          <span class="v" style="font-weight:400;font-size:11px;color:var(--sub)">行情 ${{e.latest_date||'--'}}${{g.stale_days!==null&&g.stale_days!==undefined?'（滞后'+g.stale_days+'天）':''}} ｜ PE ${{e.pe_last_date||'--'}} ｜ 官方PE·中证/国证/iFinD</span>
        </div>
      </div>`);
  }});
}})();

/* V4 组合配置体检渲染（基准权重合计 / 单品种上下限 / 集中度与强周期提示） */
(function () {{
  const box = document.getElementById('portfolio-check');
  if (!box) return;
  const P = DATA.portfolio || {{}};
  const flags = P.flags || [];
  const sum = (P.base_sum === undefined || P.base_sum === null) ? '--' : P.base_sum;
  let html = '<div style="font-size:12px;line-height:1.75;color:var(--sub)">'
    + '基准权重合计 <b style="color:' + (P.ok ? '#22c55e' : '#ef4444') + '">' + sum + '%</b>'
    + (P.ok ? '（=100%，通过）' : '（≠100%，需修正配置）')
    + '<br>'
    + DATA.etfs.map(e => {{
        const p = e.pos || {{}};
        return e.name + ' ' + (p.base_w === undefined || p.base_w === null ? '--' : p.base_w + '%')
             + (p.max_w === undefined || p.max_w === null ? '' : '（上限' + p.max_w + '%）');
      }}).join(' ｜ ');
  if (flags.length) {{
    html += '<div class="gate-row" style="margin-top:6px">'
      + flags.map(f => '<span class="gate-chip gate-'
        + (f.level === 'warn' ? 'warn' : (f.level === 'block' ? 'block' : 'ok')) + '">' + (f.label || '') + '</span>').join('')
      + '</div>';
  }}
  if (P.note) html += '<div style="font-size:11px;margin-top:4px">' + P.note + '</div>';
  html += '</div>';
  box.innerHTML = html;
}})();

/* V4 数据口径说明（P0-3）：来源 / 字段类型 / 计算口径 / 统计期间 / 最新日期 */
(function () {{
  const box = document.getElementById('provenance');
  if (!box) return;
  const rows = (DATA.meta && DATA.meta.provenance) || [];
  if (!rows.length) {{ box.textContent = '数据口径元数据暂缺'; return; }}
  let html = '<table style="width:100%;border-collapse:collapse;font-size:11.5px;line-height:1.55">'
    + '<thead><tr style="text-align:left;color:var(--sub);border-bottom:1px solid #2a3242">'
    + '<th style="padding:5px 6px">数据项</th><th style="padding:5px 6px">来源</th>'
    + '<th style="padding:5px 6px">字段类型</th><th style="padding:5px 6px">计算口径</th>'
    + '<th style="padding:5px 6px">统计期间</th><th style="padding:5px 6px">最新日期</th></tr></thead><tbody>';
  rows.forEach(function (r) {{
    html += '<tr style="border-bottom:1px solid #1c2432;vertical-align:top">'
      + '<td style="padding:5px 6px;white-space:nowrap">' + (r.item || '') + '</td>'
      + '<td style="padding:5px 6px">' + (r.source || '') + '</td>'
      + '<td style="padding:5px 6px">' + (r.field || '') + '</td>'
      + '<td style="padding:5px 6px">' + (r.calc || '') + '</td>'
      + '<td style="padding:5px 6px;white-space:nowrap">' + (r.period || '') + '</td>'
      + '<td style="padding:5px 6px;white-space:nowrap">' + (r.date || '--') + '</td></tr>';
  }});
  html += '</tbody></table>';
  box.innerHTML = html;
}})();

/* 组合相关性渲染（近250交易日日收益） */
(function () {{
  const box = document.getElementById('corr-brief');
  if (!box) return;
  const C = DATA.corr || {{}};
  const pairs = Object.keys(C).filter(k => k !== '_n');
  const n = C._n || 0;
  if (!pairs.length) {{
    box.innerHTML = '<div class="corr-item"><span class="k">组合相关性</span><span class="v" style="font-size:12px;font-weight:400">数据不足（需至少两只标的的日收益序列）</span></div>';
    return;
  }}
  const avg = pairs.reduce((s, k) => s + (C[k] || 0), 0) / pairs.length;
  function colorOf(r) {{ return r >= 0.8 ? '#ef4444' : r >= 0.6 ? '#f97316' : r >= 0.4 ? '#eab308' : r >= 0.2 ? '#22c55e' : '#3b82f6'; }}
  function labelOf(r) {{ return r >= 0.8 ? '高相关·同涨同跌' : r >= 0.6 ? '中高相关' : r >= 0.4 ? '中等相关' : r >= 0.2 ? '低相关·有分散' : '弱相关·分散佳'; }}
  let html = '';
  pairs.forEach(k => {{
    const r = C[k];
    const w = Math.min(100, Math.max(0, Math.abs(r) * 100));
    html += '<div class="corr-item"><span class="k">' + k + '</span>' +
      '<span class="v" style="color:' + colorOf(r) + '">' + r.toFixed(2) + ' <span style="font-size:11px;color:var(--sub);font-weight:400">' + labelOf(r) + '</span></span>' +
      '<div class="corr-bar"><div style="width:' + w.toFixed(0) + '%;background:' + colorOf(r) + '"></div></div></div>';
  }});
  html += '<div class="corr-item" style="flex:0 0 260px"><span class="k">解读</span>' +
    '<span class="v" style="font-size:12px;font-weight:400;line-height:1.7">' +
    (avg >= 0.7 ? '组合整体高相关（均值 ' + avg.toFixed(2) + '）：组合标的风格趋同，分散效果有限，注意同涨同跌风险；' + (n ? '样本 ' + n + ' 个交易日。' : '') :
     avg >= 0.4 ? '组合中等相关（均值 ' + avg.toFixed(2) + '）：具备一定分散度，但部分标的风格相近。' + (n ? '样本 ' + n + ' 个交易日。' : '') :
     '组合相关性较低（均值 ' + avg.toFixed(2) + '）：分散效果较好。' + (n ? '样本 ' + n + ' 个交易日。' : '')) +
    '</span></div>';
  // 高相关警示（千问评审二轮D-19：>0.8 的高相关对显式警示，不只展示均值）
  const highPairs = pairs.filter(k => (C[k] || 0) >= 0.8);
  if (highPairs.length) {{
    box.innerHTML = '<div class="corr-item" style="flex:0 0 100%;border-color:#7f1d1d"><span class="k">高相关警示</span><span class="v" style="font-size:12px;font-weight:400;color:#ef4444;line-height:1.7">' + highPairs.map(k => k + ' <b>' + C[k].toFixed(2) + '</b>').join('；') + '：同涨同跌，组合分散度下降、集中风险上升，建议按再平衡纪律控制该类仓位。</span></div>' + html;
  }} else {{
    box.innerHTML = html;
  }}
}})();

/* ETF 卡片 */
function fmtNum(v, d=2) {{ return v===null||v===undefined ? '--' : Number(v).toFixed(d); }}
const cardsEl = document.getElementById('etf-cards');
DATA.etfs.forEach(e => {{
  const up = e.pct_chg >= 0;
  const w = e.pe_windows || null;
  const w1 = w && w['近1年'] ? w['近1年'] : null;
  const w3 = w && w['近3年'] ? w['近3年'] : null;
  const w5 = w && w['近5年'] ? w['近5年'] : null;
  const peInfo = w1
    ? `PE ${{fmtNum(e.pe)}} · 1年 <b>${{w1.pct}}%</b> / 3年 <b>${{w3?w3.pct:'--'}}%</b> / 5年 <b>${{w5?w5.pct:'--'}}%</b>`
    : (e.pe!==null && e.pe!==undefined ? `PE ${{fmtNum(e.pe)}} · 分位 积累中` : 'PE N/A · 官方估值未接入');
  const priceSample = e.price_n
    ? `价格样本 <b>${{e.price_n}}</b> 条（${{e.code}} 上市以来${{e.index_n?' · 指数同步积累 '+e.index_n+' 条':''}}）：已达 250日相关性 / 120日均线参考下限；3Y/5Y 窗口与 PE 分位受上市时长限制`
    : '';
  const peSamples = w1
    ? `<div class="wins"><span>近1年 样本 <b>${{w1.n}}</b> 期 · ${{w1.start}}~${{w1.end}}</span><span>近3年 样本 <b>${{w3.n}}</b> 期 · ${{w3.start}}~${{w3.end}}</span><span>近5年 样本 <b>${{w5.n}}</b> 期 · ${{w5.start}}~${{w5.end}}</span></div>`
    : (priceSample ? `<div class="wins">${{priceSample}}</div>` : '--');
  const tagStyle = 'font-size:10px;color:var(--sub)';
  const supp = e.support.length ? e.support.map(x=>fmtNum(x.v,3)+`<span style="${{tagStyle}}">(${{x.tag}})</span>`).join(' / ') : '--';
  const pres = e.pressure.length ? e.pressure.map(x=>fmtNum(x.v,3)+`<span style="${{tagStyle}}">(${{x.tag}})</span>`).join(' / ') : '--';
  const d = e.decision || {{}};
  const sc = d.score;
  // 数据新鲜度徽章：行情更新日 vs 数据主日期对账（DeepSeek评审P0）
  const freshTag = e.fresh === 'lag'
    ? '<span style="font-size:10px;color:#fbbf24;border:1px solid #854d0e;border-radius:8px;padding:1px 6px">行情滞后</span>'
    : '<span style="font-size:10px;color:#4ade80;border:1px solid #166534;border-radius:8px;padding:1px 6px">行情新</span>';
  const bannerTxt = (d.suggestion || e.conclusion) + (sc===null||sc===undefined ? '' : `（综合评分 ${{sc}}）`);
  const bannerColor = d.sug_color || e.concl_color;
  const trendTxt2 = e.trend && e.trend.label ? e.trend.label : '--';
  const riskTxt2 = e.risk && e.risk.label ? e.risk.label : '--';
  const atrTxt2 = (e.atr_ref && e.atr_ref.ref_low !== undefined && e.atr_ref.ref_high !== undefined)
    ? e.atr_ref.ref_low + '~' + e.atr_ref.ref_high : '--';
  cardsEl.insertAdjacentHTML('beforeend', `
    <div class="card" style="border-top:3px solid ${{e.color}}">
      <h3>${{e.name}} <span class="code">${{e.code}}</span></h3>
      <div class="num-row"><span class="big">${{fmtNum(e.close,3)}}</span>
        <span class="${{up?'up':'down'}}">${{e.pct_chg>=0?'+':''}}${{e.pct_chg.toFixed(2)}}%</span>
        <span style="font-size:12px;color:var(--sub)">${{e.latest_date}} ${{freshTag}}</span></div>
      <div class="kv">
        <span class="k">PE / 分位</span><span class="v">${{peInfo}}</span>
        <span class="k">分位样本</span><span class="v">${{peSamples}}</span>
        <span class="k">股息率</span><span class="v">${{e.dy!==null?e.dy+'%':'--'}}</span>
        <span class="k">RSI(14)</span><span class="v">${{fmtNum(e.rsi,0)}}</span>
        <span class="k">MA20 / MA60</span><span class="v">${{fmtNum(e.ma20,3)}} / ${{fmtNum(e.ma60,3)}}</span>
        <span class="k">偏离20日线</span><span class="v">${{e.dev20>0?'+':''}}${{e.dev20.toFixed(1)}}%</span>
        <span class="k">趋势 / 风险</span><span class="v">${{trendTxt2}} · ${{riskTxt2}}</span>
        <span class="k">ATR参考区间</span><span class="v">${{atrTxt2}}</span>
        ${{e.special ? e.special.rows.map(r=>`<span class="k">${{r.k}}</span><span class="v">${{r.v}}${{r.note?' <span style="font-size:10px;color:var(--sub)">('+r.note+')</span>':''}}</span>`).join('') : ''}}
        <span class="k">做T支撑</span><span class="v" style="color:#22c55e">${{supp}}</span>
        <span class="k">做T压力</span><span class="v" style="color:#ef4444">${{pres}}</span>
      </div>
      <div class="chips">${{e.signals.map(s=>`<span class="chip">${{s}}</span>`).join('')}}</div>
      ${{e.note?`<div class="mini" style="margin-top:6px;color:#fbbf24;border:1px solid #854d0e;border-radius:6px;padding:6px 8px;background:rgba(251,191,36,0.06)">${{e.note}}</div>`:''}}
      <div class="banner" style="background:${{bannerColor}}22;color:${{bannerColor}}">${{bannerTxt}}</div>
      <div class="mini">定投：<b style="color:${{e.dca_color}}">${{e.dca}}</b> ｜ ${{e.t_text}}</div>
      <div class="mini" style="margin-top:6px">长期：${{e.long_txt}}</div>
    </div>`);
}});

/* 价格走势图 */
const pcEl = document.getElementById('price-charts');
DATA.etfs.forEach(e => {{
  const div = document.createElement('div');
  div.className = 'card';
  div.style.borderTop = '3px solid ' + e.color;
  div.innerHTML = `<h3>${{e.name}} 价格走势 <span class="code">近250交易日·收盘/MA20/MA60/布林带</span></h3>
    <div class="chart tall" id="pc-${{e.code}}"></div>`;
  pcEl.appendChild(div);
  const chartDiv = document.getElementById('pc-' + e.code);
  try {{
    const chart = echarts.init(chartDiv);
    const dates = e.dates, close = e.closes, ma20 = e.ma20s, ma60 = e.ma60s,
          bup = e.boll_ups, blow = e.boll_lows;
    chart.setOption({{
      backgroundColor:'transparent',
      grid:{{left:50,right:16,top:20,bottom:30}},
      tooltip:{{trigger:'axis'}},
      legend:{{data:['收盘','MA20','MA60','布林上','布林下'],textStyle:{{color:'#8a93ab',fontSize:10}},top:0}},
      xAxis:{{type:'category',data:dates,axisLabel:{{color:'#8a93ab',fontSize:10,hideOverlap:true}}}},
      yAxis:{{type:'value',scale:true,axisLabel:{{color:'#8a93ab',fontSize:10}},splitLine:{{lineStyle:{{color:'#232c46'}}}}}},
      series:[
        {{name:'布林上',type:'line',data:bup,symbol:'none',lineStyle:{{color:'#475569',width:1,type:'dashed'}},stack:'b'}},
        {{name:'布林下',type:'line',data:blow,symbol:'none',lineStyle:{{color:'#475569',width:1,type:'dashed'}},stack:'b'}},
        {{name:'MA20',type:'line',data:ma20,symbol:'none',lineStyle:{{color:'#f59e0b',width:1}}}},
        {{name:'MA60',type:'line',data:ma60,symbol:'none',lineStyle:{{color:'#10b981',width:1}}}},
        {{name:'收盘',type:'line',data:close,symbol:'none',lineStyle:{{color:e.color,width:2}},areaStyle:{{color:'rgba(255,255,255,0.03)'}}}}
      ]
    }});
    window.addEventListener('resize', () => chart.resize());
  }} catch (err) {{
    chartDiv.innerHTML = '<div style="font-size:11px;color:#fbbf24;padding:10px">价格图渲染失败（ECharts未加载），其余内容不受影响</div>';
  }}
}});

/* 估值分位图 */
const peEl = document.getElementById('pe-charts');
DATA.etfs.forEach(e => {{
  const div = document.createElement('div');
  div.className = 'card';
  div.style.borderTop = '3px solid ' + e.color;
  div.innerHTML = `<h3>${{e.name}} 估值分位走势 <span class="code">PE分位·近3年滚动窗口(750交易日)</span></h3>
    <div class="chart" style="height:170px" id="pe-${{e.code}}"></div>`;
  peEl.appendChild(div);
  const peDiv = document.getElementById('pe-' + e.code);
  try {{
    const chart = echarts.init(peDiv);
    chart.setOption({{
      backgroundColor:'transparent',
      grid:{{left:40,right:14,top:16,bottom:24}},
      tooltip:{{trigger:'axis'}},
      xAxis:{{type:'category',data:e.pe_dates,axisLabel:{{show:false}}}},
      yAxis:{{type:'value',min:0,max:100,axisLabel:{{color:'#8a93ab',fontSize:10,formatter:'{{value}}%'}},
              splitLine:{{lineStyle:{{color:'#232c46'}}}}}},
      series:[{{type:'line',data:e.pe_pcts,symbol:'none',lineStyle:{{color:'rgba(148,163,184,0.85)',width:1}},
               areaStyle:{{color:'rgba(148,163,184,0.12)'}}}}]
    }});
    window.addEventListener('resize', () => chart.resize());
  }} catch (err) {{
    peDiv.innerHTML = '<div style="font-size:11px;color:#fbbf24;padding:10px">估值图渲染失败（ECharts未加载），其余内容不受影响</div>';
  }}
}});
</script>
</body>
</html>"""
    try:
        DASHBOARD_PATH.write_text(html, encoding="utf-8")
        return str(DASHBOARD_PATH)
    except Exception as e:
        log(f"看板写盘失败: {e}")
        return None


def render_fallback(meta: dict):
    """云端容错：全部数据源失败时生成占位看板（保留上一次成功快照不被覆盖）"""
    if DASHBOARD_PATH.exists():
        return str(DASHBOARD_PATH)  # 已有历史看板则保留原样
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ETF 辅助决策看板 V4（数据暂不可用）</title>
<style>
  body {{ background:#0b1020; color:#e6ebf5; font-family:"Microsoft YaHei",system-ui,sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  .box {{ text-align:center; max-width:520px; padding:24px; }}
  h1 {{ font-size:20px; margin-bottom:12px; }}
  p {{ color:#8a93ab; font-size:14px; line-height:1.8; }}
</style>
</head>
<body>
<div class="box">
  <h1>ETF 辅助决策看板 V4 · 数据暂不可用</h1>
  <p>本次运行（{meta['gen_time']}）所有行情数据源均获取失败。<br>
     请稍后在仓库 Actions 页手动「Run workflow」重试。<br>
     若此前已有看板，将保留上一次成功快照。</p>
</div>
</body>
</html>"""
    try:
        DASHBOARD_PATH.write_text(html, encoding="utf-8")
        return str(DASHBOARD_PATH)
    except Exception as e:
        log(f"占位看板写盘失败: {e}")
        return None


# ============================================================
# 7. 主流程
# ============================================================
def main():
    run_at = dt.datetime.now()
    # 日志轮转（千问评审二轮D-20）：run_log 超限时保留最近 N 行，避免无界增长
    try:
        if LOG_PATH.exists():
            _lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
            if len(_lines) > 1500:
                LOG_PATH.write_text("\n".join(_lines[-1200:]) + "\n", encoding="utf-8")
    except Exception:
        pass
    cloud_mode = "--cloud" in sys.argv  # 云端(GitHub Actions)容错模式
    meta = {"data_date": "--", "gen_time": run_at.strftime("%Y-%m-%d %H:%M:%S"), "badges": [],
            "version": V4_VERSION, "strategy_version": STRATEGY_VERSION, "config_source": CONFIG_SOURCE}
    badges = [{"label": "东方财富行情", "ok": True}, {"label": "中证指数官方估值", "ok": True},
              {"label": "估值分位·日频估算(3Y)", "ok": True},
              {"label": "数据质量闸门 V4", "ok": True}]

    # ---- 风格轮动：创业板红利比 ----
    ratio = build_ratio()
    if ratio is None:
        badges[0]["ok"] = False
        log("创业板红利比计算失败（创业板指/中证红利行情缺失）")

    # ---- 宏观环境（一次获取，供专属指标与看板共用）----
    macro = get_macro()

    # ---- 估值历史库 ----
    hist = load_history()

    # ---- ETF 池逐标的数据采集与信号构建 ----
    etfs = []
    data_dates = set()
    price_rets = {}   # 组合相关性：近250交易日日收益
    for name, cfg in ETF_POOL.items():
        price_df = safe_call(f"{name}行情", get_etf_daily, cfg["code"])
        if price_df is None:
            badges[0]["ok"] = False
            log(f"{name} 行情缺失，跳过")
            continue
        price_df = calc_indicators(price_df)
        data_dates.add(str(price_df["date"].iloc[-1].date()))

        val_latest, pe_hist_df, v_src = get_valuation(cfg["index_code"], cfg["index_name"], hist)
        hist = pe_hist_df  # 保持全局历史
        # 官方股息率补丁：中证 XLS 近20期（仅 dy 字段，不碰 pe/pb 口径）
        dy_df = safe_call(f"{name}股息率", get_index_dy_csindex, cfg["index_code"])
        if dy_df is not None and not dy_df.empty:
            hist = patch_dy(hist, cfg["index_code"], cfg["index_name"], dy_df)
            try:
                hist.to_csv(HISTORY_PATH, index=False, encoding="utf-8-sig")
            except Exception as e:
                log(f"股息率补丁写盘失败: {e}")
            # 刷新展示用最新股息率（取最近一个有官方 dy 的日期行）
            if val_latest is not None:
                vsub = hist[hist["index_code"] == str(cfg["index_code"]).zfill(6)]
                vrow = vsub[vsub["dy"].notna()].sort_values("date")
                if not vrow.empty:
                    val_latest["dy"] = float(vrow.iloc[-1]["dy"])
        if "官网" not in v_src:
            badges[1]["ok"] = False  # 本次未取到官方估值（沿用积累库/上次数据）
        if "暂无" in v_src:
            badges[1]["ok"] = False
            badges[2]["ok"] = False

        # 日频 PE 序列（估算口径：官方最新PE × 指数点位比例回推，官方积累库覆盖近期）
        # 说明：PE 属于指数（000922/399986/000820/H30269），指数日线为真实点位、无复权问题；
        #       ETF 价格受分红/复权因子影响会扭曲 PE 回推，故 PE 序列一律基于指数日线。
        idx_df = safe_call(f"{cfg['index_name']}指数行情", get_index_daily, cfg["index_code"])
        if idx_df is None:
            log(f"{name} 指数日线缺失，停止生成日频PE估算，避免复权ETF价格冒充指数点位")
        sub_acc = hist[hist["index_code"] == cfg["index_code"]]
        pe_series = build_pe_series(idx_df, val_latest, sub_acc) if idx_df is not None else None
        # 官方积累库覆盖比指数日线更早时，展示序列直接用官方全历史
        # （980092：指数日线仅 2024-09 起约451条，官方积累 2016-01 起 2829 条，
        #   用官方序列后近3年/近5年分位可用；清洗在 calc_pe_windows 内统一做）
        if pe_series is not None and sub_acc is not None and not sub_acc.empty:
            off_s = sub_acc.dropna(subset=["pe"])[["date", "pe"]].sort_values("date")
            if len(off_s) >= 200 and pd.to_datetime(off_s["date"].iloc[0]) < pd.to_datetime(pe_series.index[0]):
                pe_series = off_s.set_index("date")["pe"].astype(float)
        if pe_series is None or len(pe_series) < 250:
            badges[2]["ok"] = False
            log(f"{name} 日频PE序列不足(样本{0 if pe_series is None else len(pe_series)})，分位不可用")

        # 官方 PE 积累历史（V3 决策引擎估值维度：仅 ≥200 条有效记录参与）
        pe_hist_official = sub_acc.dropna(subset=["pe"]).copy() if sub_acc is not None and not sub_acc.empty else None

        sig = build_signal(name, cfg, price_df, val_latest, pe_series, pe_hist_official,
                           run_date=run_at.date(), src_flags=dict(SRC_STATS))
        _ow = (sig.get("official_pe_windows") or {})
        sig["special"] = build_special(name, cfg, price_df, val_latest, macro, price_rets,
                                       official_ctx={"ow_main": _ow.get(PE_MAIN_WINDOW),
                                                     "official_count": sig.get("official_pe_count", 0)})
        # 图表数据
        tail = price_df.tail(250)
        sig["dates"] = [str(d.date()) for d in tail["date"]]
        sig["closes"] = [round(float(x), 4) for x in tail["close"]]
        sig["ma20s"] = [None if pd.isna(x) else round(float(x), 4) for x in tail["ma20"]]
        sig["ma60s"] = [None if pd.isna(x) else round(float(x), 4) for x in tail["ma60"]]
        sig["boll_ups"] = [None if pd.isna(x) else round(float(x), 4) for x in tail["boll_up"]]
        sig["boll_lows"] = [None if pd.isna(x) else round(float(x), 4) for x in tail["boll_low"]]
        sig["price_n"] = int(len(price_df))
        sig["index_n"] = int(len(idx_df)) if idx_df is not None else 0
        etfs.append(sig)
        price_rets[name] = price_df.set_index("date")["close"].pct_change().dropna().tail(250)
        w = sig.get("pe_windows") or {}
        p1 = w.get("近1年") or {}
        p3 = w.get("近3年") or {}
        p5 = w.get("近5年") or {}
        d = sig["decision"] or {}
        g = sig.get("gate") or {}
        log(f"{name}: 收盘 {sig['close']:.3f} | PE {sig['pe']} 分位 1Y {p1.get('pct')}% / "
            f"3Y {p3.get('pct')}% / 5Y {p5.get('pct')}% (n={p1.get('n')}/{p3.get('n')}/{p5.get('n')}) | "
            f"官方PE {sig['official_pe_count']}条 | 决策 {d.get('score')}分[{d.get('quality')}%] "
            f"状态「{d.get('state_label')}」| 闸门 {g.get('level', 'ok')}"
            + (f"（{g.get('summary')}）" if g.get("summary") and g.get("level") != "ok" else ""))

        # V3：输出价格历史 CSV（供 backtest.py 回测，每日运行自动积累）
        try:
            hist_csv = OUT_DIR / f"price_history_{cfg['code']}.csv"
            prev_df = None
            if hist_csv.exists():
                try:
                    prev_df = pd.read_csv(hist_csv, parse_dates=["date"])
                except Exception:
                    prev_df = None
            cur = price_df[["date", "close"]].copy()
            cur["date"] = pd.to_datetime(cur["date"])
            merged = pd.concat([prev_df, cur], ignore_index=True) if prev_df is not None and not prev_df.empty else cur
            merged = merged.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
            merged.to_csv(hist_csv, index=False, encoding="utf-8-sig")
            log(f"{name}: 价格历史已积累 {len(merged)} 条 -> {hist_csv.name}")
        except Exception as e:
            log(f"{name}: 价格历史写盘失败 {e}")

        # V3：输出指数日线历史 CSV（样本扩大/交叉验证，每日运行自动积累）
        if idx_df is not None and not idx_df.empty:
            try:
                idx_csv = OUT_DIR / f"index_history_{cfg['index_code']}.csv"
                prev_idx = None
                if idx_csv.exists():
                    try:
                        prev_idx = pd.read_csv(idx_csv, parse_dates=["date"])
                    except Exception:
                        prev_idx = None
                cur_idx = idx_df[["date", "close"]].copy()
                cur_idx["date"] = pd.to_datetime(cur_idx["date"])
                merged_idx = pd.concat([prev_idx, cur_idx], ignore_index=True) if prev_idx is not None and not prev_idx.empty else cur_idx
                merged_idx = merged_idx.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
                merged_idx.to_csv(idx_csv, index=False, encoding="utf-8-sig")
                log(f"{name}: 指数历史已积累 {len(merged_idx)} 条 -> {idx_csv.name}")
            except Exception as e:
                log(f"{name}: 指数历史写盘失败 {e}")

    # 数据日期：取最新交易日为主日期；若各标的最新行情日不一致，显式标注滞后只数（DeepSeek评审P0：数据新鲜度强制对账）
    main_date = max(data_dates) if data_dates else "--"
    if data_dates and len(data_dates) > 1:
        lag_n = sum(1 for d in data_dates if d != main_date)
        meta["data_date"] = f"{main_date}（{lag_n} 只行情滞后）"
    else:
        meta["data_date"] = main_date if data_dates else "--"
    for s in etfs:
        s["fresh"] = "ok" if s["latest_date"] == main_date else "lag"
    # V4 数据质量闸门汇总（badge 与页面提示使用）
    gate_levels = [(s.get("gate") or {}).get("level", "ok") for s in etfs]
    n_block = sum(1 for x in gate_levels if x == "block")
    n_warn = sum(1 for x in gate_levels if x == "warn")
    badges[3]["ok"] = (n_block == 0 and n_warn == 0)
    badges[3]["label"] = ("数据质量闸门 V4：通过" if badges[3]["ok"] else
                          f"数据质量闸门 V4：{n_block} 项阻断 / {n_warn} 项告警")
    meta["data_gate"] = {"block": n_block, "warn": n_warn,
                         "ok": len(gate_levels) - n_block - n_warn,
                         "items": [{"name": s["name"], "code": s["code"],
                                    "level": (s.get("gate") or {}).get("level", "ok"),
                                    "flags": (s.get("gate") or {}).get("flags", [])} for s in etfs]}
    meta["badges"] = badges
    # 实际生效行情源统计（千问评审二轮D-11：数据源状态标识与实际一致）
    meta["src_stats"] = dict(SRC_STATS)

    # V4 组合配置体检（P0-4 一致性 + 集中度静态校验）
    try:
        _cfg_list = [dict(name=n, **c) for n, c in ETF_POOL.items()]
        meta["portfolio"] = decision_engine.portfolio_check(
            _cfg_list, [s.get("decision") for s in etfs])
    except Exception as e:
        log(f"组合配置体检失败: {e}")
        meta["portfolio"] = {"ok": False, "flags": [{"level": "warn", "label": f"组合体检异常: {e}"}],
                             "note": "组合体检未完成"}

    # V4 P0-3 数据口径元数据：每项数据都登记 来源 / 字段类型 / 计算口径 / 统计期间 / 最新日期
    try:
        prov = []
        for s in etfs:
            prov.append({
                "item": f"{s['name']} 日线行情",
                "source": "东方财富(qfq) 优先，失败自动降级 腾讯/新浪(不复权)",
                "field": "收盘价（东财为前复权；腾讯/新浪为不复权原始成交价）",
                "calc": "技术指标与回测均在同一段序列内计算；跨源拼接存在复权口径差异，历史点位不可跨源直接对比",
                "period": f"{s.get('price_start', '--')} ~ {s.get('latest_date', '--')}（{s.get('price_rows', 0)} 条）",
                "date": s.get("latest_date"),
            })
            prov.append({
                "item": f"{s['name']} 官方 PE-TTM",
                "source": "中证指数官网 / 国证指数官网（指数口径）",
                "field": "指数 PE-TTM 官方发布值（非 ETF 基金口径，非估算）",
                "calc": f"分位 = 该指数官方 PE 历史中的百分位（累计 {s.get('official_pe_count', 0)} 条）",
                "period": f"累计库至 {s.get('pe_last_date') or '--'}",
                "date": s.get("pe_last_date"),
            })
        prov.append({
            "item": "股息率 · 10Y国债利差",
            "source": "中证官方股息率 XLS + 英为财情中国 10Y 国债收益率",
            "field": "股息率(近12月) 与无风险利率之差",
            "calc": "利差 > 0 表示股息仍高于无风险利率（红利/银行/煤炭/红利低波适用）",
            "period": "按指数股息率最新一期 XLS",
            "date": meta.get("data_date"),
        })
        prov.append({
            "item": "宏观环境（利率/政策/情绪）",
            "source": "英为财情（10Y国债）、央行（LPR，akshare 抓取）、腾讯指数（两市成交额）",
            "field": "国债收益率(%)、LPR(%)、两市成交额(千元→万亿元)",
            "calc": "每项带更新状态 ok/stale/fail；stale 或 fail 不参与环境判断（阈值见宏观卡）",
            "period": "国债近120天 / LPR 最新一期 / 成交额最新交易日",
            "date": meta.get("data_date"),
        })
        prov.append({
            "item": "回测（backtest.py V4）",
            "source": "本地/云端 output/price_history_<code>.csv",
            "field": "日收盘价（口径同上述行情源，东财段 qfq、腾讯/新浪段不复权）",
            "calc": "价格回报口径，未计分红再投资；成本 = (fee+slippage)×|仓位变化|，买卖双边计入；异常收益(|日涨跌|>11%)单独标记",
            "period": "各标的上巿以来（159201 仅 2025-02 起真实交易史）",
            "date": "--",
        })
        prov.append({
            "item": "159201 三种历史（务必区分）",
            "source": "ETF 行情 / 国证 980092 指数 / 策略规则回放",
            "field": "ETF 真实交易史 ≠ 跟踪指数史 ≠ 规则回测史",
            "calc": "ETF 实际交易史自 2025-02 上市；980092 指数史仅用于 PE 分位与估值；规则回测是在 ETF 真实价格上按策略规则回放，非指数回测",
            "period": "ETF 交易史 2025-02 起 / 指数史更长",
            "date": "--",
        })
        meta["provenance"] = prov
    except Exception as e:
        log(f"数据口径元数据构建失败: {e}")
        meta["provenance"] = []

    # ---- 组合相关性（近250交易日日收益两两相关系数）----
    corr = {}
    if len(price_rets) >= 2:
        try:
            ret_df = pd.DataFrame(price_rets).dropna()
            c = ret_df.corr()
            names = list(price_rets.keys())
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    corr[f"{names[i]}×{names[j]}"] = round(float(c.iloc[i, j]), 3)
            corr["_n"] = int(len(ret_df))
            log(f"组合相关性(近{len(ret_df)}个交易日日收益): {corr}")
        except Exception as e:
            log(f"组合相关性计算失败: {e}")

    if not etfs:
        log("所有标的数据缺失，本次不生成新看板")
        if cloud_mode:
            path = render_fallback(meta)
            log(f"云端容错: 保留/生成占位看板 {path}")
            return 0
        return 1

    path = render_dashboard(meta, ratio, etfs, macro=macro, corr=corr)
    if path is None:
        return 1
    log(f"看板已生成: {path}")
    if "--open" in sys.argv:
        import webbrowser
        webbrowser.open(Path(path).as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
