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
from datetime import datetime, timedelta
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
    ch1_amplitude_v: float
    ch2_amplitude_v: float
    gain_ratio: float
    gain_db: float
    phase_deg: float
    phase_wrapped_deg: float
    ch1_r2: float
    ch2_r2: float
    sample_count: int


def _fit_sine_at_frequency(times, values, frequency: float):
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
    """Calculate CH2/CH1 magnitude and phase for each known excitation frequency."""
    if np is None:
        raise RuntimeError("缺少 NumPy/Matplotlib：" + _PLOT_IMPORT_ERROR)
    points: list[BodePoint] = []
    warnings: list[str] = []
    for frequency in sorted(groups):
        times, ch1, ch2 = groups[frequency]
        count = min(len(times), len(ch1), len(ch2))
        if count < 8:
            warnings.append(f"{frequency:.12g} Hz：有效点数不足，已跳过")
            continue
        try:
            amplitude1, phase1, r2_1 = _fit_sine_at_frequency(
                times[:count], ch1[:count], frequency
            )
            amplitude2, phase2, r2_2 = _fit_sine_at_frequency(
                times[:count], ch2[:count], frequency
            )
        except Exception as exc:
            warnings.append(f"{frequency:.12g} Hz：{exc}")
            continue
        if amplitude1 <= 1e-15:
            warnings.append(f"{frequency:.12g} Hz：CH1 输入幅值接近零，已跳过")
            continue
        ratio = amplitude2 / amplitude1
        wrapped_phase = (phase2 - phase1 + 180.0) % 360.0 - 180.0
        points.append(
            BodePoint(
                frequency_hz=frequency,
                ch1_amplitude_v=amplitude1,
                ch2_amplitude_v=amplitude2,
                gain_ratio=ratio,
                gain_db=20.0 * math.log10(max(ratio, 1e-300)),
                phase_deg=wrapped_phase,
                phase_wrapped_deg=wrapped_phase,
                ch1_r2=r2_1,
                ch2_r2=r2_2,
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


def load_waveform_groups(path: str):
    """Load current-app CSV files and common frequency/CH1/CH2 CSV variants."""
    aliases = {
        "frequency": ("frequencyhz", "frequency", "freqhz", "freq", "频率hz", "频率"),
        "ch1": ("ch1voltagev", "ch1voltage", "ch1v", "ch1", "通道1电压", "通道1"),
        "ch2": ("ch2voltagev", "ch2voltage", "ch2v", "ch2", "通道2电压", "通道2"),
        "time": ("pointtimes", "times", "time", "storedtimes", "时间s", "时间"),
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
        ch1_column = locate("ch1")
        ch2_column = locate("ch2")
        time_column = locate("time", False)
        interval_column = locate("interval", False)
        raw_groups: dict[float, list[tuple[float | None, float | None, float, float]]] = {}
        skipped = 0
        for row in reader:
            try:
                frequency = float(row[frequency_column])
                value1 = float(row[ch1_column])
                value2 = float(row[ch2_column])
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
                (time_value, interval, value1, value2)
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
            # Compatibility path for simple files containing only f, CH1 and CH2.
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
        raise ValueError("CSV 中没有有效的频率、CH1、CH2 数值行。")
    if skipped:
        assumptions.append(f"已跳过 {skipped} 行无效数据")
    return groups, assumptions


def _linear_segment(
    start: float, stop: float, count: int, include_stop: bool
) -> list[float]:
    if count < 2 or stop <= start:
        raise ValueError("扫频分段参数无效。")
    divisor = count - 1 if include_stop else count
    step = (stop - start) / divisor
    values = [start + index * step for index in range(count)]
    if include_stop:
        values[-1] = stop
    return values


def make_sweep_frequencies() -> list[float]:
    """Return 300 unique points across three piecewise-linear bands."""
    return (
        _linear_segment(0.1, 10.0, 100, include_stop=False)
        + _linear_segment(10.0, 100.0, 100, include_stop=False)
        + _linear_segment(100.0, 500.0, 100, include_stop=True)
    )


def five_cycle_duration(frequency: float) -> float:
    if frequency <= 0:
        raise ValueError("频率必须大于 0。")
    return 5.0 / frequency


def sweep_timebase_preset(frequency: float) -> tuple[str, float]:
    """Return the fixed timebase used for each of the three sweep bands."""
    if frequency <= 0:
        raise ValueError("频率必须大于 0。")
    if frequency < 10.0:
        return "0.1–<10 Hz", 5.0
    if frequency < 100.0:
        return "10–<100 Hz", 0.05
    return "100–500 Hz", 0.005


def extract_middle_three_cycles(
    ch1: list[float],
    ch2: list[float],
    ch1_x_increment: float,
    ch2_x_increment: float,
    frequency: float,
) -> tuple[list[float], list[float], list[float], float]:
    """Keep raw samples from cycles 2-4 of the newest five-cycle window."""
    if len(ch1) < 2 or len(ch2) < 2:
        raise ValueError("示波器返回的波形点数不足。")
    if ch1_x_increment <= 0 or ch2_x_increment <= 0 or frequency <= 0:
        raise ValueError("波形时间间隔或频率无效。")
    relative_increment_error = abs(ch1_x_increment - ch2_x_increment) / max(
        ch1_x_increment, ch2_x_increment
    )
    if relative_increment_error > 0.001:
        raise ValueError("CH1/CH2 的 XINCrement 不一致，无法在不插值的情况下对齐。")

    period = 1.0 / frequency
    required_duration = 5.0 * period
    # Capture starts after five real-time periods.  Because cycle 1 is
    # intentionally discarded, the screen only has to contain the latest four
    # periods to preserve cycles 2-4 completely.  This also tolerates the
    # MHO98 screen returning e.g. 49.6 s for a nominal 50 s requirement.
    minimum_visible_duration = 4.0 * period

    def extract(values: list[float], increment: float):
        source_duration = (len(values) - 1) * increment
        if source_duration + increment < minimum_visible_duration:
            raise ValueError(
                f"当前屏幕波形覆盖 {source_duration:.6g} s，"
                f"完整保留第 2–4 周期至少需要覆盖 {minimum_visible_duration:.6g} s。"
                "当前频段的预设时基未得到足够屏幕数据，请检查示波器时基模式。"
            )
        five_cycle_start = source_duration - required_duration
        keep_start = five_cycle_start + period
        keep_stop = five_cycle_start + 4.0 * period
        start_index = max(0, math.ceil((keep_start - 1e-12) / increment))
        stop_index = min(len(values), math.ceil((keep_stop - 1e-12) / increment))
        selected = values[start_index:stop_index]
        point_times = [
            (start_index + index) * increment - five_cycle_start
            for index in range(len(selected))
        ]
        return selected, point_times

    values1, times1 = extract(ch1, ch1_x_increment)
    values2, _times2 = extract(ch2, ch2_x_increment)
    count = min(len(values1), len(values2), len(times1))
    if count < 2:
        recommended_scale = 1.0 / frequency
        raise ValueError(
            "中间 3 个周期内的原始波形点数不足。"
            f"当前点间隔约 {ch1_x_increment:.6g} s；"
            f"当前频段预设未生效；建议时基约 {recommended_scale:.6g} s/div 或更小。"
        )
    return values1[:count], values2[:count], times1[:count], ch1_x_increment


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
        self.sweep_active_timebase_band: str | None = None

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
            self.sweep_active_timebase_band = None

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
                    self.close()
                    attempts.append(f"{port}: 连接成功但未返回 RIGOL 标识 ({self.idn!r})")
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

    def configure_sweep_point(
        self, channel: int, frequency: float, amplitude: float, is_current
    ) -> tuple[float, str] | None:
        """Configure one AFG point and switch timebase only at band boundaries."""
        with self.lock:
            if not is_current():
                return None
            band_name, requested_timebase = sweep_timebase_preset(frequency)
            if self.sweep_original_timebase_scale is None:
                self.sweep_original_timebase_scale = float(
                    self._query_unlocked(":TIMebase:MAIN:SCALe?")
                )
            if self.sweep_active_timebase_band != band_name:
                self._write_unlocked(
                    f":TIMebase:MAIN:SCALe {requested_timebase:.12g}"
                )
                actual_timebase = float(
                    self._query_unlocked(":TIMebase:MAIN:SCALe?")
                )
                self.sweep_active_timebase_band = band_name
            else:
                actual_timebase = float(
                    self._query_unlocked(":TIMebase:MAIN:SCALe?")
                )
            self._write_unlocked(f":SOURce{channel}:FUNCtion SINusoid")
            self._write_unlocked(f":SOURce{channel}:FREQuency {frequency:.12g}")
            self._write_unlocked(f":SOURce{channel}:VOLTage:AMPLitude {amplitude:.12g}")
            self._write_unlocked(f":SOURce{channel}:VOLTage:OFFSet 0")
            self._write_unlocked(f":SOURce{channel}:OUTPut:STATe ON")
            # Synchronize the AFG commands and report the scale accepted by the
            # oscilloscope.  No scale write occurs again until the next band.
            actual_timebase = float(
                self._query_unlocked(":TIMebase:MAIN:SCALe?")
            )
            return actual_timebase, band_name

    def finish_sweep(self, channel: int) -> float | None:
        """Disable AFG output and restore the pre-sweep timebase."""
        with self.lock:
            self._write_unlocked(f":SOURce{channel}:OUTPut:STATe OFF")
            original = self.sweep_original_timebase_scale
            restored = None
            if original is not None:
                self._write_unlocked(f":TIMebase:MAIN:SCALe {original:.12g}")
                restored = float(
                    self._query_unlocked(":TIMebase:MAIN:SCALe?")
                )
            self.sweep_original_timebase_scale = None
            self.sweep_active_timebase_band = None
            return restored

    def acquire_dual_frame(self) -> tuple[list[float], float, list[float], float]:
        """Read CH1/CH2 screen data without STOP/RUN or acquisition changes."""
        with self.lock:
            # Keep waveform-transfer configuration in the same serialized
            # transaction as the read.  Recording startup must never wait on
            # USB just to become "ready".
            self._write_unlocked(":WAVeform:MODE NORMal")
            self._write_unlocked(":WAVeform:FORMat ASCii")
            self._write_unlocked(":WAVeform:STARt 1")
            self._write_unlocked(":WAVeform:STOP 1000")
            self._write_unlocked(":WAVeform:POINts 1000")
            frames = []
            for channel in (1, 2):
                display_state = self._query_unlocked(
                    f":CHANnel{channel}:DISPlay?"
                ).strip()
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
                        f"波形源切换失败：请求 CH{channel}，仪器返回 {selected_source!r}。"
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
                increment = float(preamble[4])
                if waveform_format != 2 or waveform_mode != 0:
                    raise RuntimeError(
                        f"CH{channel} 波形格式异常：format={waveform_format}, "
                        f"mode={waveform_mode}；期望 ASCii/NORMal。"
                    )
                text = self._query_unlocked(":WAVeform:DATA?")
                values = [float(item) for item in text.strip().split(",") if item.strip()]
                if expected_points > 0 and len(values) != expected_points:
                    raise RuntimeError(
                        f"CH{channel} 波形点数不完整：前导参数为 {expected_points} 点，"
                        f"实际收到 {len(values)} 点。"
                    )
                frames.append((values, increment))
            return frames[0][0], frames[0][1], frames[1][0], frames[1][1]


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RIGOL MHO98 正弦波上位机")
        self.resizable(True, True)
        self.inst = Instrument()
        self.status = tk.StringVar(value="未连接 - 请连接后面板 USB Device 或配置 LAN")
        self.ip = tk.StringVar(value="192.168.1.100")
        self.resource = tk.StringVar()
        self.native_resources: dict[str, str] = {}
        self.ch = tk.IntVar(value=1)
        self.freq = tk.DoubleVar(value=1000.0)
        self.amp = tk.DoubleVar(value=2.0)
        self.output = tk.BooleanVar(value=False)
        self.sweep_running = False
        self.sweep_generation = 0
        self.sweep_index = 0
        self.sweep_after_id = None
        self.active_sweep_point: int | None = None
        self.active_sweep_frequency: float | None = None
        self.sweep_point_started_at: float | None = None
        self.sweep_frequencies = make_sweep_frequencies()
        self.acq_running = False
        self.acq_prepared = False
        self.acq_generation = 0
        self.acq_after_id = None
        self.acq_ch1 = array("d")
        self.acq_ch2 = array("d")
        self.acq_sweep_points = array("H")
        self.acq_frequencies = array("d")
        self.acq_cycle_numbers = array("B")
        self.acq_point_times = array("d")
        self.acq_stored_times = array("d")
        self.acq_sample_intervals = array("d")
        self.acq_total_stored_seconds = 0.0
        self.acq_started_at: datetime | None = None
        self.acq_file_path: str | None = None
        self.acq_stream = None
        self.acq_writer = None
        self.bode_points: list[BodePoint] = []
        self.bode_source = ""
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
        self.apply_button = ttk.Button(f, text="应用正弦波参数", command=self.apply)
        self.apply_button.grid(column=0, row=9, columnspan=2, sticky="ew", **pad)
        self.output_check = ttk.Checkbutton(f, text="启用输出", variable=self.output, command=self.set_output)
        self.output_check.grid(column=2, row=9, columnspan=2, sticky="w", **pad)
        ttk.Separator(f).grid(column=0, row=10, columnspan=4, sticky="ew", pady=8)
        self.sweep_button = ttk.Button(f, text="开始扫频（0.1–500 Hz）", command=self.toggle_sweep)
        self.sweep_button.grid(column=0, row=11, columnspan=2, sticky="ew", **pad)
        ttk.Label(f, text="三段固定时基：5 s/div、50 ms/div、5 ms/div；仅在 10 Hz 和 100 Hz 边界切换。", wraplength=300).grid(column=2, row=11, columnspan=2, sticky="w", **pad)
        ttk.Label(f, text="范围：2 mHz–100 MHz；幅度 2 mVpp–20 Vpp。高于 50 MHz 时最大 10 Vpp；50 Ω 负载时可用幅度会更低。", wraplength=510).grid(column=0, row=12, columnspan=4, sticky="w", **pad)
        preview = ttk.LabelFrame(f, text="正弦波扫频预览（设定值，非实际采样）", padding=8)
        preview.grid(column=0, row=13, columnspan=4, sticky="ew", padx=8, pady=(10, 5))
        self.preview_text = tk.StringVar(value="0.1000 Hz | 2 Vpp | 5 周期 / 50 s")
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
        acquisition = ttk.LabelFrame(f, text="CH1 / CH2 扫频数据（每点仅保存第 2–4 周期）", padding=8)
        acquisition.grid(column=0, row=14, columnspan=4, sticky="ew", padx=8, pady=(8, 5))
        self.acq_button = ttk.Button(acquisition, text="开始记录", command=self.toggle_acquisition)
        self.acq_button.grid(column=0, row=0, sticky="ew", padx=(0, 6))
        self.save_button = ttk.Button(acquisition, text="导出 CSV 副本…", command=self.save_acquisition, state="disabled")
        self.save_button.grid(column=1, row=0, sticky="ew", padx=6)
        self.clear_button = ttk.Button(acquisition, text="清空数据", command=self.clear_acquisition)
        self.clear_button.grid(column=2, row=0, sticky="ew", padx=6)
        self.acq_text = tk.StringVar(
            value="尚未记录 | 每频点采集 5 周期，丢弃第 1 和第 5 周期"
        )
        ttk.Label(acquisition, textvariable=self.acq_text, wraplength=565).grid(
            column=0, row=1, columnspan=3, sticky="w", pady=(7, 4)
        )
        bode = ttk.LabelFrame(f, text="伯德图（CH1 输入 → CH2 输出）", padding=8)
        bode.grid(column=0, row=15, columnspan=4, sticky="ew", padx=8, pady=(8, 5))
        ttk.Button(
            bode, text="用当前采集数据绘制", command=self.generate_bode_from_current
        ).grid(column=0, row=0, sticky="ew", padx=(0, 5))
        ttk.Button(
            bode, text="加载波形 CSV…", command=self.load_bode_csv
        ).grid(column=1, row=0, sticky="ew", padx=5)
        self.save_bode_data_button = ttk.Button(
            bode, text="保存伯德数据…", command=self.save_bode_data, state="disabled"
        )
        self.save_bode_data_button.grid(column=2, row=0, sticky="ew", padx=5)
        self.save_bode_image_button = ttk.Button(
            bode, text="保存伯德图…", command=self.save_bode_image, state="disabled"
        )
        self.save_bode_image_button.grid(column=3, row=0, sticky="ew", padx=(5, 0))
        self.bode_text = tk.StringVar(
            value="尚未生成 | 幅频=20log10(CH2/CH1)，相频=φCH2-φCH1"
        )
        ttk.Label(bode, textvariable=self.bode_text, wraplength=680).grid(
            column=0, row=1, columnspan=4, sticky="w", pady=(7, 4)
        )
        if Figure is not None and FigureCanvasTkAgg is not None:
            self.bode_figure = Figure(figsize=(6.6, 4.6), dpi=100, constrained_layout=True)
            self.bode_magnitude_axis = self.bode_figure.add_subplot(211)
            self.bode_phase_axis = self.bode_figure.add_subplot(
                212, sharex=self.bode_magnitude_axis
            )
            self.bode_canvas = FigureCanvasTkAgg(self.bode_figure, master=bode)
            self.bode_canvas.get_tk_widget().grid(
                column=0, row=2, columnspan=4, sticky="ew", pady=(4, 0)
            )
            self.draw_bode_plot()
        else:
            ttk.Label(
                bode,
                text="无法加载 Matplotlib：" + _PLOT_IMPORT_ERROR,
                foreground="#a00000",
                wraplength=650,
            ).grid(column=0, row=2, columnspan=4, sticky="w")
        self.after_id = None
        self.update_wave_preview(0.1, self.amp.get(), 0)

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
        if self.sweep_running:
            self.set_status("扫频正在运行；请先停止扫频再手动设置参数。")
            return
        if not self.inst.connected: self.set_status("未连接：参数未发送。"); return
        try:
            freq, amp = float(self.freq.get()), float(self.amp.get())
            if not 0.002 <= freq <= 100_000_000: raise ValueError("频率必须在 0.002–100000000 Hz")
            max_amp = 10 if freq > 50_000_000 else 20
            if not 0.002 <= amp <= max_amp: raise ValueError(f"当前频率下幅度必须在 0.002–{max_amp} Vpp")
        except ValueError as exc:
            self.set_status("参数错误：" + str(exc)); return
        self.update_wave_preview(freq, amp)
        channel = self.ch.get()
        self.background(lambda: self._apply_scpi(channel, freq, amp))

    def _apply_scpi(self, channel: int, freq: float, amp: float) -> str:
        self.inst.write(f":SOURce{channel}:FUNCtion SINusoid")
        self.inst.write(f":SOURce{channel}:FREQuency {freq:.12g}")
        self.inst.write(f":SOURce{channel}:VOLTage:AMPLitude {amp:.12g}")
        self.inst.write(f":SOURce{channel}:VOLTage:OFFSet 0")
        err = self.inst.query(":SYSTem:ERRor?")
        if not err.startswith("0"): raise RuntimeError("仪器返回：" + err)
        return f"已线性设置 GI/GII {channel}：{freq:.12g} Hz，{amp:.12g} Vpp"

    def set_output(self):
        if not self.inst.connected: self.output.set(False); self.set_status("未连接：无法切换输出。"); return
        ch, on = self.ch.get(), self.output.get()
        self.background(lambda: self._out_scpi(ch, on))

    def _out_scpi(self, channel: int, on: bool) -> str:
        self.inst.write(f":SOURce{channel}:OUTPut:STATe {'ON' if on else 'OFF'}")
        return f"GI/GII {channel} 输出已{'开启' if on else '关闭'}"

    def toggle_sweep(self):
        if self.sweep_running:
            self.stop_sweep("用户停止扫频")
        else:
            self.start_sweep()

    def start_sweep(self):
        if not self.inst.connected:
            self.set_status("未连接：请先连接 MHO98，再开始扫频。")
            return
        try:
            amplitude = float(self.amp.get())
            if not 0.002 <= amplitude <= 20:
                raise ValueError("幅度必须在 0.002–20 Vpp")
        except ValueError as exc:
            self.set_status("扫频参数错误：" + str(exc))
            return

        if not self.acq_running:
            if not self.start_acquisition():
                return
        self.sweep_running = True
        self.sweep_generation += 1
        self.sweep_index = 0
        self.active_sweep_point = None
        self.active_sweep_frequency = None
        self.sweep_point_started_at = None
        self.sweep_channel = self.ch.get()
        self.sweep_amplitude = amplitude
        self.sweep_button.configure(text="停止扫频")
        self.apply_button.configure(state="disabled")
        self.ch1_button.configure(state="disabled")
        self.ch2_button.configure(state="disabled")
        self.output_check.configure(state="disabled")
        self.output.set(True)
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
        frequency = self.sweep_frequencies[self.sweep_index]
        channel = self.sweep_channel
        amplitude = self.sweep_amplitude
        generation = self.sweep_generation
        self.freq.set(frequency)
        self.update_wave_preview(frequency, amplitude, point_number)
        self.set_status(
            f"正在设置扫频点 {point_number}/{len(self.sweep_frequencies)}："
            f"{frequency:.6g} Hz，{amplitude:.6g} Vpp"
        )

        def worker():
            try:
                timebase_result = self.inst.configure_sweep_point(
                    channel,
                    frequency,
                    amplitude,
                    lambda: self.sweep_running and self.sweep_generation == generation,
                )
            except Exception as exc:
                message = "扫频失败：" + str(exc)
                self.after(0, lambda text=message: self.stop_sweep(text))
                return
            if timebase_result is None:
                return
            actual_timebase, band_name = timebase_result
            self.after(
                0,
                lambda index=point_number, value=frequency, token=generation,
                       scale=actual_timebase, band=band_name:
                    self._sweep_point_started(index, value, token, scale, band),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _sweep_point_started(
        self,
        point_number: int,
        frequency: float,
        generation: int,
        timebase_scale: float,
        timebase_band: str,
    ):
        if (
            not self.sweep_running
            or generation != self.sweep_generation
            or point_number != self.sweep_index + 1
        ):
            return
        period = 1.0 / frequency
        five_cycles = five_cycle_duration(frequency)
        remaining = sum(
            five_cycle_duration(value)
            for value in self.sweep_frequencies[point_number - 1:]
        )
        self.set_status(
            f"扫频点 {point_number}/{len(self.sweep_frequencies)}：{frequency:.6g} Hz | "
            f"周期 {period:.6g} s | 5 周期 {five_cycles:.6g} s；"
            f"{timebase_band} 段时基 {timebase_scale:.6g} s/div；"
            f"理论剩余 {int(remaining) // 60} 分 {int(remaining) % 60} 秒"
        )
        self.sweep_index += 1
        self.sweep_progress["value"] = point_number
        self.active_sweep_point = point_number
        self.active_sweep_frequency = frequency
        self.sweep_point_started_at = time.monotonic()
        if self.acq_running and self.acq_prepared:
            self._schedule_sweep_capture(
                self.acq_generation,
                generation,
                point_number,
                frequency,
                self.sweep_point_started_at,
            )

    def stop_sweep(self, reason: str = "扫频已停止"):
        completed_normally = reason == "扫频完成"
        was_running = self.sweep_running
        self.sweep_running = False
        self.sweep_generation += 1
        self.active_sweep_point = None
        self.active_sweep_frequency = None
        self.sweep_point_started_at = None
        if self.sweep_after_id is not None:
            self.after_cancel(self.sweep_after_id)
            self.sweep_after_id = None
        self.sweep_button.configure(text="开始扫频（0.1–500 Hz）")
        self.apply_button.configure(state="normal")
        self.ch1_button.configure(state="normal")
        self.ch2_button.configure(state="normal")
        self.output_check.configure(state="normal")
        self.output.set(False)
        if self.acq_running:
            self.stop_acquisition(reason + "；CH1/CH2 电脑端记录已结束")
        self.set_status(reason + "；正在关闭输出。")
        if was_running and self.inst.connected:
            channel = self.sweep_channel
            self.background(lambda: self._stop_sweep_output(channel, reason))
        if completed_normally and self.acq_ch1:
            self.after(250, self.generate_bode_from_current)

    def _stop_sweep_output(self, channel: int, reason: str) -> str:
        restored_scale = self.inst.finish_sweep(channel)
        if restored_scale is None:
            return reason + "；输出已关闭。"
        return reason + f"；输出已关闭，原时基已恢复为 {restored_scale:.6g} s/div。"

    def toggle_acquisition(self):
        if self.acq_running:
            if self.sweep_running:
                self.stop_sweep("用户停止采集和扫频")
            else:
                self.stop_acquisition("用户停止采集")
        else:
            self.start_acquisition()

    def start_acquisition(self) -> bool:
        if not self.inst.connected:
            self.set_status("未连接：请先连接 MHO98，再开始 CH1/CH2 采集。")
            return False
        default_name = "MHO98_CH1_CH2_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
        path = filedialog.asksaveasfilename(
            title="选择 CH1/CH2 实时记录位置",
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
            writer.writerow([
                "sample_index", "stored_time_s", "stored_timestamp",
                "sweep_point", "frequency_hz", "period_s", "cycle_number",
                "point_time_s", "sample_interval_s",
                "ch1_voltage_v", "ch2_voltage_v",
            ])
            stream.flush()
        except Exception as exc:
            self.set_status("无法创建 CSV 文件：" + str(exc))
            return False
        self.acq_running = True
        self.acq_prepared = True
        self.acq_generation += 1
        self.acq_ch1 = array("d")
        self.acq_ch2 = array("d")
        self.acq_sweep_points = array("H")
        self.acq_frequencies = array("d")
        self.acq_cycle_numbers = array("B")
        self.acq_point_times = array("d")
        self.acq_stored_times = array("d")
        self.acq_sample_intervals = array("d")
        self.acq_total_stored_seconds = 0.0
        self.acq_started_at = datetime.now()
        self.acq_file_path = path
        self.acq_stream = stream
        self.acq_writer = writer
        self.acq_button.configure(text="停止记录（仅电脑端）")
        self.save_button.configure(state="disabled")
        self.acq_text.set(f"CSV 已创建，等待扫频：{path}")
        self.set_status("CH1/CH2 记录已就绪；数据将逐批写入 CSV。")
        if (
            self.sweep_running
            and self.active_sweep_point is not None
            and self.active_sweep_frequency is not None
            and self.sweep_point_started_at is not None
        ):
            self._schedule_sweep_capture(
                self.acq_generation,
                self.sweep_generation,
                self.active_sweep_point,
                self.active_sweep_frequency,
                self.sweep_point_started_at,
            )
        return True

    def _schedule_sweep_capture(
        self,
        generation: int,
        sweep_generation: int,
        point_number: int,
        frequency: float,
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
        point_duration = five_cycle_duration(frequency)
        delay_ms = max(1, int((point_duration - elapsed) * 1000))
        self.acq_after_id = self.after(
            delay_ms,
            lambda: self._request_acquisition_frame(
                generation,
                sweep_generation,
                point_number,
                frequency,
                point_started_at,
            ),
        )

    def _request_acquisition_frame(
        self,
        generation: int,
        sweep_generation: int,
        point_number: int,
        frequency: float,
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
                raw1, increment1, raw2, increment2 = self.inst.acquire_dual_frame()
                values1, values2, point_times, sample_interval = extract_middle_three_cycles(
                    raw1, raw2, increment1, increment2, frequency
                )
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
                point=point_number, hz=frequency, times=point_times,
                interval=sample_interval, a=values1, b=values2:
                    self._accept_acquisition_frame(
                        token, sweep_token, point, hz, times, interval, a, b
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
        frequency: float,
        point_times: list[float],
        sample_interval: float,
        ch1: list[float],
        ch2: list[float],
    ):
        if (
            not self.acq_running
            or generation != self.acq_generation
            or not self.sweep_running
            or sweep_generation != self.sweep_generation
            or point_number != self.active_sweep_point
        ):
            return
        period = 1.0 / frequency
        cycle_numbers = [
            min(4, max(2, math.floor((value + period * 1e-9) / period) + 1))
            for value in point_times
        ]
        stored_times = [
            self.acq_total_stored_seconds + index * sample_interval
            for index in range(len(ch1))
        ]
        try:
            self._append_live_csv(
                point_number,
                frequency,
                cycle_numbers,
                point_times,
                stored_times,
                sample_interval,
                ch1,
                ch2,
            )
        except Exception as exc:
            message = "CSV 写入失败：" + str(exc)
            self.stop_sweep(message)
            return
        self.acq_ch1.extend(ch1)
        self.acq_ch2.extend(ch2)
        self.acq_sweep_points.extend([point_number] * len(ch1))
        self.acq_frequencies.extend([frequency] * len(ch1))
        self.acq_cycle_numbers.extend(cycle_numbers)
        self.acq_point_times.extend(point_times)
        self.acq_stored_times.extend(stored_times)
        self.acq_sample_intervals.extend([sample_interval] * len(ch1))
        self.acq_total_stored_seconds += len(ch1) * sample_interval
        self.save_button.configure(state="normal")
        self.acq_text.set(
            f"扫频点 {point_number}/{len(self.sweep_frequencies)} | {frequency:.6g} Hz | "
            f"已保存周期 2–4：{len(ch1):,} 点 | "
            f"原始间隔 {sample_interval:.6g} s"
        )
        self.sweep_after_id = self.after(1, self._run_sweep_point)

    def _append_live_csv(
        self,
        point_number: int,
        frequency: float,
        cycle_numbers: list[int],
        point_times: list[float],
        stored_times: list[float],
        sample_interval: float,
        ch1: list[float],
        ch2: list[float],
    ):
        if self.acq_writer is None or self.acq_stream is None:
            raise RuntimeError("实时 CSV 文件未打开。")
        lengths = {len(ch1), len(ch2), len(cycle_numbers), len(point_times), len(stored_times)}
        if len(lengths) != 1:
            raise RuntimeError("CH1/CH2 或周期元数据的点数不一致。")
        base_index = len(self.acq_ch1)
        started = self.acq_started_at or datetime.now()
        period = 1.0 / frequency
        rows = zip(cycle_numbers, point_times, stored_times, ch1, ch2)
        for offset, (cycle, point_time, stored_elapsed, value1, value2) in enumerate(rows):
            index = base_index + offset
            stamp = started + timedelta(seconds=stored_elapsed)
            self.acq_writer.writerow([
                index,
                f"{stored_elapsed:.6f}",
                stamp.isoformat(timespec="milliseconds"),
                point_number,
                f"{frequency:.12g}",
                f"{period:.12g}",
                cycle,
                f"{point_time:.6f}",
                f"{sample_interval:.12g}",
                f"{value1:.12g}",
                f"{value2:.12g}",
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
        if self.acq_ch1:
            self.save_button.configure(state="normal")
            self.acq_text.set(
                f"{reason} | 共 {len(self.acq_ch1):,} 点 / "
                f"中间周期累计 {self.acq_total_stored_seconds:.6g} s | 已写入：{file_path}"
            )
        else:
            self.acq_text.set(reason + f" | CSV 已创建但尚无数据：{file_path}")
        self.set_status(reason)

    def clear_acquisition(self):
        if self.acq_running:
            self.set_status("请先停止采集，再清空数据。")
            return
        self.acq_ch1 = array("d")
        self.acq_ch2 = array("d")
        self.acq_sweep_points = array("H")
        self.acq_frequencies = array("d")
        self.acq_cycle_numbers = array("B")
        self.acq_point_times = array("d")
        self.acq_stored_times = array("d")
        self.acq_sample_intervals = array("d")
        self.acq_total_stored_seconds = 0.0
        self.acq_started_at = None
        self.acq_file_path = None
        self.save_button.configure(state="disabled")
        self.acq_text.set(
            "尚未记录 | 每频点采集 5 周期，丢弃第 1 和第 5 周期"
        )

    def save_acquisition(self):
        if not self.acq_ch1:
            self.set_status("没有可保存的采集数据。")
            return
        default_name = "MHO98_CH1_CH2_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
        path = filedialog.asksaveasfilename(
            title="保存 MHO98 CH1/CH2 采集数据",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        ch1 = self.acq_ch1[:]
        ch2 = self.acq_ch2[:]
        sweep_points = self.acq_sweep_points[:]
        frequencies = self.acq_frequencies[:]
        cycle_numbers = self.acq_cycle_numbers[:]
        point_times = self.acq_point_times[:]
        stored_times = self.acq_stored_times[:]
        sample_intervals = self.acq_sample_intervals[:]
        started = self.acq_started_at or datetime.now()
        self.set_status(f"正在保存 {len(ch1):,} 个双通道采样点…")

        def worker():
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as stream:
                    writer = csv.writer(stream)
                    writer.writerow([
                        "sample_index", "stored_time_s", "stored_timestamp",
                        "sweep_point", "frequency_hz", "period_s", "cycle_number",
                        "point_time_s", "sample_interval_s",
                        "ch1_voltage_v", "ch2_voltage_v",
                    ])
                    rows = zip(
                        sweep_points,
                        frequencies,
                        cycle_numbers,
                        point_times,
                        stored_times,
                        sample_intervals,
                        ch1,
                        ch2,
                    )
                    for index, (point, frequency, cycle, point_time, stored_time, interval, value1, value2) in enumerate(rows):
                        stamp = started + timedelta(seconds=stored_time)
                        writer.writerow([
                            index,
                            f"{stored_time:.9f}",
                            stamp.isoformat(timespec="milliseconds"),
                            point,
                            f"{frequency:.12g}",
                            f"{1.0 / frequency:.12g}",
                            cycle,
                            f"{point_time:.9f}",
                            f"{interval:.12g}",
                            f"{value1:.12g}",
                            f"{value2:.12g}",
                        ])
            except Exception as exc:
                message = "保存失败：" + str(exc)
                self.after(0, lambda text=message: self.set_status(text))
                return
            self.after(0, lambda: self.set_status(f"数据已保存：{path}"))

        threading.Thread(target=worker, daemon=True).start()

    def generate_bode_from_current(self):
        count = min(
            len(self.acq_frequencies),
            len(self.acq_point_times),
            len(self.acq_ch1),
            len(self.acq_ch2),
        )
        if count < 8:
            self.set_status("当前 CH1/CH2 数据不足，无法生成伯德图。")
            return
        frequencies = self.acq_frequencies[:count]
        times = self.acq_point_times[:count]
        ch1 = self.acq_ch1[:count]
        ch2 = self.acq_ch2[:count]
        groups: dict[float, tuple[list[float], list[float], list[float]]] = {}
        for frequency, point_time, value1, value2 in zip(frequencies, times, ch1, ch2):
            if frequency not in groups:
                groups[frequency] = ([], [], [])
            group_times, group_ch1, group_ch2 = groups[frequency]
            group_times.append(point_time)
            group_ch1.append(value1)
            group_ch2.append(value2)
        self._calculate_bode_in_background(groups, "当前采集数据", [])

    def load_bode_csv(self):
        path = filedialog.askopenfilename(
            title="加载包含频率、CH1、CH2 的 CSV",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.set_status("正在读取 CSV 并计算伯德图…")

        def worker():
            try:
                groups, assumptions = load_waveform_groups(path)
                points, warnings = calculate_bode(groups)
            except Exception as exc:
                message = "伯德图生成失败：" + str(exc)
                self.after(0, lambda text=message: self.set_status(text))
                return
            self.after(
                0,
                lambda result=points, notes=assumptions + warnings, source=path:
                    self._apply_bode_results(result, source, notes),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _calculate_bode_in_background(self, groups, source: str, notes: list[str]):
        self.set_status("正在拟合 CH1/CH2 正弦波并计算伯德图…")

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
                    self._apply_bode_results(result, source, messages),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _apply_bode_results(
        self, points: list[BodePoint], source: str, notes: list[str]
    ):
        self.bode_points = points
        self.bode_source = source
        self.save_bode_data_button.configure(state="normal")
        if self.bode_figure is not None:
            self.save_bode_image_button.configure(state="normal")
        minimum_r2 = min(
            min(point.ch1_r2, point.ch2_r2) for point in self.bode_points
        )
        note_text = f" | 提示 {len(notes)} 条" if notes else ""
        self.bode_text.set(
            f"已生成 {len(points)} 个频点 | CH1 输入、CH2 输出 | "
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
                linewidth=1.5, label="CH2 / CH1",
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
                    "frequency_hz", "ch1_amplitude_v", "ch2_amplitude_v",
                    "gain_ratio", "gain_db", "phase_deg", "phase_wrapped_deg",
                    "ch1_fit_r2", "ch2_fit_r2", "sample_count",
                ])
                for point in self.bode_points:
                    writer.writerow([
                        f"{point.frequency_hz:.12g}",
                        f"{point.ch1_amplitude_v:.12g}",
                        f"{point.ch2_amplitude_v:.12g}",
                        f"{point.gain_ratio:.12g}",
                        f"{point.gain_db:.12g}",
                        f"{point.phase_deg:.12g}",
                        f"{point.phase_wrapped_deg:.12g}",
                        f"{point.ch1_r2:.12g}",
                        f"{point.ch2_r2:.12g}",
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

    def update_wave_preview(self, frequency: float, amplitude: float, point: int | None = None):
        """Draw exactly five periods; cycles 2-4 are the stored region."""
        canvas = self.preview_canvas
        canvas.delete("all")
        width = int(canvas["width"])
        height = int(canvas["height"])
        left, right, top, bottom = 48, 12, 18, 30
        plot_width = width - left - right
        plot_height = height - top - bottom
        center_y = top + plot_height / 2
        time_window = five_cycle_duration(frequency)

        # Vertical divisions are cycle boundaries.
        for cycle in range(6):
            x = left + plot_width * cycle / 5
            canvas.create_line(x, top, x, top + plot_height, fill="#1e3a4d", dash=(2, 4))
            label_time = cycle / frequency
            canvas.create_text(x, height - 14, text=f"{label_time:.3g}s", fill="#91a9b8", font=("Segoe UI", 8))
        for division in range(5):
            y = top + plot_height * division / 4
            canvas.create_line(left, y, width - right, y, fill="#1e3a4d", dash=(2, 4))
        canvas.create_line(left, center_y, width - right, center_y, fill="#6b8291")
        canvas.create_text(7, top + 3, text=f"+{amplitude / 2:.3g}V", anchor="w", fill="#91a9b8", font=("Segoe UI", 8))
        canvas.create_text(7, center_y, text="0V", anchor="w", fill="#91a9b8", font=("Segoe UI", 8))
        canvas.create_text(7, top + plot_height - 3, text=f"-{amplitude / 2:.3g}V", anchor="w", fill="#91a9b8", font=("Segoe UI", 8))

        points: list[float] = []
        sample_count = 1200
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
            f"{frequency:.4f} Hz | {amplitude:.6g} Vpp | 5 周期 / {time_window:.6g} s | "
            f"保存周期 2–4{progress}"
        )

    def quit_app(self):
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
