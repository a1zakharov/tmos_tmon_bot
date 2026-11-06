"""Intraday TMOS/TMON pre-close study using only observations known at each time."""
from __future__ import annotations

import argparse
import bisect
import csv
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import heapq
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
BOT_ROOT = ROOT.parents[1]
sys.path.insert(0, str(BOT_ROOT / "src"))
from rotation_bot.domain import MSK, PreviousDay, Snapshot, Target
from rotation_bot.strategy import target_for_snapshot


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    begin: datetime
    end: datetime
    open: float
    close: float
    volume: int


@dataclass(frozen=True)
class Scenario:
    name: str
    allocation: str = "rotation"
    cutoff: str = "full_close"
    max_age: int = 45
    participation: float | None = 1.0


def read_daily(path):
    with path.open() as f:
        return {r["begin"][:10]: r for r in csv.DictReader(f)}


def minute_bars(symbol, data):
    for path in sorted((data / "minutes").glob(symbol + "_*.csv")):
        with path.open() as f:
            for r in csv.DictReader(f):
                yield Bar(symbol, datetime.fromisoformat(r["begin"]), datetime.fromisoformat(r["end"]),
                          float(r["open"]), float(r["close"]), int(r["volume"]))


def schedule(day: date):
    # Historical exchange schedule, not an inferred last-trade timestamp.
    # Broker-specific historical schedules are unavailable in T-Invest API.
    if day == date(2022, 3, 30):
        # CBR 29.03.2022: equities 09:50–13:50, ETF trading synchronised
        # with underlying assets. TMOS resumed that day; its last minute
        # ended at 13:44:59. This is the first shared fund day after the halt.
        return time(13, 50), time(13, 40)
    if day < date(2026, 3, 23):
        return time(18, 50), time(18, 40)
    return time(19, 0), time(18, 55)


def signal_inputs(index_bars, index_daily, trading_days, mode="full_close"):
    by_day = {}
    for b in index_bars:
        by_day.setdefault(b.begin.date().isoformat(), []).append(b)
    days = sorted(index_daily)
    rows = []
    for d in sorted(trading_days):
        day = date.fromisoformat(d)
        full_close, continuous_close = schedule(day)
        anchor = full_close if mode == "full_close" else continuous_close
        cutoff = datetime.combine(day, anchor) - timedelta(minutes=5)
        previous_idx = bisect.bisect_left(days, d) - 1
        candidates = by_day.get(d, [])
        # A bar containing the decision instant is never used: only a completed
        # minute that ended strictly BEFORE the snapshot time is eligible.
        candidates = [b for b in candidates if b.end < cutoff]
        latest = max(candidates, key=lambda b: b.end) if candidates else None
        prior = index_daily[days[previous_idx]] if previous_idx >= 0 else None
        rows.append({"date": d, "cutoff": cutoff, "anchor": anchor.isoformat(),
                     "previous_date": days[previous_idx] if prior else None,
                     "previous_high": float(prior["high"]) if prior else None,
                     "previous_low": float(prior["low"]) if prior else None,
                     "snapshot": latest.close if latest else None,
                     "snapshot_at": latest.end if latest else None,
                     "age_seconds": (cutoff - latest.end).total_seconds() if latest else None,
                     "auction_window": cutoff.time() >= continuous_close})
    return rows


def xirr(flows):
    if not flows or not any(v > 0 for _, v in flows) or not any(v < 0 for _, v in flows):
        return None
    origin = flows[0][0]
    terms = [((d - origin).days / 365.25, value) for d, value in flows]
    def npv(x):
        return sum(v * math.exp(max(-700, min(700, -x*t))) for t, v in terms)
    lo, hi = -10.0, 10.0
    flo = npv(lo)
    if flo * npv(hi) > 0:
        return None
    for _ in range(150):
        mid = (lo+hi)/2
        if npv(mid) * flo > 0:
            lo, flo = mid, npv(mid)
        else:
            hi = mid
    return math.expm1((lo+hi)/2)


