"""Hardware-independent regression tests for controller math and SCPI flow."""
from __future__ import annotations

import csv
import io
import math
import threading
import unittest
from array import array
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np

from calibrate_channel_loopback import summarize_measurements
from validate_dual_afg_phase import phase_repeatability
from mho98_controller import (
    App,
    CaptureResult,
    Instrument,
    SweepPointConfiguration,
    WaveformFrame,
    WaveformNotReadyError,
    acquire_complete_cycles,
    calculate_bode,
    cycle_duration,
    extract_recent_cycles,
    load_sweep_ab_profile,
    load_waveform_groups,
    make_sweep_frequencies,
    maximum_afg_amplitude_vpp,
    recommended_timebase_scale,
    sweep_point_dwell_duration,
    validate_afg_sine_parameters,
)


class SweepDefinitionTests(unittest.TestCase):
    def test_linear_sweep_uses_requested_total_point_count(self):
        frequencies = make_sweep_frequencies(
            start_hz=1.0,
            stop_hz=5.0,
            spacing="linear",
            linear_points=5,
        )

        self.assertEqual(frequencies, [1.0, 2.0, 3.0, 4.0, 5.0])

    def test_log_sweep_uses_points_per_decade(self):
        frequencies = make_sweep_frequencies(
            start_hz=1.0,
            stop_hz=100.0,
            spacing="log",
            points_per_decade=10,
        )

        self.assertEqual(len(frequencies), 21)
        self.assertEqual(len(set(frequencies)), 21)
        self.assertTrue(all(a < b for a, b in zip(frequencies, frequencies[1:])))
        self.assertAlmostEqual(frequencies[0], 1.0)
        self.assertAlmostEqual(frequencies[10], 10.0)
        self.assertAlmostEqual(frequencies[-1], 100.0)

    def test_single_frequency_and_cycle_time_are_supported(self):
        self.assertEqual(make_sweep_frequencies(12.5, 12.5), [12.5])
        self.assertAlmostEqual(cycle_duration(2.0, 3), 1.5)
        self.assertAlmostEqual(recommended_timebase_scale(2.0, 3), 0.1875)
        self.assertAlmostEqual(sweep_point_dwell_duration(10_000.0, 5), 0.25)
        self.assertAlmostEqual(sweep_point_dwell_duration(2.0, 3), 1.5)
        with self.assertRaises(ValueError):
            cycle_duration(0.0, 3)

    def test_validates_amplitude_and_offset_output_envelope(self):
        self.assertEqual(maximum_afg_amplitude_vpp(1_000.0), 20.0)
        self.assertEqual(maximum_afg_amplitude_vpp(60_000_000.0), 10.0)
        validate_afg_sine_parameters(1_000.0, 2.0, 9.0)
        with self.assertRaisesRegex(ValueError, "偏置 B"):
            validate_afg_sine_parameters(1_000.0, 2.0, 9.001)

    def test_loads_exact_per_frequency_a_and_b_profile(self):
        profile = io.StringIO(
            "frequency_hz,a_peak_v,b_offset_v\n"
            "10,0.5,0\n"
            "100,0.25,0.2\n"
            "1000,0.1,\n"
        )
        with patch("builtins.open", return_value=profile):
            points = load_sweep_ab_profile("profile.csv")

        self.assertEqual([point.frequency_hz for point in points], [10, 100, 1000])
        self.assertEqual([point.amplitude_vpp for point in points], [1.0, 0.5, 0.2])
        self.assertEqual([point.offset_v for point in points], [0.0, 0.2, 0.0])

    def test_profile_rejects_ambiguous_peak_and_vpp_columns(self):
        profile = io.StringIO(
            "frequency_hz,a_peak_v,amplitude_vpp,offset_v\n10,0.5,1,0\n"
        )
        with patch("builtins.open", return_value=profile):
            with self.assertRaisesRegex(ValueError, "必须且只能"):
                load_sweep_ab_profile("profile.csv")

    def test_profile_accepts_short_mathematical_f_a_b_headers(self):
        with patch("builtins.open", return_value=io.StringIO("f,A,B\n25,0.3,-0.1\n")):
            point = load_sweep_ab_profile("profile.csv")[0]

        self.assertEqual(point.frequency_hz, 25.0)
        self.assertEqual(point.amplitude_peak_v, 0.3)
        self.assertEqual(point.offset_v, -0.1)


