"""把课程格子展开为 .ics（RFC 5545），用 icalendar 库渲染（不手工拼装）。

设计（对应需求）：
1. 每门课每个实际上课周 = 一条独立 VEVENT（无 RRULE），iOS/Apple 兼容最好。
2. 时区统一用 UTC（东八区时间 -8h）。DTSTART:20260907T000000Z 形式，
   订阅端会按设备时区显示为本地 08:00；无需 VTIMEZONE，最稳。
3. 描述换行/折行、字段转义全部交给 icalendar 自动处理。
4. 稳定 UID = f(学期,课程代码,星期,节次,周号)，不含教室/教师/时间表；
   换地点/换老师不产生新 UID；本次消失的旧事件以 STATUS:CANCELLED 补发。
5. 标题附授课老师；找不到老师则不附。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

from .kbmodel import CourseCell, ZfSchedule

__all__ = [
    "DEFAULT_SECTION_TIMES",
    "parse_section_times",
    "semester_week_date",
    "build_events",
    "stable_uid",
    "render_calendar",
    "event_manifest",
    "diff_events",
]

CAL_PRODID = "-//hbeu edu//jwgl-ics//CN"
BEIJING_UTC = timedelta(hours=8)  # 湖北（东八区），无夏令时

# 节次 -> (上课时间, 下课时间)。实际以 cxRjc 接口下发为准，此为兜底。
DEFAULT_SECTION_TIMES: dict[int, tuple[str, str]] = {
    1: ("08:00", "08:45"),
    2: ("08:55", "09:40"),
    3: ("10:10", "10:55"),
    4: ("11:05", "11:50"),
    5: ("14:00", "14:45"),
    6: ("14:55", "15:40"),
    7: ("16:10", "16:55"),
    8: ("17:05", "17:50"),
    9: ("19:00", "19:45"),
    10: ("19:55", "20:40"),
    11: ("20:50", "21:35"),
}


def parse_section_times(items: list[dict]) -> dict[int, tuple[str, str]]:
    """从 cxRjc 接口的 JSON 列表构建 节号->(qssj, jssj)。"""
    out: dict[int, tuple[str, str]] = {}
    for it in items or []:
        try:
            n = int(str(it.get("jcmc", "")).strip())
            qssj = str(it.get("qssj", "")).strip()
            jssj = str(it.get("jssj", "")).strip()
        except (TypeError, ValueError):
            continue
        if qssj and jssj:
            out[n] = (qssj, jssj)
    if not out:
        out = dict(DEFAULT_SECTION_TIMES)
    return out


def semester_week_date(semester_start: date, week: int, xqj: int) -> date:
    """学期第 week 周、星期 xqj(周一=1) 对应的公历日期。"""
    return semester_start + timedelta(weeks=week - 1, days=xqj - 1)


# --------------------------------------------------------------------------
# 事件数据（纯 dict，便于测试；序列化在 render_calendar 里由 icalendar 完成）
# --------------------------------------------------------------------------
def _term_key(sch: ZfSchedule) -> str:
    return f"{sch.xnm}-{sch.xqm}"


def stable_uid(sch: ZfSchedule, cell: CourseCell, week: int) -> str:
    """稳定 UID：学期+课程代码+星期+节次+周号（不含教室/教师/时间表）。"""
    key = (
        f"hbeu-jwgl|{_term_key(sch)}|{cell.kch}|{cell.xqj}"
        f"|{cell.sec_start}-{cell.sec_end}|w{week}"
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest() + "@hbeu.edu.cn"


def _summary(cell: CourseCell) -> str:
    """标题：课程名；"""
    name = cell.kcmc.strip()
    return f"{name}" 


def _description(cell: CourseCell, week: int) -> str:
    lines = [f"课程：{cell.kcmc}"]
    if cell.teacher.strip():
        lines.append(f"教师：{cell.teacher.strip()}")
    lines.append(f"周次：第{week}周（排课：{cell.zcd_raw or '—'}）")
    lines.append(f"节次：第{cell.sec_start}~{cell.sec_end}节")
    if cell.room:
        lines.append(f"教室：{cell.room}")
    extras = [s for s in (cell.xf and f"学分：{cell.xf}", cell.kcxz, cell.khfs) if s]
    if extras:
        lines.append("；".join(extras))
    return "\n".join(lines)


def build_events(
    sch: ZfSchedule,
    semester_start: date,
    bell: dict[int, tuple[str, str]],
) -> list[dict]:
    """每格每周展开成一条独立事件。返回 dict 列表（已按时间排序）。"""
    events: list[dict] = []
    for cell in sch.cells:
        if cell.sec_start not in bell:
            continue
        start_t, _ = bell[cell.sec_start]
        _, end_t = bell.get(cell.sec_end, bell[cell.sec_start])
        for week in cell.weeks:
            d = semester_week_date(semester_start, week, cell.xqj)
            events.append(
                {
                    "uid": stable_uid(sch, cell, week),
                    "date": d,
                    "start": start_t,
                    "end": end_t,
                    "summary": _summary(cell),
                    "location": cell.room,
                    "description": _description(cell, week),
                }
            )
    events.sort(key=lambda e: (e["date"], e["start"], e["summary"]))
    return events


# --------------------------------------------------------------------------
# 序列化：交给 icalendar（转义/折行/UTC 全自动）
# --------------------------------------------------------------------------
def _to_utc_aware(d: date, hhmm: str) -> datetime:
    hh, mm = map(int, hhmm.split(":"))
    local = datetime(d.year, d.month, d.day, hh, mm)
    return (local - BEIJING_UTC).replace(tzinfo=timezone.utc)


def render_calendar(
    events: list[dict],
    canceled: list[dict],
    *,
    calendar_name: str,
    now: datetime | None = None,
    prev_by_uid: dict[str, dict] | None = None,
) -> bytes:
    """渲染 .ics（bytes）。内容有变的旧事件 SEQUENCE+1；本次消失的旧事件补 CANCELLED。"""
    from icalendar import Calendar, Event  # 惰性导入

    now = now or datetime.now(timezone.utc)
    prev_by_uid = prev_by_uid or {}

    cal = Calendar()
    cal.add("prodid", CAL_PRODID)
    cal.add("version", "2.0")
    cal.add("x-wr-calname", calendar_name)

    for e in events:
        ev = Event()
        ev.add("uid", e["uid"])
        ev.add("dtstamp", now)
        ev.add("dtstart", _to_utc_aware(e["date"], e["start"]))
        ev.add("dtend", _to_utc_aware(e["date"], e["end"]))
        ev.add("summary", e["summary"])
        if e.get("location"):
            ev.add("location", e["location"])
        ev.add("description", e["description"])
        ev.add("sequence", 1 if _changed(prev_by_uid.get(e["uid"]), e) else 0)
        cal.add_component(ev)

    for c in canceled:
        c_ev = Event()
        d = c.get("date") or date(2000, 1, 1)
        c_ev.add("uid", c["uid"])
        c_ev.add("dtstamp", now)
        c_ev.add("dtstart", _to_utc_aware(d, c.get("start") or "00:00"))
        c_ev.add("dtend", _to_utc_aware(d, c.get("end") or "00:00"))
        c_ev.add("summary", c.get("summary") or "(已取消)")
        c_ev.add("status", "CANCELLED")
        c_ev.add("sequence", 1)
        cal.add_component(c_ev)

    return cal.to_ical()


def _changed(old: dict | None, new: dict) -> bool:
    if not old:
        return False
    return any(old.get(k) != new.get(k) for k in ("summary", "location", "description"))


# --------------------------------------------------------------------------
# manifest（state.json）辅助
# --------------------------------------------------------------------------
def event_manifest(events: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in events:
        out[e["uid"]] = {
            "uid": e["uid"],
            "date": e["date"].strftime("%Y-%m-%d"),
            "start": e["start"],
            "end": e["end"],
            "summary": e["summary"],
            "location": e.get("location", ""),
        }
    return out


def diff_events(prev: dict[str, dict], new: dict[str, dict]) -> list[dict]:
    """返回旧 manifest 中存在、但新 manifest 中消失的事件（需发送取消）。"""
    gone: list[dict] = []
    for uid, attrs in prev.items():
        if uid not in new:
            raw = attrs.get("date")
            try:
                parsed = date.fromisoformat(raw)
            except (TypeError, ValueError):
                parsed = date(2000, 1, 1)
            gone.append(
                {
                    "uid": uid,
                    "date": parsed,
                    "start": attrs.get("start") or "00:00",
                    "end": attrs.get("end") or "00:00",
                    "summary": attrs.get("summary") or "",
                }
            )
    return gone
