"""领域模型与不变量.

设计要点:
- 资源台账 (设备/准备间/记录间/人员) 带单调递增的版本号。
  方案确认时必须核对版本; 设备故障、班次调整都会使版本递增。
- 检查是包含多个步骤的资源链 (准备 -> 扫描 -> 记录归档), 步骤之间可设最短间隔。
- 时间用「距当日 00:00 的分钟」表示; 日期为 ISO 字符串。
- 状态迁移显式: UNSCHEDULED / SCHEDULED / STARTED / COMPLETED / CANCELLED / NO_SHOW。
  已开始的检查不可移动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

DAY_START = 7 * 60
DAY_END = 21 * 60

OPEN_STATUSES = ("UNSCHEDULED", "SCHEDULED")
LOCKED_STATUSES = ("STARTED", "COMPLETED")
INACTIVE_STATUSES = ("CANCELLED", "NO_SHOW")
ALL_STATUSES = OPEN_STATUSES + LOCKED_STATUSES + INACTIVE_STATUSES


class DomainError(ValueError):
    """领域校验失败。"""


class VersionConflict(DomainError):
    def __init__(self, resource_id: str, expected: int, actual: int):
        self.resource_id = resource_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"资源 {resource_id} 版本不一致: 方案基于 v{expected}, 当前 v{actual}"
        )


def hm(minutes: int | None) -> str:
    if minutes is None:
        return "--:--"
    minutes = max(0, int(minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class Resource:
    id: str
    name: str
    kind: str  # equipment | room | prep | staff
    capabilities: frozenset[str] = frozenset()
    version: int = 1
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "capabilities": sorted(self.capabilities),
            "version": self.version,
            "note": self.note,
        }


@dataclass(frozen=True)
class Shift:
    """资源/人员在某日的一段可用区间 (分钟)。"""

    id: str
    resource_id: str
    date: str
    start: int
    end: int
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "resource_id": self.resource_id,
            "date": self.date,
            "start": self.start,
            "end": self.end,
            "note": self.note,
        }


@dataclass(frozen=True)
class ResourceBlock:
    """不可用区间, 如设备故障、校准、消防演练。"""

    id: str
    resource_id: str
    date: str
    start: int
    end: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "resource_id": self.resource_id,
            "date": self.date,
            "start": self.start,
            "end": self.end,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class StepSpec:
    """检查模板中的一步。"""

    key: str
    label: str
    minutes: int
    capability: str
    role: str
    gap_after: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "minutes": self.minutes,
            "capability": self.capability,
            "role": self.role,
            "gap_after": self.gap_after,
        }


PREP_STEP = StepSpec(
    key="prep",
    label="检查前准备",
    minutes=0,  # 运行时取模板的 prep_minutes
    capability="prep_bay",
    role="prep_nurse",
)


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    steps: tuple[StepSpec, ...]
    prep_minutes: int = 0
    prep_instruction: str = ""
    priority_class: str = "routine"

    def step(self, key: str) -> StepSpec:
        for s in self.steps:
            if s.key == key:
                return s
        raise DomainError(f"模板 {self.id} 没有步骤 {key}")

    def runtime_steps(self, prep_done: bool) -> tuple[StepSpec, ...]:
        """未完成准备时, 资源链前插入准备步骤。"""
        if self.prep_minutes > 0 and not prep_done:
            prep = StepSpec(
                key="prep",
                label="检查前准备",
                minutes=self.prep_minutes,
                capability=PREP_STEP.capability,
                role=PREP_STEP.role,
            )
            return (prep,) + tuple(self.steps)
        return tuple(self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "prep_minutes": self.prep_minutes,
            "prep_instruction": self.prep_instruction,
            "priority_class": self.priority_class,
            "steps": [s.to_dict() for s in self.steps],
            "total_minutes": sum(s.minutes for s in self.steps)
            + sum(s.gap_after for s in self.steps),
        }


@dataclass
class Exam:
    id: str
    label: str
    template_id: str
    requested_date: str
    priority: int  # 1=紧急插单, 3=优先, 5=常规; 越小越优先
    kind: str  # scheduled | addon
    prep_done: bool = False
    notes: str = ""
    due_by: int | None = None
    created_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "template_id": self.template_id,
            "requested_date": self.requested_date,
            "priority": self.priority,
            "kind": self.kind,
            "prep_done": self.prep_done,
            "notes": self.notes,
            "due_by": self.due_by,
            "created_seq": self.created_seq,
        }


@dataclass
class Booking:
    """某项检查当日一条资源链的排程结果。

    slots: 步骤 key -> {start, end, resource_id, staff_id, ...}
    """

    exam_id: str
    date: str
    status: str = "UNSCHEDULED"
    slots: dict[str, dict[str, Any]] = field(default_factory=dict)
    revision: int = 0
    changed_by: str = ""

    def start(self) -> int | None:
        if not self.slots:
            return None
        return min(s["start"] for s in self.slots.values())

    def end(self) -> int | None:
        if not self.slots:
            return None
        return max(s["end"] for s in self.slots.values())

    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def is_locked(self) -> bool:
        return self.status in LOCKED_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "exam_id": self.exam_id,
            "date": self.date,
            "status": self.status,
            "slots": self.slots,
            "revision": self.revision,
            "changed_by": self.changed_by,
            "start": self.start(),
            "end": self.end(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Booking":
        return cls(
            exam_id=data["exam_id"],
            date=data["date"],
            status=data.get("status", "UNSCHEDULED"),
            slots=data.get("slots", {}),
            revision=data.get("revision", 0),
            changed_by=data.get("changed_by", ""),
        )


@dataclass(frozen=True)
class Conflict:
    kind: str
    message: str
    exam_id: str | None = None
    resource_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "exam_id": self.exam_id,
            "resource_id": self.resource_id,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Conflict":
        return cls(
            kind=data["kind"],
            message=data["message"],
            exam_id=data.get("exam_id"),
            resource_id=data.get("resource_id"),
            detail=data.get("detail", {}),
        )


@dataclass
class Plan:
    """一次试算产出的完整方案 (沙盒内, 不落正式台账)。"""

    id: str
    date: str
    mode: str  # global | rolling
    tie_break: str
    base_version: int
    resource_versions: dict[str, int]
    bookings: dict[str, Booking]  # 本方案要写入/调整的检查
    unplaced: list[dict[str, Any]]
    conflicts: list[Conflict]
    metrics: dict[str, Any]
    frozen: dict[str, Booking] = field(default_factory=dict)
    elapsed_ms: int = 0
    label: str = ""
    locked_ids: list[str] = field(default_factory=list)
    attempts_done: int = 0
    attempts_total: int = 0
    exam_signatures: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "date": self.date,
            "mode": self.mode,
            "tie_break": self.tie_break,
            "label": self.label,
            "base_version": self.base_version,
            "resource_versions": self.resource_versions,
            "metrics": self.metrics,
            "unplaced": self.unplaced,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "frozen": {k: v.to_dict() for k, v in self.frozen.items()},
            "elapsed_ms": self.elapsed_ms,
            "locked_ids": self.locked_ids,
            "attempts_done": self.attempts_done,
            "attempts_total": self.attempts_total,
            "exam_signatures": self.exam_signatures,
            "bookings": {k: v.to_dict() for k, v in self.bookings.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Plan":
        return cls(
            id=data["id"],
            date=data["date"],
            mode=data["mode"],
            tie_break=data.get("tie_break", "balanced"),
            base_version=data.get("base_version", 0),
            resource_versions=data.get("resource_versions", {}),
            bookings={k: Booking.from_dict(v) for k, v in data.get("bookings", {}).items()},
            unplaced=data.get("unplaced", []),
            conflicts=[Conflict.from_dict(c) for c in data.get("conflicts", [])],
            frozen={k: Booking.from_dict(v) for k, v in data.get("frozen", {}).items()},
            metrics=data.get("metrics", {}),
            elapsed_ms=data.get("elapsed_ms", 0),
            label=data.get("label", ""),
            locked_ids=data.get("locked_ids", []),
            attempts_done=data.get("attempts_done", 0),
            attempts_total=data.get("attempts_total", 0),
            exam_signatures=data.get("exam_signatures", {}),
        )


@dataclass
class State:
    """台账投影: 由事件流重建的只读状态。"""

    resources: dict[str, Resource]
    shifts: list[Shift]
    blocks: list[ResourceBlock]
    exams: dict[str, Exam]
    templates: dict[str, Template]
    bookings: dict[str, Booking]  # key = exam_id
    ops: list[dict[str, Any]]  # 执行/准备操作记录, 用于回放
    clocks: dict[str, int] = field(default_factory=dict)
    seq: int = 0

    def shifts_for(self, resource_id: str, date: str) -> list[Shift]:
        return [s for s in self.shifts if s.resource_id == resource_id and s.date == date]

    def blocks_for(self, resource_id: str, date: str) -> list[ResourceBlock]:
        return [b for b in self.blocks if b.resource_id == resource_id and b.date == date]

    def exam(self, exam_id: str) -> Exam:
        try:
            return self.exams[exam_id]
        except KeyError as exc:
            raise DomainError(f"未知检查 {exam_id}") from exc

    def template_for(self, exam: Exam) -> Template:
        try:
            return self.templates[exam.template_id]
        except KeyError as exc:
            raise DomainError(f"未知模板 {exam.template_id}") from exc

    def resource(self, resource_id: str) -> Resource:
        try:
            return self.resources[resource_id]
        except KeyError as exc:
            raise DomainError(f"未知资源 {resource_id}") from exc

    def booked_exams(self, date: str) -> Iterator[Booking]:
        for b in self.bookings.values():
            if b.date == date:
                yield b

    def exam_date(self, exam: Exam) -> str:
        booking = self.bookings.get(exam.id)
        if booking is not None:
            return booking.date
        return exam.requested_date

    def now_for(self, date: str) -> int:
        return self.clocks.get(date, DAY_START)

    def signature(self, exam_id: str) -> str:
        """确认方案时用来核对检查本身是否被改动 (状态/准备/版本/日期)。"""
        exam = self.exams[exam_id]
        booking = self.bookings.get(exam_id)
        status = booking.status if booking is not None else "UNSCHEDULED"
        revision = booking.revision if booking is not None else 0
        date = booking.date if booking is not None else exam.requested_date
        return f"{status}|prep={int(exam.prep_done)}|rev={revision}|date={date}"

    def copy(self) -> "State":
        import copy

        return copy.deepcopy(self)
