"""应用服务层: 试算沙盒、方案确认、执行、回放.

关键约定:
- 试算 (trial) 从不写正式台账; 结果写入独立 trials 目录。
- 确认 (confirm) 核对:
    * 方案基于的台账序号 base_version 必须仍是当前版本 (台账只能追加,
      因此 seq 未变意味着资源/预约没有被别人改动);
    * 每个资源当前版本 == 方案记录的版本;
    * 每个要落地的检查签名 (状态/准备/版本/日期) 未变;
    * 已开始的检查不允许被移动, 计划中也不能包含开始时间早于当前时钟的检查。
- 部分执行后再排: started/completed 检查由 planner 永久冻结。
- 多个计划员并行: 台账用追加 + seq 冲突检测; 每人试算在内存中并行安全。
"""

from __future__ import annotations

import random
import string
import threading
import time
from typing import Any

from .domain import (
    DAY_START,
    Booking,
    Conflict,
    DomainError,
    Plan,
    State,
    VersionConflict,
    hm,
)
from .ledger import EventLedger, TrialStore
from .planner import MAX_GLOBAL_ATTEMPTS, schedule

class ConfirmError(DomainError):
    def __init__(self, message: str, conflicts: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.conflicts = conflicts or []

    def to_dict(self) -> dict[str, Any]:
        return {"error": str(self), "conflicts": self.conflicts}


class InterruptedCompute(DomainError):
    """演示/真实计算中断: 检查点已存在, 但本次计算未完成。"""

    def __init__(self, trial_id: str, message: str):
        super().__init__(message)
        self.trial_id = trial_id


class SchedulingService:
    def __init__(self, ledger: EventLedger, trials: TrialStore):
        self.ledger = ledger
        self.trials = trials
        self._confirm_lock = threading.RLock()
        # 启动即做一次中断恢复: running 的检查点标记为 interrupted, 正式台账不变
        self.recovered_trials = trials.recover_interrupted()

    # ------------------------------------------------------------------
    # 读取
    def snapshot(self, date: str, state: State | None = None) -> dict[str, Any]:
        state = self.ledger.state if state is None else state
        now = state.now_for(date)
        exams = []
        bookings = state.bookings
        last_confirm = None
        for op in reversed(state.ops):
            if op.get("type") == "booking.confirmed" and op.get("date") == date:
                last_confirm = {
                    "seq": op.get("seq"),
                    "mode": op.get("mode"),
                    "planner": op.get("planner"),
                    "bookings": op.get("bookings"),
                    "plan_id": op.get("plan_id"),
                }
                break
        for exam in state.exams.values():
            booking = bookings.get(exam.id)
            bdate = booking.date if booking else exam.requested_date
            if bdate != date:
                continue
            tpl = state.templates.get(exam.template_id)
            exams.append(
                {
                    **exam.to_dict(),
                    "date": bdate,
                    "status": booking.status if booking else "UNSCHEDULED",
                    "slots": booking.slots if booking else {},
                    "start_hm": hm(booking.start()) if booking else None,
                    "end_hm": hm(booking.end()) if booking else None,
                    "revision": booking.revision if booking else 0,
                    "template_name": tpl.name if tpl else exam.template_id,
                    "prep_minutes": tpl.prep_minutes if tpl else 0,
                    "prep_instruction": tpl.prep_instruction if tpl else "",
                    "total_minutes": tpl.to_dict()["total_minutes"] if tpl else 0,
                    "steps": [s.to_dict() for s in tpl.runtime_steps(exam.prep_done)] if tpl else [],
                }
            )
        exams.sort(key=lambda e: (e["start_hm"] is None, e["start_hm"] or "", e["id"]))
        resources = []
        for res in sorted(state.resources.values(), key=lambda r: (r.kind, r.id)):
            wins = _windows_for(state, res.id, date)
            blocks = [b.to_dict() for b in state.blocks_for(res.id, date)]
            resources.append(
                {
                    **res.to_dict(),
                    "windows": [
                        {"start": w[0], "end": w[1], "start_hm": hm(w[0]), "end_hm": hm(w[1])}
                        for w in wins
                    ],
                    "blocks": blocks,
                }
            )
        plan_bookings = None
        if last_confirm and last_confirm.get("plan_id"):
            trial = self.trials.load(last_confirm["plan_id"])
            if trial is not None:
                plan_bookings = trial.get("bookings")
        dates = sorted({e.requested_date for e in state.exams.values()}
                       | {b.date for b in state.bookings.values()}
                       | {s.date for s in state.shifts})
        return {
            "date": date,
            "now": now,
            "now_hm": hm(now),
            "seq": state.seq,
            "dates": dates,
            "exams": exams,
            "resources": resources,
            "templates": [t.to_dict() for t in state.templates.values()],
            "counts": {
                "exams": len(exams),
                "scheduled": sum(1 for e in exams if e["status"] == "SCHEDULED"),
                "started": sum(1 for e in exams if e["status"] == "STARTED"),
                "completed": sum(1 for e in exams if e["status"] == "COMPLETED"),
                "unplaced": sum(1 for e in exams if e["status"] == "UNSCHEDULED"),
                "no_show": sum(1 for e in exams if e["status"] == "NO_SHOW"),
            },
            "addon_pool": self._addon_pool(state),
            "last_confirm": last_confirm,
            "last_plan_bookings": plan_bookings,
            "ops": [
                op for op in state.ops
                if op.get("type") in (
                    "booking.started", "booking.completed", "exam.no_show",
                    "exam.cancelled", "booking.reset", "booking.confirmed",
                )
            ][-40:],
        }

    def _addon_pool(self, state: State) -> list[dict[str, Any]]:
        from .demo import demo_addon_pool

        existing = set(state.exams)
        return [a for a in demo_addon_pool() if a["id"] not in existing]

    # ------------------------------------------------------------------
    # 排程计算 / 试算
    def compute(
        self,
        date: str,
        mode: str,
        tie_break: str = "balanced",
        movable_ids: list[str] | None = None,
        planner: str = "",
        tier: str = "quick",
        cascade: int = 0,
        persist: bool = True,
    ) -> dict[str, Any]:
        """在台账快照上计算, 生成 trial 记录 (persist=False 仅用于预览)。"""
        t0 = time.perf_counter()
        state = self.ledger.state
        movable = set(movable_ids or [])
        if mode == "rolling" and cascade > 0:
            movable |= _cascade_ids(state, date, cascade)
        plan = schedule(
            state,
            date,
            mode=mode,
            tie_break=tie_break,
            now=state.now_for(date),
            movable_ids=movable,
            max_attempts=1 if tier == "parallel" else MAX_GLOBAL_ATTEMPTS,
            progressive_cascade=(mode == "rolling" and cascade > 0),
        )
        plan.id = _trial_id()
        plan.label = plan.label + (f" · {planner}" if planner else "")
        payload = plan.to_dict()
        payload.update(
            {
                "status": "complete",
                "created_at": _iso(),
                "planner": planner,
                "tier": tier,
                "base_seq": state.seq,
                "cascade": cascade,
                "duration_ms": int((time.perf_counter() - t0) * 1000),
            }
        )
        if persist:
            self.trials.save(payload)
        return payload

    def interrupted_trial(self, date: str, mode: str, planner: str = "") -> dict[str, Any]:
        """演示「计算中断」: 先写检查点 (status=running), 然后计算失败。

        恢复流程 (recover_interrupted) 会把该试算标记为 interrupted,
        正式台账不受影响; 重新试算即可。
        """
        state = self.ledger.state
        plan = schedule(
            state, date, mode=mode, tie_break="balanced", now=state.now_for(date)
        )
        plan.id = _trial_id()
        payload = plan.to_dict()
        payload.update(
            {
                "status": "running",
                "created_at": _iso(),
                "planner": planner,
                "tier": "interrupted-demo",
                "base_seq": state.seq,
                "checkpoint": "before_commit",
            }
        )
        self.trials.save(payload)
        raise InterruptedCompute(
            payload["id"],
            "计算在提交结果前被中断 (演示); 检查点已保留, 正式台账未写入",
        )

    def simulate(self, date: str, mode: str, **kwargs: Any) -> dict[str, Any]:
        """不落 trial 的快速试算 (用于并行试算对比/预览)。"""
        return self.compute(
            date,
            mode,
            tie_break=kwargs.get("tie_break", "balanced"),
            movable_ids=kwargs.get("movable_ids"),
            planner=kwargs.get("planner", ""),
            cascade=int(kwargs.get("cascade") or 0),
            persist=False,
        )

    def get_trial(self, trial_id: str) -> dict[str, Any] | None:
        return self.trials.load(trial_id)

    def list_trials(self, limit: int = 20) -> list[dict[str, Any]]:
        out = []
        for t in self.trials.list(limit):
            out.append(
                {
                    "id": t["id"],
                    "date": t["date"],
                    "mode": t["mode"],
                    "tie_break": t.get("tie_break"),
                    "planner": t.get("planner"),
                    "status": t.get("status"),
                    "created_at": t.get("created_at"),
                    "metrics": t.get("metrics"),
                    "base_version": t.get("base_version"),
                    "attempts_done": t.get("attempts_done"),
                    "attempts_total": t.get("attempts_total"),
                    "interrupted_at": t.get("interrupted_at"),
                    "confirmed_at": t.get("confirmed_at"),
                    "confirmed_seq": t.get("confirmed_seq"),
                }
            )
        return out

    def parallel_trials(self, date: str, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """多个计划员并行试算 (线程并行, 各用同一不可变快照)。"""
        results: list[dict[str, Any] | None] = [None] * len(specs)
        errors: list[str | None] = [None] * len(specs)
        snapshot_seq = self.ledger.seq

        def run(i: int, spec: dict[str, Any]) -> None:
            try:
                result = self.compute(
                    date,
                    spec.get("mode", "global"),
                    tie_break=spec.get("tie_break", "balanced"),
                    planner=spec.get("planner", f"计划员{i + 1}"),
                    tier="parallel",
                    cascade=int(spec.get("cascade") or 0),
                )
                if result.get("base_version") != snapshot_seq:
                    result["warnings"] = [
                        f"试算期间台账从 v{snapshot_seq} 变为 v{result.get('base_version')}; "
                        "确认时将以版本核对结果为准"
                    ]
                results[i] = result
            except Exception as exc:  # 单个试算失败不影响其它人
                errors[i] = str(exc)

        threads = [threading.Thread(target=run, args=(i, s)) for i, s in enumerate(specs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return [
            {"spec": s, "result": results[i], "error": errors[i]}
            for i, s in enumerate(specs)
        ]

    # ------------------------------------------------------------------
    # 确认
    def confirm(self, trial_id: str, planner: str = "", note: str = "") -> dict[str, Any]:
        with self._confirm_lock:
            return self._confirm_locked(trial_id, planner, note)

    def _confirm_locked(self, trial_id: str, planner: str, note: str) -> dict[str, Any]:
        trial = self.trials.load(trial_id)
        if trial is None:
            raise ConfirmError(f"试算 {trial_id} 不存在")
        state = self.ledger.state
        if int(trial.get("base_version", 0)) > state.seq:
            raise ConfirmError("试算版本超前于台账, 记录不可信")
        # 确认的核心核对: 资源版本 + 检查签名 + 方案在新台账下的合法性。
        # 台账 seq 变化本身不拒绝, 但任何影响资源或检查的变更都会被下面三项捕获。
        resource_conflicts = self._check_resource_versions(trial, state)
        if resource_conflicts:
            raise ConfirmError("资源版本不一致, 方案可能已过期", resource_conflicts)
        exam_conflicts = self._check_exam_signatures(trial, state)
        if exam_conflicts:
            raise ConfirmError("检查状态在试算后发生变化", exam_conflicts)
        legality = self._check_plan_legality(trial, state)
        if legality:
            raise ConfirmError("方案包含不合法时段, 拒绝确认", legality)
        plan_payload = {
            "date": trial["date"],
            "mode": trial["mode"],
            "planner": planner,
            "plan_id": trial_id,
            "note": note,
            "bookings": {
                eid: {
                    "date": b["date"],
                    "slots": b["slots"],
                }
                for eid, b in trial["bookings"].items()
            },
            "unplaced_ids": [u["exam_id"] for u in trial.get("unplaced", [])],
        }
        result = self.ledger.append("booking.confirmed", plan_payload)
        trial["status"] = "confirmed"
        trial["confirmed_at"] = _iso()
        trial["confirmed_seq"] = result.seq
        trial["confirmed_by"] = planner
        self.trials.save(trial)
        return {
            "trial_id": trial_id,
            "confirmed_seq": result.seq,
            "bookings": len(plan_payload["bookings"]),
            "unplaced": len(plan_payload["unplaced_ids"]),
            "warnings": (
                [f"确认时台账已从 v{trial.get('base_version')} 前进到 v{state.seq}, "
                 "资源版本与检查签名核对通过"]
                if trial.get("base_version") != state.seq
                else []
            ),
        }

    def _check_resource_versions(self, trial: dict[str, Any], state: State) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for rid, expected in trial.get("resource_versions", {}).items():
            cur = state.resources.get(rid)
            if cur is None:
                conflicts.append({"kind": "resource_missing", "resource_id": rid,
                                  "message": f"资源 {rid} 已不存在"})
                seen.add(rid)
            elif cur.version != expected:
                conflicts.append(
                    {
                        "kind": "resource_version",
                        "resource_id": rid,
                        "message": f"{cur.name} 由 v{expected} 变为 v{cur.version}",
                    }
                )
                seen.add(rid)
        # 试算之后的资源变更事件: 即便版本号碰巧相同, 也要指出是哪次变更
        recent = self.ledger.events(
            types=["shift.added", "block.added", "resource.version_bumped"]
        )
        for event in recent:
            if event["seq"] <= int(trial.get("base_version", 0)):
                continue
            rid = event["payload"].get("resource_id")
            if rid and rid not in seen:
                seen.add(rid)
                conflicts.append(
                    {
                        "kind": "resource_changed",
                        "resource_id": rid,
                        "message": f"试算之后资源 {rid} 有变更事件 #{event['seq']} ({event['type']})",
                    }
                )
        return conflicts

    def _check_exam_signatures(self, trial: dict[str, Any], state: State) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        for eid, expected in trial.get("exam_signatures", {}).items():
            if eid not in state.exams:
                conflicts.append({"kind": "exam_missing", "exam_id": eid,
                                  "message": f"检查 {eid} 已不存在"})
                continue
            current = state.signature(eid)
            if current != expected:
                booking = state.bookings.get(eid)
                if booking is not None and booking.status in ("STARTED", "COMPLETED"):
                    msg = f"{state.exams[eid].label} 已经开始/完成, 不可再改"
                else:
                    msg = f"{state.exams[eid].label} 的状态/版本在试算后变化 ({expected} -> {current})"
                conflicts.append({"kind": "exam_changed", "exam_id": eid, "message": msg})
        return conflicts

    def _check_plan_legality(self, trial: dict[str, Any], state: State) -> list[dict[str, Any]]:
        from .feasibility import availability_windows, fits, capability_ok, staff_capability_ok

        problems: list[dict[str, Any]] = []
        date = trial["date"]
        windows = {rid: availability_windows(state, rid, date) for rid in state.resources}
        now = state.now_for(date)
        # 占用收集: 既不在方案内、也没被明确置为未排的预约 (已开始/完成/冻结)
        # 会继续占用资源; 方案内的预约由下面的计划内互斥检查覆盖。
        unplaced_ids = {
            u.get("exam_id") for u in trial.get("unplaced", []) if u.get("exam_id")
        }
        skip_ids = set(trial.get("bookings", {})) | unplaced_ids
        occupied: dict[str, list[tuple[int, int, str]]] = {}
        for b in state.booked_exams(date):
            if b.exam_id in skip_ids:
                continue
            if b.status in ("CANCELLED", "NO_SHOW"):
                continue
            for step in b.slots.values():
                for rid in (step.get("resource_id"), step.get("staff_id")):
                    if rid:
                        occupied.setdefault(rid, []).append((step["start"], step["end"], b.exam_id))
        for eid, booking in trial["bookings"].items():
            exam = state.exams.get(eid)
            locked_booking = state.bookings.get(eid)
            if locked_booking is not None and locked_booking.status in ("STARTED", "COMPLETED"):
                problems.append(
                    {
                        "kind": "locked_exam_moved",
                        "exam_id": eid,
                        "message": f"{exam.label if exam else eid} 已经开始/完成, 方案不得再移动",
                    }
                )
                continue
            if exam is None:
                problems.append({"kind": "exam_missing", "exam_id": eid, "message": f"检查 {eid} 不存在"})
                continue
            template = state.templates.get(exam.template_id)
            if template is None:
                problems.append({"kind": "template_missing", "exam_id": eid,
                                 "message": f"检查 {eid} 的模板不存在"})
                continue
            spec_by_key = {s.key: s for s in template.runtime_steps(exam.prep_done)}
            for key, slot in booking["slots"].items():
                spec = spec_by_key.get(key)
                if spec is None:
                    problems.append({"kind": "unknown_step", "exam_id": eid,
                                     "message": f"{exam.label} 的步骤 {key} 不属于当前模板"})
                    continue
                if not fits(windows.get(slot["resource_id"], []), slot["start"], slot["end"]):
                    problems.append(
                        {
                            "kind": "outside_window",
                            "exam_id": eid,
                            "resource_id": slot["resource_id"],
                            "message": f"{exam.label} 的步骤 {key} 落在 {slot['resource_id']} 不可用时段",
                        }
                    )
                if not capability_ok(state, slot["resource_id"], spec.capability):
                    problems.append(
                        {
                            "kind": "capability",
                            "exam_id": eid,
                            "message": f"{exam.label} 的步骤 {key} 资源能力不匹配",
                        }
                    )
                if not staff_capability_ok(state, slot["staff_id"], spec.role):
                    problems.append(
                        {
                            "kind": "staff_capability",
                            "exam_id": eid,
                            "message": f"{exam.label} 的步骤 {key} 人员角色不匹配",
                        }
                    )
                for rid in (slot["resource_id"], slot["staff_id"]):
                    for s, e, other in occupied.get(rid, []):
                        if other != eid and s < slot["end"] and slot["start"] < e:
                            other_label = state.exams[other].label if other in state.exams else other
                            problems.append(
                                {
                                    "kind": "overlap",
                                    "exam_id": eid,
                                    "resource_id": rid,
                                    "message": f"{exam.label} 与 {other_label} 在 {rid} 的 {hm(s)}–{hm(e)} 冲突",
                                }
                            )
                    occupied.setdefault(rid, []).append(
                        (slot["start"], slot["end"], eid)
                    )
            if template.prep_minutes > 0 and not exam.prep_done:
                prep_slot = booking["slots"].get("prep")
                first_scan = min(
                    (slot for key, slot in booking["slots"].items() if key != "prep"),
                    key=lambda x: x["start"],
                    default=None,
                )
                if prep_slot is None:
                    problems.append(
                        {
                            "kind": "prep_missing",
                            "exam_id": eid,
                            "message": f"{exam.label} 准备未完成, 且方案没有安排准备步骤",
                        }
                    )
                elif first_scan is not None and prep_slot["end"] > first_scan["start"]:
                    problems.append(
                        {
                            "kind": "prep_order",
                            "exam_id": eid,
                            "message": f"{exam.label} 的准备步骤必须在检查步骤之前完成",
                        }
                    )
            if booking["slots"] and min(s["start"] for s in booking["slots"].values()) < now:
                problems.append(
                    {
                        "kind": "in_past",
                        "exam_id": eid,
                        "message": f"{exam.label} 的开始时间早于当前时钟 {hm(now)}",
                    }
                )
        return problems

    # ------------------------------------------------------------------
    # 台账操作
    def add_exam(self, payload: dict[str, Any], by: str = "") -> dict[str, Any]:
        state = self.ledger.state
        if payload["id"] in state.exams:
            raise DomainError(f"检查 {payload['id']} 已存在")
        if payload.get("template_id") not in state.templates:
            raise DomainError(f"未知模板 {payload.get('template_id')}")
        event = self.ledger.append("exam.added", {**payload, "by": by})
        return {"seq": event.seq, "exam_id": payload["id"]}

    def mark_prep(self, exam_id: str, done: bool = True, by: str = "") -> dict[str, Any]:
        state = self.ledger.state
        if exam_id not in state.exams:
            raise DomainError(f"未知检查 {exam_id}")
        if done:
            event = self.ledger.append("exam.prep_done", {"exam_id": exam_id, "by": by})
            return {"seq": event.seq}
        raise DomainError("准备完成只能确认, 不能回退; 撤销请用取消检查")

    def mark_no_show(self, exam_id: str, by: str = "") -> dict[str, Any]:
        state = self.ledger.state
        exam = state.exam(exam_id)
        booking = state.bookings.get(exam_id)
        if booking is None or booking.status in ("STARTED", "COMPLETED"):
            raise DomainError(f"{exam.label} 当前状态不可标记爽约")
        event = self.ledger.append("exam.no_show", {"exam_id": exam_id, "by": by})
        return {"seq": event.seq}

    def cancel_exam(self, exam_id: str, reason: str = "", by: str = "") -> dict[str, Any]:
        booking = self.ledger.state.bookings.get(exam_id)
        if booking is None:
            raise DomainError(f"未知检查 {exam_id}")
        if booking.status in ("STARTED", "COMPLETED"):
            raise DomainError("已开始/完成的检查不能取消")
        event = self.ledger.append("exam.cancelled", {"exam_id": exam_id, "reason": reason, "by": by})
        return {"seq": event.seq}

    def start_exam(self, exam_id: str, at: int | None = None, by: str = "") -> dict[str, Any]:
        state = self.ledger.state
        exam = state.exam(exam_id)
        booking = state.bookings.get(exam_id)
        if booking is None or booking.status != "SCHEDULED":
            raise DomainError(f"{exam.label} 未排程, 不能开始")
        at_value = state.now_for(booking.date) if at is None else int(at)
        event = self.ledger.append(
            "booking.started", {"exam_id": exam_id, "at": at_value, "by": by}
        )
        return {"seq": event.seq, "at": at_value, "at_hm": hm(at_value)}

    def complete_exam(self, exam_id: str, at: int | None = None, by: str = "") -> dict[str, Any]:
        state = self.ledger.state
        exam = state.exam(exam_id)
        booking = state.bookings.get(exam_id)
        if booking is None or booking.status != "STARTED":
            raise DomainError(f"{exam.label} 未开始, 不能完成")
        at_value = state.now_for(booking.date) if at is None else int(at)
        event = self.ledger.append(
            "booking.completed", {"exam_id": exam_id, "at": at_value, "by": by}
        )
        return {"seq": event.seq, "at": at_value, "at_hm": hm(at_value)}

    def set_clock(self, date: str, at: int, by: str = "") -> dict[str, Any]:
        if at < DAY_START or at > 24 * 60:
            raise DomainError("时钟超出当日范围")
        event = self.ledger.append("clock.set", {"date": date, "at": int(at), "by": by})
        return {"seq": event.seq, "at": int(at), "at_hm": hm(at)}

    def break_resource(self, resource_id: str, date: str, start: int, end: int,
                       reason: str = "设备故障", by: str = "") -> dict[str, Any]:
        """设备故障/封锁: 追加封锁事件并使资源版本 +1。"""
        state = self.ledger.state
        if resource_id not in state.resources:
            raise DomainError(f"未知资源 {resource_id}")
        if end <= start:
            raise DomainError("封锁结束必须晚于开始")
        block = self.ledger.append(
            "block.added",
            {
                "id": f"BLK-{_rand(6)}",
                "resource_id": resource_id,
                "date": date,
                "start": int(start),
                "end": int(end),
                "reason": reason,
            },
        )
        bump = self.ledger.append(
            "resource.version_bumped",
            {"resource_id": resource_id, "note": f"{hm(start)}–{hm(end)} {reason}"},
        )
        return {"block_seq": block.seq, "version_seq": bump.seq,
                "message": f"{reason} 已登记, {resource_id} 版本 +1; 既有方案需要重新试算"}

    def adjust_shift(self, resource_id: str, date: str, start: int, end: int,
                     note: str = "", by: str = "") -> dict[str, Any]:
        """临时调整班次: 追加新班次并递增资源版本。"""
        state = self.ledger.state
        if resource_id not in state.resources:
            raise DomainError(f"未知资源 {resource_id}")
        if end <= start:
            raise DomainError("班次结束必须晚于开始")
        shift = self.ledger.append(
            "shift.added",
            {
                "id": f"SHF-{_rand(6)}",
                "resource_id": resource_id,
                "date": date,
                "start": int(start),
                "end": int(end),
                "note": note or "临时班次",
            },
        )
        bump = self.ledger.append(
            "resource.version_bumped",
            {"resource_id": resource_id, "note": f"班次 {hm(start)}–{hm(end)} {note}"},
        )
        return {"seq": shift.seq, "version_seq": bump.seq}

    # ------------------------------------------------------------------
    # 回放
    def replay(self, date: str, upto: int | None = None) -> dict[str, Any]:
        """按序回放与指定日期相关的事件。

        upto 给定时返回历史前缀的台账状态; 事件列表仍按最终检查集过滤,
        以便前端滑动时间轴时列表保持稳定, 只把未来的事件标灰。
        """
        final_state = self.ledger.state
        exam_ids = {
            e.id for e in final_state.exams.values() if e.requested_date == date
        }
        exam_ids |= {
            b.exam_id for b in final_state.bookings.values()
            if b.date == date and b.exam_id in final_state.exams
        }
        timeline = []
        for event in self.ledger.events():
            payload = event["payload"]
            et = event["type"]
            eid = payload.get("exam_id") or payload.get("id")
            if et in (
                "exam.added", "exam.prep_done", "exam.cancelled", "exam.no_show",
                "booking.reset", "booking.started", "booking.completed",
            ) and eid not in exam_ids:
                continue
            if et == "booking.confirmed" and payload.get("date") != date:
                continue
            if et in ("shift.added", "block.added", "clock.set") and payload.get("date") != date:
                continue
            if et == "resource.version_bumped":
                # 版本变化通过封锁/班次事件体现, 避免时间轴重复
                continue
            timeline.append(
                {
                    "seq": event["seq"],
                    "type": et,
                    "at": event["at"],
                    "summary": _event_summary(et, payload, final_state),
                }
            )
        state = final_state if upto is None else self.ledger.state_at(upto)
        current_seq = state.seq
        return {
            "date": date,
            "seq": self.ledger.seq,
            "current_seq": current_seq,
            "events": timeline,
            "count": len(timeline),
            "clocks": state.now_for(date),
            "now_hm": hm(state.now_for(date)),
            "exams": [
                {
                    "id": e.id,
                    "label": e.label,
                    "status": (state.bookings.get(e.id).status if state.bookings.get(e.id) else "UNSCHEDULED"),
                    "start_hm": hm(state.bookings[e.id].start()) if e.id in state.bookings else "--:--",
                    "end_hm": hm(state.bookings[e.id].end()) if e.id in state.bookings else "--:--",
                    "template_id": e.template_id,
                    "priority": e.priority,
                    "prep_done": e.prep_done,
                }
                for e in state.exams.values()
                if state.exam_date(e) == date
            ],
            "snapshot": self.snapshot(date, state),
        }

    def reset_demo(self) -> dict[str, Any]:
        """归档当前数据并重建演示台账 (仅演示环境)。"""
        with self._confirm_lock:
            archive = self.ledger.reset(archive=True)
            trial_archive = self.trials.reset(archive=True)
            from .demo import seed_initial_plans, seed_ledger

            seed_ledger(self.ledger)
            seed_initial_plans(self.ledger)
            self.ledger.note("seeded", {"source": "demo", "fictional": True, "reset": True})
            recovery = self.recover()
            return {
                "recovery": recovery,
                "ledger_archive": archive,
                "trials_archive": trial_archive,
                "seq": self.ledger.seq,
            }

    def refresh_missing_state(self) -> int:
        """Scheduled 但没有 slots 的预约是异常状态: 追加修复事件改为未排,

        避免恢复后把「没有时段」的检查误当成已排。修复本身也进入台账, 可回放。
        返回修复条数。
        """
        state = self.ledger.state
        broken = [
            b.exam_id
            for b in state.bookings.values()
            if b.status == "SCHEDULED" and not b.slots
        ]
        for eid in broken:
            self.ledger.append(
                "booking.reset", {"exam_id": eid, "reason": "状态修复: 预约无时段"}
            )
        return len(broken)

    def recover(self) -> dict[str, Any]:
        """计算中断后的恢复: 重放台账, 修复异常预约, 标记中断的试算。"""
        repaired = self.refresh_missing_state()
        recovered = self.trials.recover_interrupted()
        state = self.ledger.state_at(self.ledger.seq)
        return {
            "ledger_seq": self.ledger.seq,
            "corrupt_lines_dropped": self.ledger.recovered,
            "recovery_note": self.ledger.recovery_note,
            "trials_marked_interrupted": recovered,
            "bookings_repaired": repaired,
            "exams": len(state.exams),
        }


# ---------------------------------------------------------------------------
# helpers


def _event_summary(event_type: str, payload: dict[str, Any], state: State) -> str:
    exam = state.exams.get(payload.get("exam_id", ""))
    label = exam.label if exam is not None else payload.get("label", payload.get("exam_id", ""))
    if event_type == "exam.added":
        return f"新增检查 {payload.get('label', '')} (优先级 {payload.get('priority')})"
    if event_type == "exam.prep_done":
        return f"{label} 准备完成"
    if event_type == "exam.cancelled":
        return f"{label} 取消: {payload.get('reason', '')}"
    if event_type == "exam.no_show":
        return f"{label} 标记爽约"
    if event_type == "booking.reset":
        return f"{label} 退回未排: {payload.get('reason', '')}"
    if event_type == "booking.started":
        return f"{label} 开始 (时钟 {hm(payload.get('at'))})"
    if event_type == "booking.completed":
        return f"{label} 完成 (时钟 {hm(payload.get('at'))})"
    if event_type == "booking.confirmed":
        return (
            f"确认方案 ({payload.get('mode')}) · 计划员 {payload.get('planner') or '—'} · "
            f"{len(payload.get('bookings', {}))} 项"
        )
    if event_type == "clock.set":
        return f"当日时钟设为 {hm(payload.get('at'))}"
    if event_type == "block.added":
        rid = payload.get("resource_id")
        res = state.resources.get(rid)
        return f"{res.name if res else rid} 封锁 {hm(payload.get('start'))}–{hm(payload.get('end'))}: {payload.get('reason', '')}"
    if event_type == "shift.added":
        rid = payload.get("resource_id")
        res = state.resources.get(rid)
        return f"{res.name if res else rid} 班次 {hm(payload.get('start'))}–{hm(payload.get('end'))}"
    if event_type == "note":
        return f"记录: {payload.get('kind', '')}"
    return event_type


def _cascade_ids(state: State, date: str, cascade: int) -> set[str]:
    """滚动调整的邻域: 从当前时钟起, 接下来 N 项已排检查允许顺延。"""
    now = state.now_for(date)
    scheduled = [
        b
        for b in state.booked_exams(date)
        if b.status == "SCHEDULED"
        and b.start() is not None
        and (b.start() or 0) >= now
    ]
    scheduled.sort(key=lambda b: (b.start() or 0, b.exam_id))
    return {b.exam_id for b in scheduled[:cascade]}


def _windows_for(state: State, resource_id: str, date: str) -> list[tuple[int, int]]:
    from .feasibility import availability_windows

    return [(w.start, w.end) for w in availability_windows(state, resource_id, date)]


def _trial_id() -> str:
    import uuid

    return "T-" + time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _rand(n: int) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))