def write_csv(path, rows):
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def simulate(cfg, scenario, funds, daily_funds, signal_rows):
    start, end = date.fromisoformat(cfg["start"]), date.fromisoformat(cfg["end"])
    if any(cfg[k] != 0 for k in ("commission_bps", "monthly_service_fee", "slippage_bps")):
        raise ValueError("This experiment assumes zero external costs")
    if cfg["tax_mode"] != "iis_b":
        raise ValueError("This study uses the agreed IIS-B exemption, without a contribution refund")
    opened = date.fromisoformat(cfg["iis_open_date"])
    exemption_date = date.fromisoformat(cfg.get("iis_exemption_evaluation_date", cfg["end"]))
    if (opened >= date(2024, 1, 1) or opened > start or exemption_date < end
            or (exemption_date > end and cfg.get("iis_closed_at_backtest_end", True))
            or exemption_date < opened.replace(year=opened.year + 3)):
        raise ValueError("IIS-B opening/holding assumptions not satisfied")
    target = scenario.allocation if scenario.allocation != "rotation" else "TMOS"
    q = {"TMOS": 0, "TMON": 0}
    mark = {s: 0.0 for s in q}
    cash, contributions, previous_equity, nav, peak, worst_dd = 0.0, 0.0, 0.0, 1.0, 1.0, 0.0
    ready_at = datetime.combine(start, time.min)
    signals, trades, daily, flows = [], [], [], []
    signal_time = None
    accepted, stale, no_snapshot, changes = 0, 0, 0, 0
    oversized, partial_fills, maximum_demand_ratio = 0, 0, 0.0
    day_pnl, day_deposit, annual_deposits = 0.0, 0.0, {}
    cash_waiting_days = 0
    delay = timedelta(seconds=cfg["order_delay_seconds"])

    def fixed_events():
        day = start
        while day <= end:
            if day == start or day.day == 1:
                amount = cfg["monthly_contribution"] + (cfg["initial_capital"] if day == start else 0)
                yield datetime.combine(day, time.min), 0, (day, amount)
            yield datetime.combine(day, time(23, 59, 59, 999999)), 3, day
            day += timedelta(days=1)

    def signal_events():
        for row in signal_rows:
            yield row["cutoff"], 1, row

    def fund_events():
        merged = heapq.merge(funds["TMOS"], funds["TMON"], key=lambda b: (b.end, b.symbol))
        for b in merged:
            if start <= b.begin.date() <= end:
                yield b.end, 2, b

    events = heapq.merge(fixed_events(), signal_events(), fund_events(), key=lambda e: (e[0], e[1]))
    for at, kind, obj in events:
        if kind == 0:
            d, amount = obj
            cash += amount
            contributions += amount
            day_deposit += amount
            year = d.year
            annual_deposits[year] = annual_deposits.get(year, 0) + amount
            if annual_deposits[year] > 1_000_000:
                raise ValueError("IIS annual contribution limit exceeded")
            if amount:
                flows.append((d, -amount))
        elif kind == 1:
            row = obj
            old = target
            if row["snapshot"] is None or row["previous_date"] is None:
                status = "no_snapshot_or_previous_day"
                no_snapshot += 1
            elif row["age_seconds"] > scenario.max_age:
                status = "stale_snapshot"
                stale += 1
            else:
                accepted += 1
                previous = PreviousDay(date.fromisoformat(row["previous_date"]),
                                       Decimal(str(row["previous_high"])), Decimal(str(row["previous_low"])))
                snap = Snapshot("IMOEX", Decimal(str(row["snapshot"])), row["snapshot_at"].replace(tzinfo=MSK))
                result, status = target_for_snapshot(previous, snap, Target(target))
                if scenario.allocation == "rotation":
                    target = result.value
                    if target != old:
                        changes += 1
                        ready_at = at + delay
                        signal_time = at
            signals.append({"date": row["date"], "cutoff": at.isoformat(sep=" "),
                            "snapshot_at": row["snapshot_at"].isoformat(sep=" ") if row["snapshot_at"] else "",
                            "snapshot": row["snapshot"] if row["snapshot"] is not None else "",
                            "age_seconds": row["age_seconds"] if row["age_seconds"] is not None else "",
                            "previous_date": row["previous_date"] or "", "previous_low": row["previous_low"],
                            "previous_high": row["previous_high"], "status": status,
                            "target_before": old, "target_after": target,
                            "auction_window": int(row["auction_window"])})
        elif kind == 2:
            b = obj
            sec = b.symbol
            before = q[sec]
            old_mark = mark[sec]
            delta, requested = 0, 0
            other = "TMON" if target == "TMOS" else "TMOS"
            if b.begin >= ready_at and b.volume > 0:
                if sec == other and q[other]:
                    requested = q[other]
                    delta = -requested
                elif sec == target and not q[other]:
                    requested = max(0, math.floor((cash + 1e-8) / b.open))
                    delta = requested
                if requested:
                    maximum_demand_ratio = max(maximum_demand_ratio, requested / b.volume)
                    if requested > b.volume:
                        oversized += 1
                    if scenario.participation is not None:
                        capacity = math.floor(b.volume * scenario.participation)
                        if requested > capacity:
                            partial_fills += 1
                        delta = (1 if delta > 0 else -1) * min(requested, capacity)
                if delta:
                    cash -= delta * b.open
                    q[sec] += delta
                    if -1e-7 < cash < 0:
                        cash = 0.0
                    # The minute's first-print OPEN is the fill-price proxy. Its
                    # volume is only known at minute end; the next leg cannot use
                    # any of that same minute's prices.
                    trades.append({"time": at.isoformat(sep=" "), "bar_begin": b.begin.isoformat(sep=" "),
                                   "instrument": sec, "quantity": delta, "price": b.open,
                                   "bar_volume": b.volume, "participation": abs(delta) / b.volume,
                                   "signal_time": signal_time.isoformat(sep=" ") if signal_time else "",
                                   "target": target, "cash_after": cash,
                                   "reason": "exit" if delta < 0 else "buy_or_invest"})
                    ready_at = at + delay
            # All marks are updated only when this minute has completed.
            day_pnl += before * (b.open - old_mark) + q[sec] * (b.close - b.open)
            mark[sec] = b.close
            if cash < -1e-6 or any(v < 0 for v in q.values()) or (q["TMOS"] and q["TMON"]):
                raise AssertionError("Borrowing/short or simultaneous two-fund holdings")
        else:
            d = obj
            for sec in q:
                row = daily_funds[sec].get(d.isoformat())
                if row:
                    close = float(row["close"])
                    day_pnl += q[sec] * (close - mark[sec])
                    mark[sec] = close
            equity = cash + sum(q[s] * mark[s] for s in q)
            reconciliation = equity - previous_equity - day_deposit - day_pnl
            if abs(reconciliation) > max(1e-6, abs(equity) * 1e-9):
                raise AssertionError(f"Account mismatch {d}: {reconciliation}")
            base = previous_equity + day_deposit
            daily_return = equity / base - 1 if base > 0 else 0
            nav *= 1 + daily_return
            peak = max(peak, nav)
            dd = nav / peak - 1
            worst_dd = min(worst_dd, dd)
            holding = "TMOS" if q["TMOS"] else "TMON" if q["TMON"] else "cash"
            if cash > max(1, equity * .01):
                cash_waiting_days += 1
            daily.append({"date": d.isoformat(), "equity": equity, "contributed": contributions,
                          "deposit": day_deposit, "cash": cash, "tmos_units": q["TMOS"], "tmon_units": q["TMON"],
                          "tmos_value": q["TMOS"] * mark["TMOS"], "tmon_value": q["TMON"] * mark["TMON"],
                          "target": target, "holding": holding, "nav": nav, "drawdown": dd,
                          "fund_pnl": day_pnl, "reconciliation_error": reconciliation})
            previous_equity, day_pnl, day_deposit = equity, 0.0, 0.0
    years = (end-start).days / 365.25
    annual, last_nav = {}, 1.0
    for row in daily:
        y = row["date"][:4]
        annual.setdefault(y, {"factor": 1.0, "deposits": 0.0})
        annual[y]["factor"] *= row["nav"] / last_nav
        annual[y]["deposits"] += row["deposit"]
        annual[y]["end_equity"] = row["equity"]
        last_nav = row["nav"]
    summary = {"name": scenario.name, "start": start.isoformat(), "end": end.isoformat(),
               "contributions": contributions, "final_equity": daily[-1]["equity"],
               "profit": daily[-1]["equity"]-contributions,
               "xirr": xirr(flows+[(end, daily[-1]["equity"])]), "twr_cagr": nav ** (1/years)-1 if years else None,
               "max_drawdown_twr": worst_dd, "target_changes": changes, "trade_legs": len(trades),
               "signal_days": len(signals), "accepted_snapshots": accepted, "stale_snapshots": stale,
               "missing_snapshots": no_snapshot, "participation_cap": scenario.participation,
               "oversized_bar_requests": oversized, "partial_fills": partial_fills,
               "maximum_demand_to_printed_volume": maximum_demand_ratio,
               "cash_waiting_days": cash_waiting_days, "commission": 0.0, "tax_paid": 0.0,
               "contribution_tax_refund": 0.0,
               "annual": {y: {"twr": v["factor"]-1, "deposits": v["deposits"], "end_equity": v["end_equity"]}
                          for y, v in annual.items()}}
    return daily, trades, signals, summary


