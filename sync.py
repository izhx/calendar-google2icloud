#!/usr/bin/env python3
"""Google Calendar API -> iCloud, with frequent and daily synchronization windows."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import random
import signal
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import caldav
import requests
from dateutil.rrule import rrulestr, rruleset
from dotenv import load_dotenv
from icalendar import Calendar
from lxml import etree
from google_api import Google, GoogleError, Window, windows, render_cache

LOG = logging.getLogger("calendar-sync")
OWNER = "X-GICS-OWNER"
SOURCE = "X-GICS-SOURCE-UID"
SOURCE_ZONE = "X-GICS-CALENDAR-TZ"
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}
REPORT = b'''<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
 <d:prop><d:getetag/><c:calendar-data/></d:prop>
 <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/>
 </c:comp-filter></c:filter>
</c:calendar-query>'''


SyncError = GoogleError


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def secure_url(value: str) -> None:
    p = urlsplit(value)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment:
        raise SyncError("URL 必须是无内嵌账户信息的 HTTPS 地址")


@dataclass
class Config:
    username: str = field(repr=False)
    password: str = field(repr=False)
    google_client_id: str = field(default="", repr=False)
    google_client_secret: str = field(default="", repr=False)
    google_refresh_token: str = field(default="", repr=False)
    google_calendar_id: str = "primary"
    calendar_name: str = "Google"
    sync_id: str = "google-to-icloud"
    icloud_url: str = "https://caldav.icloud.com/"
    state_file: Path = Path("data/state.json")
    interval: int = 600
    sync_timezone: str = "Asia/Shanghai"
    timeout: int = 30
    max_bytes: int = 20 * 1024 * 1024
    max_pages: int = 1000
    sync_delete: bool = True
    delete_confirmations: int = 2
    delete_max_count: int = 20
    delete_max_ratio: float = 0.5
    delete_ratio_min_count: int = 5

    @classmethod
    def from_env(cls) -> Config:
        def boolean(name: str, default: str) -> bool:
            value = os.getenv(name, default).lower()
            if value not in ("true", "false"):
                raise SyncError(f"{name} 必须为 true 或 false")
            return value == "true"

        try:
            cfg = cls(
                username=os.environ["ICLOUD_USERNAME"],
                password=os.environ["ICLOUD_APP_PASSWORD"],
                google_client_id=os.environ["GOOGLE_CLIENT_ID"],
                google_client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
                google_refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
                google_calendar_id=os.getenv("GOOGLE_CALENDAR_ID", "primary"),
                calendar_name=os.getenv("ICLOUD_CALENDAR_NAME", "Google"),
                sync_id=os.getenv("SYNC_ID", "google-to-icloud"),
                icloud_url=os.getenv("ICLOUD_CALDAV_URL", "https://caldav.icloud.com/"),
                state_file=Path(os.getenv("STATE_FILE", "data/state.json")),
                interval=int(os.getenv("SYNC_INTERVAL", "600")),
                sync_timezone=os.getenv("SYNC_TIMEZONE", "Asia/Shanghai"),
                timeout=int(os.getenv("HTTP_TIMEOUT", "30")),
                max_bytes=int(os.getenv("MAX_RESPONSE_BYTES", str(20 * 1024 * 1024))),
                max_pages=int(os.getenv("GOOGLE_MAX_PAGES", "1000")),
                sync_delete=boolean("SYNC_DELETE", "true"),
                delete_confirmations=int(os.getenv("DELETE_CONFIRMATIONS", "2")),
                delete_max_count=int(os.getenv("DELETE_MAX_COUNT", "20")),
                delete_max_ratio=float(os.getenv("DELETE_MAX_RATIO", "0.5")),
                delete_ratio_min_count=int(os.getenv("DELETE_RATIO_MIN_COUNT", "5")),
            )
        except (KeyError, ValueError):
            raise SyncError("缺少必需配置或数字配置无效；参照 .env.example") from None
        secure_url(cfg.icloud_url)
        if not all((cfg.username, cfg.password, cfg.calendar_name, cfg.google_client_id,
                    cfg.google_client_secret, cfg.google_refresh_token, cfg.google_calendar_id)):
            raise SyncError("Google OAuth 配置、账户、App 专用密码和日历名称不能为空")
        try:
            ZoneInfo(cfg.sync_timezone)
        except Exception:
            raise SyncError("SYNC_TIMEZONE 必须为有效的 IANA 时区") from None
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cfg.sync_id):
            raise SyncError("SYNC_ID 只能包含 1–64 位字母、数字、下划线和连字符")
        if (min(cfg.interval, cfg.timeout, cfg.max_bytes, cfg.max_pages, cfg.delete_max_count,
                cfg.delete_ratio_min_count) < 1 or cfg.delete_confirmations < 2
                or not 0 < cfg.delete_max_ratio <= 1):
            raise SyncError("同步参数超出有效范围；删除至少需要两次成功轮询确认")
        return cfg


def target_uid(sync_id: str, source_uid: str) -> str:
    return f"{uuid5(NAMESPACE_URL, 'gics:' + sync_id + ':' + source_uid)}@gics"


def parse_calendar(raw: bytes | str) -> Calendar:
    """Reject truncated feeds and parser-recovered errors before reconciliation."""
    try:
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
        stack = []
        for line in text.splitlines():
            if not line or line[0].isspace():
                continue
            if line.startswith("BEGIN:"):
                if not stack and line != "BEGIN:VCALENDAR":
                    raise ValueError()
                stack.append(line[6:])
            elif line.startswith("END:"):
                if not stack or stack.pop() != line[4:]:
                    raise ValueError()
            elif not stack or ":" not in line:
                raise ValueError()
        if stack:
            raise ValueError()
        calendars = Calendar.from_ical(text, multiple=True)
        if len(calendars) != 1 or calendars[0].name != "VCALENDAR":
            raise ValueError()
        result = calendars[0]
        if str(result.get("VERSION")) != "2.0" or any(c.errors for c in result.walk()):
            raise ValueError()
        return result
    except Exception:
        raise SyncError("ICS 结构或字段无效；本轮不会执行同步或删除") from None


def one(component, name: str, required: bool = False):
    value = component.get(name)
    if isinstance(value, list) or (required and (value is None or not str(value).strip())):
        raise SyncError("ICS 存在缺失或重复的必需字段")
    return value


def fingerprint(raw: bytes | str) -> str:
    cal = parse_calendar(raw)
    # Compare server read-back with later server reads; ignore volatile timestamps.
    parts = []
    for comp in cal.subcomponents:
        comp = copy.deepcopy(comp)
        for child in comp.walk():
            for name in ("DTSTAMP", "LAST-MODIFIED", "CREATED", "SEQUENCE"):
                child.pop(name, None)
        parts.append(comp.to_ical())
    return digest(b"\n".join(sorted(parts)))


def source_events(raw: bytes, sync_id: str) -> dict[str, bytes]:
    cal = parse_calendar(raw)
    groups = defaultdict(dict)
    zones = {}
    for zone in cal.subcomponents:
        if zone.name == "VTIMEZONE":
            tzid = str(one(zone, "TZID", True))
            if tzid in zones and zone.to_ical() != zones[tzid].to_ical():
                raise SyncError("ICS 同一 TZID 存在冲突时区定义")
            zones[tzid] = zone
    floating_zone = str(cal.get("X-WR-TIMEZONE", ""))
    for comp in cal.subcomponents:
        if comp.name == "VTIMEZONE":
            continue
        if comp.name != "VEVENT":
            raise SyncError("源日历含非 VEVENT 内容，拒绝将其当作完整事件快照")
        uid = str(one(comp, "UID", True))
        rid = one(comp, "RECURRENCE-ID")
        key = rid.to_ical() + repr(sorted(rid.params.items())).encode() if rid else b"master"
        if key in groups[uid] and groups[uid][key].to_ical() != comp.to_ical():
            raise SyncError("同一 UID/RECURRENCE-ID 存在冲突副本；拒绝不完整快照")
        groups[uid][key] = comp
        cancelled = str(comp.get("STATUS", "")).upper() == "CANCELLED"
        if not cancelled:
            one(comp, "DTSTART", True)
        for prop_name, prop in comp.property_items(recursive=True):
            # A floating Google time uses the feed's default zone, not iCloud's.
            if prop_name in ("DTSTART", "DTEND", "RECURRENCE-ID", "RDATE", "EXDATE"):
                values = getattr(prop, "dts", [prop])
                was_floating = not prop.params.get("TZID")
                for value in values:
                    dt = getattr(value, "dt", None)
                    if isinstance(dt, datetime) and dt.tzinfo is None and was_floating:
                        try:
                            zone = ZoneInfo(floating_zone)
                        except Exception:
                            raise SyncError("浮动时间缺少有效的 X-WR-TIMEZONE；拒绝猜测时区") from None
                        value.dt = dt.replace(tzinfo=zone)
                        prop.params["TZID"] = floating_zone
            if hasattr(prop, "params") and prop.params.get("TZID"):
                tzid = str(prop.params["TZID"])
                if tzid not in zones:
                    try:
                        ZoneInfo(tzid)
                    except Exception:
                        raise SyncError("ICS 含无法解析的 TZID") from None
            if prop_name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
                if not isinstance(getattr(prop, "dt", None), date):
                    raise SyncError("ICS 日期字段无效")
        start = comp.get("DTSTART")
        end = one(comp, "DTEND")
        if start and end:
            try:
                if isinstance(start.dt, datetime) != isinstance(end.dt, datetime) or end.dt <= start.dt:
                    raise ValueError()
            except Exception:
                raise SyncError("ICS 起止时间类型不一致或结束时间无效") from None
        for rule in (comp.get("RRULE"), comp.get("EXRULE")):
            if rule is not None:
                try:
                    rule_start = start.dt if start else None
                    if rule_start and not isinstance(rule_start, datetime):
                        rule_start = datetime.combine(rule_start, datetime.min.time())
                    rrulestr(rule.to_ical().decode(), dtstart=rule_start)
                except Exception:
                    raise SyncError("ICS 重复规则无效") from None

    output = {}
    for uid, variants in groups.items():
        master = variants.get(b"master")
        if master is None:
            raise SyncError("重复日程缺少主事件；拒绝不完整快照")
        if str(master.get("STATUS", "")).upper() == "CANCELLED":
            continue  # Entire series cancelled: normal guarded deletion path.
        payload = Calendar()
        payload.add("PRODID", "-//google-icloud-sync//EN")
        payload.add("VERSION", "2.0")
        referenced_zones = set()
        for _, original in sorted(variants.items()):
            event = copy.deepcopy(original)
            event["UID"] = target_uid(sync_id, uid)
            event[OWNER] = sync_id
            event[SOURCE] = uid
            # Mirror events without inviting guests or sending RSVP responses.
            for name in ("ORGANIZER", "ATTENDEE", "REQUEST-STATUS"):
                event.pop(name, None)
            event.subcomponents = [c for c in event.subcomponents
                                   if c.name == "VALARM" and str(c.get("ACTION")) == "DISPLAY"]
            if "DTSTART" not in event and "RECURRENCE-ID" in event:
                event["DTSTART"] = copy.deepcopy(event["RECURRENCE-ID"])
            if ("DTSTART" in event and not isinstance(event["DTSTART"].dt, datetime)
                    and cal.get("X-WR-TIMEZONE")):
                event[SOURCE_ZONE] = str(cal["X-WR-TIMEZONE"])
            for _, prop in event.property_items(recursive=True):
                if hasattr(prop, "params") and prop.params.get("TZID"):
                    referenced_zones.add(str(prop.params["TZID"]))
            payload.add_component(event)
        for tzid in sorted(referenced_zones):
            if tzid in zones:
                payload.add_component(copy.deepcopy(zones[tzid]))
            else:
                payload.add_component(copy.deepcopy(zone_component(tzid)))
        output[uid] = payload.to_ical()
    return output


@lru_cache(maxsize=128)
def zone_component(tzid: str):
    # Boundaries deliberately explicit: preserved RRULEs are never expanded.
    from icalendar import Timezone
    return Timezone.from_tzinfo(ZoneInfo(tzid), tzid=tzid,
                               first_date=date(1900, 1, 1), last_date=date(2100, 1, 1))


def calendar_in_window(raw: bytes, window: Window, zone: str) -> bool:
    """Check a stored copy's scope without expanding an unbounded future series."""
    current_zone = zone
    def moment(value):
        if isinstance(value, datetime):
            return value.replace(tzinfo=ZoneInfo(current_zone)) if value.tzinfo is None else value
        return datetime.combine(value, datetime.min.time(), tzinfo=ZoneInfo(current_zone))

    def overlaps(start, end):
        return end > window.start and (window.end is None or start < window.end)

    cal = parse_calendar(raw)
    for component in cal.walk("VEVENT"):
        current_zone = str(component.get(SOURCE_ZONE, zone))
        if "DTSTART" not in component:
            raise SyncError("已管理事件缺少时间，无法安全判定同步范围")
        original_start = component["DTSTART"].dt
        start = moment(original_start)
        if "DTEND" in component:
            end = moment(component["DTEND"].dt)
        elif "DURATION" in component:
            end = start + component["DURATION"].dt
        else:
            end = start + (timedelta(0) if isinstance(original_start, datetime) else timedelta(days=1))
        if overlaps(start, end):
            return True
        if "RECURRENCE-ID" in component:
            rid = moment(component["RECURRENCE-ID"].dt)
            if overlaps(rid, rid + (end - start)):
                return True
        if "RRULE" not in component and "RDATE" not in component:
            continue
        all_day = not isinstance(original_start, datetime)
        rule_start = start.replace(tzinfo=None) if all_day else start
        def rule_moment(value):
            result = moment(value)
            return result.astimezone(start.tzinfo).replace(tzinfo=None) if all_day else result
        rules = rruleset()
        rules.rdate(rule_start)
        for prop_name, rule_method in (("RRULE", rules.rrule), ("EXRULE", rules.exrule)):
            values = component.get(prop_name, [])
            for value in values if isinstance(values, list) else [values]:
                rule_method(rrulestr(value.to_ical().decode(), dtstart=rule_start))
        for prop_name, date_method in (("RDATE", rules.rdate), ("EXDATE", rules.exdate)):
            values = component.get(prop_name, [])
            for value in values if isinstance(values, list) else [values]:
                for occurrence in value.dts:
                    if isinstance(occurrence.dt, tuple):
                        raise SyncError("范围判断暂不支持 PERIOD 类型 RDATE；停止而不删除")
                    date_method(rule_moment(occurrence.dt))
        threshold = rule_moment(window.start - (end - start))
        occurrence = rules.after(threshold, inc=False)
        if occurrence is not None and all_day:
            occurrence = occurrence.replace(tzinfo=start.tzinfo)
        if occurrence is not None and overlaps(occurrence, occurrence + (end - start)):
            return True
    return False


