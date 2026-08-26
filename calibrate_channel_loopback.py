"""Calibrate MHO98 input-channel amplitude/phase mismatch with one AFG loopback."""
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from mho98_controller import (
    Instrument,
    acquire_complete_cycles,
    fit_sine_at_frequency,
    make_sweep_frequencies,
    sweep_point_dwell_duration,
)
from windows_usbtmc import list_usbtmc_devices


RAW_FIELDS = (
    "capture_timestamp_utc",
    "repeat",
    "reference_channel",
    "channel",
    "requested_frequency_hz",
    "frequency_hz",
    "requested_amplitude_vpp",
    "amplitude_vpp",
    "timebase_scale_s",
    "waveform_span_s",
    "sample_interval_s",
    "sample_count",
    "incomplete_frame_retries",
    "reference_fit_amplitude_v",
    "channel_fit_amplitude_v",
    "amplitude_ratio",
    "phase_relative_to_reference_deg",
    "apparent_delay_relative_to_reference_s",
    "reference_fit_r2",
    "channel_fit_r2",
)


def wrap_phase_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def summarize_measurements(
    measurements: list[dict[str, float | int | str]],
    channels: tuple[int, ...],
    reference_channel: int,
) -> list[dict[str, float | int]]:
    """Fit one constant channel delay from phase-versus-frequency measurements."""
    summaries: list[dict[str, float | int]] = []
    for channel in channels:
        if channel == reference_channel:
            summaries.append(
                {
                    "channel": channel,
                    "reference_channel": reference_channel,
                    "frequency_points": 0,
                    "delay_relative_to_reference_s": 0.0,
                    "phase_intercept_deg": 0.0,
                    "phase_fit_rms_deg": 0.0,
                    "mean_amplitude_ratio": 1.0,
                    "minimum_fit_r2": 1.0,
                }
            )
            continue

        channel_rows = [
            row for row in measurements if int(row["channel"]) == channel
        ]
        grouped: dict[float, list[dict[str, float | int | str]]] = defaultdict(list)
        for row in channel_rows:
            grouped[float(row["frequency_hz"])].append(row)
        if not grouped:
            raise ValueError(f"CH{channel} 没有可用的校准数据。")

        frequencies: list[float] = []
        phases_rad: list[float] = []
        ratios: list[float] = []
        r_squared: list[float] = []
        for frequency in sorted(grouped):
            rows = grouped[frequency]
            phase_values = np.deg2rad(
                [float(row["phase_relative_to_reference_deg"]) for row in rows]
            )
            circular_mean = float(
                np.angle(np.mean(np.exp(1j * np.asarray(phase_values))))
            )
            frequencies.append(frequency)
            phases_rad.append(circular_mean)
            ratios.extend(float(row["amplitude_ratio"]) for row in rows)
            r_squared.extend(
                min(float(row["reference_fit_r2"]), float(row["channel_fit_r2"]))
                for row in rows
            )

        frequency_array = np.asarray(frequencies, dtype=float)
        unwrapped_phase = np.unwrap(np.asarray(phases_rad, dtype=float))
        if len(frequency_array) >= 2 and np.ptp(frequency_array) > 0:
            slope, intercept = np.polyfit(frequency_array, unwrapped_phase, 1)
            fitted_phase = slope * frequency_array + intercept
            delay_s = -float(slope) / (2.0 * math.pi)
            residual_rms_deg = math.degrees(
                float(np.sqrt(np.mean((unwrapped_phase - fitted_phase) ** 2)))
            )
            intercept_deg = math.degrees(float(intercept))
        else:
            frequency = float(frequency_array[0])
            delay_s = -float(unwrapped_phase[0]) / (2.0 * math.pi * frequency)
            residual_rms_deg = 0.0
            intercept_deg = 0.0

        summaries.append(
            {
                "channel": channel,
                "reference_channel": reference_channel,
                "frequency_points": len(frequency_array),
                "delay_relative_to_reference_s": delay_s,
                "phase_intercept_deg": intercept_deg,
                "phase_fit_rms_deg": residual_rms_deg,
                "mean_amplitude_ratio": float(np.mean(ratios)),
                "minimum_fit_r2": float(min(r_squared)),
            }
        )
    return summaries