class WaveformExtractionTests(unittest.TestCase):
    @staticmethod
    def frame(channel: int, increment: float = 0.005) -> WaveformFrame:
        return WaveformFrame(
            channel=channel,
            values=[float(index + channel * 1000) for index in range(1001)],
            x_increment=increment,
            x_origin=-2.5,
            x_reference=0.0,
        )

    def test_extracts_requested_cycles_from_four_aligned_channels(self):
        frames = {channel: self.frame(channel) for channel in range(1, 5)}

        values, point_times, instrument_times, increment = extract_recent_cycles(
            frames,
            frequency=2.0,
            capture_cycles=3,
        )

        self.assertEqual(set(values), {1, 2, 3, 4})
        self.assertTrue(all(len(channel_values) == 301 for channel_values in values.values()))
        self.assertEqual(values[1], frames[1].values[700:])
        self.assertAlmostEqual(point_times[0], 0.0)
        self.assertAlmostEqual(point_times[-1], 1.5)
        self.assertAlmostEqual(instrument_times[0], 1.0)
        self.assertAlmostEqual(increment, 0.005)

    def test_rejects_unaligned_channel_intervals(self):
        frames = {1: self.frame(1), 2: self.frame(2, increment=0.006)}
        with self.assertRaisesRegex(ValueError, "XINCrement"):
            extract_recent_cycles(frames, frequency=2.0, capture_cycles=3)


