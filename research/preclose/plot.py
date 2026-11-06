import csv
from datetime import date
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / "data/mplconfig"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, PercentFormatter


def main():
    series = [("hold_tmos", "Покупать TMOS", "#718096", "-"),
              ("hold_tmon", "Покупать TMON", "#ca9627", "-"),
              ("preclose", "T−5: лимит 45 с по минутам, объём ≤100%", "#008675", "-"),
              ("last_quote", "T−5: последняя цена ≤10 минут", "#1567ac", "--"),
              ("before_auction", "За 5 мин до конца непрерывных торгов", "#a455a2", ":")]
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(3, 1, figsize=(13.5, 11), sharex=True)
    for name, label, color, style in series:
        with (ROOT / "results" / (name + "_daily.csv")).open() as f:
            rows = list(csv.DictReader(f))
        ds = [date.fromisoformat(r["date"]) for r in rows]
        for ax, metric in zip(axes, ("equity", "nav", "drawdown")):
            ax.plot(ds, [float(r[metric]) for r in rows], label=label, color=color, linestyle=style, linewidth=1.6)
        if name == "hold_tmos":
            axes[0].plot(ds, [float(r["contributed"]) for r in rows], color="#333333", linestyle=":", label="Взносы", linewidth=1)
    axes[0].legend(fontsize=8.5, loc="upper left")
    axes[0].set_ylabel("Капитал, млн ₽")
    axes[0].yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x/1e6:.2f}"))
    axes[1].set_ylabel("TWR: начальное значение = 1")
    axes[2].set_ylabel("Просадка TWR")
    axes[2].yaxis.set_major_formatter(PercentFormatter(1))
    for ax in axes:
        ax.grid(alpha=.2)
        ax.axvline(date(2026, 3, 23), color="#555555", linewidth=1, alpha=.4)
    fig.suptitle("TMOS / TMON: внутридневной сигнал перед закрытием\n"
                 "12.07.2023 — 05.11.2026 · 20 000 ₽/месяц · внешние расходы 0", fontsize=15, fontweight="bold")
    fig.text(.07, .014, "Источник: ПАО Московская Биржа, MOEX ISS. Последовательные ноги, минутные цены и объёмы.\n"
             "Вертикаль — изменение расписания 23.03.2026. Аукционная очередь и брокерские ограничения не восстановлены.",
             fontsize=9, color="#555555")
    fig.tight_layout(rect=[0, .055, 1, .94])
    for ext in ("png", "svg"):
        path = ROOT / "results" / ("comparison." + ext)
        fig.savefig(path, dpi=160, facecolor="white")
        if ext == "svg":
            path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n")
        print(path)


if __name__ == "__main__":
    main()
