from pathlib import Path
import sys


# Keep the plotting dependency local to this workspace.
WORKSPACE = Path(__file__).resolve().parent
LOCAL_DEPS = WORKSPACE / ".figure_deps"
if LOCAL_DEPS.exists():
    sys.path.insert(0, str(LOCAL_DEPS))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FormatStrFormatter, MaxNLocator
import pandas as pd


CSV_PATH = Path(r"C:\Users\cjt\Desktop\MHO98_CH1_CH2_20260818_172516.csv")
OUTPUT_DIR = WORKSPACE / "plots"
OUTPUT_STEM = OUTPUT_DIR / "MHO98_CH1_CH2_voltage_vs_time"

TIME_COLUMN = "stored_time_s"
CHANNELS = (
    ("ch1_voltage_v", "CH1", "#0072B2"),
    ("ch2_voltage_v", "CH2", "#D55E00"),
)


def load_data() -> pd.DataFrame:
    required = [TIME_COLUMN, *(column for column, _, _ in CHANNELS)]
    data = pd.read_csv(CSV_PATH, usecols=required)

    if data.empty:
        raise ValueError("The CSV contains no samples.")
    if data[required].isna().any().any():
        raise ValueError("The time or voltage columns contain missing values.")
    if not data[TIME_COLUMN].is_monotonic_increasing:
        data = data.sort_values(TIME_COLUMN, kind="stable")

    return data


def create_figure(data: pd.DataFrame) -> None:
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
    for ax, (column, label, color) in zip(axes, CHANNELS):
        ax.plot(
            time_s,
            data[column].to_numpy(),
            color=color,
            linewidth=0.75,
            label=label,
            rasterized=False,
        )
        ax.set_ylabel("Voltage (V)")
        ax.yaxis.set_major_locator(MaxNLocator(nbins=6))
        if label == "CH2":
            ax.yaxis.set_major_formatter(FormatStrFormatter("%.4f"))
        ax.grid(True, which="major", color="#D0D0D0", linewidth=0.55, alpha=0.75)
        ax.legend(loc="upper right", frameon=False, handlelength=2.3)
        ax.margins(x=0)

    axes[-1].set_xlabel("Time (s)")
    axes[-1].xaxis.set_major_locator(MaxNLocator(nbins=10))

    fig.align_ylabels(axes)
    fig.subplots_adjust(left=0.105, right=0.985, bottom=0.105, top=0.985)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT_STEM.with_suffix(".png"), dpi=300, bbox_inches="tight")
    fig.savefig(OUTPUT_STEM.with_suffix(".pdf"), bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    data = load_data()
    create_figure(data)

    dt = data[TIME_COLUMN].diff().dropna().median()
    print(f"Samples: {len(data):,}")
    print(f"Time range: {data[TIME_COLUMN].iloc[0]:.6f} to {data[TIME_COLUMN].iloc[-1]:.6f} s")
    print(f"Median sampling interval: {dt:.6f} s")
    for column, label, _ in CHANNELS:
        print(f"{label} range: {data[column].min():.9f} to {data[column].max():.9f} V")
    print(f"PNG: {OUTPUT_STEM.with_suffix('.png')}")
    print(f"PDF: {OUTPUT_STEM.with_suffix('.pdf')}")


if __name__ == "__main__":
    main()
