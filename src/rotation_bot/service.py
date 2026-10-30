from datetime import timedelta
from decimal import Decimal

from .domain import BotError, Frame, MSK, Target, infer_initial_target
from .execution import plan_rotation
from .schedule import decision_window, window_status
from .state import scope_for
from .strategy import target_for_snapshot


def execution_blockers(frame, cfg):
    blockers = []
    if not frame.verified_realtime:
        blockers.append("signal_feed_realtime_not_verified")
    for ticker, ins in frame.instruments.items():
        status = frame.trading_statuses.get(ticker, {})
        if not status.get("apiTradeAvailableFlag") or not status.get("limitOrderAvailableFlag"):
            blockers.append(f"{ticker}:orders_unavailable")
        # Auction fills cannot be treated as immediate continuous-session fills.
        if status.get("tradingStatus") != "SECURITY_TRADING_STATUS_NORMAL_TRADING":
            blockers.append(f"{ticker}:not_continuous_trading")
        book = frame.books[ticker]
        age = (frame.now - book.observed_at).total_seconds()
        if not 0 <= age <= cfg.max_book_age_seconds:
            blockers.append(f"{ticker}:stale_or_future_orderbook")
        if book.bid <= 0 or book.ask < book.bid:
            blockers.append(f"{ticker}:invalid_orderbook")
        elif (book.ask - book.bid) / book.bid * 10000 > Decimal(cfg.max_spread_bps):
            blockers.append(f"{ticker}:wide_spread")
    return blockers


def check(frame: Frame, cfg, store):
    cfg.validate()
    scope = scope_for(frame.account.account_id)
    day = frame.session.day.isoformat()
    existing = store.existing(scope, day)
    if existing:
        return {"status": "already_decided", "decision": existing, "orders_submitted": 0}
    status = window_status(frame.now, frame.session, cfg.minutes_before_close, cfg.decision_window_seconds)
    if status != "due":
        lo, hi = decision_window(frame.session, cfg.minutes_before_close, cfg.decision_window_seconds)
        return {"status": status, "check_at": lo.isoformat(), "window_ends": hi.isoformat(), "orders_submitted": 0}
    account = frame.account
    if account.account_type != "ACCOUNT_TYPE_TINKOFF_IIS" or account.status != "ACCOUNT_STATUS_OPEN":
        raise BotError("The pinned account must be an open IIS")
    if account.available_rub < 0 or any(q < 0 for q in account.free_units.values()):
        raise BotError("Borrowed cash/short positions in the managed pair are not allowed")
    if account.limits_loading or account.active_pair_orders or any(account.blocked_units.values()):
        raise BotError("Positions are loading or the managed pair has blocked units/active orders")
    if set(frame.instruments) != {"TMOS", "TMON"}:
        raise BotError("The managed pair must be exactly TMOS/TMON")
    for ins in frame.instruments.values():
        if not ins.for_iis or not ins.api_available or ins.lot <= 0 or ins.tick <= 0:
            raise BotError("Invalid or unavailable instrument on IIS")
    if frame.now.astimezone(MSK).date() != frame.session.day:
        raise BotError("Broker trade date does not match the local trading day")
    if frame.previous.day >= frame.session.day:
        raise BotError("Reference range must come from a previous completed day")
    if (frame.session.day - frame.previous.day).days > 14:
        raise BotError("Reference daily range is too old; verify the exchange calendar")
    age = frame.now - frame.snapshot.observed_at
    if age < timedelta(0) or age > timedelta(seconds=cfg.max_quote_age_seconds):
        raise BotError("IMOEX snapshot is stale or timestamped in the future")
    if frame.snapshot.observed_at.astimezone(MSK).date() != frame.session.day:
        raise BotError("IMOEX quote is from a different trading day")
    last = store.previous_target(scope, day)
    initial = Target(last) if last else infer_initial_target(account)
    target, reason = target_for_snapshot(frame.previous, frame.snapshot, initial)
    blockers = execution_blockers(frame, cfg)
    # Even a validated plan has no path to order submission in this release.
    try:
        plan = plan_rotation(frame, target, cfg)
    except BotError:
        plan = {"kind": "await_executable_market", "target": target.value}
        blockers.append("plan_needs_fresh_executable_prices_or_whole_lots")
    payload = {"strategy": "preclose-v1", "trade_date": day,
               "evaluated_at": frame.now.isoformat(), "quote_at": frame.snapshot.observed_at.isoformat(),
               "quote_price": str(frame.snapshot.price), "previous_day": frame.previous.day.isoformat(),
               "previous_high": str(frame.previous.high), "previous_low": str(frame.previous.low),
               "previous_target": initial.value, "target": target.value, "reason": reason,
               "plan": plan, "execution_blockers": blockers + ["live_execution_not_implemented"],
               "orders_submitted": 0}
    payload, created = store.record_once(scope, day, payload)
    return {"status": "decision_recorded" if created else "already_decided", "decision": payload,
            "orders_submitted": 0}
