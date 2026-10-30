"""Pure pre-close signal. Current price is NOT a completed daily close."""
from .domain import PreviousDay, Snapshot, Target, BotError


def target_for_snapshot(previous: PreviousDay, current: Snapshot, last_target: Target) -> tuple[Target, str]:
    previous.validate()
    if current.price <= 0 or current.symbol != "IMOEX":
        raise BotError("Invalid IMOEX snapshot")
    if current.price < previous.low:
        return Target.TMON, "below_previous_low"
    if current.price > previous.high:
        return Target.TMOS, "above_previous_high"
    return last_target, "inside_previous_range"
