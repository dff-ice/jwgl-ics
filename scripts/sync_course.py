#!/usr/bin/env python3
"""课程表 -> .ics 主入口。

用法（离线/演示，读本地抓包 JSON）：
    python scripts/sync_course.py --input-kb kb.json --input-rjc rjc.json

用法（在线，自动登录抓取；凭据取环境变量 JW_USERNAME / JW_PASSWORD）：
    python scripts/sync_course.py [--config config.json]

每次运行都会重写 course.ics 与 state.json；事件内容相比上次有变化时，
被替换掉的旧 UID 会以 STATUS:CANCELLED 写进 .ics（避免订阅端残留重复）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_CONFIG = {
    "base_url": "http://jwgl.hbeu.edu.cn/jwglxt",
    "semester_start": "2026-09-07",  # 第 1 周星期一（每学期开学需改）
    "term": {"xnm": None, "xqm": None},  # None = 在线时自动探测
    "school": "湖北工程学院",
    "ignore_no_room": True,
    "exclude_courses": [],  # 可选：按课程名精确忽略
}


def load_config(path: Path | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path and path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _fetch_live(cfg: dict):
    """在线登录并抓取课表 + 节次时间。返回 (kb_json, rjc_json)。"""
    # 惰性导入：仅在线路径需要 requests
    from scripts.jwlogin import ZhengfangClient, LoginError  # noqa: F401

    username = os.environ.get("JW_USERNAME", "").strip()
    password = os.environ.get("JW_PASSWORD", "").strip()
    if not username or not password:
        raise SystemExit(
            "缺少登录凭据：请设置环境变量 JW_USERNAME / JW_PASSWORD"
        )

    client = ZhengfangClient(cfg["base_url"])
    client.login(username, password)

    xnm = (cfg.get("term") or {}).get("xnm") or client.xnm
    xqm = (cfg.get("term") or {}).get("xqm") or client.xqm
    if not xnm or not xqm:
        raise SystemExit("无法确定当前学年/学期")
    kb = client.fetch_schedule(xnm, xqm)
    rjc = client.fetch_section_times(xnm, xqm)
    return kb, rjc


def _load_offline(kb_path: Path, rjc_path: Path):
    kb = json.loads(kb_path.read_text(encoding="utf-8"))
    rjc = json.loads(rjc_path.read_text(encoding="utf-8"))
    return kb, rjc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="正方教务课表 -> .ics")
    ap.add_argument("--config", type=Path, default=ROOT / "config.json")
    ap.add_argument("--out", type=Path, default=ROOT / "course.ics")
    ap.add_argument("--state", type=Path, default=ROOT / "state.json")
    ap.add_argument("--input-kb", type=Path, help="离线模式：课表 JSON（cxXsgrkb 响应）")
    ap.add_argument("--input-rjc", type=Path, help="离线模式：节次时间 JSON（cxRjc 响应）")
    ap.add_argument("--exclude", action="append", default=None,
                    help="额外忽略的课程名（可多次）")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    exclude = set(cfg.get("exclude_courses") or []) | set(args.exclude or [])

    if args.input_kb and args.input_rjc:
        kb, rjc = _load_offline(args.input_kb, args.input_rjc)
    elif args.input_kb or args.input_rjc:
        raise SystemExit("--input-kb 与 --input-rjc 必须同时提供")
    else:
        kb, rjc = _fetch_live(cfg)

    # --- 解析 ---
    from scripts import kbmodel, icsgen

    sch = kbmodel.parse_kb(kb, ignore_no_room=cfg.get("ignore_no_room", True))
    if exclude:
        sch.cells = [c for c in sch.cells if c.kcmc not in exclude]

    try:
        sem_start = _dt.date.fromisoformat(str(cfg["semester_start"]))
    except (KeyError, ValueError):
        raise SystemExit(f"config.json 里 semester_start 无效: {cfg.get('semester_start')}")

    bell = icsgen.parse_section_times(rjc)
    events = icsgen.build_events(sch, sem_start, bell)
    manifest = icsgen.event_manifest(events)

    # --- 与上次比对：本次消失的事件 → 取消事件 ---
    prev_state = load_state(args.state)
    prev_manifest = prev_state.get("events") or {}
    canceled = icsgen.diff_events(prev_manifest, manifest)

    # --- 生成并写盘 ---
    prev_by_uid = prev_manifest
    now = _dt.datetime.now(_dt.timezone.utc)
    calname = f"{sch.term_label or '课表'}（{sch.xqmc or cfg.get('school','')}）"
    text = icsgen.generate_ics(
        sch, events, canceled, calendar_name=calname, now=now, prev_by_uid=prev_by_uid
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="")
    state = {
        "generated_at": now.isoformat(),
        "term": {"xnm": sch.xnm, "xqm": sch.xqm, "label": sch.term_label},
        "semester_start": str(sem_start),
        "student": {"xh": sch.xh, "xm": sch.xm, "bjmc": sch.bjmc},
        "events": manifest,
    }
    args.state.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print(f"学期: {sch.term_label or sch.xnm + '/' + sch.xqm}"
          f"  学生: {sch.xm}({sch.xh})  班级: {sch.bjmc}")
    print(f"课程格: {len(sch.cells)}   展开事件: {len(events)}   取消事件: {len(canceled)}")
    print(f"已写入: {args.out}  /  {args.state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
