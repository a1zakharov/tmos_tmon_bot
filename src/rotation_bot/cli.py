import argparse
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from .config import Config
from .domain import Account, Book, BotError, Frame, Instrument, PreviousDay, Session, Snapshot, timestamp
from .providers import MoexSignalFeed, ReadOnlyTInvest, observe_frame
from .service import check
from .state import Store

ROOT = Path(__file__).resolve().parents[2]


def frame_from_fixture(path):
    raw = json.loads(path.read_text())
    instruments = {t: Instrument(t, r["uid"], r["figi"], r["exchange"], r["lot"], Decimal(r["tick"]))
                   for t, r in raw["instruments"].items()}
    a = raw["account"]
    account = Account(a["id"], a["type"], a["status"], Decimal(a["available_rub"]),
                      a["free_units"], a.get("blocked_units", {}), a.get("active_pair_orders", 0))
    return Frame(timestamp(raw["now"]),
                 Session(date.fromisoformat(raw["session"]["day"]), timestamp(raw["session"]["start"]), timestamp(raw["session"]["end"])),
                 PreviousDay(date.fromisoformat(raw["previous"]["day"]), Decimal(raw["previous"]["high"]), Decimal(raw["previous"]["low"])),
                 Snapshot("IMOEX", Decimal(raw["snapshot"]["price"]), timestamp(raw["snapshot"]["observed_at"])),
                 account, instruments,
                 {t: Book(Decimal(b["bid"]), Decimal(b["ask"]), timestamp(b["observed_at"])) for t, b in raw["books"].items()},
                 raw["trading_statuses"], raw.get("verified_realtime", False))


def main():
    parser = argparse.ArgumentParser(description="Read-only TMOS/TMON pre-close bot scaffold")
    parser.add_argument("command", choices=("demo", "observe"))
    parser.add_argument("--config", type=Path, default=ROOT / "config.example.toml")
    parser.add_argument("--fixture", type=Path, default=ROOT / "fixtures/preclose.json")
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    store = None
    try:
        cfg = Config.load(args.config)
        if args.command == "demo":
            frame = frame_from_fixture(args.fixture)
        else:
            account_id = os.environ.get("TINVEST_ACCOUNT_ID", "").strip()
            if not account_id or account_id.startswith("replace_"):
                raise BotError("Pin TINVEST_ACCOUNT_ID to your IIS locally")
            frame = observe_frame(ReadOnlyTInvest(os.environ.get("TINVEST_TOKEN", "")),
                                  MoexSignalFeed(), account_id, cfg)
        if isinstance(frame, dict):
            result = frame
        else:
            state = args.state or ROOT / ("data/demo.sqlite3" if args.command == "demo" else cfg.state_file)
            store = Store(state)
            result = check(frame, cfg, store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (BotError, KeyError, ValueError) as exc:
        # Structured errors contain no token or full account identifier.
        print(json.dumps({"status": "blocked", "reason": str(exc), "orders_submitted": 0}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
    finally:
        if store:
            store.close()
