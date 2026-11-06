from datetime import datetime, timedelta

from .domain import BotError, Session, timestamp


def sessions_from_day(day: dict) -> list[Session]:
    if not day.get("isTradingDay"):
        return []
    trade_day = timestamp(day["date"]).date()
    result = []
    main_end = day.get("closingAuctionEndTime") or day.get("endTime")
    if day.get("startTime") and main_end:
        end = timestamp(main_end)
        if day.get("eveningStartTime"):
            end = min(end, timestamp(day["eveningStartTime"]))
        result.append(Session(trade_day, timestamp(day["startTime"]), end))
    for lo, hi in (("premarketStartTime", "premarketEndTime"), ("eveningStartTime", "eveningEndTime")):
        if day.get(lo) and day.get(hi):
            result.append(Session(trade_day, timestamp(day[lo]), timestamp(day[hi]),
                                  "premarket" if lo.startswith("premarket") else "evening"))
    if not result or any(s.start >= s.end for s in result):
        raise BotError("Missing or invalid broker trading schedule")
    return result


def common_last_session(left: list[Session], right: list[Session]) -> Session | None:
    common = []
    for a in left:
        for b in right:
            lo, hi = max(a.start, b.start), min(a.end, b.end)
            if a.day == b.day and lo < hi:
                common.append(Session(a.day, lo, hi))
    return max(common, key=lambda s: s.end) if common else None


def common_main_session(left: list[Session], right: list[Session]) -> Session | None:
    """Same anchor as the backtest, even when both funds also trade at night."""
    return common_last_session([s for s in left if s.kind == "main"],
                               [s for s in right if s.kind == "main"])


def decision_window(session: Session, minutes: int = 5, window_seconds: int = 60):
    start = session.end - timedelta(minutes=minutes)
    if start < session.start:
        raise BotError("Common session is too short for the requested five-minute cutoff")
    return start, min(start + timedelta(seconds=window_seconds), session.end)


def window_status(now: datetime, session: Session, minutes: int, seconds: int) -> str:
    lo, hi = decision_window(session, minutes, seconds)
    if now < lo:
        return "waiting"
    if now >= hi:
        return "missed_window"
    return "due"
