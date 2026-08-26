"""Validate synchronized AFG1/AFG2 phase while sweeping an MHO98.

Physical wiring used by this test:

* AFG1 -> CH2 (reference)
* AFG2 -> CH1 (response)

The test writes and reads back both AFG phases, performs the documented
``:SOURce<n>:PHASe:SYNChronize`` operation after every frequency change (and
before every repeat), then freezes one CH1/CH2 frame and measures CH1-CH2 phase.
"""
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

from calibrate_channel_loopback import summarize_measurements, wrap_phase_degrees
from mho98_controller import (
    Instrument,
    acquire_complete_cycles,
    fit_sine_at_frequency,
    sweep_point_dwell_duration,
)
from windows_usbtmc import list_usbtmc_devices


REFERENCE_CHANNEL = 2
RESPONSE_CHANNEL = 1
REFERENCE_AFG = 1
RESPONSE_AFG = 2


def query_snapshot(instrument: Instrument) -> dict[str, str]:
    queries = {
        "timebase_scale": ":TIMebase:MAIN:SCALe?",
        "trigger_status": ":TRIGger:STATus?",
        "trigger_sweep": ":TRIGger:SWEep?",
        "trigger_source": ":TRIGger:EDGE:SOURce?",
        "trigger_level": ":TRIGger:EDGE:LEVel?",
    }
    for channel in (1, 2):
        queries.update(
            {
                f"ch{channel}_display": f":CHANnel{channel}:DISPlay?",
                f"ch{channel}_scale": f":CHANnel{channel}:SCALe?",
                f"ch{channel}_offset": f":CHANnel{channel}:OFFSet?",
                f"ch{channel}_coupling": f":CHANnel{channel}:COUPling?",
                f"ch{channel}_probe": f":CHANnel{channel}:PROBe?",
                f"afg{channel}_function": f":SOURce{channel}:FUNCtion?",
                f"afg{channel}_frequency": f":SOURce{channel}:FREQuency?",
                f"afg{channel}_amplitude": (
                    f":SOURce{channel}:VOLTage:AMPLitude?"
                ),
                f"afg{channel}_offset": f":SOURce{channel}:VOLTage:OFFSet?",
                f"afg{channel}_phase": f":SOURce{channel}:PHASe?",
                f"afg{channel}_output": f":SOURce{channel}:OUTPut:STATe?",
            }
        )
    return {name: instrument.query(command) for name, command in queries.items()}


def restore_snapshot(instrument: Instrument, snapshot: dict[str, str]) -> None:
    for afg in (1, 2):
        instrument.write(f":SOURce{afg}:OUTPut:STATe OFF")
    for afg in (1, 2):
        instrument.write(
            f":SOURce{afg}:FUNCtion {snapshot[f'afg{afg}_function']}"
        )
        instrument.write(
            f":SOURce{afg}:FREQuency {snapshot[f'afg{afg}_frequency']}"
        )
        instrument.write(
            f":SOURce{afg}:VOLTage:AMPLitude {snapshot[f'afg{afg}_amplitude']}"
        )
        instrument.write(
            f":SOURce{afg}:VOLTage:OFFSet {snapshot[f'afg{afg}_offset']}"
        )
        instrument.write(f":SOURce{afg}:PHASe {snapshot[f'afg{afg}_phase']}")
        instrument.write(
            f":SOURce{afg}:OUTPut:STATe {snapshot[f'afg{afg}_output']}"
        )
    for channel in (1, 2):
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
    instrument.write(
        ":STOP" if snapshot["trigger_status"].strip().upper() == "STOP" else ":RUN"
    )
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


def align_both_afgs(instrument: Instrument, phase_deg: float) -> tuple[float, float]:
    for afg in (1, 2):
        instrument.write(f":SOURce{afg}:PHASe {phase_deg:.12g}")
    instrument.write(":SOURce1:PHASe:SYNChronize")
    instrument.write("*WAI")
    phases = tuple(
        float(instrument.query(f":SOURce{afg}:PHASe?")) for afg in (1, 2)
    )
    errors = instrument._read_scpi_errors_unlocked()
    if errors:
        raise RuntimeError("双路相位对齐后仪器错误队列：" + "；".join(errors))
    return phases


def configure_afg2(
    instrument: Instrument,
    frequency_hz: float,
    amplitude_vpp: float,
    phase_deg: float,
) -> tuple[float, float, float]:
    instrument.write(":SOURce2:FUNCtion SINusoid")
    instrument.write(f":SOURce2:FREQuency {frequency_hz:.12g}")
    instrument.write(":SOURce2:VOLTage:OFFSet 0")
    instrument.write(f":SOURce2:VOLTage:AMPLitude {amplitude_vpp:.12g}")
    instrument.write(f":SOURce2:PHASe {phase_deg:.12g}")
    actual_frequency = float(instrument.query(":SOURce2:FREQuency?"))
    actual_amplitude = float(instrument.query(":SOURce2:VOLTage:AMPLitude?"))
    actual_phase = float(instrument.query(":SOURce2:PHASe?"))
    instrument.write(":SOURce2:OUTPut:STATe ON")
    return actual_frequency, actual_amplitude, actual_phase


