import csv
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import simple_app
from core import build_inclusive_points


class Generator:
    def __init__(self):
        self.output_enabled = False
        self.amplitude = None
        self.calls = []
    def prepare(self, frequency):
        self.calls.append(('prepare', frequency))
        self.output_off()
    def output_off(self):
        self.calls.append(('off',))
        self.output_enabled = False
    def set_amplitude_mvpp(self, value):
        assert value >= 1
        self.calls.append(('set', value))
        self.amplitude = value
    def output_on(self):
        self.calls.append(('on',))
        self.output_enabled = True


class Meter:
    def __init__(self, generator):
        self.generator = generator
        self.states = []
    def read_mw(self):
        self.states.append((self.generator.output_enabled, self.generator.amplitude))
        return 1.0 if self.generator.output_enabled else 0.02


class ZeroScanTests(unittest.TestCase):
    def test_zero_and_positive_points(self):
        self.assertEqual(build_inclusive_points(0, 2, 1, allow_zero=True), [0, 1, 2])
        with self.assertRaises(ValueError):
            build_inclusive_points(0, 2, .5, allow_zero=True)
        with self.assertRaises(ValueError):
            build_inclusive_points(0, 2, 1)  # Full UI retains its original contract.

    def test_rf_is_off_for_zero_and_on_for_positive_points(self):
        app = simple_app.SimpleAOMApp.__new__(simple_app.SimpleAOMApp)
        app.generator = Generator()
        app.power_meter = Meter(app.generator)
        app.accepted_incident_mw = 10
        app.simulation = False
        app.stop_event = threading.Event()
        app._parse_scan_points = lambda: [0, 1, 2]
        app.emit = lambda *args: None
        app._start_worker = lambda name, work: work()
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(simple_app, 'MEASUREMENT_DIR', Path(folder)), \
             patch.object(simple_app, 'SCAN_SETTLE_S', 0), \
             patch.object(simple_app, 'SCAN_SAMPLE_INTERVAL_S', 0), \
             patch.object(simple_app.messagebox, 'askokcancel', return_value=True):
            app.start_scan()
            with next(Path(folder).glob('*.csv')).open(encoding='utf-8-sig') as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual([float(r['amplitude_mVpp']) for r in rows], [0, 1, 2])
        self.assertEqual(app.power_meter.states[:5], [(False, None)] * 5)
        self.assertEqual(app.power_meter.states[5:10], [(True, 1)] * 5)
        self.assertEqual(app.power_meter.states[10:], [(True, 2)] * 5)
        self.assertEqual(float(rows[0]['double_pass_power_mW']), .02)
        self.assertFalse(app.generator.output_enabled)


if __name__ == '__main__':
    unittest.main()
