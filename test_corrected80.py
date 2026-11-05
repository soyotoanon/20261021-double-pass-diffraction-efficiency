import csv
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

import simple_app_corrected80 as corrected
from test_zero_scan import Generator, Meter


class Corrected80Tests(unittest.TestCase):
    def test_same_meter_correction_cancels_in_efficiency(self):
        incident = corrected.corrected_power_mw(8.0)
        output = corrected.corrected_power_mw(4.0)
        self.assertEqual(incident, 10.0)
        self.assertEqual(output, 5.0)
        self.assertEqual(corrected.efficiency_percent(output, incident, 0), 50.0)

    def test_incident_acquisition_and_csv_apply_factor_once(self):
        app = corrected.SimpleAOMApp.__new__(corrected.SimpleAOMApp)
        app.generator = Generator()
        app.power_meter = MagicMock()
        app.power_meter.read_mw.return_value = 8.0
        app.simulation = True
        app.stop_event = threading.Event()
        app.accepted_var = MagicMock()
        app.incident_stats_var = MagicMock()
        app._update_button_states = lambda: None
        app._start_worker = lambda name, work: work()
        events = []
        app.emit = lambda kind, payload: events.append((kind, payload))
        current_time = iter(i * .25 for i in range(1000))
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(corrected, 'MEASUREMENT_DIR', Path(folder)), \
             patch.object(corrected.time, 'monotonic', side_effect=lambda: next(current_time)), \
             patch.object(corrected, 'interruptible_sleep', return_value=True):
            app.start_incident_measurement()
            with next(Path(folder).glob('*_corrected80.csv')).open(encoding='utf-8-sig') as handle:
                rows = list(csv.DictReader(handle))
        self.assertGreater(len(rows), 3)
        self.assertEqual(float(rows[0]['incident_power_mW']), 10.0)
        self.assertEqual(float(rows[0]['meter_raw_mW']), 8.0)
        self.assertEqual(float(rows[0]['meter_response_factor']), .8)
        result = next(payload['result'] for kind, payload in events if kind == 'incident_done')
        self.assertEqual(result.mean_raw_mw, 10.0)
        sample = next(payload for kind, payload in events if kind == 'incident_sample')
        self.assertEqual(sample[1], 10.0)

    def test_scan_saves_corrected_and_original_values_and_rf_off_point(self):
        app = corrected.SimpleAOMApp.__new__(corrected.SimpleAOMApp)
        app.generator = Generator()
        app.power_meter = Meter(app.generator)
        app.accepted_incident_mw = corrected.corrected_power_mw(8.0)
        app.simulation = False
        app.stop_event = threading.Event()
        app._parse_scan_points = lambda: [0, 1]
        events = []
        app.emit = lambda kind, payload: events.append((kind, payload))
        app._start_worker = lambda name, work: work()
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(corrected, 'MEASUREMENT_DIR', Path(folder)), \
             patch.object(corrected, 'SCAN_SETTLE_S', 0), \
             patch.object(corrected, 'SCAN_SAMPLE_INTERVAL_S', 0), \
             patch.object(corrected.messagebox, 'askokcancel', return_value=True):
            app.start_scan()
            with next(Path(folder).glob('*_corrected80.csv')).open(encoding='utf-8-sig') as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(float(rows[0]['double_pass_power_mW']), 0.025)
        self.assertEqual(float(rows[0]['meter_raw_mean_mW']), 0.02)
        self.assertEqual(float(rows[1]['double_pass_power_mW']), 1.25)
        self.assertEqual(float(rows[1]['meter_raw_mean_mW']), 1.0)
        self.assertEqual(float(rows[1]['incident_reference_mW']), 10.0)
        self.assertEqual(float(rows[1]['incident_meter_raw_mW']), 8.0)
        self.assertEqual(float(rows[1]['efficiency_percent']), 12.5)
        self.assertEqual(float(rows[1]['meter_response_factor']), .8)
        self.assertEqual(app.power_meter.states[:5], [(False, None)] * 5)
        self.assertFalse(app.generator.output_enabled)
        point = [item for kind, item in events if kind == 'scan_point'][1]['reading']
        self.assertEqual(point['power_mw'], 1.25)
        self.assertEqual(point['efficiency_pct'], 12.5)

    def test_only_corrected_reference_files_are_loaded(self):
        # Original files may come from a different meter: they must never be silently reinterpreted.
        with tempfile.TemporaryDirectory() as folder:
            old = Path(folder) / '20261021_incident_60s.csv'
            old.write_text('elapsed_s,incident_power_mW\n10,8\n11,8\n12,8\n', encoding='utf-8')
            app = corrected.SimpleAOMApp.__new__(corrected.SimpleAOMApp)
            with patch.object(corrected, 'MEASUREMENT_DIR', Path(folder)):
                app.load_latest_incident_csv()  # returns before accessing UI widgets


if __name__ == '__main__':
    unittest.main()
