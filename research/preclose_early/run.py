"""Reuse the audited pre-close engine with LQDT in its defensive allocation role.

TMON below is an INTERNAL role name in the shared engine, never source data.
All exported instruments, targets and holdings are relabelled as actual LQDT;
actual VTBM/LQDT exchange tickers are also exported for each defensive fill.
"""
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "preclose"))
import run_backtest as engine

LABELS = {"hold_tmos": "Покупать TMOS", "hold_lqdt": "Покупать LQDT / VTBM",
          "preclose": "Закрытие −5 мин, свежесть ≤45 с",
          "last_quote": "Закрытие −5 мин, последняя цена ≤10 мин",
          "before_auction": "IMOEX: до аукциона −5 мин, свежесть ≤45 с",
          "participation_10pct": "Закрытие −5 мин, цена ≤10 мин, объём ≤10%"}


def merge_aliases(old, new):
    merged = sorted(old + new, key=lambda b: b.begin)
    if len({b.begin for b in merged}) != len(merged):
        raise ValueError("Overlapping VTBM/LQDT minutes")
    if old and new and max(b.begin for b in old) >= min(b.begin for b in new):
        raise ValueError("VTBM and LQDT source intervals overlap")
    return [replace(b, symbol="TMON") for b in merged]


def exported_row(row):
    # Explicit boundary adapter: never present internal defensive-role units as
    # actual TMON, which did not yet exist during this experiment.
    result = {k.replace("tmon_", "lqdt_"): ("LQDT" if v == "TMON" else v) for k, v in row.items()}
    if "instrument" in result:
        result["source_ticker"] = ("VTBM" if result["bar_begin"][:10] <= "2022-07-21" else "LQDT") if result["instrument"] == "LQDT" else "TMOS"
    return result


