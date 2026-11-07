"""Normalize raw minutes and corporate actions, preserving gaps and real prices."""
from datetime import date, datetime, timedelta
import html
import json
from pathlib import Path
import sys
import hashlib

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from openpyxl import load_workbook
import exchange_calendars as xc

ROOT = Path(__file__).resolve().parent
DATA, RAW = ROOT / "data", ROOT / "data/raw"
NY = "America/New_York"


def issuer_dividends():
    cfg = json.loads((ROOT / "config.json").read_text())
    rows = []
    book = load_workbook(RAW / "SPDR_distributions.xlsx", read_only=True, data_only=True)
    for r in book.active.iter_rows(min_row=2, values_only=True):
        if r[1] in ("SPY", "BIL"):
            ex = datetime.strptime(str(r[3]).strip(), "%m/%d/%Y").date()
            pay = datetime.strptime(str(r[5]).strip(), "%m/%d/%Y").date()
            amount = sum(float(str(v).strip()) for v in r[6:9] if v and str(v).strip())
            if amount:
                rows.append({"symbol": r[1], "ex_date": ex.isoformat(), "pay_date": pay.isoformat(), "amount": amount})
    text = html.unescape((RAW / "SGOV_issuer.html").read_text())
    columns = {}
    for key in ("exDate", "payableDate", "totalDistribution"):
        marker = '"'+key+'":'
        index = text.index(marker) + len(marker)
        obj, _ = json.JSONDecoder().raw_decode(text[index:])
        columns[key] = obj["value"]
    assert len({len(v) for v in columns.values()}) == 1
    for ex, pay, amount in zip(columns["exDate"], columns["payableDate"], columns["totalDistribution"]):
        if float(amount):
            rows.append({"symbol": "SGOV", "ex_date": datetime.strptime(str(ex), "%Y%m%d").date().isoformat(),
                         "pay_date": datetime.strptime(str(pay), "%Y%m%d").date().isoformat(), "amount": float(amount)})
    # Do not import pre-study corporate actions. In particular the issuer
    # workbook has an inconsistent BIL ex/pay date in 2008, outside our horizon.
    rows = [r for r in rows if r["ex_date"] >= (min(cfg["periods"].values()) if r["symbol"] == "SPY"
                                              else cfg["periods"][r["symbol"]])]
    assert len({(r["symbol"], r["ex_date"]) for r in rows}) == len(rows)
    assert all(r["ex_date"] <= r["pay_date"] and r["amount"] > 0 for r in rows)
    return sorted(rows, key=lambda r: (r["ex_date"], r["symbol"]))


