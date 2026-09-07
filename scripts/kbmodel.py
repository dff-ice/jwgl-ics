"""解析正方教务（jwglxt）个人课表 JSON 为结构化的课程格子。

数据源：POST /jwglxt/kbcx/xskbcx_cxXsgrkb.html 返回的 JSON，其 `kbList`
数组里每个元素是一个"排课格子"（每周模板），字段含义见 README。
本模块只做解析，不涉及网络、不产生任何 I/O，便于离线测试。

字段说明（kbList 单元素，均为字符串）：
  kcmc   课程名称        kch   课程代码
  kcxz   课程性质        xm    任课教师       zfjmc  主讲
  jxbmc  教学班名称      xqj   星期码 1~7（周一~周日）  xqjmc 星期名
  jcs    节次，如 "1-2" 或 "5-8"（jcor / jc 同义）
  zcd    周次，如 "1-16周"、"2-4周(双)"、"3-5周(单)"
  cdmc   教室            cdbh  教室编码      lh  楼
  xf     学分            khfsmc 考核方式
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["CourseCell", "parse_zcd", "parse_kb", "ZfSchedule"]


# --------------------------------------------------------------------------
# 周次解析
# --------------------------------------------------------------------------
_ZCD_SEG_RE = re.compile(r"(\d+)(?:\s*(?:-|–|~|至)\s*(\d+))?")
_PARITY_MARK = re.compile(r"[单双]")


def parse_zcd(text: str) -> list[int]:
    """把 '1-16周' / '2-4周(双)' / '3-5周(单)' / '1-8周,10-16周' 展开成周号列表。

    - '(单)' 只保留奇数周，'(双)' 只保留偶数周（按"学期周号"的奇偶，与公历无关）。
    - 支持逗号分隔的多段范围，如 '1-6周,9-16周'。
    """
    if not text:
        return []
    t = text.strip()
    m = _PARITY_MARK.search(t)
    parity = None
    if m:
        parity = "odd" if m.group(0) == "单" else "even"
    # 去掉"周"及括号内的单双标记，只留数字段
    body = re.sub(r"周", "", t)
    body = re.sub(r"[（(][单双][)）]", "", body)
    weeks: set[int] = set()
    for chunk in re.split(r"[,，、\s]+", body):
        chunk = chunk.strip()
        if not chunk:
            continue
        seg = _ZCD_SEG_RE.fullmatch(chunk)
        if not seg:
            continue
        a = int(seg.group(1))
        b = int(seg.group(2)) if seg.group(2) else a
        for w in range(a, b + 1):
            if parity == "odd" and w % 2 == 0:
                continue
            if parity == "even" and w % 2 == 1:
                continue
            weeks.add(w)
    return sorted(weeks)


# --------------------------------------------------------------------------
# 节次解析
# --------------------------------------------------------------------------
_SEC_RE = re.compile(r"(\d+)\s*(?:-|–|~|至)\s*(\d+)")


def parse_sections(raw: str) -> tuple[int, int] | None:
    """把节次字符串 '1-2' / '5-8' / '3' 解析成 (起始节, 结束节)。"""
    if not raw:
        return None
    t = re.sub(r"[节课]", "", raw).strip()
    m = _SEC_RE.search(t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"\d+", t)
    if m:
        n = int(m.group(0))
        return n, n
    return None


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------
@dataclass
class CourseCell:
    kcmc: str                  # 课程名
    kch: str                   # 课程代码
    kcxz: str                  # 课程性质
    teacher: str               # 任课教师
    room: str                  # 教室
    building: str              # 楼
    xqj: int                   # 星期码 1..7 = 周一..周日
    xqjmc: str                 # 星期名
    zcd_raw: str               # 原始周次文本（含单双）
    weeks: tuple[int, ...]     # 展开后的周号
    sec_start: int             # 起始节
    sec_end: int               # 结束节
    xf: str = ""               # 学分
    khfs: str = ""             # 考核方式
    raw: dict = field(default_factory=dict)

    @property
    def term_free_key(self) -> tuple:
        """用于同响应内去重（不含教室/教师等易变字段）。"""
        return (self.kch, self.xqj, self.sec_start, self.sec_end, self.weeks)


@dataclass
class ZfSchedule:
    xnm: str = ""
    xqm: str = ""
    xnmc: str = ""             # "2026-2027"
    xqmmc: str = ""            # 第几学期中文序号
    xh: str = ""               # 学号
    xm: str = ""               # 姓名
    bjmc: str = ""             # 班级
    zymc: str = ""             # 专业
    xqmc: str = ""             # 学校
    cells: list[CourseCell] = field(default_factory=list)

    @property
    def term_label(self) -> str:
        return f"{self.xnmc}学年第{self.xqmmc}学期" if self.xnmc else ""


_CELL_FIELDS = ("kcmc", "kch", "kcxz", "xm", "cdmc", "lh", "xqj",
                "xqjmc", "zcd", "xf", "khfsmc")


def parse_cell(raw: dict) -> CourseCell | None:
    """把一个 kbList 元素解析成 CourseCell；非排课/缺关键信息返回 None。"""
    def g(*names: str) -> str:
        for n in names:
            v = raw.get(n)
            if v is not None:
                return str(v)
        return ""

    kcmc = g("kcmc").strip()
    if not kcmc:
        return None
    # 星期码
    xqj_s = g("xqj")
    if not xqj_s:
        return None
    try:
        xqj = int(float(xqj_s))
    except ValueError:
        return None
    # 周次
    zcd = g("zcd", "zcdm")
    weeks = tuple(parse_zcd(zcd))
    if not weeks:
        return None
    # 节次
    sec_raw = g("jcs", "jcor", "jc")
    sec = parse_sections(sec_raw)
    if not sec:
        return None
    return CourseCell(
        kcmc=kcmc,
        kch=g("kch").strip(),
        kcxz=g("kcxz").strip(),
        teacher=g("xm", "jsxm", "jghxm").strip(),
        room=g("cdmc", "cdbh").strip(),
        building=g("lh").strip(),
        xqj=xqj,
        xqjmc=g("xqjmc").strip() or f"星期{'一二三四五六日'[xqj - 1] if 1 <= xqj <= 7 else '?'}",
        zcd_raw=zcd,
        weeks=weeks,
        sec_start=sec[0],
        sec_end=sec[1],
        xf=g("xf"),
        khfs=g("khfsmc"),
        raw=dict(raw),
    )


def parse_kb(obj: dict, *, ignore_no_room: bool = True) -> ZfSchedule:
    """解析 cxXsgrkb 响应 JSON 顶层对象。只取有排课的 kbList。"""
    s = ZfSchedule()
    xsxx = obj.get("xsxx") or {}
    s.xnm = str(xsxx.get("XNM", "") or "")
    s.xqm = str(xsxx.get("XQM", "") or "")
    s.xnmc = str(xsxx.get("XNMC", "") or "")
    s.xqmmc = str(xsxx.get("XQMMC", "") or "")
    s.xh = str(xsxx.get("XH", "") or "")
    s.xm = str(xsxx.get("XM", "") or "")
    s.bjmc = str(xsxx.get("BJMC", "") or "")
    s.zymc = str(xsxx.get("ZYMC", "") or "")
    s.xqmc = str(xsxx.get("XQMC", "") or "")

    seen: set[tuple] = set()
    for raw in obj.get("kbList") or []:
        cell = parse_cell(raw)
        if cell is None:
            continue
        # 线上课/无固定排课的兜底过滤：没有教室又没有有效节次就跳过
        if ignore_no_room and not cell.room:
            continue
        if cell.term_free_key in seen:
            continue
        seen.add(cell.term_free_key)
        s.cells.append(cell)
    # 排序：星期 → 节次 → 课程，保证输出稳定
    s.cells.sort(key=lambda c: (c.xqj, c.sec_start, c.sec_end, c.kcmc))
    return s
