import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path


def scope_for(account_id: str) -> str:
    return hashlib.sha256((account_id + ":TMOS:TMON:IMOEX:preclose-v1").encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        os.chmod(path, 0o600)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS decisions(
          scope TEXT NOT NULL, trade_date TEXT NOT NULL, target TEXT NOT NULL,
          intent_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
          PRIMARY KEY(scope, trade_date))""")

    def close(self):
        self.connection.close()

    def previous_target(self, scope, before_day):
        row = self.connection.execute("SELECT target FROM decisions WHERE scope=? AND trade_date<? ORDER BY trade_date DESC LIMIT 1",
                                      (scope, before_day)).fetchone()
        return row[0] if row else None

    def existing(self, scope, day):
        row = self.connection.execute("SELECT payload FROM decisions WHERE scope=? AND trade_date=?",
                                      (scope, day)).fetchone()
        return json.loads(row[0]) if row else None

    def record_once(self, scope, day, payload):
        """Concurrent workers atomically agree on one immutable daily snapshot."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.existing(scope, day)
            if existing:
                self.connection.execute("COMMIT")
                return existing, False
            intent_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}:{day}:preclose-v1"))
            payload = {**payload, "intent_id": intent_id}
            self.connection.execute("INSERT INTO decisions VALUES(?,?,?,?,?)",
                                    (scope, day, payload["target"], intent_id,
                                     json.dumps(payload, ensure_ascii=False)))
            self.connection.execute("COMMIT")
            return payload, True
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
