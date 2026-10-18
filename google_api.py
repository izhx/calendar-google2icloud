"""Read-only Google Calendar API, finite recurring masters and complete pagination."""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from datetime import date, datetime, time as day_time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from dateutil.relativedelta import relativedelta
from icalendar import Alarm, Calendar, Event

SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://www.googleapis.com/calendar/v3/calendars/"


class GoogleError(Exception):
    """Messages contain no response bodies, credentials or private event content."""


class SyncTokenExpired(GoogleError):
    pass


@dataclass(frozen=True)
class Window:
    name: str
    start: datetime
    end: datetime | None
    day: str


def windows(now: datetime, zone: str) -> dict[str, Window]:
    local = now.astimezone(ZoneInfo(zone))
    midnight = datetime.combine(local.date(), day_time.min, tzinfo=local.tzinfo)
    return {
        "fast": Window("fast", local - timedelta(days=7), None, local.date().isoformat()),
        "history": Window("history", midnight - relativedelta(months=6),
                          local - timedelta(days=7), local.date().isoformat()),
    }


def api_date(value: dict, default_zone: str) -> date | datetime:
    try:
        if not isinstance(value, dict) or ("date" in value) == ("dateTime" in value):
            raise ValueError()
        if "date" in value:
            return date.fromisoformat(value["date"])
        result = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
        zone = ZoneInfo(value.get("timeZone", default_zone))
        return result.replace(tzinfo=zone) if result.tzinfo is None else result.astimezone(zone)
    except Exception:
        raise GoogleError("Google API 返回无效日期或时区；停止本轮") from None


def render_event(item: dict, uid: str, zone: str, reminders: list, parent: dict | None = None) -> Event:
    if parent:
        zone = parent.get("start", {}).get("timeZone", zone)
    result = Event()
    result.add("UID", uid)
    cancelled = item.get("status") == "cancelled"
    original = item.get("originalStartTime")
    start_value = item.get("start", original if cancelled else None)
    if start_value is None:
        raise GoogleError("Google 活动缺少开始时间；停止本轮")
    start = api_date(start_value, zone)
    result.add("DTSTART", start)
    if "end" in item:
        result.add("DTEND", api_date(item["end"], zone))
    elif not cancelled:
        raise GoogleError("Google 活动缺少结束时间；停止本轮")
    if original is not None:
        recurrence_id = api_date(original, zone)
        if isinstance(recurrence_id, datetime):
            recurrence_id = recurrence_id.astimezone(ZoneInfo(zone))
        result.add("RECURRENCE-ID", recurrence_id)
    timestamp = item.get("updated", item.get("created", "1970-01-01T00:00:00Z"))
    result.add("DTSTAMP", api_date({"dateTime": timestamp}, "UTC"))
    for source, target in (("summary", "SUMMARY"), ("description", "DESCRIPTION"), ("location", "LOCATION")):
        if source in item:
            if not isinstance(item[source], str):
                raise GoogleError("Google 活动文本字段无效")
            result.add(target, item[source])
    status = item.get("status", "confirmed").upper()
    if status not in ("CONFIRMED", "TENTATIVE", "CANCELLED"):
        raise GoogleError("Google 活动状态无效")
    result.add("STATUS", status)
    if "sequence" in item:
        result.add("SEQUENCE", int(item["sequence"]))
    if "transparency" in item:
        result.add("TRANSP", item["transparency"].upper())
    if item.get("visibility") in ("private", "public", "confidential"):
        result.add("CLASS", item["visibility"].upper())
    if item.get("hangoutLink") or item.get("htmlLink"):
        result.add("URL", item.get("hangoutLink", item.get("htmlLink")))
    recurrence = item.get("recurrence", [])
    if not isinstance(recurrence, list):
        raise GoogleError("Google 重复规则无效")
    if recurrence:
        if any(not isinstance(line, str) or "\n" in line or "\r" in line
               or line.split(":", 1)[0].split(";", 1)[0] not in ("RRULE", "EXRULE", "RDATE", "EXDATE")
               for line in recurrence):
            raise GoogleError("Google 重复规则无效")
        try:
            raw = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:rule\r\n" + "\r\n".join(recurrence)
            parsed = Calendar.from_ical(raw + "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n").walk("VEVENT")[0]
            if parsed.errors:
                raise ValueError()
            for name, value in parsed.items():
                if name != "UID":
                    result[name] = value
        except Exception:
            raise GoogleError("Google 重复规则无法解析") from None
    reminder_settings = item.get("reminders", (parent or {}).get("reminders", {"useDefault": True}))
    entries = reminders if reminder_settings.get("useDefault", True) else reminder_settings.get("overrides", [])
    if not cancelled:
        for entry in entries:
            if entry.get("method") == "popup":
                minutes = entry.get("minutes")
                if type(minutes) is not int or not 0 <= minutes <= 40320:
                    raise GoogleError("Google 提醒时间无效")
                alarm = Alarm()
                alarm.add("ACTION", "DISPLAY")
                alarm.add("DESCRIPTION", item.get("summary", "Reminder"))
                alarm.add("TRIGGER", -timedelta(minutes=minutes))
                result.add_component(alarm)
    return result


