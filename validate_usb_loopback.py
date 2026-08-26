"""Run a reversible AFG1-to-input-channel hardware smoke test on an MHO98."""
from __future__ import annotations

import argparse
import csv
import math
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from mho98_controller import (
    Instrument,
    acquire_complete_cycles,
    fit_sine_at_frequency,
    sweep_point_dwell_duration,
    validate_afg_sine_parameters,
)
from windows_usbtmc import list_usbtmc_devices


def estimate_frequency(
    times: list[float], values: list[float], expected_frequency: float
) -> tuple[float, float]:
    """Refine frequency around the AFG readback by maximizing sine-fit R²."""
    center = expected_frequency
    half_width = expected_frequency * 0.01
    best_frequency = center
    best_r_squared = -math.inf
    for _ in range(4):
        candidates = np.linspace(center - half_width, center + half_width, 81)
        for candidate in candidates:
            _amplitude, _phase, r_squared = fit_sine_at_frequency(
                times, values, float(candidate)
            )
            if r_squared > best_r_squared:
                best_frequency = float(candidate)
                best_r_squared = r_squared
        center = best_frequency
        half_width /= 10.0
    return best_frequency, best_r_squared


def query_snapshot(instrument: Instrument, channels: tuple[int, ...]) -> dict[str, str]:
    queries = {
        "timebase_scale": ":TIMebase:MAIN:SCALe?",
        "trigger_status": ":TRIGger:STATus?",
        "trigger_sweep": ":TRIGger:SWEep?",
        "trigger_source": ":TRIGger:EDGE:SOURce?",
        "trigger_level": ":TRIGger:EDGE:LEVel?",
        "afg_frequency": ":SOURce1:FREQuency?",
        "afg_amplitude": ":SOURce1:VOLTage:AMPLitude?",
        "afg_offset": ":SOURce1:VOLTage:OFFSet?",
        "afg_phase": ":SOURce1:PHASe?",
        "afg_output": ":SOURce1:OUTPut:STATe?",
    }
    for channel in channels:
        queries.update(
            {
                f"ch{channel}_display": f":CHANnel{channel}:DISPlay?",
                f"ch{channel}_scale": f":CHANnel{channel}:SCALe?",
                f"ch{channel}_offset": f":CHANnel{channel}:OFFSet?",
                f"ch{channel}_coupling": f":CHANnel{channel}:COUPling?",
                f"ch{channel}_probe": f":CHANnel{channel}:PROBe?",
            }
        )
    return {name: instrument.query(command) for name, command in queries.items()}


def restore_snapshot(
    instrument: Instrument, channels: tuple[int, ...], snapshot: dict[str, str]
) -> None:
    instrument.write(f":SOURce1:FREQuency {snapshot['afg_frequency']}")
    instrument.write(f":SOURce1:VOLTage:AMPLitude {snapshot['afg_amplitude']}")
    instrument.write(f":SOURce1:VOLTage:OFFSet {snapshot['afg_offset']}")
    instrument.write(f":SOURce1:PHASe {snapshot['afg_phase']}")
    instrument.write(f":SOURce1:OUTPut:STATe {snapshot['afg_output']}")
    for channel in channels:
        instrument.write(f":CHANnel{channel}:SCALe {snapshot[f'ch{channel}_scale']}")
        instrument.write(f":CHANnel{channel}:OFFSet {snapshot[f'ch{channel}_offset']}")
        instrument.write(
            f":CHANnel{channel}:COUPling {snapshot[f'ch{channel}_coupling']}"
        )
        instrument.write(
            f":CHANnel{channel}:DISPlay {snapshot[f'ch{channel}_display']}"
        )
    instrument.write(f":TRIGger:EDGE:SOURce {snapshot['trigger_source']}")
    instrument.write(f":TRIGger:EDGE:LEVel {snapshot['trigger_level']}")
    instrument.write(f":TRIGger:SWEep {snapshot['trigger_sweep']}")
    instrument.write(f":TIMebase:MAIN:SCALe {snapshot['timebase_scale']}")
    restored_timebase = float(instrument.query(":TIMebase:MAIN:SCALe?"))
    if not math.isclose(
        restored_timebase,
        float(snapshot["timebase_scale"]),
        rel_tol=1e-6,
        abs_tol=1e-15,
    ):
        raise RuntimeError(
            "时基恢复回读不一致："
            f"目标 {snapshot['timebase_scale']}，实际 {restored_timebase:.12g}。"
        )


