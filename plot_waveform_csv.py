"""Plot MHO98 CH1/CH2 sweep waveforms from the controller CSV format."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_FIGURE_DEPS = Path(__file__).with_name(".figure_deps")
if _FIGURE_DEPS.is_dir() and str(_FIGURE_DEPS) not in sys.path:
    sys.path.insert(0, str(_FIGURE_DEPS))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection


COLORS = {
    "ch1": "#0072B2",
    "ch2": "#D55E00",
    "frequency": "#6A3D9A",
    "boundary": "#555555",
}


def configure_style() -> None:
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["DejaVu Sans", "Arial"],
            "font.size": 10,
            "axes.labelsize": 11,
            "axes.titlesize": 11,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "legend.fontsize": 9,
            "figure.dpi": 150,
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
            "axes.grid": True,
            "grid.alpha": 0.25,
            "grid.linestyle": ":",
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )


def load_data(path: Path) -> pd.DataFrame:
    data = pd.read_csv(path)
    required = {
        "sample_index",
        "stored_time_s",
        "sweep_point",
        "frequency_hz",
        "point_time_s",
        "ch1_voltage_v",
        "ch2_voltage_v",
    }
    missing = sorted(required.difference(data.columns))
    if missing:
        raise ValueError("CSV is missing columns: " + ", ".join(missing))
    numeric = list(required)
    for column in numeric:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=numeric).sort_values("sample_index").reset_index(drop=True)
    if data.empty:
        raise ValueError("CSV contains no valid waveform samples.")
    return data


def segmented_lines(data: pd.DataFrame, value_column: str) -> list[np.ndarray]:
    segments: list[np.ndarray] = []
    for _, group in data.groupby("sweep_point", sort=True):
        if len(group) >= 2:
            segments.append(
                np.column_stack(
                    (
                        group["stored_time_s"].to_numpy(float),
                        group[value_column].to_numpy(float),
                    )
                )
            )
    return segments


def add_band_boundaries(axes, data: pd.DataFrame) -> None:
    for point, label in ((101, "10 Hz band"), (201, "100 Hz band")):
        rows = data.loc[data["sweep_point"] == point, "stored_time_s"]
        if rows.empty:
            continue
        boundary = float(rows.iloc[0])
        for axis in axes:
            axis.axvline(
                boundary,
                color=COLORS["boundary"],
                linewidth=0.9,
                linestyle="--",
                alpha=0.75,
            )
        axes[-1].annotate(
            label,
            xy=(boundary, 0.985),
            xycoords=("data", "axes fraction"),
            xytext=(4, 0),
            textcoords="offset points",
            va="top",
            ha="left",
            fontsize=8,
            color=COLORS["boundary"],
            rotation=90,
        )


def plot_full_record(data: pd.DataFrame, output_dir: Path) -> tuple[Path, Path]:
    fig, axes = plt.subplots(
        3,
        1,
        figsize=(11.5, 7.5),
        sharex=True,
        gridspec_kw={"height_ratios": [1.0, 1.0, 0.72], "hspace": 0.12},
    )
    x_min = float(data["stored_time_s"].min())
    x_max = float(data["stored_time_s"].max())

    for axis, column, color, label in (
        (axes[0], "ch1_voltage_v", COLORS["ch1"], "CH1 voltage (V)"),
        (axes[1], "ch2_voltage_v", COLORS["ch2"], "CH2 voltage (V)"),
    ):
        collection = LineCollection(
            segmented_lines(data, column),
            colors=color,
            linewidths=0.55,
            alpha=0.9,
            rasterized=True,
        )
        axis.add_collection(collection)
        y_min = float(data[column].min())
        y_max = float(data[column].max())
        padding = max((y_max - y_min) * 0.08, 1e-6)
        axis.set_xlim(x_min, x_max)
        axis.set_ylim(y_min - padding, y_max + padding)
        axis.set_ylabel(label)
        axis.axhline(0.0, color="#777777", linewidth=0.6, alpha=0.7)

    summary = (
        data.groupby("sweep_point", sort=True)
        .agg(stored_time_s=("stored_time_s", "median"), frequency_hz=("frequency_hz", "first"))
        .reset_index()
    )
    axes[2].step(
        summary["stored_time_s"],
        summary["frequency_hz"],
        where="mid",
        color=COLORS["frequency"],
        linewidth=1.4,
    )
    axes[2].set_yscale("log")
    axes[2].set_ylabel("Frequency (Hz)")
    axes[2].set_xlabel("Concatenated saved time (s)")
    axes[2].set_xlim(x_min, x_max)
    axes[2].set_ylim(float(data["frequency_hz"].min()) * 0.8, float(data["frequency_hz"].max()) * 1.25)
    add_band_boundaries(axes, data)

    fig.text(
        0.5,
        0.005,
        "Only cycles 2-4 of each sweep point are shown; adjacent frequency blocks are not connected.",
        ha="center",
        va="bottom",
        fontsize=8.5,
        color="#444444",
    )
    fig.subplots_adjust(left=0.085, right=0.985, top=0.985, bottom=0.09)

    png = output_dir / "MHO98_sweep_waveforms_full.png"
    pdf = output_dir / "MHO98_sweep_waveforms_full.pdf"
    fig.savefig(png, dpi=300)
    fig.savefig(pdf)
    plt.close(fig)
    return png, pdf


def nearest_frequency(available: np.ndarray, target: float) -> float:
    return float(available[np.argmin(np.abs(available - target))])


def plot_representative_details(data: pd.DataFrame, output_dir: Path) -> tuple[Path, Path]:
    available = np.sort(data["frequency_hz"].unique())
    targets = [0.1, 1.0, 5.0, 10.0, 100.0, 500.0]
    selected = []
    for target in targets:
        frequency = nearest_frequency(available, target)
        if frequency not in selected:
            selected.append(frequency)

    fig, axes = plt.subplots(2, 3, figsize=(11.5, 6.3), sharex=True, sharey=True)
    for axis, frequency in zip(axes.flat, selected):
        group = data.loc[np.isclose(data["frequency_hz"], frequency)].sort_values("point_time_s")
        cycles = group["point_time_s"].to_numpy(float) * frequency
        axis.plot(
            cycles,
            group["ch1_voltage_v"],
            color=COLORS["ch1"],
            linewidth=1.15,
            label="CH1",
        )
        axis.plot(
            cycles,
            group["ch2_voltage_v"],
            color=COLORS["ch2"],
            linewidth=1.05,
            label="CH2",
        )
        axis.set_title(f"{frequency:.6g} Hz  |  n={len(group)}")
        axis.set_xlim(1.0, 4.0)
        axis.axhline(0.0, color="#777777", linewidth=0.55, alpha=0.7)

    for axis in axes[:, 0]:
        axis.set_ylabel("Voltage (V)")
    for axis in axes[-1, :]:
        axis.set_xlabel("Cycle position")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=2, frameon=False, bbox_to_anchor=(0.5, 0.995))
    fig.text(
        0.5,
        0.012,
        "Representative stored waveforms at selected sweep frequencies (cycles 2-4).",
        ha="center",
        fontsize=8.5,
        color="#444444",
    )
    fig.subplots_adjust(left=0.075, right=0.985, top=0.92, bottom=0.10, hspace=0.28, wspace=0.12)

    png = output_dir / "MHO98_waveform_details.png"
    pdf = output_dir / "MHO98_waveform_details.pdf"
    fig.savefig(png, dpi=300)
    fig.savefig(pdf)
    plt.close(fig)
    return png, pdf


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("output/figures"))
    args = parser.parse_args()

    configure_style()
    data = load_data(args.csv_path)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    full_png, full_pdf = plot_full_record(data, args.output_dir)
    detail_png, detail_pdf = plot_representative_details(data, args.output_dir)
    print(f"rows={len(data)} sweep_points={data['sweep_point'].nunique()}")
    print(f"frequency_range={data['frequency_hz'].min():.12g}..{data['frequency_hz'].max():.12g} Hz")
    for path in (full_png, full_pdf, detail_png, detail_pdf):
        print(path.resolve())


if __name__ == "__main__":
    main()
