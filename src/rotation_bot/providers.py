"""Read-only adapters. The authenticated transport cannot call order mutations."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib import error, parse, request

from .domain import (Account, Book, BotError, Frame, Instrument, MSK, PreviousDay,
                     Snapshot, quotation, timestamp)
from .schedule import common_main_session, sessions_from_day, window_status, decision_window
from .state import scope_for


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ReadOnlyTInvest:
    BASE = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1."
    ALLOWED = {
        "UsersService/GetAccounts", "InstrumentsService/FindInstrument",
        "InstrumentsService/GetInstrumentBy", "InstrumentsService/TradingSchedules",
        "OperationsService/GetPositions", "OrdersService/GetOrders",
        "MarketDataService/GetOrderBook", "MarketDataService/GetTradingStatus",
    }

    def __init__(self, token: str, timeout: int = 15):
        if not token.strip() or token.startswith("replace_"):
            raise BotError("Set a read-only TINVEST_TOKEN locally")
        self._token, self.timeout = token.strip(), timeout
        self._opener = request.build_opener(NoRedirect())

    def call(self, method: str, payload: dict) -> dict:
        if method not in self.ALLOWED:
            raise BotError("Authenticated transport forbids this API method")
        req = request.Request(self.BASE + method, data=json.dumps(payload).encode(),
                              headers={"Authorization": "Bearer " + self._token,
                                       "Content-Type": "application/json", "x-app-name": "tmos-tmon-observer"},
                              method="POST")
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                return json.load(response)
        except error.HTTPError as exc:
            # No request headers, body, token or untrusted server response in logs.
            raise BotError(f"T-Invest read request failed: HTTP {exc.code}") from None
        except (error.URLError, TimeoutError, json.JSONDecodeError):
            raise BotError("T-Invest read request failed; check connectivity/schema") from None

    def account(self, account_id: str) -> dict:
        result = self.call("UsersService/GetAccounts", {})
        matches = [a for a in result.get("accounts", []) if a.get("id") == account_id]
        if len(matches) != 1:
            raise BotError("The explicitly pinned account is not available to this token")
        a = matches[0]
        if a.get("type") != "ACCOUNT_TYPE_TINKOFF_IIS" or a.get("status") != "ACCOUNT_STATUS_OPEN":
            raise BotError("The pinned account must be an open IIS")
        return a

    def instrument(self, ticker: str, class_code: str) -> Instrument:
        result = self.call("InstrumentsService/FindInstrument", {"query": ticker, "apiTradeAvailableFlag": True})
        matches = [r for r in result.get("instruments", [])
                   if r.get("ticker") == ticker
                   and (r.get("classCode") == class_code if class_code else r.get("classCode") in ("TQBR", "TQTF"))
                   and r.get("instrumentType") == "etf" and r.get("apiTradeAvailableFlag")
                   and r.get("forIisFlag")]
        if len(matches) != 1:
            raise BotError(f"Resolve exactly one API/IIS-available ETF for {ticker}/{class_code}")
        detail = self.call("InstrumentsService/GetInstrumentBy",
                           {"idType": "INSTRUMENT_ID_TYPE_UID", "id": matches[0]["uid"]})["instrument"]
        if detail.get("currency", "").lower() != "rub" or detail.get("otcFlag"):
            raise BotError("Managed funds must be exchange-traded RUB instruments")
        if not detail.get("buyAvailableFlag") or not detail.get("sellAvailableFlag"):
            raise BotError(f"Both buy and sell must be available for {ticker}")
        return Instrument(ticker, detail["uid"], detail["figi"], detail["exchange"],
                          int(detail["lot"]), quotation(detail["minPriceIncrement"]),
                          bool(detail.get("forIisFlag")), bool(detail.get("apiTradeAvailableFlag")))

    def day_sessions(self, exchange: str, today):
        start = datetime.combine(today, datetime.min.time(), tzinfo=MSK)
        result = self.call("InstrumentsService/TradingSchedules",
                           {"exchange": exchange, "from": start.astimezone(UTC).isoformat(),
                            "to": (start + timedelta(days=2)).astimezone(UTC).isoformat()})
        days = [d for e in result.get("exchanges", []) if e.get("exchange") == exchange
                for d in e.get("days", []) if timestamp(d["date"]).date() == today]
        if len(days) != 1:
            raise BotError("Broker schedule for the selected day is unavailable")
        return sessions_from_day(days[0])

    def positions(self, account_meta, instruments):
        account_id = account_meta["id"]
        raw = self.call("OperationsService/GetPositions", {"accountId": account_id})
        orders = self.call("OrdersService/GetOrders", {"accountId": account_id}).get("orders", [])
        money = sum((quotation(x) for x in raw.get("money", []) if x.get("currency", "").lower() == "rub"), Decimal(0))
        blocked_money = sum((quotation(x) for x in raw.get("blocked", []) if x.get("currency", "").lower() == "rub"), Decimal(0))
        free, blocked = {}, {}
        for ticker, ins in instruments.items():
            rows = [r for r in raw.get("securities", []) if r.get("instrumentUid") == ins.uid or r.get("figi") == ins.figi]
            # GetPositions.balance is already the unblocked units; never subtract
            # PositionsSecurities.blocked from balance a second time.
            free[ticker] = sum(int(r.get("balance", 0)) for r in rows)
            blocked[ticker] = sum(int(r.get("blocked", 0)) for r in rows)
            if any(r.get("exchangeBlocked") for r in rows):
                blocked[ticker] = max(1, blocked[ticker])
        ids = {i.uid for i in instruments.values()} | {i.figi for i in instruments.values()}
        active = sum(r.get("instrumentUid") in ids or r.get("figi") in ids for r in orders)
        return Account(account_id, account_meta["type"], account_meta["status"],
                       money - blocked_money, free, blocked, active, bool(raw.get("limitsLoadingInProgress")))

    def book(self, ins):
        result = self.call("MarketDataService/GetOrderBook", {"instrumentId": ins.uid, "depth": 1})
        if not result.get("bids") or not result.get("asks") or not result.get("orderbookTs"):
            raise BotError(f"No timestamped two-sided exchange orderbook for {ins.ticker}")
        return Book(quotation(result["bids"][0]["price"]), quotation(result["asks"][0]["price"]),
                    timestamp(result["orderbookTs"]))

    def trading_status(self, ins):
        return self.call("MarketDataService/GetTradingStatus", {"instrumentId": ins.uid})


class MoexSignalFeed:
    """Timestamped completed minutes, with no realtime-delivery assumption."""
    BASE = "https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/securities/IMOEX"

    def _read(self, suffix, params):
        url = self.BASE + suffix + "?" + parse.urlencode({"iss.meta": "off", **params})
        try:
            with request.urlopen(url, timeout=15) as response:
                return json.load(response)
        except (error.URLError, TimeoutError, json.JSONDecodeError):
            raise BotError("MOEX signal data unavailable") from None

    def previous_day(self, today):
        block = self._read("/candles.json", {"interval": 24,
                           "from": (today - timedelta(days=40)).isoformat(),
                           "till": (today - timedelta(days=1)).isoformat()})["candles"]
        rows = [dict(zip(block["columns"], r)) for r in block["data"]]
        rows = [r for r in rows if r["begin"][:10] < today.isoformat()]
        if not rows:
            raise BotError("No previous completed IMOEX day")
        row = max(rows, key=lambda r: r["begin"])
        return PreviousDay(datetime.fromisoformat(row["begin"]).date(), Decimal(str(row["high"])), Decimal(str(row["low"])))

    def snapshot(self, cutoff: datetime) -> Snapshot | None:
        if cutoff.tzinfo is None:
            raise BotError("Signal cutoff must have a timezone")
        cutoff = cutoff.astimezone(MSK)
        block = self._read("/candles.json", {
            "interval": 1, "from": (cutoff - timedelta(minutes=11)).strftime("%Y-%m-%d %H:%M:%S"),
            "till": cutoff.strftime("%Y-%m-%d %H:%M:%S")})["candles"]
        candidates = []
        for values in block["data"]:
            row = dict(zip(block["columns"], values))
            begin = datetime.fromisoformat(row["begin"]).replace(tzinfo=MSK)
            end = datetime.fromisoformat(row["end"]).replace(tzinfo=MSK)
            if (begin.date() == cutoff.date() and begin <= end < cutoff
                    and begin + timedelta(minutes=1) <= cutoff):
                price = Decimal(str(row["close"]))
                if not price.is_finite() or price <= 0 or end - begin > timedelta(seconds=59):
                    raise BotError("Invalid completed IMOEX minute")
                candidates.append(Snapshot("IMOEX", price, end))
        return max(candidates, key=lambda s: s.observed_at) if candidates else None


def observe_frame(client, feed, account_id, cfg, store=None, clock=None):
    """Return a validated input frame, or a scheduling result before quote polling."""
    clock = clock or (lambda: datetime.now(UTC))
    now = clock()
    meta = client.account(account_id)
    instruments = {t: client.instrument(t, cfg.class_code) for t in ("TMOS", "TMON")}
    sessions = {t: client.day_sessions(i.exchange, now.astimezone(MSK).date()) for t, i in instruments.items()}
    common = common_main_session(sessions["TMOS"], sessions["TMON"])
    if not common:
        return {"status": "no_common_session", "orders_submitted": 0}
    status = window_status(now, common, cfg.minutes_before_close, cfg.decision_window_seconds)
    prior = store.latest(scope_for(account_id), common.day.isoformat()) if store else None
    if status != "due" and not prior:
        lo, hi = decision_window(common, cfg.minutes_before_close, cfg.decision_window_seconds)
        return {"status": status, "check_at": lo.isoformat(), "window_ends": hi.isoformat(), "orders_submitted": 0}
    account = client.positions(meta, instruments)
    needs_signal = status == "due" and not (prior and prior["trade_date"] == common.day.isoformat())
    cutoff, _ = decision_window(common, cfg.minutes_before_close, cfg.decision_window_seconds)
    previous = feed.previous_day(common.day) if needs_signal else None
    snapshot = feed.snapshot(cutoff) if needs_signal else None
    books, statuses = {}, {}
    for t, ins in instruments.items():
        # An unavailable execution market must not erase a valid daily signal.
        try:
            books[t] = client.book(ins)
        except BotError:
            pass
        try:
            statuses[t] = client.trading_status(ins)
        except BotError:
            pass
    return Frame(clock(), common, previous, snapshot, account, instruments, books,
                 statuses, cfg.signal_feed_verified_realtime)
