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
from rotation_bot.providers import MoexSignalFeed, ReadOnlyTInvest, observe_frame
from rotation_bot.schedule import common_last_session, common_main_session, decision_window, sessions_from_day
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

    def test_quote_at_or_after_cutoff_or_from_wrong_day_is_rejected(self):
        cutoff, _ = decision_window(self.frame.session)
        for at in (cutoff, cutoff + timedelta(seconds=1), cutoff - timedelta(days=1)):
            snapshot = replace(self.frame.snapshot, observed_at=at)
            with self.assertRaises(BotError):
                check(replace(self.frame, snapshot=snapshot), self.cfg, self.store)

    def test_current_day_candle_cannot_be_reference(self):
        f = replace(self.frame, previous=replace(self.frame.previous, day=self.frame.session.day))
        with self.assertRaises(BotError):
            check(f, self.cfg, self.store)

    def test_wrong_account_or_borrowed_cash_is_rejected(self):
        for account in (replace(self.frame.account, account_type="ACCOUNT_TYPE_TINKOFF"),
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

    def test_auction_blocks_execution_but_delayed_imoex_is_allowed(self):
        statuses = copy.deepcopy(self.frame.trading_statuses)
        statuses["TMOS"]["tradingStatus"] = "SECURITY_TRADING_STATUS_CLOSING_AUCTION"
        result = check(replace(self.frame, verified_realtime=False, trading_statuses=statuses), self.cfg, self.store)
        blockers = result["decision"]["execution_blockers"]
        self.assertNotIn("signal_feed_realtime_not_verified", blockers)
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
        main = common_main_session(sessions_from_day(day), sessions_from_day(other))
        self.assertEqual(decision_window(main)[0].isoformat(), "2026-10-30T15:45:00+00:00")
        aggregate = {**day, "endTime": day["eveningEndTime"]}
        self.assertEqual(common_main_session(sessions_from_day(aggregate), sessions_from_day(other)).end, main.end)
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

    def test_ten_minute_boundary_is_measured_at_fixed_cutoff(self):
        cutoff, _ = decision_window(self.frame.session)
        snapshot = replace(self.frame.snapshot, observed_at=cutoff - timedelta(seconds=600))
        # Timer is 59 seconds late; the same T-5 observation is still eligible.
        now = cutoff + timedelta(seconds=59)
        books = {t: replace(b, observed_at=now) for t, b in self.frame.books.items()}
        result = check(replace(self.frame, now=now, snapshot=snapshot, books=books), self.cfg, self.store)
        self.assertEqual(result["decision"]["target"], "TMON")
        self.assertEqual(result["decision"]["quote_age_seconds_at_cutoff"], 600)
        self.assertEqual(result["decision"]["cutoff_at"], cutoff.isoformat())

    def test_stale_and_missing_quotes_retain_persisted_target(self):
        scope = scope_for(self.frame.account.account_id)
        self.store.record_once(scope, "2026-10-29", {"target": "TMON", "trade_date": "2026-10-29"})
        cutoff, _ = decision_window(self.frame.session)
        f = replace(self.frame, snapshot=replace(self.frame.snapshot,
                    price=Decimal("9999"), observed_at=cutoff - timedelta(seconds=601)))
        first = check(f, self.cfg, self.store)
        self.assertEqual(first["decision"]["target"], "TMON")
        self.assertEqual(first["decision"]["reason"], "stale_snapshot")
        # Isolate the missing-quote case to a different account's namespace.
        f = replace(self.frame, snapshot=None, account=replace(self.frame.account,
                    account_id="other-iis", free_units={"TMOS": 0, "TMON": 10}))
        second = check(f, self.cfg, self.store)
        self.assertEqual(second["decision"]["reason"], "no_snapshot")
        self.assertEqual(second["decision"]["target"], "TMON")

    def test_closed_market_does_not_erase_signal_and_plan_refreshes_after_restart(self):
        f = replace(self.frame, books={}, trading_statuses={})
        first = check(f, self.cfg, self.store)
        self.assertEqual(first["decision"]["target"], "TMON")
        self.assertEqual(first["execution"]["state"], "waiting_for_market")
        self.store.close()
        self.store = Store(self.path)
        later = self.frame.now + timedelta(hours=1)
        books = {t: replace(b, observed_at=later, bid=b.bid+1, ask=b.ask+1) for t, b in self.frame.books.items()}
        # Reference data intentionally absent: only the persisted target is used.
        ready = check(replace(self.frame, now=later, previous=None, snapshot=None, books=books), self.cfg, self.store)
        self.assertEqual(first["decision"], ready["decision"])
        self.assertEqual(ready["execution"]["state"], "plan_available")
        self.assertEqual(ready["execution"]["plan"]["sell"]["limit_price"], "7.28")
        self.assertEqual(ready["orders_submitted"], 0)

    def test_pending_target_is_planned_next_morning_without_new_signal(self):
        original = check(self.frame, self.cfg, self.store)["decision"]
        next_day = timedelta(days=3)  # Monday, before its own signal window.
        now = self.frame.session.start + next_day
        f = replace(self.frame, now=now,
                    session=replace(self.frame.session, day=now.date(),
                                    start=self.frame.session.start+next_day, end=self.frame.session.end+next_day),
                    previous=None, snapshot=None,
                    books={t: replace(b, observed_at=now) for t,b in self.frame.books.items()})
        result = check(f, self.cfg, self.store)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["decision"]["intent_id"], original["intent_id"])
        self.assertEqual(result["execution"]["target"], "TMON")
        self.assertEqual(result["execution"]["plan"]["kind"], "switch")
        self.assertIsNone(self.store.existing(scope_for("demo-iis"), now.date().isoformat()))

    def test_active_order_or_stale_book_blocks_only_execution(self):
        for account, books in ((replace(self.frame.account, active_pair_orders=1), self.frame.books),
                               (self.frame.account, {t: replace(b, observed_at=self.frame.now-timedelta(seconds=31))
                                                     for t,b in self.frame.books.items()})):
            f = replace(self.frame, account=account, books=books)
            result = check(f, self.cfg, self.store)
            self.assertEqual(result["decision"]["target"], "TMON")
            self.assertEqual(result["execution"]["state"], "waiting_for_market")

    def test_old_45_second_config_requires_explicit_migration(self):
        with self.assertRaisesRegex(BotError, "600"):
            replace(self.cfg, max_quote_age_seconds=45).validate()

    def test_v1_daily_decision_remains_idempotent_after_upgrade(self):
        scope = scope_for("demo-iis")
        old, _ = self.store.record_once(scope, "2026-10-30",
                    {"strategy": "preclose-v1", "trade_date": "2026-10-30", "target": "TMOS"})
        result = check(self.frame, self.cfg, self.store)
        self.assertEqual(result["decision"], old)
        self.assertEqual(result["status"], "already_decided")

    def test_feed_uses_last_completed_minute_and_does_not_look_past_cutoff(self):
        cutoff, _ = decision_window(self.frame.session)
        cutoff = cutoff.astimezone(self.frame.snapshot.observed_at.tzinfo)
        def row(offset, price, duration=59):
            begin = cutoff + timedelta(seconds=offset)
            return [begin.replace(tzinfo=None).isoformat(sep=" "),
                    (begin+timedelta(seconds=duration)).replace(tzinfo=None).isoformat(sep=" "), price]
        # Feed strings are exchange-local, so use the provider's MSK timezone.
        from rotation_bot.domain import MSK
        cutoff = cutoff.astimezone(MSK)
        class FakeFeed(MoexSignalFeed):
            def _read(self, suffix, params):
                self.params = params
                return {"candles": {"columns": ["begin", "end", "close"], "data":
                        [row(-300, 2480), row(0, 9999), row(-60, 2481), row(-86400, 9998)]}}
        feed = FakeFeed()
        snapshot = feed.snapshot(cutoff)
        self.assertEqual(snapshot.price, Decimal(2481))
        self.assertEqual(snapshot.observed_at, cutoff - timedelta(seconds=1))
        self.assertEqual(feed.params["interval"], 1)
        self.assertEqual(feed.params["till"], cutoff.strftime("%Y-%m-%d %H:%M:%S"))

    def test_observe_flow_keeps_main_cutoff_and_replans_without_refetching_signal(self):
        f = self.frame
        class Client:
            fail_books = True
            def account(self, account_id):
                return {"id": account_id}
            def instrument(self, ticker, class_code):
                return f.instruments[ticker]
            def day_sessions(self, exchange, today):
                return [f.session, replace(f.session, start=f.session.end+timedelta(minutes=5),
                                          end=f.session.end+timedelta(hours=4), kind="evening")]
            def positions(self, meta, instruments):
                return f.account
            def book(self, ins):
                if self.fail_books:
                    raise BotError("Market closed")
                return replace(f.books[ins.ticker], observed_at=now[0])
            def trading_status(self, ins):
                return f.trading_statuses[ins.ticker]
        class Feed:
            calls = 0
            def previous_day(self, today):
                self.calls += 1
                return f.previous
            def snapshot(self, cutoff):
                self.cutoff = cutoff
                self.calls += 1
                return f.snapshot
        client, feed, now = Client(), Feed(), [f.now]
        first = observe_frame(client, feed, "demo-iis", self.cfg, self.store, lambda: now[0])
        result = check(first, self.cfg, self.store)
        self.assertEqual(feed.cutoff, decision_window(f.session)[0])
        self.assertEqual(result["execution"]["state"], "waiting_for_market")
        self.assertEqual(result["decision"]["target"], "TMON")
        now[0] += timedelta(hours=1)
        client.fail_books = False
        second = observe_frame(client, feed, "demo-iis", self.cfg, self.store, lambda: now[0])
        result = check(second, self.cfg, self.store)
        self.assertEqual(feed.calls, 2)
        self.assertIsNone(second.snapshot)
        self.assertEqual(result["execution"]["state"], "plan_available")

    def test_in_progress_minute_is_excluded_even_if_its_reported_end_is_old(self):
        from rotation_bot.domain import MSK
        cutoff = decision_window(self.frame.session)[0].astimezone(MSK) + timedelta(seconds=30)
        class Feed(MoexSignalFeed):
            def _read(self, suffix, params):
                return {"candles": {"columns": ["begin", "end", "close"], "data": [
                    [cutoff.replace(second=0,tzinfo=None).isoformat(sep=" "),
                     cutoff.replace(second=10,tzinfo=None).isoformat(sep=" "), 9999]]}}
        self.assertIsNone(Feed().snapshot(cutoff))


if __name__ == "__main__":
    unittest.main()
