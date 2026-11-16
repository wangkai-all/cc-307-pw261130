"""排程计算.

两种模式:
  global  —— 当日全部未开始检查重排; 多组顺序 + 局部右移, 比较整体目标
             (优先级/截止/平均等待/完成时间)。改期次数可能上升。
  rolling —— 冻结已开始/已完成/爽约, 并默认冻结既有已排预约;
             只为新增插单和显式受影响者做局部调整, 以最少改期为先。

本模块只产出沙盒 Plan, 不写台账; 确认写入由 service 负责。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .domain import (
    DAY_END,
    DAY_START,
    Booking,
    Conflict,
    DomainError,
    Exam,
    Plan,
    State,
    StepSpec,
    Template,
)
from .feasibility import (
    availability_windows,
    capability_ok,
    describe_windows,
    missing_capabilities,
    staff_capability_ok,
)

MAX_GLOBAL_ATTEMPTS = 64


def _hm(minutes: int | None) -> str:
    if minutes is None:
        return "--:--"
    minutes = max(0, int(minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class Candidate:
    exam: Exam
    template: Template
    prev_booking: Booking | None
    prev_start: int | None
    was_scheduled: bool
    steps: list[StepSpec]
    desired_start: int


@dataclass
class Context:
    state: State
    date: str
    mode: str
    start_floor: int
    resources_by_cap: dict[str, list[str]]
    staff_by_role: dict[str, list[str]]
    windows: dict[str, list]
    busy: dict[str, list[tuple[int, int, str]]]
    existing: dict[str, Booking]


@dataclass
class Attempt:
    placed: dict[str, Booking]
    conflicts: list[Conflict]
    score: tuple


def _resource_index(state: State) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    by_cap: dict[str, list[str]] = {}
    by_role: dict[str, list[str]] = {}
    for res in state.resources.values():
        if res.kind == "staff":
            for cap in sorted(res.capabilities):
                by_role.setdefault(cap, []).append(res.id)
        else:
            for cap in sorted(res.capabilities):
                by_cap.setdefault(cap, []).append(res.id)
    for cap in by_cap:
        by_cap[cap].sort()
    for role in by_role:
        by_role[role].sort()
    return by_cap, by_role


def _locked_ids(state: State, date: str) -> set[str]:
    locked: set[str] = set()
    for b in state.booked_exams(date):
        if b.is_locked() or b.status in ("NO_SHOW", "CANCELLED"):
            locked.add(b.exam_id)
    return locked


def _seed_busy(
    existing: dict[str, Booking], fixed: set[str]
) -> dict[str, list[tuple[int, int, str]]]:
    busy: dict[str, list[tuple[int, int, str]]] = {}
    for eid, b in existing.items():
        if eid not in fixed:
            continue
        if b.status in ("CANCELLED", "NO_SHOW"):
            continue
        for slot in b.slots.values():
            for rid in (slot.get("resource_id"), slot.get("staff_id")):
                if rid:
                    busy.setdefault(rid, []).append((slot["start"], slot["end"], eid))
    for rid in busy:
        busy[rid].sort()
    return busy


def _candidates(
    state: State,
    date: str,
    locked: set[str],
    movable: set[str],
    mode: str,
    start_floor: int,
) -> list[tuple[Exam, Booking | None, bool]]:
    """返回 (exam, 已有预约或 None, 是否原本已排)。"""
    out: list[tuple[Exam, Booking | None, bool]] = []
    seen: set[str] = set()
    for b in state.booked_exams(date):
        exam = state.exams.get(b.exam_id)
        if exam is None or b.exam_id in locked or b.status in ("CANCELLED", "NO_SHOW"):
            continue
        if mode == "rolling" and b.status == "SCHEDULED" and b.exam_id not in movable:
            continue
        seen.add(exam.id)
        out.append((exam, b, b.status == "SCHEDULED" and bool(b.slots)))
    for exam in state.exams.values():
        if exam.id in seen or exam.id in locked:
            continue
        if state.exam_date(exam) != date:
            continue
        b = state.bookings.get(exam.id)
        if mode == "rolling" and b is not None and b.status == "SCHEDULED" and b.exam_id not in movable:
            continue
        out.append((exam, b, False))
    return out


def schedule(
    state: State,
    date: str,
    mode: str = "global",
    tie_break: str = "balanced",
    now: int = DAY_START,
    movable_ids: set[str] | None = None,
    max_attempts: int = MAX_GLOBAL_ATTEMPTS,
    progressive_cascade: bool = False,
) -> Plan:
    """执行一次排程计算, 返回沙盒 Plan。

    rolling + progressive_cascade: 从零顺延开始, 若主导入项仍未排入,
    则按优先级顺序逐项扩大顺延邻域 (类似迭代加深), 直到成功或没有可顺延项。
    """
    t0 = time.perf_counter()
    if mode not in ("global", "rolling"):
        raise DomainError(f"未知排程模式 {mode}")
    movable = set(movable_ids or set())
    by_cap, by_role = _resource_index(state)
    windows = {rid: availability_windows(state, rid, date) for rid in state.resources}
    start_floor = max(DAY_START, min(int(now), DAY_END - 1))
    existing = {b.exam_id: b for b in state.booked_exams(date)}
    locked = _locked_ids(state, date)

    candidates: list[Candidate] = []
    static_conflicts: list[Conflict] = []
    for exam, prev, was_scheduled in _candidates(state, date, locked, movable, mode, start_floor):
        template = state.template_for(exam)
        steps = list(template.runtime_steps(exam.prep_done))
        missing = missing_capabilities(state, date, template, exam.prep_done)
        if missing:
            readable = []
            for item in missing:
                if item["kind"] == "capability":
                    readable.append(f"步骤 {item['step']} 无具备 {item['capability']} 的设备")
                elif item["kind"] == "device_window":
                    readable.append(f"步骤 {item['step']} 的 {item['capability']} 设备当日无开放班次")
                elif item["kind"] == "role":
                    readable.append(f"步骤 {item['step']} 无具备 {item['role']} 的人员")
                else:
                    readable.append(f"步骤 {item['step']} 的 {item['role']} 人员当日无班次")
            static_conflicts.append(
                Conflict(
                    "resource_unavailable",
                    f"{exam.label} 无法排入: " + "; ".join(readable),
                    exam_id=exam.id,
                    detail={"missing": missing, "date": date,
                            "label": exam.label, "priority": exam.priority,
                            "exam_kind": exam.kind},
                )
            )
            continue
        desired = prev.start() if prev is not None and prev.start() is not None else start_floor
        candidates.append(
            Candidate(
                exam=exam,
                template=template,
                prev_booking=prev,
                prev_start=prev.start() if prev is not None else None,
                was_scheduled=was_scheduled,
                steps=steps,
                desired_start=max(start_floor, desired),
            )
        )

    # 已完全移除的检查 (例如改期到其它日) 不参与本日排程
    best: Attempt | None = None
    attempts_done = 0
    orders = _orders(candidates, mode, tie_break, existing)
    seen_sig: set[tuple[str, ...]] = set()
    if mode == "rolling" and progressive_cascade:
        return _rolling_progressive(
            state, date, candidates, static_conflicts, existing, locked,
            by_cap, by_role, windows, start_floor, orders[0], tie_break, t0,
        )
    for order in orders:
        if attempts_done >= max(1, max_attempts):
            break
        sig = tuple(c.exam.id for c in order)
        if sig in seen_sig:
            continue
        seen_sig.add(sig)
        ctx = Context(
            state=state,
            date=date,
            mode=mode,
            start_floor=start_floor,
            resources_by_cap=by_cap,
            staff_by_role=by_role,
            windows=windows,
            busy=_seed_busy(
                existing,
                locked | (
                    {eid for eid, b in existing.items()
                     if b.status == "SCHEDULED" and eid not in movable}
                    if mode == "rolling" else set()
                ),
            ),
            existing=existing,
        )
        attempt = _run_order(ctx, order, list(static_conflicts))
        attempts_done += 1
        score = _attempt_score(attempt, candidates, start_floor, mode, tie_break)
        if best is None or score < best.score:
            best = attempt
            best.score = score

    if best is None:
        best = Attempt({}, list(static_conflicts), (len(candidates),))
    placed = best.placed
    conflicts = list(best.conflicts)
    unplaced = _build_unplaced(candidates, placed, conflicts, static_conflicts)
    frozen = _frozen_bookings(state, date, placed, {u["exam_id"] for u in unplaced})
    metrics = _compute_metrics(
        state, date, candidates, placed, conflicts, mode, start_floor, windows,
        frozen=frozen,
    )
    metrics["unplaced"] = len(unplaced)
    metrics["moved"] = len(placed)
    metrics["frozen"] = len(frozen)
    metrics["placed"] = len(placed) + len(frozen)
    elapsed = int((time.perf_counter() - t0) * 1000)
    return Plan(
        id="",
        date=date,
        mode=mode,
        tie_break=tie_break,
        base_version=state.seq,
        resource_versions={rid: r.version for rid, r in sorted(state.resources.items())},
        bookings=placed,
        frozen=frozen,
        unplaced=unplaced,
        conflicts=conflicts,
        metrics=metrics,
        elapsed_ms=elapsed,
        label=("整体优化" if mode == "global" else "滚动局部调整") + f" · {date}",
        locked_ids=sorted(locked),
        attempts_done=attempts_done,
        attempts_total=len(orders),
        exam_signatures={
            c.exam.id: state.signature(c.exam.id) for c in candidates
        }
        | {
            b.exam_id: state.signature(b.exam_id)
            for b in existing.values()
            if b.date == date and b.exam_id not in {c.exam.id for c in candidates}
        },
    )


def _rolling_progressive(
    state: State,
    date: str,
    candidates: list[Candidate],
    static_conflicts: list[Conflict],
    existing: dict[str, Booking],
    locked: set[str],
    by_cap: dict[str, list[str]],
    by_role: dict[str, list[str]],
    windows: dict[str, list],
    start_floor: int,
    base_order: list[Candidate],
    tie_break: str,
    t0: float,
) -> Plan:
    """滚动局部调整的逐级扩大顺延邻域。"""
    order = list(base_order)
    addon_ids = [c.exam.id for c in order if c.exam.kind == "addon"]
    rest_ids = [c.exam.id for c in order if c.exam.kind != "addon"]
    fixed_base = {eid for eid, b in existing.items()
                  if b.status == "SCHEDULED" and eid not in set(addon_ids)}
    # 可逐级纳入顺延的既有检查, 按开始时间与优先级排序
    movable_pool = [
        b for b in existing.values()
        if b.status == "SCHEDULED" and b.exam_id in rest_ids
    ]
    movable_pool.sort(key=lambda b: ((b.start() or DAY_END), state.exams[b.exam_id].priority, b.exam_id))
    best: Attempt | None = None
    attempts = 0
    for level in range(0, len(movable_pool) + 1):
        movable_now = {b.exam_id for b in movable_pool[:level]}
        ctx = Context(
            state=state,
            date=date,
            mode="rolling",
            start_floor=start_floor,
            resources_by_cap=by_cap,
            staff_by_role=by_role,
            windows=windows,
            busy=_seed_busy(existing, locked | (set(existing) - movable_now - set(addon_ids))),
            existing=existing,
        )
        order_now = [
            c for c in order
            if c.exam.id in movable_now or c.exam.id in addon_ids or c.exam.id not in fixed_base
        ]
        attempt = _run_order(ctx, order_now, list(static_conflicts))
        attempts += 1
        score = _attempt_score(attempt, candidates, start_floor, "rolling", tie_break)
        if best is None or score < best.score:
            best = attempt
            best.score = score
        if all(c.exam.id in attempt.placed for c in candidates):
            break
    assert best is not None
    placed = best.placed
    conflicts = list(best.conflicts)
    unplaced = _build_unplaced(candidates, placed, conflicts, static_conflicts)
    frozen = _frozen_bookings(state, date, placed, {u["exam_id"] for u in unplaced})
    metrics = _compute_metrics(
        state, date, candidates, placed, conflicts, "rolling", start_floor, windows,
        frozen=frozen,
    )
    metrics["unplaced"] = len(unplaced)
    metrics["moved"] = len(placed)
    metrics["frozen"] = len(frozen)
    metrics["placed"] = len(placed) + len(frozen)
    elapsed = int((time.perf_counter() - t0) * 1000)
    return Plan(
        id="",
        date=date,
        mode="rolling",
        tie_break=tie_break,
        base_version=state.seq,
        resource_versions={rid: r.version for rid, r in sorted(state.resources.items())},
        bookings=placed,
        frozen=frozen,
        unplaced=unplaced,
        conflicts=conflicts,
        metrics=metrics,
        elapsed_ms=elapsed,
        label="滚动局部调整 · " + date,
        locked_ids=sorted(locked),
        attempts_done=attempts,
        attempts_total=len(movable_pool) + 1,
        exam_signatures={
            c.exam.id: state.signature(c.exam.id) for c in candidates
        }
        | {
            b.exam_id: state.signature(b.exam_id)
            for b in existing.values()
            if b.date == date and b.exam_id not in {c.exam.id for c in candidates}
        },
    )


# ---------------------------------------------------------------------------
# 排序候选


def _orders(
    candidates: list[Candidate],
    mode: str,
    tie_break: str,
    existing: dict[str, Booking],
) -> list[list[Candidate]]:
    base = list(candidates)

    def prio(c: Candidate) -> tuple:
        due = c.exam.due_by if c.exam.due_by is not None else DAY_END
        return (c.exam.priority, due, c.exam.created_seq, c.exam.id)

    def due_first(c: Candidate) -> tuple:
        due = c.exam.due_by if c.exam.due_by is not None else DAY_END
        return (due, c.exam.priority, c.exam.created_seq, c.exam.id)

    def oldest(c: Candidate) -> tuple:
        return (c.exam.created_seq, c.exam.priority, c.exam.id)

    def longest(c: Candidate) -> tuple:
        total = sum(s.minutes + s.gap_after for s in c.steps)
        return (-total, c.exam.priority, c.exam.id)

    def prep_first(c: Candidate) -> tuple:
        prep = next((s for s in c.steps if s.key == "prep"), None)
        return (-(prep.minutes if prep else 0), c.exam.priority, c.exam.id)

    if mode == "rolling":
        addons = sorted(
            [c for c in base if c.exam.kind == "addon"],
            key=lambda c: (c.exam.priority, c.exam.created_seq, c.exam.id),
        )
        rest = sorted(
            [c for c in base if c.exam.kind != "addon"],
            key=lambda c: (c.exam.priority, c.prev_start or DAY_END, c.exam.id),
        )
        return [addons + rest]

    orders: list[list[Candidate]] = [
        sorted(base, key=prio),
        sorted(base, key=due_first),
        sorted(base, key=oldest),
        sorted(base, key=longest),
        sorted(base, key=prep_first),
    ]
    if tie_break == "fairness":
        orders.insert(0, _interleave(sorted(base, key=prio)))
    elif tie_break == "stability":
        orders.insert(0, sorted(base, key=lambda c: (not c.was_scheduled, prio(c))))
    else:
        # balanced 默认也尝试「已排优先 + 优先级」的稳定顺序
        orders.insert(0, sorted(base, key=lambda c: (not c.was_scheduled, prio(c))))
        orders.insert(1, sorted(base, key=prio))
    return orders


def _interleave(items: list[Candidate]) -> list[Candidate]:
    if len(items) < 3:
        return items
    out: list[Candidate] = []
    lo, hi = 0, len(items) - 1
    while lo <= hi:
        out.append(items[lo])
        lo += 1
        if lo <= hi:
            out.append(items[hi])
            hi -= 1
    return out


# ---------------------------------------------------------------------------
# 放置


def _run_order(
    ctx: Context,
    order: list[Candidate],
    static_conflicts: list[Conflict],
    displaced: list[tuple[int, str]] | None = None,
) -> Attempt:
    placed: dict[str, Booking] = {}
    conflicts = list(static_conflicts)
    for cand in order:
        target = cand.desired_start if ctx.mode == "rolling" else ctx.start_floor
        slots = _find_chain(ctx, cand, target)
        if slots is None:
            slots = _retry_later(ctx, cand)
        if slots is None:
            conflicts.append(_no_feasible_conflict(ctx, cand))
            continue
        if displaced is not None:
            displaced.append((cand.exam.priority, cand.exam.id))
        prev = ctx.existing.get(cand.exam.id)
        placed[cand.exam.id] = Booking(
            exam_id=cand.exam.id,
            date=ctx.date,
            status="SCHEDULED",
            slots=slots,
            revision=(prev.revision + 1) if prev is not None and prev.slots else 0,
            changed_by=ctx.mode,
        )
    return Attempt(placed, conflicts, ())


def _find_chain(ctx: Context, cand: Candidate, start_at: int) -> dict[str, dict] | None:
    slots: dict[str, dict] = {}
    cursor = max(start_at, ctx.start_floor)
    touched: list[tuple[str, tuple[int, int, str]]] = []
    for spec in cand.steps:
        option = _place_step(ctx, cand.exam, spec, cursor)
        if option is None:
            _rollback(ctx, touched)
            return None
        start, rid, sid = option
        end = start + spec.minutes
        slots[spec.key] = {
            "start": start,
            "end": end,
            "resource_id": rid,
            "staff_id": sid,
            "resource_name": ctx.state.resource(rid).name,
            "staff_name": ctx.state.resource(sid).name,
            "label": spec.label,
            "capability": spec.capability,
        }
        for holder in (rid, sid):
            entry = (start, end, cand.exam.id)
            ctx.busy.setdefault(holder, []).append(entry)
            ctx.busy[holder].sort()
            touched.append((holder, entry))
        cursor = end + spec.gap_after
    return slots


def _place_step(
    ctx: Context, exam: Exam, spec: StepSpec, cursor: int
) -> tuple[int, str, str] | None:
    resources = [
        r for r in ctx.resources_by_cap.get(spec.capability, [])
        if ctx.windows.get(r) and capability_ok(ctx.state, r, spec.capability)
    ]
    staff = [
        s for s in ctx.staff_by_role.get(spec.role, [])
        if ctx.windows.get(s) and staff_capability_ok(ctx.state, s, spec.role)
    ]
    if not resources or not staff:
        return None
    starts: set[int] = set()
    for rid in resources + staff:
        for win in ctx.windows[rid]:
            starts.add(max(cursor, win.start))
        for _s, e, _x in ctx.busy.get(rid, []):
            if e >= cursor:
                starts.add(e)
    for start in sorted(starts):
        end = start + spec.minutes
        if end > DAY_END:
            break
        rid = next(
            (
                r for r in resources
                if _covers(ctx.windows[r], start, end)
                and _free(ctx.busy.get(r, []), start, end)
            ),
            None,
        )
        if rid is None:
            continue
        sid = next(
            (
                s for s in staff
                if _covers(ctx.windows[s], start, end)
                and _free(ctx.busy.get(s, []), start, end)
            ),
            None,
        )
        if sid is not None:
            return start, rid, sid
    return None


def _retry_later(ctx: Context, cand: Candidate) -> dict[str, dict] | None:
    """失败后, 以现有占用结束点为候选起点再试, 但不越过当日结束。"""
    horizon: set[int] = set()
    for ranges in ctx.busy.values():
        for _s, e, _x in ranges:
            if ctx.start_floor <= e < DAY_END:
                horizon.add(e)
    for target in sorted(horizon):
        slots = _find_chain(ctx, cand, max(target, ctx.start_floor))
        if slots is not None:
            return slots
    return None


def _covers(windows: list, start: int, end: int) -> bool:
    return any(w.start <= start and end <= w.end for w in windows)


def _free(ranges: list[tuple[int, int, str]], start: int, end: int) -> bool:
    return not any(s < end and start < e for s, e, _ in ranges)


def _rollback(ctx: Context, touched: list[tuple[str, tuple[int, int, str]]]) -> None:
    for holder, entry in touched:
        try:
            ctx.busy[holder].remove(entry)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# 评分、冲突与指标


def _attempt_score(
    attempt: Attempt,
    candidates: list[Candidate],
    start_floor: int,
    mode: str,
    tie_break: str,
) -> tuple:
    placed = attempt.placed
    unplaced = sum(1 for c in candidates if c.exam.id not in placed)
    if not placed:
        return (unplaced, 10**9, 10**9, 10**9)
    waits: list[int] = []
    weighted_wait = 0
    urgent = 0
    changed = 0
    overdue = 0
    ends: list[int] = []
    for c in candidates:
        b = placed.get(c.exam.id)
        if b is None:
            continue
        start = b.start()
        if start is None:
            continue
        wait = max(0, start - start_floor)
        waits.append(wait)
        # 优先级越高权重越大; 用加权等待而不是总等待做目标,
        # 否则「把插单排到最后」会显得总等待更小 —— 那是不公平的伪优化。
        weighted_wait += wait * _priority_weight(c.exam.priority)
        if c.exam.priority <= 2:
            urgent = max(urgent, wait)
        if c.exam.due_by is not None:
            overdue += max(0, (b.end() or start) - c.exam.due_by)
        if c.was_scheduled and c.prev_start is not None and start != c.prev_start:
            changed += 1
        ends.append(b.end() or start)
    total_wait = sum(waits)
    makespan = max(ends) if ends else DAY_END
    if mode == "rolling":
        # 滚动: 先救回最多检查, 再压改期与紧急等待
        return (unplaced, changed, -urgent, weighted_wait, makespan)
    if tie_break == "fairness":
        return (unplaced, overdue, -urgent, weighted_wait, changed, makespan)
    if tie_break == "stability":
        return (unplaced, overdue, changed, -urgent, weighted_wait, makespan)
    return (unplaced, overdue, weighted_wait, -urgent, changed, makespan)


def _priority_weight(priority: int) -> int:
    return {1: 8, 2: 4, 3: 3, 4: 2}.get(priority, 1)


def _no_feasible_conflict(ctx: Context, cand: Candidate) -> Conflict:
    reasons: list[str] = []
    for spec in cand.steps:
        resources = [
            r for r in ctx.resources_by_cap.get(spec.capability, [])
            if ctx.windows.get(r)
        ]
        if not resources:
            reasons.append(
                f"步骤「{spec.label}」当日没有具备 {spec.capability} 且开放的设备"
            )
        else:
            rid = resources[0]
            busy = ctx.busy.get(rid, [])
            busy_desc = ", ".join(f"{_hm(s)}–{_hm(e)}" for s, e, _x in busy[:5])
            reasons.append(
                f"步骤「{spec.label}」在 {ctx.state.resource(rid).name} 上需连续 {spec.minutes} 分钟; "
                f"窗口 {describe_windows(ctx.windows[rid])}"
                + (f", 已占用 {busy_desc}" if busy_desc else "")
            )
        staff = [s for s in ctx.staff_by_role.get(spec.role, []) if ctx.windows.get(s)]
        if not staff:
            reasons.append(f"步骤「{spec.label}」没有当班且具备 {spec.role} 的人员")
    msg = (
        f"{cand.exam.label} 未排入: " + "; ".join(reasons)
        if reasons
        else f"{cand.exam.label} 未排入"
    )
    return Conflict(
        "no_feasible_slot",
        msg,
        exam_id=cand.exam.id,
        detail={
            "priority": cand.exam.priority,
            "kind": cand.exam.kind,
            "mode": ctx.mode,
            "reasons": reasons,
            "steps": [s.key for s in cand.steps],
        },
    )


def _frozen_bookings(
    state: State,
    date: str,
    placed: dict[str, Booking],
    exclude: set[str] | None = None,
) -> dict[str, Booking]:
    """方案视为不可动、但仍属于当日排程的既有预约 (已开始/完成/冻结)。

    它们不计入「改期」，但时间线需要展示，指标也需要把它们算进当日已排。
    """
    frozen: dict[str, Booking] = {}
    exclude = exclude or set()
    for b in state.booked_exams(date):
        if b.exam_id in placed or b.exam_id in exclude:
            continue
        if b.status in ("CANCELLED", "NO_SHOW"):
            continue
        if not b.slots:
            continue
        frozen[b.exam_id] = b
    return frozen


def _build_unplaced(
    candidates: list[Candidate],
    placed: dict[str, Booking],
    conflicts: list[Conflict],
    static_conflicts: list[Conflict] | None = None,
) -> list[dict]:
    by_exam = {c.exam_id: c for c in conflicts if c.exam_id}
    out: list[dict] = []
    seen: set[str] = set()
    for cand in candidates:
        if cand.exam.id in placed:
            continue
        seen.add(cand.exam.id)
        c = by_exam.get(cand.exam.id)
        out.append(
            {
                "exam_id": cand.exam.id,
                "label": cand.exam.label,
                "priority": cand.exam.priority,
                "kind": cand.exam.kind,
                "reason": c.message if c else "未排入",
                "conflict_kind": c.kind if c else "unknown",
            }
        )
    # 资源能力/班次缺失的检查不会进入候选集, 但同样属于「未排」, 必须显式列出
    for conflict in static_conflicts or []:
        exam_id = conflict.exam_id
        if not exam_id or exam_id in seen:
            continue
        detail = conflict.detail or {}
        out.append(
            {
                "exam_id": exam_id,
                "label": detail.get("label", exam_id),
                "priority": detail.get("priority", 5),
                "kind": detail.get("exam_kind", "scheduled"),
                "reason": conflict.message,
                "conflict_kind": conflict.kind,
            }
        )
    return out


def _compute_metrics(
    state: State,
    date: str,
    candidates: list[Candidate],
    placed: dict[str, Booking],
    conflicts: list[Conflict],
    mode: str,
    start_floor: int,
    windows: dict[str, list],
    frozen: dict[str, Booking] | None = None,
) -> dict:
    waits: list[int] = []
    reschedules = 0
    makespan_end = 0
    urgent_wait: list[int] = []
    per_resource: dict[str, int] = {}
    total_busy = 0
    cand_by_id = {c.exam.id: c for c in candidates}
    for exam_id, booking in placed.items():
        cand = cand_by_id.get(exam_id)
        start, end = booking.start(), booking.end()
        if cand is None or start is None or end is None:
            continue
        ref = (
            cand.prev_start
            if cand.was_scheduled and cand.prev_start is not None
            else start_floor
        )
        if cand.was_scheduled and cand.prev_start is not None and start != cand.prev_start:
            reschedules += 1
        wait = max(0, start - ref)
        waits.append(wait)
        if cand.exam.priority <= 2:
            urgent_wait.append(wait)
        makespan_end = max(makespan_end, end)
        for slot in booking.slots.values():
            total_busy += slot["end"] - slot["start"]
            rid = slot.get("resource_id")
            if rid:
                per_resource[rid] = per_resource.get(rid, 0) + (slot["end"] - slot["start"])
    frozen = frozen or {}
    for booking in frozen.values():
        start, end = booking.start(), booking.end()
        if start is None or end is None:
            continue
        makespan_end = max(makespan_end, end)
        for slot in booking.slots.values():
            total_busy += slot["end"] - slot["start"]
            rid = slot.get("resource_id")
            if rid:
                per_resource[rid] = per_resource.get(rid, 0) + (slot["end"] - slot["start"])
    avg_wait = round(sum(waits) / len(waits), 1) if waits else 0
    total_capacity = sum(
        w.end - w.start for rid in per_resource for w in windows.get(rid, [])
    )
    spread = (max(per_resource.values()) - min(per_resource.values())) if per_resource else 0
    return {
        "mode": mode,
        "placed": len(placed),
        "conflicts": len(conflicts),
        "reschedules": reschedules,
        "avg_wait": avg_wait,
        "max_wait": max(waits) if waits else 0,
        "urgent_max_wait": max(urgent_wait) if urgent_wait else 0,
        "makespan_end": makespan_end,
        "makespan_hm": _hm(makespan_end),
        "resource_spread": spread,
        "utilization": round(total_busy / total_capacity * 100, 1) if total_capacity else 0,
        "per_resource_busy": per_resource,
        "fairness": _fairness({**frozen, **placed}, state, start_floor),
        "begins_before_now": sum(
            1
            for b in placed.values()
            if (b.start() or DAY_END) < start_floor
        ),
    }


def _fairness(placed: dict[str, Booking], state: State, start_floor: int) -> float:
    """简化公平度: 各优先级组平均等待的相对差距 (0-100, 越高越均衡)。"""
    groups: dict[int, list[int]] = {}
    for exam_id, booking in placed.items():
        exam = state.exams.get(exam_id)
        start = booking.start()
        if exam is None or start is None:
            continue
        groups.setdefault(exam.priority, []).append(max(0, start - start_floor))
    if len(groups) < 2:
        return 100.0
    avgs = [sum(v) / len(v) for v in groups.values()]
    hi, lo = max(avgs), min(avgs)
    if hi <= 0:
        return 100.0
    return round(100 * (1 - (hi - lo) / max(hi, 60)), 1)
