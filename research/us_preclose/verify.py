"""Independent daily ledger replay from exported trades and issuer actions."""
from collections import defaultdict
import csv
from datetime import date
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(path):
    with path.open() as f:
        return list(csv.DictReader(f))


def main():
    data,out = ROOT/"data",ROOT/"results"
    summaries=json.loads((out/"summary.json").read_text())
    source_divs=read(data/"dividends.csv")
    prices={s:{r["date"]:float(r["close"]) for r in read(data/(s+"_daily.csv"))} for s in ("SPY","SGOV","BIL")}
    audit={}
    for name,s in summaries.items():
        rows=read(out/(name+"_daily.csv"))
        by_trade,by_ex,by_pay=defaultdict(list),defaultdict(list),defaultdict(list)
        for t in read(out/(name+"_trades.csv")):
            by_trade[t["time"][:10]].append(t)
        for d in source_divs:
            if d["symbol"] in ("SPY",s["defensive"]) and s["start"]<=d["ex_date"]<=s["end"]:
                by_ex[d["ex_date"]].append(d)
                by_pay[d["pay_date"]].append(d)
        units={symbol:0 for symbol in ("SPY",s["defensive"])}
        marks={symbol:0.0 for symbol in units}
        cash=contributions=earned=paid=0.0
        due={}
        nav=1.0
        previous=0.0
        maximum_error=0.0
        max_nav_error=0.0
        for row in rows:
            day=row["date"]
            deposit=200 if day==s["start"] or day.endswith("-01") else 0
            cash+=deposit
            contributions+=deposit
            for d in by_ex[day]:
                amount=units[d["symbol"]]*s["share_step"]*float(d["amount"])
                due[d["symbol"],d["ex_date"]]=amount
                earned+=amount
            for d in by_pay[day]:
                amount=due.pop((d["symbol"],d["ex_date"]),0.0)
                cash+=amount
                paid+=amount
            for t in by_trade[day]:
                steps=int(t["quantity_steps"])
                cash-=steps*s["share_step"]*float(t["price"])
                units[t["symbol"]]+=steps
                assert cash>=-1e-6 and min(units.values())>=0 and not all(units.values()),name
            for symbol in marks:
                if day in prices[symbol]:
                    marks[symbol]=prices[symbol][day]
            equity=cash+sum(due.values())+sum(units[k]*s["share_step"]*marks[k] for k in units)
            maximum_error=max(maximum_error,abs(equity-float(row["equity"])))
            nav*=equity/(previous+deposit) if previous+deposit else 1
            max_nav_error=max(max_nav_error,abs(nav-float(row["nav"])))
            previous=equity
        assert maximum_error<1e-6 and max_nav_error<1e-8,(name,maximum_error,max_nav_error)
        assert abs(earned-s["dividends_earned"])<1e-6 and abs(paid-s["dividends_paid"])<1e-6
        start=date.fromisoformat(s["start"])
        rate=s["xirr"]
        npv=sum(-float(r["deposit"])/(1+rate)**((date.fromisoformat(r["date"])-start).days/365.25) for r in rows)
        npv+=s["final_equity"]/(1+rate)**((date.fromisoformat(s["end"])-start).days/365.25)
        assert abs(npv)<1e-6,(name,npv)
        audit[name]={"status":"passed","max_daily_equity_error_usd":maximum_error,
            "max_nav_error":max_nav_error,"dividend_accrual_and_payment_replayed":True,
            "xirr_npv_residual_usd":npv,"contributions_replayed":contributions}
    (out/"independent_audit.json").write_text(json.dumps(audit,indent=2))
    for src,dst in (("quality.json","data_quality.json"),("manifest.json","source_manifest.json")):
        (out/dst).write_text((data/src).read_text())
    print("Independent dividend, trade, cash, daily NAV and XIRR replay passed:",len(audit),"scenarios")


if __name__ == "__main__":
    main()
