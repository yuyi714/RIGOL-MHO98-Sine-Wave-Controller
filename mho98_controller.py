"""MHO98 AFG desktop controller (USB-VISA or LAN/SCPI)."""
from __future__ import annotations

import csv
import math
import socket
import sys
import threading
import time
import tkinter as tk
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from windows_usbtmc import WindowsUsbTmc, list_usbtmc_devices


_FIGURE_DEPS = Path(__file__).with_name(".figure_deps")
if _FIGURE_DEPS.is_dir() and str(_FIGURE_DEPS) not in sys.path:
    sys.path.insert(0, str(_FIGURE_DEPS))

try:
    import numpy as np
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
except Exception as exc:  # UI still starts and reports the missing plot dependency.
    np = None
    FigureCanvasTkAgg = None
    Figure = None
    _PLOT_IMPORT_ERROR = str(exc)
else:
    _PLOT_IMPORT_ERROR = ""


@dataclass
class BodePoint:
    frequency_hz: float
    reference_amplitude_v: float
    response_amplitude_v: float
    gain_ratio: float
    gain_db: float
    phase_deg: float
    phase_wrapped_deg: float
    reference_r2: float
    response_r2: float
    sample_count: int


@dataclass(frozen=True)
class WaveformFrame:
    channel: int
    values: list[float]
    x_increment: float
    x_origin: float
    x_reference: float


@dataclass(frozen=True)
class SweepPointConfiguration:
    requested_frequency_hz: float
    actual_frequency_hz: float
    requested_amplitude_vpp: float
    actual_amplitude_vpp: float
    timebase_scale_s: float
    requested_offset_v: float = 0.0
    actual_offset_v: float = 0.0
    requested_phase_deg: float = 0.0
    actual_phase_deg: float = 0.0
    phase_synchronized: bool = True


@dataclass(frozen=True)
class SweepPointRequest:
    frequency_hz: float
    amplitude_vpp: float
    offset_v: float = 0.0

    @property
    def amplitude_peak_v(self) -> float:
        return self.amplitude_vpp / 2.0


@dataclass(frozen=True)
class CaptureResult:
    frames: dict[int, WaveformFrame]
    captured_at_utc: datetime
    captured_monotonic_s: float
    scope_was_running: bool


class WaveformNotReadyError(RuntimeError):
    """The scope has not completed the requested screen waveform yet."""


class IncompleteWaveformError(ValueError):
    """The downloaded frame does not yet cover the requested cycle window."""


HORIZONTAL_TIME_DIVISIONS = 10.0
MINIMUM_TIMEBASE_HEADROOM = 1.02
MINIMUM_POINT_DWELL_S = 0.25


def maximum_afg_amplitude_vpp(frequency: float) -> float:
    if not math.isfinite(frequency) or not 0.002 <= frequency <= 100_000_000:
        raise ValueError("频率必须在 0.002–100000000 Hz。")
    return 10.0 if frequency > 50_000_000 else 20.0


def validate_afg_sine_parameters(
    frequency: float, amplitude_vpp: float, offset_v: float
) -> None:
    """Validate the High-Z sine envelope B ± A_vpp/2.

    The instrument performs the final validation because its limit also
    depends on the configured output impedance.  This check enforces the
    documented High-Z envelope without issuing a potentially disruptive load
    query during a sweep.
    """
    maximum_amplitude = maximum_afg_amplitude_vpp(frequency)
    if not math.isfinite(amplitude_vpp) or not 0.002 <= amplitude_vpp <= maximum_amplitude:
        raise ValueError(
            f"{frequency:.12g} Hz 下幅度必须在 0.002–{maximum_amplitude:g} Vpp。"
        )
    if not math.isfinite(offset_v):
        raise ValueError("偏置 B 必须是有限数值。")
    maximum_offset = (maximum_amplitude - amplitude_vpp) / 2.0
    if abs(offset_v) > maximum_offset + 1e-12:
        raise ValueError(
            f"{frequency:.12g} Hz、{amplitude_vpp:.12g} Vpp 下，"
            f"偏置 B 必须在 ±{maximum_offset:.12g} V；"
            "需满足 |B| + Vpp/2 不超过输出范围。"
        )


def fit_sine_at_frequency(times, values, frequency: float):
    if np is None:
        raise RuntimeError("缺少 NumPy/Matplotlib：" + _PLOT_IMPORT_ERROR)
    t = np.asarray(times, dtype=float)
    y = np.asarray(values, dtype=float)
    valid = np.isfinite(t) & np.isfinite(y)
    t, y = t[valid], y[valid]
    if len(y) < 8:
        raise ValueError("有效采样点少于 8 个。")
    omega_t = 2.0 * math.pi * frequency * t
    design = np.column_stack((np.sin(omega_t), np.cos(omega_t), np.ones(len(t))))
    coefficients, *_ = np.linalg.lstsq(design, y, rcond=None)
    sine_coefficient, cosine_coefficient, _offset = coefficients
    amplitude = float(math.hypot(sine_coefficient, cosine_coefficient))
    phase_deg = math.degrees(math.atan2(cosine_coefficient, sine_coefficient))
    fitted = design @ coefficients
    residual = float(np.sum((y - fitted) ** 2))
    total = float(np.sum((y - np.mean(y)) ** 2))
    r_squared = 1.0 if total <= 1e-30 else 1.0 - residual / total
    return amplitude, phase_deg, r_squared


def calculate_bode(groups: dict[float, tuple[list[float], list[float], list[float]]]):
    """Calculate response/reference magnitude and phase at each excitation frequency."""
    if np is None:
        raise RuntimeError("缺少 NumPy/Matplotlib：" + _PLOT_IMPORT_ERROR)
    points: list[BodePoint] = []
    warnings: list[str] = []
    for frequency in sorted(groups):
        times, reference, response = groups[frequency]
        count = min(len(times), len(reference), len(response))
        if count < 8:
            warnings.append(f"{frequency:.12g} Hz：有效点数不足，已跳过")
            continue
        try:
            reference_amplitude, reference_phase, reference_r2 = fit_sine_at_frequency(
                times[:count], reference[:count], frequency
            )
            response_amplitude, response_phase, response_r2 = fit_sine_at_frequency(
                times[:count], response[:count], frequency
            )
        except Exception as exc:
            warnings.append(f"{frequency:.12g} Hz：{exc}")
            continue
        if reference_amplitude <= 1e-15:
            warnings.append(f"{frequency:.12g} Hz：参考通道幅值接近零，已跳过")
            continue
        ratio = response_amplitude / reference_amplitude
        wrapped_phase = (response_phase - reference_phase + 180.0) % 360.0 - 180.0
        points.append(
            BodePoint(
                frequency_hz=frequency,
                reference_amplitude_v=reference_amplitude,
                response_amplitude_v=response_amplitude,
                gain_ratio=ratio,
                gain_db=20.0 * math.log10(max(ratio, 1e-300)),
                phase_deg=wrapped_phase,
                phase_wrapped_deg=wrapped_phase,
                reference_r2=reference_r2,
                response_r2=response_r2,
                sample_count=count,
            )
        )
    if not points:
        raise ValueError("没有能够计算伯德图的有效频率数据。" + ("；".join(warnings) if warnings else ""))
    unwrapped = np.rad2deg(
        np.unwrap(np.deg2rad([point.phase_wrapped_deg for point in points]))
    )
    for point, phase in zip(points, unwrapped):
        point.phase_deg = float(phase)
    return points, warnings


def _normalized_column_name(name: str) -> str:
    return "".join(character.lower() for character in name if character.isalnum())


