"""US ETF rotation: actual RTH minutes, fixed T-5 signal, dividends and cash flows."""
from __future__ import annotations
import bisect
import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import heapq
import json
import math
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
NY = ZoneInfo("America/New_York")
sys.path.insert(0, str(ROOT.parent / "preclose"))
from run_backtest import xirr


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    begin: datetime
    end: datetime
    open: float
    close: float
    volume: float


def csv_rows(path):
    with path.open() as f:
        return list(csv.DictReader(f))


def iter_csv(path):
    with path.open() as f:
        yield from csv.DictReader(f)


def write_csv(path, rows):
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def local_datetime(value):
    return datetime.fromisoformat(value).astimezone(NY).replace(tzinfo=None)


def load_bars(symbol, data, start, end):
    result = []
    for row in iter_csv(data / (symbol+"_minutes.csv")):
        begin = local_datetime(row["ts"])
        if start <= begin.date() <= end:
            result.append(Bar(symbol, begin, begin+timedelta(seconds=59),
                              float(row["open"]), float(row["close"]), float(row["volume"])))
    return result


def choose_target(price, low, high, old, defensive):
    if not (0 < low <= high and math.isfinite(price) and price > 0):
        raise ValueError("Invalid signal prices")
    if price < low:
        return defensive, "below_previous_low"
    if price > high:
        return "SPY", "above_previous_high"
    return old, "inside_previous_range"


def make_signals(bars, spy_daily, defensive_daily, calendar, start, end, dividends, neutral=False):
    index = {}
    for bar in bars:
        index.setdefault(bar.begin.date().isoformat(), []).append(bar)
    reference_days = sorted(spy_daily)
    exdiv = {r["ex_date"]:float(r["amount"]) for r in dividends if r["symbol"] == "SPY"}
    signals = []
    for day, session in sorted(calendar.items()):
        if not start.isoformat() <= day <= end.isoformat() or day not in spy_daily or day not in defensive_daily:
            continue
        cutoff = local_datetime(session["close"])-timedelta(minutes=5)
        candles = [b for b in index.get(day, []) if b.end < cutoff]
        last = max(candles, key=lambda b:b.end) if candles else None
        previous = bisect.bisect_left(reference_days, day)-1
        assert previous >= 0
        prior_day = reference_days[previous]
        prior = spy_daily[prior_day]
        adjustment = exdiv.get(day,0) if neutral else 0
        signals.append({"date": day, "cutoff": cutoff,
            "session_close": local_datetime(session["close"]).isoformat(sep=" "),
            "snapshot": last.close if last else None, "snapshot_at": last.end if last else None,
            "age_seconds": (cutoff-last.end).total_seconds() if last else None,
            "previous_date": prior_day, "previous_low": float(prior["low"])-adjustment,
            "previous_high": float(prior["high"])-adjustment, "exdiv_range_adjustment": adjustment})
    return signals


