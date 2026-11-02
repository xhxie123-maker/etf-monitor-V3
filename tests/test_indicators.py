import unittest
import numpy as np
import pandas as pd
import sys, types
sys.modules.setdefault("akshare", types.SimpleNamespace())
import etf_monitor as em

class IndicatorTests(unittest.TestCase):
    def test_normalize_daily_sorts_deduplicates(self):
        raw = pd.DataFrame({"日期":["2024-01-02","2024-01-01","2024-01-02"],
                            "收盘":[2,1,2.1]})
        got = em.normalize_daily(raw, "test")
        self.assertEqual(len(got), 2)
        self.assertEqual(got.iloc[-1]["close"], 2.1)

    def test_rsi_bounds_and_warmup(self):
        prices = pd.DataFrame({"close": np.arange(1, 80, dtype=float)})
        got = em.calc_indicators(prices)
        self.assertTrue(pd.isna(got.loc[13, "rsi"]))
        self.assertAlmostEqual(got.loc[14, "rsi"], 100.0)

    def test_pe_window_requires_coverage(self):
        s = pd.Series(np.linspace(10, 20, 300),
                      index=pd.date_range("2024-01-01", periods=300))
        got = em.calc_pe_windows(s)
        self.assertIsNotNone(got["近1年"])
        self.assertIsNone(got["近3年"])
        self.assertIsNone(got["近5年"])

    def test_patch_dy_fills_existing_rows_without_overwriting_pe(self):
        hist = pd.DataFrame({
            "date": pd.to_datetime(["2026-10-29", "2026-10-30"]),
            "index_code": ["000922", "000922"],
            "index_name": ["中证红利", "中证红利"],
            "pe": [9.0, 9.22], "pb": [np.nan, np.nan],
            "dy": [np.nan, np.nan], "source": ["csindex", "csindex"]})
        dy = pd.DataFrame({"date": pd.to_datetime(["2026-10-30"]), "dy": [5.8]})
        got = em.patch_dy(hist, "000922", "中证红利", dy)
        # 命中行：dy 被填充，pe 保持原值
        row = got[got["date"] == pd.Timestamp("2026-10-30")].iloc[0]
        self.assertEqual(row["pe"], 9.22)
        self.assertEqual(row["dy"], 5.8)
        # 未命中日期不新增行
        self.assertEqual(len(got), 2)

    def test_patch_dy_does_not_insert_new_rows(self):
        hist = pd.DataFrame({
            "date": pd.to_datetime(["2026-10-30"]),
            "index_code": ["000922"], "index_name": ["中证红利"],
            "pe": [9.22], "pb": [np.nan], "dy": [np.nan], "source": ["csindex"]})
        dy = pd.DataFrame({"date": pd.to_datetime(["2026-10-31"]), "dy": [5.9]})
        got = em.patch_dy(hist, "000922", "中证红利", dy)
        self.assertEqual(len(got), 1)
        self.assertTrue(pd.isna(got.iloc[0]["dy"]))

if __name__ == "__main__":
    unittest.main()
