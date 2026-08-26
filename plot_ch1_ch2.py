"""Plot CH1/CH2 voltage against time from a controller CSV file."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


# Keep the plotting dependency local to this workspace when present.
WORKSPACE = Path(__file__).resolve().parent
LOCAL_DEPS = WORKSPACE / ".figure_deps"
if LOCAL_DEPS.exists():
    sys.path.insert(0, str(LOCAL_DEPS))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.ticker import FormatStrFormatter, MaxNLocator


TIME_COLUMN = "plot_time_s"
TIME_COLUMN_CANDIDATES = ("sweep_elapsed_s", "stored_time_s")
CHANNELS = (
    ("ch1_voltage_v", "CH1", "#0072B2"),
    ("ch2_voltage_v", "CH2", "#D55E00"),
)


def load_data(path: Path) -> pd.DataFrame:
    data = pd.read_csv(path)
    source_time_column = next(
        (column for column in TIME_COLUMN_CANDIDATES if column in data.columns), None
    )
    if source_time_column is None:
        raise ValueError(
            "The CSV needs sweep_elapsed_s (current format) or stored_time_s "
            "(legacy format)."
        )
    channel_columns = [column for column, _, _ in CHANNELS]
    missing = [column for column in channel_columns if column not in data.columns]
    if missing:
        raise ValueError("The CSV is missing columns: " + ", ".join(missing))
    data = data[[source_time_column, *channel_columns]].rename(
        columns={source_time_column: TIME_COLUMN}
    )
    required = [TIME_COLUMN, *channel_columns]

    if data.empty:
        raise ValueError("The CSV contains no samples.")
    if data[required].isna().any().any():
        raise ValueError("The time or voltage columns contain missing values.")
    if not data[TIME_COLUMN].is_monotonic_increasing:
        data = data.sort_values(TIME_COLUMN, kind="stable")

    return data


def create_figure(data: pd.DataFrame, output_stem: Path) -> tuple[Path, Path]:
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "DejaVu Sans"],
            "font.size": 10,
            "axes.labelsize": 11,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "legend.fontsize": 10,
            "figure.dpi": 300,
            "savefig.dpi": 300,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )

    fig, axes = plt.subplots(
        2,
        1,
        figsize=(10, 6.6),
        sharex=True,
        gridspec_kw={"hspace": 0.10},
    )

    time_s = data[TIME_COLUMN].to_numpy()
    for axis, (column, label, color) in zip(axes, CHANNELS):
        axis.plot(
            time_s,
            data[column].to_numpy(),
            color=color,
            linewidth=0.75,
            label=label,
            rasterized=False,
        )
        axis.set_ylabel("Voltage (V)")
        axis.yaxis.set_major_locator(MaxNLocator(nbins=6))
        if label == "CH2":
            axis.yaxis.set_major_formatter(FormatStrFormatter("%.4f"))
        axis.grid(True, which="major", color="#D0D0D0", linewidth=0.55, alpha=0.75)
        axis.legend(loc="upper right", frameon=False, handlelength=2.3)
        axis.margins(x=0)

    axes[-1].set_xlabel("Time (s)")
    axes[-1].xaxis.set_major_locator(MaxNLocator(nbins=10))

    fig.align_ylabels(axes)
    fig.subplots_adjust(left=0.105, right=0.985, bottom=0.105, top=0.985)

    output_stem.parent.mkdir(parents=True, exist_ok=True)
    png_path = output_stem.with_suffix(".png")
    pdf_path = output_stem.with_suffix(".pdf")
    fig.savefig(png_path, dpi=300, bbox_inches="tight")
    fig.savefig(pdf_path, bbox_inches="tight")
    plt.close(fig)
    return png_path, pdf_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, help="controller waveform CSV")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/figures"),
        help="directory for generated PNG/PDF files (default: output/figures)",
    )
    parser.add_argument(
        "--output-stem",
        default="MHO98_CH1_CH2_voltage_vs_time",
        help="output filename without an extension",
    )
    args = parser.parse_args()

    data = load_data(args.csv_path)
    png_path, pdf_path = create_figure(data, args.output_dir / args.output_stem)

    dt = data[TIME_COLUMN].diff().dropna().median()
    print(f"Samples: {len(data):,}")
    print(
        f"Time range: {data[TIME_COLUMN].iloc[0]:.6f} to "
        f"{data[TIME_COLUMN].iloc[-1]:.6f} s"
    )
    print(f"Median sampling interval: {dt:.6f} s")
    for column, label, _ in CHANNELS:
        print(f"{label} range: {data[column].min():.9f} to {data[column].max():.9f} V")
    print(f"PNG: {png_path.resolve()}")
    print(f"PDF: {pdf_path.resolve()}")


if __name__ == "__main__":
    main()
