from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import tomllib

from .domain import BotError


@dataclass(frozen=True)
class Config:
    mode: str = "observe"
    state_file: str = "data/state.sqlite3"
    signal_symbol: str = "IMOEX"
    risk_ticker: str = "TMOS"
    defensive_ticker: str = "TMON"
    class_code: str = ""
    minutes_before_close: int = 5
    decision_window_seconds: int = 60
    max_quote_age_seconds: int = 600
    max_book_age_seconds: int = 30
    max_spread_bps: str = "30"
    max_limit_deviation_bps: str = "10"
    cash_reserve_rub: str = "1000"
    invest_idle_cash: bool = False
    # Legacy input retained so existing config files still load. The last-quote
    # model validates the minute's exchange timestamp instead of this flag.
    signal_feed_verified_realtime: bool = False

    def validate(self):
        if self.mode != "observe":
            raise BotError("This scaffold supports observe mode only; real execution is not implemented")
        if (self.risk_ticker, self.defensive_ticker, self.signal_symbol) != ("TMOS", "TMON", "IMOEX"):
            raise BotError("This strategy version is pinned to TMOS/TMON with an IMOEX signal")
        if self.minutes_before_close != 5:
            raise BotError("This strategy version is fixed at five minutes before the common session close")
        if not 0 < self.decision_window_seconds < 300:
            raise BotError("Invalid decision window")
        if self.max_quote_age_seconds != 600:
            raise BotError("The last-quote model requires max_quote_age_seconds = 600; update your local config")
        if self.max_book_age_seconds <= 0:
            raise BotError("Data age limits must be positive")
        for value in (self.max_spread_bps, self.max_limit_deviation_bps, self.cash_reserve_rub):
            if Decimal(value) < 0:
                raise BotError("Money/risk limits cannot be negative")
        if Decimal(self.max_limit_deviation_bps) >= 10000:
            raise BotError("Limit price deviation must be less than 100 percent")

    @classmethod
    def load(cls, path: Path):
        result = cls(**tomllib.loads(path.read_text()))
        result.validate()
        return result
