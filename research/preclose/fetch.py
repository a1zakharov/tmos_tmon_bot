"""Fetch actual fund minutes and pre-close IMOEX minutes from public MOEX ISS.

No broker token is used. Raw responses are compressed and preserve exact URLs,
download times and SHA-256. Failed runs can resume from the on-disk cache.
"""
from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import csv
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import threading
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RAW = DATA / "raw"
BASE = "https://iss.moex.com/iss"


def records(payload, key):
    block = payload[key]
    return [dict(zip(block["columns"], row)) for row in block["data"]]


class Iss:
    def __init__(self, raw_dir=None):
        self.raw_dir = Path(raw_dir) if raw_dir is not None else RAW
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.used = set()
        self.lock = threading.Lock()

    def get(self, path, **params):
        url = BASE + path + "?" + urllib.parse.urlencode({"iss.meta": "off", **params})
        key = hashlib.sha256(url.encode()).hexdigest()
        cached = self.raw_dir / (key + ".json.gz")
        source = self.raw_dir / (key + ".source.json")
        with self.lock:
            self.used.add(key)
        if cached.exists() and source.exists():
            return json.loads(gzip.decompress(cached.read_bytes()))
        last_error = None
        for attempt in range(6):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "TMOS-TMON-preclose-research/1.0"})
                with urllib.request.urlopen(req, timeout=45) as response:
                    body = response.read()
                result = json.loads(body)
                cached.write_bytes(gzip.compress(body, mtime=0))
                source.write_text(json.dumps({"url": url,
                    "downloaded_at": datetime.now(timezone.utc).isoformat(),
                    "sha256_uncompressed": hashlib.sha256(body).hexdigest()}, indent=2))
                return result
            except Exception as exc:
                last_error = exc
                time.sleep(min(2 ** attempt, 15))
        raise RuntimeError(f"ISS request failed {url}: {last_error}")

    def pages(self, path, key, **params):
        offset = 0
        while True:
            batch = records(self.get(path, **params, start=offset), key)
            if not batch:
                return
            yield from batch
            offset += len(batch)


FIELDS = ["begin", "end", "open", "close", "high", "low", "volume", "value", "board"]


def csv_write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def normalize(row, board):
    if any(row.get(k) is None or row[k] <= 0 for k in ("open", "close", "high", "low")):
        raise ValueError(f"Invalid OHLC in {row}")
    return {**{k: row[k] for k in FIELDS if k != "board"}, "board": board}


def months(start, end):
    first = date.fromisoformat(start).replace(day=1)
    while first.isoformat() <= end:
        finish = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        yield first.strftime("%Y%m"), max(start, first.isoformat()), min(end, finish.isoformat())
        first = (finish + timedelta(days=1)).replace(day=1)


def fund_month(client, sec, meta, month, lo, hi, data_dir=None):
    data_dir = Path(data_dir) if data_dir is not None else DATA
    path = data_dir / "minutes" / f"{sec}_{month}.csv"
    rows = []
    for board in meta:
        start = max(lo, board["history_from"] or lo)
        end = min(hi, board["history_till"] or hi)
        if start > end:
            continue
        endpoint = f"/engines/stock/markets/shares/boards/{board['boardid']}/securities/{sec}/candles.json"
        for row in client.pages(endpoint, "candles", **{"from": start, "till": end, "interval": 1}):
            if start <= row["begin"][:10] <= end and row.get("volume", 0) > 0:
                rows.append(normalize(row, board["boardid"]))
    rows.sort(key=lambda r: r["begin"])
    if len({r["begin"] for r in rows}) != len(rows):
        raise ValueError("Overlapping board history")
    csv_write(path, rows)
    print(f"{sec} {month}: {len(rows)} minute bars", flush=True)
    return {"file": str(path.relative_to(data_dir)), "rows": len(rows),
            "first": rows[0]["begin"] if rows else None, "last": rows[-1]["begin"] if rows else None}