def make_report(results, cfg, quality, out):
    money = lambda x: f"{x:,.0f}".replace(",", " ")
    lines = ["# Внутридневной бэктест TMOS ↔ LQDT до появления TMON", "",
             f"Период: **{cfg['start']} — {cfg['end']}**. Старт 0 ₽, по 20 000 ₽ в месяц; всего **720 000 ₽**.",
             "Комиссии, абонентская плата, проскальзывание и налог на прибыль — 0. ИИС типа Б, без возврата со взносов.", "",
             "| Вариант | Итоговый капитал | Прибыль к взносам | XIRR, годовых | Макс. просадка TWR | Смен цели |",
             "|---|---:|---:|---:|---:|---:|"]
    for name, r in results.items():
        lines.append(f"| {LABELS[name]} | {money(r['final_equity'])} ₽ | {money(r['profit'])} ₽ | {r['xirr']:.2%} | {r['max_drawdown_twr']:.2%} | {r['target_changes']} |")
    strict, last = results["preclose"], results["last_quote"]
    lines += ["", "## Сигнал и качество данных", "",
              "IMOEX ниже минимума предыдущего завершённого торгового дня → LQDT; выше максимума → TMOS; внутри диапазона или на границе → прежняя цель. Начальное состояние — TMOS.",
              "Обычно проверка в 18:45 МСК: за пять минут до завершения основной сессии в 18:50, включая послеторговый период фондов. Дополнительный контроль — 18:35, за пять минут до конца непрерывного расчёта IMOEX перед аукционом акций. Исключение 30.03.2022: укороченная сессия TMOS, проверка в 13:45, контроль в 13:35. Вечерние сессии не используются для выбора времени сигнала, но доступные вечерние сделки могут участвовать в исполнении.",
              f"Общих торговых дней: **{strict['signal_days']}**. При допуске 45 секунд принято **{strict['accepted_snapshots']}**, устаревших наблюдений **{strict['stale_snapshots']}**, без подходящего наблюдения **{strict['missing_snapshots']}**.",
              f"При допуске 10 минут принято **{last['accepted_snapshots']}**, пропущено **{last['stale_snapshots'] + last['missing_snapshots']}**.",
              "Перед решением берётся только полностью завершённая минутная свеча IMOEX, закончившаяся строго раньше момента проверки. Свеча, содержащая момент решения, и финальные значения текущего дня для сигнала не используются.",
              "Минутный архив не позволяет подтвердить возраст последней сделки с точностью до 45 секунд. Во время аукциона индекс часто сохраняет значение конца непрерывных торгов: вариант с допуском 10 минут — отдельное правило свежести, а не исполнение лимита 45 секунд.", "",
              "| Год | Дней | ≤45 с | Устаревших | Нет наблюдения |", "|---|---:|---:|---:|---:|"]
    for y, c in quality["coverage_by_year"].items():
        lines.append(f"| {y} | {c['total']} | {c['fresh']} | {c['stale']} | {c['missing']} |")
    lines += ["", "## Фонд и исполнение", "",
              "Защитный актив — один реальный фонд с ISIN RU000A1014L8: VTBM до 21.07.2022 включительно, LQDT с 22.07.2022. Переименование не считается продажей или переключением. История TMON назад не продлевается, доходности не синтезируются.",
              "Котировки — публичные минутные и дневные свечи MOEX ISS. Для инструментов использованы только реальные биржевые свечи с положительным объёмом; исходные ответы, URL, время загрузки и SHA-256 сохранены в data/. У каждого исполнения есть source_ticker.",
              "Как и в позднем тесте: целые паи, без плеча и шорта, сначала полная продажа прежнего фонда, затем покупка нового. Минимальная задержка 1 секунда; обе операции не могут исполниться по одной минутной свече. Взнос в первый календарный день месяца, первый — 26.08.2020. Покупка на ближайших доступных сделках, остаток денег переносится.",
              "Основной лимит — 100% фактического минутного объёма. Дополнительный вариант ограничен 10%. Частичные исполнения и ожидание торгов могут переноситься на следующие дни. Новый сигнал заменяет незавершённую цель. Число смен цели не равно числу полностью исполненных переключений.",
              "В старом расписании торговый период фондов завершался в 18:44:59, затем шёл послеторговый период до 18:49:59. Поэтому решение в 18:45 не означает исполнения в 18:45: используются только следующие реальные минуты торгов, в том числе следующего торгового дня. Этот расчёт сохраняет привязку предыдущего исследования к концу основной сессии; вариант «за пять минут до конца торгового периода фондов», то есть в 18:40, здесь отдельно не моделировался.",
              "Цена исполнения — OPEN следующей допустимой минутной свечи; это оптимистичная модель цены сделки. Спред, очередь заявок, доступность конкретного брокера и статус аукциона архивом не воспроизводятся. Нулевое проскальзывание задано пользователем. Внутренние расходы фондов уже отражены в котировках.",
              "В период остановки торгов в 2022 году сделки не создаются. Сигналы вычисляются только в дни, когда есть дневные котировки обоих фондов и IMOEX. Если к 18:45 нет достаточно свежего значения индекса, цель сохраняется; раннее закрытие индекса не заменяется финальной ценой дня с более поздним временем. Даты без снимка перечислены в data_quality.json.",
              "Просадка рассчитана по ежедневной оценке портфеля с поправкой на взносы (TWR); внутридневной минимум может быть ниже. В дни остановки торгов оценка переносится по последней доступной цене, это не означает доступность продажи. XIRR учитывает точные даты денежных потоков, это не простое отношение прибыли к сумме взносов.", "",
              "## ИИС", "",
              "Принята прежняя дата открытия 26.08.2020. Дата 11.07.2023 — только оценка портфеля, без закрытия ИИС или вывода денег. Предполагается сохранение ИИС как минимум до 26.08.2023 и соблюдение условий льготы типа Б; возврат НДФЛ со взносов не начисляется. Досрочное закрытие в июле 2023 не моделируется. В итоговом капитале не учитываются доходности после конца теста.", "",
              "## Годовые результаты TWR", "",
              "| Вариант | 2020, неполный | 2021 | 2022 | 2023, неполный |", "|---|---:|---:|---:|---:|"]
    for name, r in results.items():
        lines.append("| " + LABELS[name] + " | " + " | ".join(f"{r['annual'][str(y)]['twr']:.2%}" for y in range(2020, 2024)) + " |")
    lines += ["", "## Проверки и воспроизведение", "",
              "SHA-256 всех нормализованных файлов проверен. Денежные потоки и позиции сверены по каждому дню, выполнено независимое воспроизведение журнала сделок; проверены отсутствие коротких позиций, одновременного владения двумя фондами и превышения лимита объёма. Подробности — audit.json.",
              "```sh", "python research/preclose_early/fetch.py --workers 4", "python research/preclose_early/run.py", "python -m unittest discover -s research/preclose -p 'test_*.py'", "python -m unittest discover -s research/preclose_early -p 'test_*.py'", "```", "",
              "Расчёт использует общий движок research/preclose/run_backtest.py. Его внутренний идентификатор защитной роли TMON явно сопоставляется реальной истории VTBM/LQDT; во всех экспортированных журналах указаны LQDT и реальный source_ticker. Это не использование котировок TMON до появления фонда.", "",
              "## Источники", "",
              "- [Мосбиржа: VTBM переименован в LQDT с 22.07.2022](https://www.moex.com/n49955).",
              "- [Мосбиржа: расписание с 31.03.2022](https://www.moex.com/n46282).",
              "- [Мосбиржа: торговый и послеторговый периоды фондов](https://www.moex.com/n20033).",
              "- [Пресс-служба Банка России: 30.03.2022, возобновление TMOS и синхронизация фондов с базовым имуществом](https://www.cbr.ru/press/pr/?file=29032022_195220sup_meas29032022_185602.htm).",
              "- [Публичный MOEX ISS](https://iss.moex.com/iss/): точные запросы и хеши — data/manifest.json.", ""]
    (out / "report.md").write_text("\n".join(lines))