def simulate(cfg, name, defensive, start, end, funds, daily_prices, signals, dividends,
             allocation="rotation", share_step=1.0, participation=1.0):
    assert all(cfg[k] == 0 for k in ("initial_capital","commission_bps","slippage_bps","monthly_service_fee","tax_rate"))
    target = "SPY" if allocation == "rotation" else allocation
    q, mark = {s:0 for s in funds}, {s:0.0 for s in funds}
    cash = contributed = receivable = dividends_earned = dividends_paid = 0.0
    previous_equity = day_deposit = day_pnl = 0.0
    nav = peak = 1.0
    worst_dd = 0.0
    ready = datetime.combine(start, time.min)
    delay = timedelta(seconds=cfg["order_delay_seconds"])
    signal_time = None
    daily, trades, decisions, distributions, flows = [], [], [], [], []
    entitlements = {}
    accepted = stale = missing = changes = partial = 0
    relevant = [d for d in dividends if d["symbol"] in funds and start.isoformat() <= d["ex_date"] <= end.isoformat()]
    by_ex, by_pay = {}, {}
    for d in relevant:
        by_ex.setdefault(d["ex_date"], []).append(d)
        by_pay.setdefault(d["pay_date"], []).append(d)

    def fixed():
        day = start
        while day <= end:
            at = datetime.combine(day,time.min)
            if day == start or day.day == 1:
                yield at,0,("deposit",cfg["monthly_contribution"])
            for d in by_ex.get(day.isoformat(),[]):
                yield at,1,("accrue",d)
            for d in by_pay.get(day.isoformat(),[]):
                yield at,2,("pay",d)
            yield datetime.combine(day,time(23,59,59,999999)),5,day
            day += timedelta(days=1)

    def signal_events():
        for row in signals:
            yield row["cutoff"],3,row

    def bar_events():
        for b in heapq.merge(*(funds[s] for s in sorted(funds)),key=lambda b:(b.end,b.symbol)):
            yield b.end,4,b

    for at,kind,obj in heapq.merge(fixed(),signal_events(),bar_events(),key=lambda x:(x[0],x[1])):
        if kind == 0:
            amount = obj[1]
            cash += amount
            contributed += amount
            day_deposit += amount
            flows.append((at.date(),-amount))
        elif kind == 1:
            d = obj[1]
            amount = q[d["symbol"]]*share_step*float(d["amount"])
            key = (d["symbol"],d["ex_date"])
            entitlements[key] = amount
            receivable += amount
            dividends_earned += amount
            day_pnl += amount
            if amount:
                distributions.append({"date":at.date().isoformat(),"symbol":d["symbol"],"kind":"accrued",
                    "amount":amount,"ex_date":d["ex_date"],"pay_date":d["pay_date"],
                    "units_entitled":q[d["symbol"]]*share_step})
        elif kind == 2:
            d = obj[1]
            amount = entitlements.pop((d["symbol"],d["ex_date"]),0)
            receivable -= amount
            cash += amount
            dividends_paid += amount
            if amount:
                distributions.append({"date":at.date().isoformat(),"symbol":d["symbol"],"kind":"paid",
                    "amount":amount,"ex_date":d["ex_date"],"pay_date":d["pay_date"],"units_entitled":""})
        elif kind == 3:
            row = obj
            before = target
            if row["snapshot"] is None:
                reason = "no_snapshot"
                missing += 1
            elif row["age_seconds"] > cfg["max_quote_age_seconds"]:
                reason = "stale_snapshot"
                stale += 1
            else:
                accepted += 1
                candidate,reason = choose_target(row["snapshot"],row["previous_low"],row["previous_high"],target,defensive)
                if allocation == "rotation":
                    target = candidate
                    if target != before:
                        changes += 1
                        ready = at+delay
                        signal_time = at
            decisions.append({**row,"target_before":before,"target_after":target,"reason":reason})
        elif kind == 4:
            b = obj
            sec = b.symbol
            before = q[sec]
            old_mark = mark[sec]
            other = defensive if target == "SPY" else "SPY"
            quantity = requested = 0
            if b.begin >= ready:
                if sec == other and q[sec]:
                    requested = q[sec]
                    quantity = -requested
                elif sec == target and q[other] == 0:
                    requested = max(0,math.floor((cash+1e-9)/(b.open*share_step)))
                    quantity = requested
                capacity = math.floor(b.volume*participation/share_step)
                if abs(quantity) > capacity:
                    partial += 1
                    quantity = (1 if quantity > 0 else -1)*capacity
                if quantity:
                    value = quantity*share_step*b.open
                    cash -= value
                    q[sec] += quantity
                    if -1e-7 < cash < 0:
                        cash = 0.0
                    trades.append({"time":at.isoformat(sep=" "),"bar_begin":b.begin.isoformat(sep=" "),
                        "symbol":sec,"quantity":quantity*share_step,"quantity_steps":quantity,
                        "share_step":share_step,"price":b.open,"bar_volume":b.volume,
                        "participation":abs(quantity)*share_step/b.volume,"cash_after":cash,
                        "signal_time":signal_time.isoformat(sep=" ") if signal_time else "",
                        "target":target,"reason":"exit" if quantity < 0 else "buy_or_reinvest"})
                    ready = at+delay
            day_pnl += before*share_step*(b.open-old_mark)+q[sec]*share_step*(b.close-b.open)
            mark[sec] = b.close
            assert cash >= -1e-6 and min(q.values()) >= 0 and not all(q.values())
        else:
            day = obj
            for sec in q:
                row = daily_prices[sec].get(day.isoformat())
                if row:
                    close = float(row["close"])
                    day_pnl += q[sec]*share_step*(close-mark[sec])
                    mark[sec] = close
            equity = cash+receivable+sum(q[s]*share_step*mark[s] for s in q)
            error = equity-previous_equity-day_deposit-day_pnl
            assert abs(error)<max(1e-7,equity*1e-10),(name,day,error)
            base = previous_equity+day_deposit
            nav *= equity/base if base else 1
            peak = max(peak,nav)
            drawdown = nav/peak-1
            worst_dd = min(worst_dd,drawdown)
            daily.append({"date":day.isoformat(),"equity":equity,"cash":cash,"dividend_receivable":receivable,
                "contributed":contributed,"deposit":day_deposit,"SPY_units":q["SPY"]*share_step,
                "defensive_units":q[defensive]*share_step,"defensive_symbol":defensive,"target":target,
                "nav":nav,"drawdown":drawdown,"accounting_error":error,
                "dividends_earned":dividends_earned,"dividends_paid":dividends_paid})
            previous_equity,day_deposit,day_pnl = equity,0.0,0.0
    annual,last_nav = {},1.0
    for row in daily:
        year = row["date"][:4]
        a = annual.setdefault(year,{"factor":1.0,"deposits":0.0})
        a["factor"] *= row["nav"]/last_nav
        a["deposits"] += row["deposit"]
        a["end_equity"] = row["equity"]
        last_nav = row["nav"]
    years = (end-start).days/365.25
    summary = {"name":name,"risk":"SPY","defensive":defensive,"allocation":allocation,
        "start":start.isoformat(),"end":end.isoformat(),"share_step":share_step,
        "contributions":contributed,"final_equity":daily[-1]["equity"],
        "profit":daily[-1]["equity"]-contributed,
        "xirr":xirr(flows+[(end,daily[-1]["equity"])]) if years else None,
        "twr_cagr":nav**(1/years)-1 if years else None,
        "max_drawdown":worst_dd,"target_changes":changes,"trade_fills":len(trades),
        "partial_fills":partial,"accepted_signals":accepted,"stale_signals":stale,"missing_signals":missing,
        "dividends_earned":dividends_earned,"dividends_paid":dividends_paid,"final_receivable":receivable,
        "final_cash":cash,"cash_fraction_final":cash/daily[-1]["equity"],
        "commission":0,"slippage":0,"tax_paid":0,
        "annual":{y:{"twr":a["factor"]-1,"deposits":a["deposits"],"end_equity":a["end_equity"]} for y,a in annual.items()}}
    # Independent final-account replay: cash paid, not just accrued, funds purchases.
    ledger_cash = contributed+dividends_paid
    ledger_units = {s:0 for s in q}
    for t in trades:
        ledger_cash -= t["quantity"]*t["price"]
        ledger_units[t["symbol"]] += t["quantity_steps"]
        assert min(ledger_units.values()) >= 0 and not all(ledger_units.values())
        assert not t["signal_time"] or t["bar_begin"] > t["signal_time"]
        assert t["participation"] <= participation+1e-9
    assert abs(ledger_cash-cash) < 1e-6 and ledger_units == q
    audit = {"status":"passed","cash_ledger_error":ledger_cash-cash,
             "max_daily_accounting_error":max(abs(r["accounting_error"]) for r in daily),
             "end_units_steps":q,"dividend_entitlements_match":abs(sum(entitlements.values())-receivable)<1e-6}
    assert audit["dividend_entitlements_match"]
    return summary,daily,trades,decisions,distributions,audit


