import copy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rotation_bot.cli import frame_from_fixture
from rotation_bot.config import Config
from rotation_bot.domain import BotError, Target
from rotation_bot.execution import LiveExecutionNotImplemented, buy_lots, limit_price
from rotation_bot.providers import ReadOnlyTInvest
from rotation_bot.schedule import common_last_session, decision_window, sessions_from_day
from rotation_bot.service import check
from rotation_bot.state import Store, scope_for
from rotation_bot.strategy import target_for_snapshot


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.frame = frame_from_fixture(ROOT / "fixtures/preclose.json")
        self.cfg = Config()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_preclose_is_current_price_against_previous_day(self):
        f = self.frame
        self.assertEqual(target_for_snapshot(f.previous, f.snapshot, Target.TMOS)[0], Target.TMON)
        s = replace(f.snapshot, price=f.previous.high + 1)
        self.assertEqual(target_for_snapshot(f.previous, s, Target.TMON)[0], Target.TMOS)
        for p in (f.previous.low, f.previous.high, (f.previous.low + f.previous.high) / 2):
            self.assertEqual(target_for_snapshot(f.previous, replace(s, price=p), Target.TMON)[0], Target.TMON)

    def test_daily_decision_and_plan_are_idempotent_after_restart(self):
        first = check(self.frame, self.cfg, self.store)
        self.assertEqual(first["decision"]["target"], "TMON")
        self.assertEqual(first["decision"]["plan"]["sell"]["lots"], 1000)
        self.assertIsNone(first["decision"]["plan"]["buy"]["lots"])
        self.store.close()
        self.store = Store(self.path)
        changed = replace(self.frame, snapshot=replace(self.frame.snapshot, price=Decimal("2600")))
        second = check(changed, self.cfg, self.store)
        self.assertEqual(second["status"], "already_decided")
        self.assertEqual(first["decision"], second["decision"])
        self.assertEqual(second["orders_submitted"], 0)

    def test_wait_before_window_and_skip_after_window(self):
        lo, hi = decision_window(self.frame.session)
        for now, status in ((lo - timedelta(seconds=1), "waiting"), (hi, "missed_window")):
            result = check(replace(self.frame, now=now), self.cfg, self.store)
            self.assertEqual(result["status"], status)
        self.assertIsNone(self.store.existing(scope_for("demo-iis"), "2026-10-30"))

    def test_stale_future_or_wrong_day_quote_is_rejected(self):
        for delta in (-60, 60):
            snapshot = replace(self.frame.snapshot, observed_at=self.frame.now + timedelta(seconds=delta))
            with self.assertRaises(BotError):
                check(replace(self.frame, snapshot=snapshot), self.cfg, self.store)

    def test_current_day_candle_cannot_be_reference(self):
        f = replace(self.frame, previous=replace(self.frame.previous, day=self.frame.session.day))
        with self.assertRaises(BotError):
            check(f, self.cfg, self.store)

    def test_wrong_account_or_active_orders_is_rejected(self):
        for account in (replace(self.frame.account, account_type="ACCOUNT_TYPE_TINKOFF"),
                        replace(self.frame.account, active_pair_orders=1),
                        replace(self.frame.account, blocked_units={"TMOS": 1}),
                        replace(self.frame.account, available_rub=Decimal(-1))):
            with self.assertRaises(BotError):
                check(replace(self.frame, account=account), self.cfg, self.store)

    def test_inside_range_initializes_from_actual_held_fund(self):
        account = replace(self.frame.account, free_units={"TMOS": 0, "TMON": 1000})
        snapshot = replace(self.frame.snapshot, price=Decimal("2500"))
        result = check(replace(self.frame, account=account, snapshot=snapshot), self.cfg, self.store)
        self.assertEqual(result["decision"]["target"], "TMON")
        self.assertEqual(result["decision"]["plan"]["kind"], "hold")

    def test_no_automatic_cash_sweep(self):
        account = replace(self.frame.account, free_units={"TMOS": 0, "TMON": 1000}, available_rub=Decimal("100000"))
        result = check(replace(self.frame, account=account), self.cfg, self.store)
        self.assertEqual(result["decision"]["plan"]["kind"], "hold")

    def test_auction_and_unverified_feed_are_execution_blockers(self):
        statuses = copy.deepcopy(self.frame.trading_statuses)
        statuses["TMOS"]["tradingStatus"] = "SECURITY_TRADING_STATUS_CLOSING_AUCTION"
        result = check(replace(self.frame, verified_realtime=False, trading_statuses=statuses), self.cfg, self.store)
        blockers = result["decision"]["execution_blockers"]
        self.assertIn("signal_feed_realtime_not_verified", blockers)
        self.assertIn("TMOS:not_continuous_trading", blockers)
        self.assertIn("live_execution_not_implemented", blockers)

    def test_whole_lots_and_tick_rounding_stay_inside_budget(self):
        self.assertEqual(buy_lots(Decimal("1050"), Decimal("50"), Decimal("10"), 10), 10)
        self.assertEqual(buy_lots(Decimal("1050"), Decimal("50"), Decimal("10"), 10, Decimal("500")), 5)
        self.assertEqual(limit_price(Decimal("6.28"), Decimal("0.01"), "sell", Decimal("10")), Decimal("6.28"))
        self.assertEqual(limit_price(Decimal("6.29"), Decimal("0.01"), "buy", Decimal("10")), Decimal("6.29"))
        with self.assertRaises(BotError):
            limit_price(Decimal("6.28"), Decimal("0.01"), "sell", Decimal("10000"))

    def test_common_schedule_includes_evening_and_uses_earlier_end(self):
        day = {"date": "2026-10-30T00:00:00Z", "isTradingDay": True,
               "startTime": "2026-10-30T07:00:00Z", "endTime": "2026-10-30T15:40:00Z",
               "closingAuctionEndTime": "2026-10-30T15:50:00Z",
               "eveningStartTime": "2026-10-30T16:05:00Z", "eveningEndTime": "2026-10-30T20:50:00Z"}
        other = {**day, "eveningEndTime": "2026-10-30T20:40:00Z"}
        common = common_last_session(sessions_from_day(day), sessions_from_day(other))
        self.assertEqual(decision_window(common)[0].isoformat(), "2026-10-30T20:35:00+00:00")
        self.assertEqual(sessions_from_day({**day, "isTradingDay": False}), [])

    def test_state_is_isolated_per_account_and_never_reads_future_day(self):
        check(self.frame, self.cfg, self.store)
        self.assertIsNone(self.store.previous_target(scope_for("different-iis"), "2026-10-31"))
        self.assertIsNone(self.store.previous_target(scope_for("demo-iis"), "2026-10-30"))

    def test_mutating_api_methods_and_live_mode_are_unavailable(self):
        client = ReadOnlyTInvest("fake-test-token")
        with self.assertRaises(BotError):
            client.call("OrdersService/PostOrder", {})
        with self.assertRaises(BotError):
            LiveExecutionNotImplemented().submit_limit()
        with self.assertRaises(BotError):
            replace(self.cfg, mode="live").validate()

    def test_positions_balance_is_not_reduced_by_blocked_units_twice(self):
        class FakeClient(ReadOnlyTInvest):
            def call(self, method, payload):
                if method == "OrdersService/GetOrders":
                    return {"orders": []}
                return {"money": [{"currency": "rub", "units": "100", "nano": 0}], "blocked": [],
                        "securities": [{"instrumentUid": "demo-tmos-uid", "balance": "8", "blocked": "2"}]}
        account = FakeClient("fake").positions({"id": "demo-iis", "type": "ACCOUNT_TYPE_TINKOFF_IIS", "status": "ACCOUNT_STATUS_OPEN"},
                                               self.frame.instruments)
        self.assertEqual(account.free_units["TMOS"], 8)
        self.assertEqual(account.blocked_units["TMOS"], 2)


if __name__ == "__main__":
    unittest.main()