class InstrumentFlowTests(unittest.TestCase):
    def test_sweep_configuration_returns_instrument_accepted_values(self):
        instrument = Instrument()
        writes: list[str] = []
        timebase_responses = iter((0.001, 0.2))

        instrument._write_unlocked = writes.append

        def query(command: str) -> str:
            if command == ":TIMebase:MAIN:SCALe?":
                return str(next(timebase_responses))
            if command == ":TRIGger:STATus?":
                return "STOP"
            if command.endswith(":FREQuency?"):
                return "12.25"
            if command.endswith(":VOLTage:AMPLitude?"):
                return "1.8"
            if command.endswith(":VOLTage:OFFSet?"):
                return "0.24"
            if command.endswith(":PHASe?"):
                return "0"
            if command == ":SYSTem:ERRor?":
                return '0,"No error"'
            raise AssertionError(f"unexpected query: {command}")

        instrument._query_unlocked = query

        result = instrument.configure_sweep_point(
            channel=1,
            frequency=12.3,
            amplitude=2.0,
            capture_cycles=3,
            is_current=lambda: True,
            offset_v=0.25,
        )

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.requested_frequency_hz, 12.3)
        self.assertAlmostEqual(result.actual_frequency_hz, 12.25)
        self.assertAlmostEqual(result.requested_amplitude_vpp, 2.0)
        self.assertAlmostEqual(result.actual_amplitude_vpp, 1.8)
        self.assertAlmostEqual(result.requested_offset_v, 0.25)
        self.assertAlmostEqual(result.actual_offset_v, 0.24)
        zero_offset_index = writes.index(":SOURce1:VOLTage:OFFSet 0")
        amplitude_index = writes.index(":SOURce1:VOLTage:AMPLitude 2")
        requested_offset_index = writes.index(":SOURce1:VOLTage:OFFSet 0.25")
        self.assertLess(zero_offset_index, amplitude_index)
        self.assertLess(amplitude_index, requested_offset_index)
        self.assertIn(":SOURce1:OUTPut:STATe ON", writes)
        self.assertIn(":SOURce1:PHASe 0", writes)
        self.assertIn(":SOURce1:PHASe:SYNChronize", writes)
        self.assertIn("*WAI", writes)
        self.assertIn(":RUN", writes)
        self.assertEqual(result.actual_phase_deg, 0.0)
        self.assertTrue(result.phase_synchronized)

    def test_timebase_is_retried_when_instrument_rounds_down_too_far(self):
        instrument = Instrument()
        writes: list[str] = []
        timebase_responses = iter((0.001, 0.02, 0.05))
        instrument._write_unlocked = writes.append

        def query(command: str) -> str:
            if command == ":TIMebase:MAIN:SCALe?":
                return str(next(timebase_responses))
            if command == ":TRIGger:STATus?":
                return "RUN"
            if command.endswith(":FREQuency?"):
                return "10"
            if command.endswith(":VOLTage:AMPLitude?"):
                return "1"
            if command.endswith(":VOLTage:OFFSet?"):
                return "0"
            if command.endswith(":PHASe?"):
                return "0"
            if command == ":SYSTem:ERRor?":
                return '0,"No error"'
            raise AssertionError(f"unexpected query: {command}")

        instrument._query_unlocked = query

        result = instrument.configure_sweep_point(
            channel=1,
            frequency=10.0,
            amplitude=1.0,
            capture_cycles=3,
            is_current=lambda: True,
        )

        timebase_writes = [
            command for command in writes if command.startswith(":TIMebase:MAIN:SCALe ")
        ]
        self.assertEqual(len(timebase_writes), 2)
        self.assertAlmostEqual(result.timebase_scale_s, 0.05)

    def test_sweep_phase_can_be_set_without_alignment_for_batch_configuration(self):
        instrument = Instrument()
        writes: list[str] = []
        timebase_responses = iter((0.001, 0.2))
        instrument._write_unlocked = writes.append

        def query(command: str) -> str:
            if command == ":TIMebase:MAIN:SCALe?":
                return str(next(timebase_responses))
            if command == ":TRIGger:STATus?":
                return "RUN"
            if command.endswith(":FREQuency?"):
                return "12.5"
            if command.endswith(":VOLTage:AMPLitude?"):
                return "1"
            if command.endswith(":VOLTage:OFFSet?"):
                return "0"
            if command.endswith(":PHASe?"):
                return "45"
            if command == ":SYSTem:ERRor?":
                return '0,"No error"'
            raise AssertionError(f"unexpected query: {command}")

        instrument._query_unlocked = query
        result = instrument.configure_sweep_point(
            channel=2,
            frequency=12.5,
            amplitude=1.0,
            capture_cycles=3,
            is_current=lambda: True,
            phase_deg=45.0,
            synchronize_phase=False,
        )

        self.assertEqual(result.actual_phase_deg, 45.0)
        self.assertFalse(result.phase_synchronized)
        self.assertIn(":SOURce2:PHASe 45", writes)
        self.assertNotIn(":SOURce2:PHASe:SYNChronize", writes)

    def test_finish_sweep_restores_initial_stop_state(self):
        instrument = Instrument()
        writes: list[str] = []
        instrument._write_unlocked = writes.append
        instrument._query_unlocked = lambda command: (
            "0.002" if command == ":TIMebase:MAIN:SCALe?" else "0"
        )
        instrument.sweep_original_timebase_scale = 0.002
        instrument.sweep_original_scope_running = False

        restored = instrument.finish_sweep(2)

        self.assertEqual(restored, 0.002)
        self.assertIn(":STOP", writes)
        self.assertEqual(writes[-1], ":TIMebase:MAIN:SCALe 0.002")
        self.assertIsNone(instrument.sweep_original_scope_running)

    def test_freezes_once_reads_four_channels_and_restores_run(self):
        instrument = Instrument()
        writes: list[str] = []
        selected_channel = 1
        trigger_states = iter(("RUN", "STOP"))

        def write(command: str) -> None:
            nonlocal selected_channel
            writes.append(command)
            if command.startswith(":WAVeform:SOURce CHANnel"):
                selected_channel = int(command.rsplit("nel", 1)[1])

        def query(command: str) -> str:
            if command == ":TRIGger:STATus?":
                return next(trigger_states)
            if command.startswith(":CHANnel") and command.endswith(":DISPlay?"):
                return "1"
            if command == ":WAVeform:SOURce?":
                return f"CHAN{selected_channel}"
            if command == ":WAVeform:PREamble?":
                return "2,0,1000,1,0.001,-0.5,0,1,0,0"
            if command == ":WAVeform:DATA?":
                return ",".join(
                    str(selected_channel + index / 1000) for index in range(1000)
                )
            raise AssertionError(f"unexpected query: {command}")

        instrument._write_unlocked = write
        instrument._query_unlocked = query

        result = instrument.acquire_channels((1, 2, 3, 4))

        self.assertEqual(set(result.frames), {1, 2, 3, 4})
        self.assertTrue(result.scope_was_running)
        self.assertEqual(writes.count(":STOP"), 1)
        self.assertEqual(writes.count(":RUN"), 1)
        self.assertLess(writes.index(":STOP"), writes.index(":WAVeform:MODE NORMal"))
        self.assertEqual(writes[-1], ":RUN")

    def test_incomplete_screen_frame_is_retried_without_changing_point(self):
        class FakeInstrument:
            def __init__(self):
                self.calls = 0

            def acquire_channels(self, channels):
                self.calls += 1
                if self.calls == 1:
                    raise WaveformNotReadyError("only 2 of 1000 points")
                values = [math.sin(2 * math.pi * 2 * index * 0.002) for index in range(1000)]
                return CaptureResult(
                    frames={
                        2: WaveformFrame(
                            channel=2,
                            values=values,
                            x_increment=0.002,
                            x_origin=-1.0,
                            x_reference=0.0,
                        )
                    },
                    captured_at_utc=datetime.now(timezone.utc),
                    captured_monotonic_s=1.0,
                    scope_was_running=True,
                )

        instrument = FakeInstrument()
        retries = []
        with patch("mho98_controller.time.sleep", return_value=None):
            result = acquire_complete_cycles(
                instrument=instrument,
                channels=(2,),
                frequency=2.0,
                capture_cycles=3,
                on_retry=lambda attempt, error: retries.append((attempt, str(error))),
            )

        self.assertEqual(instrument.calls, 2)
        self.assertEqual(result[-1], 1)
        self.assertEqual(retries, [(1, "only 2 of 1000 points")])