def load_waveform_groups(
    path: str, reference_channel: int = 1, response_channel: int = 2
):
    """Load two selected channels from current-app and common waveform CSV files."""
    if reference_channel not in range(1, 5) or response_channel not in range(1, 5):
        raise ValueError("伯德图通道必须是 CH1–CH4。")
    if reference_channel == response_channel:
        raise ValueError("伯德图参考通道和响应通道不能相同。")

    def channel_aliases(channel: int) -> tuple[str, ...]:
        return (
            f"ch{channel}voltagev",
            f"ch{channel}voltage",
            f"ch{channel}v",
            f"ch{channel}",
            f"通道{channel}电压",
            f"通道{channel}",
        )

    aliases = {
        "frequency": (
            "actualfrequencyhz", "frequencyhz", "frequency", "freqhz", "freq",
            "频率hz", "频率",
        ),
        "reference": channel_aliases(reference_channel),
        "response": channel_aliases(response_channel),
        "time": (
            "pointtimes", "instrumenttimes", "times", "time", "storedtimes",
            "sweepelapseds", "时间s", "时间",
        ),
        "interval": ("sampleintervals", "xincrement", "dt", "采样间隔s", "时间间隔s"),
    }
    with open(path, newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError("CSV 没有表头。")
        normalized = {
            _normalized_column_name(field): field for field in reader.fieldnames
        }

        def locate(kind: str, required: bool = True):
            for alias in aliases[kind]:
                if alias in normalized:
                    return normalized[alias]
            if required:
                raise ValueError(
                    f"CSV 缺少 {kind} 列。现有列：" + ", ".join(reader.fieldnames or [])
                )
            return None

        frequency_column = locate("frequency")
        reference_column = locate("reference")
        response_column = locate("response")
        time_column = locate("time", False)
        interval_column = locate("interval", False)
        raw_groups: dict[float, list[tuple[float | None, float | None, float, float]]] = {}
        skipped = 0
        for row in reader:
            try:
                frequency = float(row[frequency_column])
                reference_value = float(row[reference_column])
                response_value = float(row[response_column])
                time_value = float(row[time_column]) if time_column and row.get(time_column) else None
                interval = (
                    float(row[interval_column])
                    if interval_column and row.get(interval_column)
                    else None
                )
                if frequency <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                skipped += 1
                continue
            raw_groups.setdefault(frequency, []).append(
                (time_value, interval, reference_value, response_value)
            )

    groups: dict[float, tuple[list[float], list[float], list[float]]] = {}
    assumptions: list[str] = []
    for frequency, rows in raw_groups.items():
        if all(row[0] is not None for row in rows):
            times = [float(row[0]) for row in rows]
        elif all(row[1] is not None and row[1] > 0 for row in rows):
            interval = float(rows[0][1])
            times = [index * interval for index in range(len(rows))]
        else:
            # Compatibility path for simple files containing only f and two channels.
            times = [index * (3.0 / frequency) / len(rows) for index in range(len(rows))]
            assumptions.append(
                f"{frequency:.12g} Hz 无时间列，按该组数据均匀覆盖 3 个周期处理"
            )
        groups[frequency] = (
            times,
            [row[2] for row in rows],
            [row[3] for row in rows],
        )
    if not groups:
        raise ValueError(
            f"CSV 中没有有效的频率、CH{reference_channel}、"
            f"CH{response_channel} 数值行。"
        )
    if skipped:
        assumptions.append(f"已跳过 {skipped} 行无效数据")
    return groups, assumptions


def load_sweep_ab_profile(path: str | Path) -> list[SweepPointRequest]:
    """Load explicit frequency-dependent sine amplitude A and offset B.

    Preferred mathematical columns are ``frequency_hz,a_peak_v,b_offset_v``
    for ``B + A*sin(2*pi*f*t)``.  ``amplitude_vpp,offset_v`` is also accepted
    for users who prefer the instrument's native peak-to-peak convention.
    Row order is preserved as the sweep order.
    """
    frequency_aliases = {
        "frequencyhz", "frequency", "freqhz", "freq", "f", "频率hz", "频率"
    }
    peak_aliases = {
        "apeakv", "amplitudepeakv", "peakamplitudev", "a", "峰值v", "幅值峰值v"
    }
    vpp_aliases = {
        "amplitudevpp", "avpp", "vpp", "幅度vpp", "峰峰值vpp"
    }
    offset_aliases = {
        "boffsetv", "offsetv", "biasv", "bv", "b", "偏置v", "直流偏置v"
    }
    with open(path, newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError("A/B 曲线 CSV 没有表头。")
        normalized = {
            _normalized_column_name(field): field for field in reader.fieldnames
        }

        def locate(aliases: set[str]) -> str | None:
            return next(
                (normalized[alias] for alias in aliases if alias in normalized),
                None,
            )

        frequency_column = locate(frequency_aliases)
        peak_column = locate(peak_aliases)
        vpp_column = locate(vpp_aliases)
        offset_column = locate(offset_aliases)
        if frequency_column is None:
            raise ValueError("A/B 曲线 CSV 缺少 frequency_hz 列。")
        if (peak_column is None) == (vpp_column is None):
            raise ValueError(
                "A/B 曲线 CSV 必须且只能提供 a_peak_v 或 amplitude_vpp 其中一列。"
            )

        points: list[SweepPointRequest] = []
        seen_frequencies: set[float] = set()
        for line_number, row in enumerate(reader, start=2):
            try:
                frequency = float(row[frequency_column])
                if peak_column is not None:
                    amplitude_peak = float(row[peak_column])
                    amplitude_vpp = amplitude_peak * 2.0
                else:
                    amplitude_vpp = float(row[vpp_column])
                offset_text = row.get(offset_column, "") if offset_column else ""
                offset = float(offset_text) if str(offset_text).strip() else 0.0
                validate_afg_sine_parameters(frequency, amplitude_vpp, offset)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"A/B 曲线第 {line_number} 行无效：{exc}") from exc
            if frequency in seen_frequencies:
                raise ValueError(
                    f"A/B 曲线第 {line_number} 行频率 {frequency:.12g} Hz 重复。"
                )
            seen_frequencies.add(frequency)
            points.append(
                SweepPointRequest(
                    frequency_hz=frequency,
                    amplitude_vpp=amplitude_vpp,
                    offset_v=offset,
                )
            )
    if not points:
        raise ValueError("A/B 曲线 CSV 没有数据行。")
    return points


def make_sweep_frequencies(
    start_hz: float = 0.1,
    stop_hz: float = 500.0,
    spacing: str = "linear",
    linear_points: int = 300,
    points_per_decade: int = 30,
) -> list[float]:
    """Generate an inclusive arbitrary linear or logarithmic frequency sweep."""
    if not 0.002 <= start_hz <= 100_000_000:
        raise ValueError("起始频率必须在 0.002–100000000 Hz。")
    if not 0.002 <= stop_hz <= 100_000_000:
        raise ValueError("终止频率必须在 0.002–100000000 Hz。")
    if stop_hz < start_hz:
        raise ValueError("终止频率不能小于起始频率。")
    if stop_hz == start_hz:
        return [start_hz]

    normalized_spacing = spacing.strip().lower()
    if normalized_spacing in {"linear", "线性"}:
        if not 2 <= linear_points <= 100_000:
            raise ValueError("线性扫频总点数必须在 2–100000。")
        step = (stop_hz - start_hz) / (linear_points - 1)
        frequencies = [start_hz + index * step for index in range(linear_points)]
    elif normalized_spacing in {"log", "logarithmic", "对数"}:
        if not 1 <= points_per_decade <= 10_000:
            raise ValueError("对数扫频每十倍频点数必须在 1–10000。")
        decades = math.log10(stop_hz / start_hz)
        interval_count = max(1, math.ceil(decades * points_per_decade))
        ratio = (stop_hz / start_hz) ** (1.0 / interval_count)
        frequencies = [start_hz * ratio**index for index in range(interval_count + 1)]
    else:
        raise ValueError("扫频方式必须是 linear 或 log。")

    frequencies[0] = start_hz
    frequencies[-1] = stop_hz
    return frequencies


def cycle_duration(frequency: float, cycles: int | float) -> float:
    if frequency <= 0:
        raise ValueError("频率必须大于 0。")
    if cycles <= 0:
        raise ValueError("周期数必须大于 0。")
    return float(cycles) / frequency


def sweep_point_dwell_duration(frequency: float, cycles: int | float) -> float:
    """Wait for requested cycles and a minimum hardware settling interval."""
    return max(cycle_duration(frequency, cycles), MINIMUM_POINT_DWELL_S)


def five_cycle_duration(frequency: float) -> float:
    """Backward-compatible helper retained for external callers."""
    return cycle_duration(frequency, 5)


def recommended_timebase_scale(
    frequency: float, capture_cycles: int, headroom: float = 1.25
) -> float:
    """Choose s/div so the screen contains the requested cycles plus margin."""
    if headroom < 1.0:
        raise ValueError("时基余量不能小于 1。")
    return (
        cycle_duration(frequency, capture_cycles)
        * headroom
        / HORIZONTAL_TIME_DIVISIONS
    )


def extract_recent_cycles(
    frames: dict[int, WaveformFrame],
    frequency: float,
    capture_cycles: int,
) -> tuple[dict[int, list[float]], list[float], list[float], float]:
    """Extract the newest requested cycles from aligned channels of one frozen frame."""
    if not frames:
        raise ValueError("没有可提取的通道波形。")
    if frequency <= 0 or capture_cycles < 1:
        raise ValueError("波形频率或采集周期数无效。")

    ordered = [frames[channel] for channel in sorted(frames)]
    reference = ordered[0]
    if len(reference.values) < 2 or reference.x_increment <= 0:
        raise ValueError(f"CH{reference.channel} 返回的波形点数或时间间隔无效。")
    for frame in ordered[1:]:
        if len(frame.values) != len(reference.values):
            raise ValueError(
                f"CH{reference.channel}/CH{frame.channel} 波形点数不一致，"
                "无法按同一采集索引对齐。"
            )
        relative_increment_error = abs(frame.x_increment - reference.x_increment) / max(
            frame.x_increment, reference.x_increment
        )
        if relative_increment_error > 0.001:
            raise ValueError(
                f"CH{reference.channel}/CH{frame.channel} 的 XINCrement 不一致，"
                "无法在不插值的情况下对齐。"
            )
        if abs(frame.x_origin - reference.x_origin) > reference.x_increment:
            raise ValueError(
                f"CH{reference.channel}/CH{frame.channel} 的 XORigin 不一致，"
                "不是同一时间轴。"
            )

    increment = reference.x_increment
    source_duration = (len(reference.values) - 1) * increment
    requested_duration = cycle_duration(frequency, capture_cycles)
    if source_duration + increment < requested_duration:
        raise IncompleteWaveformError(
            f"冻结波形仅覆盖 {source_duration:.6g} s，"
            f"保存 {capture_cycles} 周期至少需要 {requested_duration:.6g} s。"
        )

    window_start = source_duration - requested_duration
    start_index = max(0, math.ceil((window_start - increment * 1e-9) / increment))
    selected_values = {
        frame.channel: frame.values[start_index:] for frame in ordered
    }
    count = min(len(values) for values in selected_values.values())
    if count < 8:
        raise ValueError("请求周期内的原始波形点数少于 8 个。")
    selected_values = {
        channel: values[:count] for channel, values in selected_values.items()
    }
    point_times = [
        (start_index + index) * increment - window_start for index in range(count)
    ]
    instrument_times = [
        reference.x_origin
        + (start_index + index - reference.x_reference) * increment
        for index in range(count)
    ]
    return selected_values, point_times, instrument_times, increment


class Instrument:
    """One serialized SCPI connection.  Never call the scope concurrently."""
    def __init__(self) -> None:
        self.kind: str | None = None
        self.session = None
        self.sock: socket.socket | None = None
        self.native_usb: WindowsUsbTmc | None = None
        self.idn = ""
        self.lock = threading.Lock()
        self.sweep_original_timebase_scale: float | None = None
        self.sweep_active_timebase_scale: float | None = None
        self.sweep_original_scope_running: bool | None = None

    @property
    def connected(self) -> bool:
        return self.kind is not None

    def close(self) -> None:
        with self.lock:
            if self.session:
                self.session.close()
            if self.sock:
                self.sock.close()
            if self.native_usb:
                self.native_usb.close()
            self.kind = self.session = self.sock = self.native_usb = None
            self.idn = ""
            self.sweep_original_timebase_scale = None
            self.sweep_active_timebase_scale = None
            self.sweep_original_scope_running = None

    def connect_lan(self, host: str) -> str:
        """Connect without requiring NI-VISA; RIGOL raw-SCPI normally uses 5555."""
        self.close()
        if host in {"192.168.1.1", "0.0.0.0", "127.0.0.1"}:
            raise RuntimeError(
                "请填写示波器自身的 IP Address，不是 DNS/Gateway。"
                "192.168.1.1 通常是路由器地址。"
            )
        attempts = []
        for port in (5555, 5025):
            try:
                s = socket.create_connection((host, port), timeout=3)
                s.settimeout(3)
                self.sock, self.kind = s, f"LAN-SCPI:{port}"
                self.idn = self.query("*IDN?")
                if "RIGOL" not in self.idn.upper():
                    unexpected_idn = self.idn
                    self.close()
                    attempts.append(
                        f"{port}: 连接成功但未返回 RIGOL 标识 ({unexpected_idn!r})"
                    )
                    continue
                return self.idn
            except OSError as exc:
                self.close()
                attempts.append(f"{port}: {exc}")
        raise RuntimeError(
            "无法连接示波器 LAN SCPI。请确认填写的是示波器 IP Address，电脑和示波器在同一网段，"
            "且示波器 LAN 状态为 CONNECTED。端口诊断：" + "； ".join(attempts)
        )

    def connect_visa(self, resource: str, label: str = "USB-VISA") -> str:
        self.close()
        try:
            import pyvisa
        except ImportError as exc:
            raise RuntimeError("未安装 PyVISA。请先运行：pip install -r requirements.txt") from exc
        rm = pyvisa.ResourceManager()
        inst = rm.open_resource(resource)
        inst.timeout = 3000
        inst.write_termination = "\n"
        inst.read_termination = "\n"
        self.session, self.kind = inst, label
        self.idn = self.query("*IDN?")
        if "RIGOL" not in self.idn.upper():
            self.close()
            raise RuntimeError("该 USB 设备未返回 RIGOL 设备标识。")
        return self.idn

    def connect_native_usb(self, path: str) -> str:
        self.close()
        native = WindowsUsbTmc(path)
        self.native_usb, self.kind = native, "USB-USBTMC"
        try:
            self.idn = self.query("*IDN?")
            if "RIGOL" not in self.idn.upper():
                raise RuntimeError(f"设备未返回 RIGOL 标识：{self.idn!r}")
            return self.idn
        except Exception:
            self.close()
            raise

    def write(self, command: str) -> None:
        with self.lock:
            self._write_unlocked(command)

    def query(self, command: str) -> str:
        with self.lock:
            return self._query_unlocked(command)

    def _write_unlocked(self, command: str) -> None:
        if self.session:
            self.session.write(command)
        elif self.sock:
            self.sock.sendall((command + "\n").encode("ascii"))
        elif self.native_usb:
            self.native_usb.write(command)
        else:
            raise RuntimeError("仪器未连接")

    def _query_unlocked(self, command: str) -> str:
        if self.session:
            return str(self.session.query(command)).strip()
        if self.sock:
            self.sock.sendall((command + "\n").encode("ascii"))
            chunks = []
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if chunk.endswith(b"\n"):
                    break
            return b"".join(chunks).decode("ascii", "replace").strip()
        if self.native_usb:
            return self.native_usb.query(command)
        raise RuntimeError("仪器未连接")

    def _read_scpi_errors_unlocked(self, limit: int = 16) -> list[str]:
        errors: list[str] = []
        for _ in range(limit):
            response = self._query_unlocked(":SYSTem:ERRor?").strip()
            if response.startswith("0"):
                return errors
            errors.append(response)
        errors.append(f"错误队列超过 {limit} 条，已停止继续读取")
        return errors

    def configure_sweep_point(
        self,
        channel: int,
        frequency: float,
        amplitude: float,
        capture_cycles: int,
        is_current,
        offset_v: float = 0.0,
        phase_deg: float = 0.0,
        synchronize_phase: bool = True,
    ) -> SweepPointConfiguration | None:
        """Configure one AFG point, align phase, and size the screen.

        The phase is written and read back at every frequency. By default the
        MHO98 hardware phase-alignment operation is then executed, so changing
        frequency does not leave the AFG at an arbitrary phase relationship.
        """
        if channel not in (1, 2):
            raise ValueError("AFG 通道只能是 1 或 2。")
        validate_afg_sine_parameters(frequency, amplitude, offset_v)
        if not 0.0 <= phase_deg <= 360.0:
            raise ValueError("AFG 相位必须在 0–360°。")
        with self.lock:
            if not is_current():
                return None
            if self.sweep_original_timebase_scale is None:
                self.sweep_original_timebase_scale = float(
                    self._query_unlocked(":TIMebase:MAIN:SCALe?")
                )
                trigger_status = self._query_unlocked(":TRIGger:STATus?").strip().upper()
                self.sweep_original_scope_running = trigger_status != "STOP"
            self._write_unlocked(f":SOURce{channel}:FUNCtion SINusoid")
            self._write_unlocked(f":SOURce{channel}:FREQuency {frequency:.12g}")
            # Offset and amplitude share one output-envelope limit. Clear the
            # previous point's offset before changing amplitude, then apply B.
            self._write_unlocked(f":SOURce{channel}:VOLTage:OFFSet 0")
            self._write_unlocked(f":SOURce{channel}:VOLTage:AMPLitude {amplitude:.12g}")
            self._write_unlocked(f":SOURce{channel}:VOLTage:OFFSet {offset_v:.12g}")
            self._write_unlocked(f":SOURce{channel}:PHASe {phase_deg:.12g}")
            actual_frequency = float(
                self._query_unlocked(f":SOURce{channel}:FREQuency?")
            )
            actual_amplitude = float(
                self._query_unlocked(f":SOURce{channel}:VOLTage:AMPLitude?")
            )
            actual_offset = float(
                self._query_unlocked(f":SOURce{channel}:VOLTage:OFFSet?")
            )
            actual_phase = float(
                self._query_unlocked(f":SOURce{channel}:PHASe?")
            )
            requested_timebase = recommended_timebase_scale(
                actual_frequency, capture_cycles
            )
            minimum_timebase = (
                cycle_duration(actual_frequency, capture_cycles)
                * MINIMUM_TIMEBASE_HEADROOM
                / HORIZONTAL_TIME_DIVISIONS
            )
            for _attempt in range(4):
                self._write_unlocked(
                    f":TIMebase:MAIN:SCALe {requested_timebase:.12g}"
                )
                actual_timebase = float(
                    self._query_unlocked(":TIMebase:MAIN:SCALe?")
                )
                if actual_timebase >= minimum_timebase:
                    break
                # Some firmware quantizes s/div downward. Ask for a clearly
                # larger scale and verify the value accepted by the instrument.
                requested_timebase = max(
                    requested_timebase * 2.0,
                    minimum_timebase * 1.25,
                )
            else:
                raise RuntimeError(
                    "仪器接受的时基不足以覆盖所要求的完整采集周期："
                    f"需要至少 {minimum_timebase:.12g} s/div，"
                    f"实际为 {actual_timebase:.12g} s/div。"
                )
            self.sweep_active_timebase_scale = actual_timebase
            self._write_unlocked(f":SOURce{channel}:OUTPut:STATe ON")
            if synchronize_phase:
                self._write_unlocked(f":SOURce{channel}:PHASe:SYNChronize")
                self._write_unlocked("*WAI")
            # A sweep must acquire a fresh frame even if the scope was STOP
            # before the sweep. finish_sweep restores that original state.
            self._write_unlocked(":RUN")
            errors = self._read_scpi_errors_unlocked()
            if errors:
                raise RuntimeError("仪器错误队列：" + "；".join(errors))
            return SweepPointConfiguration(
                requested_frequency_hz=frequency,
                actual_frequency_hz=actual_frequency,
                requested_amplitude_vpp=amplitude,
                actual_amplitude_vpp=actual_amplitude,
                timebase_scale_s=actual_timebase,
                requested_offset_v=offset_v,
                actual_offset_v=actual_offset,
                requested_phase_deg=phase_deg,
                actual_phase_deg=actual_phase,
                phase_synchronized=synchronize_phase,
            )

    def finish_sweep(self, channel: int) -> float | None:
        """Disable AFG output and restore the pre-sweep timebase/run state."""
        with self.lock:
            original = self.sweep_original_timebase_scale
            original_running = self.sweep_original_scope_running
            restored = None
            try:
                self._write_unlocked(f":SOURce{channel}:OUTPut:STATe OFF")
                if original_running is False:
                    self._write_unlocked(":STOP")
                elif original_running is True:
                    self._write_unlocked(":RUN")
                if original is not None:
                    for _attempt in range(2):
                        self._write_unlocked(
                            f":TIMebase:MAIN:SCALe {original:.12g}"
                        )
                        restored = float(
                            self._query_unlocked(":TIMebase:MAIN:SCALe?")
                        )
                        if math.isclose(restored, original, rel_tol=1e-6, abs_tol=1e-15):
                            break
                    else:
                        raise RuntimeError(
                            "原时基恢复后回读不一致："
                            f"目标 {original:.12g} s/div，"
                            f"实际 {restored:.12g} s/div。"
                        )
                return restored
            finally:
                self.sweep_original_timebase_scale = None
                self.sweep_active_timebase_scale = None
                self.sweep_original_scope_running = None

    def acquire_channels(self, channels: tuple[int, ...]) -> CaptureResult:
        """Freeze one acquisition and download selected CH1–CH4 from that frame."""
        channels = tuple(sorted(set(channels)))
        if not channels or any(channel not in range(1, 5) for channel in channels):
            raise ValueError("采集通道必须是 CH1–CH4 中的至少一个通道。")
        with self.lock:
            trigger_status = self._query_unlocked(":TRIGger:STATus?").strip().upper()
            scope_was_running = trigger_status != "STOP"
            self._write_unlocked(":STOP")
            try:
                for _ in range(20):
                    if self._query_unlocked(":TRIGger:STATus?").strip().upper() == "STOP":
                        break
                    time.sleep(0.01)
                else:
                    raise RuntimeError("示波器未在规定时间内进入 STOP 状态。")
            except Exception:
                if scope_was_running:
                    self._write_unlocked(":RUN")
                raise
            captured_at = datetime.now(timezone.utc)
            captured_monotonic = time.monotonic()
            try:
                self._write_unlocked(":WAVeform:MODE NORMal")
                self._write_unlocked(":WAVeform:FORMat ASCii")
                self._write_unlocked(":WAVeform:STARt 1")
                self._write_unlocked(":WAVeform:STOP 1000")
                self._write_unlocked(":WAVeform:POINts 1000")
                frames: dict[int, WaveformFrame] = {}
                for channel in channels:
                    display_state = self._query_unlocked(
                        f":CHANnel{channel}:DISPlay?"
                    ).strip().upper()
                    if display_state not in {"1", "ON"}:
                        raise RuntimeError(
                            f"CH{channel} 当前未开启。程序不会自动开关采集通道；"
                            f"请先在示波器上开启 CH{channel}。"
                        )
                    self._write_unlocked(f":WAVeform:SOURce CHANnel{channel}")
                    selected_source = self._query_unlocked(
                        ":WAVeform:SOURce?"
                    ).strip().upper()
                    if selected_source != f"CHAN{channel}":
                        raise RuntimeError(
                            f"波形源切换失败：请求 CH{channel}，"
                            f"仪器返回 {selected_source!r}。"
                        )
                    preamble_text = self._query_unlocked(":WAVeform:PREamble?")
                    preamble = [item.strip() for item in preamble_text.split(",")]
                    if len(preamble) != 10:
                        raise RuntimeError(
                            f"CH{channel} 波形前导参数不完整：{preamble_text!r}"
                        )
                    waveform_format = int(float(preamble[0]))
                    waveform_mode = int(float(preamble[1]))
                    expected_points = int(float(preamble[2]))
                    if waveform_format != 2 or waveform_mode != 0:
                        raise RuntimeError(
                            f"CH{channel} 波形格式异常：format={waveform_format}, "
                            f"mode={waveform_mode}；期望 ASCii/NORMal。"
                        )
                    text = self._query_unlocked(":WAVeform:DATA?")
                    values = [
                        float(item) for item in text.strip().split(",") if item.strip()
                    ]
                    if expected_points != 1000 or len(values) != 1000:
                        raise WaveformNotReadyError(
                            f"CH{channel} 波形点数不完整：前导参数为 "
                            f"{expected_points} 点，实际收到 {len(values)} 点；"
                            "已请求 1000 点。"
                        )
                    frames[channel] = WaveformFrame(
                        channel=channel,
                        values=values,
                        x_increment=float(preamble[4]),
                        x_origin=float(preamble[5]),
                        x_reference=float(preamble[6]),
                    )
                return CaptureResult(
                    frames=frames,
                    captured_at_utc=captured_at,
                    captured_monotonic_s=captured_monotonic,
                    scope_was_running=scope_was_running,
                )
            finally:
                if scope_was_running:
                    self._write_unlocked(":RUN")


def acquire_complete_cycles(
    instrument: Instrument,
    channels: tuple[int, ...],
    frequency: float,
    capture_cycles: int,
    is_current=lambda: True,
    on_retry=None,
):
    """Wait/retry until the scope exposes a complete 1000-point cycle window."""
    retry_timeout_s = max(
        10.0,
        min(60.0, cycle_duration(frequency, capture_cycles)),
    )
    # A STOP/RUN pair can restart the slow screen-record formation. Give the
    # scope one uninterrupted acquisition interval instead of polling rapidly.
    retry_delay_s = max(2.0, min(10.0, cycle_duration(frequency, 20)))
    deadline = time.monotonic() + retry_timeout_s
    retry_count = 0
    last_error: Exception | None = None
    while is_current():
        try:
            capture = instrument.acquire_channels(channels)
            values, point_times, instrument_times, sample_interval = (
                extract_recent_cycles(
                    capture.frames,
                    frequency,
                    capture_cycles,
                )
            )
            return (
                capture,
                values,
                point_times,
                instrument_times,
                sample_interval,
                retry_count,
            )
        except (WaveformNotReadyError, IncompleteWaveformError) as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                break
            retry_count += 1
            if on_retry is not None:
                on_retry(retry_count, exc)
            time.sleep(min(retry_delay_s, max(0.0, deadline - time.monotonic())))
    if not is_current():
        return None
    raise RuntimeError(
        f"示波器在额外等待 {retry_timeout_s:.6g} s 后仍未返回完整波形："
        f"{last_error}"
    )


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RIGOL MHO98 正弦波上位机")
        self.resizable(True, True)
        self.inst = Instrument()
        self.closing = False
        self.status = tk.StringVar(value="未连接 - 请连接后面板 USB Device 或配置 LAN")
        self.ip = tk.StringVar(value="192.168.1.100")
        self.resource = tk.StringVar()
        self.native_resources: dict[str, str] = {}
        self.ch = tk.IntVar(value=1)
        self.freq = tk.DoubleVar(value=1000.0)
        self.amp = tk.DoubleVar(value=2.0)
        self.offset = tk.DoubleVar(value=0.0)
        self.output = tk.BooleanVar(value=False)
        self.sweep_spacing = tk.StringVar(value="linear")
        self.sweep_start_frequency = tk.DoubleVar(value=0.1)
        self.sweep_stop_frequency = tk.DoubleVar(value=500.0)
        self.sweep_linear_points = tk.IntVar(value=300)
        self.sweep_points_per_decade = tk.IntVar(value=30)
        self.capture_cycles = tk.IntVar(value=3)
        self.settle_cycles = tk.DoubleVar(value=2.0)
        self.sweep_profile_points: list[SweepPointRequest] = []
        self.sweep_profile_path: str | None = None
        self.sweep_profile_text = tk.StringVar(
            value="逐频点 A/B：未加载（使用统一幅度/偏置；默认 B=0 V）"
        )
        self.capture_channel_vars = {
            channel: tk.BooleanVar(value=channel <= 2) for channel in range(1, 5)
        }
        self.bode_reference_channel = tk.IntVar(value=2)
        self.bode_response_channel = tk.IntVar(value=1)
        self.sweep_running = False
        self.sweep_stopping = False
        self.sweep_generation = 0
        self.sweep_index = 0
        self.sweep_after_id = None
        self.active_sweep_point: int | None = None
        self.active_sweep_frequency: float | None = None
        self.active_sweep_configuration: SweepPointConfiguration | None = None
        self.sweep_point_started_at: float | None = None
        self.sweep_frequencies = make_sweep_frequencies()
        self.sweep_points = [
            SweepPointRequest(frequency, self.amp.get(), self.offset.get())
            for frequency in self.sweep_frequencies
        ]
        self.sweep_capture_cycles = 3
        self.sweep_settle_cycles = 2.0
        self.sweep_capture_channels = (1, 2)
        self.sweep_started_monotonic: float | None = None
        self.acq_running = False
        self.acq_prepared = False
        self.acq_generation = 0
        self.acq_after_id = None
        self.acq_channels: tuple[int, ...] = ()
        self.acq_values: dict[int, array] = {}
        self.acq_sweep_points = array("I")
        self.acq_requested_frequencies = array("d")
        self.acq_frequencies = array("d")
        self.acq_requested_amplitudes = array("d")
        self.acq_amplitudes = array("d")
        self.acq_timebase_scales = array("d")
        self.acq_cycle_numbers = array("I")
        self.acq_point_times = array("d")
        self.acq_instrument_times = array("d")
        self.acq_sweep_elapsed_times = array("d")
        self.acq_capture_epoch_times = array("d")
        self.acq_sample_epoch_times = array("d")
        self.acq_sample_intervals = array("d")
        self.acq_file_path: str | None = None
        self.acq_stream = None
        self.acq_writer = None
        self.bode_points: list[BodePoint] = []
        self.bode_source = ""
        self.bode_result_reference_channel = 2
        self.bode_result_response_channel = 1
        self.bode_figure = None
        self.bode_canvas = None
        self._make_ui()
        self.geometry("760x950")
        self.minsize(660, 650)
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.after(350, self.scan_usb)

    def _make_ui(self) -> None:
        pad = {"padx": 8, "pady": 5}
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        shell = ttk.Frame(self)
        shell.grid(row=0, column=0, sticky="nsew")
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(0, weight=1)
        self.ui_scroller = tk.Canvas(shell, highlightthickness=0)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=self.ui_scroller.yview)
        self.ui_scroller.configure(yscrollcommand=scrollbar.set)
        self.ui_scroller.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        f = ttk.Frame(self.ui_scroller, padding=12)
        window_id = self.ui_scroller.create_window((0, 0), window=f, anchor="nw")

        def update_scroll_region(_event=None):
            self.ui_scroller.configure(scrollregion=self.ui_scroller.bbox("all"))

        def fit_content_width(event):
            self.ui_scroller.itemconfigure(window_id, width=event.width)

        f.bind("<Configure>", update_scroll_region)
        self.ui_scroller.bind("<Configure>", fit_content_width)
        ttk.Label(f, textvariable=self.status, foreground="#a00000", wraplength=500).grid(column=0, row=0, columnspan=4, sticky="w", **pad)
        ttk.Separator(f).grid(column=0, row=1, columnspan=4, sticky="ew", pady=8)
        ttk.Label(f, text="示波器 IP Address").grid(column=0, row=2, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.ip, width=17).grid(column=1, row=2, sticky="w", **pad)
        ttk.Button(f, text="连接 LAN", command=self.connect_lan).grid(column=2, row=2, columnspan=2, sticky="ew", **pad)
        ttk.Button(f, text="扫描并连接 USB", command=self.scan_usb).grid(column=0, row=3, columnspan=2, sticky="ew", **pad)
        ttk.Label(f, text="填写示波器的 IP，不是 DNS/网关", foreground="#a00000").grid(column=2, row=3, columnspan=2, sticky="w", **pad)
        ttk.Combobox(f, textvariable=self.resource, width=47, state="readonly").grid(column=0, row=4, columnspan=3, sticky="ew", **pad)
        ttk.Button(f, text="连接 USB", command=self.connect_usb).grid(column=3, row=4, sticky="ew", **pad)
        ttk.Separator(f).grid(column=0, row=5, columnspan=4, sticky="ew", pady=8)
        ttk.Label(f, text="AFG 通道").grid(column=0, row=6, sticky="e", **pad)
        self.ch1_button = ttk.Radiobutton(f, text="GI / CH1", variable=self.ch, value=1)
        self.ch1_button.grid(column=1, row=6, sticky="w", **pad)
        self.ch2_button = ttk.Radiobutton(f, text="GII / CH2", variable=self.ch, value=2)
        self.ch2_button.grid(column=2, row=6, sticky="w", **pad)
        ttk.Label(f, text="频率 (Hz)").grid(column=0, row=7, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.freq, width=18).grid(column=1, row=7, sticky="w", **pad)
        ttk.Scale(f, from_=0.002, to=100_000_000, variable=self.freq, orient="horizontal", length=260, command=lambda _: self.schedule_apply()).grid(column=2, row=7, columnspan=2, **pad)
        ttk.Label(f, text="幅度 (Vpp)").grid(column=0, row=8, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.amp, width=18).grid(column=1, row=8, sticky="w", **pad)
        ttk.Scale(f, from_=0.002, to=20, variable=self.amp, orient="horizontal", length=260, command=lambda _: self.schedule_apply()).grid(column=2, row=8, columnspan=2, **pad)
        ttk.Label(f, text="偏置 B (V)").grid(column=0, row=9, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.offset, width=18).grid(column=1, row=9, sticky="w", **pad)
        ttk.Label(
            f,
            text="实际公式：B + (Vpp/2)·sin(2πft)；默认 B=0 V",
            wraplength=330,
        ).grid(column=2, row=9, columnspan=2, sticky="w", **pad)
        self.apply_button = ttk.Button(f, text="应用正弦波参数", command=self.apply)
        self.apply_button.grid(column=0, row=10, columnspan=2, sticky="ew", **pad)
        self.output_check = ttk.Checkbutton(f, text="启用输出", variable=self.output, command=self.set_output)
        self.output_check.grid(column=2, row=10, columnspan=2, sticky="w", **pad)
        ttk.Separator(f).grid(column=0, row=11, columnspan=4, sticky="ew", pady=8)
        sweep_config = ttk.LabelFrame(f, text="可配置扫频", padding=8)
        sweep_config.grid(column=0, row=12, columnspan=4, sticky="ew", padx=8, pady=(8, 5))
        ttk.Label(sweep_config, text="方式").grid(column=0, row=0, sticky="e", padx=4, pady=3)
        self.sweep_spacing_box = ttk.Combobox(
            sweep_config,
            textvariable=self.sweep_spacing,
            values=("linear", "log"),
            width=8,
            state="readonly",
        )
        self.sweep_spacing_box.grid(column=1, row=0, sticky="w", padx=4, pady=3)
        ttk.Label(sweep_config, text="起始 Hz").grid(column=2, row=0, sticky="e", padx=4, pady=3)
        ttk.Entry(sweep_config, textvariable=self.sweep_start_frequency, width=12).grid(
            column=3, row=0, sticky="w", padx=4, pady=3
        )
        ttk.Label(sweep_config, text="终止 Hz").grid(column=4, row=0, sticky="e", padx=4, pady=3)
        ttk.Entry(sweep_config, textvariable=self.sweep_stop_frequency, width=12).grid(
            column=5, row=0, sticky="w", padx=4, pady=3
        )
        ttk.Label(sweep_config, text="线性总点数").grid(column=0, row=1, sticky="e", padx=4, pady=3)
        ttk.Entry(sweep_config, textvariable=self.sweep_linear_points, width=9).grid(
            column=1, row=1, sticky="w", padx=4, pady=3
        )
        ttk.Label(sweep_config, text="对数点/十倍频").grid(column=2, row=1, sticky="e", padx=4, pady=3)
        ttk.Entry(sweep_config, textvariable=self.sweep_points_per_decade, width=9).grid(
            column=3, row=1, sticky="w", padx=4, pady=3
        )
        ttk.Label(sweep_config, text="保存周期 M").grid(column=4, row=1, sticky="e", padx=4, pady=3)
        ttk.Entry(sweep_config, textvariable=self.capture_cycles, width=9).grid(
            column=5, row=1, sticky="w", padx=4, pady=3
        )
        ttk.Label(sweep_config, text="稳定周期").grid(column=0, row=2, sticky="e", padx=4, pady=3)
        ttk.Entry(sweep_config, textvariable=self.settle_cycles, width=9).grid(
            column=1, row=2, sticky="w", padx=4, pady=3
        )
        self.sweep_button = ttk.Button(
            sweep_config, text="开始扫频", command=self.toggle_sweep
        )
        self.sweep_button.grid(column=2, row=2, columnspan=2, sticky="ew", padx=4, pady=3)
        ttk.Label(
            sweep_config,
            text="范围 2 mHz–100 MHz；线性使用总点数，对数使用每十倍频点数。",
            wraplength=270,
        ).grid(column=4, row=2, columnspan=2, sticky="w", padx=4, pady=3)
        self.load_sweep_profile_button = ttk.Button(
            sweep_config, text="加载逐频点 A/B CSV…", command=self.load_sweep_profile
        )
        self.load_sweep_profile_button.grid(
            column=0, row=3, columnspan=2, sticky="ew", padx=4, pady=3
        )
        self.clear_sweep_profile_button = ttk.Button(
            sweep_config, text="清除 A/B 曲线", command=self.clear_sweep_profile
        )
        self.clear_sweep_profile_button.grid(
            column=2, row=3, sticky="ew", padx=4, pady=3
        )
        ttk.Label(
            sweep_config,
            textvariable=self.sweep_profile_text,
            wraplength=390,
        ).grid(column=3, row=3, columnspan=3, sticky="w", padx=4, pady=3)
        preview = ttk.LabelFrame(f, text="正弦波扫频预览（设定值，非实际采样）", padding=8)
        preview.grid(column=0, row=13, columnspan=4, sticky="ew", padx=8, pady=(10, 5))
        self.preview_text = tk.StringVar(
            value="0.1000 Hz | A=1 V peak | B=0 V | 保存 3 周期"
        )
        ttk.Label(preview, textvariable=self.preview_text).grid(column=0, row=0, sticky="w")
        self.sweep_progress = ttk.Progressbar(
            preview, maximum=len(self.sweep_frequencies), length=230, mode="determinate"
        )
        self.sweep_progress.grid(column=1, row=0, sticky="e", padx=(16, 0))
        self.preview_canvas = tk.Canvas(
            preview,
            width=590,
            height=210,
            background="#071421",
            highlightthickness=1,
            highlightbackground="#506070",
        )
        self.preview_canvas.grid(column=0, row=1, columnspan=2, pady=(7, 0))
        acquisition = ttk.LabelFrame(
            f, text="同一冻结采集帧的 CH1–CH4 数据", padding=8
        )
        acquisition.grid(column=0, row=14, columnspan=4, sticky="ew", padx=8, pady=(8, 5))
        ttk.Label(acquisition, text="采集通道").grid(column=0, row=0, sticky="e", padx=(0, 6))
        self.capture_channel_buttons = []
        for index, channel in enumerate(range(1, 5), start=1):
            button = ttk.Checkbutton(
                acquisition,
                text=f"CH{channel}",
                variable=self.capture_channel_vars[channel],
            )
            button.grid(column=index, row=0, sticky="w", padx=4)
            self.capture_channel_buttons.append(button)
        self.acq_button = ttk.Button(acquisition, text="开始记录", command=self.toggle_acquisition)
        self.acq_button.grid(column=0, row=1, columnspan=2, sticky="ew", padx=(0, 6), pady=(6, 0))
        self.save_button = ttk.Button(acquisition, text="导出 CSV 副本…", command=self.save_acquisition, state="disabled")
        self.save_button.grid(column=2, row=1, columnspan=2, sticky="ew", padx=6, pady=(6, 0))
        self.clear_button = ttk.Button(acquisition, text="清空数据", command=self.clear_acquisition)
        self.clear_button.grid(column=4, row=1, sticky="ew", padx=6, pady=(6, 0))
        self.acq_text = tk.StringVar(
            value="尚未记录 | 每个频点先稳定，再冻结一次并读取所有勾选通道"
        )
        ttk.Label(acquisition, textvariable=self.acq_text, wraplength=565).grid(
            column=0, row=2, columnspan=5, sticky="w", pady=(7, 4)
        )
        bode = ttk.LabelFrame(f, text="伯德图（响应 / 参考）", padding=8)
        bode.grid(column=0, row=15, columnspan=4, sticky="ew", padx=8, pady=(8, 5))
        ttk.Label(bode, text="参考/激励").grid(column=0, row=0, sticky="e", padx=4)
        ttk.Combobox(
            bode,
            textvariable=self.bode_reference_channel,
            values=(1, 2, 3, 4),
            width=5,
            state="readonly",
        ).grid(column=1, row=0, sticky="w", padx=4)
        ttk.Label(bode, text="响应").grid(column=2, row=0, sticky="e", padx=4)
        ttk.Combobox(
            bode,
            textvariable=self.bode_response_channel,
            values=(1, 2, 3, 4),
            width=5,
            state="readonly",
        ).grid(column=3, row=0, sticky="w", padx=4)
        ttk.Button(
            bode, text="用当前采集数据绘制", command=self.generate_bode_from_current
        ).grid(column=0, row=1, sticky="ew", padx=(0, 5), pady=(6, 0))
        ttk.Button(
            bode, text="加载波形 CSV…", command=self.load_bode_csv
        ).grid(column=1, row=1, sticky="ew", padx=5, pady=(6, 0))
        self.save_bode_data_button = ttk.Button(
            bode, text="保存伯德数据…", command=self.save_bode_data, state="disabled"
        )
        self.save_bode_data_button.grid(column=2, row=1, sticky="ew", padx=5, pady=(6, 0))
        self.save_bode_image_button = ttk.Button(
            bode, text="保存伯德图…", command=self.save_bode_image, state="disabled"
        )
        self.save_bode_image_button.grid(column=3, row=1, sticky="ew", padx=(5, 0), pady=(6, 0))
        self.bode_text = tk.StringVar(
            value="尚未生成 | 默认参考 CH2（信号源回读），响应 CH1（传感器）"
        )
        ttk.Label(bode, textvariable=self.bode_text, wraplength=680).grid(
            column=0, row=2, columnspan=4, sticky="w", pady=(7, 4)
        )
        if Figure is not None and FigureCanvasTkAgg is not None:
            self.bode_figure = Figure(figsize=(6.6, 4.6), dpi=100, constrained_layout=True)
            self.bode_magnitude_axis = self.bode_figure.add_subplot(211)
            self.bode_phase_axis = self.bode_figure.add_subplot(
                212, sharex=self.bode_magnitude_axis
            )
            self.bode_canvas = FigureCanvasTkAgg(self.bode_figure, master=bode)
            self.bode_canvas.get_tk_widget().grid(
                column=0, row=3, columnspan=4, sticky="ew", pady=(4, 0)
            )
            self.draw_bode_plot()
        else:
            ttk.Label(
                bode,
                text="无法加载 Matplotlib：" + _PLOT_IMPORT_ERROR,
                foreground="#a00000",
                wraplength=650,
            ).grid(column=0, row=3, columnspan=4, sticky="w")
        self.after_id = None
        self.update_wave_preview(
            0.1,
            self.amp.get(),
            self.offset.get(),
            0,
            self.capture_cycles.get(),
        )

    def set_status(self, text: str, ok: bool = False) -> None:
        self.status.set(text)

    def background(self, fn):
        def runner():
            try:
                result = fn()
            except Exception as exc:
                # Python clears the exception variable after this block.  Convert it
                # now so the Tk callback can safely run later on the main thread.
                error_message = "操作失败：" + str(exc)
                self.after(0, lambda message=error_message: self.set_status(message))
            else:
                self.after(0, lambda message=result: self.set_status(message, True))
        threading.Thread(target=runner, daemon=True).start()

    def load_sweep_profile(self):
        if self.sweep_running or self.sweep_stopping:
            self.set_status("扫频运行或停止清理期间不能更换逐频点 A/B 曲线。")
            return
        path = filedialog.askopenfilename(
            title="加载逐频点 A/B 正弦曲线",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            points = load_sweep_ab_profile(path)
        except Exception as exc:
            self.set_status("A/B 曲线加载失败：" + str(exc))
            return
        self.sweep_profile_points = points
        self.sweep_profile_path = path
        frequencies = [point.frequency_hz for point in points]
        first = points[0]
        self.sweep_profile_text.set(
            f"{Path(path).name}：{len(points)} 点，"
            f"{min(frequencies):.6g}–{max(frequencies):.6g} Hz；按文件行序扫频"
        )
        self.sweep_progress.configure(maximum=len(points))
        self.sweep_progress["value"] = 0
        self.update_wave_preview(
            first.frequency_hz,
            first.amplitude_vpp,
            first.offset_v,
            0,
            self.capture_cycles.get(),
        )
        self.set_status(
            f"已加载逐频点 A/B 曲线：{len(points)} 点；"
            "开始扫频时将忽略统一频率范围、统一幅度和统一偏置。"
        )

    def clear_sweep_profile(self):
        if self.sweep_running or self.sweep_stopping:
            self.set_status("扫频运行或停止清理期间不能清除逐频点 A/B 曲线。")
            return
        self.sweep_profile_points = []
        self.sweep_profile_path = None
        self.sweep_profile_text.set(
            "逐频点 A/B：未加载（使用统一幅度/偏置；默认 B=0 V）"
        )
        try:
            self.update_wave_preview(
                float(self.sweep_start_frequency.get()),
                float(self.amp.get()),
                float(self.offset.get()),
                0,
                int(self.capture_cycles.get()),
            )
        except (ValueError, tk.TclError):
            pass
        self.set_status("已清除逐频点 A/B 曲线；恢复使用 GUI 统一扫频参数。")

    def connect_lan(self):
        self.background(lambda: "已连接 LAN：" + self.inst.connect_lan(self.ip.get().strip()))

    def scan_usb(self):
        def work():
            devices = list_usbtmc_devices()
            native = {device.resource: device.path for device in devices}
            self.native_resources = native
            if native:
                resources = list(native)
                selected = resources[0]
                self.after(0, lambda value=selected: self.resource.set(value))
                idn = self.inst.connect_native_usb(native[selected])
                return "已自动连接 USB：" + idn

            # Compatibility fallback for systems whose VISA implementation does
            # enumerate USB resources normally.
            try:
                import pyvisa
                resources = list(pyvisa.ResourceManager().list_resources())
            except Exception as exc:
                raise RuntimeError(
                    "无法扫描 USB-VISA：" + str(exc) + "。请用当前 Python 安装依赖，"
                    "并安装 RIGOL Ultra Sigma（含 NI-VISA）后重新插拔示波器后面板 USB Device 数据线。"
                )
            usb = [r for r in resources if r.upper().startswith("USB")]
            self.after(0, lambda: self.resource.set(usb[0] if usb else ""))
            return "发现 USB-VISA：" + ("； ".join(usb) if usb else "无（Windows 未发现 USBTMC 接口）")
        self.background(work)

    def connect_usb(self):
        r = self.resource.get().strip()
        if not r: self.set_status("没有可连接的 USBTMC 设备，请先扫描。 "); return
        if r in self.native_resources:
            self.background(lambda: "已连接 USB：" + self.inst.connect_native_usb(self.native_resources[r]))
        else:
            self.background(lambda: "已连接 USB：" + self.inst.connect_visa(r))

    def schedule_apply(self):
        if self.after_id: self.after_cancel(self.after_id)
        self.after_id = self.after(180, self.apply)

    def apply(self):
        if self.sweep_running or self.sweep_stopping:
            self.set_status("扫频正在运行或停止清理中；暂不能手动设置参数。")
            return
        if not self.inst.connected: self.set_status("未连接：参数未发送。"); return
        try:
            freq = float(self.freq.get())
            amp = float(self.amp.get())
            offset = float(self.offset.get())
            validate_afg_sine_parameters(freq, amp, offset)
        except (ValueError, tk.TclError) as exc:
            self.set_status("参数错误：" + str(exc)); return
        self.update_wave_preview(freq, amp, offset)
        channel = self.ch.get()
        self.background(lambda: self._apply_scpi(channel, freq, amp, offset))

    def _apply_scpi(
        self, channel: int, freq: float, amp: float, offset: float
    ) -> str:
        self.inst.write(f":SOURce{channel}:FUNCtion SINusoid")
        self.inst.write(f":SOURce{channel}:FREQuency {freq:.12g}")
        self.inst.write(f":SOURce{channel}:VOLTage:OFFSet 0")
        self.inst.write(f":SOURce{channel}:VOLTage:AMPLitude {amp:.12g}")
        self.inst.write(f":SOURce{channel}:VOLTage:OFFSet {offset:.12g}")
        actual_frequency = float(self.inst.query(f":SOURce{channel}:FREQuency?"))
        actual_amplitude = float(
            self.inst.query(f":SOURce{channel}:VOLTage:AMPLitude?")
        )
        actual_offset = float(
            self.inst.query(f":SOURce{channel}:VOLTage:OFFSet?")
        )
        err = self.inst.query(":SYSTem:ERRor?")
        if not err.startswith("0"):
            raise RuntimeError("仪器返回：" + err)
        return (
            f"GI/GII {channel} 已接受：{actual_frequency:.12g} Hz，"
            f"A={actual_amplitude / 2.0:.12g} V peak "
            f"({actual_amplitude:.12g} Vpp)，B={actual_offset:.12g} V"
        )

    def set_output(self):
        if not self.inst.connected: self.output.set(False); self.set_status("未连接：无法切换输出。"); return
        ch, on = self.ch.get(), self.output.get()
        self.background(lambda: self._out_scpi(ch, on))

    def _out_scpi(self, channel: int, on: bool) -> str:
        self.inst.write(f":SOURce{channel}:OUTPut:STATe {'ON' if on else 'OFF'}")
        return f"GI/GII {channel} 输出已{'开启' if on else '关闭'}"

    def toggle_sweep(self):
        if self.sweep_stopping:
            self.set_status("正在关闭输出并恢复时基，请等待停止完成。")
            return
        if self.sweep_running:
            self.stop_sweep("用户停止扫频")
        else:
            self.start_sweep()

    def start_sweep(self):
        if self.sweep_stopping:
            self.set_status("上一次扫频仍在停止清理中，请稍候。")
            return
        if not self.inst.connected:
            self.set_status("未连接：请先连接 MHO98，再开始扫频。")
            return
        try:
            capture_cycles = int(self.capture_cycles.get())
            settle_cycles = float(self.settle_cycles.get())
            if not 1 <= capture_cycles <= 100_000:
                raise ValueError("保存周期 M 必须在 1–100000。")
            if not 0 <= settle_cycles <= 100_000:
                raise ValueError("稳定周期必须在 0–100000。")
            if self.sweep_profile_points:
                sweep_points = list(self.sweep_profile_points)
                for point in sweep_points:
                    validate_afg_sine_parameters(
                        point.frequency_hz, point.amplitude_vpp, point.offset_v
                    )
            else:
                amplitude = float(self.amp.get())
                offset = float(self.offset.get())
                frequencies = make_sweep_frequencies(
                    start_hz=float(self.sweep_start_frequency.get()),
                    stop_hz=float(self.sweep_stop_frequency.get()),
                    spacing=self.sweep_spacing.get(),
                    linear_points=int(self.sweep_linear_points.get()),
                    points_per_decade=int(self.sweep_points_per_decade.get()),
                )
                sweep_points = []
                for frequency in frequencies:
                    validate_afg_sine_parameters(frequency, amplitude, offset)
                    sweep_points.append(
                        SweepPointRequest(frequency, amplitude, offset)
                    )
            capture_channels = tuple(
                channel
                for channel in range(1, 5)
                if self.capture_channel_vars[channel].get()
            )
            if not capture_channels:
                raise ValueError("请至少勾选一个采集通道。")
        except (ValueError, tk.TclError) as exc:
            self.set_status("扫频参数错误：" + str(exc))
            return

        if self.acq_running and self.acq_channels != capture_channels:
            self.set_status("当前记录文件的通道与勾选项不同；请先停止记录再开始扫频。")
            return
        frequencies = [point.frequency_hz for point in sweep_points]
        self.sweep_points = sweep_points
        self.sweep_frequencies = frequencies
        self.sweep_capture_cycles = capture_cycles
        self.sweep_settle_cycles = settle_cycles
        self.sweep_capture_channels = capture_channels
        if not self.acq_running:
            if not self.start_acquisition():
                return
        self.sweep_running = True
        self.sweep_generation += 1
        self.sweep_index = 0
        self.active_sweep_point = None
        self.active_sweep_frequency = None
        self.active_sweep_configuration = None
        self.sweep_point_started_at = None
        self.sweep_started_monotonic = time.monotonic()
        self.sweep_channel = self.ch.get()
        self.sweep_button.configure(text="停止扫频")
        self.apply_button.configure(state="disabled")
        self.ch1_button.configure(state="disabled")
        self.ch2_button.configure(state="disabled")
        self.output_check.configure(state="disabled")
        self.output.set(True)
        self.sweep_progress.configure(maximum=len(self.sweep_frequencies))
        self.sweep_progress["value"] = 0
        self._run_sweep_point()

    def _run_sweep_point(self):
        self.sweep_after_id = None
        if not self.sweep_running:
            return
        if self.sweep_index >= len(self.sweep_frequencies):
            self.stop_sweep("扫频完成")
            return

        point_number = self.sweep_index + 1
        request = self.sweep_points[self.sweep_index]
        frequency = request.frequency_hz
        channel = self.sweep_channel
        amplitude = request.amplitude_vpp
        offset = request.offset_v
        generation = self.sweep_generation
        self.freq.set(frequency)
        self.amp.set(amplitude)
        self.offset.set(offset)
        self.update_wave_preview(
            frequency,
            amplitude,
            offset,
            point_number,
            self.sweep_capture_cycles,
        )
        self.set_status(
            f"正在设置扫频点 {point_number}/{len(self.sweep_frequencies)}："
            f"{frequency:.6g} Hz，A={amplitude / 2.0:.6g} V peak，"
            f"B={offset:.6g} V"
        )

        def worker():
            try:
                configuration = self.inst.configure_sweep_point(
                    channel,
                    frequency,
                    amplitude,
                    self.sweep_capture_cycles,
                    lambda: self.sweep_running and self.sweep_generation == generation,
                    offset_v=offset,
                )
            except Exception as exc:
                message = "扫频失败：" + str(exc)
                self.after(0, lambda text=message: self.stop_sweep(text))
                return
            if configuration is None:
                return
            self.after(
                0,
                lambda index=point_number, token=generation, config=configuration:
                    self._sweep_point_started(index, token, config),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _sweep_point_started(
        self,
        point_number: int,
        generation: int,
        configuration: SweepPointConfiguration,
    ):
        if (
            not self.sweep_running
            or generation != self.sweep_generation
            or point_number != self.sweep_index + 1
        ):
            return
        frequency = configuration.actual_frequency_hz
        period = 1.0 / frequency
        dwell_cycles = self.sweep_settle_cycles + self.sweep_capture_cycles
        dwell_seconds = sweep_point_dwell_duration(frequency, dwell_cycles)
        remaining = sum(
            sweep_point_dwell_duration(value, dwell_cycles)
            for value in self.sweep_frequencies[point_number - 1:]
        )
        self.set_status(
            f"扫频点 {point_number}/{len(self.sweep_frequencies)} | "
            f"设定/回读 {configuration.requested_frequency_hz:.6g}/"
            f"{frequency:.6g} Hz | 计划驻留 {dwell_seconds:.6g} s；"
            f"A={configuration.actual_amplitude_vpp / 2.0:.6g} V peak，"
            f"B={configuration.actual_offset_v:.6g} V；"
            f"时基 {configuration.timebase_scale_s:.6g} s/div；"
            f"预计纯驻留剩余 {int(remaining) // 60} 分 {int(remaining) % 60} 秒"
        )
        self.sweep_index += 1
        self.sweep_progress["value"] = point_number
        self.active_sweep_point = point_number
        self.active_sweep_frequency = frequency
        self.active_sweep_configuration = configuration
        self.sweep_point_started_at = time.monotonic()
        if self.acq_running and self.acq_prepared:
            self._schedule_sweep_capture(
                self.acq_generation,
                generation,
                point_number,
                configuration,
                self.sweep_point_started_at,
            )

    def stop_sweep(self, reason: str = "扫频已停止"):
        if self.sweep_stopping:
            return
        completed_normally = reason == "扫频完成"
        was_running = self.sweep_running
        self.sweep_running = False
        self.sweep_generation += 1
        self.active_sweep_point = None
        self.active_sweep_frequency = None
        self.active_sweep_configuration = None
        self.sweep_point_started_at = None
        if self.sweep_after_id is not None:
            self.after_cancel(self.sweep_after_id)
            self.sweep_after_id = None
        self.output.set(False)
        if self.acq_running:
            self.stop_acquisition(reason + "；多通道电脑端记录已结束")
        if was_running and self.inst.connected:
            self.sweep_stopping = True
            self.sweep_button.configure(text="正在停止…", state="disabled")
            self.set_status(reason + "；正在关闭输出并恢复时基，完成前禁止重新启动。")
            channel = self.sweep_channel

            def worker():
                try:
                    restored_scale = self.inst.finish_sweep(channel)
                except Exception as exc:
                    message = reason + "；输出/时基清理失败：" + str(exc)
                else:
                    if restored_scale is None:
                        message = reason + "；输出已关闭。"
                    else:
                        message = (
                            reason + f"；输出已关闭，原时基已恢复为 "
                            f"{restored_scale:.6g} s/div。"
                        )
                if not self.closing:
                    self.after(
                        0,
                        lambda text=message, completed=completed_normally:
                            self._finish_sweep_cleanup(text, completed),
                    )

            threading.Thread(target=worker, daemon=True).start()
        else:
            self._finish_sweep_cleanup(reason, completed_normally)

    def _finish_sweep_cleanup(self, message: str, completed_normally: bool):
        self.sweep_stopping = False
        self.sweep_started_monotonic = None
        self.sweep_button.configure(text="开始扫频", state="normal")
        self.apply_button.configure(state="normal")
        self.ch1_button.configure(state="normal")
        self.ch2_button.configure(state="normal")
        self.output_check.configure(state="normal")
        self.set_status(message)
        if completed_normally and self._acquisition_sample_count() > 0:
            self.after(250, self.generate_bode_from_current)

    def toggle_acquisition(self):
        if self.acq_running:
            if self.sweep_running:
                self.stop_sweep("用户停止采集和扫频")
            else:
                self.stop_acquisition("用户停止采集")
        else:
            self.start_acquisition()

    def _selected_capture_channels(self) -> tuple[int, ...]:
        return tuple(
            channel
            for channel in range(1, 5)
            if self.capture_channel_vars[channel].get()
        )

    @staticmethod
    def _acquisition_csv_header(channels: tuple[int, ...]) -> list[str]:
        return [
            "sample_index",
            "estimated_sample_timestamp_utc",
            "capture_timestamp_utc",
            "sweep_elapsed_s",
            "sweep_point",
            "requested_frequency_hz",
            "frequency_hz",
            "requested_amplitude_vpp",
            "amplitude_vpp",
            "requested_sine_amplitude_peak_v",
            "sine_amplitude_peak_v",
            "requested_offset_v",
            "offset_v",
            "requested_phase_deg",
            "phase_deg",
            "timebase_scale_s",
            "period_s",
            "cycle_number",
            "point_time_s",
            "instrument_time_s",
            "sample_interval_s",
            *(f"ch{channel}_voltage_v" for channel in channels),
        ]

    def _reset_acquisition_arrays(self, channels: tuple[int, ...] = ()) -> None:
        self.acq_channels = channels
        self.acq_values = {channel: array("d") for channel in channels}
        self.acq_sweep_points = array("I")
        self.acq_requested_frequencies = array("d")
        self.acq_frequencies = array("d")
        self.acq_requested_amplitudes = array("d")
        self.acq_amplitudes = array("d")
        self.acq_requested_offsets = array("d")
        self.acq_offsets = array("d")
        self.acq_requested_phases = array("d")
        self.acq_phases = array("d")
        self.acq_timebase_scales = array("d")
        self.acq_cycle_numbers = array("I")
        self.acq_point_times = array("d")
        self.acq_instrument_times = array("d")
        self.acq_sweep_elapsed_times = array("d")
        self.acq_capture_epoch_times = array("d")
        self.acq_sample_epoch_times = array("d")
        self.acq_sample_intervals = array("d")

    def _acquisition_sample_count(self) -> int:
        if not self.acq_channels:
            return 0
        return min(len(self.acq_values[channel]) for channel in self.acq_channels)

    def start_acquisition(self) -> bool:
        if not self.inst.connected:
            self.set_status("未连接：请先连接 MHO98，再开始多通道采集。")
            return False
        channels = self._selected_capture_channels()
        if not channels:
            self.set_status("请至少勾选一个采集通道。")
            return False
        channel_label = "_".join(f"CH{channel}" for channel in channels)
        default_name = (
            f"MHO98_{channel_label}_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
        )
        path = filedialog.asksaveasfilename(
            title=f"选择 {channel_label} 实时记录位置",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            self.set_status("已取消记录：未选择 CSV 保存位置。")
            return False
        try:
            stream = open(path, "w", newline="", encoding="utf-8-sig")
            writer = csv.writer(stream)
            writer.writerow(self._acquisition_csv_header(channels))
            stream.flush()
        except Exception as exc:
            self.set_status("无法创建 CSV 文件：" + str(exc))
            return False
        self.acq_running = True
        self.acq_prepared = True
        self.acq_generation += 1
        self._reset_acquisition_arrays(channels)
        self.acq_file_path = path
        self.acq_stream = stream
        self.acq_writer = writer
        self.acq_button.configure(text="停止记录（仅电脑端）")
        for button in self.capture_channel_buttons:
            button.configure(state="disabled")
        self.save_button.configure(state="disabled")
        self.acq_text.set(f"CSV 已创建，等待扫频：{path}")
        self.set_status(f"{channel_label} 记录已就绪；数据将逐频点写入 CSV。")
        if (
            self.sweep_running
            and self.active_sweep_point is not None
            and self.active_sweep_frequency is not None
            and self.active_sweep_configuration is not None
            and self.sweep_point_started_at is not None
        ):
            self._schedule_sweep_capture(
                self.acq_generation,
                self.sweep_generation,
                self.active_sweep_point,
                self.active_sweep_configuration,
                self.sweep_point_started_at,
            )
        return True

    def _schedule_sweep_capture(
        self,
        generation: int,
        sweep_generation: int,
        point_number: int,
        configuration: SweepPointConfiguration,
        point_started_at: float,
    ):
        if self.acq_after_id is not None:
            self.after_cancel(self.acq_after_id)
            self.acq_after_id = None
        if not self.acq_running or not self.acq_prepared:
            return
        if (
            not self.sweep_running
            or sweep_generation != self.sweep_generation
            or point_number != self.active_sweep_point
        ):
            return
        elapsed = time.monotonic() - point_started_at
        point_duration = sweep_point_dwell_duration(
            configuration.actual_frequency_hz,
            self.sweep_settle_cycles + self.sweep_capture_cycles,
        )
        delay_ms = max(1, int((point_duration - elapsed) * 1000))
        self.acq_after_id = self.after(
            delay_ms,
            lambda: self._request_acquisition_frame(
                generation,
                sweep_generation,
                point_number,
                configuration,
                point_started_at,
            ),
        )

    def _request_acquisition_frame(
        self,
        generation: int,
        sweep_generation: int,
        point_number: int,
        configuration: SweepPointConfiguration,
        point_started_at: float,
    ):
        self.acq_after_id = None
        if (
            not self.acq_running
            or generation != self.acq_generation
            or not self.sweep_running
            or sweep_generation != self.sweep_generation
            or point_number != self.active_sweep_point
        ):
            return

        def worker():
            try:
                result = acquire_complete_cycles(
                    instrument=self.inst,
                    channels=self.acq_channels,
                    frequency=configuration.actual_frequency_hz,
                    capture_cycles=self.sweep_capture_cycles,
                    is_current=lambda: (
                        self.acq_running
                        and generation == self.acq_generation
                        and self.sweep_running
                        and sweep_generation == self.sweep_generation
                        and point_number == self.active_sweep_point
                    ),
                    on_retry=lambda attempt, _error: self.after(
                        0,
                        lambda number=attempt: (
                            self.set_status(
                                f"扫频点 {point_number} 波形尚未完整，"
                                f"保持当前频率/时基并第 {number} 次等待重试。"
                            )
                            if (
                                self.acq_running
                                and generation == self.acq_generation
                                and self.sweep_running
                                and sweep_generation == self.sweep_generation
                                and point_number == self.active_sweep_point
                            )
                            else None
                        ),
                    ),
                )
                if result is None:
                    return
                (
                    capture,
                    values,
                    point_times,
                    instrument_times,
                    sample_interval,
                    _retry_count,
                ) = result
                sweep_started = self.sweep_started_monotonic or point_started_at
                capture_elapsed = capture.captured_monotonic_s - sweep_started
            except Exception as exc:
                message = "采集失败：" + str(exc)
                self.after(
                    0,
                    lambda text=message: self._stop_sweep_for_current_capture_error(
                        generation, sweep_generation, point_number, text
                    ),
                )
                return
            self.after(
                0,
                lambda token=generation, sweep_token=sweep_generation,
                point=point_number, config=configuration, times=point_times,
                scope_times=instrument_times, interval=sample_interval,
                channel_values=values, captured=capture.captured_at_utc,
                elapsed=capture_elapsed:
                    self._accept_acquisition_frame(
                        token, sweep_token, point, config, times, scope_times,
                        interval, channel_values, captured, elapsed
                    ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _stop_sweep_for_current_capture_error(
        self,
        generation: int,
        sweep_generation: int,
        point_number: int,
        message: str,
    ):
        if (
            self.acq_running
            and generation == self.acq_generation
            and self.sweep_running
            and sweep_generation == self.sweep_generation
            and point_number == self.active_sweep_point
        ):
            self.stop_sweep(message)

    def _accept_acquisition_frame(
        self,
        generation: int,
        sweep_generation: int,
        point_number: int,
        configuration: SweepPointConfiguration,
        point_times: list[float],
        instrument_times: list[float],
        sample_interval: float,
        values_by_channel: dict[int, list[float]],
        captured_at_utc: datetime,
        capture_elapsed_s: float,
    ):
        if (
            not self.acq_running
            or generation != self.acq_generation
            or not self.sweep_running
            or sweep_generation != self.sweep_generation
            or point_number != self.active_sweep_point
        ):
            return
        frequency = configuration.actual_frequency_hz
        period = 1.0 / frequency
        cycle_numbers = [
            min(
                self.sweep_capture_cycles,
                max(1, math.floor((value + period * 1e-9) / period) + 1),
            )
            for value in point_times
        ]
        final_point_time = point_times[-1]
        sweep_elapsed_times = [
            capture_elapsed_s - (final_point_time - value) for value in point_times
        ]
        capture_epoch = captured_at_utc.timestamp()
        capture_epoch_times = [capture_epoch] * len(point_times)
        sample_epoch_times = [
            capture_epoch - (final_point_time - value) for value in point_times
        ]
        try:
            self._append_live_csv(
                point_number,
                configuration,
                cycle_numbers,
                point_times,
                instrument_times,
                sweep_elapsed_times,
                capture_epoch_times,
                sample_epoch_times,
                sample_interval,
                values_by_channel,
            )
        except Exception as exc:
            message = "CSV 写入失败：" + str(exc)
            self.stop_sweep(message)
            return
        count = len(point_times)
        for channel in self.acq_channels:
            self.acq_values[channel].extend(values_by_channel[channel])
        self.acq_sweep_points.extend([point_number] * count)
        self.acq_requested_frequencies.extend(
            [configuration.requested_frequency_hz] * count
        )
        self.acq_frequencies.extend([frequency] * count)
        self.acq_requested_amplitudes.extend(
            [configuration.requested_amplitude_vpp] * count
        )
        self.acq_amplitudes.extend([configuration.actual_amplitude_vpp] * count)
        self.acq_requested_offsets.extend([configuration.requested_offset_v] * count)
        self.acq_offsets.extend([configuration.actual_offset_v] * count)
        self.acq_requested_phases.extend([configuration.requested_phase_deg] * count)
        self.acq_phases.extend([configuration.actual_phase_deg] * count)
        self.acq_timebase_scales.extend([configuration.timebase_scale_s] * count)
        self.acq_cycle_numbers.extend(cycle_numbers)
        self.acq_point_times.extend(point_times)
        self.acq_instrument_times.extend(instrument_times)
        self.acq_sweep_elapsed_times.extend(sweep_elapsed_times)
        self.acq_capture_epoch_times.extend(capture_epoch_times)
        self.acq_sample_epoch_times.extend(sample_epoch_times)
        self.acq_sample_intervals.extend([sample_interval] * count)
        self.save_button.configure(state="normal")
        channels_text = "/".join(f"CH{channel}" for channel in self.acq_channels)
        self.acq_text.set(
            f"扫频点 {point_number}/{len(self.sweep_frequencies)} | "
            f"实际 {frequency:.6g} Hz | {channels_text} 同帧 {count:,} 点 | "
            f"保存 {self.sweep_capture_cycles} 周期 | "
            f"原始间隔 {sample_interval:.6g} s"
        )
        self.sweep_after_id = self.after(1, self._run_sweep_point)

    def _append_live_csv(
        self,
        point_number: int,
        configuration: SweepPointConfiguration,
        cycle_numbers: list[int],
        point_times: list[float],
        instrument_times: list[float],
        sweep_elapsed_times: list[float],
        capture_epoch_times: list[float],
        sample_epoch_times: list[float],
        sample_interval: float,
        values_by_channel: dict[int, list[float]],
    ):
        if self.acq_writer is None or self.acq_stream is None:
            raise RuntimeError("实时 CSV 文件未打开。")
        lengths = {
            len(cycle_numbers),
            len(point_times),
            len(instrument_times),
            len(sweep_elapsed_times),
            len(capture_epoch_times),
            len(sample_epoch_times),
            *(len(values_by_channel[channel]) for channel in self.acq_channels),
        }
        if len(lengths) != 1:
            raise RuntimeError("多通道波形或采集元数据的点数不一致。")
        base_index = self._acquisition_sample_count()
        frequency = configuration.actual_frequency_hz
        period = 1.0 / frequency
        capture_stamp = datetime.fromtimestamp(
            capture_epoch_times[0], timezone.utc
        ).isoformat(timespec="milliseconds")
        for offset in range(len(point_times)):
            index = base_index + offset
            estimated_stamp = datetime.fromtimestamp(
                sample_epoch_times[offset], timezone.utc
            ).isoformat(timespec="milliseconds")
            self.acq_writer.writerow([
                index,
                estimated_stamp,
                capture_stamp,
                f"{sweep_elapsed_times[offset]:.9f}",
                point_number,
                f"{configuration.requested_frequency_hz:.12g}",
                f"{frequency:.12g}",
                f"{configuration.requested_amplitude_vpp:.12g}",
                f"{configuration.actual_amplitude_vpp:.12g}",
                f"{configuration.requested_amplitude_vpp / 2.0:.12g}",
                f"{configuration.actual_amplitude_vpp / 2.0:.12g}",
                f"{configuration.requested_offset_v:.12g}",
                f"{configuration.actual_offset_v:.12g}",
                f"{configuration.requested_phase_deg:.12g}",
                f"{configuration.actual_phase_deg:.12g}",
                f"{configuration.timebase_scale_s:.12g}",
                f"{period:.12g}",
                cycle_numbers[offset],
                f"{point_times[offset]:.9f}",
                f"{instrument_times[offset]:.12g}",
                f"{sample_interval:.12g}",
                *(
                    f"{values_by_channel[channel][offset]:.12g}"
                    for channel in self.acq_channels
                ),
            ])
        # Flush each frequency point so completed cycle data is already on disk.
        self.acq_stream.flush()

    def stop_acquisition(self, reason: str = "采集已停止"):
        self.acq_running = False
        self.acq_prepared = False
        self.acq_generation += 1
        if self.acq_after_id is not None:
            self.after_cancel(self.acq_after_id)
            self.acq_after_id = None
        file_path = self.acq_file_path
        if self.acq_stream is not None:
            try:
                self.acq_stream.flush()
                self.acq_stream.close()
            except Exception as exc:
                reason += "；CSV 关闭失败：" + str(exc)
        self.acq_stream = None
        self.acq_writer = None
        self.acq_button.configure(text="开始记录")
        for button in self.capture_channel_buttons:
            button.configure(state="normal")
        sample_count = self._acquisition_sample_count()
        if sample_count:
            self.save_button.configure(state="normal")
            channels_text = "/".join(f"CH{channel}" for channel in self.acq_channels)
            self.acq_text.set(
                f"{reason} | {channels_text} 各 {sample_count:,} 点 | 已写入：{file_path}"
            )
        else:
            self.acq_text.set(reason + f" | CSV 已创建但尚无数据：{file_path}")
        self.set_status(reason)

    def clear_acquisition(self):
        if self.acq_running:
            self.set_status("请先停止采集，再清空数据。")
            return
        self._reset_acquisition_arrays()
        self.acq_file_path = None
        self.save_button.configure(state="disabled")
        self.acq_text.set(
            "尚未记录 | 每个频点先稳定，再冻结一次并读取所有勾选通道"
        )

    def save_acquisition(self):
        count = self._acquisition_sample_count()
        if not count:
            self.set_status("没有可保存的采集数据。")
            return
        channels = self.acq_channels
        channel_label = "_".join(f"CH{channel}" for channel in channels)
        default_name = (
            f"MHO98_{channel_label}_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
        )
        path = filedialog.asksaveasfilename(
            title=f"保存 MHO98 {channel_label} 采集数据",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        values = {channel: self.acq_values[channel][:count] for channel in channels}
        sweep_points = self.acq_sweep_points[:count]
        requested_frequencies = self.acq_requested_frequencies[:count]
        frequencies = self.acq_frequencies[:count]
        requested_amplitudes = self.acq_requested_amplitudes[:count]
        amplitudes = self.acq_amplitudes[:count]
        requested_offsets = self.acq_requested_offsets[:count]
        offsets = self.acq_offsets[:count]
        requested_phases = self.acq_requested_phases[:count]
        phases = self.acq_phases[:count]
        timebase_scales = self.acq_timebase_scales[:count]
        cycle_numbers = self.acq_cycle_numbers[:count]
        point_times = self.acq_point_times[:count]
        instrument_times = self.acq_instrument_times[:count]
        sweep_elapsed_times = self.acq_sweep_elapsed_times[:count]
        capture_epoch_times = self.acq_capture_epoch_times[:count]
        sample_epoch_times = self.acq_sample_epoch_times[:count]
        sample_intervals = self.acq_sample_intervals[:count]
        self.set_status(f"正在保存 {count:,} 个 {channel_label} 同帧采样点…")

        def worker():
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(self._acquisition_csv_header(channels))
                    for index in range(count):
                        frequency = frequencies[index]
                        capture_stamp = datetime.fromtimestamp(
                            capture_epoch_times[index], timezone.utc
                        ).isoformat(timespec="milliseconds")
                        estimated_stamp = datetime.fromtimestamp(
                            sample_epoch_times[index], timezone.utc
                        ).isoformat(timespec="milliseconds")
                        writer.writerow([
                            index,
                            estimated_stamp,
                            capture_stamp,
                            f"{sweep_elapsed_times[index]:.9f}",
                            sweep_points[index],
                            f"{requested_frequencies[index]:.12g}",
                            f"{frequency:.12g}",
                            f"{requested_amplitudes[index]:.12g}",
                            f"{amplitudes[index]:.12g}",
                            f"{requested_amplitudes[index] / 2.0:.12g}",
                            f"{amplitudes[index] / 2.0:.12g}",
                            f"{requested_offsets[index]:.12g}",
                            f"{offsets[index]:.12g}",
                            f"{requested_phases[index]:.12g}",
                            f"{phases[index]:.12g}",
                            f"{timebase_scales[index]:.12g}",
                            f"{1.0 / frequency:.12g}",
                            cycle_numbers[index],
                            f"{point_times[index]:.9f}",
                            f"{instrument_times[index]:.12g}",
                            f"{sample_intervals[index]:.12g}",
                            *(
                                f"{values[channel][index]:.12g}"
                                for channel in channels
                            ),
                        ])
            except Exception as exc:
                message = "保存失败：" + str(exc)
                self.after(0, lambda text=message: self.set_status(text))
                return
            self.after(0, lambda: self.set_status(f"数据已保存：{path}"))

        threading.Thread(target=worker, daemon=True).start()

    def generate_bode_from_current(self):
        reference_channel = int(self.bode_reference_channel.get())
        response_channel = int(self.bode_response_channel.get())
        if reference_channel == response_channel:
            self.set_status("伯德图参考通道和响应通道不能相同。")
            return
        if (
            reference_channel not in self.acq_values
            or response_channel not in self.acq_values
        ):
            self.set_status(
                f"当前数据不同时包含 CH{reference_channel} 和 CH{response_channel}。"
            )
            return
        count = min(
            len(self.acq_frequencies),
            len(self.acq_point_times),
            len(self.acq_values[reference_channel]),
            len(self.acq_values[response_channel]),
        )
        if count < 8:
            self.set_status("当前参考/响应通道数据不足，无法生成伯德图。")
            return
        frequencies = self.acq_frequencies[:count]
        times = self.acq_point_times[:count]
        reference_values = self.acq_values[reference_channel][:count]
        response_values = self.acq_values[response_channel][:count]
        groups: dict[float, tuple[list[float], list[float], list[float]]] = {}
        for frequency, point_time, reference, response in zip(
            frequencies, times, reference_values, response_values
        ):
            if frequency not in groups:
                groups[frequency] = ([], [], [])
            group_times, group_reference, group_response = groups[frequency]
            group_times.append(point_time)
            group_reference.append(reference)
            group_response.append(response)
        self._calculate_bode_in_background(
            groups,
            "当前采集数据",
            [],
            reference_channel,
            response_channel,
        )

    def load_bode_csv(self):
        reference_channel = int(self.bode_reference_channel.get())
        response_channel = int(self.bode_response_channel.get())
        if reference_channel == response_channel:
            self.set_status("伯德图参考通道和响应通道不能相同。")
            return
        path = filedialog.askopenfilename(
            title=(
                f"加载包含频率、CH{reference_channel}、"
                f"CH{response_channel} 的 CSV"
            ),
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.set_status("正在读取 CSV 并计算伯德图…")

        def worker():
            try:
                groups, assumptions = load_waveform_groups(
                    path, reference_channel, response_channel
                )
                points, warnings = calculate_bode(groups)
            except Exception as exc:
                message = "伯德图生成失败：" + str(exc)
                self.after(0, lambda text=message: self.set_status(text))
                return
            self.after(
                0,
                lambda result=points, notes=assumptions + warnings, source=path:
                    self._apply_bode_results(
                        result,
                        source,
                        notes,
                        reference_channel,
                        response_channel,
                    ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _calculate_bode_in_background(
        self,
        groups,
        source: str,
        notes: list[str],
        reference_channel: int,
        response_channel: int,
    ):
        self.set_status(
            f"正在拟合 CH{response_channel}/CH{reference_channel} 并计算伯德图…"
        )

        def worker():
            try:
                points, warnings = calculate_bode(groups)
            except Exception as exc:
                message = "伯德图生成失败：" + str(exc)
                self.after(0, lambda text=message: self.set_status(text))
                return
            self.after(
                0,
                lambda result=points, messages=notes + warnings:
                    self._apply_bode_results(
                        result,
                        source,
                        messages,
                        reference_channel,
                        response_channel,
                    ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _apply_bode_results(
        self,
        points: list[BodePoint],
        source: str,
        notes: list[str],
        reference_channel: int,
        response_channel: int,
    ):
        self.bode_points = points
        self.bode_source = source
        self.bode_result_reference_channel = reference_channel
        self.bode_result_response_channel = response_channel
        self.save_bode_data_button.configure(state="normal")
        if self.bode_figure is not None:
            self.save_bode_image_button.configure(state="normal")
        minimum_r2 = min(
            min(point.reference_r2, point.response_r2) for point in self.bode_points
        )
        note_text = f" | 提示 {len(notes)} 条" if notes else ""
        self.bode_text.set(
            f"已生成 {len(points)} 个频点 | CH{response_channel}/"
            f"CH{reference_channel}（响应/参考）| "
            f"最低拟合 R²={minimum_r2:.4f}{note_text} | 来源：{source}"
        )
        self.draw_bode_plot()
        status = f"伯德图已生成：{len(points)} 个频点。"
        if notes:
            status += " " + "；".join(notes[:3])
            if len(notes) > 3:
                status += f"；其他 {len(notes) - 3} 条提示"
        self.set_status(status)

    def draw_bode_plot(self):
        if self.bode_figure is None or self.bode_canvas is None:
            return
        magnitude = self.bode_magnitude_axis
        phase = self.bode_phase_axis
        magnitude.clear()
        phase.clear()
        magnitude.set_ylabel("Magnitude (dB)")
        phase.set_ylabel("Phase (deg)")
        phase.set_xlabel("Frequency (Hz)")
        magnitude.grid(True, which="both", linestyle=":", alpha=0.65)
        phase.grid(True, which="both", linestyle=":", alpha=0.65)
        magnitude.axhline(0.0, color="#777777", linewidth=0.8)
        phase.axhline(0.0, color="#777777", linewidth=0.8)
        if self.bode_points:
            frequencies = [point.frequency_hz for point in self.bode_points]
            gains = [point.gain_db for point in self.bode_points]
            phases = [point.phase_deg for point in self.bode_points]
            magnitude.semilogx(
                frequencies, gains, color="#1565c0", marker="o", markersize=3.5,
                linewidth=1.5,
                label=(
                    f"CH{self.bode_result_response_channel} / "
                    f"CH{self.bode_result_reference_channel}"
                ),
            )
            phase.semilogx(
                frequencies, phases, color="#c62828", marker="o", markersize=3.5,
                linewidth=1.5,
            )
            magnitude.legend(loc="best")
            if len(frequencies) == 1:
                frequency = frequencies[0]
                phase.set_xlim(frequency / 1.25, frequency * 1.25)
        else:
            magnitude.text(
                0.5, 0.5, "Acquire data or load a waveform CSV",
                transform=magnitude.transAxes, ha="center", va="center", color="#666666",
            )
        magnitude.tick_params(labelbottom=False)
        self.bode_canvas.draw_idle()

    def save_bode_data(self):
        if not self.bode_points:
            self.set_status("尚无伯德图数据。")
            return
        path = filedialog.asksaveasfilename(
            title="保存伯德图数据",
            defaultextension=".csv",
            initialfile="MHO98_Bode_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.writer(stream)
                writer.writerow([
                    "frequency_hz", "reference_channel", "response_channel",
                    "reference_amplitude_v", "response_amplitude_v",
                    "gain_ratio", "gain_db", "phase_deg", "phase_wrapped_deg",
                    "reference_fit_r2", "response_fit_r2", "sample_count",
                ])
                for point in self.bode_points:
                    writer.writerow([
                        f"{point.frequency_hz:.12g}",
                        self.bode_result_reference_channel,
                        self.bode_result_response_channel,
                        f"{point.reference_amplitude_v:.12g}",
                        f"{point.response_amplitude_v:.12g}",
                        f"{point.gain_ratio:.12g}",
                        f"{point.gain_db:.12g}",
                        f"{point.phase_deg:.12g}",
                        f"{point.phase_wrapped_deg:.12g}",
                        f"{point.reference_r2:.12g}",
                        f"{point.response_r2:.12g}",
                        point.sample_count,
                    ])
        except Exception as exc:
            self.set_status("伯德数据保存失败：" + str(exc))
            return
        self.set_status("伯德数据已保存：" + path)

    def save_bode_image(self):
        if not self.bode_points or self.bode_figure is None:
            self.set_status("尚无可保存的伯德图。")
            return
        path = filedialog.asksaveasfilename(
            title="保存伯德图",
            defaultextension=".png",
            initialfile="MHO98_Bode_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".png",
            filetypes=[("PNG 图片", "*.png"), ("SVG 矢量图", "*.svg"), ("PDF 文件", "*.pdf")],
        )
        if not path:
            return
        try:
            self.bode_figure.savefig(path, dpi=180, bbox_inches="tight")
        except Exception as exc:
            self.set_status("伯德图保存失败：" + str(exc))
            return
        self.set_status("伯德图已保存：" + path)

    def update_wave_preview(
        self,
        frequency: float,
        amplitude: float,
        offset: float = 0.0,
        point: int | None = None,
        capture_cycles: int | None = None,
    ):
        """Draw the requested number of saved periods."""
        if capture_cycles is None:
            try:
                capture_cycles = int(self.capture_cycles.get())
            except (ValueError, tk.TclError):
                capture_cycles = 1
        capture_cycles = max(1, capture_cycles)
        canvas = self.preview_canvas
        canvas.delete("all")
        width = int(canvas["width"])
        height = int(canvas["height"])
        left, right, top, bottom = 48, 12, 18, 30
        plot_width = width - left - right
        plot_height = height - top - bottom
        center_y = top + plot_height / 2
        time_window = cycle_duration(frequency, capture_cycles)

        vertical_divisions = min(capture_cycles, 10)
        for division in range(vertical_divisions + 1):
            ratio = division / vertical_divisions
            x = left + plot_width * ratio
            canvas.create_line(x, top, x, top + plot_height, fill="#1e3a4d", dash=(2, 4))
            label_time = ratio * time_window
            canvas.create_text(x, height - 14, text=f"{label_time:.3g}s", fill="#91a9b8", font=("Segoe UI", 8))
        for division in range(5):
            y = top + plot_height * division / 4
            canvas.create_line(left, y, width - right, y, fill="#1e3a4d", dash=(2, 4))
        canvas.create_line(left, center_y, width - right, center_y, fill="#6b8291")
        high_level = offset + amplitude / 2.0
        low_level = offset - amplitude / 2.0
        canvas.create_text(7, top + 3, text=f"{high_level:+.3g}V", anchor="w", fill="#91a9b8", font=("Segoe UI", 8))
        canvas.create_text(7, center_y, text=f"B={offset:+.3g}V", anchor="w", fill="#91a9b8", font=("Segoe UI", 8))
        canvas.create_text(7, top + plot_height - 3, text=f"{low_level:+.3g}V", anchor="w", fill="#91a9b8", font=("Segoe UI", 8))

        points: list[float] = []
        sample_count = min(5000, max(1200, capture_cycles * 80))
        vertical_scale = plot_height * 0.43
        for index in range(sample_count + 1):
            ratio = index / sample_count
            time_value = ratio * time_window
            x = left + ratio * plot_width
            y = center_y - math.sin(2 * math.pi * frequency * time_value) * vertical_scale
            points.extend((x, y))
        canvas.create_line(*points, fill="#25e6a4", width=2, smooth=False)
        canvas.create_rectangle(left, top, width - right, top + plot_height, outline="#506070")

        progress = (
            f" | 扫频点 {point}/{len(self.sweep_frequencies)}" if point else ""
        )
        self.preview_text.set(
            f"{frequency:.6g} Hz | B + A·sin(2πft)："
            f"A={amplitude / 2.0:.6g} V peak，B={offset:.6g} V | "
            f"保存 {capture_cycles} 周期 / {time_window:.6g} s{progress}"
        )

    def quit_app(self):
        self.closing = True
        self.acq_running = False
        self.acq_generation += 1
        if self.acq_after_id is not None:
            self.after_cancel(self.acq_after_id)
        if self.acq_stream is not None:
            try:
                self.acq_stream.flush()
                self.acq_stream.close()
            except Exception:
                pass
            self.acq_stream = None
            self.acq_writer = None
        if self.sweep_running:
            self.sweep_running = False
            if self.sweep_after_id is not None:
                self.after_cancel(self.sweep_after_id)
            try:
                self.inst.finish_sweep(self.sweep_channel)
            except Exception:
                pass
        self.inst.close(); self.destroy()


if __name__ == "__main__":
    App().mainloop()
