import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests
from icalendar import Calendar

import sync


def event(uid="a", summary="Meeting", extra="", start="DTSTART:20261018T090000Z"):
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20261018T000000Z\r\n"
            f"{start}\r\nSUMMARY:{summary}\r\n{extra}END:VEVENT\r\n")


def feed(*events, metadata=""):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//tests//EN\r\n" + metadata
            + "".join(events) + "END:VCALENDAR\r\n").encode()


class FakeICloud:
    def __init__(self):
        self.url = "https://p01-caldav.icloud.com/test/"
        self.objects = {}
        self.operations = []
        self.fail_put = False
        self.fail_inventory = False

    def connect(self, cached_url=None):
        self.operations.append(("connect", cached_url))

    def close(self):
        pass

    def inventory(self):
        self.operations.append(("inventory",))
        if self.fail_inventory:
            raise sync.SyncError("test inventory failure")
        return list(self.objects.values())

    def get_remote(self, uid, url=None):
        self.operations.append(("get", uid))
        return self.objects.get(url or self.url + uid + ".ics")

    def put(self, uid, data, previous):
        if self.fail_put:
            raise sync.SyncError("test put failure")
        url = previous.url if previous else self.url + uid + ".ics"
        obj = sync.Remote(url, '"v1"', data)
        self.objects[url] = obj
        self.operations.append(("put", uid))
        return obj

    def delete(self, remote):
        self.operations.append(("delete", remote.url))
        del self.objects[remote.url]


NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def cache(calendar_id="fake@example.com"):
    return {"calendar_id": calendar_id, "requested_id": "primary", "token": "FAKE_SYNC",
            "items": {}, "defaults": [], "timezone": "Asia/Shanghai"}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = sync.Config("fake", "fake",
                               state_file=Path(self.tmp.name) / "state.json")
        self.state = sync.State(self.cfg.state_file)
        self.addCleanup(lambda: self.state.close())
        self.cloud = FakeICloud()
        self.calendar_id = "fake@example.com"

    def run_sync(self, data, **kwargs):
        google = Mock()
        google.refresh.return_value = cache(self.calendar_id)
        with patch("sync.render_cache", return_value=data):
            result = sync.sync_cycle(self.cfg, self.state, google=google, now=NOW,
                                     selection="fast", destination_factory=lambda _: self.cloud, **kwargs)
        return result["windows"][0]

    def test_create_idempotent_update_and_manual_edit_repair(self):
        self.assertEqual(self.run_sync(feed(event()))["create"], 1)
        self.assertEqual(self.run_sync(feed(event()))["unchanged"], 1)
        self.assertEqual(self.run_sync(feed(event(summary="Changed")))["update"], 1)
        obj = next(iter(self.cloud.objects.values()))
        obj.data = obj.data.replace(b"Changed", b"Phone edit")
        self.assertEqual(self.run_sync(feed(event(summary="Changed")))["update"], 1)

    def test_remote_deletion_recreated(self):
        self.run_sync(feed(event()))
        self.cloud.objects.clear()
        self.assertEqual(self.run_sync(feed(event()))["create"], 1)

    def test_delete_requires_two_complete_source_checks_and_ignores_foreign(self):
        foreign = sync.Remote(self.cloud.url + "foreign.ics", '"f"', feed(event("personal")))
        self.cloud.objects[foreign.url] = foreign
        self.run_sync(feed(event("a"), event("b")))
        self.assertEqual(self.run_sync(feed(event("b")))["delete"], 0)
        self.assertEqual(self.run_sync(feed(event("b")))["delete"], 1)
        self.assertIn(foreign.url, self.cloud.objects)

    def test_parse_failure_does_not_delete_or_advance_state(self):
        self.run_sync(feed(event("a"), event("b")))
        before = self.cfg.state_file.read_bytes()
        for bad in (b"<html>login</html>", feed(event())[:-20], feed(event("")),
                    feed(event(extra="DTSTART:20261019T090000Z\r\n")),
                    feed(event(extra="RRULE:FREQ=WRONG\r\n"))):
            with self.subTest(bad=bad):
                with self.assertRaises(sync.SyncError):
                    self.run_sync(bad)
                self.assertEqual(before, self.cfg.state_file.read_bytes())

    def test_source_download_failure_never_opens_destination(self):
        dest = Mock()
        google = Mock()
        google.refresh.side_effect = requests.Timeout("FAKE")
        with self.assertRaises(requests.Timeout):
            sync.sync_cycle(self.cfg, self.state, now=NOW, google=google, destination_factory=dest)
        dest.assert_not_called()

    def test_inventory_failure_never_changes_state(self):
        self.run_sync(feed(event()))
        before = self.cfg.state_file.read_bytes()
        self.cloud.fail_inventory = True
        with self.assertRaises(sync.SyncError):
            self.run_sync(feed())
        self.assertEqual(before, self.cfg.state_file.read_bytes())

    def test_empty_source_latches_even_after_many_polls(self):
        self.run_sync(feed(event()))
        for _ in range(4):
            stats = self.run_sync(feed())
            self.assertTrue(stats["delete_blocked"])
            self.assertEqual(stats["delete"], 0)
        self.assertEqual(self.run_sync(feed(), allow_large_delete=True)["delete"], 1)

    def test_mass_delete_count_and_persistent_latch(self):
        self.cfg.delete_max_count = 2
        self.run_sync(feed(*(event(str(i)) for i in range(6))))
        for _ in range(3):
            self.assertTrue(self.run_sync(feed(event("0")))["delete_blocked"])
        # Returning most events must not silently unlock an earlier bulk-delete alarm.
        self.assertTrue(self.run_sync(feed(*(event(str(i)) for i in range(5))))["delete_blocked"])
        self.assertFalse(self.run_sync(feed(*(event(str(i)) for i in range(6))))["delete_blocked"])

    def test_mass_delete_ratio(self):
        self.cfg.delete_ratio_min_count = 2
        self.run_sync(feed(*(event(str(i)) for i in range(4))))
        self.assertTrue(self.run_sync(feed(event("0")))["delete_blocked"])

    def test_mass_delete_latch_survives_restart(self):
        self.run_sync(feed(event()))
        self.run_sync(feed())
        self.state.close()
        self.state = sync.State(self.cfg.state_file)
        self.assertTrue(self.state.data["deletion_latches"]["fast"])

    def test_delete_disabled(self):
        self.run_sync(feed(event("a"), event("b")))
        self.cfg.sync_delete = False
        for _ in range(3):
            self.assertEqual(self.run_sync(feed(event("b")))["delete"], 0)
        self.assertEqual(len(self.cloud.objects), 2)

    def test_upsert_failure_prevents_deletions(self):
        self.run_sync(feed(event("a"), event("b")))
        self.run_sync(feed(event("b")))
        self.cloud.fail_put = True
        with self.assertRaises(sync.SyncError):
            self.run_sync(feed(event("b", summary="changed")))
        self.assertEqual(len(self.cloud.objects), 2)

    def test_dry_run_does_not_mutate_state_remote_or_counters(self):
        self.run_sync(feed(event("a"), event("b")))
        before = self.cfg.state_file.read_bytes()
        memory = copy.deepcopy(self.state.data)
        objects = copy.deepcopy(self.cloud.objects)
        for _ in range(3):
            stats = self.run_sync(feed(event("b")), dry_run=True)
            self.assertEqual(stats["delete"], 0)
        self.assertEqual(before, self.cfg.state_file.read_bytes())
        self.assertEqual(memory, self.state.data)
        self.assertEqual(objects, self.cloud.objects)

    def test_first_dry_run_leaves_no_state_or_binding(self):
        before = copy.deepcopy(self.state.data)
        self.assertEqual(self.run_sync(feed(event()), dry_run=True)["create"], 1)
        self.assertFalse(self.cfg.state_file.exists())
        self.assertEqual(self.state.data, before)
        self.assertEqual(self.cloud.objects, {})

    def test_write_then_connection_loss_recovers_by_uid(self):
        normal_put = self.cloud.put
        def interrupted(*args):
            normal_put(*args)
            raise requests.Timeout("fake readback interrupted")
        self.cloud.put = interrupted
        with self.assertRaises(requests.Timeout):
            self.run_sync(feed(event()))
        self.cloud.put = normal_put
        self.assertEqual(self.run_sync(feed(event()))["create"], 0)
        self.assertEqual(len(self.cloud.objects), 1)

    def test_server_normalization_does_not_cause_repeated_updates(self):
        normal_put = self.cloud.put
        def normalized(uid, data, previous):
            data = data.replace(b"BEGIN:VEVENT\r\n", b"BEGIN:VEVENT\r\nX-APPLE-TEST:NORMALIZED\r\n")
            return normal_put(uid, data, previous)
        self.cloud.put = normalized
        self.run_sync(feed(event()))
        self.assertEqual(self.run_sync(feed(event()))["unchanged"], 1)

    def test_restart_and_lost_state_recover_without_duplicates(self):
        self.run_sync(feed(event()))
        self.state.close()
        self.state = sync.State(self.cfg.state_file)
        self.assertEqual(self.run_sync(feed(event()))["unchanged"], 1)
        self.state.close()
        self.cfg.state_file.unlink()
        self.state = sync.State(self.cfg.state_file)
        self.assertEqual(self.run_sync(feed(event()))["create"], 0)
        self.assertEqual(len(self.cloud.objects), 1)

    def test_binding_refuses_different_source_or_calendar(self):
        self.run_sync(feed(event()))
        self.calendar_id = "other@example.com"
        with self.assertRaises(sync.SyncError):
            self.run_sync(feed(event()))
        self.calendar_id = "fake@example.com"
        self.cloud.url += "other/"
        with self.assertRaises(sync.SyncError):
            self.run_sync(feed(event()))

    def test_unmanaged_target_uid_collision_is_never_overwritten(self):
        uid = sync.target_uid(self.cfg.sync_id, "a")
        obj = sync.Remote(self.cloud.url + "foreign.ics", '"f"', feed(event(uid)))
        self.cloud.objects[obj.url] = obj
        with self.assertRaises(sync.SyncError):
            self.run_sync(feed(event()))
        self.assertFalse(any(op[0] in ("put", "delete") for op in self.cloud.operations))

    def test_marker_removed_is_not_overwritten(self):
        self.run_sync(feed(event()))
        obj = next(iter(self.cloud.objects.values()))
        cal = Calendar.from_ical(obj.data)
        del cal.walk("VEVENT")[0][sync.OWNER]
        obj.data = cal.to_ical()
        with self.assertRaises(sync.SyncError):
            self.run_sync(feed(event()))

    def test_duplicate_remote_uid_aborts(self):
        self.run_sync(feed(event()))
        obj = copy.deepcopy(next(iter(self.cloud.objects.values())))
        obj.url += "copy"
        self.cloud.objects[obj.url] = obj
        with self.assertRaises(sync.SyncError):
            self.run_sync(feed(event()))

    def test_state_lock_and_corruption(self):
        with self.assertRaises(sync.SyncError):
            sync.State(self.cfg.state_file)
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text("{broken")
        with self.assertRaises(sync.SyncError):
            sync.State(bad)
        self.assertEqual(bad.read_text(), "{broken")

    def test_state_permissions_and_atomic_file(self):
        self.run_sync(feed(event()))
        self.assertEqual(self.cfg.state_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.cfg.state_file.read_text())["format"], "incremental-v1")
        self.assertFalse(list(Path(self.tmp.name).glob(".state-*")))


