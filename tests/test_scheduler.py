"""Exercise the real main loop with an accelerated clock, without network or sleeping."""
import os
import sys
import unittest
from unittest.mock import Mock, patch

import sync


class SchedulerTests(unittest.TestCase):
    def simulate(self, durations, failures=(), interval=300):
        clock = {"now": 0.0}
        starts, waits = [], []

        class StopEvent:
            stopped = False

            def is_set(self):
                return self.stopped

            def set(self):
                self.stopped = True

            def wait(self, timeout):
                waits.append(timeout)
                clock["now"] += timeout
                if len(waits) == len(durations):
                    self.stopped = True
                return self.stopped

        def run_job(*_, **__):
            starts.append(clock["now"])
            clock["now"] += durations[len(starts) - 1]
            if len(starts) - 1 in failures:
                raise sync.SyncError("fake failure")
            return {"delete_blocked": False}

        cfg = sync.Config("fake", "fake", interval=interval)
        state = Mock()
        stop = StopEvent()
        previous_umask = os.umask(0o077)
        try:
            with patch.object(sys, "argv", ["sync.py"]), patch("sync.load_dotenv"), \
                 patch.object(sync.Config, "from_env", return_value=cfg), \
                 patch("sync.State", return_value=state), patch("sync.Google"), \
                 patch("sync.signal.signal"), patch("sync.threading.Event", return_value=stop), \
                 patch("sync.time.monotonic", side_effect=lambda: clock["now"]), \
                 patch("sync.random.uniform", return_value=0), \
                 patch("sync.sync_cycle", side_effect=run_job), \
                 self.assertLogs("calendar-sync", level="INFO") as captured:
                self.assertEqual(sync.main(), 0)
        finally:
            os.umask(previous_umask)
        state.close.assert_called_once()
        return starts, waits, captured.output

    def test_short_tasks_keep_five_minute_schedule(self):
        starts, waits, _ = self.simulate([60, 60, 60])
        self.assertEqual(starts, [0, 300, 600])
        self.assertEqual(waits, [240, 240, 240])

    def test_seven_minute_task_skips_five_minute_trigger(self):
        starts, waits, _ = self.simulate([420, 60])
        self.assertEqual(starts, [0, 600])
        self.assertEqual(waits, [180, 240])

    def test_long_task_skips_every_elapsed_trigger_without_catchup(self):
        starts, waits, _ = self.simulate([901, 60])
        self.assertEqual(starts, [0, 1200])
        self.assertEqual(waits, [299, 240])

    def test_completion_exactly_on_a_trigger_can_start_next_task(self):
        starts, waits, _ = self.simulate([600, 60])
        self.assertEqual(starts, [0, 600])
        self.assertEqual(waits, [0, 240])

    def test_repeated_overruns_preserve_schedule_without_drift(self):
        starts, waits, _ = self.simulate([420, 420, 60])
        self.assertEqual(starts, [0, 600, 1200])
        self.assertEqual(waits, [180, 180, 240])

    def test_ten_minute_default_skips_thirteen_minute_overrun(self):
        starts, waits, _ = self.simulate([780, 60], interval=600)
        self.assertEqual(starts, [0, 1200])
        self.assertEqual(waits, [420, 540])

    def test_failure_backoff_and_success_restores_regular_interval(self):
        starts, waits, _ = self.simulate([0, 0, 0, 0], failures={0, 1}, interval=600)
        self.assertEqual(starts, [0, 1200, 3600, 4200])
        self.assertEqual(waits, [1200, 2400, 600, 600])

    def test_backoff_caps_at_six_hours(self):
        starts, waits, _ = self.simulate([0] * 8, failures=set(range(8)), interval=600)
        self.assertEqual(waits, [1200, 2400, 4800, 9600, 19200, 21600, 21600, 21600])


if __name__ == "__main__":
    unittest.main()