def yahoo_daily(symbol):
    data = json.loads((RAW / (symbol+"_yahoo.json")).read_text())["chart"]["result"][0]
    quotes = data["indicators"]["quote"][0]
    rows = []
    for i, ts in enumerate(data["timestamp"]):
        day = pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(NY).date().isoformat()
        if any(quotes[k][i] is None for k in ("open", "high", "low", "close")):
            continue
        rows.append({"date": day, **{k: quotes[k][i] for k in ("open", "high", "low", "close", "volume")}})
    splits = list(data.get("events", {}).get("splits", {}).values())
    return pd.DataFrame(rows).set_index("date"), splits


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    manifest = json.loads((DATA / "manifest.json").read_text())
    for name, source in manifest["sources"].items():
        assert hashlib.sha256((RAW/name).read_bytes()).hexdigest() == source["sha256"], name
    calendar = xc.get_calendar("XNYS", start="2015-01-01", end="2026-12-31")
    schedule = calendar.schedule.loc["2016-01-01":cfg["end"], ["open", "close"]].copy()
    schedule.index = schedule.index.strftime("%Y-%m-%d")
    schedule.to_csv(DATA / "schedule.csv", index_label="date")
    dividends = issuer_dividends()
    pd.DataFrame(dividends).to_csv(DATA / "dividends.csv", index=False)
    quality = {"hf_revision": cfg["hf_revision"], "symbols": {}, "calendar_package": xc.__version__}
    normalized = []
    for symbol in ("SPY", "SGOV", "BIL"):
        print("Preparing", symbol, flush=True)
        # Avoid optional dataset discovery and make IO deterministic in containers.
        frame = pq.ParquetFile(RAW/(symbol+".parquet")).read(use_threads=False).to_pandas(use_threads=False)
        assert set(frame.symbol) == {symbol}
        frame = frame.sort_values("ts")
        frame["date"] = frame.ts.dt.tz_convert(NY).dt.strftime("%Y-%m-%d")
        frame = frame.join(schedule.rename(columns={"open":"open_session","close":"close_session"}), on="date")
        before = len(frame)
        frame = frame[(frame.ts >= frame.open_session) & (frame.ts+pd.Timedelta(minutes=1) <= frame.close_session)].copy()
        outside = before-len(frame)
        assert not frame.ts.duplicated().any(), symbol
        assert (frame.ts.dt.second == 0).all() and (frame.ts.dt.microsecond == 0).all(), symbol
        values = frame[["open", "high", "low", "close", "volume"]].to_numpy()
        invalid = (~np.isfinite(values).all(axis=1) | (values[:, :4] <= 0).any(axis=1)
                   | (values[:,4] <= 0) | (frame.high < frame[["open","close"]].max(axis=1))
                   | (frame.low > frame[["open","close"]].min(axis=1)) | (frame.high < frame.low))
        bad = int(invalid.sum())
        frame = frame[~invalid].copy()
        daily, splits = yahoo_daily(symbol)
        validation_daily = daily
        # Some very early fund sessions have real minutes but no Yahoo daily
        # candle. Aggregate ONLY their observed regular-session trades.
        fallback = frame.groupby("date").agg(open=("open","first"), high=("high","max"),
                    low=("low","min"), close=("close","last"), volume=("volume","sum"))
        fallback = fallback.loc[~fallback.index.isin(daily.index)]
        fallback_dates = list(fallback.index)
        if len(fallback):
            daily = pd.concat([daily,fallback]).sort_index()
        start = min(cfg["periods"].values()) if symbol == "SPY" else cfg["periods"][symbol]
        for split in splits:
            d = pd.Timestamp(split["date"], unit="s", tz="UTC").tz_convert(NY).date().isoformat()
            if start <= d <= cfg["end"]:
                raise ValueError("Corporate action inside configured study requires explicit share conversion: "+str(split))
        daily.to_csv(DATA/(symbol+"_daily.csv"))
        # Compare raw minute closes with independently sourced daily closing prices.
        last = frame.groupby("date").tail(1).set_index("date")
        compare = last[["close"]].join(validation_daily[["close"]].rename(columns={"close":"close_daily"})).dropna()
        compare = compare.loc[start:cfg["end"]]
        errors = (compare.close/compare.close_daily-1).abs()
        if float(errors.median()) > .002:
            raise ValueError("Raw/split/dividend price basis mismatch for "+symbol)
        columns = frame[["ts", "open", "high", "low", "close", "volume"]].copy()
        columns.to_csv(DATA/(symbol+"_minutes.csv"), index=False)
        normalized += [DATA/(symbol+"_daily.csv"), DATA/(symbol+"_minutes.csv")]
        issuer = {r["ex_date"]:r["amount"] for r in dividends if r["symbol"] == symbol}
        yahoo = json.loads((RAW/(symbol+"_yahoo.json")).read_text())["chart"]["result"][0]
        ydiv = {pd.Timestamp(v["date"], unit="s", tz="UTC").tz_convert(NY).date().isoformat():v["amount"]
                for v in yahoo.get("events", {}).get("dividends", {}).values()}
        diffs = [abs(a-ydiv[d]) for d,a in issuer.items() if start <= d <= cfg["end"] and d in ydiv]
        quality["symbols"][symbol] = {"raw_rows": before, "regular_session_rows": len(frame),
            "outside_session_removed": outside, "invalid_rows_removed": bad,
            "first_minute": str(frame.ts.min()), "last_minute": str(frame.ts.max()),
            "daily_close_relative_error_median": float(errors.median()),
            "daily_close_relative_error_p99": float(errors.quantile(.99)),
            "close_difference_over_1pct_dates": list(errors[errors>.01].index),
            "dividend_crosscheck_max_difference_usd": max(diffs, default=0),
            "issuer_dividend_events_in_period": sum(start <= d <= cfg["end"] for d in issuer),
            "daily_candles_aggregated_from_observed_minutes": fallback_dates,
            "splits_outside_period": splits}
        print(symbol, len(frame), "RTH minutes; median closing-price discrepancy",float(errors.median()),flush=True)
    normalized += [DATA/"schedule.csv", DATA/"dividends.csv"]
    manifest["normalized_files"] = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in normalized}
    (DATA/"manifest.json").write_text(json.dumps(manifest,indent=2))
    (DATA/"quality.json").write_text(json.dumps(quality,indent=2))


if __name__ == "__main__":
    main()
