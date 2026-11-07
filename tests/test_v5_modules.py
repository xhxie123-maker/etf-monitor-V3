# -*- coding: utf-8 -*-
"""
V5 分层模块的契约测试（unittest，可在本地或 CI 直接跑）。

覆盖 PRD 验收第 3/4/6/7/9/10 项的机器可验证部分：
- 五维权重和为 1、可用维度归一化不记零分（factor_engine）
- 风险预算权重 + 整手计划（Q = floor(B/(P×L))×L）（position_sizing）
- 四级风控返回结构 + 熔断档位（risk_control）
- 决策中心未确认风险偏好时只出「仅观察」（PRD 15.4）
- 数据库 20 张表与 schema 版本（smart_db）
- 13 个 API 端点契约与已落盘快照（api_layer）
- 回测防前视校验可复现（backtest）

运行：python -m unittest discover -s tests -v
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import backtest as bt          # noqa: E402
import decision_center as dc  # noqa: E402
import factor_engine as fe     # noqa: E402
import position_sizing as ps   # noqa: E402
import risk_control as rc      # noqa: E402
import smart_db as sdb         # noqa: E402
import api_layer as api        # noqa: E402


def _series(n=260, start=2.0, drift=0.002):
    import numpy as np
    import pandas as pd
    prices = [start * (1.0 + drift) ** i for i in range(n)]
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    return pd.DataFrame({"date": dates, "close": prices})


class TestFactorEngine(unittest.TestCase):
    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(fe.WEIGHTS.values()), 1.0, places=9)

    def test_missing_dimension_not_zeroed(self):
        df = _series(260, drift=0.004)
        r = fe.composite("core_equity", df)  # 无 PE/流动性/价差输入
        self.assertIn("total", r)
        self.assertGreaterEqual(r["total"], 0.0)
        self.assertTrue(r["missing"])
        self.assertEqual(set(r["w_used"].keys()), set(r["sub"].keys()) - set(r["missing"]))

    def test_money_etf_valuation_not_applicable(self):
        df = _series(260, drift=0.0004)
        r = fe.composite("defensive", df, is_money=True)
        self.assertEqual(r["sub"]["valuation"], 60)

    def test_signal_band_monotonic(self):
        self.assertEqual(fe.signal_band(85)["label"], "优先关注")
        self.assertEqual(fe.signal_band(75)["label"], "买入观察")
        self.assertEqual(fe.signal_band(60)["label"], "持有")
        self.assertEqual(fe.signal_band(40)["label"], "谨慎持有")
        self.assertEqual(fe.signal_band(20)["label"], "减仓观察")


class TestPositionSizing(unittest.TestCase):
    def test_budget_weights_sum(self):
        items = [{"code": "510300", "base_w": 20}, {"code": "511010", "base_w": 15}]
        sigma = {"510300": 18.0, "511010": 2.0}
        r = ps.risk_budget_weights(items, sigma, target_total_pct=57.0)
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(sum(r["weights"].values()), 57.0, places=1)
        self.assertAlmostEqual(r["cash_reserve_pct"], 43.0, places=1)

    def test_lot_formula(self):
        plan = ps.position_plan({"510300": 10.0}, 1_000_000, {"510300": 4.0},
                                {"510300": {"lot": 100, "min": 100, "step": 100, "tick": 0.001}},
                                fee=0.0002, slippage=0.0005)
        self.assertTrue(plan["ok"])
        for o in plan["plan"]:
            if o["qty"] is None:
                continue
            self.assertEqual(o["qty"] % 100, 0)
            # 预算内：数量 × 全成本 ≤ 预算
            budget = 1_000_000 * 0.10
            self.assertLessEqual(o["qty"] * o["price"], budget + 1e-6)

    def test_no_capital_returns_reason(self):
        plan = ps.position_plan({"510300": 10.0}, None, {"510300": 4.0}, {}, 0.0002, 0.0005, 5)
        self.assertFalse(plan["ok"])
        self.assertNotIn("plan", plan)


class TestRiskControl(unittest.TestCase):
    def test_structure(self):
        import numpy as np
        import pandas as pd
        idx = pd.date_range("2025-01-01", periods=300, freq="B")
        rets = pd.DataFrame({"510300": np.random.RandomState(7).normal(0, 0.012, 300),
                             "511010": np.random.RandomState(9).normal(0, 0.002, 300)},
                            index=idx)
        r = rc.evaluate(rets, {"510300": 30.0, "511010": 20.0},
                        [{"code": "510300"}, {"code": "511010"}],
                        {"510300": "core_equity", "511010": "defensive"})
        self.assertTrue(r["ok"])
        for k in ("volatility_pct", "max_drawdown_pct", "var_95_daily_pct",
                  "cvar_95_daily_pct", "concentration", "risk_level", "circuit_breaker",
                  "portfolio_checks", "overall_state"):
            self.assertIn(k, r)
        self.assertLessEqual(r["max_drawdown_pct"], 0.0)


class TestDecisionCenter(unittest.TestCase):
    def test_unconfigured_profile_only_observe(self):
        rows = [{"code": "510300", "name": "沪深300ETF", "asset_class": "core_equity",
                 "quality": "合格", "price": 4.0, "strategic_w_pct": 20.0,
                 "score_row": {"total": 60.0, "signal": fe.signal_band(60)}}]
        out = dc.build_plan(rows, {"ok": False, "reason": "未提供资金规模"},
                            {"trade_date": "2026-11-06",
                             "risk_tolerance_configured": False,
                             "regime": {"regime": "防守", "label": "防守", "risk_multiplier": 0.6},
                             "breaker": {"level": "green", "label": "正常"},
                             "holdings": {}})
        self.assertFalse(out["risk_tolerance_configured"])
        self.assertEqual(out["items"][0]["action"], "仅观察（未确认投资期限与最大回撤）")
        self.assertIsNone(out["items"][0]["target_weight_pct"])

    def test_decision_field_count(self):
        rows = [{"code": "510300", "name": "沪深300ETF", "asset_class": "core_equity",
                 "quality": "合格", "price": 4.0, "strategic_w_pct": 20.0,
                 "score_row": {"total": 60.0, "signal": fe.signal_band(60)}}]
        out = dc.build_plan(rows, {"ok": False, "reason": "未提供资金规模"},
                            {"trade_date": "2026-11-06",
                             "risk_tolerance_configured": False,
                             "regime": {"regime": "防守", "label": "防守", "risk_multiplier": 0.6},
                             "breaker": {"level": "green", "label": "正常"},
                             "holdings": {}})
        item = out["items"][0]
        for k in ("recommendation_id", "etf_code", "action", "reasons", "gates",
                  "valid_until", "review_status", "confidence"):
            self.assertIn(k, item)


class TestDatabase(unittest.TestCase):
    def test_schema_tables(self):
        path = os.path.join(ROOT, "output", "smart_etf.db")
        if not os.path.exists(path):
            self.skipTest("smart_etf.db 未生成")
        conn = sdb.connect(path)
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = [r[0] for r in rows if not r[0].startswith("sqlite_")]
        self.assertGreaterEqual(len(names), 20)
        counts = sdb.counts(conn)
        self.assertGreater(counts.get("indicator_daily", 0), 1000)
        self.assertGreater(counts.get("etf_daily_bar", 0), 1000)
        conn.close()


class TestApiContract(unittest.TestCase):
    def test_thirteen_endpoints(self):
        self.assertEqual(len(api.ENDPOINTS), 13)
        methods = [e["method"] for e in api.ENDPOINTS]
        self.assertEqual(methods.count("GET"), 10)
        self.assertEqual(methods.count("POST"), 3)
        for e in api.ENDPOINTS:
            self.assertTrue(e["path"].startswith("/api/v1"))
            self.assertTrue(e["file"].endswith(".json"))
            self.assertTrue(e["key"])

    def test_snapshot_envelope(self):
        path = os.path.join(ROOT, "output", "api", "v1", "market_status.json")
        if not os.path.exists(path):
            self.skipTest("API 快照未生成")
        snap = json.load(open(path, encoding="utf-8"))
        for k in ("data_date", "updated_at", "sources", "model_versions",
                  "api_version", "disclaimer", "data"):
            self.assertIn(k, snap)
        self.assertTrue(snap["disclaimer"])


class TestBacktestHonesty(unittest.TestCase):
    def test_no_lookahead_on_synthetic_uptrend(self):
        df = _series(400, drift=0.003)
        la = bt.no_lookahead_check(df)
        self.assertTrue(la["ok"])
        self.assertEqual(la["mismatches"], 0)
        self.assertGreater(la["checked"], 200)

    def test_metrics_present(self):
        df = _series(400, drift=0.003)
        r = bt.run_backtest(df, fee=0.0002, slippage=0.0005)
        for k in ("strategy_ann", "buyhold_ann", "strategy_mdd", "buyhold_mdd",
                  "sharpe", "sortino", "calmar", "cvar95", "turnover_per_year",
                  "cost_total_pct", "recovery", "lookahead"):
            self.assertIn(k, r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
