"""Incremental protocol and real cache/reconciliation integration without credentials."""
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests
from icalendar import Calendar

import google_api
import sync
from test_google_api import item, page
from test_sync import FakeICloud

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
METADATA = {"id": "fake@example.com", "timeZone": "Asia/Shanghai"}


def response(items, token="next", **kwargs):
    return page(items, nextSyncToken=token, **kwargs)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.cfg = sync.Config("fake", "fake", google_client_id="FAKE_ID",
                               google_client_secret="FAKE_SECRET", google_refresh_token="FAKE_REFRESH")
        self.google = google_api.Google(self.cfg)
        self.google.metadata = METADATA.copy()
        self.google.metadata_at = sync.time.monotonic()
        self.addCleanup(self.google.close)

    def test_all_pages_keep_original_sync_token_and_no_date_filters(self):
        self.google.get = Mock(side_effect=[page([], nextPageToken="page2"), response([item()], "finished")])
        items, token, settings = self.google.changes(METADATA["id"], "original")
        self.assertEqual(set(items), {"id-a"})
        self.assertEqual(token, "finished")
        calls = self.google.get.call_args_list
        for call in calls:
            params = call.kwargs["params"]
            self.assertEqual(params["syncToken"], "original")
            self.assertEqual(params["singleEvents"], "false")
            self.assertEqual(params["showDeleted"], "true")
            self.assertFalse(set(params) & {"timeMin", "timeMax", "iCalUID", "updatedMin"})
        self.assertEqual(calls[1].kwargs["params"]["pageToken"], "page2")

    def test_full_read_has_no_sync_token_or_time_limits(self):
        self.google.get = Mock(return_value=response([item()]))
        result = self.google.refresh(None)
        self.assertEqual(result["token"], "next")
        self.assertNotIn("syncToken", self.google.get.call_args.kwargs["params"])

    def test_failed_pagination_never_returns_partial_result(self):
        scenarios = [
            [page([item()], nextPageToken="n"), requests.Timeout("FAKE_SECRET")],
            [page([item()], nextPageToken="n"), page([], nextPageToken="n")],
            [page([item()], nextPageToken="n"), response([item(summary="conflict")])],
            [page([])], [response([], accessRole="freeBusyReader")],
            [response([], timeZone="Wrong/Zone")],
            [page([], nextPageToken="n"), response([], timeZone="UTC")],
        ]
        for replies in scenarios:
            with self.subTest(replies=replies):
                self.google.get = Mock(side_effect=replies)
                with self.assertRaises((google_api.GoogleError, requests.Timeout)):
                    self.google.changes(METADATA["id"], "before")

    def test_page_limit_fails_instead_of_truncating(self):
        self.cfg.max_pages = 1
        self.google.get = Mock(return_value=page([item()], nextPageToken="more"))
        with self.assertRaises(google_api.GoogleError):
            self.google.refresh(None)

    def test_410_rebuild_replaces_source_cache_without_using_partial_delta(self):
        self.google.get = Mock(return_value=response([item()], "old-token"))
        old = self.google.refresh(None)
        self.google.get = Mock(side_effect=[google_api.SyncTokenExpired("expired"), response([item("b")], "rebuilt")])
        result = self.google.refresh(old)
        self.assertEqual(set(result["items"]), {"id-b"})
        self.assertEqual(result["token"], "rebuilt")
        self.assertIn("syncToken", self.google.get.call_args_list[0].kwargs["params"])
        self.assertNotIn("syncToken", self.google.get.call_args_list[1].kwargs["params"])
        self.assertEqual(set(old["items"]), {"id-a"})

    def test_metadata_cached_but_identity_checked_before_reusing_token(self):
        self.google.get = Mock(return_value=response([]))
        self.google.refresh(None)
        self.google.refresh(None)
        self.assertEqual(self.google.get.call_count, 2)
        self.google.metadata = None
        self.google.get.return_value = {**METADATA, "id": "other@example.com"}
        previous = {"calendar_id": METADATA["id"], "requested_id": "primary"}
        with self.assertRaises(google_api.GoogleError):
            self.google.refresh(previous)
        self.google.get.assert_called_with("primary")

    def test_complete_recurrence_family_survives_unrelated_empty_deltas(self):
        master = item(recurrence=["RRULE:FREQ=WEEKLY"])
        cancelled = {"id": "old-cancel", "status": "cancelled", "recurringEventId": master["id"],
                     "originalStartTime": {"dateTime": "2025-01-01T09:00:00Z"}}
        moved = item("moved", recurringEventId=master["id"], originalStartTime=master["start"])
        self.google.get = Mock(side_effect=[response([master, cancelled, moved]), response([])])
        cache = self.google.refresh(None)
        result = self.google.refresh(cache)
        events = Calendar.from_ical(google_api.render_cache(result)).walk("VEVENT")
        self.assertEqual(len(events), 3)
        self.assertEqual({str(event["UID"]) for event in events}, {"a"})
        self.assertEqual(sum(str(event["STATUS"]) == "CANCELLED" for event in events), 1)
        self.assertEqual(self.google.get.call_count, 2)  # No per-series queries.

    def test_master_tombstone_removes_cached_active_and_cancelled_exceptions(self):
        master = item(recurrence=["RRULE:FREQ=WEEKLY"])
        child = item("child", recurringEventId=master["id"], originalStartTime=master["start"])
        self.google.get = Mock(side_effect=[response([master, child]), response([{"id": master["id"], "status": "cancelled"}])])
        cache = self.google.refresh(self.google.refresh(None))
        self.assertEqual(cache["items"], {})
        self.assertFalse(Calendar.from_ical(google_api.render_cache(cache)).walk("VEVENT"))

    def test_exception_cancellation_updates_only_exception_and_keeps_master(self):
        master = item(recurrence=["RRULE:FREQ=WEEKLY"])
        child = item("child", recurringEventId=master["id"], originalStartTime=master["start"])
        cancelled = {key: child[key] for key in ("id", "recurringEventId", "originalStartTime")}
        cancelled["status"] = "cancelled"
        self.google.get = Mock(side_effect=[response([master, child]), response([cancelled])])
        cache = self.google.refresh(self.google.refresh(None))
        events = Calendar.from_ical(google_api.render_cache(cache)).walk("VEVENT")
        self.assertEqual(len(events), 2)
        self.assertEqual(str(events[1]["STATUS"]), "CANCELLED")

    def test_series_timezone_and_all_day_reminders_are_preserved(self):
        master = item(recurrence=["RRULE:FREQ=WEEKLY"],
                      start={"dateTime": "2026-10-18T09:00:00-07:00", "timeZone": "America/Los_Angeles"},
                      end={"dateTime": "2026-10-18T10:00:00-07:00", "timeZone": "America/Los_Angeles"})
        cancelled = {"id": "cancel", "status": "cancelled", "recurringEventId": master["id"],
                     "originalStartTime": {"dateTime": "2026-10-25T16:00:00Z"}}
        all_day = item("all-day", start={"date": "2026-10-18"}, end={"date": "2026-10-20"})
        self.google.get = Mock(return_value=response([master, cancelled, all_day],
                                    defaultReminders=[{"method": "popup", "minutes": 15}]))
        events = Calendar.from_ical(google_api.render_cache(self.google.refresh(None))).walk("VEVENT")
        exception = next(event for event in events if "RECURRENCE-ID" in event)
        self.assertEqual(exception["RECURRENCE-ID"].params["TZID"], "America/Los_Angeles")
        self.assertEqual(exception["RECURRENCE-ID"].dt.hour, 9)
        all_day = next(event for event in events if str(event["UID"]) == "all-day")
        self.assertEqual(all_day["DTEND"].dt.isoformat(), "2026-10-20")
        self.assertEqual(len(all_day.walk("VALARM")), 1)

    def test_invalid_event_data_and_orphan_exception_reject_cache(self):
        for bad in (item(start={"dateTime": "bad"}), item(start=None), item(status="unknown"),
                    item(recurrence=["RRULE:FREQ=WRONG"]), item(recurrence=["ATTENDEE:fake"]),
                    item("orphan", recurringEventId="missing", originalStartTime=item()["start"])):
            self.google.get = Mock(return_value=response([bad]))
            with self.subTest(bad=bad), self.assertRaises(google_api.GoogleError):
                sync.source_events(google_api.render_cache(self.google.refresh(None)), "test")

    def test_token_refresh_cached_and_secrets_kept_out_of_url(self):
        reply = Mock(status_code=200)
        reply.json.return_value = {"access_token": "FAKE_ACCESS", "expires_in": 3600, "scope": google_api.SCOPE}
        self.google.session.post = Mock(return_value=reply)
        self.assertEqual(self.google.token(), "FAKE_ACCESS")
        self.assertEqual(self.google.token(), "FAKE_ACCESS")
        self.google.session.post.assert_called_once()
        self.assertEqual(self.google.session.post.call_args.args[0], google_api.TOKEN_URL)
        self.google.token(force=True)
        self.assertEqual(self.google.session.post.call_count, 2)

    def test_refresh_failure_does_not_expose_response_body(self):
        self.google.session.post = Mock(return_value=Mock(status_code=400, text="FAKE_SECRET"))
        with self.assertRaises(google_api.GoogleError) as raised:
            self.google.token()
        self.assertNotIn("FAKE_SECRET", str(raised.exception))

    def test_401_refresh_once_410_and_size_limit(self):
        def reply(status, data=b'{"id":"fake"}'):
            result = MagicMock(status_code=status)
            result.__enter__.return_value = result
            result.iter_content.return_value = [data]
            return result
        self.google.token = Mock(return_value="FAKE_ACCESS")
        self.google.session.get = Mock(side_effect=[reply(401), reply(200)])
        self.assertEqual(self.google.get("fake"), {"id": "fake"})
        self.assertEqual(self.google.token.call_args.kwargs, {"force": True})
        self.google.session.get = Mock(return_value=reply(410))
        with self.assertRaises(google_api.SyncTokenExpired):
            self.google.get("fake")
        self.cfg.max_bytes = 1
        self.google.session.get = Mock(return_value=reply(200))
        with self.assertRaises(google_api.GoogleError):
            self.google.get("fake")


class IncrementalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = sync.Config("fake", "fake", state_file=Path(self.tmp.name) / "state.json")
        self.state = sync.State(self.cfg.state_file)
        self.addCleanup(lambda: self.state.close())
        self.cloud = FakeICloud()
        self.google = google_api.Google(self.cfg)
        self.addCleanup(self.google.close)
        self.google.metadata = METADATA.copy()
        self.google.metadata_at = sync.time.monotonic()
        self.google.get = Mock()

    def run_cycle(self, items, now=NOW, **kwargs):
        self.google.get.return_value = response(copy.deepcopy(items), "token-" + str(self.google.get.call_count))
        return sync.sync_cycle(self.cfg, self.state, google=self.google, now=now,
                               destination_factory=lambda _: self.cloud, **kwargs)

    def test_no_change_polls_google_once_and_never_opens_icloud(self):
        result = self.run_cycle([item()])
        self.assertEqual(result["windows"][0]["create"], 1)
        self.assertEqual(sum(op[0] == "inventory" for op in self.cloud.operations), 1)
        self.cloud.operations.clear()
        self.run_cycle([])
        self.assertEqual(self.cloud.operations, [])
        self.assertEqual(self.google.get.call_count, 2)

    def test_one_changed_event_reads_only_that_resource(self):
        self.run_cycle([item(), item("b")])
        self.cloud.operations.clear()
        result = self.run_cycle([item(summary="updated")])
        self.assertEqual(result["windows"][0]["update"], 1)
        self.assertEqual([op[0] for op in self.cloud.operations], ["connect", "get", "put"])
        self.assertEqual(self.cloud.operations[0][1], self.cloud.url)
        self.assertEqual(len(self.cloud.objects), 2)

    def test_restart_reuses_persisted_token_and_daily_audit_date(self):
        self.run_cycle([item()])
        before_token = self.state.data["source"]["token"]
        self.state.close()
        self.state = sync.State(self.cfg.state_file)
        self.google.metadata = None
        self.google.get = Mock(side_effect=[METADATA.copy(), response([], "resumed")])
        self.cloud.operations.clear()
        sync.sync_cycle(self.cfg, self.state, google=self.google, now=NOW,
                        destination_factory=lambda _: self.cloud)
        self.assertEqual(self.google.get.call_args.kwargs["params"]["syncToken"], before_token)
        self.assertEqual(self.cloud.operations, [])

    def test_daily_audit_repairs_manual_changes_and_deletions_once(self):
        self.run_cycle([item(), item("b")])
        objects = list(self.cloud.objects.values())
        objects[0].data = objects[0].data.replace(b"Title", b"Phone change")
        del self.cloud.objects[objects[1].url]
        self.cloud.operations.clear()
        self.run_cycle([])
        self.assertEqual(self.cloud.operations, [])
        result = self.run_cycle([], NOW + timedelta(days=1))
        self.assertTrue(result["audit"])
        self.assertEqual(result["windows"][0]["update"], 1)
        self.assertEqual(result["windows"][0]["create"], 1)
        self.assertEqual(sum(op[0] == "inventory" for op in self.cloud.operations), 1)
        self.cloud.operations.clear()
        self.run_cycle([], NOW + timedelta(days=1, minutes=10))
        self.assertEqual(self.cloud.operations, [])

    def test_daily_audit_protects_foreign_uid_and_removed_ownership(self):
        self.run_cycle([item()])
        remote = next(iter(self.cloud.objects.values()))
        remote.data = remote.data.replace(b"X-GICS-OWNER:google-to-icloud\r\n", b"")
        with self.assertRaises(sync.SyncError):
            self.run_cycle([], NOW + timedelta(days=1))
        self.assertEqual(self.state.data["audit_day"], "2026-10-18")

    def test_history_changes_wait_for_daily_job_and_old_events_stay_untouched(self):
        history = item("history", start={"dateTime": "2026-05-01T09:00:00Z"},
                       end={"dateTime": "2026-05-01T10:00:00Z"})
        old = item("old", start={"dateTime": "2020-01-01T09:00:00Z"}, end={"dateTime": "2020-01-01T10:00:00Z"})
        self.run_cycle([item(), history, old])
        self.assertEqual(set(self.state.data["events"]), {"a", "history"})
        self.cloud.operations.clear()
        self.run_cycle([{**history, "summary": "edited"}])
        self.assertEqual(self.cloud.operations, [])
        result = self.run_cycle([], NOW + timedelta(days=1))
        self.assertEqual(result["windows"][1]["update"], 1)
        self.assertNotIn("old", self.state.data["events"])

    def test_move_out_of_fast_is_retained_until_history_job(self):
        self.run_cycle([item(), item("b")])
        moved = item(start={"dateTime": "2026-05-01T09:00:00Z"}, end={"dateTime": "2026-05-01T10:00:00Z"})
        self.cloud.operations.clear()
        result = self.run_cycle([moved])
        self.assertEqual(result["windows"][0]["pending_delete"], 0)
        self.assertEqual(self.cloud.operations, [])
        self.assertEqual(self.run_cycle([], NOW + timedelta(days=1))["windows"][1]["update"], 1)

    def test_deleted_event_needs_two_successful_polls_even_with_empty_next_delta(self):
        self.run_cycle([item(), item("b")])
        self.cloud.operations.clear()
        first = self.run_cycle([{"id": "id-a", "status": "cancelled"}])
        self.assertEqual(first["windows"][0]["delete"], 0)
        self.assertEqual(self.cloud.operations, [])
        second = self.run_cycle([])
        self.assertEqual(second["windows"][0]["delete"], 1)
        self.assertEqual([op[0] for op in self.cloud.operations], ["connect", "get", "delete"])
        self.assertNotIn("a", self.state.data["events"])

    def test_history_deletion_needs_checks_on_two_days(self):
        historical = item("history", start={"dateTime": "2026-05-01T09:00:00Z"},
                          end={"dateTime": "2026-05-01T10:00:00Z"})
        other = {**historical, "id": "id-other", "iCalUID": "other"}
        self.run_cycle([historical, other])
        self.run_cycle([{"id": "id-history", "status": "cancelled"}])
        self.assertEqual(self.state.data["events"]["history"]["missing"]["history"], 0)
        first = self.run_cycle([], NOW + timedelta(days=1))
        self.assertEqual(first["windows"][1]["delete"], 0)
        second = self.run_cycle([], NOW + timedelta(days=2))
        self.assertEqual(second["windows"][1]["delete"], 1)

    def test_410_failed_rebuild_or_invalid_delta_never_changes_state_or_icloud(self):
        self.run_cycle([item(), item("b")])
        before = self.cfg.state_file.read_bytes()
        self.cloud.operations.clear()
        for replies in ([google_api.SyncTokenExpired("expired"), requests.Timeout("private")],
                        [response([item(start=None)])],
                        [page([{"id": "id-a", "status": "cancelled"}], nextPageToken="more"), requests.Timeout("fake")]):
            self.google.get = Mock(side_effect=replies)
            with self.assertRaises((sync.SyncError, requests.Timeout)):
                sync.sync_cycle(self.cfg, self.state, google=self.google, now=NOW,
                                destination_factory=lambda _: self.cloud)
            self.assertEqual(self.cfg.state_file.read_bytes(), before)
            self.assertEqual(self.cloud.operations, [])

    def test_410_success_rebuild_uses_normal_two_check_delete_protection(self):
        self.run_cycle([item(), item("b")])
        self.google.get = Mock(side_effect=[google_api.SyncTokenExpired("expired"), response([item("b")])])
        result = sync.sync_cycle(self.cfg, self.state, google=self.google, now=NOW,
                                 destination_factory=lambda _: self.cloud)
        self.assertEqual(result["windows"][0]["pending_delete"], 1)
        self.assertEqual(len(self.cloud.objects), 2)

    def test_icloud_write_failure_keeps_source_delta_pending_for_next_poll(self):
        self.run_cycle([item()])
        self.cloud.fail_put = True
        with self.assertRaises(sync.SyncError):
            self.run_cycle([item(summary="pending change")])
        self.assertEqual(self.state.data["source"]["items"]["id-a"]["summary"], "pending change")
        self.cloud.fail_put = False
        self.assertEqual(self.run_cycle([])["windows"][0]["update"], 1)

    def test_interrupted_new_write_is_recovered_without_full_audit(self):
        self.run_cycle([item()])
        normal_put = self.cloud.put
        def interrupted(*args):
            normal_put(*args)
            raise requests.Timeout("fake interrupted readback")
        self.cloud.put = interrupted
        with self.assertRaises(requests.Timeout):
            self.run_cycle([item("new")])
        self.cloud.put = normal_put
        self.cloud.operations.clear()
        result = self.run_cycle([])
        self.assertEqual(result["windows"][0]["create"], 0)
        self.assertEqual(result["windows"][0]["update"], 1)
        self.assertNotIn("inventory", [op[0] for op in self.cloud.operations])
        self.assertEqual(len(self.cloud.objects), 2)

    def test_foreign_resource_cannot_be_deleted_by_targeted_path(self):
        self.run_cycle([item(), item("b")])
        self.run_cycle([{"id": "id-a", "status": "cancelled"}])
        remote = next(obj for obj in self.cloud.objects.values() if b"X-GICS-SOURCE-UID:a" in obj.data)
        remote.data = remote.data.replace(b"X-GICS-OWNER:google-to-icloud\r\n", b"")
        before = copy.deepcopy(self.cloud.objects)
        with self.assertRaises(sync.SyncError):
            self.run_cycle([])
        self.assertEqual(self.cloud.objects, before)

    def test_default_reminder_change_is_applied_even_with_no_event_deltas(self):
        self.run_cycle([item()])
        self.google.get.return_value = response([], defaultReminders=[{"method": "popup", "minutes": 30}])
        result = sync.sync_cycle(self.cfg, self.state, google=self.google, now=NOW,
                                 destination_factory=lambda _: self.cloud)
        self.assertEqual(result["windows"][0]["update"], 1)
        self.assertEqual(len(Calendar.from_ical(next(iter(self.cloud.objects.values())).data).walk("VALARM")), 1)

    def test_dry_run_keeps_source_token_and_daily_dates_unchanged(self):
        self.run_cycle([item()])
        before = self.cfg.state_file.read_bytes()
        data = copy.deepcopy(self.state.data)
        remote = copy.deepcopy(self.cloud.objects)
        self.run_cycle([item(summary="preview")], NOW + timedelta(days=1), dry_run=True)
        self.assertEqual(before, self.cfg.state_file.read_bytes())
        self.assertEqual(self.state.data, data)
        self.assertEqual(self.cloud.objects, remote)


if __name__ == "__main__":
    unittest.main()
