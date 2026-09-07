"""把解析出的课程格子展开成紧凑的 .ics（RFC 5545）日历。

设计要点（对应需求）：
1. 单双周分开 —— 每个"排课格"转成一条周期事件（RRULE）：
   - 连续周 如 1-16周 → RRULE:FREQ=WEEKLY;COUNT=16
   - 单/双周 如 2-16周(双)、1-15周(单) → RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=n
     日期由学期第 1 周周一锚点精确推出，无公历奇偶歧义。
   - 周次不规整（如 1-6,9-12）自动拆成多条周期事件；极个别无法用 step1/2
     表达的周次退回逐条单次事件，保证正确优先。
   因此一个学期通常只有“课程格数”量级（约 20~40 条）VEVENT，文件几 KB~十几 KB。
2. 线上课忽略 —— 解析阶段已过滤无教室行；本模块只收已过滤的 `cells`。
3. 稳定 UID 防重复 —— UID = f(学期, 课程代码, 星期, 节次, 周次序列特征)，不含
   教室/教师/时间表。换地点、换老师、打铃时间变化都不产生新 UID；真正换了
   时段/周次的，会把旧时段整条 STATUS:CANCELLED，再补新时段，避免残留重复。
4. 节假日暂不处理 —— 按周次模板直出，节假日偏移留待后续。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

from .kbmodel import CourseCell, ZfSchedule

__all__ = [
    "DEFAULT_SECTION_TIMES",
    "parse_section_times",
    "semester_week_date",
    "weeks_to_runs",
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
# 周次 -> RRULE 段
# --------------------------------------------------------------------------
def weeks_to_runs(weeks: tuple[int, ...]) -> list[tuple[int, int, int]]:
    """把周号集合拆成若干等差子序列 (起始周, 步长, 项数)。

    步长只取 1(每周)或 2(隔周=单/双)。无法纳入(如出现间隔>2)的周各自成单条。
    """
    ws = sorted(weeks)
    runs: list[tuple[int, int, int]] = []
    i = 0
    while i < len(ws):
        w0 = ws[i]
        if i + 1 >= len(ws):
            runs.append((w0, 1, 1))
            i += 1
            continue
        step = ws[i + 1] - ws[i]
        if step not in (1, 2):
            runs.append((w0, 1, 1))
            i += 1
            continue
        j = i + 1
        while j + 1 < len(ws) and ws[j + 1] - ws[j] == step:
            j += 1
        runs.append((w0, step, j - i + 1))
        i = j + 1
    return runs


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
# 事件构建（每个排课格 -> 一条周期事件）
# --------------------------------------------------------------------------
def _term_key(sch: ZfSchedule) -> str:
    return f"{sch.xnm}-{sch.xqm}"


def stable_uid(sch: ZfSchedule, cell: CourseCell, run: tuple[int, int, int]) -> str:
    """稳定 UID：学期 + 课程代码 + 星期 + 节次 + 周次序列特征。
    故意不含教室/教师/时间表 → 换地点、换老师、改打铃时间不产生新 UID。"""
    w0, step, cnt = run
    key = (
        f"hbeu-jwgl|{_term_key(sch)}|{cell.kch}|{cell.xqj}"
        f"|{cell.sec_start}-{cell.sec_end}|r{w0}s{step}n{cnt}"
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest() + "@hbeu.edu.cn"


def _run_label(run: tuple[int, int, int]) -> str:
    w0, step, cnt = run
    last = w0 + step * (cnt - 1)
    if step == 1:
        return f"{w0}-{last}周" if cnt > 1 else f"{w0}周"
    tag = "单" if w0 % 2 == 1 else "双"
    return f"{w0}-{last}周({tag})"


def _description(cell: CourseCell, run: tuple[int, int, int]) -> str:
    w0, step, cnt = run
    label = _run_label(run)
    lines = [
        f"课程：{cell.kcmc}",
        f"教师：{cell.teacher or '—'}",
        f"周次：{cell.zcd_raw or label}（共{cnt}周，每{step}周一次）",
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
    """每个排课格(必要时拆多段)转一条周期事件。事件含 RRULE，date 为首个发生日。"""
    events: list[dict] = []
    for cell in sch.cells:
        if cell.sec_start not in bell:
            continue
        start_t, _ = bell[cell.sec_start]
        _, end_t = bell.get(cell.sec_end, bell[cell.sec_start])
        for run in weeks_to_runs(cell.weeks):
            w0, step, cnt = run
            d = semester_week_date(semester_start, w0, cell.xqj)
            if step == 1:
                rrule = f"FREQ=WEEKLY;COUNT={cnt}"
            else:
                rrule = f"FREQ=WEEKLY;INTERVAL=2;COUNT={cnt}"
            events.append(
                {
                    "uid": stable_uid(sch, cell, run),
                    "date": d,
                    "start": start_t,
                    "end": end_t,
                    "summary": cell.kcmc,
                    "location": cell.room,
                    "description": _description(cell, run),
                    "rrule": rrule,
                    "occurrences": cnt,
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
        f"RRULE:{e['rrule']}",  # RECUR 值不用文本转义(其 ; , 是语法分隔符)
        f"SEQUENCE:{seq}",
    ]
    if e.get("location"):
        lines.append(f"LOCATION:{_escape(e['location'])}")
    if e.get("description"):
        lines.append(f"DESCRIPTION:{_escape(e['description'])}")
    lines.append("END:VEVENT")
    return "\r\n".join(_fold(l) for l in lines)


def _cancel_event(old: dict, dtstamp: str) -> str:
    """把旧时段以 STATUS:CANCELLED 形式输出，通知订阅端删除该 UID 整条周期。"""
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
    ]
    if old.get("rrule"):
        lines.append(f"RRULE:{old['rrule']}")
    lines.append("END:VEVENT")
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
    keys = ("summary", "location", "description", "rrule")
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
            "rrule": e.get("rrule", ""),
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
                    "rrule": attrs.get("rrule") or "",
                }
            )
    return gone
