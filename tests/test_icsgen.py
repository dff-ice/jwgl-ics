import json
import os
import sys
import unittest
from dataclasses import replace
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts import icsgen, kbmodel  # noqa: E402

KB = os.path.join(ROOT, "tests", "fixtures", "anon_kb.json")
RJC = os.path.join(ROOT, "tests", "fixtures", "anon_rjc.json")
START = date(2026, 9, 7)  # 2026-2027 学年第 1 学期第 1 周周一


def _schedule():
    with open(KB, encoding="utf-8") as f:
        obj = json.load(f)
    with open(RJC, encoding="utf-8") as f:
        rjc = json.load(f)
    sch = kbmodel.parse_kb(obj)
    bell = icsgen.parse_section_times(rjc)
    return sch, bell


def expand(event):
    """按 RRULE 把周期事件展开成 (date, ...) 列表，便于断言。"""
    dates = []
    import re

    m = re.fullmatch(
        r"FREQ=WEEKLY;INTERVAL=(\d+);COUNT=(\d+)", event["rrule"]
    ) or re.fullmatch(r"FREQ=WEEKLY;COUNT=(\d+)", event["rrule"])
    if m and len(m.groups()) == 1:
        interval, count = 1, int(m.group(1))
    elif m:
        interval, count = int(m.group(1)), int(m.group(2))
    else:
        return []
    for i in range(count):
        dates.append(event["date"] + timedelta(weeks=interval * i))
    return dates


class TestWeeksToRuns(unittest.TestCase):
    def test_contiguous(self):
        self.assertEqual(icsgen.weeks_to_runs(tuple(range(1, 17))), [(1, 1, 16)])

    def test_parity(self):
        self.assertEqual(icsgen.weeks_to_runs((2, 4, 6, 8)), [(2, 2, 4)])
        self.assertEqual(icsgen.weeks_to_runs((1, 3, 5)), [(1, 2, 3)])

    def test_multi_range(self):
        # 1-6,9-12 -> 两条连续段
        self.assertEqual(icsgen.weeks_to_runs(tuple(range(1, 7)) + tuple(range(9, 13))),
                         [(1, 1, 6), (9, 1, 4)])

    def test_gap_over2_splits(self):
        self.assertEqual(icsgen.weeks_to_runs((1, 5, 9)), [(1, 1, 1), (5, 1, 1), (9, 1, 1)])


class TestBuildEvents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sch, bell = _schedule()
        cls.sch, cls.bell = sch, bell
        cls.events = icsgen.build_events(sch, START, bell)

    def test_compact_and_total_occurrences(self):
        # 13 格 -> 13 条周期事件，覆盖周次总数仍 = 116
        self.assertEqual(len(self.events), 13)
        self.assertEqual(sum(e["occurrences"] for e in self.events),
                         sum(len(c.weeks) for c in self.sch.cells))
        self.assertEqual(sum(e["occurrences"] for e in self.events), 116)

    def test_first_date_and_times(self):
        # 数据结构 周一5-6节(1-16周) -> DTSTART 2026-09-07 14:00-15:40，COUNT=16
        e = next(x for x in self.events
                 if x["summary"].startswith("数据结构")
                 and x["rrule"] == "FREQ=WEEKLY;COUNT=16")
        self.assertEqual(e["date"], date(2026, 9, 7))
        self.assertEqual(e["start"], "14:00")
        self.assertEqual(e["end"], "15:40")

    def test_parity_rrule(self):
        # 电路基础 周一3-4 有两格: 2-4双周 与 6-16双周 -> 两条 INTERVAL=2
        dg = [x for x in self.events if x["summary"] == "电路基础"
              and x["start"] == "10:10"]
        self.assertEqual(len(dg), 2)
        self.assertTrue(all("INTERVAL=2" in x["rrule"] for x in dg))
        starts = sorted(x["date"] for x in dg)
        self.assertEqual(starts[0], date(2026, 9, 14))   # 第2周(双)起点
        self.assertEqual(starts[1], date(2026, 10, 12))  # 第6周(双)起点

    def test_military_no_overlap(self):
        mil = [x for x in self.events if x["summary"] == "军事理论"]
        self.assertEqual(len(mil), 2)
        ds = []
        for e in mil:
            self.assertIn("INTERVAL=2", e["rrule"])
            ds += expand(e)
        self.assertEqual(len(ds), len(set(ds)))  # 无重复日期
        self.assertEqual(len(ds), 16)            # 单1..15 + 双2..16

    def test_uid_excludes_room_teacher(self):
        c1 = next(c for c in self.sch.cells if c.kcmc == "形势与政策")
        c2 = replace(c1, room="别的楼999", teacher="某老师")
        run = icsgen.weeks_to_runs(c1.weeks)[0]
        self.assertEqual(icsgen.stable_uid(self.sch, c1, run),
                         icsgen.stable_uid(self.sch, c2, run))

    def test_uid_changes_on_time_or_weeks_change(self):
        c = next(c for c in self.sch.cells if c.kcmc == "形势与政策")
        run = icsgen.weeks_to_runs(c.weeks)[0]
        moved = replace(c, sec_start=7, sec_end=8)
        wider = replace(c, weeks=tuple(range(1, 19)))  # 1-16周 -> 1-18周
        self.assertNotEqual(icsgen.stable_uid(self.sch, c, run),
                            icsgen.stable_uid(self.sch, moved, run))
        self.assertNotEqual(icsgen.stable_uid(self.sch, c, run),
                            icsgen.stable_uid(self.sch, wider, (1, 1, 18)))


class TestIcsText(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sch, bell = _schedule()
        cls.sch, cls.events = sch, icsgen.build_events(sch, START, bell)
        man = icsgen.event_manifest(cls.events)
        fake_old = {**man}
        fake_old["deadbeef@hbeu.edu.cn"] = {
            "uid": "deadbeef@hbeu.edu.cn", "date": "2026-09-14",
            "start": "08:00", "end": "09:40", "summary": "旧课程",
            "rrule": "FREQ=WEEKLY;COUNT=16",
        }
        cls.canceled = icsgen.diff_events(fake_old, man)
        cls.text = icsgen.generate_ics(sch, cls.events, cls.canceled)

    def test_header_and_footer(self):
        self.assertTrue(self.text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(self.text.strip().endswith("END:VCALENDAR"))
        self.assertIn("X-WR-CALNAME:", self.text)

    def test_crlf_only(self):
        self.assertNotIn("\n", self.text.replace("\r\n", ""))

    def test_no_line_over_75(self):
        for line in self.text.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line)

    def test_rrule_and_cancel_present(self):
        self.assertIn("RRULE:", self.text)
        self.assertIn("STATUS:CANCELLED", self.text)
        self.assertIn("deadbeef@hbeu.edu.cn", self.text)
        self.assertEqual(self.text.count("BEGIN:VEVENT"),
                         len(self.events) + 1)

    def test_folded_long_summary(self):
        self.assertTrue(self.text.count("毛泽东思想和中国特色") >= 1)

    def test_cancel_carries_rrule(self):
        for b in self.text.split("END:VEVENT"):
            if "deadbeef@hbeu.edu.cn" in b:
                self.assertIn("STATUS:CANCELLED", b)
                self.assertIn("FREQ=WEEKLY;COUNT=16", b)
                break


if __name__ == "__main__":
    unittest.main()