def index_month(client, days, month, lo, hi, data_dir=None):
    data_dir = Path(data_dir) if data_dir is not None else DATA
    rows = []
    endpoint = "/engines/stock/markets/index/boards/SNDX/securities/IMOEX/candles.json"
    for day in days:
        if not lo <= day <= hi:
            continue
        # Includes both historical closing schedules and the diagnostic T-5
        # before the continuous session ends, without downloading all index minutes.
        # TMOS resumed on 30 March 2022, synchronised with the shortened
        # underlying equity session. Later studies are unaffected.
        lo_time, hi_time = ("13:25:00", "14:05:59") if day == "2022-03-30" else ("18:25:00", "19:05:59")
        params = {"from": day + " " + lo_time, "till": day + " " + hi_time, "interval": 1}
        for row in client.pages(endpoint, "candles", **params):
            if row["begin"][:10] == day:
                rows.append(normalize(row, "SNDX"))
    rows.sort(key=lambda r: r["begin"])
    path = data_dir / "minutes" / f"IMOEX_{month}.csv"
    csv_write(path, rows)
    print(f"IMOEX {month}: {len(rows)} pre-close minute bars", flush=True)
    return {"file": str(path.relative_to(data_dir)), "rows": len(rows),
            "first": rows[0]["begin"] if rows else None, "last": rows[-1]["begin"] if rows else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text())
    start, end = cfg["start"], cfg["end"]
    client = Iss()
    metadata, daily_files = {}, []
    for sec in ("TMOS", "TMON"):
        payload = client.get(f"/securities/{sec}.json")
        desc = {r["name"]: r["value"] for r in records(payload, "description")}
        metadata[sec] = [b for b in records(payload, "boards") if b["boardid"] in ("TQTF", "TQBR")]
        rows = []
        for b in metadata[sec]:
            lo, hi = max(start, b["history_from"] or start), min(end, b["history_till"] or end)
            if lo > hi:
                continue
            endpoint = f"/engines/stock/markets/shares/boards/{b['boardid']}/securities/{sec}/candles.json"
            rows.extend(normalize(r, b["boardid"]) for r in client.pages(endpoint, "candles", **{"from": lo, "till": hi, "interval": 24}))
        rows.sort(key=lambda r: r["begin"])
        path = DATA / (sec.lower() + "_daily.csv")
        csv_write(path, rows)
        daily_files.append(path)
        (DATA / (sec.lower() + "_metadata.json")).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"{sec}: {desc.get('ISIN')}; daily {len(rows)}", flush=True)
    endpoint = "/engines/stock/markets/index/boards/SNDX/securities/IMOEX/candles.json"
    warmup = (date.fromisoformat(start) - timedelta(days=45)).isoformat()
    index_daily = [normalize(r, "SNDX") for r in client.pages(endpoint, "candles", **{"from": warmup, "till": end, "interval": 24})]
    index_daily.sort(key=lambda r: r["begin"])
    csv_write(DATA / "imoex_daily.csv", index_daily)
    daily_files.append(DATA / "imoex_daily.csv")
    days = [r["begin"][:10] for r in index_daily if r["begin"][:10] >= start]
    summaries = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = []
        for month, lo, hi in months(start, end):
            for sec in ("TMOS", "TMON"):
                jobs.append(pool.submit(fund_month, client, sec, metadata[sec], month, lo, hi))
            jobs.append(pool.submit(index_month, client, days, month, lo, hi))
        for job in concurrent.futures.as_completed(jobs):
            summaries.append(job.result())
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(), "config": cfg,
                "monthly_files": sorted(summaries, key=lambda r: r["file"]),
                "files": {str(p.relative_to(DATA)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in daily_files + [DATA / r["file"] for r in summaries]},
                "sources": [json.loads((RAW / (key + ".source.json")).read_text()) for key in sorted(client.used)],
                "schedule_sources": ["https://www.moex.com/n97596", "https://www.moex.com/n103622?nt=106",
                                     "https://www.moex.com/s1167"]}
    (DATA / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("DONE", len(summaries), "monthly files;", len(client.used), "ISS requests", flush=True)


if __name__ == "__main__":
    main()
