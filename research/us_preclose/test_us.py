"""Independent regressions for dividend ownership, execution timing and NY clocks."""
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
from run import Bar, choose_target, make_signals, simulate


def bar(symbol, day, at, price, volume=10000):
    begin = datetime.fromisoformat(day+" "+at+":00")
    return Bar(symbol,begin,begin+timedelta(seconds=59),price,price,volume)


def fixture():
    cfg = {"initial_capital":0,"monthly_contribution":200,"commission_bps":0,"slippage_bps":0,
           "monthly_service_fee":0,"tax_rate":0,"order_delay_seconds":1,"max_quote_age_seconds":600}
    funds = {"SPY":[],"BIL":[]}
    daily = {"SPY":{},"BIL":{}}
    for day,price in (("2024-01-10",100),("2024-01-11",95),("2024-01-12",95)):
        for symbol,value in (("SPY",price),("BIL",1)):
            funds[symbol] += [bar(symbol,day,at,value) for at in ("09:30","09:31","15:55","15:56","15:57")]
            daily[symbol][day] = {"close":str(value)}
    dividend = {"symbol":"SPY","ex_date":"2024-01-11","pay_date":"2024-01-12","amount":5}
    signal = {"date":"2024-01-11","cutoff":datetime(2024,1,11,15,55),
              "snapshot":90,"snapshot_at":datetime(2024,1,11,15,54,59),"age_seconds":1,
              "previous_date":"2024-01-10","previous_low":99,"previous_high":101}
    return cfg,funds,daily,dividend,signal


