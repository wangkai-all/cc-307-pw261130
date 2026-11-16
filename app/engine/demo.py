"""虚构演示数据.

所有名称、设备、人员、检查均为演示用虚构数据, 不代表任何真实机构或个体;
本系统只做运营排程分析, 不给出医疗诊断或临床建议。
"""

from __future__ import annotations

import random
from typing import Any

from .domain import DAY_START, Exam

DEMO_DATES = ["2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-21"]

RESOURCES: list[dict[str, Any]] = [
    # 设备
    {"id": "EQ-CT-1", "name": "CT-A", "kind": "equipment", "capabilities": ["ct_scan"], "note": "螺旋 CT"},
    {"id": "EQ-CT-2", "name": "CT-B", "kind": "equipment", "capabilities": ["ct_scan"], "note": "螺旋 CT"},
    {"id": "EQ-MR-1", "name": "MR-A", "kind": "equipment", "capabilities": ["mr_scan"], "note": "1.5T"},
    {"id": "EQ-MR-2", "name": "MR-B", "kind": "equipment", "capabilities": ["mr_scan", "mr_contrast"], "note": "3T"},
    {"id": "EQ-US-1", "name": "US-1", "kind": "equipment", "capabilities": ["us_scan"], "note": "腹部彩超"},
    {"id": "EQ-US-2", "name": "US-2", "kind": "equipment", "capabilities": ["us_scan"], "note": "浅表彩超"},
    {"id": "EQ-XR-1", "name": "DR-1", "kind": "equipment", "capabilities": ["xray"], "note": "数字摄影"},
    {"id": "EQ-PET-1", "name": "PET-A", "kind": "equipment", "capabilities": ["pet_scan"], "note": "演示用"},
    # 准备间
    {"id": "PREP-1", "name": "准备间 1", "kind": "prep", "capabilities": ["prep_bay"]},
    {"id": "PREP-2", "name": "准备间 2", "kind": "prep", "capabilities": ["prep_bay"]},
    {"id": "PREP-3", "name": "准备间 3", "kind": "prep", "capabilities": ["prep_bay"]},
    # 记录/报告间
    {"id": "ROOM-R1", "name": "协议室", "kind": "room", "capabilities": ["protocol"]},
    # 人员
    {"id": "ST-RAD-1", "name": "影像技师 林", "kind": "staff", "capabilities": ["radiographer"]},
    {"id": "ST-RAD-2", "name": "影像技师 周", "kind": "staff", "capabilities": ["radiographer"]},
    {"id": "ST-RAD-3", "name": "影像技师 吴", "kind": "staff", "capabilities": ["radiographer"]},
    {"id": "ST-NUR-1", "name": "准备护士 陈", "kind": "staff", "capabilities": ["prep_nurse"]},
    {"id": "ST-NUR-2", "name": "准备护士 许", "kind": "staff", "capabilities": ["prep_nurse"]},
    {"id": "ST-RPT-1", "name": "记录员 何", "kind": "staff", "capabilities": ["report_clerk"]},
    {"id": "ST-DOC-1", "name": "值班医师 赵", "kind": "staff", "capabilities": ["radiologist"]},
]

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "TPL-CT",
        "name": "CT 扫描 (含记录归档)",
        "priority_class": "routine",
        "prep_minutes": 20,
        "prep_instruction": "去除金属饰品, 空腹 15 分钟 (演示要求)",
        "steps": [
            {"key": "scan", "label": "CT 扫描", "minutes": 20, "capability": "ct_scan", "role": "radiographer", "gap_after": 0},
            {"key": "protocol", "label": "协议核对与归档", "minutes": 10, "capability": "protocol", "role": "report_clerk", "gap_after": 0},
        ],
    },
    {
        "id": "TPL-MR",
        "name": "MR 扫描 (含记录归档)",
        "priority_class": "routine",
        "prep_minutes": 35,
        "prep_instruction": "完成金属筛查问卷; 静坐 10 分钟 (演示要求)",
        "steps": [
            {"key": "scan", "label": "MR 扫描", "minutes": 40, "capability": "mr_scan", "role": "radiographer", "gap_after": 0},
            {"key": "protocol", "label": "协议核对与归档", "minutes": 10, "capability": "protocol", "role": "report_clerk", "gap_after": 0},
        ],
    },
    {
        "id": "TPL-US",
        "name": "超声检查",
        "priority_class": "routine",
        "prep_minutes": 10,
        "prep_instruction": "按演示流程准备检查区域",
        "steps": [
            {"key": "scan", "label": "超声扫描", "minutes": 15, "capability": "us_scan", "role": "radiographer", "gap_after": 0},
        ],
    },
    {
        "id": "TPL-XR",
        "name": "数字摄影",
        "priority_class": "routine",
        "prep_minutes": 5,
        "prep_instruction": "更换检查袍 (演示要求)",
        "steps": [
            {"key": "scan", "label": "摄影扫描", "minutes": 10, "capability": "xray", "role": "radiographer", "gap_after": 0},
        ],
    },
    {
        "id": "TPL-PET",
        "name": "PET 检查 (演示)",
        "priority_class": "routine",
        "prep_minutes": 45,
        "prep_instruction": "静息准备 45 分钟 (演示要求)",
        "steps": [
            {"key": "prep_bay2", "label": "准备间静息", "minutes": 30, "capability": "prep_bay", "role": "prep_nurse", "gap_after": 0},
            {"key": "scan", "label": "PET 扫描", "minutes": 35, "capability": "pet_scan", "role": "radiographer", "gap_after": 0},
            {"key": "protocol", "label": "图像归档", "minutes": 10, "capability": "protocol", "role": "report_clerk", "gap_after": 0},
        ],
    },
    {
        "id": "TPL-CT-URG",
        "name": "急诊 CT (演示)",
        "priority_class": "urgent",
        "prep_minutes": 0,
        "prep_instruction": "急诊绿色通道, 无需准备 (演示)",
        "steps": [
            {"key": "scan", "label": "急诊 CT 扫描", "minutes": 15, "capability": "ct_scan", "role": "radiographer", "gap_after": 0},
            {"key": "protocol", "label": "报告归档", "minutes": 5, "capability": "protocol", "role": "report_clerk", "gap_after": 0},
        ],
    },
]

