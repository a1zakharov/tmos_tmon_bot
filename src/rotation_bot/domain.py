from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")


class BotError(Exception):
    pass


class Target(StrEnum):
    TMOS = "TMOS"
    TMON = "TMON"


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise BotError("Timestamp must have an explicit timezone")
    return result


def quotation(value: dict) -> Decimal:
    return Decimal(str(value.get("units", "0"))) + Decimal(str(value.get("nano", 0))) / Decimal(10**9)


@dataclass(frozen=True)
class PreviousDay:
    day: date
    high: Decimal
    low: Decimal

    def validate(self):
        if self.low <= 0 or self.high < self.low:
            raise BotError("Invalid previous-day range")


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    price: Decimal
    observed_at: datetime


@dataclass(frozen=True)
class Session:
    day: date
    start: datetime
    end: datetime
    kind: str = "main"


@dataclass(frozen=True)
class Instrument:
    ticker: str
    uid: str
    figi: str
    exchange: str
    lot: int
    tick: Decimal
    for_iis: bool = True
    api_available: bool = True


@dataclass(frozen=True)
class Book:
    bid: Decimal
    ask: Decimal
    observed_at: datetime


@dataclass(frozen=True)
class Account:
    account_id: str
    account_type: str
    status: str
    available_rub: Decimal
    free_units: dict[str, int]
    blocked_units: dict[str, int]
    active_pair_orders: int = 0
    limits_loading: bool = False


@dataclass(frozen=True)
class Frame:
    now: datetime
    session: Session
    previous: PreviousDay | None
    snapshot: Snapshot | None
    account: Account
    instruments: dict[str, Instrument]
    books: dict[str, Book]
    trading_statuses: dict[str, dict]
    verified_realtime: bool = False


def infer_initial_target(account: Account) -> Target:
    held = [t for t in Target if account.free_units.get(t.value, 0) > 0]
    if len(held) == 1:
        return held[0]
    if not held:
        # Does not cause a purchase unless invest_idle_cash is explicitly enabled.
        return Target.TMOS
    raise BotError("First start with both funds requires an explicit bootstrap decision")
