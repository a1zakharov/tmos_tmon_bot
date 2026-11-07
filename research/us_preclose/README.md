# Американский рынок: SPY / SGOV и SPY / BIL

Самостоятельный эксперимент; код российского торгового бота не меняет.

- [Отчёт с допущениями и источниками](results/report.md).
- [Графики](results/comparison.png).
- [Все 14 сценариев](results/summary.json).
- [Независимая сверка финансового журнала](results/independent_audit.json).

Начальный капитал 0 $, взнос 200 $ в месяц, внешние расходы и налоги 0.
Сигнал — последнее завершённое минутное значение SPY до T−5, не старше 600 с.
Исполнение по реальным минутам, последовательно продажа → покупка, целые паи.
Отдельно рассчитаны дробные паи и контроль дивидендной отсечки SPY.

Прямое значение SPX не используется: SPY выступает прокси S&P 500.
Внутренние расходы фондов уже отражены в ценах и распределениях.
ИИС, валютная переоценка и конкретный брокерский тариф не моделируются.

Периоды: 28.05.2020–23.10.2026 с SGOV; 01.12.2017–23.10.2026 с BIL.
Начало длинного периода исключает обратное дробление BIL 30.11.2017.
В обоих периодах используются настоящие котировки существовавших фондов.

Из корня репозитория бота:

```sh
python -m venv research/us_preclose/.venv
research/us_preclose/.venv/bin/python -m pip install -r research/us_preclose/requirements.txt
research/us_preclose/.venv/bin/python research/us_preclose/fetch.py
research/us_preclose/.venv/bin/python research/us_preclose/prepare.py
python research/us_preclose/run.py
python research/us_preclose/verify.py
python -m unittest discover -s research/us_preclose -p 'test_*.py'
research/us_preclose/.venv/bin/python research/us_preclose/report.py
```

Подготовка использует Parquet и календарь XNYS; закреплённые зависимости требуют
Python 3.12+ (проверено на Python 3.14). Сам расчёт и сверка работают
на стандартной библиотеке Python 3.11+. Исходные ответы, SHA-256 и URL
сохраняются в игнорируемой папке data/. Общедоступный архив минут зафиксирован
ревизией Hugging Face; независимые дневные цены и календарные действия —
Yahoo Finance и официальные страницы State Street/iShares.

Архив минут сторонний; исходный поставщик и полнота биржевой ленты не описаны.
Стакан, очередь заявок и расчётные сроки брокера не воспроизводятся.
Все ограничения и расхождения ценовых источников перечислены в отчёте.
