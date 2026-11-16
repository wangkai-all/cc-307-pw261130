"""事件台账: 只追加 JSONL, 投影为 State, 支持中断恢复与回放.

正式台账 (ledger.jsonl) 与试算记录 (trials.jsonl) 是两个独立文件:
- 试算可以反复运行、失败、被中断, 永远不写正式台账;
- 只有确认 (confirm) 才向正式台账追加事件。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .domain import (
    Booking,
    Exam,
    Resource,
    ResourceBlock,
    Shift,
    State,
    Template,
    StepSpec,
)

LEDGER_VERSION = 1


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _minutes_or_none(value: Any) -> int | None:
    """due_by 允许 HH:MM 或分钟数; 其它一律视为无截止, 避免坏数据拖垮排程。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and ":" in value:
        h, _, m = value.partition(":")
        try:
            return int(h) * 60 + int(m)
        except ValueError:
            return None
    return None


def _resource_from(payload: dict[str, Any]) -> Resource:
    return Resource(
        id=payload["id"],
        name=payload["name"],
        kind=payload["kind"],
        capabilities=frozenset(payload.get("capabilities", [])),
        version=payload.get("version", 1),
        note=payload.get("note", ""),
    )


def _template_from(payload: dict[str, Any]) -> Template:
    steps = tuple(
        StepSpec(
            key=s["key"],
            label=s["label"],
            minutes=int(s["minutes"]),
            capability=s["capability"],
            role=s["role"],
            gap_after=int(s.get("gap_after", 0)),
        )
        for s in payload["steps"]
    )
    return Template(
        id=payload["id"],
        name=payload["name"],
        steps=steps,
        prep_minutes=int(payload.get("prep_minutes", 0)),
        prep_instruction=payload.get("prep_instruction", ""),
        priority_class=payload.get("priority_class", "routine"),
    )


def empty_state() -> State:
    return State(
        resources={},
        shifts=[],
        blocks=[],
        exams={},
        templates={},
        bookings={},
        ops=[],
        clocks={},
        seq=0,
    )