def write_results(output_dir: Path, rows: list[dict[str, object]]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"usb_loopback_validation_{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-channel", type=int, choices=(1, 2, 3, 4), default=2)
    parser.add_argument(
        "--channels",
        type=int,
        nargs="+",
        metavar="CH",
        help="channels downloaded from the same frozen frame (default: input channel)",
    )
    parser.add_argument(
        "--frequencies", type=float, nargs="+", default=(10, 100, 1000, 10000)
    )
    parser.add_argument("--amplitude-vpp", type=float, default=1.0)
    parser.add_argument("--offset-v", type=float, default=0.0)
    parser.add_argument("--capture-cycles", type=int, default=3)
    parser.add_argument("--settle-cycles", type=float, default=2.0)
    parser.add_argument("--usb-index", type=int, default=0)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("output/hardware_validation")
    )
    args = parser.parse_args()
    channels = tuple(dict.fromkeys(args.channels or (args.input_channel,)))
    if any(channel not in range(1, 5) for channel in channels):
        parser.error("--channels 只能使用 1、2、3、4")
    if args.input_channel not in channels:
        parser.error("--input-channel 必须包含在 --channels 中")
    if args.usb_index < 0:
        parser.error("--usb-index 不能为负数")
    if any(not 0.002 <= value <= 100_000_000 for value in args.frequencies):
        parser.error("频率必须在 0.002–100000000 Hz")
    try:
        for frequency in args.frequencies:
            validate_afg_sine_parameters(
                frequency, args.amplitude_vpp, args.offset_v
            )
    except ValueError as exc:
        parser.error(str(exc))
    if args.capture_cycles < 1 or args.settle_cycles < 0:
        parser.error("采集周期至少为 1，稳定周期不能为负数")

    devices = list_usbtmc_devices()
    if args.usb_index >= len(devices):
        raise RuntimeError(
            f"只发现 {len(devices)} 台 USBTMC 设备，无法选择 {args.usb_index}。"
        )
    instrument = Instrument()
    snapshot: dict[str, str] | None = None
    sweep_configured = False
    rows: list[dict[str, object]] = []
    restore_errors: list[str] = []
    try:
        identity = instrument.connect_native_usb(devices[args.usb_index].path)
        snapshot = query_snapshot(instrument, channels)
        print("已连接：" + identity)
        print("原始状态：" + repr(snapshot))
        if not math.isclose(
            float(snapshot[f"ch{args.input_channel}_probe"]),
            1.0,
            rel_tol=1e-9,
        ):
            raise RuntimeError(
                f"CH{args.input_channel} 当前探头倍率不是 1×："
                f"{snapshot[f'ch{args.input_channel}_probe']}。请先设置为 1×。"
            )
        instrument.write("*CLS")
        for channel in channels:
            instrument.write(f":CHANnel{channel}:DISPlay ON")
        instrument.write(f":CHANnel{args.input_channel}:COUPling DC")
        instrument.write(
            f":CHANnel{args.input_channel}:SCALe {args.amplitude_vpp / 4.0:.12g}"
        )
        instrument.write(f":CHANnel{args.input_channel}:OFFSet 0")
        instrument.write(f":TRIGger:EDGE:SOURce CHANnel{args.input_channel}")
        instrument.write(f":TRIGger:EDGE:LEVel {args.offset_v:.12g}")
        instrument.write(":TRIGger:SWEep AUTO")
        time.sleep(0.25)

        for point, requested_frequency in enumerate(args.frequencies, start=1):
            configuration = instrument.configure_sweep_point(
                channel=1,
                frequency=requested_frequency,
                amplitude=args.amplitude_vpp,
                capture_cycles=args.capture_cycles,
                is_current=lambda: True,
                offset_v=args.offset_v,
            )
            if configuration is None:
                raise RuntimeError("频点配置被意外取消。")
            sweep_configured = True
            actual_frequency = configuration.actual_frequency_hz
            time.sleep(
                sweep_point_dwell_duration(
                    actual_frequency, args.capture_cycles + args.settle_cycles
                )
            )
            result = acquire_complete_cycles(
                instrument=instrument,
                channels=channels,
                frequency=actual_frequency,
                capture_cycles=args.capture_cycles,
                on_retry=lambda attempt, error: print(
                    f"  帧未完整，第 {attempt} 次等待重试：{error}"
                ),
            )
            if result is None:
                raise RuntimeError("闭环采集被意外取消。")
            (
                capture,
                channel_values,
                point_times,
                instrument_times,
                sample_interval,
                retry_count,
            ) = result
            frame = capture.frames[args.input_channel]
            waveform_span = (len(frame.values) - 1) * frame.x_increment
            print(
                f"  原始帧：n={len(frame.values)}, "
                f"xinc={frame.x_increment:.12g} s, "
                f"xorigin={frame.x_origin:.12g} s, "
                f"span={waveform_span:.12g} s, "
                f"实际时基={configuration.timebase_scale_s:.12g} s/div"
            )
            for channel in channels:
                channel_frame = capture.frames[channel]
                print(
                    f"    CH{channel}: n={len(channel_frame.values)}, "
                    f"xinc={channel_frame.x_increment:.12g}, "
                    f"xorigin={channel_frame.x_origin:.12g}"
                )
            values = channel_values[args.input_channel]
            fitted_amplitude, fitted_phase, fit_r_squared = fit_sine_at_frequency(
                point_times, values, actual_frequency
            )
            measured_frequency, frequency_fit_r_squared = estimate_frequency(
                point_times, values, actual_frequency
            )
            frequency_error_ppm = (
                (measured_frequency - actual_frequency) / actual_frequency * 1e6
            )
            measured_vpp = 2.0 * fitted_amplitude
            measured_offset = float(np.mean(values))
            offset_error = measured_offset - configuration.actual_offset_v
            row = {
                "point": point,
                "captured_channels": "/".join(map(str, channels)),
                "requested_frequency_hz": requested_frequency,
                "afg_readback_frequency_hz": actual_frequency,
                "measured_frequency_hz": measured_frequency,
                "frequency_error_ppm": frequency_error_ppm,
                "requested_amplitude_vpp": args.amplitude_vpp,
                "afg_readback_amplitude_vpp": configuration.actual_amplitude_vpp,
                "requested_offset_v": configuration.requested_offset_v,
                "afg_readback_offset_v": configuration.actual_offset_v,
                "measured_offset_v": measured_offset,
                "offset_error_v": offset_error,
                "requested_phase_deg": configuration.requested_phase_deg,
                "afg_readback_phase_deg": configuration.actual_phase_deg,
                "phase_synchronized": configuration.phase_synchronized,
                "measured_amplitude_vpp": measured_vpp,
                "measured_to_readback_ratio": (
                    measured_vpp / configuration.actual_amplitude_vpp
                ),
                "sine_fit_phase_deg": fitted_phase,
                "sine_fit_r2": fit_r_squared,
                "frequency_fit_r2": frequency_fit_r_squared,
                "timebase_scale_s": configuration.timebase_scale_s,
                "sample_interval_s": sample_interval,
                "waveform_span_s": waveform_span,
                "sample_count": len(values),
                "incomplete_frame_retries": retry_count,
                "instrument_time_start_s": instrument_times[0],
                "instrument_time_stop_s": instrument_times[-1],
                "capture_timestamp_utc": capture.captured_at_utc.isoformat(
                    timespec="milliseconds"
                ),
                "pass": (
                    fit_r_squared >= 0.98
                    and frequency_fit_r_squared >= 0.98
                    and abs(frequency_error_ppm) <= 5000
                    and abs(offset_error) <= max(0.02, args.amplitude_vpp * 0.05)
                ),
            }
            rows.append(row)
            print(
                f"[{point}/{len(args.frequencies)}] "
                f"{actual_frequency:.9g} Hz → {measured_frequency:.9g} Hz "
                f"({frequency_error_ppm:+.2f} ppm), "
                f"CH{args.input_channel}={measured_vpp:.6g} Vpp, "
                f"B={measured_offset:.6g} V, "
                f"R2={fit_r_squared:.9f}, "
                f"时基={configuration.timebase_scale_s:.6g} s/div, "
                f"dt={sample_interval:.6g} s"
            )
    finally:
        if instrument.connected:
            if sweep_configured:
                try:
                    instrument.finish_sweep(1)
                except Exception as exc:
                    restore_errors.append("扫频状态恢复失败：" + str(exc))
            if snapshot is not None:
                try:
                    restore_snapshot(instrument, channels, snapshot)
                except Exception as exc:
                    restore_errors.append("仪器设置恢复失败：" + str(exc))
            try:
                errors = instrument._read_scpi_errors_unlocked()
                restore_errors.extend("仪器错误队列：" + error for error in errors)
            except Exception as exc:
                restore_errors.append("错误队列读取失败：" + str(exc))
            instrument.close()

    if not rows:
        raise RuntimeError("没有完成任何闭环测量。" + "；".join(restore_errors))
    output_path = write_results(args.output_dir, rows)
    passed = sum(bool(row["pass"]) for row in rows)
    print(f"通过 {passed}/{len(rows)} 个频点")
    print("结果文件：" + str(output_path.resolve()))
    if restore_errors:
        print("恢复/错误队列警告：")
        for error in restore_errors:
            print("  " + error)
        raise SystemExit(2)
    if passed != len(rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
