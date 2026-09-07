"""把解析出的课程格子展开成 .ics（RFC 5545）日历。

设计要点（对应需求）：
1. 单双周分开 —— 每格按 `weeks` 逐周展开成独立 VEVENT，落在真实日期上；(单)/(双)
   在解析阶段已只留奇数/偶数周，天然不重叠。
2. 线上课忽略 —— 解析阶段已过滤无教室行；本模块只收已过滤的 `cells`。
3. 稳定 UID 防重复 —— UID = f(学期, 课程代码, 星期, 节次, 周号)，不含教室/教师；
   时间表(打铃)变更或换教室/老师不产生新 UID。同时支持把"本次已消失"的旧事件以
   STATUS:CANCELLED 输出，避免订阅端残留重复。
4. 节假日暂不处理 —— 直接按周次模板生成，节假日偏移留待后续。
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
    "generate_ics",
]

CAL_PRODID = "-//hbeu edu//jwgl-ics//CN"
TZ_OFFSET = "+0800"  # 湖北（东八区），中国无夏令时

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
# 文本工具
# --------------------------------------------------------------------------
def _escape(text: str) -> str:
    """ICS 文本转义：\, \; \, 换行。"""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def _fold(line: str, limit: int = 75) -> str:
    """按 RFC 5545 把长行折到 <=75 字节（在 UTF-8 字符边界处切，续行以空格开头）。"""
    lines: list[str] = []
    cur = ""
    for ch in line:
        if cur and len((cur + ch).encode("utf-8")) > limit:
            lines.append(cur)
            cur = " "  # 续行前缀
        cur += ch
    if cur:
        lines.append(cur)
    return "\r\n".join(lines)


def _fmt_dt(d: date, t: str) -> str:
    """'2026-09-07' + '14:00' -> '20260907T140000+0800'"""
    hh, mm = t.split(":")
    return f"{d.strftime('%Y%m%d')}T{hh}{mm}00{TZ_OFFSET}"


# --------------------------------------------------------------------------
# 事件构建
# --------------------------------------------------------------------------
def _term_key(sch: ZfSchedule) -> str:
    return f"{sch.xnm}-{sch.xqm}"


def stable_uid(sch: ZfSchedule, cell: CourseCell, week: int) -> str:
    """稳定 UID：学期 + 课程代码 + 星期 + 节次 + 周号。
    故意不含教室/教师/教室楼/原始 zcd 文本 → 换地点换老师不产生新 UID。"""
    key = f"hbeu-jwgl|{_term_key(sch)}|{cell.kch}|{cell.xqj}|{cell.sec_start}-{cell.sec_end}|w{week}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest() + "@hbeu.edu.cn"


def _description(cell: CourseCell, week: int) -> str:
    lines = [
        f"课程：{cell.kcmc}",
        f"教师：{cell.teacher or '—'}",
        f"周次：第{week}周（排课：{cell.zcd_raw or '—'}）",
        f"节次：第{cell.sec_start}~{cell.sec_end}节",
    ]
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
    """逐格逐周展开。返回事件 dict，字段含 uid/date/start/end/summary/..."""
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
                    "summary": cell.kcmc,
                    "location": cell.room,
                    "description": _description(cell, week),
                }
            )
    events.sort(key=lambda e: (e["date"], e["start"], e["summary"]))
    return events


# --------------------------------------------------------------------------
# .ics 文本生成
# --------------------------------------------------------------------------
def _vevent(e: dict, dtstamp: str, seq: int = 0) -> str:
    d = e["date"]
    lines = [
        "BEGIN:VEVENT",
        f"UID:{_escape(e['uid'])}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{_fmt_dt(d, e['start'])}",
        f"DTEND:{_fmt_dt(d, e['end'])}",
        f"SUMMARY:{_escape(e['summary'])}",
        f"SEQUENCE:{seq}",
    ]
    if e.get("location"):
        lines.append(f"LOCATION:{_escape(e['location'])}")
    if e.get("description"):
        lines.append(f"DESCRIPTION:{_escape(e['description'])}")
    lines.append("END:VEVENT")
    return "\r\n".join(_fold(l) for l in lines)


def _cancel_event(old: dict, dtstamp: str) -> str:
    """把旧事件以 STATUS:CANCELLED 形式输出，通知订阅端删除该 UID。"""
    d = old.get("date") or date(2000, 1, 1)
    start = old.get("start") or "00:00"
    end = old.get("end") or "00:00"
    lines = [
        "BEGIN:VEVENT",
        f"UID:{_escape(old['uid'])}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{_fmt_dt(d, start)}",
        f"DTEND:{_fmt_dt(d, end)}",
        f"SUMMARY:{_escape(old.get('summary') or '(已取消)')}",
        "STATUS:CANCELLED",
        "SEQUENCE:1",
        "END:VEVENT",
    ]
    return "\r\n".join(_fold(l) for l in lines)


def generate_ics(
    sch: ZfSchedule,
    events: list[dict],
    canceled: list[dict],
    *,
    calendar_name: str | None = None,
    now: datetime | None = None,
    prev_by_uid: dict[str, dict] | None = None,
) -> str:
    """拼装完整 .ics 文本。prev_by_uid 用于给"内容有变"的事件递增 SEQUENCE。"""
    dtstamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    prev_by_uid = prev_by_uid or {}
    calname = calendar_name or (sch.term_label + " 课表")
    parts = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{CAL_PRODID}",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape(calname)}",
    ]
    for e in events:
        old = prev_by_uid.get(e["uid"])
        seq = 0
        if old and _event_changed(old, e):
            seq = 1
        parts.append(_vevent(e, dtstamp, seq))
    for c in canceled:
        parts.append(_cancel_event(c, dtstamp))
    parts.append("END:VCALENDAR")
    return "\r\n".join(parts) + "\r\n"


def _event_changed(old: dict, new: dict) -> bool:
    keys = ("summary", "location", "description")
    return any(old.get(k) != new.get(k) for k in keys)


# --------------------------------------------------------------------------
# manifest（state.json）辅助
# --------------------------------------------------------------------------
def event_manifest(events: list[dict]) -> dict[str, dict]:
    """每个 uid -> 缩略属性（用于下次 diff 与取消）。"""
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
            d = attrs.get("date")
            try:
                parsed = date.fromisoformat(d)
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