LABELS = {"hold_tmos": "Покупать TMOS", "hold_tmon": "Покупать TMON",
          "preclose": "T−5, свежесть ≤45 с, объём ≤100%",
          "preclose_unlimited": "T−5, без ограничения объёма",
          "last_quote": "T−5, последняя цена ≤10 мин",
          "before_auction": "За 5 мин до конца непрерывных торгов",
          "participation_10pct": "T−5, объём ≤10%"}


def report(results, cfg, quality, out):
    money = lambda x: f"{x:,.0f}".replace(",", " ")
    pct = lambda x: f"{x*100:.2f}%" if x is not None else "—"
    s = results["preclose"]
    lines = ["# Внутридневной бэктест TMOS / TMON, закрытие минус 5 минут", "",
             f"Период {cfg['start']} — {cfg['end']}. Начальный капитал {money(cfg['initial_capital'])} ₽; "
             f"пополнение {money(cfg['monthly_contribution'])} ₽ в месяц. "
             f"Всего внесено {money(s['contributions'])} ₽. Комиссии, абонплата и проскальзывание равны нулю. "
             "ИИС типа Б с предполагаемым открытием 26.08.2020, НДФЛ по сделкам 0; возврата со взносов нет.", "",
             "## Основной результат", "",
             f"При цене IMOEX, доступной за пять минут до конца общей основной сессии, "
             f"проверке свежести до 45 секунд и ограничении исполнения наблюдавшимся минутным объёмом: "
             f"**{money(s['final_equity'])} ₽**, XIRR **{pct(s['xirr'])}** годовых, "
             f"максимальная просадка **{pct(s['max_drawdown_twr'])}**.", "",
             f"Свежий минутный снимок найден в **{s['accepted_snapshots']} из {s['signal_days']}** "
             f"сессий; устаревших снимков — **{s['stale_snapshots']}**. "
             "Это важное отличие от расчёта по финальному дневному закрытию. До изменения "
             "расписания 23.03.2026 строгая версия оставалась в TMOS: в окне аукциона "
             "не было свежей минутной свечи. Пропуск не означает ошибку загрузки: "
             "в эти минуты индекс не публиковал новый минутный бар.", "",
             "## Сравнение на одном периоде и с одинаковыми взносами", "",
             "| Сценарий | Итог, ₽ | Прибыль, ₽ | XIRR годовых | TWR годовых | Макс. просадка | Изменений цели |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name, r in results.items():
        lines.append(f"| {LABELS[name]} | {money(r['final_equity'])} | {money(r['profit'])} | {pct(r['xirr'])} | "
                     f"{pct(r['twr_cagr'])} | {pct(r['max_drawdown_twr'])} | {r['target_changes']} |")
    lines += ["", "Основной сценарий — строка T−5 со свежестью ≤45 с и объёмом ≤100%. "
              "Дополнительные строки показывают чувствительность к исполнению и времени сигнала, "
              "а не подбирают лучший параметр. Принятие цены возрастом до 10 минут отличается "
              "от ограничения свежести текущего скелета. Проверка до аукциона меняет время стратегии.", "",
              "## Покрытие строгого сигнала по годам", "",
              "| Год | Общих сессий | Свежих снимков | Устаревших | Нет снимка |",
              "|---|---:|---:|---:|---:|"]
    for year, q in sorted(quality["signal_coverage_by_year"].items()):
        lines.append(f"| {year} | {q['total']} | {q['fresh']} | {q['stale']} | {q['missing']} |")
    lines += ["", "Точка T−5 оказалась на аукционе или его начале в "
              f"{quality['cutoffs_in_auction_window']} из {quality['shared_signal_days']} "
              "плановых проверок. Исторические статусы торгов конкретного брокера не восстановлены.", "",
              "Проверка 45 секунд здесь приближённая: временем цены считается конец минутной "
              "свечи. Точный последний тик может быть раньше. 686 снимков однозначно не проходят "
              "этот лимит; для подтверждения реальной свежести остальных нужны тиковые данные. "
              "По минутным свечам можно проверить отсутствие будущих цен и минуту решения, "
              "но нельзя точно воспроизвести проверку свежести до секунды.", "",
              "## Данные и расписание", "",
              "- Использованы фактические минутные свечи MOEX ISS для TMOS и TMON, "
              "а также минутные IMOEX в интервале перед закрытием и опубликованные дневные high/low. "
              "TMON начал торговаться 12.07.2023; его историю не заменяли LQDT. "
              "Тест заканчивается 05.11.2026, последним полностью завершённым днём на момент начала работы.",
              "- TMON не включён в вечернюю сессию в проверенном описании инструмента. "
              "Поэтому используется общая основная сессия пары. Её конец моделируется по "
              "официальному расписанию: 18:50 до 23.03.2026, затем 19:00. "
              "Проверка — соответственно в 18:45 и 18:55 по Москве. Последняя зарегистрированная "
              "сделка не используется для угадывания конца сессии.",
              "- С 23.03.2026 аукцион закрытия идёт с 18:55 до 19:00; до изменения "
              "основной аукцион начинался в 18:40. Проверка T−5 попадает в аукцион или на его начало. "
              "До этого индекс часто не имел новой минуты между началом аукциона и итоговым значением.",
              "- С 26.09.2026 IMOEX рассчитывается и в дополнительные сессии, "
              "при этом официальное закрытие по-прежнему определяется основной сессией. "
              "Исторический IMOEX2 в сигнал вместо IMOEX не подставляется.",
              "- Учитывается переход TQTF → TQBR 22.06.2026. Исторические расписания "
              "самого брокера через TradingSchedules недоступны; отдельные досрочные закрытия "
              "и ограничения брокера не восстановлены. Это биржевой исследовательский сценарий.", "",
              "## Отсутствие будущих данных", "",
              "1. Диапазон берётся из последнего опубликованного дневного бара со строго "
              "более ранней датой. Дневной high/low текущего дня не используется.",
              "2. Снимок — CLOSE последней минутной свечи IMOEX, завершившейся строго "
              "до момента T−5. Вся минутная свеча, содержащая момент проверки, исключается. "
              "Возраст оценивается относительно конца свечи; точный момент последнего тика внутри "
              "минуты неизвестен. Отсутствие/устаревание снимка сохраняет предыдущую цель.",
              "3. Решение вычисляется той же чистой функцией `target_for_snapshot`, "
              "что используется скелетом бота. Ниже предыдущего low — TMON; выше high — TMOS; "
              "внутри, включая равенство, цель сохраняется.",
              "4. Заявка может использовать только минуту, начавшуюся после сигнала плюс "
              "одна секунда задержки. Продажа учитывается завершённой в конце своей минуты. "
              "Покупка может начаться только в следующей доступной минуте после продажи. "
              "При отсутствии торгов она переносится на следующий доступный интервал/день.", "",
              "## Исполнение и денежные потоки", "",
              "- Основной вариант ограничивает исполнение 100% фактического объёма соответствующей "
              "минуты. При нехватке объёма остаётся частично проданная позиция или денежный остаток. "
              "Одновременное владение обоими фондами и заёмные средства запрещены. "
              "После нового дневного сигнала невыполненная часть следует новой целевой позиции.",
              "- Цена исполнения — OPEN первой допустимой минутной свечи с реальными сделками. "
              "Это ценовой прокси, а не доказанное исполнение всего объёма по одной цене. "
              "Даже ограничение 100% минутного объёма оптимистично: не восстановлены стакан, "
              "встречная ликвидность, очередь, точное время сделок внутри минуты и брокерские лимиты. "
              "Отдельно показан предел участия 10%. Строка журнала отражает минутное исполнение, "
              "а не обязательно отдельное брокерское поручение.",
              "- Исполнение по историческим принтам допускает аукционные сделки и ожидание следующей "
              "сессии. Текущий скелет, напротив, помечает аукцион блокирующей причиной для будущего "
              "исполнителя. До реальной торговли это противоречие нужно разрешить; данный отчёт "
              "не утверждает, что текущий скелет уже способен исполнить эти операции.",
              "- Взнос поступает первого календарного числа месяца; первый — на дату начала теста. "
              "Свободные деньги покупают текущий целевой фонд при первой доступной минуте. "
              "Это условие исследования регулярных покупок; скелет пока не инвестирует "
              "свободные деньги автоматически. Лот принят равным одному паю для обоих фондов.",
              "- Оценка капитала на конец календарного дня — по опубликованной дневной цене "
              "закрытия каждого фонда; в неторговые дни цена переносится без искусственного "
              "начисления процентов. Доход и расходы управляющих уже содержатся в цене паёв.", "",
              "## Проверяемость", "",
              f"Нормализованных минутных записей: TMOS {quality['minute_rows']['TMOS']}, "
              f"TMON {quality['minute_rows']['TMON']}, IMOEX перед закрытием {quality['minute_rows']['IMOEX']}.",
              "Для каждого сценария сохранены `*_daily.csv`, `*_trades.csv`, `*_signals.csv`. "
              "`audit.json` содержит сверку денег, объёмов и времени двух ног. "
              "`data/manifest.json` содержит URL исходных запросов и контрольные суммы. "
              "Raw-ответы сохранены с gzip-сжатием. Данные не требуют токена брокера.", "",
              "## Источники", "",
              "- Источник котировок: [ПАО Московская Биржа, индексы](https://www.moex.com/ru/indices), MOEX ISS.",
              "- [Изменение основной сессии с 23.03.2026](https://www.moex.com/n97596)",
              "- [Расчёт IMOEX во все сессии с 26.09.2026](https://www.moex.com/n103622?nt=106)",
              "- [Расписание торгов](https://www.moex.com/s1167)",
              "- [История TMON](https://www.moex.com/ru/issuehistory.aspx?code=TMON)", ""]
    (out / "report.md").write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results")
    args = parser.parse_args()
    data = ROOT / "data"
    cfg = json.loads((ROOT / "config.json").read_text())
    manifest = json.loads((data / "manifest.json").read_text())
    for file, digest in manifest["files"].items():
        if hashlib.sha256((data / file).read_bytes()).hexdigest() != digest:
            raise AssertionError(f"Data SHA mismatch: {file}")
    funds = {s: list(minute_bars(s, data)) for s in ("TMOS", "TMON")}
    index = list(minute_bars("IMOEX", data))
    daily_funds = {s: read_daily(data / (s.lower() + "_daily.csv")) for s in funds}
    index_daily = read_daily(data / "imoex_daily.csv")
    common_days = set(daily_funds["TMOS"]) & set(daily_funds["TMON"]) & set(index_daily)
    full = signal_inputs(index, index_daily, common_days)
    early = signal_inputs(index, index_daily, common_days, "continuous_close")
    scenarios = [Scenario("hold_tmos", "TMOS"), Scenario("hold_tmon", "TMON"),
                 Scenario("preclose", max_age=cfg["snapshot_max_age_seconds"]),
                 Scenario("preclose_unlimited", participation=None),
                 Scenario("last_quote", max_age=600),
                 Scenario("before_auction", cutoff="continuous_close"),
                 Scenario("participation_10pct", participation=.1)]
    results, audit = {}, {"status": "passed", "scenarios": {}}
    args.output.mkdir(exist_ok=True, parents=True)
    for sc in scenarios:
        print("Running", sc.name, flush=True)
        rows, trades, sig, summary = simulate(cfg, sc, funds, daily_funds, early if sc.cutoff == "continuous_close" else full)
        for t in trades:
            if t["signal_time"] and t["bar_begin"] <= t["signal_time"]:
                raise AssertionError("Execution before signal")
            if sc.participation is not None and abs(t["quantity"]) > math.floor(t["bar_volume"] * sc.participation):
                raise AssertionError("Fill exceeds participation cap")
        # Independent replay of the exported trade ledger, without relying on
        # the simulator's P&L accumulation or valuation code.
        ledger_cash = summary["contributions"]
        ledger_units = {"TMOS": 0, "TMON": 0}
        for t in trades:
            ledger_cash -= t["quantity"] * t["price"]
            ledger_units[t["instrument"]] += t["quantity"]
            if any(v < 0 for v in ledger_units.values()) or all(ledger_units.values()):
                raise AssertionError("Trade ledger has short or simultaneous holdings")
        if abs(ledger_cash - rows[-1]["cash"]) > 1e-6:
            raise AssertionError("Exported trade ledger cash does not reconcile")
        if ledger_units != {"TMOS": rows[-1]["tmos_units"], "TMON": rows[-1]["tmon_units"]}:
            raise AssertionError("Exported trade ledger holdings do not reconcile")
        audit["scenarios"][sc.name] = {"max_accounting_error_rub": max(abs(r["reconciliation_error"]) for r in rows),
                                       "min_cash": min(r["cash"] for r in rows), "trade_legs": len(trades),
                                       "independent_ledger_cash_error": ledger_cash - rows[-1]["cash"]}
        write_csv(args.output / (sc.name + "_daily.csv"), rows)
        write_csv(args.output / (sc.name + "_trades.csv"), trades)
        write_csv(args.output / (sc.name + "_signals.csv"), sig)
        results[sc.name] = summary
    coverage = {}
    for row in full:
        year = row["date"][:4]
        coverage.setdefault(year, {"total": 0, "fresh": 0, "stale": 0, "missing": 0})
        coverage[year]["total"] += 1
        key = "missing" if row["age_seconds"] is None else "fresh" if row["age_seconds"] <= cfg["snapshot_max_age_seconds"] else "stale"
        coverage[year][key] += 1
    quality = {"minute_rows": {"TMOS": len(funds["TMOS"]), "TMON": len(funds["TMON"]), "IMOEX": len(index)},
               "shared_signal_days": len(full), "signal_coverage_by_year": coverage,
               "cutoffs_in_auction_window": sum(row["auction_window"] for row in full),
               "tmon_minutes_after_1905": sum(b.begin.time() >= time(19, 5) for b in funds["TMON"])}
    audit["checks"] = ["Processed data hashes match manifest", "All daily account/P&L identities reconcile",
                       "No margin, short positions or simultaneous fund holdings", "Trade minutes start after signals",
                       "Fills respect observed minute volume participation where configured",
                       "Independent exported-trade replay reproduces final cash and both fund holdings"]
    (args.output / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    (args.output / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2))
    (args.output / "data_quality.json").write_text(json.dumps(quality, indent=2))
    (args.output / "config_used.json").write_text(json.dumps(cfg, indent=2))
    report(results, cfg, quality, args.output)
    print(json.dumps({n: {k: v for k, v in r.items() if k != "annual"} for n, r in results.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
