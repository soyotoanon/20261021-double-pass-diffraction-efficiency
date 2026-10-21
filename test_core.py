import math
import unittest

from core import (
    SweepReading,
    SweepSummary,
    analyze_stability,
    build_inclusive_points,
    efficiency_percent,
    recommend_work_point,
    summarize_readings,
)
from devices import SimulatedPM100D, SimulatedRigol


class CoreTests(unittest.TestCase):
    def test_inclusive_points_exact_endpoint(self):
        self.assertEqual(build_inclusive_points(300, 550, 10)[-1], 550)
        self.assertEqual(len(build_inclusive_points(300, 550, 10)), 26)
        self.assertEqual(build_inclusive_points(300, 305, 2), [300, 302, 304, 305])

    def test_hard_limit(self):
        with self.assertRaisesRegex(ValueError, "硬上限"):
            build_inclusive_points(300, 551, 10)

    def test_efficiency_dark_subtraction(self):
        self.assertAlmostEqual(efficiency_percent(5.1, 10.1, 0.1), 50.0)
        self.assertGreater(efficiency_percent(11.0, 10.0, 0.0), 100.0)

    def test_stability_pass_and_fail(self):
        stable = [(index * 0.2, 10.0 + 0.002 * math.sin(index)) for index in range(30)]
        result = analyze_stability(stable, dark_mw=0.0, threshold_pct=1.0)
        self.assertTrue(result.passed)
        drifting = [(index * 0.2, 10.0 + index * 0.01) for index in range(30)]
        result = analyze_stability(drifting, dark_mw=0.0, threshold_pct=1.0)
        self.assertFalse(result.passed)

    def test_summary_and_work_point(self):
        readings = []
        for pass_index, offset in ((1, 0.0), (2, 1.0)):
            for amplitude, efficiency in ((300, 10), (400, 30), (500, 50)):
                readings.append(
                    SweepReading(
                        timestamp="2026-10-21T00:00:00+08:00",
                        pass_index=pass_index,
                        direction="up" if pass_index == 1 else "down",
                        amplitude_mvpp=amplitude,
                        power_mw=efficiency / 10 + offset / 100,
                        power_std_mw=0.0,
                        efficiency_pct=efficiency + offset,
                        efficiency_std_pct=0.0,
                        sample_count=5,
                    )
                )
        summary = summarize_readings(readings)
        self.assertEqual(len(summary), 3)
        work = recommend_work_point(summary, 0.8)
        self.assertAlmostEqual(work.target_efficiency_pct, 40.4)
        self.assertGreater(work.amplitude_mvpp, 400)
        self.assertLess(work.amplitude_mvpp, 500)

    def test_noisy_monotonic_envelope_for_work_point(self):
        summary = [
            SweepSummary(300, 1, 0, 10, 0, 1),
            SweepSummary(350, 2, 0, 25, 0, 1),
            SweepSummary(400, 2, 0, 24, 0, 1),
            SweepSummary(450, 4, 0, 50, 0, 1),
        ]
        work = recommend_work_point(summary, 0.8)
        self.assertGreaterEqual(work.amplitude_mvpp, 400)
        self.assertLessEqual(work.amplitude_mvpp, 450)

    def test_simulated_devices(self):
        generator = SimulatedRigol(lambda _message: None)
        meter = SimulatedPM100D(generator)
        meter.mode = "diffracted"
        generator.set_amplitude_mvpp(300)
        generator.output_on()
        low = sum(meter.read_mw() for _ in range(10)) / 10
        generator.set_amplitude_mvpp(500)
        high = sum(meter.read_mw() for _ in range(10)) / 10
        self.assertGreater(high, low)
        generator.output_off()
        self.assertLess(meter.read_mw(), 0.01)


if __name__ == "__main__":
    unittest.main(verbosity=2)