def main():
    cfg = json.loads((ROOT/"config.json").read_text())
    data,out = ROOT/"data",ROOT/"results"
    out.mkdir(exist_ok=True)
    manifest = json.loads((data/"manifest.json").read_text())
    for name,digest in manifest["normalized_files"].items():
        assert hashlib.sha256((data/name).read_bytes()).hexdigest() == digest,name
    calendar = {r["date"]:r for r in csv_rows(data/"schedule.csv")}
    daily_prices = {s:{r["date"]:r for r in csv_rows(data/(s+"_daily.csv"))} for s in ("SPY","SGOV","BIL")}
    dividends = csv_rows(data/"dividends.csv")
    earliest,end = date.fromisoformat(min(cfg["periods"].values())),date.fromisoformat(cfg["end"])
    all_bars = {s:load_bars(s,data,earliest,end) for s in daily_prices}
    results,audits,coverage = {},{},{}
    for defensive,start_s in cfg["periods"].items():
        start = date.fromisoformat(start_s)
        funds = {s:[b for b in all_bars[s] if b.begin.date() >= start] for s in ("SPY",defensive)}
        signals = make_signals(funds["SPY"],daily_prices["SPY"],daily_prices[defensive],calendar,start,end,dividends)
        neutral = make_signals(funds["SPY"],daily_prices["SPY"],daily_prices[defensive],calendar,start,end,dividends,True)
        coverage[defensive] = {"signal_days":len(signals),
            "early_close_days":sum(datetime.fromisoformat(r["session_close"]).hour == 13 for r in signals),
            "missing_SPY_snapshot_dates":[r["date"] for r in signals if r["snapshot"] is None],
            "stale_SPY_snapshot_dates":[r["date"] for r in signals if r["age_seconds"] is not None and r["age_seconds"]>600],
            "minute_rows":{s:len(v) for s,v in funds.items()}}
        for granularity,step in (("whole",1.0),("fractional",.000001)):
            for kind,allocation in (("hold_spy","SPY"),("hold_defensive",defensive),("rotation","rotation")):
                name = defensive.lower()+"_"+granularity+"_"+kind
                print("Running",name,flush=True)
                result = simulate(cfg,name,defensive,start,end,funds,daily_prices,signals,dividends,allocation,step)
                results[name],daily,trades,decisions,distributions,audits[name] = result
                for suffix,rows in (("daily",daily),("trades",trades),("signals",decisions),("dividends",distributions)):
                    write_csv(out/(name+"_"+suffix+".csv"),rows)
                print(name,round(results[name]["final_equity"],2),results[name]["xirr"],flush=True)
        name = defensive.lower()+"_whole_rotation_exdiv_neutral"
        print("Running",name,flush=True)
        result = simulate(cfg,name,defensive,start,end,funds,daily_prices,neutral,dividends)
        results[name],daily,trades,decisions,distributions,audits[name] = result
        for suffix,rows in (("daily",daily),("trades",trades),("signals",decisions),("dividends",distributions)):
            write_csv(out/(name+"_"+suffix+".csv"),rows)
    for name,obj in (("summary",results),("audit",audits),("coverage",coverage),("config_used",cfg)):
        (out/(name+".json")).write_text(json.dumps(obj,indent=2))
    print("DONE",len(results),"scenarios",flush=True)


if __name__ == "__main__":
    main()
