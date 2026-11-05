import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import simple_app_corrected80 as app_module
from test_probe_delay import ProbeDelayTests


class DescendingTests(unittest.TestCase):
    def test_points_and_limits(self):
        points = app_module.build_descending_points(750, 0, 5)
        self.assertEqual(points, list(range(750, -1, -5)))
        self.assertEqual(app_module.build_descending_points(750, 0, 200), [750, 550, 350, 150, 0])
        for args in [(751, 0, 5), (0, 750, 5), (750, -1, 5), (750, 0, 0), (2, 0, .5)]:
            with self.assertRaises(ValueError):
                app_module.build_descending_points(*args)

    def test_actual_scan_order_csv_and_zero_rf_off(self):
        app = ProbeDelayTests().app()
        points = app_module.build_descending_points(750, 0, 5)
        app._parse_scan_points = lambda: points
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(app_module, 'MEASUREMENT_DIR', Path(folder)), \
             patch.object(app_module, 'interruptible_sleep', return_value=True), \
             patch.object(app_module.messagebox, 'askokcancel', return_value=True):
            app.start_scan()
            with next(Path(folder).glob('*.csv')).open(encoding='utf-8-sig') as f:
                rows = list(csv.DictReader(f))
        self.assertEqual([float(row['amplitude_mVpp']) for row in rows], points)
        self.assertEqual([call[1] for call in app.generator.calls if call[0] == 'set'], points[:-1])
        self.assertEqual(app.power_meter.states[:5], [(True, 750)] * 5)
        self.assertTrue(all(not state[0] for state in app.power_meter.states[-5:]))
        self.assertFalse(app.generator.output_enabled)
        self.assertEqual(app.accepted_incident_mw, 15.518445)


if __name__ == '__main__':
    unittest.main()
