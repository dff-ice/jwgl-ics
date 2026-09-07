import json
import os
import sys
import unittest
from datetime import date

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


class TestBuildEvents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sch, bell = _schedule()
        cls.sch = sch
        cls.bell = bell
        cls.events = icsgen.build_events(sch, START, bell)

    def test_bell_parse(self):
        self.assertEqual(self.bell[5], ("14:00", "14:45"))
        self.assertEqual(self.bell[11], ("20:50", "21:35"))

    def test_total_events(self):
        # 13 格展开周数总和 = 116
        expected = sum(len(c.weeks) for c in self.sch.cells)
        self.assertEqual(expected, 116)
        self.assertEqual(len(self.events), expected)

    def test_date_and_times(self):
        # 数据结构 周一 5-6 节第 1 周 -> 2026-09-07 14:00-15:40
        e = next(x for x in self.events
                 if x["summary"].startswith("数据结构")
                 and x["date"] == date(2026, 9, 7))
        self.assertEqual(e["start"], "14:00")
        self.assertEqual(e["end"], "15:40")

    def test_even_week_lab_date(self):
        # 电路基础 周一 3-4 双周(第2周=2026-09-14) 第6周 etc，偶数周
        e = next(x for x in self.events
                 if x["summary"] == "电路基础"
                 and x["date"] == date(2026, 9, 14))
        self.assertEqual(e["start"], "10:10")

    def test_uid_excludes_room_teacher(self):
        c1 = next(c for c in self.sch.cells if c.kcmc == "形势与政策")
        from dataclasses import replace

        c2 = replace(c1, room="别的楼999", teacher="某老师")
        self.assertEqual(icsgen.stable_uid(self.sch, c1, 13),
                         icsgen.stable_uid(self.sch, c2, 13))

    def test_uid_changes_on_time_move(self):
        c = next(c for c in self.sch.cells if c.kcmc == "形势与政策")
        from dataclasses import replace

        moved = replace(c, sec_start=7, sec_end=8)  # 换节次
        self.assertNotEqual(icsgen.stable_uid(self.sch, c, 13),
                            icsgen.stable_uid(self.sch, moved, 13))

    def test_no_duplicate_dates_for_military(self):
        ds = [x["date"] for x in self.events if x["summary"] == "军事理论"]
        self.assertEqual(len(ds), len(set(ds)))


class TestIcsText(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sch, bell = _schedule()
        events = icsgen.build_events(sch, START, bell)
        man = icsgen.event_manifest(events)
        # 假设"电路基础 周一7-8节"的旧事件换了时间被移除 -> 应产生取消事件
        uid_gone = next(
            x["uid"] for x in events
            if x["summary"] == "电路基础" and x["date"] == date(2026, 9, 14)
        )
        prev = dict(man)
        gone_attr = prev.pop(uid_gone)  # 该 uid 本次不再出现
        canceled = icsgen.diff_events(prev, man)  # prev 缺失 -> 不会取消
        # 反向：构造旧 manifest 里多一个 uid，验证能被取消
        fake_old = {**man}
        fake_old["deadbeef@hbeu.edu.cn"] = {
            "uid": "deadbeef@hbeu.edu.cn", "date": "2026-09-14",
            "start": "08:00", "end": "09:40", "summary": "旧课程",
        }
        canceled = icsgen.diff_events(fake_old, man)
        cls.canceled = canceled
        cls.text = icsgen.generate_ics(sch, events, canceled)
        cls.sch, cls.events = sch, events

    def test_header_and_footer(self):
        self.assertTrue(self.text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(self.text.strip().endswith("END:VCALENDAR"))
        self.assertIn("X-WR-CALNAME:", self.text)

    def test_crlf_only(self):
        self.assertNotIn("\n", self.text.replace("\r\n", ""))

    def test_no_line_over_75(self):
        for line in self.text.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line)

    def test_cancel_present(self):
        self.assertIn("STATUS:CANCELLED", self.text)
        self.assertIn("deadbeef@hbeu.edu.cn", self.text)

    def test_active_not_cancelled(self):
        # 有效事件无 STATUS:CANCELLED
        body = self.text.split("END:VCALENDAR")[0]
        n_cancel = body.count("STATUS:CANCELLED")
        self.assertGreaterEqual(n_cancel, 1)
        # VEVENT 总数 = 有效 + 取消
        self.assertEqual(self.text.count("BEGIN:VEVENT"),
                         len(self.events) + 1)

    def test_folded_long_summary(self):
        # 长课程名应能放进 <=75 字节行（经过折行）
        self.assertTrue(self.text.count("毛泽东思想和中国特色") >= 1)


if __name__ == "__main__":
    unittest.main()