class USBacktestTests(unittest.TestCase):
    def test_dividend_offsets_ex_date_drop_without_early_cash(self):
        cfg,funds,daily,dividend,_ = fixture()
        summary,rows,_,_,journal,audit = simulate(cfg,"hold","BIL",date(2024,1,10),date(2024,1,12),
            funds,daily,[],[dividend],"SPY")
        self.assertEqual(rows[1]["equity"],200)
        self.assertEqual(rows[1]["dividend_receivable"],10)
        self.assertEqual(rows[1]["cash"],0)
        self.assertEqual(rows[2]["equity"],200)
        self.assertEqual(rows[2]["cash"],10)
        self.assertEqual(summary["dividends_paid"],10)
        self.assertAlmostEqual(summary["max_drawdown"],0)
        self.assertEqual([r["kind"] for r in journal],["accrued","paid"])
        self.assertEqual(audit["status"],"passed")

    def test_selling_after_ex_date_keeps_dividend_entitlement(self):
        cfg,funds,daily,dividend,signal = fixture()
        summary,rows,trades,_,journal,_ = simulate(cfg,"rotation","BIL",date(2024,1,10),date(2024,1,12),
            funds,daily,[signal],[dividend])
        self.assertEqual(rows[1]["SPY_units"],0)
        self.assertEqual(rows[1]["defensive_units"],190)
        self.assertEqual(rows[1]["dividend_receivable"],10)
        self.assertEqual(rows[2]["defensive_units"],200)
        self.assertEqual(summary["dividends_paid"],10)
        self.assertEqual(summary["final_equity"],200)
        exits = [t for t in trades if t["quantity"]<0]
        self.assertEqual(exits[0]["bar_begin"],"2024-01-11 15:56:00")
        entries = [t for t in trades if t["symbol"]=="BIL"]
        self.assertGreater(entries[0]["bar_begin"],exits[0]["time"])

    def test_buying_on_ex_date_does_not_receive_distribution(self):
        cfg,funds,daily,dividend,_ = fixture()
        funds = {s:[b for b in v if b.begin.date()>=date(2024,1,11)] for s,v in funds.items()}
        summary,*_ = simulate(cfg,"hold","BIL",date(2024,1,11),date(2024,1,12),funds,daily,[],[dividend],"SPY")
        self.assertEqual(summary["dividends_earned"],0)
        self.assertEqual(summary["final_equity"],200)

    def test_unpaid_dividend_is_part_of_terminal_equity(self):
        cfg,funds,daily,dividend,_ = fixture()
        funds = {s:[b for b in v if b.begin.date()<=date(2024,1,11)] for s,v in funds.items()}
        summary,*_ = simulate(cfg,"hold","BIL",date(2024,1,10),date(2024,1,11),funds,daily,[],[dividend],"SPY")
        self.assertEqual(summary["final_equity"],200)
        self.assertEqual(summary["final_receivable"],10)
        self.assertEqual(summary["dividends_paid"],0)

    def test_fractional_shares_and_whole_share_cash_drag(self):
        cfg,_,_,_,_ = fixture()
        funds = {s:[bar(s,"2024-01-10","09:30",400 if s=="SPY" else 1)] for s in ("SPY","BIL")}
        daily = {"SPY":{"2024-01-10":{"close":"440"}},"BIL":{"2024-01-10":{"close":"1"}}}
        whole,*_ = simulate(cfg,"whole","BIL",date(2024,1,10),date(2024,1,10),funds,daily,[],[],"SPY")
        fractional,*_ = simulate(cfg,"fractional","BIL",date(2024,1,10),date(2024,1,10),funds,daily,[],[],"SPY",.000001)
        self.assertEqual(whole["final_equity"],200)
        self.assertEqual(fractional["final_equity"],220)

    def test_signal_cannot_use_later_minute_or_current_day_range(self):
        cfg,_,_,_,_ = fixture()
        bars = [bar("SPY","2024-01-11","15:54",98),bar("SPY","2024-01-11","15:55",999)]
        daily = {"2024-01-10":{"low":99,"high":101},"2024-01-11":{"low":1,"high":999}}
        schedule = {"2024-01-11":{"close":"2024-01-11 21:00:00+00:00"}}
        rows = make_signals(bars,daily,{"2024-01-11":{}},schedule,date(2024,1,11),date(2024,1,11),[])
        self.assertEqual(rows[0]["snapshot"],98)
        self.assertEqual(rows[0]["previous_low"],99)
        self.assertEqual(rows[0]["age_seconds"],1)

    def test_early_closes_respect_daylight_saving_time(self):
        for day,previous,utc_close in (("2023-11-24","2023-11-22","18:00"),
                                       ("2024-07-03","2024-07-02","17:00")):
            d=date.fromisoformat(day)
            bars=[bar("SPY",day,"12:54",100),bar("SPY",day,"12:55",999)]
            rows=make_signals(bars,{previous:{"low":99,"high":101},day:{"low":1,"high":999}},
                {day:{}},{day:{"close":day+" "+utc_close+":00+00:00"}},d,d,[])
            self.assertEqual(rows[0]["cutoff"].strftime("%H:%M"),"12:55")
            self.assertEqual(rows[0]["snapshot"],100)

    def test_stale_signal_retains_previous_target(self):
        cfg,funds,daily,_,signal = fixture()
        signal={**signal,"age_seconds":601}
        summary,_,_,decisions,_,_=simulate(cfg,"stale","BIL",date(2024,1,10),date(2024,1,12),
            funds,daily,[signal],[])
        self.assertEqual(summary["target_changes"],0)
        self.assertEqual(decisions[0]["target_after"],"SPY")

    def test_exdiv_control_adjusts_only_previous_range_on_the_ex_date(self):
        day="2024-01-11"
        bars=[bar("SPY",day,"15:54",95)]
        daily={"2024-01-10":{"low":99,"high":101},day:{"low":1,"high":999}}
        calendar={day:{"close":day+" 21:00:00+00:00"}}
        dividend={"symbol":"SPY","ex_date":day,"pay_date":"2024-01-12","amount":"5"}
        raw=make_signals(bars,daily,{day:{}},calendar,date(2024,1,11),date(2024,1,11),[dividend])[0]
        neutral=make_signals(bars,daily,{day:{}},calendar,date(2024,1,11),date(2024,1,11),[dividend],True)[0]
        self.assertEqual(choose_target(95,raw["previous_low"],raw["previous_high"],"SPY","BIL")[0],"BIL")
        self.assertEqual(choose_target(95,neutral["previous_low"],neutral["previous_high"],"SPY","BIL")[0],"SPY")
        self.assertEqual(neutral["snapshot"],raw["snapshot"])

    def test_partial_exit_has_to_finish_before_defensive_purchase(self):
        cfg,funds,daily,_,signal=fixture()
        funds["SPY"]=[replace(b,volume=1) if b.begin.date()==date(2024,1,11) and b.begin.hour==15 else b
                      for b in funds["SPY"]]
        _,_,trades,_,_,_=simulate(cfg,"partial","BIL",date(2024,1,10),date(2024,1,12),
            funds,daily,[signal],[])
        exits=[t for t in trades if t["quantity"]<0]
        buy=next(t for t in trades if t["symbol"]=="BIL")
        self.assertEqual([t["quantity"] for t in exits],[-1,-1])
        self.assertGreater(buy["bar_begin"],exits[-1]["time"])


if __name__ == "__main__":
    unittest.main()