def apply_event(state: State, event: dict[str, Any]) -> None:
    et = event["type"]
    p = event["payload"]
    seq = int(event["seq"])
    state.seq = max(state.seq, seq)
    if et == "resource.added":
        res = _resource_from(p)
        state.resources[res.id] = res
    elif et == "resource.version_bumped":
        rid = p["resource_id"]
        cur = state.resources.get(rid)
        if cur is not None:
            state.resources[rid] = Resource(
                id=cur.id,
                name=cur.name,
                kind=cur.kind,
                capabilities=cur.capabilities,
                version=cur.version + 1,
                note=p.get("note", cur.note),
            )
    elif et == "shift.added":
        state.shifts.append(
            Shift(
                id=p["id"],
                resource_id=p["resource_id"],
                date=p["date"],
                start=int(p["start"]),
                end=int(p["end"]),
                note=p.get("note", ""),
            )
        )
    elif et == "block.added":
        state.blocks.append(
            ResourceBlock(
                id=p["id"],
                resource_id=p["resource_id"],
                date=p["date"],
                start=int(p["start"]),
                end=int(p["end"]),
                reason=p.get("reason", ""),
            )
        )
    elif et == "template.added":
        tpl = _template_from(p)
        state.templates[tpl.id] = tpl
    elif et == "exam.added":
        if p["id"] in state.exams:
            raise ValueError(f"检查 {p['id']} 重复添加")
        exam = Exam(
            id=p["id"],
            label=p["label"],
            template_id=p["template_id"],
            requested_date=p["requested_date"],
            priority=int(p["priority"]),
            kind=p.get("kind", "scheduled"),
            prep_done=bool(p.get("prep_done", False)),
            notes=p.get("notes", ""),
            due_by=_minutes_or_none(p.get("due_by")),
            created_seq=seq,
        )
        state.exams[exam.id] = exam
        prev = state.bookings.get(exam.id)
        if prev is not None and prev.status in ("STARTED", "COMPLETED"):
            raise ValueError(f"检查 {p['id']} 已开始/完成, 不能重新添加")
        state.bookings[exam.id] = Booking(
            exam_id=exam.id,
            date=exam.requested_date,
            status="UNSCHEDULED",
            slots={},
            revision=prev.revision if prev is not None else 0,
        )
    elif et == "exam.prep_done":
        exam = state.exams[p["exam_id"]]
        exam.prep_done = True
        b = state.bookings.get(exam.id)
        if b is not None:
            b.revision += 1
    elif et == "exam.cancelled":
        b = state.bookings[p["exam_id"]]
        b.status = "CANCELLED"
        b.revision += 1
        b.changed_by = p.get("by", "")
        state.ops.append({"seq": seq, "type": et, **p})
    elif et == "booking.reset":
        b = state.bookings[p["exam_id"]]
        b.status = "UNSCHEDULED"
        b.slots = {}
        b.revision += 1
        state.ops.append({"seq": seq, "type": et, **p})
    elif et == "exam.no_show":
        b = state.bookings[p["exam_id"]]
        b.status = "NO_SHOW"
        b.revision += 1
        state.ops.append({"seq": seq, "type": et, **p})
    elif et == "booking.confirmed":
        plan = p.get("plan", p)
        touched_dates: set[str] = set()
        for eid, booking in plan.get("bookings", {}).items():
            cur = state.bookings.get(eid)
            if cur is not None and cur.is_locked():
                raise ValueError(f"检查 {eid} 已开始/完成, 不能再被方案移动")
            slots = booking["slots"]
            new = Booking(
                exam_id=eid,
                date=booking["date"],
                status="SCHEDULED",
                slots=slots,
                revision=(cur.revision + 1 if cur is not None else 1),
                changed_by=p.get("mode", "confirm"),
            )
            state.bookings[eid] = new
            touched_dates.add(new.date)
        # 计划中明确「不排」的检查保持/回到未排
        for eid in plan.get("unplaced_ids", []):
            cur = state.bookings.get(eid)
            if cur is not None and cur.status == "SCHEDULED" and cur.slots:
                cur.status = "UNSCHEDULED"
                cur.slots = {}
                cur.revision += 1
                touched_dates.add(cur.date)
        state.ops.append(
            {
                "seq": seq,
                "type": et,
                "date": plan.get("date"),
                "mode": p.get("mode"),
                "planner": p.get("planner", ""),
                "plan_id": p.get("plan_id", ""),
                "bookings": len(plan.get("bookings", {})),
                "planner_note": p.get("note", ""),
            }
        )
    elif et == "booking.started":
        b = state.bookings[p["exam_id"]]
        if b.status not in ("SCHEDULED",):
            raise ValueError(f"检查 {p['exam_id']} 当前状态 {b.status} 不能开始")
        b.status = "STARTED"
        b.revision += 1
        state.clocks[b.date] = max(state.now_for(b.date), int(p["at"]))
        state.ops.append({"seq": seq, "type": et, **p})
    elif et == "booking.completed":
        b = state.bookings[p["exam_id"]]
        if b.status != "STARTED":
            raise ValueError(f"检查 {p['exam_id']} 未开始, 不能完成")
        b.status = "COMPLETED"
        b.revision += 1
        state.clocks[b.date] = max(state.now_for(b.date), int(p["at"]))
        state.ops.append({"seq": seq, "type": et, **p})
    elif et == "clock.set":
        state.clocks[p["date"]] = int(p["at"])
        state.ops.append({"seq": seq, "type": et, **p})
    elif et == "note":
        state.ops.append({"seq": seq, "type": et, **p})
    else:
        # 未知事件不应静默忽略, 否则恢复后的状态会悄悄失真
        raise ValueError(f"未知事件类型: {et}")


@dataclass
class AppendResult:
    seq: int
    event: dict[str, Any]


