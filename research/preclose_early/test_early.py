"""Regressions for the early-history role adapter and IIS valuation horizon."""
from datetime import date, datetime, time
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import engine, exported_row, merge_aliases
from test_intraday import fixture


class EarlyTests(unittest.TestCase):
    def test_first_session_after_halt_uses_shortened_calendar(self):
        self.assertEqual(engine.schedule(date(2022, 3, 30)), (time(13, 50), time(13, 40)))
        self.assertEqual(engine.schedule(date(2022, 3, 31)), (time(18, 50), time(18, 40)))

    def test_same_fund_aliases_merge_without_rescaling_prices(self):
        def b(sec, day, price):
            return engine.Bar(sec, datetime.fromisoformat(day + " 10:00:00"),
                              datetime.fromisoformat(day + " 10:00:59"), price, price, 100)
        old, new = b("VTBM", "2022-07-21", 1.1673), b("LQDT", "2022-07-22", 1.1674)
        result = merge_aliases([old], [new])
        self.assertEqual([v.open for v in result], [1.1673, 1.1674])
        self.assertEqual([v.symbol for v in result], ["TMON", "TMON"])
        with self.assertRaises(ValueError):
            merge_aliases([old], [old])

    def test_exports_identify_actual_fund_and_source_ticker(self):
        for day, sec in (("2022-07-21", "VTBM"), ("2022-07-22", "LQDT")):
            r = exported_row({"instrument": "TMON", "bar_begin": day + " 18:46:00", "target": "TMON", "tmon_units": 100})
            self.assertEqual(r["instrument"], "LQDT")
            self.assertEqual(r["target"], "LQDT")
            self.assertEqual(r["source_ticker"], sec)
            self.assertEqual(r["lqdt_units"], 100)

    def test_short_valuation_horizon_requires_continuing_iis(self):
        cfg, funds, daily, sig = fixture()
        cfg["iis_open_date"] = "2022-01-10"
        with self.assertRaises(ValueError):
            engine.simulate(cfg, engine.Scenario("hold", "TMOS"), funds, daily, sig)
        cfg["iis_exemption_evaluation_date"] = "2025-01-10"
        with self.assertRaises(ValueError):
            engine.simulate(cfg, engine.Scenario("hold", "TMOS"), funds, daily, sig)
        cfg["iis_closed_at_backtest_end"] = False
        _, _, _, result = engine.simulate(cfg, engine.Scenario("hold", "TMOS"), funds, daily, sig)
        self.assertEqual(result["end"], "2024-01-11")
        self.assertEqual(result["tax_paid"], 0)
        self.assertEqual(result["contribution_tax_refund"], 0)


if __name__ == "__main__":
    unittest.main()
