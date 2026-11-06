"""Standalone comparison figure; plotting dependencies are not needed to backtest."""
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
    series = [("hold_tmos", "TMOS / T−5 с лимитом 45 с", "#6b7785", "-"),
              ("hold_lqdt", "LQDT / VTBM", "#bb8a23", "-"),
              ("last_quote", "TMOS ↔ LQDT: T−5, последняя цена ≤10 мин", "#087f8c", "-"),
              ("before_auction", "TMOS ↔ LQDT: до аукциона IMOEX −5 мин", "#974c9f", "--")]
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(3, 1, figsize=(13, 11), sharex=True)
    for name, label, color, style in series:
        with (ROOT / "results" / (name + "_daily.csv")).open() as f:
            rows = list(csv.DictReader(f))
        ds = [date.fromisoformat(r["date"]) for r in rows]
        for ax, metric in zip(axes, ("equity", "nav", "drawdown")):
            ax.plot(ds, [float(r[metric]) for r in rows], label=label, color=color, linestyle=style, linewidth=1.6)
        if name == "hold_tmos":
            axes[0].plot(ds, [float(r["contributed"]) for r in rows], label="Взносы", color="#333333", linestyle=":", linewidth=1.1)
    axes[0].legend(loc="upper left", fontsize=8.5)
    axes[0].set_ylabel("Капитал, тыс. ₽")
    axes[0].yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v/1000:,.0f}".replace(",", " ")))
    axes[1].set_ylabel("TWR: начальное значение = 1")
    axes[2].set_ylabel("Просадка TWR")
    axes[2].yaxis.set_major_formatter(PercentFormatter(1))
    for ax in axes:
        ax.grid(alpha=.2)
        ax.axvspan(date(2022, 2, 28), date(2022, 3, 30), color="#777777", alpha=.1)
    fig.suptitle("TMOS ↔ LQDT до появления TMON\n26.08.2020 — 11.07.2023 · 20 000 ₽/месяц · расходы и налог 0", fontsize=15, fontweight="bold")
    fig.text(.5, .022, "Дневная оценка · исполнение по минутным сделкам с лимитом объёма 100%\n"
             "18:45: при свежести 45 с переключений нет; вариант ≤10 мин меняет правило свежести", ha="center", fontsize=9, color="#444444")
    fig.tight_layout(rect=(0, .06, 1, .94))
    for ext in ("png", "svg"):
        path = ROOT / "results" / ("comparison." + ext)
        fig.savefig(path, dpi=160)
        if ext == "svg":
            path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n")


if __name__ == "__main__":
    main()
