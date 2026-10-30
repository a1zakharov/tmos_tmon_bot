"""Planning primitives only. No real trading method exists in this scaffold."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Protocol

from .domain import BotError, Frame, Target


def limit_price(reference: Decimal, tick: Decimal, side: str, max_deviation_bps: Decimal) -> Decimal:
    if reference <= 0 or tick <= 0 or not Decimal(0) <= max_deviation_bps < Decimal(10000):
        raise BotError("Invalid reference price or tick")
    if side == "buy":
        raw = reference * (1 + max_deviation_bps / 10000)
        return (raw / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    if side == "sell":
        raw = reference * (1 - max_deviation_bps / 10000)
        return (raw / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    raise BotError("Unknown order side")


def buy_lots(available_rub: Decimal, reserve_rub: Decimal, price: Decimal,
             lot: int, confirmed_exit_proceeds: Decimal | None = None) -> int:
    if lot <= 0 or price <= 0 or reserve_rub < 0:
        raise BotError("Invalid lot/price/reserve")
    budget = max(Decimal(0), available_rub - reserve_rub)
    if confirmed_exit_proceeds is not None:
        budget = min(budget, max(Decimal(0), confirmed_exit_proceeds))
    return int(budget // (price * lot))


def plan_rotation(frame: Frame, target: Target, cfg) -> dict:
    source = Target.TMOS if target == Target.TMON else Target.TMON
    held = frame.account.free_units.get(source.value, 0)
    ins = frame.instruments[source.value]
    if held < 0 or held % ins.lot:
        raise BotError("Owned source units must form whole sellable lots")
    if held:
        return {"kind": "switch", "target": target.value,
                "sell": {"ticker": source.value, "instrument_uid": ins.uid,
                         "lots": held // ins.lot,
                         "limit_price": str(limit_price(frame.books[source.value].bid, ins.tick, "sell",
                                                       Decimal(cfg.max_limit_deviation_bps)))},
                "buy": {"ticker": target.value, "lots": None,
                        "sizing": "after_confirmed_exit_and_fresh_available_cash",
                        "use_exit_proceeds_only": not cfg.invest_idle_cash}}
    if cfg.invest_idle_cash:
        return {"kind": "invest_idle_cash", "target": target.value,
                "buy": {"ticker": target.value, "lots": None,
                        "sizing": "fresh_available_cash_less_reserve"}}
    return {"kind": "hold", "target": target.value}


class TradingGateway(Protocol):
    """Future implementation must persist client IDs BEFORE submission."""
    def submit_limit(self, intent_id: str, account_id: str, instrument_uid: str,
                     side: str, lots: int, price: Decimal) -> dict: ...
    def reconcile(self, intent_id: str, account_id: str) -> dict: ...


class LiveExecutionNotImplemented:
    def submit_limit(self, *args, **kwargs):
        raise BotError("Live and sandbox order submission are not implemented in this scaffold")

    def reconcile(self, *args, **kwargs):
        raise BotError("Order recovery/execution belongs to the next development stage")