def phase_repeatability(rows: list[dict[str, object]]) -> list[dict[str, float]]:
    grouped: dict[float, list[float]] = defaultdict(list)
    for row in rows:
        grouped[float(row["frequency_hz"])].append(
            float(row["phase_ch1_minus_ch2_deg"])
        )
    result: list[dict[str, float]] = []
    for frequency, phases in sorted(grouped.items()):
        radians = np.deg2rad(np.asarray(phases, dtype=float))
        mean_deg = math.degrees(float(np.angle(np.mean(np.exp(1j * radians)))))
        deviations = np.asarray(
            [wrap_phase_degrees(value - mean_deg) for value in phases], dtype=float
        )
        result.append(
            {
                "frequency_hz": frequency,
                "circular_mean_phase_deg": mean_deg,
                "phase_repeatability_rms_deg": float(
                    np.sqrt(np.mean(deviations**2))
                ),
                "phase_repeatability_peak_to_peak_deg": float(
                    np.ptp(deviations) if len(deviations) > 1 else 0.0
                ),
            }
        )
    return result


def write_outputs(
    output_dir: Path,
    rows: list[dict[str, object]],
    repeatability: list[dict[str, float]],
    overall: dict[str, object],
) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = output_dir / f"dual_afg_phase_raw_{stamp}.csv"
    summary_path = output_dir / f"dual_afg_phase_summary_{stamp}.csv"
    json_path = output_dir / f"dual_afg_phase_result_{stamp}.json"
    with raw_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with summary_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=tuple(repeatability[0]))
        writer.writeheader()
        writer.writerows(repeatability)
    json_path.write_text(
        json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return raw_path, summary_path, json_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--frequencies",
        type=float,
        nargs="+",
        default=(100_000, 200_000, 500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000),
    )
    parser.add_argument("--amplitude-vpp", type=float, default=1.0)
    parser.add_argument("--phase-deg", type=float, default=0.0)
    parser.add_argument("--capture-cycles", type=int, default=8)
    parser.add_argument("--settle-cycles", type=float, default=3.0)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--passes",
        type=int,
        default=2,
        help="number of sweeps; even passes run in descending order",
    )
    parser.add_argument("--usb-index", type=int, default=0)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("output/phase_validation")
    )
    parser.add_argument("--yes", action="store_true", help="skip wiring confirmation")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    frequencies = sorted(set(args.frequencies))
    if not frequencies or any(not 0.002 <= value <= 100_000_000 for value in frequencies):
        parser.error("频率必须在 0.002–100000000 Hz")
    if not 0.002 <= args.amplitude_vpp <= 20.0:
        parser.error("幅度必须在 0.002–20 Vpp")
    if not 0.0 <= args.phase_deg <= 360.0:
        parser.error("相位必须在 0–360°")
    if (
        args.capture_cycles < 1
        or args.settle_cycles < 0
        or args.repeats < 1
        or args.passes < 1
    ):
        parser.error("采集周期、重复次数和扫频轮数至少为 1，稳定周期不能为负数")
    if args.usb_index < 0:
        parser.error("--usb-index 不能为负数")

    print("双 AFG 相位验证接线：AFG1→CH2（参考），AFG2→CH1（响应）。")
    print("测得相位包含 AFG 双路、两根 BNC 线和 CH1/CH2 输入链路的总差异。")
    if not args.yes and input("确认接线后输入 PHASE：").strip() != "PHASE":
        raise SystemExit("已取消，AFG 输出未开启。")

    devices = list_usbtmc_devices()
    if args.usb_index >= len(devices):
        raise RuntimeError(
            f"只发现 {len(devices)} 台 USBTMC 设备，无法选择 {args.usb_index}。"
        )

    instrument = Instrument()
    snapshot: dict[str, str] | None = None
    rows: list[dict[str, object]] = []
    restore_errors: list[str] = []
    instrument_id = ""
    try:
        instrument_id = instrument.connect_native_usb(devices[args.usb_index].path)
        snapshot = query_snapshot(instrument)
        print("已连接：" + instrument_id)
        for channel in (1, 2):
            if not math.isclose(
                float(snapshot[f"ch{channel}_probe"]), 1.0, rel_tol=1e-9
            ):
                raise RuntimeError(
                    f"CH{channel} 探头倍率不是 1×："
                    f"{snapshot[f'ch{channel}_probe']}。"
                )
        instrument.write("*CLS")
        for channel in (1, 2):
            instrument.write(f":CHANnel{channel}:DISPlay ON")
            instrument.write(f":CHANnel{channel}:COUPling DC")
            instrument.write(
                f":CHANnel{channel}:SCALe {args.amplitude_vpp / 4.0:.12g}"
            )
            instrument.write(f":CHANnel{channel}:OFFSet 0")
        instrument.write(":TRIGger:EDGE:SOURce CHANnel2")
        instrument.write(":TRIGger:EDGE:LEVel 0")
        instrument.write(":TRIGger:SWEep AUTO")

        sweep_points: list[tuple[int, float]] = []
        for sweep_pass in range(1, args.passes + 1):
            ordered = frequencies if sweep_pass % 2 else list(reversed(frequencies))
            sweep_points.extend((sweep_pass, frequency) for frequency in ordered)

        for point, (sweep_pass, requested_frequency) in enumerate(
            sweep_points, start=1
        ):
            afg1 = instrument.configure_sweep_point(
                channel=REFERENCE_AFG,
                frequency=requested_frequency,
                amplitude=args.amplitude_vpp,
                capture_cycles=args.capture_cycles,
                is_current=lambda: True,
                phase_deg=args.phase_deg,
                synchronize_phase=False,
            )
            if afg1 is None:
                raise RuntimeError("AFG1 频点配置被意外取消。")
            afg2_frequency, afg2_amplitude, _afg2_phase = configure_afg2(
                instrument,
                requested_frequency,
                args.amplitude_vpp,
                args.phase_deg,
            )
            if not math.isclose(
                afg1.actual_frequency_hz,
                afg2_frequency,
                rel_tol=1e-10,
                abs_tol=1e-12,
            ):
                raise RuntimeError(
                    "双路 AFG 回读频率不一致："
                    f"AFG1={afg1.actual_frequency_hz:.12g} Hz，"
                    f"AFG2={afg2_frequency:.12g} Hz。"
                )
            frequency = afg1.actual_frequency_hz
            print(
                f"[{point}/{len(sweep_points)}] 第 {sweep_pass}/{args.passes} 轮，"
                f"{requested_frequency:.9g} Hz，"
                f"回读 {frequency:.9g} Hz，"
                f"时基 {afg1.timebase_scale_s:.9g} s/div"
            )

            for repeat in range(1, args.repeats + 1):
                afg1_phase, afg2_phase = align_both_afgs(
                    instrument, args.phase_deg
                )
                instrument.write(":RUN")
                time.sleep(
                    sweep_point_dwell_duration(
                        frequency, args.capture_cycles + args.settle_cycles
                    )
                )
                result = acquire_complete_cycles(
                    instrument=instrument,
                    channels=(RESPONSE_CHANNEL, REFERENCE_CHANNEL),
                    frequency=frequency,
                    capture_cycles=args.capture_cycles,
                    on_retry=lambda attempt, error: print(
                        f"  波形未完整，第 {attempt} 次等待重试：{error}"
                    ),
                )
                if result is None:
                    raise RuntimeError("双路相位采集被意外取消。")
                (
                    capture,
                    values,
                    point_times,
                    _instrument_times,
                    sample_interval,
                    retry_count,
                ) = result
                ch1_amplitude, ch1_phase, ch1_r2 = fit_sine_at_frequency(
                    point_times, values[1], frequency
                )
                ch2_amplitude, ch2_phase, ch2_r2 = fit_sine_at_frequency(
                    point_times, values[2], frequency
                )
                if ch2_amplitude <= 1e-15:
                    raise RuntimeError("CH2 参考拟合幅度接近零，请检查 AFG1→CH2。")
                relative_phase = wrap_phase_degrees(ch1_phase - ch2_phase)
                waveform_span = (
                    (len(capture.frames[1].values) - 1)
                    * capture.frames[1].x_increment
                )
                rows.append(
                    {
                        "capture_timestamp_utc": capture.captured_at_utc.isoformat(
                            timespec="milliseconds"
                        ),
                        "point": point,
                        "sweep_pass": sweep_pass,
                        "repeat": repeat,
                        "requested_frequency_hz": requested_frequency,
                        "frequency_hz": frequency,
                        "afg2_frequency_hz": afg2_frequency,
                        "requested_amplitude_vpp": args.amplitude_vpp,
                        "afg1_amplitude_vpp": afg1.actual_amplitude_vpp,
                        "afg2_amplitude_vpp": afg2_amplitude,
                        "requested_phase_deg": args.phase_deg,
                        "afg1_phase_deg": afg1_phase,
                        "afg2_phase_deg": afg2_phase,
                        "ch1_fit_amplitude_v": ch1_amplitude,
                        "ch2_fit_amplitude_v": ch2_amplitude,
                        "amplitude_ratio_ch1_over_ch2": ch1_amplitude / ch2_amplitude,
                        "ch1_fit_phase_deg": ch1_phase,
                        "ch2_fit_phase_deg": ch2_phase,
                        "phase_ch1_minus_ch2_deg": relative_phase,
                        "apparent_delay_ch1_minus_ch2_s": (
                            -relative_phase / (360.0 * frequency)
                        ),
                        "ch1_fit_r2": ch1_r2,
                        "ch2_fit_r2": ch2_r2,
                        "timebase_scale_s": afg1.timebase_scale_s,
                        "waveform_span_s": waveform_span,
                        "sample_interval_s": sample_interval,
                        "sample_count": len(point_times),
                        "incomplete_frame_retries": retry_count,
                    }
                )
                print(
                    f"  重复 {repeat}/{args.repeats}: CH1-CH2="
                    f"{relative_phase:+.6f}°，幅值比="
                    f"{ch1_amplitude / ch2_amplitude:.6f}，"
                    f"R2={min(ch1_r2, ch2_r2):.9f}"
                )
    finally:
        if instrument.connected:
            try:
                instrument.write(":SOURce2:OUTPut:STATe OFF")
                if instrument.sweep_original_timebase_scale is not None:
                    instrument.finish_sweep(1)
            except Exception as exc:
                restore_errors.append("扫频清理失败：" + str(exc))
            if snapshot is not None:
                try:
                    restore_snapshot(instrument, snapshot)
                except Exception as exc:
                    restore_errors.append("仪器设置恢复失败：" + str(exc))
            try:
                errors = instrument._read_scpi_errors_unlocked()
                restore_errors.extend("仪器错误队列：" + error for error in errors)
            except Exception as exc:
                restore_errors.append("错误队列读取失败：" + str(exc))
            instrument.close()

    if not rows:
        raise RuntimeError("没有完成双路相位测量。" + "；".join(restore_errors))
    repeatability = phase_repeatability(rows)
    calibration_rows = [
        {
            "channel": RESPONSE_CHANNEL,
            "reference_channel": REFERENCE_CHANNEL,
            "frequency_hz": row["frequency_hz"],
            "phase_relative_to_reference_deg": row["phase_ch1_minus_ch2_deg"],
            "amplitude_ratio": row["amplitude_ratio_ch1_over_ch2"],
            "reference_fit_r2": row["ch2_fit_r2"],
            "channel_fit_r2": row["ch1_fit_r2"],
            "repeat": row["repeat"],
        }
        for row in rows
    ]
    delay_summary = summarize_measurements(
        calibration_rows, channels=(RESPONSE_CHANNEL,), reference_channel=REFERENCE_CHANNEL
    )[0]
    maximum_repeatability = max(
        row["phase_repeatability_peak_to_peak_deg"] for row in repeatability
    )
    minimum_r2 = min(
        min(float(row["ch1_fit_r2"]), float(row["ch2_fit_r2"])) for row in rows
    )
    overall = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "instrument_id": instrument_id,
        "wiring": "AFG1->CH2(reference), AFG2->CH1(response)",
        "phase_definition": "CH1 phase minus CH2 phase",
        "phase_alignment_command": ":SOURce1:PHASe:SYNChronize",
        "combined_delay_ch1_minus_ch2_s": delay_summary[
            "delay_relative_to_reference_s"
        ],
        "combined_phase_intercept_deg": delay_summary["phase_intercept_deg"],
        "phase_line_fit_rms_deg": delay_summary["phase_fit_rms_deg"],
        "maximum_repeatability_peak_to_peak_deg": maximum_repeatability,
        "minimum_sine_fit_r2": minimum_r2,
        "note": (
            "Delay includes AFG channel skew, both BNC cables, and scope CH1/CH2 skew; "
            "it is not cable-only unless the other terms are independently calibrated."
        ),
    }
    raw_path, summary_path, json_path = write_outputs(
        args.output_dir, rows, repeatability, overall
    )
    print("\n结果：")
    print(
        "  CH1 相对 CH2 综合延迟："
        f"{float(overall['combined_delay_ch1_minus_ch2_s']) * 1e9:+.6f} ns"
    )
    print(
        "  相位-频率直线拟合 RMS："
        f"{float(overall['phase_line_fit_rms_deg']):.6f}°"
    )
    print(f"  最差重复峰峰值：{maximum_repeatability:.6f}°")
    print(f"  最低正弦拟合 R2：{minimum_r2:.9f}")
    print("  原始数据：" + str(raw_path.resolve()))
    print("  逐频点汇总：" + str(summary_path.resolve()))
    print("  综合结果：" + str(json_path.resolve()))
    if restore_errors:
        print("恢复/错误队列警告：")
        for error in restore_errors:
            print("  " + error)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