def connect_instrument(instrument: Instrument, args: argparse.Namespace) -> str:
    if args.lan:
        return instrument.connect_lan(args.lan)
    if args.visa:
        return instrument.connect_visa(args.visa, label="校准 VISA")

    native_devices = list_usbtmc_devices()
    if native_devices:
        if args.usb_index >= len(native_devices):
            raise RuntimeError(
                f"只发现 {len(native_devices)} 台原生 USBTMC 设备，"
                f"无法选择索引 {args.usb_index}。"
            )
        return instrument.connect_native_usb(native_devices[args.usb_index].path)

    try:
        import pyvisa
    except ImportError as exc:
        raise RuntimeError(
            "未发现原生 USBTMC 设备，并且未安装 PyVISA。"
        ) from exc
    resources = [
        resource
        for resource in pyvisa.ResourceManager().list_resources()
        if str(resource).upper().startswith("USB")
    ]
    if args.usb_index >= len(resources):
        raise RuntimeError(
            f"PyVISA 只发现 {len(resources)} 台 USB 仪器，"
            f"无法选择索引 {args.usb_index}。"
        )
    return instrument.connect_visa(resources[args.usb_index], label="校准 USB-VISA")


def validate_scope_channels(instrument: Instrument, channels: tuple[int, ...]) -> None:
    disabled = [
        channel
        for channel in channels
        if instrument.query(f":CHANnel{channel}:DISPlay?").strip().upper()
        not in {"1", "ON"}
    ]
    if disabled:
        labels = ", ".join(f"CH{channel}" for channel in disabled)
        raise RuntimeError(f"请先在示波器上开启这些输入通道：{labels}。")


def collect_measurements(
    instrument: Instrument,
    frequencies: list[float],
    channels: tuple[int, ...],
    reference_channel: int,
    afg_channel: int,
    amplitude_vpp: float,
    capture_cycles: int,
    settle_cycles: float,
    repeats: int,
) -> list[dict[str, float | int | str]]:
    measurements: list[dict[str, float | int | str]] = []
    for point_number, requested_frequency in enumerate(frequencies, start=1):
        configuration = instrument.configure_sweep_point(
            channel=afg_channel,
            frequency=requested_frequency,
            amplitude=amplitude_vpp,
            capture_cycles=capture_cycles,
            is_current=lambda: True,
        )
        if configuration is None:
            raise RuntimeError("校准频点配置被意外取消。")
        frequency = configuration.actual_frequency_hz
        print(
            f"[{point_number}/{len(frequencies)}] "
            f"设定/回读 {requested_frequency:.9g}/{frequency:.9g} Hz，"
            f"时基 {configuration.timebase_scale_s:.9g} s/div"
        )

        for repeat in range(1, repeats + 1):
            wait_cycles = capture_cycles + settle_cycles
            time.sleep(sweep_point_dwell_duration(frequency, wait_cycles))
            result = acquire_complete_cycles(
                instrument=instrument,
                channels=channels,
                frequency=frequency,
                capture_cycles=capture_cycles,
                on_retry=lambda attempt, error: print(
                    f"  波形未完整，第 {attempt} 次等待重试：{error}"
                ),
            )
            if result is None:
                raise RuntimeError("校准采集被意外取消。")
            (
                capture,
                values,
                point_times,
                _instrument_times,
                sample_interval,
                retry_count,
            ) = result
            fits = {
                channel: fit_sine_at_frequency(
                    point_times,
                    values[channel],
                    frequency,
                )
                for channel in channels
            }
            reference_amplitude, reference_phase, reference_r2 = fits[
                reference_channel
            ]
            if reference_amplitude <= 1e-15:
                raise RuntimeError("参考通道拟合幅度接近零，请检查回环接线和量程。")
            reference_frame = capture.frames[reference_channel]
            waveform_span = (
                max(0, len(reference_frame.values) - 1)
                * reference_frame.x_increment
            )
            for channel in channels:
                amplitude, phase, r2 = fits[channel]
                relative_phase = wrap_phase_degrees(phase - reference_phase)
                apparent_delay = -relative_phase / (360.0 * frequency)
                measurements.append(
                    {
                        "capture_timestamp_utc": capture.captured_at_utc.isoformat(
                            timespec="milliseconds"
                        ),
                        "repeat": repeat,
                        "reference_channel": reference_channel,
                        "channel": channel,
                        "requested_frequency_hz": requested_frequency,
                        "frequency_hz": frequency,
                        "requested_amplitude_vpp": amplitude_vpp,
                        "amplitude_vpp": configuration.actual_amplitude_vpp,
                        "timebase_scale_s": configuration.timebase_scale_s,
                        "waveform_span_s": waveform_span,
                        "sample_interval_s": sample_interval,
                        "sample_count": len(point_times),
                        "incomplete_frame_retries": retry_count,
                        "reference_fit_amplitude_v": reference_amplitude,
                        "channel_fit_amplitude_v": amplitude,
                        "amplitude_ratio": amplitude / reference_amplitude,
                        "phase_relative_to_reference_deg": relative_phase,
                        "apparent_delay_relative_to_reference_s": apparent_delay,
                        "reference_fit_r2": reference_r2,
                        "channel_fit_r2": r2,
                    }
                )
            print(
                f"  重复 {repeat}/{repeats}：{len(point_times)} 点，"
                f"dt={sample_interval:.6g} s"
            )
    return measurements


