"""Actual pre-TMON history: TMOS and the VTBM -> LQDT fund, no synthetic returns."""
import argparse
import concurrent.futures
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
spec = importlib.util.spec_from_file_location("common_fetch", ROOT.parent / "preclose/fetch.py")
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text())
    start, end = cfg["start"], cfg["end"]
    client = common.Iss(DATA / "raw")
    metadata, files, segments = {}, [], []
    for sec, isin in (("TMOS", "RU000A101X76"), ("VTBM", cfg["defensive_fund_isin"]),
                      ("LQDT", cfg["defensive_fund_isin"])):
        payload = client.get(f"/securities/{sec}.json")
        desc = {r["name"]: r["value"] for r in common.records(payload, "description")}
        assert desc["ISIN"] == isin, (sec, desc)
        metadata[sec] = [b for b in common.records(payload, "boards") if b["boardid"] in ("TQTF", "TQBR")]
        (DATA / (sec.lower() + "_metadata.json")).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        rows = []
        for b in metadata[sec]:
            lo, hi = max(start, b["history_from"] or start), min(end, b["history_till"] or end)
            if lo > hi:
                continue
            endpoint = f"/engines/stock/markets/shares/boards/{b['boardid']}/securities/{sec}/candles.json"
            part = [common.normalize(r, b["boardid"]) for r in client.pages(endpoint, "candles", **{"from": lo, "till": hi, "interval": 24})]
            rows.extend(part)
            segments.append({"ticker": sec, "board": b["boardid"], "from": lo, "till": hi,
                             "isin": isin, "daily_rows": len(part)})
        path = DATA / (sec.lower() + "_daily.csv")
        common.csv_write(path, sorted(rows, key=lambda r: r["begin"]))
        files.append(path)
        print(sec, "daily", len(rows), flush=True)
    endpoint = "/engines/stock/markets/index/boards/SNDX/securities/IMOEX/candles.json"
    warmup = (date.fromisoformat(start) - timedelta(days=45)).isoformat()
    rows = [common.normalize(r, "SNDX") for r in client.pages(endpoint, "candles", **{"from": warmup, "till": end, "interval": 24})]
    rows.sort(key=lambda r: r["begin"])
    path = DATA / "imoex_daily.csv"
    common.csv_write(path, rows)
    files.append(path)
    days = [r["begin"][:10] for r in rows if r["begin"][:10] >= start]
    summaries = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = []
        for month, lo, hi in common.months(start, end):
            for sec in ("TMOS", "VTBM", "LQDT"):
                jobs.append(pool.submit(common.fund_month, client, sec, metadata[sec], month, lo, hi, DATA))
            jobs.append(pool.submit(common.index_month, client, days, month, lo, hi, DATA))
        for job in concurrent.futures.as_completed(jobs):
            summaries.append(job.result())
    # Preserve the actual source ticker in separate files. The simulator merges
    # VTBM and LQDT by time, asserting that the two aliases never overlap.
    files += [DATA / r["file"] for r in summaries]
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(), "config": cfg,
                "segments": segments, "monthly_files": sorted(summaries, key=lambda r: r["file"]),
                "files": {str(p.relative_to(DATA)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                "sources": [json.loads((client.raw_dir / (k + ".source.json")).read_text()) for k in sorted(client.used)],
                "references": ["https://www.moex.com/n49955", "https://www.moex.com/n46282",
                               "https://www.cbr.ru/press/pr/?file=29032022_195220sup_meas29032022_185602.htm"]}
    (DATA / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("DONE", len(summaries), "files;", len(client.used), "requests", flush=True)


if __name__ == "__main__":
    main()
