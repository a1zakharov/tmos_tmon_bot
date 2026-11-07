"""Russian report and portable publication figures for the US comparison."""
import csv
from datetime import date
import json
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parent
OUT=ROOT/"results"
os.environ.setdefault("MPLCONFIGDIR",str(ROOT/"data/mplconfig"))


def main():
    summary=json.loads((OUT/"summary.json").read_text())
    coverage=json.loads((OUT/"coverage.json").read_text())
    cfg=json.loads((ROOT/"config.json").read_text())
    money=lambda x:f"{x:,.2f}".replace(","," ").replace(".",",")
    pct=lambda x:f"{x:.2%}".replace(".",",")
    lines=["# Американский рынок: SPY ↔ SGOV / BIL, сигнал за 5 минут до закрытия","",
        "Старт 0 $, ежемесячный взнос 200 $, результаты в USD. Комиссии, абонплата, проскальзывание и налоги — 0.",
        "Это безналоговый сценарий сравнения, а не модель российского ИИС или конкретного американского счёта.",
        "Доходы от распределений фондов учтены; повторно вычитать внутренние расходы фондов из котировок и распределений не нужно.","",
        "## Выбор фондов","",
        "- **SPY** — State Street SPDR S&P 500 ETF Trust, фонд крупных компаний США.",
        "- **SGOV** — iShares 0–3 Month Treasury Bond ETF, основной защитный актив из казначейских бумаг США со сроком до 3 месяцев.",
        "- **BIL** — State Street SPDR Bloomberg 1–3 Month T-Bill ETF; дополнительная более длинная история защитного актива.",
        "Фонды выбраны по экономической роли, до расчёта результатов. Параметры не оптимизировались по доходности.","",
        "## Результаты: целые паи, как в российском тесте",""]
    labels={"hold_spy":"Покупать SPY","hold_defensive":"Покупать защитный ETF","rotation":"Переключать SPY ↔ защитный ETF"}
    for defense in cfg["periods"]:
        ref=summary[defense.lower()+"_whole_rotation"]
        lines += ["",f"### SPY / {defense}: {ref['start']} — {ref['end']}",
            "",f"Взносы: **{money(ref['contributions'])} $**.","",
            "| Вариант | Итоговый капитал | Прибыль | XIRR годовых | Макс. просадка TWR |",
            "|---|---:|---:|---:|---:|"]
        for suffix,label in labels.items():
            row=summary[defense.lower()+"_whole_"+suffix]
            lines.append(f"| {label.replace('защитный ETF',defense)} | {money(row['final_equity'])} $ | {money(row['profit'])} $ | {pct(row['xirr'])} | {pct(row['max_drawdown'])} |")
        base=summary[defense.lower()+"_whole_hold_spy"]
        lines += ["",f"Переключение отстаёт от накопления SPY на **{money(base['final_equity']-ref['final_equity'])} $** итогового капитала; смен целевого фонда — **{ref['target_changes']}**."]
    lines += ["","## Дробные паи: отдельная чувствительность","",
        "При взносе 200 $ целый пай SPY дороже месячного пополнения. Поэтому в основном тесте часть денег ждёт покупки. Здесь допускаются дроби до 0,000001 пая, без минимальной суммы заявки: это идеализированное условие, доступность которого у конкретного брокера не проверяется.","",
        "| Период / защитный ETF | Вариант | Итоговый капитал | XIRR годовых | Макс. просадка TWR |",
        "|---|---|---:|---:|---:|"]
    for defense in cfg["periods"]:
        for suffix,label in labels.items():
            row=summary[defense.lower()+"_fractional_"+suffix]
            lines.append(f"| {defense} | {label.replace('защитный ETF',defense)} | {money(row['final_equity'])} $ | {pct(row['xirr'])} | {pct(row['max_drawdown'])} |")
    lines += ["","## Правила сигнала","",
        "Сигнальный инструмент — **SPY как прокси S&P 500**, а не значение самого индекса SPX. В российском тесте использовался IMOEX. Таким образом, это перенос правила на американский ETF, с явно изменённым источником сигнала.",
        "За 5 минут до завершения основной сессии NYSE выбирается CLOSE последней полностью завершённой минутной свечи SPY. Конец свечи должен быть строго раньше точки решения; возраст относительно неё — не более 600 секунд.",
        "Цена ниже минимума предыдущего завершённого торгового дня → защитный фонд. Выше максимума → SPY. Внутри диапазона или на границе → прежняя цель. Начальная цель — SPY. Пропущенный или устаревший снимок сохраняет цель.",
        "Обычное время решения — 15:55 America/New_York. В сокращённые дни — 12:55; учитываются летнее время, праздники и календарь XNYS. Вечерние торги исключены. Предыдущий диапазон — независимые дневные OHLC SPY из Yahoo Finance, в некорректированных на дивиденды ценах.",
        "Дивидендная отсечка самого SPY способна влиять на сигнал, хотя не является падением индекса. Поэтому отдельно проверен контроль: из вчерашнего диапазона вычитается объявленное распределение SPY на сегодняшнюю отсечку. Другие параметры не меняются.","",
        "| Защитный ETF | Основной XIRR | XIRR с нейтрализацией отсечки SPY | Капитал контроля |","|---|---:|---:|---:|"]
    for defense in cfg["periods"]:
        base=summary[defense.lower()+"_whole_rotation"]
        neutral=summary[defense.lower()+"_whole_rotation_exdiv_neutral"]
        lines.append(f"| {defense} | {pct(base['xirr'])} | {pct(neutral['xirr'])} | {money(neutral['final_equity'])} $ |")
    lines += ["","## Исполнение и дивиденды","",
        "Взнос в первый календарный день месяца, первый — в дату начала соответствующего теста. Никакой конвертации USD/RUB нет.",
        "Сначала полностью продаётся прежний фонд, затем покупается новый. Минимальная задержка — 1 секунда после сигнала и после завершения предыдущей минутной свечи исполнения. Обе операции не используют одну свечу. До полной продажи не начинается покупка другого фонда.",
        "Цена сделки — OPEN следующей допустимой реальной минуты. Лимит исполнения — 100% её опубликованного объёма. Частичные исполнения и остатки денег переносятся. Это оптимистичная модель цены и ликвидности: спред, очередь, рыночное влияние, условия дробных заявок и расчётные сроки конкретного брокера не воспроизводятся.",
        "На дату отсечки распределение начисляется только на паи, которыми портфель владел до начала этого дня. Возникает дебиторская задолженность фонда перед инвестором, входящая в стоимость портфеля. Она остаётся у инвестора при последующей продаже ETF.",
        "На официальную дату выплаты задолженность превращается в денежные средства; лишь после этого они могут реинвестироваться в текущий целевой фонд. Предполагается доступность выплат к открытию в день выплаты. Никакого двойного прибавления дивиденда или реинвестирования раньше выплаты нет. Конечный капитал включает начисленные, ещё не выплаченные распределения.",
        "Просадка рассчитана по ежедневной TWR-оценке с поправкой на взносы и с учётом дивидендной задолженности. Это не внутридневная просадка и не отношение капитала к сумме взносов. XIRR использует фактические даты всех пополнений и конечную стоимость.","",
        "## Периоды и качество источников","",
        "Период SGOV начинается 28.05.2020 — с первых фактических минутных данных, а не с синтетического продолжения фонда до его появления. Он не покрывает февраль–март 2020.",
        "Для более длинного теста BIL выбрано 01.12.2017: первый день после его обратного дробления 1:2 от 30.11.2017. Более ранние цены в архиве имеют другую номинальную базу. Внутри выбранных периодов дроблений этих ETF нет; параметры периодов зафиксированы до расчёта доходностей.",
        "Архив заканчивается 23.10.2026; более поздние дневные котировки не продлевают внутридневной тест.",
        "Периоды и капиталы двух пар различаются; сравнивать конечные суммы между периодами напрямую нельзя. Внутри каждого периода у всех вариантов одинаковые даты и взносы.",
        "Минуты: открытый сторонний набор Syncster Historical Market Data на Hugging Face, зафиксированная ревизия "+cfg["hf_revision"]+". Провайдер исходной биржевой ленты и полнота покрытия всех сделок не документированы. Поэтому объём и исполнение — модель по наблюдаемым данным, не подтверждение возможности сделок у брокера.",
        "Сырые OHLC и конечные цены проверены на положительность, непротиворечивость, дубли и совпадение ценовой базы с независимыми дневными котировками. Наблюдения вне основной сессии удалены. Пробелы не заполняются фиктивными свечами.",
        "Для первых двух сессий SGOV (28–29.05.2020) в Yahoo нет дневной свечи, хотя в архиве есть реальные минутные сделки. Их дневные OHLC агрегированы из наблюдавшихся минут; это не синтетическое продление. Полный список таких случаев указан в data_quality.json.",
        "Дивиденды SPY/BIL взяты из официального Excel-реестра State Street, SGOV — из официальной страницы iShares, включая даты выплат. Присутствующая в реестре BIL ошибка дат за 2008 год находится вне обоих периодов и не используется.","",
        "| Пара | Дней сигнала | Сокращённых дней | Нет снимка | Снимок старше 10 мин |","|---|---:|---:|---:|---:|"]
    for defense,c in coverage.items():
        lines.append(f"| SPY/{defense} | {c['signal_days']} | {c['early_close_days']} | {len(c['missing_SPY_snapshot_dates'])} | {len(c['stale_SPY_snapshot_dates'])} |")
    lines += ["","В длинном периоде найден один устаревший снимок SPY — 12.08.2019; цель в этот день сохранена. Отсутствие сделок в архиве не доказывает отсутствие торгов на рынке.",
        "SHA-256 исходников и нормализованных файлов, точные URL и ревизия сохранены в source_manifest.json; дополнительные проверки — data_quality.json.","",
        "## Вывод","",
        "В обоих выбранных периодах правило переключения уступило простому накоплению SPY по доходности. В основном периоде SGOV максимальная просадка также оказалась глубже. На длинном периоде с BIL просадка меньше, но потеря доходности существенна. Дробные паи и контроль дивидендных отсечек не меняют этот вывод.",
        "Прибыльный результат российского теста не переносится автоматически на другую структуру рынка. Это исторический результат данных сценариев, а не прогноз.","",
        "## Проверки и воспроизведение","",
        "10 автоматических регрессий проверяют право на дивиденды, отсечку/выплату, продажу между датами, неоплаченную задолженность, целые/дробные паи, последовательность операций, отсутствие будущих цен, сокращённые дни, DST и контроль отсечки.",
        "Все 14 сценариев дополнительно воспроизведены независимым дневным журналом по экспортированным сделкам и исходным действиям фондов: сверены деньги, позиции, дивиденды, конечный капитал, ежедневная NAV и XIRR. Подробности — audit.json и independent_audit.json.",
        "```sh","python -m venv research/us_preclose/.venv",
        "research/us_preclose/.venv/bin/python -m pip install -r research/us_preclose/requirements.txt",
        "research/us_preclose/.venv/bin/python research/us_preclose/fetch.py",
        "research/us_preclose/.venv/bin/python research/us_preclose/prepare.py",
        "python research/us_preclose/run.py","python research/us_preclose/verify.py",
        "python -m unittest discover -s research/us_preclose -p 'test_*.py'",
        "research/us_preclose/.venv/bin/python research/us_preclose/report.py","```","",
        "## Источники","",
        "- [SPY: State Street](https://www.ssga.com/us/en/intermediary/etfs/state-street-spdr-sp-500-etf-trust-spy).",
        "- [SGOV: iShares](https://www.ishares.com/us/products/314116/ishares-0-3-month-treasury-bond-etf).",
        "- [BIL: State Street](https://www.ssga.com/us/en/intermediary/etfs/state-street-spdr-bloomberg-1-3-month-t-bill-etf-bil).",
        "- [Исторические распределения SPDR](https://www.ssga.com/library-content/products/fund-data/etfs/us/spdr-etf-historical-distributions.xlsx).",
        "- [Минутный архив Syncster](https://huggingface.co/datasets/syncster-data/historical-market-data).",
        "- [NYSE: часы и календарь](https://www.nyse.com/markets/hours-calendars).",
        "- [Календарь XNYS: exchange_calendars](https://github.com/gerrymanoim/exchange_calendars).",
        "- [Yahoo Finance: история SPY](https://finance.yahoo.com/quote/SPY/history/).",""]
    (OUT/"report.md").write_text("\n".join(lines))
    plot(summary,cfg)


