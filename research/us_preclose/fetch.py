"""Download public ETF bars, independent daily quotes and issuer distributions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data/raw"
ISSUER_URLS = {
    "SGOV_issuer.html": "https://www.ishares.com/us/products/314116/ishares-0-3-month-treasury-bond-etf",
    "SPDR_distributions.xlsx": "https://www.ssga.com/library-content/products/fund-data/etfs/us/spdr-etf-historical-distributions.xlsx",
    "SPY_issuer.html": "https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-500-etf-trust-spy",
    "BIL_issuer.html": "https://www.ssga.com/us/en/individual/etfs/state-street-spdr-bloomberg-1-3-month-t-bill-etf-bil"
}


def download(item):
    name, url = item
    RAW.mkdir(parents=True, exist_ok=True)
    path, meta = RAW / name, RAW / (name.rsplit(".", 1)[0] + ".source.json")
    if path.exists() and meta.exists():
        source = json.loads(meta.read_text())
        assert source["url"] == url and hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"]
        return name, source
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as response:
                body = response.read()
            source = {"url": url, "downloaded_at": datetime.now(timezone.utc).isoformat(),
                      "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
            path.write_bytes(body)
            meta.write_text(json.dumps(source, indent=2))
            print(name, len(body), flush=True)
            return name, source
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2**attempt)


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    urls = dict(ISSUER_URLS)
    for sym in ("SPY", "SGOV", "BIL"):
        urls[sym+".parquet"] = ("https://huggingface.co/datasets/syncster-data/historical-market-data/resolve/"
                               + cfg["hf_revision"] + f"/data/stocks/1m/{sym[0]}/{sym}.parquet")
        urls[sym+"_yahoo.json"] = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
                                   "?range=20y&interval=1d&events=div%2Csplits")
    with ThreadPoolExecutor(max_workers=4) as pool:
        sources = dict(pool.map(download, urls.items()))
    (ROOT / "data/manifest.json").write_text(json.dumps({
        "config": cfg, "sources": sources, "dataset_card":
        "https://huggingface.co/datasets/syncster-data/historical-market-data/tree/"+cfg["hf_revision"],
        "price_adjustment": "validated raw ETF bars; no splits inside either configured study period"
    }, indent=2))
    print("DONE", len(sources), "source files", flush=True)


if __name__ == "__main__":
    main()
