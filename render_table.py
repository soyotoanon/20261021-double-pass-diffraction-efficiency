"""Render the raw double-pass scan CSV as a table image (one point per row, 4 column groups)."""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
from matplotlib import rcParams
from matplotlib.figure import Figure

rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False

APP_DIR = Path(__file__).resolve().parent
MEASUREMENT_DIR = APP_DIR / "measurements"
OUT_PATH = APP_DIR / "double_pass_efficiency_table.png"


def load_rows(path: Path) -> list[dict[str, float]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [
            {
                "amp": float(row["amplitude_mVpp"]),
                "power": float(row["double_pass_power_mW"]),
                "std": float(row["power_std_mW"]),
                "eff": float(row["efficiency_percent"]),
                "incident": float(row["incident_reference_mW"]),
            }
            for row in csv.DictReader(handle)
        ]


def render(rows: list[dict[str, float]], path: Path, out_path: Path) -> None:
    incident = rows[0]["incident"]
    amps = [row["amp"] for row in rows]
    best = max(rows, key=lambda row: row["eff"])
    groups = 4
    per_group = -(-len(rows) // groups)  # ceil
    columns = [rows[index : index + per_group] for index in range(0, len(rows), per_group)]

    figure = Figure(figsize=(13.0, 10.5), dpi=200)
    figure.patch.set_facecolor("white")
    figure.text(
        0.5,
        0.982,
        "AOM double-pass diffraction efficiency — raw scan data",
        ha="center",
        va="top",
        fontsize=17,
        fontweight="bold",
    )
    figure.text(
        0.5,
        0.951,
        "2026-10-21 15:02–15:05   ·   AOM SGT100-556-2TA-B30   ·   100 MHz   ·   556 nm   ·   "
        "Rigol DG5352 Pro CH1   ·   Thorlabs PM100D3 #M01306624",
        ha="center",
        va="top",
        fontsize=9.5,
    )
    figure.text(
        0.5,
        0.929,
        f"incident reference (post-50 s mean) = {incident:.6f} mW   |   "
        f"η = P_double-pass / P_incident × 100 %   |   "
        f"{len(rows)} points, {amps[0]:g}–{amps[-1]:g} mVpp   |   "
        f"max η = {best['eff']:.2f} % @ {best['amp']:g} mVpp",
        ha="center",
        va="top",
        fontsize=9.5,
    )
    figure.text(
        0.5,
        0.903,
        "列：幅度 mVpp  /  double-pass 输出功率 mW  /  5 次采样标准差 mW  /  衍射效率 η %",
        ha="center",
        va="top",
        fontsize=9.5,
        color="#444444",
    )

    header = f"{'mVpp':>4}  {'P/mW':>8}  {'std/mW':>8}  {'eta/%':>7}"
    top = 0.872
    bottom = 0.035
    line_fraction = (top - bottom) / (per_group + 1)
    linespacing = 1.12
    fontsize = min(9.0, line_fraction * 10.5 * 72 / linespacing)

    x_positions = [0.025 + index * 0.242 for index in range(groups)]
    for x, column in zip(x_positions, columns):
        lines = [header]
        lines += [
            f"{row['amp']:4.0f}  {row['power']:8.5f}  {row['std']:8.5f}  {row['eff']:7.2f}"
            for row in column
        ]
        figure.text(
            x,
            top,
            "\n".join(lines),
            ha="left",
            va="top",
            family="Consolas",
            fontsize=fontsize,
            linespacing=linespacing,
        )

    figure.savefig(out_path, dpi=200, facecolor="white")
    print(f"saved {out_path} ({len(rows)} rows, {groups} groups)")


def main() -> None:
    candidates = sorted(
        MEASUREMENT_DIR.glob("*_double_pass_scan.csv"), key=lambda item: item.stat().st_mtime
    )
    if not candidates:
        raise SystemExit("measurements 里没有 double-pass 扫描 CSV。")
    source = candidates[-1]
    render(load_rows(source), source, OUT_PATH)


if __name__ == "__main__":
    main()
