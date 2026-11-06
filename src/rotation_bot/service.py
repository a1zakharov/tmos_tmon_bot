"""A fixed daily signal and independently refreshed, read-only execution plans."""
from datetime import timedelta
from decimal import Decimal

from .domain import BotError, Frame, MSK, Target, infer_initial_target
from .execution import plan_rotation
from .schedule import decision_window, window_status
from .state import scope_for
from .strategy import target_for_snapshot

MODEL = "preclose-last-quote-v2"


def execution_blockers(frame, cfg):
    blockers = []
    account = frame.account
    if account.limits_loading or account.active_pair_orders or any(account.blocked_units.values()):
        blockers.append("positions_loading_or_pair_orders_pending")
    # The signal may be ten minutes old. Executable prices may NOT be.
    for ticker in frame.instruments:
        status = frame.trading_statuses.get(ticker, {})
        if not status.get("apiTradeAvailableFlag") or not status.get("limitOrderAvailableFlag"):
            blockers.append(f"{ticker}:orders_unavailable")
        if status.get("tradingStatus") != "SECURITY_TRADING_STATUS_NORMAL_TRADING":
            blockers.append(f"{ticker}:not_continuous_trading")
        book = frame.books.get(ticker)
        if book is None:
            blockers.append(f"{ticker}:orderbook_unavailable")
            continue
        age = (frame.now - book.observed_at).total_seconds()
        if not 0 <= age <= cfg.max_book_age_seconds:
            blockers.append(f"{ticker}:stale_or_future_orderbook")
        if not book.bid.is_finite() or not book.ask.is_finite() or book.bid <= 0 or book.ask < book.bid:
            blockers.append(f"{ticker}:invalid_orderbook")
        elif (book.ask - book.bid) / book.bid * 10000 > Decimal(cfg.max_spread_bps):
            blockers.append(f"{ticker}:wide_spread")
    return blockers


def execution_preview(frame, cfg, target):
    blockers = execution_blockers(frame, cfg)
    plan = {"kind": "await_executable_market", "target": target.value}
    if not blockers:
        try:
            plan = plan_rotation(frame, target, cfg)
        except BotError:
            blockers.append("plan_needs_fresh_executable_prices_or_whole_lots")
    return {"evaluated_at": frame.now.isoformat(), "target": target.value,
            "state": "waiting_for_market" if blockers else "plan_available",
            "plan": plan, "execution_blockers": blockers + ["live_execution_not_implemented"],
            "orders_submitted": 0}


def validate_account(frame):
    account = frame.account
    if account.account_type != "ACCOUNT_TYPE_TINKOFF_IIS" or account.status != "ACCOUNT_STATUS_OPEN":
        raise BotError("The pinned account must be an open IIS")
    if account.available_rub < 0 or any(q < 0 for q in account.free_units.values()):
        raise BotError("Borrowed cash/short positions in the managed pair are not allowed")
    if set(frame.instruments) != {"TMOS", "TMON"}:
        raise BotError("The managed pair must be exactly TMOS/TMON")
    for ins in frame.instruments.values():
        if not ins.for_iis or not ins.api_available or ins.lot <= 0 or ins.tick <= 0:
            raise BotError("Invalid or unavailable instrument on IIS")
    if frame.now.astimezone(MSK).date() != frame.session.day:
        raise BotError("Broker trade date does not match the local trading day")
    if frame.session.kind != "main":
        raise BotError("The signal must be anchored to the common main session")


def check(frame: Frame, cfg, store):
    cfg.validate()
    scope = scope_for(frame.account.account_id)
    day = frame.session.day.isoformat()
    existing = store.existing(scope, day)
    status = window_status(frame.now, frame.session, cfg.minutes_before_close, cfg.decision_window_seconds)
    cutoff, window_end = decision_window(frame.session, cfg.minutes_before_close, cfg.decision_window_seconds)
    latest = existing or store.latest(scope, day)
    if status != "due" and not latest:
        return {"status": status, "check_at": cutoff.isoformat(), "window_ends": window_end.isoformat(), "orders_submitted": 0}
    validate_account(frame)
    if existing or status != "due":
        # Reuse a persisted target before/after the signal window and on later
        # trading days. No new IMOEX observation or daily decision is needed.
        return {"status": "already_decided" if existing else status, "decision": latest,
                "check_at": cutoff.isoformat(), "window_ends": window_end.isoformat(),
                "execution": execution_preview(frame, cfg, Target(latest["target"])), "orders_submitted": 0}
    if frame.previous is None or frame.previous.day >= frame.session.day:
        raise BotError("Reference range must come from a previous completed day")
    frame.previous.validate()
    if (frame.session.day - frame.previous.day).days > 14:
        raise BotError("Reference daily range is too old; verify the exchange calendar")
    last = store.previous_target(scope, day)
    initial = Target(last) if last else infer_initial_target(frame.account)
    target, reason, age_seconds = initial, "no_snapshot", None
    snapshot = frame.snapshot
    if snapshot is not None:
        if snapshot.observed_at.tzinfo is None:
            raise BotError("IMOEX snapshot must have a timezone")
        if snapshot.observed_at.astimezone(MSK).date() != frame.session.day:
            raise BotError("IMOEX quote is from a different trading day")
        age_seconds = (cutoff - snapshot.observed_at).total_seconds()
        if age_seconds <= 0:
            raise BotError("IMOEX observation must end strictly before the fixed cutoff")
        if age_seconds > cfg.max_quote_age_seconds:
            reason = "stale_snapshot"
        else:
            target, reason = target_for_snapshot(frame.previous, snapshot, initial)
    preview = execution_preview(frame, cfg, target)
    payload = {"strategy": MODEL, "trade_date": day,
               "evaluated_at": frame.now.isoformat(), "cutoff_at": cutoff.isoformat(),
               "signal_source": "IMOEX_completed_minute_close",
               "max_quote_age_seconds": cfg.max_quote_age_seconds,
               "quote_age_seconds_at_cutoff": age_seconds,
               "quote_at": snapshot.observed_at.isoformat() if snapshot else None,
               "quote_price": str(snapshot.price) if snapshot else None,
               "previous_day": frame.previous.day.isoformat(),
               "previous_high": str(frame.previous.high), "previous_low": str(frame.previous.low),
               "previous_target": initial.value, "target": target.value, "reason": reason,
               "plan": preview["plan"], "execution_blockers": preview["execution_blockers"],
               "orders_submitted": 0}
    payload, created = store.record_once(scope, day, payload)
    # Another worker may have won with a different target: always preview the
    # actual persisted decision, never the losing worker's proposed target.
    preview = execution_preview(frame, cfg, Target(payload["target"]))
    return {"status": "decision_recorded" if created else "already_decided", "decision": payload,
            "execution": preview, "orders_submitted": 0}