def main():
    data, out = ROOT / "data", ROOT / "results"
    out.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((ROOT / "config.json").read_text())
    manifest = json.loads((data / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((data / name).read_bytes()).hexdigest() == digest, name
    funds = {"TMOS": list(engine.minute_bars("TMOS", data)),
             "TMON": merge_aliases(list(engine.minute_bars("VTBM", data)), list(engine.minute_bars("LQDT", data)))}
    daily_funds = {"TMOS": engine.read_daily(data / "tmos_daily.csv")}
    old, new = engine.read_daily(data / "vtbm_daily.csv"), engine.read_daily(data / "lqdt_daily.csv")
    assert not (set(old) & set(new)), "Overlapping fund alias dates"
    daily_funds["TMON"] = {**old, **new}
    index, index_daily = list(engine.minute_bars("IMOEX", data)), engine.read_daily(data / "imoex_daily.csv")
    days = set(daily_funds["TMOS"]) & set(daily_funds["TMON"]) & set(index_daily)
    full = engine.signal_inputs(index, index_daily, days)
    early = engine.signal_inputs(index, index_daily, days, "continuous_close")
    scenarios = [engine.Scenario("hold_tmos", "TMOS"), engine.Scenario("hold_lqdt", "TMON"),
                 engine.Scenario("preclose"), engine.Scenario("last_quote", max_age=600),
                 engine.Scenario("before_auction", cutoff="continuous_close"),
                 engine.Scenario("participation_10pct", max_age=600, participation=.1)]
    results, audit = {}, {"status": "passed", "scenarios": {}}
    for sc in scenarios:
        print("Running", sc.name, flush=True)
        rows, trades, sig, summary = engine.simulate(cfg, sc, funds, daily_funds, early if sc.cutoff == "continuous_close" else full)
        ledger_cash, units = summary["contributions"], {"TMOS": 0, "TMON": 0}
        for t in trades:
            assert not t["signal_time"] or t["bar_begin"] > t["signal_time"]
            assert abs(t["quantity"]) <= int(t["bar_volume"] * sc.participation)
            ledger_cash -= t["quantity"] * t["price"]
            units[t["instrument"]] += t["quantity"]
            assert min(units.values()) >= 0 and not all(units.values())
        assert abs(ledger_cash - rows[-1]["cash"]) < 1e-6
        assert units == {"TMOS": rows[-1]["tmos_units"], "TMON": rows[-1]["tmon_units"]}
        audit["scenarios"][sc.name] = {"maximum_daily_accounting_error": max(abs(r["reconciliation_error"]) for r in rows),
                                      "independent_ledger_cash_error": ledger_cash - rows[-1]["cash"],
                                      "source_ticker_fills": dict(Counter(exported_row(t)["source_ticker"] for t in trades))}
        for suffix, output in (("daily", rows), ("trades", trades), ("signals", sig)):
            engine.write_csv(out / f"{sc.name}_{suffix}.csv", [exported_row(r) for r in output])
        results[sc.name] = summary
    quality = {"minute_rows": {"TMOS": len(funds["TMOS"]), "LQDT_including_VTBM": len(funds["TMON"]), "IMOEX": len(index)},
               "coverage_by_year": {}, "dates_without_snapshot": [r["date"] for r in full if r["snapshot"] is None],
               "cutoffs_in_auction_window": sum(r["auction_window"] for r in full)}
    for r in full:
        c = quality["coverage_by_year"].setdefault(r["date"][:4], {"total": 0, "fresh": 0, "stale": 0, "missing": 0})
        c["total"] += 1
        c["missing" if r["age_seconds"] is None else "fresh" if r["age_seconds"] <= 45 else "stale"] += 1
    for name, obj in (("summary", results), ("audit", audit), ("data_quality", quality), ("config_used", cfg)):
        (out / (name + ".json")).write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    make_report(results, cfg, quality, out)
    print(json.dumps({n: {k: r[k] for k in ("final_equity", "xirr", "max_drawdown_twr", "target_changes", "accepted_snapshots", "stale_snapshots", "missing_snapshots")} for n, r in results.items()}, indent=2))


if __name__ == "__main__":
    main()
