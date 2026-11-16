"""可用窗口与静态可行性校验.

这里只回答「在某日, 某资源何时可用」以及「一项检查的资源链需要什么」,
不做跨检查搜索 (搜索在 planner 中)。
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import (
    DAY_END,
    DAY_START,
    ResourceBlock,
    Shift,
    State,
    StepSpec,
    Template,
)


@dataclass(frozen=True)
class Interval:
    start: int
    end: int

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end

    def contains_slot(self, start: int, end: int) -> bool:
        return self.start <= start and end <= self.end


def merge_intervals(items: list[Interval]) -> list[Interval]:
    if not items:
        return []
    ordered = sorted(items, key=lambda i: (i.start, i.end))
    out = [ordered[0]]
    for item in ordered[1:]:
        last = out[-1]
        if item.start <= last.end:
            out[-1] = Interval(last.start, max(last.end, item.end))
        else:
            out.append(item)
    return out


def capability_ok(state: State, resource_id: str, capability: str) -> bool:
    res = state.resources.get(resource_id)
    return res is not None and capability in res.capabilities


def staff_capability_ok(state: State, staff_id: str, role: str) -> bool:
    staff = state.resources.get(staff_id)
    return staff is not None and staff.kind == "staff" and role in staff.capabilities


def availability_windows(
    state: State,
    resource_id: str,
    date: str,
    day_start: int = DAY_START,
    day_end: int = DAY_END,
) -> list[Interval]:
    """资源当日的净可用区间: 班次/开放时间减去硬性封锁。

    没有定义任何班次时视为不可用 —— 宁可报告「无可用窗口」,
    也不要把检查塞进未经确认的时段。
    """
    res = state.resources.get(resource_id)
    if res is None:
        return []
    shifts: list[Shift] = state.shifts_for(resource_id, date)
    blocks: list[ResourceBlock] = state.blocks_for(resource_id, date)
    if not shifts:
        return []
    base = merge_intervals(
        [Interval(max(s.start, day_start), min(s.end, day_end)) for s in shifts]
    )
    blocked = merge_intervals(
        [Interval(max(b.start, day_start), min(b.end, day_end)) for b in blocks]
    )
    out: list[Interval] = []
    for win in base:
        cursor = win.start
        for blk in blocked:
            if blk.end <= cursor or blk.start >= win.end:
                continue
            if blk.start >= cursor:
                out.append(Interval(cursor, min(blk.start, win.end)))
            cursor = max(cursor, blk.end)
            if cursor >= win.end:
                break
        if cursor < win.end:
            out.append(Interval(cursor, win.end))
    return [w for w in out if w.end > w.start]


def fits(windows: list[Interval], start: int, end: int) -> bool:
    return any(win.contains_slot(start, end) for win in windows)


def earliest_fit(windows: list[Interval], start_at: int, minutes: int) -> int | None:
    for win in windows:
        candidate = max(win.start, start_at)
        if candidate + minutes <= win.end:
            return candidate
    return None


def template_needs(template: Template, prep_done: bool) -> list[StepSpec]:
    return list(template.runtime_steps(prep_done))


def missing_capabilities(
    state: State, date: str, template: Template, prep_done: bool
) -> list[dict]:
    """静态缺少的能力, 附带台帐中已知的资源。"""
    missing: list[dict] = []
    for spec in template_needs(template, prep_done):
        devices = [
            r for r in state.resources.values()
            if r.kind != "staff" and spec.capability in r.capabilities
        ]
        staff = [
            r for r in state.resources.values()
            if r.kind == "staff" and spec.role in r.capabilities
        ]
        if not devices:
            missing.append({"step": spec.key, "kind": "capability", "capability": spec.capability})
        elif not any(availability_windows(state, r.id, date) for r in devices):
            missing.append({"step": spec.key, "kind": "device_window", "capability": spec.capability})
        if not staff:
            missing.append({"step": spec.key, "kind": "role", "role": spec.role})
        elif not any(availability_windows(state, r.id, date) for r in staff):
            missing.append({"step": spec.key, "kind": "staff_window", "role": spec.role})
    return missing


def describe_windows(windows: list[Interval]) -> str:
    if not windows:
        return "无可用窗口"
    return ", ".join(f"{_hm(w.start)}–{_hm(w.end)}" for w in windows)


def _hm(minutes: int) -> str:
    minutes = max(0, int(minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