class Google:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = requests.Session()
        self.access_token = ""
        self.expires_at = 0.0
        self.metadata = None
        self.metadata_at = 0.0

    def close(self):
        self.session.close()

    def token(self, force=False):
        if not force and self.access_token and time.monotonic() < self.expires_at:
            return self.access_token
        response = self.session.post(TOKEN_URL, data={
            "client_id": self.cfg.google_client_id,
            "client_secret": self.cfg.google_client_secret,
            "refresh_token": self.cfg.google_refresh_token,
            "grant_type": "refresh_token",
        }, timeout=(self.cfg.timeout, self.cfg.timeout), allow_redirects=False)
        if response.status_code != 200:
            raise GoogleError(f"Google 授权刷新失败（HTTP {response.status_code}）；请检查本地 OAuth 配置")
        try:
            payload = response.json()
            token, lifetime = payload["access_token"], int(payload["expires_in"])
            if not isinstance(token, str) or not token or lifetime < 1:
                raise ValueError()
            if "scope" in payload and SCOPE not in payload["scope"].split():
                raise ValueError()
            self.access_token = token
            self.expires_at = time.monotonic() + max(0, lifetime - 60)
            return token
        except Exception:
            raise GoogleError("Google 令牌响应无效或缺少日历只读权限") from None

    def get(self, suffix: str, params=None):
        for attempt in range(2):
            token = self.token(force=bool(attempt))
            with self.session.get(API_URL + suffix, params=params,
                                  headers={"Authorization": "Bearer " + token}, stream=True,
                                  timeout=(self.cfg.timeout, self.cfg.timeout), allow_redirects=False) as response:
                if response.status_code == 401 and attempt == 0:
                    continue
                if response.status_code == 410:
                    raise SyncTokenExpired("Google 增量令牌过期；需要重新读取完整源日历")
                if response.status_code != 200:
                    raise GoogleError(f"Google Calendar API 读取失败（HTTP {response.status_code}）；本轮停止，稍后重试")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > self.cfg.max_bytes:
                        raise GoogleError("Google API 单页响应超出大小限制；本轮停止")
                    chunks.append(chunk)
                try:
                    import json
                    data = json.loads(b"".join(chunks))
                    if not isinstance(data, dict):
                        raise ValueError()
                    return data
                except Exception:
                    raise GoogleError("Google API 返回无效 JSON；本轮停止") from None

    def calendar(self):
        # Resolve primary again at process startup and at most once per day thereafter.
        if self.metadata is None or time.monotonic() - self.metadata_at >= 86400:
            metadata = self.get(quote(self.cfg.google_calendar_id, safe=""))
            try:
                if not isinstance(metadata["id"], str) or not metadata["id"]:
                    raise ValueError()
                ZoneInfo(metadata["timeZone"])
            except Exception:
                raise GoogleError("无法确认 Google 日历身份与时区") from None
            self.metadata = metadata
            self.metadata_at = time.monotonic()
        return self.metadata

    def changes(self, calendar_id: str, token: str | None):
        params = {"singleEvents": "false", "showDeleted": "true", "maxResults": 2500}
        if token:
            params["syncToken"] = token
        items, seen_tokens, settings = {}, set(), None
        for _ in range(self.cfg.max_pages):
            data = self.get(quote(calendar_id, safe="") + "/events", params=params.copy())
            if (data.get("kind") != "calendar#events" or not isinstance(data.get("items", []), list)
                    or data.get("accessRole") in ("freeBusyReader", "none")):
                raise GoogleError("Google API 事件列表不完整或权限不足")
            current_settings = {"timezone": data.get("timeZone"),
                                "defaults": data.get("defaultReminders", [])}
            try:
                ZoneInfo(current_settings["timezone"])
                if not isinstance(current_settings["defaults"], list):
                    raise ValueError()
            except Exception:
                raise GoogleError("Google API 列表缺少有效时区或提醒配置") from None
            if settings is not None and settings != current_settings:
                raise GoogleError("Google API 分页期间日历设置改变；下轮重试")
            settings = current_settings
            for item in data.get("items", []):
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                    raise GoogleError("Google API 活动缺少稳定 ID")
                if item["id"] in items and items[item["id"]] != item:
                    raise GoogleError("Google API 分页出现冲突事件；下轮重新读取")
                items[item["id"]] = item
            page_token = data.get("nextPageToken")
            if page_token is None or page_token == "":
                next_token = data.get("nextSyncToken")
                if not isinstance(next_token, str) or not next_token:
                    raise GoogleError("Google API 缺少完整增量令牌；禁止使用部分结果")
                return items, next_token, settings
            if not isinstance(page_token, str) or page_token in seen_tokens:
                raise GoogleError("Google API 分页令牌异常；禁止使用部分结果")
            seen_tokens.add(page_token)
            params["pageToken"] = page_token
        raise GoogleError("Google API 分页超过保护上限；禁止使用部分结果")

    def refresh(self, previous: dict | None) -> dict:
        metadata = self.calendar()
        if previous and (previous["calendar_id"] != metadata["id"]
                         or previous["requested_id"] != self.cfg.google_calendar_id):
            raise GoogleError("Google 源日历身份已改变；停止以保护已有副本")
        token = previous["token"] if previous else None
        try:
            changes, next_token, settings = self.changes(metadata["id"], token)
            items = copy.deepcopy(previous["items"]) if previous else {}
        except SyncTokenExpired:
            if token is None:
                raise
            # Build a replacement in isolation. A failed rebuild preserves the old cache and iCloud.
            changes, next_token, settings = self.changes(metadata["id"], None)
            items = {}
        deleted_parents = set()
        for event_id, item in changes.items():
            if item.get("status") == "cancelled" and not item.get("recurringEventId"):
                items.pop(event_id, None)
                deleted_parents.add(event_id)
            else:
                items[event_id] = item
        # Remove every cached exception when its whole series has been deleted.
        items = {key: value for key, value in items.items()
                 if not (value.get("recurringEventId") in deleted_parents or
                         value.get("status") == "cancelled" and value.get("recurringEventId") not in items)}
        return {"calendar_id": metadata["id"], "requested_id": self.cfg.google_calendar_id,
                "token": next_token, "items": items, **settings}