class SweepLifecycleTests(unittest.TestCase):
    def test_restart_is_blocked_until_instrument_cleanup_finishes(self):
        cleanup_started = threading.Event()
        allow_cleanup_to_finish = threading.Event()
        cleanup_finished = threading.Event()

        class FakeInstrument:
            connected = True

            def finish_sweep(self, channel: int) -> float:
                self.channel = channel
                cleanup_started.set()
                self.assert_cleanup_released = allow_cleanup_to_finish.wait(1.0)
                return 0.001

        class FakeVariable:
            def __init__(self):
                self.value = True

            def set(self, value):
                self.value = value

        class FakeButton:
            def __init__(self):
                self.calls = []

            def configure(self, **kwargs):
                self.calls.append(kwargs)

        app = object.__new__(App)
        app.sweep_stopping = False
        app.sweep_running = True
        app.sweep_generation = 3
        app.active_sweep_point = 1
        app.active_sweep_frequency = 10.0
        app.active_sweep_configuration = object()
        app.sweep_point_started_at = 1.0
        app.sweep_after_id = None
        app.output = FakeVariable()
        app.acq_running = False
        app.inst = FakeInstrument()
        app.sweep_channel = 2
        app.sweep_button = FakeButton()
        app.closing = False
        statuses = []
        app.set_status = statuses.append
        app.after = lambda _delay, callback: callback()

        def finish_cleanup(_message, _completed_normally):
            app.sweep_stopping = False
            cleanup_finished.set()

        app._finish_sweep_cleanup = finish_cleanup

        app.stop_sweep("用户停止扫频")
        self.assertTrue(cleanup_started.wait(1.0))
        self.assertTrue(app.sweep_stopping)
        self.assertEqual(app.sweep_button.calls[-1]["state"], "disabled")

        app.start_sweep()
        self.assertIn("仍在停止清理中", statuses[-1])
        self.assertTrue(app.sweep_stopping)

        allow_cleanup_to_finish.set()
        self.assertTrue(cleanup_finished.wait(1.0))
        self.assertFalse(app.sweep_stopping)


class CsvSchemaTests(unittest.TestCase):
    def test_csv_uses_actual_metadata_and_explicit_timestamp_semantics(self):
        app = object.__new__(App)
        app.acq_channels = (1, 2, 3, 4)
        app.acq_values = {channel: array("d") for channel in app.acq_channels}
        stream = io.StringIO()
        app.acq_stream = stream
        app.acq_writer = csv.writer(stream)
        app.acq_writer.writerow(app._acquisition_csv_header(app.acq_channels))
        configuration = SweepPointConfiguration(
            requested_frequency_hz=10.0,
            actual_frequency_hz=9.9,
            requested_amplitude_vpp=2.0,
            actual_amplitude_vpp=1.95,
            timebase_scale_s=0.04,
            requested_offset_v=0.2,
            actual_offset_v=0.19,
            requested_phase_deg=30.0,
            actual_phase_deg=29.99,
        )
        epoch = 1_700_000_000.0

        app._append_live_csv(
            point_number=1,
            configuration=configuration,
            cycle_numbers=[1, 1],
            point_times=[0.0, 0.1],
            instrument_times=[-0.1, 0.0],
            sweep_elapsed_times=[0.9, 1.0],
            capture_epoch_times=[epoch, epoch],
            sample_epoch_times=[epoch - 0.1, epoch],
            sample_interval=0.1,
            values_by_channel={
                1: [1.0, 1.1],
                2: [2.0, 2.1],
                3: [3.0, 3.1],
                4: [4.0, 4.1],
            },
        )

        rows = list(csv.DictReader(io.StringIO(stream.getvalue())))
        self.assertEqual(len(rows), 2)
        self.assertNotIn("stored_timestamp", rows[0])
        self.assertNotIn("stored_time_s", rows[0])
        self.assertEqual(float(rows[0]["frequency_hz"]), 9.9)
        self.assertEqual(float(rows[0]["requested_frequency_hz"]), 10.0)
        self.assertEqual(float(rows[0]["amplitude_vpp"]), 1.95)
        self.assertEqual(float(rows[0]["requested_sine_amplitude_peak_v"]), 1.0)
        self.assertEqual(float(rows[0]["sine_amplitude_peak_v"]), 0.975)
        self.assertEqual(float(rows[0]["requested_offset_v"]), 0.2)
        self.assertEqual(float(rows[0]["offset_v"]), 0.19)
        self.assertEqual(float(rows[0]["requested_phase_deg"]), 30.0)
        self.assertEqual(float(rows[0]["phase_deg"]), 29.99)
        self.assertEqual(float(rows[0]["timebase_scale_s"]), 0.04)
        self.assertEqual(float(rows[1]["ch4_voltage_v"]), 4.1)


