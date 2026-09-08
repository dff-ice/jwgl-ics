import json
import os
import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts import icsgen, kbmodel  # noqa: E402

KB = os.path.join(ROOT, "tests", "fixtures", "anon_kb.json")
RJC = os.path.join(ROOT, "tests", "fixtures", "anon_rjc.json")
START = date(2026, 9, 7)  # 2026-2027 学年第 1 学期第 1 周周一
UTC = timezone.utc


def _schedule():
    with open(KB, encoding="utf-8") as f:
        obj = json.load(f)
    with open(RJC, encoding="utf-8") as f:
        rjc = json.load(f)
    sch = kbmodel.parse_kb(obj)
    bell = icsgen.parse_section_times(rjc)
    return sch, bell


def _event_dates(events, summary_prefix, start=None):
    out = []
    for e in events:
        if e["summary"].startswith(summary_prefix):
            if start and e["start"] != start:
                continue
            out.append(e["date"])
    return out


class TestBuildEvents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sch, bell = _schedule()
        cls.sch, cls.bell = sch, bell
        cls.events = icsgen.build_events(sch, START, bell)

    def test_every_week_is_independent(self):
        # 每格每周一条：总条数 = 各格周数之和 = 116，且 UID 互不重复
        self.assertEqual(len(self.events), 116)
        self.assertEqual(len({e["uid"] for e in self.events}), 116)
        # 不出现 RRULE 相关键
        self.assertTrue(all("rrule" not in e for e in self.events))

    def test_first_date_and_times(self):
        e = next(x for x in self.events
                 if x["summary"].startswith("数据结构")
                 and x["date"] == date(2026, 9, 7))
        self.assertEqual(e["start"], "14:00")
        self.assertEqual(e["end"], "15:40")

    def test_even_weeks_only(self):
        # 电路基础 周一3-4 只有 2-4双 / 6-16双 -> 8 个实例、均为偶数周
        ds = _event_dates(self.events, "电路基础", start="10:10")
        self.assertEqual(len(ds), 8)
        weeks = sorted((d - START).days // 7 + 1 for d in ds)
        self.assertEqual(weeks, list(range(2, 17, 2)))

    def test_military_no_overlap(self):
        # 军事理论 单周1..15 + 双周2..16 -> 16 个不同日期
        ds = _event_dates(self.events, "军事理论", start="10:10")
        self.assertEqual(len(ds), len(set(ds)))
        self.assertEqual(len(ds), 16)

    def test_uid_excludes_room_teacher(self):
        c1 = next(c for c in self.sch.cells if c.kcmc == "形势与政策")
        c2 = replace(c1, room="别的楼999", teacher="某老师")
        w = c1.weeks[0]
        self.assertEqual(icsgen.stable_uid(self.sch, c1, w),
                         icsgen.stable_uid(self.sch, c2, w))

    def test_uid_changes_on_time_or_week(self):
        c = next(c for c in self.sch.cells if c.kcmc == "形势与政策")
        w = c.weeks[0]
        moved = replace(c, sec_start=7, sec_end=8)
        self.assertNotEqual(icsgen.stable_uid(self.sch, c, w),
                            icsgen.stable_uid(self.sch, moved, w))
        self.assertNotEqual(icsgen.stable_uid(self.sch, c, w),
                            icsgen.stable_uid(self.sch, c, w + 1))

    def test_summary_has_teacher(self):
        e = next(x for x in self.events if x["summary"].startswith("数据结构"))
        self.assertTrue(e["summary"].startswith("数据结构与算法"))
        self.assertIn("范老师", e["summary"])

    def test_summary_without_teacher_keeps_name(self):
        c = next(c for c in self.sch.cells if c.kcmc == "形势与政策")
        self.assertEqual(icsgen._summary(replace(c, teacher="   ")), "形势与政策")
        self.assertEqual(icsgen._summary(c), "形势与政策 · 周老师")


class TestIcsRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sch, bell = _schedule()
        cls.sch, cls.events = sch, icsgen.build_events(sch, START, bell)
        man = icsgen.event_manifest(cls.events)
        fake_old = {**man}
        fake_old["deadbeef@hbeu.edu.cn"] = {
            "uid": "deadbeef@hbeu.edu.cn", "date": "2026-09-14",
            "start": "08:00", "end": "09:40", "summary": "旧课程",
        }
        cls.canceled = icsgen.diff_events(fake_old, man)
        cls.now = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
        cls.blob = icsgen.render_calendar(
            cls.events, cls.canceled,
            calendar_name="测试课表", now=cls.now, prev_by_uid=man,
        )
        from icalendar import Calendar as ICal
        cls.parsed = ICal.from_ical(cls.blob)
        cls.components = [c for c in cls.parsed.walk() if c.name == "VEVENT"]

    def test_parseable_and_counts(self):
        self.assertEqual(len(self.components), len(self.events) + 1)  # +1 取消
        statuses = {str(c.get("status", "")) for c in self.components}
        self.assertIn("CANCELLED", statuses)

    def test_dtstart_utc(self):
        e = next(c for c in self.components
                 if str(c.get("summary", "")).startswith("数据结构"))
        dt = e.get("dtstart").dt
        # 周一5-6节：北京 14:00-15:40 -> UTC 06:00-07:40
        self.assertEqual(dt, datetime(2026, 9, 7, 6, 0, tzinfo=UTC))
        self.assertEqual(e.get("dtend").dt, datetime(2026, 9, 7, 7, 40, tzinfo=UTC))
        self.assertIsNotNone(dt.tzinfo)  # 必须带时区(Z)，而非裸数字

    def test_description_newlines(self):
        e = next(c for c in self.components
                 if str(c.get("summary", "")).startswith("数据结构"))
        desc = str(e.get("description"))
        self.assertIn("课程：数据结构与算法", desc)
        self.assertIn("\n教师：范老师", desc)
        self.assertIn("\n周次：第1周", desc)
        # 不应残留字面反斜杠 \n（icalendar 已还原成真实换行）
        self.assertNotIn("\\n", desc)

    def test_cancel_present_with_uid(self):
        canceled = [c for c in self.components
                    if str(c.get("status", "")) == "CANCELLED"]
        self.assertEqual(len(canceled), 1)
        self.assertEqual(str(canceled[0].get("uid")), "deadbeef@hbeu.edu.cn")

    def test_no_line_over_75_and_crlf(self):
        text = self.blob.decode("utf-8")
        for line in text.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line)
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("X-WR-CALNAME:测试课表", text)


if __name__ == "__main__":
    unittest.main()