SPECIALTY = ["腹部", "胸部", "头颅", "脊柱", "关节", "血管", "盆腔", "颈部"]


def _shift(sid: str, rid: str, date: str, start: int, end: int, note: str = "") -> dict[str, Any]:
    return {"id": sid, "resource_id": rid, "date": date, "start": start, "end": end, "note": note}


def _hm_to_min(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def build_demo_events() -> list[tuple[str, dict[str, Any]]]:
    """返回按顺序应用的事件列表 (不含 seq/at, 由 seed 写入)。"""
    events: list[tuple[str, dict[str, Any]]] = []
    for res in RESOURCES:
        events.append(("resource.added", {**res, "version": 1}))

    for rid, start, end in [
        ("EQ-CT-1", "07:30", "15:30"),
        ("EQ-CT-2", "08:00", "16:00"),
        ("EQ-MR-1", "08:00", "14:00"),
        ("EQ-MR-2", "09:00", "17:00"),
        ("EQ-US-1", "08:00", "12:00"),
        ("EQ-US-2", "13:00", "17:00"),
        ("EQ-XR-1", "08:00", "18:00"),
        ("EQ-PET-1", "09:00", "15:00"),
        ("PREP-1", "07:30", "16:00"),
        ("PREP-2", "08:00", "16:00"),
        ("PREP-3", "09:00", "17:00"),
        ("ROOM-R1", "08:00", "17:00"),
    ]:
        for i, date in enumerate(DEMO_DATES):
            s, e = _hm_to_min(start), _hm_to_min(end)
            # 最后一天设备提前收工, 制造容量紧张
            if date == DEMO_DATES[-1] and rid.startswith("EQ"):
                e -= 120
            events.append(("shift.added", _shift(f"SHF-{rid}-{i}", rid, date, s, e)))

    staff_plan = {
        "ST-RAD-1": ("07:30", "15:30"),
        "ST-RAD-2": ("09:00", "17:00"),
        "ST-RAD-3": ("11:00", "19:00"),
        "ST-NUR-1": ("07:30", "15:30"),
        "ST-NUR-2": ("10:00", "18:00"),
        "ST-RPT-1": ("08:00", "16:00"),
        "ST-DOC-1": ("08:00", "16:00"),
    }
    for sid, (start, end) in staff_plan.items():
        for i, date in enumerate(DEMO_DATES):
            if sid == "ST-RAD-3" and date == DEMO_DATES[0]:
                continue  # 第一天下午才到岗
            if sid == "ST-RPT-1" and date == "2026-11-19":
                # 记录员半天班, 下午的归档无人可用 —— 暴露具体冲突
                events.append(
                    ("shift.added", _shift(f"SHF-{sid}-{i}", sid, date, _hm_to_min(start), _hm_to_min("12:00"), "上午半天"))
                )
                continue
            events.append(
                ("shift.added", _shift(f"SHF-{sid}-{i}", sid, date, _hm_to_min(start), _hm_to_min(end)))
            )

    # 预设设备封锁: 故障与校准
    events.append(
        (
            "block.added",
            {
                "id": "BLK-1",
                "resource_id": "EQ-MR-1",
                "date": "2026-11-18",
                "start": _hm_to_min("10:00"),
                "end": _hm_to_min("12:00"),
                "reason": "梯度线圈故障 (演示)",
            },
        )
    )
    events.append(
        (
            "block.added",
            {
                "id": "BLK-2",
                "resource_id": "EQ-CT-2",
                "date": "2026-11-19",
                "start": _hm_to_min("13:00"),
                "end": _hm_to_min("15:00"),
                "reason": "年检校准 (演示)",
            },
        )
    )

    for tpl in TEMPLATES:
        events.append(("template.added", tpl))

    rng = random.Random(20261117)
    seq = 0
    exam_plan = [
        # (date, template, count, priority, prep_done_ratio)
        ("2026-11-17", "TPL-CT", 6, 5, 0.6),
        ("2026-11-17", "TPL-US", 4, 5, 0.8),
        ("2026-11-17", "TPL-MR", 2, 5, 0.4),
        ("2026-11-17", "TPL-XR", 4, 5, 0.9),
        ("2026-11-17", "TPL-CT-URG", 1, 2, 1.0),
        ("2026-11-18", "TPL-CT", 7, 5, 0.5),
        ("2026-11-18", "TPL-MR", 4, 5, 0.5),
        ("2026-11-18", "TPL-US", 5, 5, 0.7),
        ("2026-11-18", "TPL-PET", 2, 4, 0.5),
        ("2026-11-19", "TPL-CT", 5, 5, 0.5),
        ("2026-11-19", "TPL-MR", 3, 4, 0.6),
        ("2026-11-19", "TPL-XR", 5, 5, 0.8),
        ("2026-11-19", "TPL-CT-URG", 2, 1, 1.0),
        ("2026-11-20", "TPL-CT", 6, 5, 0.4),
        ("2026-11-20", "TPL-MR", 3, 5, 0.5),
        ("2026-11-20", "TPL-US", 4, 5, 0.8),
        ("2026-11-21", "TPL-CT", 5, 5, 0.5),
        ("2026-11-21", "TPL-MR", 2, 5, 0.5),
        ("2026-11-21", "TPL-PET", 2, 4, 0.5),
    ]
    for date, tpl, count, priority, prep_ratio in exam_plan:
        for i in range(count):
            seq += 1
            prep_done = rng.random() < prep_ratio
            label = f"{rng.choice(SPECIALTY)} {tpl.replace('TPL-', '').lower()}-{seq:03d} (虚构)"
            noise = rng.randint(-8, 8)
            due = None
            if priority <= 2:
                due = _hm_to_min("12:00")
            events.append(
                (
                    "exam.added",
                    {
                        "id": f"EX-{seq:04d}",
                        "label": label,
                        "template_id": tpl,
                        "requested_date": date,
                        "priority": priority,
                        "kind": "scheduled",
                        "prep_done": prep_done,
                        "notes": "演示数据, 非真实患者",
                        "due_by": due,
                        "created_seq": seq,
                        "noise": noise,
                    },
                )
            )
    return events


def seed_ledger(ledger) -> None:
    """把演示事件写入空台账。调用方需保证台账为空。"""
    ledger.append_many(build_demo_events())


def seed_initial_plans(ledger) -> None:
    """为前三天生成一份「已确认」的初始排程, 让页面从真实的正式台账开始。

    这只是演示用的初始排班, 不涉及任何真实预约; 之后的所有试算都以它为基线。
    """
    from .planner import schedule

    for date in DEMO_DATES[:3]:
        state = ledger.state
        plan = schedule(state, date, mode="global", tie_break="balanced", now=DAY_START)
        ledger.append(
            "booking.confirmed",
            {
                "date": date,
                "mode": "global",
                "planner": "初始化示例",
                "plan_id": "seed-" + date,
                "note": "演示初始排班 (虚构)",
                "bookings": {
                    eid: {"date": b.date, "slots": b.slots}
                    for eid, b in plan.bookings.items()
                },
                "unplaced_ids": [u["exam_id"] for u in plan.unplaced],
            },
        )
    # 第一天演示「部分执行」: 前两项已开始, 其中一项完成; 时钟推进到 09:10
    day = DEMO_DATES[0]
    state = ledger.state
    ordered = sorted(
        [b for b in state.booked_exams(day) if b.status == "SCHEDULED"],
        key=lambda b: (b.start() or 0, b.exam_id),
    )
    if ordered:
        first = ordered[0]
        ledger.append("booking.started", {"exam_id": first.exam_id, "at": first.start()})
        ledger.append("booking.completed", {"exam_id": first.exam_id, "at": first.end()})
    if len(ordered) > 1:
        second = ordered[1]
        ledger.append("booking.started", {"exam_id": second.exam_id, "at": second.start()})
    ledger.append("clock.set", {"date": day, "at": 9 * 60 + 10})


def demo_addon_pool() -> list[dict[str, Any]]:
    """「新增插单」面板用的候选检查 (仍为虚构)。"""
    return [
        {
            "id": "ADD-001",
            "label": "紧急 头颅 non-contrast (虚构)",
            "template_id": "TPL-CT-URG",
            "priority": 1,
            "kind": "addon",
            "prep_done": True,
            "notes": "急诊窗口提交",
        },
        {
            "id": "ADD-002",
            "label": "加急 腹部 MR (虚构)",
            "template_id": "TPL-MR",
            "priority": 2,
            "kind": "addon",
            "prep_done": False,
            "notes": "门诊加急",
        },
        {
            "id": "ADD-003",
            "label": "常规 颈部超声 (虚构)",
            "template_id": "TPL-US",
            "priority": 5,
            "kind": "addon",
            "prep_done": True,
            "notes": "补做",
        },
        {
            "id": "ADD-004",
            "label": "加急 胸部 PET (虚构)",
            "template_id": "TPL-PET",
            "priority": 2,
            "kind": "addon",
            "prep_done": False,
            "notes": "需要长准备",
        },
        {
            "id": "ADD-005",
            "label": "常规 关节 DR (虚构)",
            "template_id": "TPL-XR",
            "priority": 5,
            "kind": "addon",
            "prep_done": False,
            "notes": "临时加做",
        },
    ]