def write_outputs(
    output_dir: Path,
    measurements: list[dict[str, float | int | str]],
    summaries: list[dict[str, float | int]],
    instrument_id: str,
    channels: tuple[int, ...],
    reference_channel: int,
) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = output_dir / f"channel_loopback_raw_{stamp}.csv"
    summary_path = output_dir / f"channel_loopback_summary_{stamp}.csv"
    json_path = output_dir / f"channel_loopback_calibration_{stamp}.json"

    with raw_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=RAW_FIELDS)
        writer.writeheader()
        writer.writerows(measurements)

    summary_fields = tuple(summaries[0])
    with summary_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(summaries)

    calibration = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "instrument_id": instrument_id,
        "reference_channel": reference_channel,
        "channels": list(channels),
        "positive_delay_definition": (
            "positive means this channel is measured later than the reference channel"
        ),
        "phase_correction": (
            "corrected_response_minus_reference_deg = measured_deg + "
            "360 * frequency_hz * (response_delay_s - reference_delay_s)"
        ),
        "channel_calibrations": {
            str(int(row["channel"])): row for row in summaries
        },
    }
    json_path.write_text(
        json.dumps(calibration, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return raw_path, summary_path, json_path


def save_plot(
    output_dir: Path,
    measurements: list[dict[str, float | int | str]],
    summaries: list[dict[str, float | int]],
    reference_channel: int,
) -> Path | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        print(f"未生成校准图：{exc}")
        return None

    fig, (phase_axis, ratio_axis) = plt.subplots(
        2, 1, figsize=(9, 7), sharex=True, constrained_layout=True
    )
    for summary in summaries:
        channel = int(summary["channel"])
        if channel == reference_channel:
            continue
        rows = [row for row in measurements if int(row["channel"]) == channel]
        frequencies = np.asarray([float(row["frequency_hz"]) for row in rows])
        phases = np.asarray(
            [float(row["phase_relative_to_reference_deg"]) for row in rows]
        )
        ratios = np.asarray([float(row["amplitude_ratio"]) for row in rows])
        phase_axis.scatter(frequencies, phases, s=18, label=f"CH{channel}")
        ratio_axis.scatter(frequencies, ratios, s=18, label=f"CH{channel}")
    phase_axis.axhline(0.0, color="#777777", linewidth=0.7)
    ratio_axis.axhline(1.0, color="#777777", linewidth=0.7)
    phase_axis.set_ylabel(f"Phase vs CH{reference_channel} (deg)")
    ratio_axis.set_ylabel(f"Amplitude / CH{reference_channel}")
    ratio_axis.set_xlabel("Frequency (Hz)")
    ratio_axis.set_xscale("log")
    phase_axis.grid(True, which="both", alpha=0.25)
    ratio_axis.grid(True, which="both", alpha=0.25)
    phase_axis.legend(frameon=False)
    ratio_axis.legend(frameon=False)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"channel_loopback_calibration_{stamp}.png"
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument("--lan", metavar="IP", help="connect by LAN SCPI")
    connection.add_argument("--visa", metavar="RESOURCE", help="connect by VISA")
    parser.add_argument(
        "--usb-index",
        type=int,
        default=0,
        help="auto USB device index when --lan/--visa is omitted (default: 0)",
    )
    parser.add_argument("--afg-channel", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--channels", type=int, nargs="+", default=(1, 2, 3, 4), metavar="CH"
    )
    parser.add_argument("--reference", type=int, default=1, metavar="CH")
    parser.add_argument("--start-hz", type=float, default=10.0)
    parser.add_argument("--stop-hz", type=float, default=100_000.0)
    parser.add_argument("--spacing", choices=("linear", "log"), default="log")
    parser.add_argument("--linear-points", type=int, default=21)
    parser.add_argument("--points-per-decade", type=int, default=5)
    parser.add_argument(
        "--frequencies",
        type=float,
        nargs="+",
        help="explicit frequencies; overrides sweep range/spacing",
    )
    parser.add_argument("--amplitude-vpp", type=float, default=1.0)
    parser.add_argument("--capture-cycles", type=int, default=8)
    parser.add_argument("--settle-cycles", type=float, default=3.0)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("output/channel_calibration")
    )
    parser.add_argument(
        "--yes", action="store_true", help="skip the physical wiring confirmation"
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    channels = tuple(dict.fromkeys(args.channels))
    if not channels or any(channel not in range(1, 5) for channel in channels):
        parser.error("--channels 只能使用 1、2、3、4")
    if len(channels) < 2:
        parser.error("回环校准至少需要参考通道和另一个待校准通道")
    if args.reference not in channels:
        parser.error("--reference 必须包含在 --channels 中")
    if args.usb_index < 0:
        parser.error("--usb-index 不能为负数")
    if args.capture_cycles < 1 or args.settle_cycles < 0 or args.repeats < 1:
        parser.error("采集周期和重复次数至少为 1，稳定周期不能为负数")

    if args.frequencies:
        frequencies = sorted(set(args.frequencies))
        if any(not 0.002 <= frequency <= 100_000_000 for frequency in frequencies):
            parser.error("显式频率必须在 0.002–100000000 Hz")
    else:
        frequencies = make_sweep_frequencies(
            args.start_hz,
            args.stop_hz,
            args.spacing,
            args.linear_points,
            args.points_per_decade,
        )
    maximum_amplitude = 10.0 if max(frequencies) > 50_000_000 else 20.0
    if not 0.002 <= args.amplitude_vpp <= maximum_amplitude:
        parser.error(
            f"当前频率范围下 --amplitude-vpp 必须在 0.002–{maximum_amplitude:g} Vpp"
        )

    print("回环接线要求：")
    print(
        f"  将 AFG {args.afg_channel} 用等长 BNC/分配器同时接到 "
        + ", ".join(f"CH{channel}" for channel in channels)
    )
    print("  所有输入使用相同探头倍率、带宽限制和耦合；建议全部使用 1 MΩ。")
    print("  不要把四个 50 Ω 输入直接并联到一个 AFG 输出。")
    if not args.yes and input("确认接线后输入 CALIBRATE：").strip() != "CALIBRATE":
        raise SystemExit("已取消，AFG 输出未开启。")

    instrument = Instrument()
    configured = False
    try:
        instrument_id = connect_instrument(instrument, args)
        print("已连接：" + instrument_id)
        validate_scope_channels(instrument, channels)
        configured = True
        measurements = collect_measurements(
            instrument=instrument,
            frequencies=frequencies,
            channels=channels,
            reference_channel=args.reference,
            afg_channel=args.afg_channel,
            amplitude_vpp=args.amplitude_vpp,
            capture_cycles=args.capture_cycles,
            settle_cycles=args.settle_cycles,
            repeats=args.repeats,
        )
    finally:
        if configured and instrument.connected:
            try:
                instrument.finish_sweep(args.afg_channel)
            except Exception as exc:
                print("警告：关闭输出/恢复时基失败：" + str(exc))
        instrument.close()

    summaries = summarize_measurements(measurements, channels, args.reference)
    raw_path, summary_path, json_path = write_outputs(
        args.output_dir,
        measurements,
        summaries,
        instrument_id,
        channels,
        args.reference,
    )
    plot_path = save_plot(
        args.output_dir, measurements, summaries, args.reference
    )
    print("\n校准结果（正值表示该通道相对参考通道更晚）：")
    for summary in summaries:
        channel = int(summary["channel"])
        delay_ns = float(summary["delay_relative_to_reference_s"]) * 1e9
        print(
            f"  CH{channel}: {delay_ns:+.3f} ns，"
            f"幅值比 {float(summary['mean_amplitude_ratio']):.9g}，"
            f"相位拟合 RMS {float(summary['phase_fit_rms_deg']):.6g}°"
        )
    print("原始数据：" + str(raw_path.resolve()))
    print("汇总数据：" + str(summary_path.resolve()))
    print("校准 JSON：" + str(json_path.resolve()))
    if plot_path:
        print("校准图：" + str(plot_path.resolve()))


if __name__ == "__main__":
    main()
