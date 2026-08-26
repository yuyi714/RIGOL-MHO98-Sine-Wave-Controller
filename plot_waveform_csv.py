"""Plot arbitrary MHO98 sweep waveforms from current or legacy controller CSV."""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

_FIGURE_DEPS = Path(__file__).with_name(".figure_deps")
if _FIGURE_DEPS.is_dir() and str(_FIGURE_DEPS) not in sys.path:
    sys.path.insert(0, str(_FIGURE_DEPS))

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection


TIME_COLUMN = "plot_time_s"
TIME_COLUMN_CANDIDATES = ("sweep_elapsed_s", "stored_time_s")
COLORS = {
    1: "#0072B2",
    2: "#D55E00",
    3: "#009E73",
    4: "#CC79A7",
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


def load_data(path: Path, channels: tuple[int, ...]) -> pd.DataFrame:
    data = pd.read_csv(path)
    source_time_column = next(
        (column for column in TIME_COLUMN_CANDIDATES if column in data.columns), None
    )
    if source_time_column is None:
        raise ValueError(
            "CSV needs sweep_elapsed_s (current format) or stored_time_s (legacy format)."
        )
    channel_columns = [f"ch{channel}_voltage_v" for channel in channels]
    required = {
        "sample_index",
        "sweep_point",
        "frequency_hz",
        "point_time_s",
        source_time_column,
        *channel_columns,
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
    data[TIME_COLUMN] = data[source_time_column]
    return data


def segmented_lines(data: pd.DataFrame, value_column: str) -> list[np.ndarray]:
    segments: list[np.ndarray] = []
    for _, group in data.groupby("sweep_point", sort=True):
        if len(group) >= 2:
            segments.append(
                np.column_stack(
                    (
                        group[TIME_COLUMN].to_numpy(float),
                        group[value_column].to_numpy(float),
                    )
                )
            )
    return segments


def add_decade_boundaries(axes, summary: pd.DataFrame) -> None:
    previous_decade: int | None = None
    for row in summary.itertuples(index=False):
        frequency = float(row.frequency_hz)
        decade = math.floor(math.log10(frequency))
        if previous_decade is not None and decade != previous_decade:
            boundary = float(getattr(row, TIME_COLUMN))
            for axis in axes:
                axis.axvline(
                    boundary,
                    color=COLORS["boundary"],
                    linewidth=0.8,
                    linestyle="--",
                    alpha=0.65,
                )
            axes[-1].annotate(
                f"{frequency:.4g} Hz",
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
        previous_decade = decade


def plot_full_record(
    data: pd.DataFrame, output_dir: Path, channels: tuple[int, ...]
) -> tuple[Path, Path]:
    figure_height = 2.0 * len(channels) + 2.2
    fig, axes = plt.subplots(
        len(channels) + 1,
        1,
        figsize=(11.5, figure_height),
        sharex=True,
        gridspec_kw={
            "height_ratios": [*[1.0] * len(channels), 0.72],
            "hspace": 0.12,
        },
    )
    x_min = float(data[TIME_COLUMN].min())
    x_max = float(data[TIME_COLUMN].max())

    for axis, channel in zip(axes[:-1], channels):
        column = f"ch{channel}_voltage_v"
        collection = LineCollection(
            segmented_lines(data, column),
            colors=COLORS[channel],
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
        axis.set_ylabel(f"CH{channel} (V)")
        axis.axhline(0.0, color="#777777", linewidth=0.6, alpha=0.7)

    summary = (
        data.groupby("sweep_point", sort=True)
        .agg(**{TIME_COLUMN: (TIME_COLUMN, "median")}, frequency_hz=("frequency_hz", "first"))
        .reset_index()
    )
    frequency_axis = axes[-1]
    frequency_axis.step(
        summary[TIME_COLUMN],
        summary["frequency_hz"],
        where="mid",
        color=COLORS["frequency"],
        linewidth=1.4,
    )
    frequency_axis.set_yscale("log")
    frequency_axis.set_ylabel("Frequency (Hz)")
    frequency_axis.set_xlabel("Sweep elapsed time (s)")
    frequency_axis.set_xlim(x_min, x_max)
    minimum_frequency = float(data["frequency_hz"].min())
    maximum_frequency = float(data["frequency_hz"].max())
    if minimum_frequency == maximum_frequency:
        frequency_axis.set_ylim(minimum_frequency / 1.25, maximum_frequency * 1.25)
    else:
        frequency_axis.set_ylim(minimum_frequency * 0.8, maximum_frequency * 1.25)
    add_decade_boundaries(axes, summary)

    fig.text(
        0.5,
        0.005,
        "Frequency blocks are separated; selected channels come from one frozen frame per point.",
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


def representative_frequencies(available: np.ndarray, count: int = 6) -> list[float]:
    if len(available) <= count:
        return [float(value) for value in available]
    targets = np.geomspace(float(available.min()), float(available.max()), count)
    selected: list[float] = []
    for target in targets:
        frequency = nearest_frequency(available, float(target))
        if frequency not in selected:
            selected.append(frequency)
    return selected


def plot_representative_details(
    data: pd.DataFrame, output_dir: Path, channels: tuple[int, ...]
) -> tuple[Path, Path]:
    available = np.sort(data["frequency_hz"].unique())
    selected = representative_frequencies(available)

    fig, axes = plt.subplots(2, 3, figsize=(11.5, 6.3), squeeze=False)
    for axis, frequency in zip(axes.flat, selected):
        group = data.loc[np.isclose(data["frequency_hz"], frequency)].sort_values(
            "point_time_s"
        )
        cycles = group["point_time_s"].to_numpy(float) * frequency
        for channel in channels:
            axis.plot(
                cycles,
                group[f"ch{channel}_voltage_v"],
                color=COLORS[channel],
                linewidth=1.05,
                label=f"CH{channel}",
            )
        axis.set_title(f"{frequency:.6g} Hz  |  n={len(group)}")
        axis.axhline(0.0, color="#777777", linewidth=0.55, alpha=0.7)
    for axis in list(axes.flat)[len(selected):]:
        axis.set_visible(False)
    for axis in axes[:, 0]:
        axis.set_ylabel("Voltage (V)")
    for axis in axes[-1, :]:
        axis.set_xlabel("Cycle position")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(channels), frameon=False)
    fig.subplots_adjust(left=0.075, right=0.985, top=0.92, bottom=0.10, hspace=0.28, wspace=0.18)

    png = output_dir / "MHO98_waveform_details.png"
    pdf = output_dir / "MHO98_waveform_details.pdf"
    fig.savefig(png, dpi=300)
    fig.savefig(pdf)
    plt.close(fig)
    return png, pdf


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("output/figures"))
    parser.add_argument(
        "--channels",
        type=int,
        nargs="+",
        default=(1, 2),
        metavar="CH",
        help="channels to plot, for example: --channels 1 2 3 4",
    )
    args = parser.parse_args()
    channels = tuple(dict.fromkeys(args.channels))
    if not channels or any(channel not in range(1, 5) for channel in channels):
        parser.error("--channels accepts CH numbers 1 through 4")

    configure_style()
    data = load_data(args.csv_path, channels)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    full_png, full_pdf = plot_full_record(data, args.output_dir, channels)
    detail_png, detail_pdf = plot_representative_details(data, args.output_dir, channels)
    print(f"rows={len(data)} sweep_points={data['sweep_point'].nunique()}")
    print(f"channels={','.join(map(str, channels))}")
    print(
        f"frequency_range={data['frequency_hz'].min():.12g}.."
        f"{data['frequency_hz'].max():.12g} Hz"
    )
    for path in (full_png, full_pdf, detail_png, detail_pdf):
        print(path.resolve())


if __name__ == "__main__":
    main()
