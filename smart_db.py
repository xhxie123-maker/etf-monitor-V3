# -*- coding: utf-8 -*-
"""
V5.0 数据层（smart_db）
=======================
按 Smart ETF Quant V4.0 PRD 第 5 章建立 20 张表的 SQLite 实现。

为什么是 SQLite 而不是 PostgreSQL：
  PRD 写「初期采用 PostgreSQL」，但本系统实际运行在 GitHub Pages（纯静态）
  + GitHub Actions（定时任务、无常驻进程、无公网数据库）上，没有可托管的
  PostgreSQL 实例。SQLite 是同一套关系模型、同一套 SQL，单文件零运维，
  能在 Actions 内直接读写，是当前部署形态下唯一可落地的关系库。
  迁移到 PostgreSQL 时只需替换连接串与少量 DDL 方言，表结构不变。

金额字段一律用「分」为单位的整数（*_cents），符合 PRD「金额使用 Decimal 或定点数，
避免浮点精度误差」的要求。

本模块只做存取，不做策略判断。所有输出均为量化参考，不构成投资建议。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime

import pandas as pd

DB_VERSION = "smart-etf-quant-v5"
SCHEMA_VERSION = "schema-v5-2026-11-07"

DDL = [
    # 1 ETF 基础资料
    """CREATE TABLE IF NOT EXISTS etf_basic (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE, name TEXT, exchange TEXT, category TEXT,
        asset_class TEXT, index_code TEXT, benchmark_name TEXT, listing_date TEXT,
        manager TEXT, management_fee REAL, custody_fee REAL, tracking_type TEXT,
        status TEXT DEFAULT 'listed', updated_at TEXT
    )""",
    # 2 ETF 日线（原始与复权分开存）
    """CREATE TABLE IF NOT EXISTS etf_daily_bar (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        etf_code TEXT NOT NULL, trade_date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, pre_close REAL,
        volume REAL, amount REAL, adj_factor REAL, adj_close REAL,
        price_basis TEXT, source_id TEXT, collected_at TEXT,
        UNIQUE(etf_code, trade_date)
    )""",
    # 3 定期基本面快照
    """CREATE TABLE IF NOT EXISTS etf_profile_snapshot (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        etf_code TEXT NOT NULL, report_date TEXT NOT NULL,
        nav REAL, total_assets_cents INTEGER, fund_shares REAL,
        tracking_error REAL, expense_ratio REAL, benchmark TEXT,
        source_date TEXT, published_at TEXT,
        UNIQUE(etf_code, report_date)
    )""",
    # 4 估值
    """CREATE TABLE IF NOT EXISTS etf_valuation (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        etf_code TEXT NOT NULL, trade_date TEXT NOT NULL,
        pe REAL, pb REAL, dividend_yield REAL,
        pe_percentile REAL, pb_percentile REAL,
        lookback_period TEXT, data_quality TEXT, valuation_basis TEXT,
        UNIQUE(etf_code, trade_date)
    )""",
    # 5 份额与资金流（区分份额变化与成交资金流）
    """CREATE TABLE IF NOT EXISTS etf_flow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        etf_code TEXT NOT NULL, trade_date TEXT NOT NULL,
        shares_outstanding REAL, shares_change REAL, shares_change_pct REAL,
        estimated_net_flow_cents INTEGER, amount_cents INTEGER,
        source_type TEXT, quality_flag TEXT,
        UNIQUE(etf_code, trade_date)
    )""",
    # 6 交易规则（按生效日期管理）
    """CREATE TABLE IF NOT EXISTS instrument_rule (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        etf_code TEXT NOT NULL, exchange TEXT,
        effective_from TEXT, effective_to TEXT,
        settlement_type TEXT, min_order_qty INTEGER, order_step INTEGER,
        price_tick REAL, price_limit_rule TEXT, trading_sessions TEXT,
        after_hours_fixed_price_allowed INTEGER,
        source_url TEXT, verified INTEGER DEFAULT 0, verified_at TEXT,
        rule_version TEXT,
        UNIQUE(etf_code, effective_from)
    )""",
    # 7 交易日历
    """CREATE TABLE IF NOT EXISTS trading_calendar (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market TEXT NOT NULL, trade_date TEXT NOT NULL,
        is_open INTEGER, open_time TEXT, close_time TEXT,
        session_type TEXT, source_id TEXT,
        UNIQUE(market, trade_date)
    )""",
    # 8 指标
    """CREATE TABLE IF NOT EXISTS indicator_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        etf_code TEXT NOT NULL, trade_date TEXT NOT NULL,
        ma20 REAL, ma60 REAL, ma120 REAL, ma250 REAL,
        momentum20 REAL, momentum60 REAL, momentum120 REAL,
        volatility20 REAL, volatility60 REAL, max_drawdown60 REAL,
        adx REAL, rsi REAL, atr REAL,
        calculated_at TEXT, model_version TEXT,
        UNIQUE(etf_code, trade_date)
    )""",
    # 9 市场状态
    """CREATE TABLE IF NOT EXISTS market_regime (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date TEXT NOT NULL, index_code TEXT,
        trend_score REAL, breadth_score REAL, volatility_score REAL,
        regime TEXT, risk_multiplier REAL, reason_json TEXT, model_version TEXT,
        UNIQUE(trade_date, index_code)
    )""",
    # 10 策略定义
    """CREATE TABLE IF NOT EXISTS strategy_definition (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id TEXT NOT NULL UNIQUE, strategy_name TEXT, strategy_type TEXT,
        description TEXT, active_version TEXT, enabled INTEGER DEFAULT 1
    )""",
    # 11 策略版本
    """CREATE TABLE IF NOT EXISTS strategy_version (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id TEXT NOT NULL UNIQUE, strategy_id TEXT NOT NULL,
        parameters_json TEXT, parameter_hash TEXT,
        training_start TEXT, training_end TEXT,
        validation_start TEXT, validation_end TEXT,
        created_at TEXT, approval_status TEXT
    )""",
    # 12 策略信号
    """CREATE TABLE IF NOT EXISTS etf_signal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id TEXT, etf_code TEXT NOT NULL, signal_date TEXT NOT NULL,
        strategy_id TEXT, version_id TEXT,
        quality_score REAL, trend_score REAL, momentum_score REAL,
        valuation_score REAL, liquidity_score REAL, risk_score REAL,
        total_score REAL, signal_type TEXT, confidence TEXT,
        explanation_json TEXT, data_snapshot_json TEXT, valid_until TEXT,
        UNIQUE(etf_code, signal_date, strategy_id)
    )""",
    # 13 组合
    """CREATE TABLE IF NOT EXISTS portfolio (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        portfolio_id TEXT NOT NULL UNIQUE, portfolio_name TEXT,
        initial_capital_cents INTEGER, current_cash_cents INTEGER,
        currency TEXT DEFAULT 'CNY', benchmark_id TEXT, risk_profile TEXT,
        max_drawdown_limit_pct REAL, created_at TEXT, updated_at TEXT
    )""",
    # 14 持仓
    """CREATE TABLE IF NOT EXISTS portfolio_position (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        portfolio_id TEXT NOT NULL, etf_code TEXT NOT NULL,
        quantity INTEGER, average_cost_cents INTEGER, market_value_cents INTEGER,
        current_weight_pct REAL, target_weight_pct REAL, updated_at TEXT,
        UNIQUE(portfolio_id, etf_code)
    )""",
    # 15 人工交易记录
    """CREATE TABLE IF NOT EXISTS trade_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id TEXT, portfolio_id TEXT NOT NULL, etf_code TEXT NOT NULL,
        trade_date TEXT NOT NULL, direction TEXT, quantity INTEGER,
        price_cents INTEGER, commission_cents INTEGER,
        estimated_slippage_cents INTEGER, actual_slippage_cents INTEGER, note TEXT
    )""",
    # 16 交易计划
    """CREATE TABLE IF NOT EXISTS recommendation (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recommendation_id TEXT UNIQUE, portfolio_id TEXT,
        etf_code TEXT NOT NULL, signal_id TEXT, action TEXT,
        current_weight_pct REAL, target_weight_pct REAL,
        suggested_quantity INTEGER, estimated_cost_cents INTEGER,
        trade_cost_cents INTEGER, risk_warning TEXT, reason_json TEXT,
        review_status TEXT DEFAULT 'pending', review_note TEXT,
        created_at TEXT, expired_at TEXT
    )""",
    # 17 组合每日净值
    """CREATE TABLE IF NOT EXISTS portfolio_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        portfolio_id TEXT NOT NULL, trade_date TEXT NOT NULL,
        total_assets_cents INTEGER, cash_cents INTEGER, market_value_cents INTEGER,
        daily_return_pct REAL, cumulative_return_pct REAL, drawdown_pct REAL,
        annualized_volatility_pct REAL,
        UNIQUE(portfolio_id, trade_date)
    )""",
    # 18 回测记录
    """CREATE TABLE IF NOT EXISTS backtest_run (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        backtest_id TEXT UNIQUE, strategy_id TEXT, version_id TEXT,
        start_date TEXT, end_date TEXT,
        initial_capital_cents INTEGER, final_assets_cents INTEGER,
        cagr_pct REAL, max_drawdown_pct REAL, sharpe REAL, sortino REAL,
        calmar REAL, turnover REAL, transaction_cost_cents INTEGER,
        benchmark_return_pct REAL, out_of_sample_cagr_pct REAL,
        recovery_days INTEGER, run_status TEXT, run_at TEXT
    )""",
    # 19 数据采集日志
    """CREATE TABLE IF NOT EXISTS data_ingestion_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT, data_type TEXT, start_time TEXT, end_time TEXT,
        requested_rows INTEGER, successful_rows INTEGER, failed_rows INTEGER,
        status TEXT, error_message TEXT
    )""",
    # 20 风险快照
    """CREATE TABLE IF NOT EXISTS risk_snapshot (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        portfolio_id TEXT, trade_date TEXT NOT NULL,
        volatility_pct REAL, max_drawdown_pct REAL,
        var_95_pct REAL, cvar_95_pct REAL,
        concentration REAL, correlation_risk REAL,
        risk_level TEXT, risk_multiplier REAL, circuit_breaker TEXT,
        warnings_json TEXT,
        UNIQUE(portfolio_id, trade_date)
    )""",
]

INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_bar_code_date ON etf_daily_bar(etf_code, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_val_code_date ON etf_valuation(etf_code, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_ind_code_date ON indicator_daily(etf_code, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_sig_code_date ON etf_signal(etf_code, signal_date)",
    "CREATE INDEX IF NOT EXISTS idx_rec_status ON recommendation(review_status)",
    "CREATE INDEX IF NOT EXISTS idx_risk_date ON risk_snapshot(trade_date)",
]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str) -> sqlite3.Connection:
    conn = connect(path)
    for ddl in DDL:
        conn.execute(ddl)
    for idx in INDEX_DDL:
        conn.execute(idx)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _iso(d) -> str:
    if d is None:
        return ""
    try:
        return str(pd.to_datetime(d).date())
    except Exception:
        return str(d)


def sync_etf_basic(conn, cfg: dict, exchange: str = "SH") -> int:
    row = {
        "code": cfg["code"], "name": cfg.get("name"), "exchange": exchange,
        "category": cfg.get("asset_class"), "asset_class": cfg.get("asset_class"),
        "index_code": cfg.get("index_code"), "benchmark_name": cfg.get("index_name"),
        "listing_date": cfg.get("listing_date"), "manager": cfg.get("manager"),
        "management_fee": cfg.get("expense_ratio"), "custody_fee": None,
        "tracking_type": None, "status": "listed", "updated_at": _now(),
    }
    conn.execute(
        """INSERT INTO etf_basic (code,name,exchange,category,asset_class,index_code,
           benchmark_name,listing_date,manager,management_fee,custody_fee,tracking_type,status,updated_at)
           VALUES (:code,:name,:exchange,:category,:asset_class,:index_code,:benchmark_name,
           :listing_date,:manager,:management_fee,:custody_fee,:tracking_type,:status,:updated_at)
           ON CONFLICT(code) DO UPDATE SET name=excluded.name, exchange=excluded.exchange,
           category=excluded.category, asset_class=excluded.asset_class,
           index_code=excluded.index_code, benchmark_name=excluded.benchmark_name,
           management_fee=excluded.management_fee, status=excluded.status,
           updated_at=excluded.updated_at""", row)
    return 1


def sync_daily_bars(conn, code: str, df: pd.DataFrame, source: str = "em_qfq",
                    price_basis: str = "qfq") -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "etf_code": code, "trade_date": _iso(r.get("date")),
            "open": _f(r.get("open")), "high": _f(r.get("high")), "low": _f(r.get("low")),
            "close": _f(r.get("close")), "pre_close": None,
            "volume": _f(r.get("volume")), "amount": _f(r.get("amount")),
            "adj_factor": 1.0, "adj_close": _f(r.get("close")),
            "price_basis": price_basis, "source_id": source, "collected_at": _now(),
        })
    conn.executemany(
        """INSERT INTO etf_daily_bar (etf_code,trade_date,open,high,low,close,pre_close,
           volume,amount,adj_factor,adj_close,price_basis,source_id,collected_at)
           VALUES (:etf_code,:trade_date,:open,:high,:low,:close,:pre_close,:volume,:amount,
           :adj_factor,:adj_close,:price_basis,:source_id,:collected_at)
           ON CONFLICT(etf_code,trade_date) DO UPDATE SET close=excluded.close,
           high=excluded.high, low=excluded.low, open=excluded.open,
           amount=excluded.amount, adjusted_at=excluded.collected_at""".replace(
            "adjusted_at", "collected_at"), rows)
    return len(rows)


def sync_valuation(conn, code: str, trade_date, pe=None, pb=None, dy=None,
                   pe_pct=None, lookback="近3年", quality="ok", basis="official_index") -> int:
    conn.execute(
        """INSERT INTO etf_valuation (etf_code,trade_date,pe,pb,dividend_yield,
           pe_percentile,pb_percentile,lookback_period,data_quality,valuation_basis)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(etf_code,trade_date) DO UPDATE SET pe=excluded.pe, pb=excluded.pb,
           dividend_yield=excluded.dividend_yield, pe_percentile=excluded.pe_percentile,
           data_quality=excluded.data_quality""",
        (code, _iso(trade_date), _f(pe), _f(pb), _f(dy), _f(pe_pct), None,
         lookback, quality, basis))
    return 1


def sync_indicator(conn, code: str, trade_date, ind: dict, model_version: str) -> int:
    conn.execute(
        """INSERT INTO indicator_daily (etf_code,trade_date,ma20,ma60,ma120,ma250,
           momentum20,momentum60,momentum120,volatility20,volatility60,max_drawdown60,
           adx,rsi,atr,calculated_at,model_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(etf_code,trade_date) DO UPDATE SET ma20=excluded.ma20,ma60=excluded.ma60,
           ma120=excluded.ma120,ma250=excluded.ma250,momentum20=excluded.momentum20,
           momentum60=excluded.momentum60,momentum120=excluded.momentum120,
           volatility20=excluded.volatility20,volatility60=excluded.volatility60,
           max_drawdown60=excluded.max_drawdown60,adx=excluded.adx,rsi=excluded.rsi,
           atr=excluded.atr,model_version=excluded.model_version""",
        (code, _iso(trade_date), _f(ind.get("ma20")), _f(ind.get("ma60")),
         _f(ind.get("ma120")), _f(ind.get("ma250")), _f(ind.get("momentum20")),
         _f(ind.get("momentum60")), _f(ind.get("momentum120")), _f(ind.get("volatility20")),
         _f(ind.get("volatility60")), _f(ind.get("max_drawdown60")), _f(ind.get("adx")),
         _f(ind.get("rsi")), _f(ind.get("atr")), _now(), model_version))
    return 1


def sync_signal(conn, row: dict) -> int:
    row = dict(row)
    row.setdefault("signal_id", f"{row['etf_code']}-{_iso(row['signal_date'])}-{row.get('strategy_id','s5')}")
    row.setdefault("signal_date", _iso(row.get("signal_date")))
    row.setdefault("data_snapshot_json", None)
    cols = ["signal_id", "etf_code", "signal_date", "strategy_id", "version_id",
            "quality_score", "trend_score", "momentum_score", "valuation_score",
            "liquidity_score", "risk_score", "total_score", "signal_type",
            "confidence", "explanation_json", "data_snapshot_json", "valid_until"]
    vals = [row.get(c) for c in cols]
    conn.execute(
        f"INSERT INTO etf_signal ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))}) "
        "ON CONFLICT(etf_code,signal_date,strategy_id) DO UPDATE SET "
        "total_score=excluded.total_score, signal_type=excluded.signal_type, "
        "confidence=excluded.confidence, explanation_json=excluded.explanation_json, "
        "valid_until=excluded.valid_until", vals)
    return 1


def sync_market_regime(conn, trade_date, index_code, regime: dict) -> int:
    conn.execute(
        """INSERT INTO market_regime (trade_date,index_code,trend_score,breadth_score,
           volatility_score,regime,risk_multiplier,reason_json,model_version)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(trade_date,index_code) DO UPDATE SET regime=excluded.regime,
           risk_multiplier=excluded.risk_multiplier, reason_json=excluded.reason_json""",
        (_iso(trade_date), index_code or "000300", _f(regime.get("trend_score")),
         _f(regime.get("breadth_score")), _f(regime.get("volatility_score")),
         regime.get("regime"), _f(regime.get("risk_multiplier")),
         __import__("json").dumps(regime.get("reasons") or [], ensure_ascii=False),
         regime.get("model_version")))
    return 1


def sync_risk_snapshot(conn, portfolio_id: str, snap: dict) -> int:
    import json
    conn.execute(
        """INSERT INTO risk_snapshot (portfolio_id,trade_date,volatility_pct,max_drawdown_pct,
           var_95_pct,cvar_95_pct,concentration,correlation_risk,risk_level,risk_multiplier,
           circuit_breaker,warnings_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(portfolio_id,trade_date) DO UPDATE SET volatility_pct=excluded.volatility_pct,
           max_drawdown_pct=excluded.max_drawdown_pct, var_95_pct=excluded.var_95_pct,
           cvar_95_pct=excluded.cvar_95_pct, risk_level=excluded.risk_level,
           risk_multiplier=excluded.risk_multiplier, circuit_breaker=excluded.circuit_breaker,
           warnings_json=excluded.warnings_json""",
        (portfolio_id, _iso(snap.get("trade_date")),
         _f(snap.get("volatility_pct")), _f(snap.get("max_drawdown_pct")),
         _f(snap.get("var_95_daily_pct")), _f(snap.get("cvar_95_daily_pct")),
         _f((snap.get("concentration") or {}).get("hhi")),
         _f((snap.get("correlation_risk") or {}).get("max_corr")),
         (snap.get("risk_level") or {}).get("state"),
         _f((snap.get("risk_level") or {}).get("multiplier")),
         (snap.get("circuit_breaker") or {}).get("level"),
         json.dumps(snap.get("warnings") or [], ensure_ascii=False)))
    return 1


def sync_instrument_rules(conn, rules: list) -> int:
    n = 0
    for r in rules:
        if not r.get("available"):
            continue
        conn.execute(
            """INSERT INTO instrument_rule (etf_code,exchange,effective_from,effective_to,
               settlement_type,min_order_qty,order_step,price_tick,price_limit_rule,
               trading_sessions,after_hours_fixed_price_allowed,source_url,verified,
               verified_at,rule_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(etf_code,effective_from) DO UPDATE SET settlement_type=excluded.settlement_type,
               min_order_qty=excluded.min_order_qty, order_step=excluded.order_step,
               price_tick=excluded.price_tick, price_limit_rule=excluded.price_limit_rule,
               trading_sessions=excluded.trading_sessions,
               after_hours_fixed_price_allowed=excluded.after_hours_fixed_price_allowed,
               verified=excluded.verified, rule_version=excluded.rule_version""",
            (r["etf_code"], r.get("exchange"), r.get("effective_from"), r.get("effective_to"),
             r.get("settlement_type"), r.get("min_order_qty"), r.get("order_step"),
             r.get("price_tick"), r.get("price_limit_rule"),
             "|".join(r.get("trading_sessions") or []),
             1 if r.get("after_hours_fixed_price_allowed") else 0,
             r.get("source_url"), 1 if r.get("verified") else 0, r.get("verified_at"),
             r.get("rule_version")))
        n += 1
    return n


def sync_recommendation(conn, rec: dict) -> int:
    import json
    conn.execute(
        """INSERT INTO recommendation (recommendation_id,portfolio_id,etf_code,signal_id,
           action,current_weight_pct,target_weight_pct,suggested_quantity,estimated_cost_cents,
           trade_cost_cents,risk_warning,reason_json,review_status,created_at,expired_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(recommendation_id) DO UPDATE SET action=excluded.action,
           target_weight_pct=excluded.target_weight_pct, suggested_quantity=excluded.suggested_quantity,
           estimated_cost_cents=excluded.estimated_cost_cents, trade_cost_cents=excluded.trade_cost_cents,
           risk_warning=excluded.risk_warning, reason_json=excluded.reason_json,
           expired_at=excluded.expired_at""",
        (rec.get("recommendation_id"), rec.get("portfolio_id"), rec.get("etf_code"),
         rec.get("signal_id"), rec.get("action"), _f(rec.get("current_weight_pct")),
         _f(rec.get("target_weight_pct")), rec.get("suggested_quantity"),
         _cents(rec.get("estimated_cost")), _cents(rec.get("trade_cost")),
         rec.get("risk_warning"), json.dumps(rec.get("reasons") or [], ensure_ascii=False),
         rec.get("review_status", "pending"), _now(), rec.get("expired_at")))
    return 1


def sync_backtest(conn, bt: dict) -> int:
    conn.execute(
        """INSERT INTO backtest_run (backtest_id,strategy_id,version_id,start_date,end_date,
           initial_capital_cents,final_assets_cents,cagr_pct,max_drawdown_pct,sharpe,sortino,
           calmar,turnover,transaction_cost_cents,benchmark_return_pct,out_of_sample_cagr_pct,
           recovery_days,run_status,run_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(backtest_id) DO UPDATE SET cagr_pct=excluded.cagr_pct,
           max_drawdown_pct=excluded.max_drawdown_pct, sharpe=excluded.sharpe,
           sortino=excluded.sortino, calmar=excluded.calmar, turnover=excluded.turnover,
           run_status=excluded.run_status, run_at=excluded.run_at""",
        (bt.get("backtest_id"), bt.get("strategy_id"), bt.get("version_id"),
         bt.get("start_date"), bt.get("end_date"),
         _cents(bt.get("initial_capital")), _cents(bt.get("final_assets")),
         _f(bt.get("cagr")), _f(bt.get("max_drawdown")), _f(bt.get("sharpe")),
         _f(bt.get("sortino")), _f(bt.get("calmar")), _f(bt.get("turnover")),
         _cents(bt.get("transaction_cost")), _f(bt.get("benchmark_return")),
         _f(bt.get("out_of_sample_cagr")), bt.get("recovery_days"),
         bt.get("run_status", "ok"), _now()))
    return 1


def log_ingestion(conn, source_id, data_type, requested=0, success=0, failed=0,
                  status="ok", error=None, start_time=None) -> int:
    conn.execute(
        """INSERT INTO data_ingestion_log (source_id,data_type,start_time,end_time,
           requested_rows,successful_rows,failed_rows,status,error_message)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (source_id, data_type, start_time or _now(), _now(), requested, success,
         failed, status, error))
    return 1


def counts(conn) -> dict:
    out = {}
    for t in ("etf_basic", "etf_daily_bar", "etf_valuation", "instrument_rule",
              "indicator_daily", "etf_signal", "market_regime", "risk_snapshot",
              "recommendation", "backtest_run", "data_ingestion_log"):
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            out[t] = None
    return out


def data_health(conn) -> dict:
    """数据健康（PRD 第 12 章验收：数据日期、来源、更新时间可追溯）。"""
    c = counts(conn)
    last_bar = conn.execute("SELECT MAX(trade_date) FROM etf_daily_bar").fetchone()[0] \
        if c.get("etf_daily_bar") else None
    last_sig = conn.execute("SELECT MAX(signal_date) FROM etf_signal").fetchone()[0] \
        if c.get("etf_signal") else None
    failed = conn.execute("SELECT COUNT(*) FROM data_ingestion_log WHERE status<>'ok'").fetchone()[0] \
        if c.get("data_ingestion_log") else 0
    return {"ok": bool(c.get("etf_basic")) and failed == 0,
            "counts": c, "last_bar_date": last_bar, "last_signal_date": last_sig,
            "failed_ingestions": failed}
    # 注：失败采集只记录不阻断，是否阻断决策由 data_gate 决定


def _f(x):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (v != v) else v


def _cents(x):
    if x is None:
        return None
    try:
        return int(round(float(x) * 100))
    except (TypeError, ValueError):
        return None


def self_test() -> list:
    """自测：建表、写入、幂等 upsert、查询。"""
    import tempfile
    fails = []
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    conn = init_db(path)
    # 20 张表都存在
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in ("etf_basic", "etf_daily_bar", "etf_profile_snapshot", "etf_valuation",
              "etf_flow", "instrument_rule", "trading_calendar", "indicator_daily",
              "market_regime", "strategy_definition", "strategy_version", "etf_signal",
              "portfolio", "portfolio_position", "trade_ledger", "recommendation",
              "portfolio_daily", "backtest_run", "data_ingestion_log", "risk_snapshot"):
        if t not in tables:
            fails.append(f"缺表 {t}")
    if len([t for t in tables]) != 20:
        fails.append(f"表数量应为 20，实际 {len(tables)}")
    # 写入基础资料
    sync_etf_basic(conn, {"code": "510300", "name": "沪深300ETF", "asset_class": "core_equity",
                          "index_code": "000300", "expense_ratio": 0.6})
    # 日线幂等
    df = pd.DataFrame({"date": ["2026-11-05", "2026-11-06"], "open": [1.0, 1.1],
                       "high": [1.2, 1.3], "low": [0.9, 1.0], "close": [1.1, 1.2],
                       "volume": [1000, 2000], "amount": [1e6, 2e6]})
    sync_daily_bars(conn, "510300", df)
    sync_daily_bars(conn, "510300", df)
    n = conn.execute("SELECT COUNT(*) FROM etf_daily_bar").fetchone()[0]
    if n != 2:
        fails.append(f"日线应幂等写入 2 行，实际 {n}")
    # 规则
    import trading_rules as tr
    sync_instrument_rules(conn, tr.rules_table())
    rn = conn.execute("SELECT COUNT(*) FROM instrument_rule").fetchone()[0]
    if rn < 10:
        fails.append(f"交易规则应写入 ≥10 条，实际 {rn}")
    # 信号
    sync_signal(conn, {"etf_code": "510300", "signal_date": "2026-11-06",
                       "strategy_id": "s5", "version_id": "v5", "total_score": 72.5,
                       "signal_type": "买入观察", "trend_score": 80, "momentum_score": 70})
    sync_signal(conn, {"etf_code": "510300", "signal_date": "2026-11-06",
                       "strategy_id": "s5", "version_id": "v5", "total_score": 74.0,
                       "signal_type": "买入观察"})
    s = conn.execute("SELECT total_score FROM etf_signal").fetchone()[0]
    if abs(s - 74.0) > 1e-6:
        fails.append("信号 upsert 未覆盖为最新值")
    # 风险快照
    sync_risk_snapshot(conn, "P1", {"trade_date": "2026-11-06", "volatility_pct": 9.8,
                                    "max_drawdown_pct": -7.8, "var_95_daily_pct": -1.2,
                                    "cvar_95_daily_pct": -2.0,
                                    "concentration": {"hhi": 3350},
                                    "correlation_risk": {"max_corr": 0.71},
                                    "risk_level": {"state": "mid", "multiplier": 0.75},
                                    "circuit_breaker": {"level": "green"},
                                    "warnings": []})
    h = data_health(conn)
    if not h["counts"].get("etf_basic"):
        fails.append("健康检查未统计到基础表")
    conn.close()
    return fails


if __name__ == "__main__":
    import sys
    if "selftest" in sys.argv:
        f = self_test()
        print("smart_db self_test:", "PASS" if not f else "FAIL")
        for x in f:
            print(" -", x)
        sys.exit(0 if not f else 1)
    print(SCHEMA_VERSION, len(DDL), "tables")