@dataclass
class Remote:
    url: str
    etag: str
    data: bytes


class ICloud:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.discovery = caldav.DAVClient(url=cfg.icloud_url, username=cfg.username,
                                         password=cfg.password, timeout=cfg.timeout)
        self.session = requests.Session()
        self.session.auth = (cfg.username, cfg.password)
        self.url = ""

    def close(self):
        self.session.close()
        self.discovery.close()

    def connect(self, cached_url: str | None = None):
        if cached_url:
            secure_url(cached_url)
            self.url = cached_url
            return
        calendars = self.discovery.principal().calendars()
        matches = [c for c in calendars if c.get_display_name() == self.cfg.calendar_name]
        if len(matches) != 1:
            raise SyncError("必须存在且仅存在一个同名的专用 iCloud 日历；不会自动创建或猜选")
        self.url = str(matches[0].url).rstrip("/") + "/"
        secure_url(self.url)

    def resource_url(self, href: str) -> str:
        from posixpath import normpath
        from urllib.parse import unquote
        result = urljoin(self.url, href)
        base, candidate = urlsplit(self.url), urlsplit(result)
        decoded_path = unquote(candidate.path)
        if (candidate.scheme != base.scheme or candidate.netloc != base.netloc
                or not candidate.path.startswith(base.path) or candidate.path == base.path
                or not normpath(decoded_path).startswith(unquote(base.path))
                or candidate.query or candidate.fragment):
            raise SyncError("CalDAV 返回了专用日历以外的资源地址")
        return result

    def request(self, method, url, **kwargs):
        response = self.session.request(method, url, timeout=(self.cfg.timeout, self.cfg.timeout),
                                        allow_redirects=False, **kwargs)
        return response

    def inventory(self) -> list[Remote]:
        response = self.request("REPORT", self.url, data=REPORT,
                                headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        if response.status_code != 207:
            raise SyncError("iCloud 事件列表读取失败；不修改事件")
        try:
            root = etree.fromstring(response.content, etree.XMLParser(resolve_entities=False, no_network=True))
            if root.tag != "{DAV:}multistatus":
                raise ValueError()
            result = []
            for item in root.findall("d:response", NS):
                href = item.findtext("d:href", namespaces=NS)
                # iCloud includes the collection itself: it has an ETag but no calendar-data.
                # Only this exact URL may omit event data; partial event responses still fail.
                if href and urljoin(self.url, href).rstrip("/") == self.url.rstrip("/"):
                    status = item.findtext("d:status", "", NS)
                    propstats = item.findall("d:propstat", NS)
                    if status and " 200 " not in status or not propstats:
                        raise ValueError()
                    for propstat in propstats:
                        prop = propstat.find("d:prop", NS)
                        status = propstat.findtext("d:status", "", NS)
                        if prop is None:
                            raise ValueError()
                        if " 200 " not in status and not (
                                " 404 " in status and {p.tag for p in prop} ==
                                {"{urn:ietf:params:xml:ns:caldav}calendar-data"}):
                            raise ValueError()
                        if any(p.tag == "{urn:ietf:params:xml:ns:caldav}calendar-data"
                               and ((p.text or "").strip() or len(p)) for p in prop):
                            raise ValueError()
                    continue
                properties = {}
                status = item.findtext("d:status", namespaces=NS)
                if status and " 200 " not in status:
                    raise ValueError()
                for propstat in item.findall("d:propstat", NS):
                    if " 200 " not in propstat.findtext("d:status", "", NS):
                        raise ValueError()
                    prop = propstat.find("d:prop", NS)
                    properties.update({p.tag: p.text for p in prop})
                data = properties.get("{urn:ietf:params:xml:ns:caldav}calendar-data")
                etag = properties.get("{DAV:}getetag")
                if not href or not data or not etag:
                    raise ValueError()
                result.append(Remote(self.resource_url(href), etag, data.encode()))
            return result
        except SyncError:
            raise
        except Exception:
            raise SyncError("iCloud 返回了不完整事件列表；不修改事件") from None

    def get_remote(self, uid: str, url: str | None = None) -> Remote | None:
        url = self.resource_url(url or uid + ".ics")
        response = self.request("GET", url)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SyncError("iCloud 单个事件读取失败；本轮停止")
        result = Remote(url, response.headers.get("ETag", ""), response.content)
        strong_etag(result.etag)
        return result

    def put(self, uid: str, payload: bytes, previous: Remote | None) -> Remote:
        url = previous.url if previous else self.resource_url(uid + ".ics")
        headers = {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"}
        if previous:
            headers = {"Content-Type": headers["Content-Type"], "If-Match": strong_etag(previous.etag)}
        response = self.request("PUT", url, data=payload, headers=headers)
        if response.status_code not in (200, 201, 204):
            raise SyncError("iCloud 写入失败或事件已被并发修改；下轮重试")
        response = self.request("GET", url)
        if response.status_code != 200:
            raise SyncError("iCloud 写入后回读失败；下轮按稳定 UID 恢复")
        result = Remote(url, response.headers.get("ETag", ""), response.content)
        strong_etag(result.etag)
        return result

    def delete(self, remote: Remote):
        response = self.request("DELETE", remote.url, headers={"If-Match": strong_etag(remote.etag)})
        if response.status_code not in (200, 204, 404):
            raise SyncError("iCloud 删除失败或事件已被并发修改；停止本轮删除")


def strong_etag(value: str) -> str:
    if not value or value.startswith("W/") or not (value.startswith('"') and value.endswith('"')):
        raise SyncError("iCloud 缺少强 ETag；拒绝无条件覆盖或删除")
    return value


def identify(remote: Remote, sync_id: str) -> tuple[str | None, set[str]]:
    events = parse_calendar(remote.data).walk("VEVENT")
    uids = {str(one(e, "UID", True)) for e in events}
    owned = [e for e in events if str(e.get(OWNER, "")) == sync_id]
    if not owned:
        return None, uids
    sources = {str(one(e, SOURCE, True)) for e in owned}
    if len(owned) != len(events) or len(sources) != 1:
        raise SyncError("iCloud 事件归属标记不一致；拒绝修改")
    source = sources.pop()
    if uids != {target_uid(sync_id, source)}:
        raise SyncError("iCloud 事件归属 UID 不一致；拒绝修改")
    return source, uids


class State:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(path.with_suffix(path.suffix + ".lock"), "a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise SyncError("已有实例使用此状态目录；禁止并发同步") from None
        try:
            self.data = json.loads(path.read_text()) if path.exists() else {
                "format": "incremental-v1", "binding": None, "target": None, "source": None,
                "events": {}, "deletion_latches": {"fast": False, "history": False},
                "history_day": None, "audit_day": None}
            if (self.data["format"] != "incremental-v1" or not isinstance(self.data["events"], dict)
                    or self.data["binding"] is not None and not re.fullmatch(r"[0-9a-f]{64}", self.data["binding"])):
                raise ValueError()
            if self.data["target"] is not None:
                secure_url(self.data["target"])
            for key in ("history_day", "audit_day"):
                if self.data[key] is not None:
                    date.fromisoformat(self.data[key])
            if not all(type(self.data["deletion_latches"][job]) is bool for job in ("fast", "history")):
                raise ValueError()
            source = self.data["source"]
            if source is not None:
                if (not all(isinstance(source[key], str) and source[key]
                            for key in ("token", "calendar_id", "requested_id", "timezone"))
                        or not isinstance(source["items"], dict) or not isinstance(source["defaults"], list)):
                    raise ValueError()
                ZoneInfo(source["timezone"])
                if any(not isinstance(value, dict) or value.get("id") != key
                       for key, value in source["items"].items()):
                    raise ValueError()
            for uid, item in self.data["events"].items():
                if not isinstance(uid, str) or not isinstance(item, dict) or not isinstance(item["payload"], str):
                    raise ValueError()
                counts = [item["missing"][job] for job in ("fast", "history")]
                if any(type(count) is not int or count < 0 for count in counts):
                    raise ValueError()
                secure_url(item["url"])
                for name in ("source_hash", "remote_hash"):
                    if item[name] is not None and not re.fullmatch(r"[0-9a-f]{64}", item[name]):
                        raise ValueError()
            self.saved = copy.deepcopy(self.data)
        except Exception:
            self.close()
            raise SyncError("状态文件损坏或格式不支持；保留文件并停止，禁止自动重置") from None

    def bind(self, cfg: Config, target: str, calendar_id: str):
        binding = digest(json.dumps(["google-api", calendar_id, cfg.username.lower(), cfg.sync_id, target]))
        if self.data["binding"] not in (None, binding):
            raise SyncError("Google 日历、iCloud 账户、SYNC_ID 或目标已改变；停止以保护已有副本")
        self.data["binding"] = binding
        self.data["target"] = target

    def save(self):
        if self.data == self.saved and self.path.exists():
            return
        fd, temporary = tempfile.mkstemp(prefix=".state-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(self.data, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.saved = copy.deepcopy(self.data)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def close(self):
        self.lock.close()


def remote_inventory(destination, cfg: Config, expected: set[str]) -> dict[str, Remote]:
    owned, all_uids = {}, set()
    for remote in destination.inventory():
        source, uids = identify(remote, cfg.sync_id)
        if all_uids.intersection(uids):
            raise SyncError("目标日历存在重复 UID；停止，避免误选资源")
        all_uids.update(uids)
        if source is None and uids.intersection(expected):
            raise SyncError("目标 UID 被非本程序事件占用；不会覆盖")
        if source is not None:
            owned[source] = remote
    return owned


def reconcile(cfg: Config, state: State, window: Window, desired_all: dict[str, bytes],
              destination, owned: dict[str, Remote] | None, *, dry_run=False,
              allow_large_delete=False) -> dict:
    records = state.data["events"]
    desired = {uid: payload for uid, payload in desired_all.items()
               if calendar_in_window(payload, window, cfg.sync_timezone)}
    scoped = {uid for uid, record in records.items()
              if calendar_in_window(record["payload"], window, cfg.sync_timezone)}
    absent = scoped - set(desired_all)  # Complete cache includes events moved outside the window.
    for uid in scoped | set(desired):
        if uid in records:
            if uid in desired_all:
                records[uid]["missing"] = {"fast": 0, "history": 0}
            else:
                records[uid]["missing"][window.name] = (
                    records[uid]["missing"][window.name] + 1 if cfg.sync_delete else 0)
    reason = None
    if absent:
        if not desired:
            reason = "源事件数为零"
        elif len(absent) > cfg.delete_max_count:
            reason = "删除数量超过阈值"
        elif (len(absent) >= cfg.delete_ratio_min_count
              and len(absent) / max(1, len(scoped | set(desired))) > cfg.delete_max_ratio):
            reason = "删除比例超过阈值"
    else:
        state.data["deletion_latches"][window.name] = False
    if reason and cfg.sync_delete:
        state.data["deletion_latches"][window.name] = True
    if allow_large_delete:
        state.data["deletion_latches"][window.name] = False
    blocked = cfg.sync_delete and state.data["deletion_latches"][window.name]
    if blocked:
        LOG.warning("%s 删除保护已锁定（%s）；核验后使用单次覆盖参数", window.name, reason or "此前触发")
    hashes = {uid: fingerprint(payload) for uid, payload in desired.items()}
    changed = {uid for uid in desired if records.get(uid, {}).get("source_hash") != hashes[uid]}
    if owned is not None:
        changed.update(uid for uid in desired if uid not in owned
                       or records.get(uid, {}).get("remote_hash") != fingerprint(owned[uid].data))
    deletions = {uid for uid in absent if cfg.sync_delete and not blocked
                 and records[uid]["missing"][window.name] >= cfg.delete_confirmations}
    remote_map = {}
    # Validate all affected resources before any writes, using current strong ETags.
    for uid in sorted(changed | deletions):
        record = records.get(uid, {})
        remote = (owned.get(uid) if owned is not None else
                  destination().get_remote(target_uid(cfg.sync_id, uid), record.get("url")))
        if remote is not None and identify(remote, cfg.sync_id)[0] != uid:
            raise SyncError("目标事件 UID 或归属被改变；不会覆盖或删除")
        remote_map[uid] = remote
    stats = {"window": window.name, "create": 0, "update": 0, "delete": 0,
             "unchanged": len(desired) - len(changed), "pending_delete": len(absent),
             "delete_blocked": bool(blocked)}
    if not dry_run:
        state.save()
    for uid in sorted(changed):
        remote, payload = remote_map[uid], desired[uid]
        stats["update" if remote else "create"] += 1
        if not dry_run:
            result = destination().put(target_uid(cfg.sync_id, uid), payload, remote)
            if identify(result, cfg.sync_id)[0] != uid:
                raise SyncError("iCloud 未保留事件归属标记；停止以保护日历")
            records[uid] = {"source_hash": hashes[uid], "remote_hash": fingerprint(result.data),
                            "url": result.url, "payload": payload.decode(),
                            "missing": {"fast": 0, "history": 0}}
            if owned is not None:
                owned[uid] = result
            state.save()
    # Every upsert must finish before deletion starts.
    for uid in sorted(deletions):
        remote = remote_map[uid]
        stats["delete"] += int(remote is not None)
        if not dry_run:
            if remote:
                destination().delete(remote)
            records.pop(uid, None)
            if owned is not None:
                owned.pop(uid, None)
            state.save()
    if not dry_run and window.name == "history":
        state.data["history_day"] = window.day
        state.save()
    return stats


def due_windows(cfg: Config, state: State, now: datetime, selection="auto") -> list[Window]:
    current = windows(now, cfg.sync_timezone)
    if selection in ("fast", "history"):
        return [current[selection]]
    result = [current["fast"]]
    if selection == "all" or state.data["history_day"] != current["history"].day:
        result.append(current["history"])
    return result


def sync_cycle(cfg: Config, state: State, *, google, now: datetime, selection="auto",
               dry_run=False, allow_large_delete=False, destination_factory=ICloud) -> dict:
    original = copy.deepcopy(state.data)
    cloud = None
    owned = None
    day = now.astimezone(ZoneInfo(cfg.sync_timezone)).date().isoformat()
    audit = state.data["audit_day"] != day or state.data["binding"] is None

    def destination():
        nonlocal cloud
        if cloud is None:
            cloud = destination_factory(cfg)
            cloud.connect(None if audit else state.data["target"])
            state.bind(cfg, cloud.url, source["calendar_id"])
            if not dry_run:
                state.save()  # Bind before the first remote mutation.
        return cloud

    try:
        source = google.refresh(state.data["source"])
        # Validate every source family before saving a new token or opening iCloud.
        desired_all = source_events(render_cache(source), cfg.sync_id)
        if state.data["target"]:
            state.bind(cfg, state.data["target"], source["calendar_id"])
        state.data["source"] = source
        if not dry_run:
            state.save()
        jobs = due_windows(cfg, state, now, selection)
        if audit:
            owned = remote_inventory(destination(), cfg, {target_uid(cfg.sync_id, uid)
                                                        for uid in set(desired_all) | set(state.data["events"])})
            for uid, remote in owned.items():
                state.data["events"].setdefault(uid, {
                    "source_hash": None, "remote_hash": None, "url": remote.url,
                    "payload": remote.data.decode(), "missing": {"fast": 0, "history": 0}})
                state.data["events"][uid]["url"] = remote.url
        stats = [reconcile(cfg, state, window, desired_all, destination, owned,
                           dry_run=dry_run, allow_large_delete=allow_large_delete) for window in jobs]
        if not dry_run and audit and selection in ("auto", "all"):
            state.data["audit_day"] = day
            state.save()
        return {"windows": stats, "audit": audit, "dry_run": dry_run,
                "delete_blocked": any(item["delete_blocked"] for item in stats)}
    except Exception:
        state.data = copy.deepcopy(state.saved)
        raise
    finally:
        if dry_run:
            state.data = original
        if cloud:
            cloud.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="执行一次后退出；失败返回非零")
    parser.add_argument("--dry-run", action="store_true", help="读取预览，不写事件、缓存或状态")
    parser.add_argument("--allow-large-delete", action="store_true", help="仅限 --once，解除本轮批量删除保护")
    parser.add_argument("--window", choices=("auto", "fast", "history", "all"), default="auto",
                        help="auto: 高频任务及当天未完成历史任务；其他选项用于单次执行")
    args = parser.parse_args()
    if args.allow_large_delete and not args.once:
        parser.error("--allow-large-delete 必须与 --once 一起使用")
    if args.window != "auto" and not (args.once or args.dry_run):
        parser.error("手动选择 --window 必须使用 --once 或 --dry-run")
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for name in ("caldav", "urllib3", "niquests", "requests"):
        logging.getLogger(name).addHandler(logging.NullHandler())
        logging.getLogger(name).propagate = False
    load_dotenv(override=False, interpolate=False)
    state = google = None
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    try:
        cfg = Config.from_env()
        state, google = State(cfg.state_file), Google(cfg)
        next_start, failures = time.monotonic(), 0
        while not stopped.is_set():
            failed = False
            try:
                stats = sync_cycle(cfg, state, google=google, now=datetime.now(timezone.utc),
                                   selection=args.window, dry_run=args.dry_run,
                                   allow_large_delete=args.allow_large_delete)
                LOG.info("同步结果 %s", json.dumps(stats, ensure_ascii=False))
                failed = stats["delete_blocked"]
                failures = 0  # Deletion protection is not a network failure.
            except Exception as error:
                LOG.error("同步失败：%s", str(error) if isinstance(error, SyncError)
                          else type(error).__name__)
                failures += 1
                failed = True
            if args.once or args.dry_run:
                return 1 if failed else 0
            # Keep the original cadence. Never queue elapsed triggers.
            intervals = max(1, math.ceil((time.monotonic() - next_start) / cfg.interval))
            if intervals > 1:
                LOG.info("本轮执行期间跳过 %d 次定时触发", intervals - 1)
            next_start += intervals * cfg.interval
            if failures:
                delay = min(21600, cfg.interval * 2 ** min(failures, 10))
                delay = min(21600, delay + random.uniform(0, min(60, delay * 0.1)))
                next_start = max(next_start, time.monotonic() + delay)
                LOG.info("连续失败 %d 次，退避后重试", failures)
            stopped.wait(max(0, next_start - time.monotonic()))
        return 0
    except SyncError as error:
        LOG.error("%s", error)
        return 1
    finally:
        if state:
            state.close()
        if google:
            google.close()


if __name__ == "__main__":
    raise SystemExit(main())
