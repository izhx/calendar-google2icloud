import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from icalendar import Calendar

import authorize_google
import google_api
import sync
from test_sync import FakeICloud, event, feed, cache

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def item(uid="a", **changes):
    result = {"id": "id-" + uid, "iCalUID": uid, "summary": "Title", "status": "confirmed",
              "start": {"dateTime": "2026-10-18T09:00:00Z"},
              "end": {"dateTime": "2026-10-18T10:00:00Z"},
              "updated": "2026-10-18T01:00:00Z"}
    result.update(changes)
    return result


def page(items, **changes):
    return {"kind": "calendar#events", "items": items, "timeZone": "Asia/Shanghai", **changes}


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = sync.Config("fake", "fake", state_file=Path(self.tmp.name) / "state.json")
        self.state = sync.State(self.cfg.state_file)
        self.addCleanup(lambda: self.state.close())
        self.cloud = FakeICloud()
        self.now = NOW
        self.current = google_api.windows(NOW, self.cfg.sync_timezone)

    def run_job(self, raw, name="fast", **kwargs):
        google = Mock()
        google.refresh.return_value = cache()
        with patch("sync.render_cache", return_value=raw):
            result = sync.sync_cycle(self.cfg, self.state, now=self.now, google=google, selection=name,
                                     destination_factory=lambda _: self.cloud, **kwargs)
        return result["windows"][0]

    def test_fast_includes_all_future_without_arbitrary_upper_bound(self):
        raw = feed(event("recent"), event("future", start="DTSTART:20991231T090000Z"),
                   event("history", start="DTSTART:20260501T090000Z"), event("old", start="DTSTART:20200101T090000Z"))
        self.assertEqual(self.run_job(raw)["create"], 2)
        self.assertEqual(set(self.state.data["events"]), {"recent", "future"})

    def test_history_half_year_to_seven_days_ago(self):
        raw = feed(event("recent"), event("future", start="DTSTART:20261020T090000Z"),
                   event("history", start="DTSTART:20260501T090000Z"), event("old", start="DTSTART:20260101T090000Z"))
        self.assertEqual(self.run_job(raw, "history")["create"], 1)
        self.assertEqual(set(self.state.data["events"]), {"history"})

    def test_range_end_is_exclusive_and_spanning_event_included(self):
        raw = feed(event("boundary", start="DTSTART;TZID=Asia/Shanghai:20261011T200000"),
                   event("spanning", start="DTSTART;TZID=Asia/Shanghai:20260417T230000",
                         extra="DTEND;TZID=Asia/Shanghai:20260418T010000\r\n"))
        self.assertEqual(self.run_job(raw, "history")["create"], 1)
        self.assertIn("spanning", self.state.data["events"])

    def test_all_day_uses_source_calendar_zone_at_history_boundary(self):
        raw = feed(event("a", start="DTSTART;VALUE=DATE:20260417", extra="DTEND;VALUE=DATE:20260418\r\n"),
                   metadata="X-WR-TIMEZONE:America/New_York\r\n")
        self.assertEqual(self.run_job(raw, "history")["create"], 1)
        converted = Calendar.from_ical(next(iter(self.cloud.objects.values())).data).walk("VEVENT")[0]
        self.assertEqual(str(converted[sync.SOURCE_ZONE]), "America/New_York")

    def test_out_of_scope_remote_events_are_untouched(self):
        self.run_job(feed(event("history", start="DTSTART:20260501T090000Z")), "history")
        before = copy.deepcopy(self.cloud.objects)
        counts = copy.deepcopy(self.state.data["events"]["history"]["missing"])
        for _ in range(3):
            result = self.run_job(feed())
            self.assertEqual(result["pending_delete"], 0)
            self.assertFalse(result["delete_blocked"])
        self.assertEqual(before, self.cloud.objects)
        self.assertEqual(counts, self.state.data["events"]["history"]["missing"])

    def test_aging_out_of_fast_window_is_not_deletion(self):
        self.run_job(feed(event("a", start="DTSTART:20261012T090000Z")))
        self.now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
        self.current = google_api.windows(self.now, self.cfg.sync_timezone)
        result = self.run_job(feed())
        self.assertEqual(result["pending_delete"], 0)
        self.assertEqual(len(self.cloud.objects), 1)

    def test_moved_to_history_is_protected_then_updated_by_daily_job(self):
        self.run_job(feed(event("a"), event("b")))
        for _ in range(3):
            self.assertEqual(self.run_job(feed(event("b"), event("a", start="DTSTART:20260501T090000Z")))["pending_delete"], 0)
        result = self.run_job(feed(event("a", start="DTSTART:20260501T090000Z"), event("b")), "history")
        self.assertEqual(result["update"], 1)
        self.assertEqual(len(self.cloud.objects), 2)

    def test_overlap_jobs_do_not_double_count_deletion_confirmations(self):
        self.run_job(feed(event("a"), event("b")))
        self.assertEqual(self.run_job(feed(event("b")))["delete"], 0)
        self.assertEqual(self.run_job(feed(event("b")), "history")["delete"], 0)
        self.assertEqual(self.state.data["events"]["a"]["missing"], {"fast": 1, "history": 0})
        self.assertEqual(self.run_job(feed(event("b")))["delete"], 1)

    def test_daily_success_persists_and_restart_does_not_repeat_same_day(self):
        self.run_job(feed(event()), "history")
        self.state.close()
        self.state = sync.State(self.cfg.state_file)
        self.assertEqual([w.name for w in sync.due_windows(self.cfg, self.state, NOW)], ["fast"])
        tomorrow = datetime(2026, 10, 18, 16, tzinfo=timezone.utc)
        self.assertEqual([w.name for w in sync.due_windows(self.cfg, self.state, tomorrow)], ["fast", "history"])

    def test_failed_history_remains_due_and_dry_run_does_not_mark_success(self):
        self.run_job(feed(event()), "history", dry_run=True)
        self.assertIsNone(self.state.data["history_day"])
        google = Mock()
        google.refresh.side_effect = google_api.GoogleError("fake error")
        with self.assertRaises(google_api.GoogleError):
            sync.sync_cycle(self.cfg, self.state, now=NOW, google=google)
        self.assertIsNone(self.state.data["history_day"])

    def test_recurring_old_master_matches_future_and_preserves_all_day_until(self):
        raw = feed(event("series", start="DTSTART:20200101T090000Z", extra="RRULE:FREQ=WEEKLY\r\n"),
                   event("ended", start="DTSTART;VALUE=DATE:20200101", extra="RRULE:FREQ=DAILY;UNTIL=20200110\r\n"))
        self.assertEqual(self.run_job(raw)["create"], 1)
        self.assertIn("series", self.state.data["events"])

    def test_half_year_uses_calendar_months_and_local_timezone(self):
        result = google_api.windows(datetime(2026, 8, 31, 1, tzinfo=timezone.utc), "Asia/Shanghai")
        self.assertEqual(result["history"].start.isoformat(), "2026-02-28T00:00:00+08:00")
        self.assertEqual(result["history"].end.isoformat(), "2026-08-24T09:00:00+08:00")

    def test_history_final_save_failure_keeps_task_due(self):
        normal_save = self.state.save
        def failing_save():
            if self.state.data.get("history_day"):
                raise OSError("fake disk failure")
            normal_save()
        self.state.save = failing_save
        with self.assertRaises(OSError):
            self.run_job(feed(event()), "history")
        self.assertIsNone(self.state.data["history_day"])
        self.assertIsNone(json.loads(self.cfg.state_file.read_text())["history_day"])

    def test_event_end_at_window_start_is_outside_but_next_recurrence_is_inside(self):
        raw = feed(event("series", start="DTSTART:20261004T110000Z",
                         extra="DTEND:20261004T120000Z\r\nRRULE:FREQ=WEEKLY;COUNT=5\r\n"))
        self.assertTrue(sync.calendar_in_window(raw, self.current["fast"], self.cfg.sync_timezone))



class OAuthHelperTests(unittest.TestCase):
    def test_refresh_token_saved_locally_without_changing_other_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            original = "ICLOUD_APP_PASSWORD=private-fake\nGOOGLE_CLIENT_SECRET=fake-secret\nGOOGLE_REFRESH_TOKEN=\n"
            path.write_text(original)
            authorize_google.save_refresh_token(path, "FAKE_REFRESH/new-token", original)
            self.assertEqual(path.read_text(), original.replace("GOOGLE_REFRESH_TOKEN=", "GOOGLE_REFRESH_TOKEN=FAKE_REFRESH/new-token"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_intervening_env_edit_and_newline_token_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("KEY=updated\n")
            for token, original in (("FAKE", "KEY=old\n"), ("FAKE\nINJECT=1", path.read_text())):
                with self.assertRaises(google_api.GoogleError):
                    authorize_google.save_refresh_token(path, token, original)
            self.assertEqual(path.read_text(), "KEY=updated\n")


if __name__ == "__main__":
    unittest.main()