class ICSTests(unittest.TestCase):
    def test_recurrence_exceptions_and_cancelled_instance_share_uid(self):
        raw = feed(event(extra="RRULE:FREQ=WEEKLY;COUNT=5\r\nEXDATE:20261025T090000Z\r\n"),
                   event(summary="Moved", extra="RECURRENCE-ID:20261101T090000Z\r\n"),
                   event(summary="Cancelled", extra="RECURRENCE-ID:20261108T090000Z\r\nSTATUS:CANCELLED\r\n"))
        output = sync.source_events(raw, "test")
        self.assertEqual(len(output), 1)
        events = Calendar.from_ical(output["a"]).walk("VEVENT")
        self.assertEqual(len(events), 3)
        self.assertEqual({str(e["UID"]) for e in events}, {sync.target_uid("test", "a")})
        self.assertTrue(any("RRULE" in e for e in events))
        self.assertTrue(any("EXDATE" in e for e in events))

    def test_identical_duplicate_deduplicates_conflicting_duplicate_rejects(self):
        self.assertEqual(len(sync.source_events(feed(event(), event()), "test")), 1)
        with self.assertRaises(sync.SyncError):
            sync.source_events(feed(event(), event(summary="conflict")), "test")

    def test_orphan_recurrence_rejected(self):
        with self.assertRaises(sync.SyncError):
            sync.source_events(feed(event(extra="RECURRENCE-ID:20261018T090000Z\r\n")), "test")

    def test_cancelled_master_treated_as_absent(self):
        self.assertEqual(sync.source_events(feed(event(extra="STATUS:CANCELLED\r\n")), "test"), {})

    def test_all_day_exclusive_end(self):
        raw = feed(event(start="DTSTART;VALUE=DATE:20261018", extra="DTEND;VALUE=DATE:20261020\r\n"))
        e = Calendar.from_ical(sync.source_events(raw, "test")["a"]).walk("VEVENT")[0]
        self.assertEqual(e["DTSTART"].dt.isoformat(), "2026-10-18")
        self.assertEqual(e["DTEND"].dt.isoformat(), "2026-10-20")
        self.assertEqual(e["DTSTART"].params["VALUE"], "DATE")

    def test_timezone_and_dst_are_preserved(self):
        raw = feed(event(start="DTSTART;TZID=America/New_York:20261018T090000",
                         extra="DTEND;TZID=America/New_York:20261018T100000\r\nRRULE:FREQ=WEEKLY;COUNT=5\r\n"))
        cal = Calendar.from_ical(sync.source_events(raw, "test")["a"])
        self.assertEqual(cal.walk("VEVENT")[0]["DTSTART"].params["TZID"], "America/New_York")
        self.assertEqual(len(cal.walk("VTIMEZONE")), 1)

    def test_source_vtimezone_and_custom_tzid_roundtrip(self):
        timezone = ("BEGIN:VTIMEZONE\r\nTZID:Custom/Shanghai\r\nBEGIN:STANDARD\r\n"
                    "DTSTART:19700101T000000\r\nTZOFFSETFROM:+0800\r\nTZOFFSETTO:+0800\r\n"
                    "END:STANDARD\r\nEND:VTIMEZONE\r\n")
        raw = feed(timezone, event(start="DTSTART;TZID=Custom/Shanghai:20261018T090000"))
        cal = Calendar.from_ical(sync.source_events(raw, "test")["a"])
        self.assertEqual(str(cal.walk("VTIMEZONE")[0]["TZID"]), "Custom/Shanghai")
        self.assertEqual(cal.walk("VEVENT")[0]["DTSTART"].dt.utcoffset().total_seconds(), 28800)

    def test_utc_recurrence_until_and_all_day_rdate(self):
        raw = feed(event(extra="RRULE:FREQ=WEEKLY;UNTIL=20261231T235959Z\r\n"),
                   event("b", start="DTSTART;VALUE=DATE:20261018", extra="RDATE;VALUE=DATE:20261020,20261022\r\n"))
        self.assertEqual(len(sync.source_events(raw, "test")), 2)

    def test_floating_exdates_normalize_every_value(self):
        raw = feed(event(start="DTSTART:20261018T090000", extra="EXDATE:20261025T090000,20261101T090000\r\n"),
                   metadata="X-WR-TIMEZONE:Asia/Shanghai\r\n")
        cal = Calendar.from_ical(sync.source_events(raw, "test")["a"])
        values = cal.walk("VEVENT")[0]["EXDATE"].dts
        self.assertEqual([v.dt.utcoffset().total_seconds() for v in values], [28800, 28800])

    def test_floating_uses_feed_timezone_and_rejects_missing_default(self):
        raw = feed(event(start="DTSTART:20261018T090000"), metadata="X-WR-TIMEZONE:Asia/Shanghai\r\n")
        cal = Calendar.from_ical(sync.source_events(raw, "test")["a"])
        self.assertEqual(cal.walk("VEVENT")[0]["DTSTART"].dt.utcoffset().total_seconds(), 28800)
        with self.assertRaises(sync.SyncError):
            sync.source_events(feed(event(start="DTSTART:20261018T090000")), "test")

    def test_unknown_timezone_rejected(self):
        with self.assertRaises(sync.SyncError):
            sync.source_events(feed(event(start="DTSTART;TZID=Fake/Unknown:20261018T090000")), "test")

    def test_non_events_and_multiple_calendars_rejected(self):
        for raw in (feed("BEGIN:VTODO\r\nUID:a\r\nEND:VTODO\r\n"), feed() + feed()):
            with self.assertRaises(sync.SyncError):
                sync.source_events(raw, "test")

    def test_invitations_and_email_alarms_stripped_display_alarm_retained(self):
        raw = feed(event(extra="ORGANIZER:mailto:owner@example.com\r\nATTENDEE:mailto:guest@example.com\r\n"
                              "BEGIN:VALARM\r\nACTION:EMAIL\r\nTRIGGER:-PT10M\r\nEND:VALARM\r\n"
                              "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Reminder\r\nTRIGGER:-PT5M\r\nEND:VALARM\r\n"),
                   metadata="METHOD:PUBLISH\r\n")
        cal = Calendar.from_ical(sync.source_events(raw, "test")["a"])
        e = cal.walk("VEVENT")[0]
        self.assertNotIn("ORGANIZER", e)
        self.assertNotIn("ATTENDEE", e)
        self.assertNotIn("METHOD", cal)
        self.assertEqual([str(a["ACTION"]) for a in e.walk("VALARM")], ["DISPLAY"])


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.cfg = sync.Config("fake", "fake")
        self.cloud = sync.ICloud(self.cfg)
        self.cloud.url = "https://p01-caldav.icloud.com/home/calendar/"
        self.addCleanup(self.cloud.close)

    @staticmethod
    def response(status=200, data=b"", headers=None):
        return Mock(status_code=status, content=data, headers=headers or {})

    def test_conditional_put_create_update_get_and_delete(self):
        uid = sync.target_uid("test", "a")
        payload = sync.source_events(feed(event()), "test")["a"]
        self.cloud.session.request = Mock(side_effect=[self.response(201), self.response(data=payload, headers={"ETag": '"v1"'}),
                                                      self.response(204), self.response(data=payload, headers={"ETag": '"v2"'}),
                                                      self.response(204)])
        first = self.cloud.put(uid, payload, None)
        second = self.cloud.put(uid, payload, first)
        self.cloud.delete(second)
        calls = self.cloud.session.request.call_args_list
        self.assertEqual(calls[0].kwargs["headers"]["If-None-Match"], "*")
        self.assertEqual(calls[2].kwargs["headers"]["If-Match"], '"v1"')
        self.assertEqual(calls[4].kwargs["headers"]["If-Match"], '"v2"')
        self.assertFalse(calls[0].kwargs["allow_redirects"])

    def test_precondition_failure_and_weak_etag_abort(self):
        self.cloud.session.request = Mock(return_value=self.response(412))
        remote = sync.Remote(self.cloud.url + "a.ics", '"old"', feed(event()))
        with self.assertRaises(sync.SyncError):
            self.cloud.put("a", feed(event()), remote)
        with self.assertRaises(sync.SyncError):
            self.cloud.delete(remote)
        remote.etag = 'W/"weak"'
        with self.assertRaises(sync.SyncError):
            self.cloud.delete(remote)

    def test_targeted_get_requires_valid_resource_and_strong_etag(self):
        payload = feed(event())
        self.cloud.session.request = Mock(return_value=self.response(200, payload, {"ETag": '"v1"'}))
        result = self.cloud.get_remote("uid")
        self.assertEqual(result.url, self.cloud.url + "uid.ics")
        self.assertEqual(self.cloud.session.request.call_args.args[0], "GET")
        self.cloud.session.request.return_value = self.response(404)
        self.assertIsNone(self.cloud.get_remote("uid"))
        for reply in (self.response(500), self.response(200, payload),
                      self.response(200, payload, {"ETag": 'W/"weak"'})):
            self.cloud.session.request.return_value = reply
            with self.assertRaises(sync.SyncError):
                self.cloud.get_remote("uid")

    def test_resource_cannot_escape_calendar(self):
        for href in ("https://evil.example/a.ics", "../other.ics", self.cloud.url, "a.ics?query=1", "%2e%2e/other.ics"):
            with self.assertRaises(sync.SyncError):
                self.cloud.resource_url(href)

    def test_report_requires_complete_data_and_successful_propstats(self):
        payload = feed(event()).decode()
        xml = (f'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
               f'<d:response><d:href>/home/calendar/a.ics</d:href><d:propstat><d:prop>'
               f'<d:getetag>"v1"</d:getetag><c:calendar-data><![CDATA[{payload}]]></c:calendar-data>'
               f'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>').encode()
        self.cloud.session.request = Mock(return_value=self.response(207, xml))
        self.assertEqual(len(self.cloud.inventory()), 1)
        for bad in (xml.replace(b"200 OK", b"404 Not Found"), xml.replace(b'<d:getetag>"v1"</d:getetag>', b""), b"<html/>"):
            self.cloud.session.request.return_value = self.response(207, bad)
            with self.assertRaises(sync.SyncError):
                self.cloud.inventory()

    def test_icloud_collection_entry_without_event_data_is_not_a_partial_event(self):
        collection = ('<d:response><d:href>/home/calendar/</d:href><d:propstat><d:prop>'
                      '<d:getetag>"collection"</d:getetag></d:prop><d:status>HTTP/1.1 200 OK</d:status>'
                      '</d:propstat><d:propstat><d:prop><c:calendar-data/></d:prop>'
                      '<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>')
        resource = ('<d:response><d:href>/home/calendar/a.ics</d:href><d:propstat><d:prop>'
                    '<d:getetag>"event"</d:getetag><c:calendar-data><![CDATA[' + feed(event()).decode() +
                    ']]></c:calendar-data></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
        def report(body):
            xml = ('<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">' + body + '</d:multistatus>').encode()
            self.cloud.session.request = Mock(return_value=self.response(207, xml))
            return self.cloud.inventory()
        self.assertEqual(report(collection), [])
        self.assertEqual(len(report(collection + resource)), 1)
        for invalid in (collection.replace('404 Not Found', '403 Forbidden'),
                        collection.replace('<c:calendar-data/>', '<d:getetag/>'),
                        collection.replace('<c:calendar-data/>', '<c:calendar-data>unexpected</c:calendar-data>'),
                        collection.replace('/home/calendar/', '/home/calendar/incomplete.ics')):
            with self.subTest(invalid=invalid), self.assertRaises(sync.SyncError):
                report(invalid)

    def test_exact_calendar_name_no_create_or_ambiguous_selection(self):
        self.cloud.discovery = Mock()
        cal = Mock(url=self.cloud.url)
        cal.get_display_name.return_value = "Google"
        self.cloud.discovery.principal.return_value.calendars.return_value = [cal]
        self.cloud.connect()
        for calendars in ([], [cal, cal]):
            self.cloud.discovery.principal.return_value.calendars.return_value = calendars
            with self.assertRaises(sync.SyncError):
                self.cloud.connect()
        self.cloud.discovery.principal.return_value.make_calendar.assert_not_called()



class ConfigTests(unittest.TestCase):
    def test_environment_defaults_and_invalid_inputs(self):
        base = {"GOOGLE_CLIENT_ID": "fake", "GOOGLE_CLIENT_SECRET": "fake", "GOOGLE_REFRESH_TOKEN": "fake", "ICLOUD_USERNAME": "fake", "ICLOUD_APP_PASSWORD": "fake"}
        with patch.dict(os.environ, base, clear=True):
            self.assertEqual(sync.Config.from_env().interval, 600)
        for name, value in (("SYNC_DELETE", "maybe"), ("SYNC_INTERVAL", "0"), ("DELETE_CONFIRMATIONS", "1"),
                            ("DELETE_MAX_RATIO", "nan"), ("SYNC_TIMEZONE", "Wrong/Zone"), ("SYNC_ID", "bad/id")):
            with self.subTest(name=name), patch.dict(os.environ, dict(base, **{name: value}), clear=True):
                with self.assertRaises(sync.SyncError):
                    sync.Config.from_env()

    def test_literal_dollar_password_and_local_dotenv_format(self):
        from dotenv import dotenv_values
        from io import StringIO
        parsed = dotenv_values(stream=StringIO("ICLOUD_APP_PASSWORD=literal${DO_NOT_EXPAND}\nICLOUD_CALENDAR_NAME=Google\n"), interpolate=False)
        self.assertEqual(parsed["ICLOUD_APP_PASSWORD"], "literal${DO_NOT_EXPAND}")
        self.assertEqual(parsed["ICLOUD_CALENDAR_NAME"], "Google")

    def test_main_hides_exception_details_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = sync.Config("fake", "FAKE_SECRET_PASSWORD",
                              state_file=Path(folder) / "state.json")
            old_umask = os.umask(0o077)
            try:
                with patch.object(sys, "argv", ["sync.py", "--once"]), patch("sync.load_dotenv"), \
                     patch.object(sync.Config, "from_env", return_value=cfg), \
                     patch("sync.sync_cycle", side_effect=requests.Timeout("FAKE_SECRET_URL FAKE_SECRET_PASSWORD")), \
                     self.assertLogs("calendar-sync", level="ERROR") as captured:
                    self.assertEqual(sync.main(), 1)
                self.assertNotIn("FAKE_SECRET", "".join(captured.output))
                self.assertIn("Timeout", "".join(captured.output))
            finally:
                os.umask(old_umask)


if __name__ == "__main__":
    unittest.main()
