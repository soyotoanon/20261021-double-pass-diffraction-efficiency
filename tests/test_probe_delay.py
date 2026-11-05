from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import simple_app_corrected80 as corrected
from test_zero_scan import Generator, Meter


class ProbeDelayTests(unittest.TestCase):
    def app(self):
        app = corrected.SimpleAOMApp.__new__(corrected.SimpleAOMApp)
        app.generator = Generator()
        app.power_meter = Meter(app.generator)
        app.accepted_incident_mw = 15.518445
        app.simulation = False
        app.stop_event = threading.Event()
        app._parse_scan_points = lambda: [0, 5]
        app.events = []
        app.emit = lambda kind, payload: app.events.append((kind, payload))
        app._start_worker = lambda name, work: work()
        return app

    def test_five_second_wait_happens_before_samples_and_rf_on(self):
        app = self.app()
        waits = []
        def sleep(event, duration):
            if duration == 1:
                waits.append(duration)
                self.assertFalse(app.generator.output_enabled)
                self.assertEqual(app.power_meter.states, [])
            return True
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(corrected, 'MEASUREMENT_DIR', Path(folder)), \
             patch.object(corrected, 'interruptible_sleep', side_effect=sleep), \
             patch.object(corrected.messagebox, 'askokcancel', return_value=True):
            app.start_scan()
            self.assertEqual(len(list(Path(folder).glob('*.csv'))), 1)
        self.assertEqual(waits, [1] * 5)
        self.assertEqual(len(app.power_meter.states), 10)
        self.assertEqual([x[1].split('：')[1].split(' ')[0] for x in app.events if x[0] == 'status'][:5], list('54321'))
        self.assertEqual(app.accepted_incident_mw, 15.518445)

    def test_stop_during_wait_does_not_scan_or_create_file(self):
        app = self.app()
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(corrected, 'MEASUREMENT_DIR', Path(folder)), \
             patch.object(corrected, 'interruptible_sleep', return_value=False), \
             patch.object(corrected.messagebox, 'askokcancel', return_value=True):
            app.start_scan()
            self.assertEqual(list(Path(folder).glob('*.csv')), [])
        self.assertEqual(app.power_meter.states, [])
        self.assertFalse(app.generator.output_enabled)


if __name__ == '__main__': unittest.main()
