"""Replay exported last-quote research signals through the production controller.

No market/API calls; temporary SQLite state only. Early LQDT targets are mapped
to the TMON defensive role solely to compare identical signal rules.
"""
import csv
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "src"))
from rotation_bot.config import Config
from rotation_bot.domain import Account, Book, Frame, Instrument, MSK, PreviousDay, Session, Snapshot
from rotation_bot.service import check
from rotation_bot.state import Store


def main():
    results = {}
    with tempfile.TemporaryDirectory() as temp:
        store = Store(Path(temp) / "replay.sqlite3")
        try:
            for study in ("preclose_early", "preclose"):
                path = ROOT / study / "results/last_quote_signals.csv"
                with path.open() as f:
                    signals = list(csv.DictReader(f))
                changes = 0
                for row in signals:
                    day = date.fromisoformat(row["date"])
                    cutoff = datetime.fromisoformat(row["cutoff"]).replace(tzinfo=MSK)
                    now = cutoff + timedelta(seconds=5)
                    instruments = {s: Instrument(s, s+"-uid", s+"-figi", "MOEX", 1, Decimal(".0001"))
                                   for s in ("TMOS", "TMON")}
                    frame = Frame(now, Session(day, datetime.combine(day, time(10), MSK),
                                               cutoff+timedelta(minutes=5)),
                                  PreviousDay(date.fromisoformat(row["previous_date"]),
                                              Decimal(row["previous_high"]), Decimal(row["previous_low"])),
                                  Snapshot("IMOEX", Decimal(row["snapshot"]),
                                           datetime.fromisoformat(row["snapshot_at"]).replace(tzinfo=MSK)),
                                  Account("replay-"+study, "ACCOUNT_TYPE_TINKOFF_IIS", "ACCOUNT_STATUS_OPEN",
                                          Decimal(1000), {"TMOS": 1000, "TMON": 0}, {}),
                                  instruments, {s: Book(Decimal(1), Decimal(1), now) for s in instruments},
                                  {s: {"apiTradeAvailableFlag": True, "limitOrderAvailableFlag": True,
                                       "tradingStatus": "SECURITY_TRADING_STATUS_NORMAL_TRADING"} for s in instruments})
                    decision = check(frame, Config(), store)["decision"]
                    expected = row["target_after"].replace("LQDT", "TMON")
                    assert decision["target"] == expected, (study, row["date"], decision["target"], expected)
                    assert decision["reason"] == row["status"], (study, row["date"], decision["reason"], row["status"])
                    assert decision["quote_age_seconds_at_cutoff"] == float(row["age_seconds"])
                    changes += decision["target"] != decision["previous_target"]
                results[study] = {"decisions_matched": len(signals), "target_changes": changes,
                                  "targets_reasons_and_quote_ages_match": True}
        finally:
            store.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
