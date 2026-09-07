import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts import kbmodel  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "anon_kb.json")


class TestParseZcd(unittest.TestCase):
    def test_weekly(self):
        self.assertEqual(kbmodel.parse_zcd("1-16周"), list(range(1, 17)))
        self.assertEqual(kbmodel.parse_zcd("13-16周"), [13, 14, 15, 16])
        self.assertEqual(kbmodel.parse_zcd("1-18周"), list(range(1, 19)))

    def test_single_double(self):
        self.assertEqual(kbmodel.parse_zcd("2-4周(双)"), [2, 4])
        self.assertEqual(kbmodel.parse_zcd("6-16周(双)"), [6, 8, 10, 12, 14, 16])
        self.assertEqual(kbmodel.parse_zcd("3-5周(单)"), [3, 5])
        self.assertEqual(kbmodel.parse_zcd("1-15周(单)"),
                         [1, 3, 5, 7, 9, 11, 13, 15])
        self.assertEqual(kbmodel.parse_zcd("2-16周(双)"),
                         [2, 4, 6, 8, 10, 12, 14, 16])

    def test_multi_range(self):
        self.assertEqual(kbmodel.parse_zcd("1-6周,9-12周"),
                         [1, 2, 3, 4, 5, 6, 9, 10, 11, 12])

    def test_single_week(self):
        self.assertEqual(kbmodel.parse_zcd("5周"), [5])


class TestParseKb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import json

        with open(FIXTURE, encoding="utf-8") as f:
            cls.obj = json.load(f)
        cls.sch = kbmodel.parse_kb(cls.obj, ignore_no_room=True)

    def test_meta(self):
        self.assertEqual(self.sch.xnm, "2026")
        self.assertEqual(self.sch.xqm, "3")
        self.assertEqual(self.sch.term_label, "2026-2027学年第1学期")
        self.assertEqual(self.sch.xm, "演示学生")

    def test_online_filtered(self):
        names = {c.kcmc for c in self.sch.cells}
        self.assertNotIn("广告心理学（UOOC在线课程）", names)
        # 13 排课格 = 14 kbList 行 - 1 条线上无教室行
        self.assertEqual(len(self.sch.cells), 13)

    def test_military_odd_even_stay_separate(self):
        mil = [c for c in self.sch.cells if c.kch == "JSLL03"]
        self.assertEqual(len(mil), 2)
        odds = {w for c in mil for w in c.weeks}
        self.assertEqual(len(odds), 16)  # 单周1..15 + 双周2..16，互不重叠

    def test_sections(self):
        d = [c for c in self.sch.cells if c.kcmc.startswith("数据结构")]
        got = {(c.xqj, c.sec_start, c.sec_end) for c in d}
        self.assertIn((1, 5, 6), got)
        self.assertIn((4, 9, 10), got)

    def test_multi_section_4h(self):
        d = [c for c in self.sch.cells if c.kcmc == "电子技术与数字逻辑"
             and c.weeks == tuple(range(1, 7))]
        self.assertEqual(len(d), 1)
        self.assertEqual((d[0].sec_start, d[0].sec_end), (5, 8))


if __name__ == "__main__":
    unittest.main()