class BodeCalculationTests(unittest.TestCase):
    def test_recovers_known_response_over_reference(self):
        frequency = 5.0
        times = np.linspace(0.0, 2.0, 2000, endpoint=False)
        reference = (
            2.0 * np.sin(2.0 * math.pi * frequency * times + math.radians(20.0))
            + 0.3
        )
        response = (
            0.5 * np.sin(2.0 * math.pi * frequency * times + math.radians(-40.0))
            - 0.2
        )

        points, warnings = calculate_bode(
            {frequency: (times.tolist(), reference.tolist(), response.tolist())}
        )

        self.assertEqual(warnings, [])
        point = points[0]
        self.assertAlmostEqual(point.reference_amplitude_v, 2.0, places=10)
        self.assertAlmostEqual(point.response_amplitude_v, 0.5, places=10)
        self.assertAlmostEqual(point.gain_ratio, 0.25, places=10)
        self.assertAlmostEqual(point.gain_db, 20.0 * math.log10(0.25), places=10)
        self.assertAlmostEqual(point.phase_deg, -60.0, places=10)
        self.assertGreater(point.reference_r2, 0.999999)
        self.assertGreater(point.response_r2, 0.999999)

    def test_loads_selected_channels_from_simple_csv(self):
        rows = ["frequency,CH2,CH4"]
        rows.extend(f"10.0,{index},{index / 2}" for index in range(12))
        with patch("builtins.open", return_value=io.StringIO("\n".join(rows))):
            groups, assumptions = load_waveform_groups(
                "simple.csv", reference_channel=2, response_channel=4
            )

        times, reference, response = groups[10.0]
        self.assertEqual(len(times), 12)
        self.assertEqual(reference[-1], 11.0)
        self.assertEqual(response[-1], 5.5)
        self.assertTrue(any("均匀覆盖 3 个周期" in note for note in assumptions))


class LoopbackCalibrationTests(unittest.TestCase):
    def test_recovers_constant_channel_delay_from_frequency_phase_slope(self):
        delay_s = 75e-9
        measurements = []
        for frequency in (100_000.0, 300_000.0, 1_000_000.0):
            for repeat in (1, 2, 3):
                measurements.append(
                    {
                        "channel": 2,
                        "frequency_hz": frequency,
                        "phase_relative_to_reference_deg": -360.0
                        * frequency
                        * delay_s,
                        "amplitude_ratio": 0.995,
                        "reference_fit_r2": 0.999,
                        "channel_fit_r2": 0.998,
                        "repeat": repeat,
                    }
                )

        summaries = summarize_measurements(
            measurements,
            channels=(1, 2),
            reference_channel=1,
        )

        channel_2 = next(row for row in summaries if row["channel"] == 2)
        self.assertAlmostEqual(
            channel_2["delay_relative_to_reference_s"], delay_s, places=15
        )
        self.assertAlmostEqual(channel_2["mean_amplitude_ratio"], 0.995)
        self.assertLess(channel_2["phase_fit_rms_deg"], 1e-10)

    def test_phase_repeatability_handles_wrap_at_180_degrees(self):
        summary = phase_repeatability(
            [
                {"frequency_hz": 1000.0, "phase_ch1_minus_ch2_deg": 179.0},
                {"frequency_hz": 1000.0, "phase_ch1_minus_ch2_deg": -179.0},
            ]
        )

        self.assertEqual(len(summary), 1)
        self.assertAlmostEqual(
            summary[0]["phase_repeatability_peak_to_peak_deg"], 2.0
        )


if __name__ == "__main__":
    unittest.main()
