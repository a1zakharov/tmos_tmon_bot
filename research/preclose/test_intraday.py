"""Independent financial/timing regressions for the intraday study."""
from datetime import date, datetime
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_backtest import Bar, Scenario, schedule, signal_inputs, simulate


def bar(symbol, day, at, price, volume=1000):
    lo = datetime.fromisoformat(day + " " + at + ":00")
    hi = lo.replace(second=59)
    return Bar(symbol, lo, hi, price, price, volume)


def fixture():
    cfg = {"start": "2024-01-10", "end": "2024-01-11", "initial_capital": 10000,
           "monthly_contribution": 0, "commission_bps": 0, "monthly_service_fee": 0,
           "slippage_bps": 0, "order_delay_seconds": 1, "tax_mode": "iis_b", "iis_open_date": "2020-01-01"}
    funds = {
        "TMOS": [bar("TMOS", "2024-01-10", "10:00", 100),
                 bar("TMOS", "2024-01-10", "18:45", 500, 1000),
                 bar("TMOS", "2024-01-10", "18:46", 100, 20),
                 bar("TMOS", "2024-01-11", "10:00", 100, 80)],
        "TMON": [bar("TMON", "2024-01-10", "10:00", 10),
                 bar("TMON", "2024-01-10", "18:46", 10),
                 bar("TMON", "2024-01-11", "10:00", 10),
                 bar("TMON", "2024-01-11", "10:01", 10)]}
    daily = {s: {d: {"close": str(p)} for d in ("2024-01-10", "2024-01-11")}
             for s, p in (("TMOS", 100), ("TMON", 10))}
    sig = [{"date": "2024-01-10", "cutoff": datetime(2024, 1, 10, 18, 45), "anchor": "18:50:00",
            "snapshot": 98, "snapshot_at": datetime(2024, 1, 10, 18, 44, 59), "age_seconds": 1,
            "previous_date": "2024-01-09", "previous_high": 101, "previous_low": 99,
            "auction_window": True}]
    return cfg, funds, daily, sig


class IntradayTests(unittest.TestCase):
    def test_partial_exit_and_next_leg_never_share_a_minute(self):
        cfg, funds, daily, sig = fixture()
        _, trades, _, s = simulate(cfg, Scenario("test"), funds, daily, sig)
        exits = [t for t in trades if t["quantity"] < 0]
        entries = [t for t in trades if t["instrument"] == "TMON"]
        self.assertEqual([t["quantity"] for t in exits], [-20, -80])
        self.assertEqual(entries[0]["bar_begin"], "2024-01-11 10:01:00")
        self.assertGreater(entries[0]["bar_begin"], exits[-1]["time"])
        self.assertEqual(s["final_equity"], 10000)

    def test_candle_containing_signal_cannot_fill_the_exit(self):
        cfg, funds, daily, sig = fixture()
        _, trades, _, _ = simulate(cfg, Scenario("test"), funds, daily, sig)
        self.assertFalse(any(t["bar_begin"] == "2024-01-10 18:45:00" for t in trades))

    def test_no_future_fund_price_changes_earlier_orders(self):
        cfg, funds, daily, sig = fixture()
        _, original, _, _ = simulate(cfg, Scenario("test"), funds, daily, sig)
        altered = copy.deepcopy(funds)
        b = altered["TMON"][-1]
        altered["TMON"][-1] = Bar(b.symbol, b.begin, b.end, 20, 20, b.volume)
        _, changed, _, _ = simulate(cfg, Scenario("test"), altered, daily, sig)
        self.assertEqual([r for r in original if r["time"] < "2024-01-11"],
                         [r for r in changed if r["time"] < "2024-01-11"])

    def test_stale_index_preserves_target(self):
        cfg, funds, daily, sig = fixture()
        sig[0]["age_seconds"] = 241
        _, trades, signals, s = simulate(cfg, Scenario("test"), funds, daily, sig)
        self.assertEqual(signals[0]["status"], "stale_snapshot")
        self.assertEqual(s["target_changes"], 0)
        self.assertFalse(any(t["quantity"] < 0 for t in trades))

    def test_participation_cap_is_respected(self):
        cfg, funds, daily, sig = fixture()
        _, trades, _, _ = simulate(cfg, Scenario("test", participation=.1), funds, daily, sig)
        self.assertTrue(all(abs(t["quantity"]) <= int(t["bar_volume"] * .1) for t in trades))

    def test_flat_prices_and_two_months_have_zero_return(self):
        cfg, _, _, _ = fixture()
        cfg = {**cfg, "start": "2024-01-31", "end": "2024-02-01", "initial_capital": 0, "monthly_contribution": 20000}
        funds = {s: [bar(s, d, "10:00", p) for d in ("2024-01-31", "2024-02-01")]
                 for s, p in (("TMOS", 100), ("TMON", 10))}
        daily = {s: {d: {"close": str(p)} for d in ("2024-01-31", "2024-02-01")}
                 for s, p in (("TMOS", 100), ("TMON", 10))}
        rows, _, _, summary = simulate(cfg, Scenario("test", allocation="TMOS"), funds, daily, [])
        self.assertEqual(summary["contributions"], 40000)
        self.assertEqual(summary["final_equity"], 40000)
        self.assertTrue(all(abs(r["reconciliation_error"]) < 1e-7 for r in rows))

    def test_no_current_minute_close_or_current_daily_range_in_signal(self):
        bars = [bar("IMOEX", "2024-01-10", "18:44", 100),
                bar("IMOEX", "2024-01-10", "18:45", 500)]
        daily = {"2024-01-09": {"high": "101", "low": "99"},
                 "2024-01-10": {"high": "1000", "low": "1"}}
        signal = signal_inputs(bars, daily, {"2024-01-10"})[0]
        self.assertEqual(signal["snapshot"], 100)
        self.assertEqual(signal["previous_high"], 101)
        self.assertEqual(signal["previous_low"], 99)
        self.assertEqual(signal["snapshot_at"], datetime(2024, 1, 10, 18, 44, 59))

    def test_historical_schedule_changes_on_correct_date(self):
        self.assertEqual(schedule(date(2026, 3, 20))[0].isoformat(), "18:50:00")
        self.assertEqual(schedule(date(2026, 3, 23))[0].isoformat(), "19:00:00")


if __name__ == "__main__":
    unittest.main()
