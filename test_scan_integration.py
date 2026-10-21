import json
from pathlib import Path
import tempfile
import threading
import unittest

import app as app_module
from app import AOMScanApp
from core import StabilityResult
from devices import SimulatedPM100D, SimulatedRigol


def make_harness(output_dir: Path):
    harness = AOMScanApp.__new__(AOMScanApp)
    harness.simulation = True
    harness.generator = SimulatedRigol(lambda _message: None)
    harness.power_meter = SimulatedPM100D(harness.generator)
    harness.dark_mw = 0.0002
    harness.incident_mw = 10.0
    harness.stability_result = StabilityResult(
        sample_count=20,
        duration_s=4.0,
        mean_raw_mw=10.0,
        mean_corrected_mw=9.9998,
        std_mw=0.001,
        relative_std_pct=0.01,
        peak_to_peak_pct=0.05,
        fitted_drift_pct=0.01,
        fitted_drift_pct_per_min=0.15,
        threshold_pct=1.0,
        passed=True,
    )
    harness.stop_event = threading.Event()
    harness.events_for_test = []
    harness.emit = lambda kind, payload=None: harness.events_for_test.append((kind, payload))
    app_module.MEASUREMENT_DIR = output_dir
    return harness


class ScanIntegrationTests(unittest.TestCase):
    def test_simulated_scan_writes_consistent_files_and_turns_rf_off(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = make_harness(Path(temporary))
            config = {
                "frequency_mhz": 100.0,
                "start_mvpp": 300.0,
                "stop_mvpp": 320.0,
                "step_mvpp": 10.0,
                "points": [300.0, 310.0, 320.0],
                "settle_s": 0.0,
                "samples_per_point": 3,
                "sample_gap_s": 0.0,
                "amplifier_gain_db_recorded": 40.0,
                "target_fraction_of_max": 0.8,
                "bidirectional": True,
            }
            harness._run_scan(config)
            self.assertFalse(harness.generator.output_enabled)
            done = [payload for kind, payload in harness.events_for_test if kind == "scan_done"]
            self.assertEqual(len(done), 1)
            result = done[0]
            self.assertFalse(result["stopped"])
            self.assertEqual(len(result["readings"]), 6)
            raw_path, summary_path, json_path, _png_path = result["files"]
            self.assertTrue(raw_path.exists())
            self.assertTrue(summary_path.exists())
            self.assertTrue(json_path.exists())
            metadata = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "complete")
            self.assertEqual(len(metadata["readings"]), 6)
            self.assertEqual(len(metadata["summary"]), 3)
            self.assertIn("recommended_work_point", metadata)

    def test_rf_is_off_when_power_meter_raises(self):
        class FailingMeter:
            identity = {"model": "FAIL"}
            mode = "diffracted"

            def read_mw(self):
                raise RuntimeError("synthetic meter failure")

        with tempfile.TemporaryDirectory() as temporary:
            harness = make_harness(Path(temporary))
            harness.power_meter = FailingMeter()
            config = {
                "frequency_mhz": 100.0,
                "start_mvpp": 300.0,
                "stop_mvpp": 310.0,
                "step_mvpp": 10.0,
                "points": [300.0, 310.0],
                "settle_s": 0.0,
                "samples_per_point": 1,
                "sample_gap_s": 0.0,
                "amplifier_gain_db_recorded": 40.0,
                "target_fraction_of_max": 0.8,
                "bidirectional": False,
            }
            with self.assertRaisesRegex(RuntimeError, "synthetic meter failure"):
                harness._run_scan(config)
            self.assertFalse(harness.generator.output_enabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