def render_cache(cache: dict) -> bytes:
    """Complete master/exception families come from the persistent incremental cache."""
    zone, defaults, items = cache["timezone"], cache["defaults"], cache["items"]
    parents, children = {}, {}
    for item in items.values():
        if item.get("recurringEventId"):
            children.setdefault(item["recurringEventId"], []).append(item)
        else:
            uid = item.get("iCalUID")
            if not isinstance(uid, str) or not uid or uid in parents:
                raise GoogleError("Google 活动缺少 iCalUID 或存在冲突主事件")
            parents[uid] = item
    by_id = {item["id"]: item for item in parents.values()}
    if any(parent_id not in by_id or not by_id[parent_id].get("recurrence") for parent_id in children):
        raise GoogleError("重复例外缺少有效主事件；禁止使用不完整缓存")
    result = Calendar()
    result.add("VERSION", "2.0")
    result.add("PRODID", "-//google-icloud-sync-api//EN")
    result.add("X-WR-TIMEZONE", zone)
    for uid, parent in sorted(parents.items()):
        result.add_component(render_event(parent, uid, zone, defaults))
        for item in sorted(children.get(parent["id"], []), key=lambda value: value["id"]):
            if "originalStartTime" not in item:
                raise GoogleError("重复例外缺少 originalStartTime")
            result.add_component(render_event(item, uid, zone, defaults, parent))
    return result.to_ical()