def plot(summary,cfg):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter,PercentFormatter
    plt.rcParams.update({"font.family":"DejaVu Sans","font.size":10,
                         "axes.spines.top":False,"axes.spines.right":False})
    fig,axes=plt.subplots(3,2,figsize=(15,11),sharex="col")
    colors={"hold_spy":"#1763a5","hold_defensive":"#b28416","rotation":"#008a78"}
    for col,defense in enumerate(cfg["periods"]):
        ref=summary[defense.lower()+"_whole_rotation"]
        axes[0,col].set_title(f"SPY / {defense}: {ref['start']} — {ref['end']}\nВзносы {ref['contributions']:,.0f} $".replace(","," "),fontsize=11)
        for suffix,color in colors.items():
            name=defense.lower()+"_whole_"+suffix
            with (OUT/(name+"_daily.csv")).open() as f:rows=list(csv.DictReader(f))
            days=[date.fromisoformat(r["date"]) for r in rows]
            label={"hold_spy":"Покупать SPY","hold_defensive":"Покупать "+defense,"rotation":"Переключать SPY ↔ "+defense}[suffix]
            for ax,metric in zip(axes[:,col],("equity","nav","drawdown")):
                ax.plot(days,[float(r[metric]) for r in rows],label=label,color=color,linewidth=1.5)
            if suffix=="hold_spy":
                axes[0,col].plot(days,[float(r["contributed"]) for r in rows],color="#444",linestyle=":",label="Взносы",linewidth=1)
        axes[0,col].legend(fontsize=8,loc="upper left")
        axes[0,col].yaxis.set_major_formatter(FuncFormatter(lambda v,_:f"{v/1000:.0f}"))
        axes[2,col].yaxis.set_major_formatter(PercentFormatter(1))
        for ax in axes[:,col]:ax.grid(alpha=.2)
    axes[0,0].set_ylabel("Капитал, тыс. $")
    axes[1,0].set_ylabel("TWR: начальное значение = 1")
    axes[2,0].set_ylabel("Просадка TWR")
    fig.suptitle("Американский рынок: SPY ↔ короткие казначейские ETF\nСигнал за 5 минут до закрытия · последнее значение ≤10 минут",fontsize=15,fontweight="bold")
    fig.text(.5,.018,"Целые паи · 200 $/месяц · комиссии, проскальзывание и налоги 0\n"
             "Дивиденды начисляются по отсечке и реинвестируются после выплаты · сигнал по SPY, не SPX",ha="center",fontsize=9)
    fig.tight_layout(rect=(0,.06,1,.92))
    for ext in ("png","svg"):
        path=OUT/("comparison."+ext)
        fig.savefig(path,dpi=160)
        if ext=="svg":path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines())+"\n")


if __name__ == "__main__":
    main()