class EventLedger:
    """线程安全的追加式台账。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._state: State = empty_state()
        self.recovered = 0
        self.recovery_note: dict[str, Any] | None = None
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            self._flush_state()
            return
        good_events: list[dict[str, Any]] = []
        bad = 0
        with self.path.open("rb") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if "seq" not in event or "type" not in event:
                        raise ValueError("missing fields")
                except (json.JSONDecodeError, ValueError):
                    bad += 1
                    continue
                good_events.append(event)
        good_events.sort(key=lambda e: e["seq"])
        state = empty_state()
        valid: list[dict[str, Any]] = []
        for event in good_events:
            # 序号必须严格递增, 重复/倒退的事件视为损坏
            if valid and event["seq"] <= valid[-1]["seq"]:
                bad += 1
                continue
            try:
                apply_event(state, event)
            except Exception:
                bad += 1
                continue
            valid.append(event)
        self._events = valid
        self._state = state
        self.recovered = bad
        if bad:
            # 真正修复文件: 保留可解析前缀, 尾部损坏另存
            stamp = time.strftime("%Y%m%d-%H%M%S")
            bad_path = self.path.with_suffix(self.path.suffix + f".corrupt-{stamp}")
            os.replace(self.path, bad_path)
            with self.path.open("w", encoding="utf-8") as fh:
                for event in valid:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.recovery_note = {
                "dropped_events": bad,
                "kept_events": len(valid),
                "backup": bad_path.name,
            }
            self.note("recovery", self.recovery_note)

    def _flush_state(self) -> None:
        self._state = empty_state()
        for event in self._events:
            apply_event(self._state, event)

    def _append_line(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def append(self, event_type: str, payload: dict[str, Any]) -> AppendResult:
        with self._lock:
            seq = (self._events[-1]["seq"] + 1) if self._events else 1
            event = {
                "seq": seq,
                "type": event_type,
                "at": _now_iso(),
                "payload": payload,
            }
            # 先验证事件可以应用, 再落盘, 避免坏事件进入台账
            probe = self._state.copy()
            apply_event(probe, event)
            self._append_line(event)
            self._events.append(event)
            self._state = probe
            return AppendResult(seq=seq, event=event)

    def append_many(
        self, items: list[tuple[str, dict[str, Any]]]
    ) -> list[AppendResult]:
        """批量追加 (用于初始化演示数据): 先全部校验, 再一次落盘 + fsync。"""
        with self._lock:
            seq = (self._events[-1]["seq"] + 1) if self._events else 1
            probe = self._state.copy()
            new_events: list[dict[str, Any]] = []
            results: list[AppendResult] = []
            for event_type, payload in items:
                event = {
                    "seq": seq,
                    "type": event_type,
                    "at": _now_iso(),
                    "payload": payload,
                }
                apply_event(probe, event)
                new_events.append(event)
                results.append(AppendResult(seq=seq, event=event))
                seq += 1
            if new_events:
                lines = "".join(
                    json.dumps(e, ensure_ascii=False) + "\n" for e in new_events
                )
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(lines)
                    fh.flush()
                    os.fsync(fh.fileno())
                self._events.extend(new_events)
                self._state = probe
            return results

    def note(self, kind: str, payload: dict[str, Any]) -> AppendResult:
        return self.append("note", {"kind": kind, **payload})

    # -- reads -------------------------------------------------------------
    @property
    def state(self) -> State:
        with self._lock:
            return self._state.copy()

    @property
    def seq(self) -> int:
        with self._lock:
            return self._events[-1]["seq"] if self._events else 0

    def events(self, upto: int | None = None, types: Iterable[str] | None = None) -> list[dict]:
        with self._lock:
            out = self._events if upto is None else [e for e in self._events if e["seq"] <= upto]
            if types is not None:
                allowed = set(types)
                out = [e for e in out if e["type"] in allowed]
            return list(out)

    def state_at(self, seq: int) -> State:
        with self._lock:
            state = empty_state()
            for event in self._events:
                if event["seq"] > seq:
                    break
                apply_event(state, event)
            return state

    def is_empty(self) -> bool:
        with self._lock:
            return not self._events

    def reset(self, archive: bool = True) -> str:
        """清空台账 (演示重建用), 旧文件归档为 .bak-<时间戳>。"""
        with self._lock:
            backup = ""
            if self.path.exists():
                if archive:
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    bak = self.path.with_suffix(self.path.suffix + f".bak-{stamp}")
                    os.replace(self.path, bak)
                    backup = bak.name
                else:
                    self.path.unlink()
            self._events = []
            self._state = empty_state()
            self.recovered = 0
            self.recovery_note = None
            self._flush_state()
            return backup


class TrialStore:
    """试算记录的独立存储; 支持逐尝试检查点与中断恢复。

    每个 trial 是独立 JSON 文件, 写入使用临时文件 + 原子替换。
    """

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, trial_id: str) -> Path:
        if not trial_id.replace("-", "").isalnum():
            raise ValueError("非法 trial id")
        return self.dir / f"{trial_id}.json"

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            path = self._path(data["id"])
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    def load(self, trial_id: str) -> dict[str, Any] | None:
        path = self._path(trial_id)
        if not path.exists():
            return None
        with self._lock:
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                with path.open(encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except Exception:
                continue
            if len(out) >= limit:
                break
        return out

    def reset(self, archive: bool = True) -> str:
        """归档并清空全部试算记录。"""
        with self._lock:
            backup = ""
            files = sorted(self.dir.glob("*.json"))
            if files:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                target = self.dir / f"archive-{stamp}"
                target.mkdir(exist_ok=True)
                for path in files:
                    os.replace(path, target / path.name)
                backup = target.name
            return backup

    def recover_interrupted(self) -> list[str]:
        """进程重启后, 恢复未完成的试算。

        - 带 checkpoint 的 running 试算 → interrupted, 保留检查点供 UI 重算;
        - 写了一半的临时文件 (.tmp-*) 直接清理;
        - 无法解析的 trial JSON → `.broken` 改名保留, 不让它拖垮启动。
        """
        touched: list[str] = []
        for tmp in self.dir.glob(".tmp-*.json"):
            try:
                tmp.unlink()
            except OSError:
                pass
        for path in list(self.dir.glob("*.json")):
            try:
                with path.open(encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                os.replace(path, path.with_suffix(".json.broken"))
                continue
            if data.get("status") == "running":
                data["status"] = "interrupted"
                data["interrupted_at"] = _now_iso()
                data.setdefault("checkpoint", "started")
                self.save(data)
                touched.append(data["id"])
        return touched
