"""后端单元/集成测试 (标准库 unittest, 无需第三方依赖)。

运行:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine.demo import seed_ledger  # noqa: E402
from app.engine.domain import DAY_START, Booking, DomainError, hm  # noqa: E402
from app.engine.ledger import EventLedger, TrialStore  # noqa: E402
from app.engine.planner import schedule  # noqa: E402
from app.engine.service import ConfirmError, InterruptedCompute, SchedulingService  # noqa: E402

DAY = "2026-11-18"


def make_service(tmp: Path, seed: bool = True) -> SchedulingService:
    ledger = EventLedger(tmp / "ledger.jsonl")
    trials = TrialStore(tmp / "trials")
    if seed and ledger.is_empty():
        seed_ledger(ledger)
    return SchedulingService(ledger, trials)


def day_snapshot(service: SchedulingService) -> list[dict]:
    return service.snapshot(DAY)["exams"]


def confirm_plan(service: SchedulingService, plan: dict, planner: str = "测试") -> dict:
    """把 plan dict 存为 trial 再确认 (模拟 UI 流程)。"""
    payload = dict(plan)
    payload["id"] = "T-TEST-" + str(len(service.trials.list(100)))
    payload["status"] = "complete"
    service.trials.save(payload)
    return service.confirm(payload["id"], planner=planner)


class LedgerTests(unittest.TestCase):
    def test_append_replay_and_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp))
            seq0 = service.ledger.seq
            self.assertGreater(seq0, 0)
            before = service.snapshot(DAY)
            result = service.break_resource("EQ-CT-1", DAY, 10 * 60, 11 * 60, reason="测试故障")
            self.assertGreater(result["block_seq"], seq0)
            after = service.snapshot(DAY)
            ct = next(r for r in after["resources"] if r["id"] == "EQ-CT-1")
            self.assertEqual(ct["version"], 2)
            self.assertEqual(len(ct["blocks"]), 1)
            # 回放到封锁之前: 版本与封锁都不存在
            past = service.replay(DAY, upto=seq0)
            past_ct = next(r for r in past["snapshot"]["resources"] if r["id"] == "EQ-CT-1")
            self.assertEqual(past_ct["version"], 1)
            self.assertEqual(past_ct["blocks"], [])
            self.assertLessEqual(past["current_seq"], seq0)
            # 回放只读: 台账序号不变
            self.assertEqual(service.ledger.seq, after["seq"])
            self.assertNotEqual(before["seq"], after["seq"])

    def test_corrupt_line_is_dropped_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp))
            path = Path(tmp) / "ledger.jsonl"
            good = path.read_text(encoding="utf-8").splitlines()
            path.write_text(
                "\n".join(good[:5] + ["{not json", *good[5:]]) + "\n", encoding="utf-8"
            )
            reopened = EventLedger(path)
            self.assertEqual(reopened.recovered, 1)
            self.assertIsNotNone(reopened.recovery_note)
            self.assertEqual(reopened.recovery_note["dropped_events"], 1)
            self.assertEqual(reopened.recovery_note["kept_events"], len(good))
            # 丢弃损坏行 + 追加一条恢复备注 (两者都持久化)
            self.assertEqual(len(reopened.events()), len(good) + 1)
            self.assertEqual(reopened.events()[-1]["payload"]["kind"], "recovery")
            # 修复后备注事件自身也可重放, 且不再报损坏
            again = EventLedger(path)
            self.assertEqual(again.recovered, 0)
            self.assertEqual(len(again.events()), len(good) + 1)

    def test_duplicate_seq_is_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = EventLedger(Path(tmp) / "ledger.jsonl")
            ledger.append("note", {"kind": "x", "n": 1})
            ledger.append("note", {"kind": "x", "n": 2})
            path = Path(tmp) / "ledger.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text("\n".join([lines[0], lines[0], lines[1]]) + "\n", encoding="utf-8")
            reopened = EventLedger(path)
            self.assertEqual(reopened.recovered, 1)
            notes = [e for e in reopened.events() if e["type"] == "note"]
            self.assertEqual(len(notes), 3)  # 2 条正常事件 + 1 条恢复备注
            self.assertEqual(reopened.seq, 3)


class PlannerTests(unittest.TestCase):
    def test_global_plan_has_no_resource_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp))
            plan = schedule(service.ledger.state, DAY, mode="global", now=DAY_START)
            self.assertEqual(plan.unplaced, [])
            used: dict[str, list[tuple[int, int]]] = {}
            for booking in plan.bookings.values():
                for slot in booking.slots.values():
                    for rid in (slot["resource_id"], slot["staff_id"]):
                        used.setdefault(rid, []).append((slot["start"], slot["end"]))
            for rid, spans in used.items():
                spans.sort()
                for a, b in zip(spans, spans[1:]):
                    self.assertLessEqual(a[1], b[0], f"{rid} 出现重叠")

    def test_prep_step_precedes_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp))
            plan = schedule(service.ledger.state, DAY, mode="global", now=DAY_START)
            checked = 0
            for exam_id, booking in plan.bookings.items():
                exam = service.ledger.state.exams[exam_id]
                if exam.prep_done or "prep" not in booking.slots:
                    continue
                prep = booking.slots["prep"]
                first_scan = min(
                    (s for k, s in booking.slots.items() if k != "prep"),
                    key=lambda s: s["start"],
                )
                self.assertLessEqual(prep["end"], first_scan["start"])
                checked += 1
            self.assertGreater(checked, 0)

    def test_unavailable_capability_is_reported_not_stuffed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp))
            # 封锁唯一的 pet_scan 设备整日: PET 检查必须显式未排, 而不是硬塞
            service.break_resource("EQ-PET-1", DAY, 0, 24 * 60, reason="设备整日故障")
            plan = schedule(
                service.ledger.state, DAY, mode="global", now=DAY_START
            )
            pet_unplaced = [u for u in plan.unplaced if u["exam_id"] in {
                eid for eid, e in service.ledger.state.exams.items()
                if e.template_id == "TPL-PET" and e.requested_date == DAY
            }]
            self.assertTrue(pet_unplaced, "PET 无设备时必须进入未排")
            reason = pet_unplaced[0]["reason"]
            self.assertTrue("PET" in reason or "pet_scan" in reason, reason)
            for booking in plan.bookings.values():
                for slot in booking.slots.values():
                    self.assertNotEqual(slot["resource_id"], "EQ-PET-1")

    def test_rolling_keeps_scheduled_exams_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp))
            global_plan = schedule(service.ledger.state, DAY, mode="global", now=DAY_START)
            confirm_plan(service, global_plan.to_dict())
            state = service.ledger.state
            base = {
                eid: b.start()
                for eid, b in state.bookings.items()
                if b.date == DAY and b.status == "SCHEDULED"
            }
            plan = schedule(state, DAY, mode="rolling", now=DAY_START)
            for eid, start in base.items():
                if eid in plan.bookings:
                    self.assertEqual(plan.bookings[eid].start(), start, f"{eid} 被移动")
                else:
                    self.assertIn(eid, plan.frozen, f"{eid} 丢失")
            self.assertEqual(plan.metrics["placed"], len(base))

    def test_locked_exam_is_never_moved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp))
            plan = schedule(service.ledger.state, DAY, mode="global", now=DAY_START)
            confirm_plan(service, plan.to_dict())
            state = service.ledger.state
            ordered = sorted(
                (b for b in state.booked_exams(DAY) if b.status == "SCHEDULED"),
                key=lambda b: b.start(),
            )
            first = ordered[0]
            service.start_exam(first.exam_id, at=first.start())
            state = service.ledger.state
            locked_start = state.bookings[first.exam_id].start()
            for mode in ("global", "rolling"):
                moved = schedule(state, DAY, mode=mode, now=first.start() + 5)
                booking = moved.bookings.get(first.exam_id)
                if booking is not None:
                    self.assertEqual(booking.start(), locked_start)
                self.assertIn(first.exam_id, state.exams)


class ConfirmTests(unittest.TestCase):
    def test_stale_resource_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp))
            plan = schedule(service.ledger.state, DAY, mode="global", now=DAY_START)
            payload = plan.to_dict()
            payload["id"] = "T-STALE-1"
            payload["status"] = "complete"
            service.trials.save(payload)
            service.break_resource("EQ-CT-2", DAY, 9 * 60, 10 * 60, reason="故障")
            with self.assertRaises(ConfirmError) as ctx:
                service.confirm("T-STALE-1")
            kinds = {c["kind"] for c in ctx.exception.conflicts}
            self.assertIn("resource_version", kinds)

    def test_overlapping_plan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp))
            plan = schedule(service.ledger.state, DAY, mode="global", now=DAY_START).to_dict()
            state = service.ledger.state
            ids = sorted(plan["bookings"])
            a, b = ids[0], ids[1]
            slot = plan["bookings"][a]["slots"]["scan"]
            # 把 b 的扫描步骤硬塞到与 a 完全相同的时间/资源
            plan["bookings"][b]["slots"]["scan"] = dict(slot)
            plan["id"] = "T-OVERLAP-1"
            plan["status"] = "complete"
            plan["exam_signatures"] = {
                eid: state.signature(eid) for eid in plan["bookings"]
            }
            service.trials.save(plan)
            with self.assertRaises(ConfirmError) as ctx:
                service.confirm("T-OVERLAP-1")
            kinds = {c["kind"] for c in ctx.exception.conflicts}
            self.assertTrue(kinds & {"overlap"}, kinds)

    def test_started_exam_cannot_be_moved_by_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp))
            plan = schedule(service.ledger.state, DAY, mode="global", now=DAY_START).to_dict()
            confirm_plan(service, plan)
            state = service.ledger.state
            target = sorted(
                (b for b in state.booked_exams(DAY) if b.status == "SCHEDULED"),
                key=lambda b: b.start(),
            )[0]
            service.start_exam(target.exam_id, at=target.start())
            stale = service.trials.load(
                sorted((p.stem for p in (Path(tmp) / "trials").glob("T-TEST-*.json")))[0]
            )
            moved = stale["bookings"][target.exam_id]
            shifted = {
                k: {**v, "start": v["start"] + 30, "end": v["end"] + 30}
                for k, v in moved["slots"].items()
            }
            stale["bookings"][target.exam_id] = {**moved, "slots": shifted}
            stale["id"] = "T-MOVE-LOCKED"
            stale["status"] = "complete"
            stale["base_version"] = service.ledger.seq
            stale["resource_versions"] = {
                rid: r.version for rid, r in service.ledger.state.resources.items()
            }
            stale["exam_signatures"] = {
                eid: service.ledger.state.signature(eid) for eid in stale["bookings"]
            }
            service.trials.save(stale)
            with self.assertRaises(ConfirmError) as ctx:
                service.confirm("T-MOVE-LOCKED")
            self.assertIn("locked_exam_moved", {c["kind"] for c in ctx.exception.conflicts})

    def test_confirm_does_not_write_trial_to_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp))
            plan = schedule(service.ledger.state, DAY, mode="global", now=DAY_START).to_dict()
            seq_before = service.ledger.seq
            payload = plan
            payload["id"] = "T-SEPARATE-1"
            payload["status"] = "complete"
            service.trials.save(payload)
            result = service.confirm("T-SEPARATE-1")
            self.assertEqual(result["confirmed_seq"], seq_before + 1)
            events = service.ledger.events()
            self.assertEqual(events[-1]["type"], "booking.confirmed")
            trial = service.trials.load("T-SEPARATE-1")
            self.assertEqual(trial["status"], "confirmed")
            # 确认只写正式预约, 不把试算 JSON 写进台账
            self.assertFalse(any(e["type"] == "trial.saved" for e in events))


class RecoveryTests(unittest.TestCase):
    def test_interrupted_trial_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp))
            seq_before = service.ledger.seq
            with self.assertRaises(InterruptedCompute) as ctx:
                service.interrupted_trial(DAY, "global", planner="演示")
            trial_id = ctx.exception.trial_id
            self.assertEqual(service.ledger.seq, seq_before, "中断不得写正式台账")
            self.assertEqual(service.trials.load(trial_id)["status"], "running")
            report = service.recover()
            self.assertIn(trial_id, report["trials_marked_interrupted"])
            self.assertEqual(service.trials.load(trial_id)["status"], "interrupted")
            # 中断后可以重新试算出完整方案
            fresh = service.compute(DAY, "global", planner="恢复后")
            self.assertEqual(fresh["status"], "complete")
            self.assertEqual(fresh["metrics"]["unplaced"], 0)

    def test_broken_trial_file_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp))
            bad = Path(tmp) / "trials" / "T-BROKEN.json"
            bad.write_text("{oops", encoding="utf-8")
            (Path(tmp) / "trials" / ".tmp-abc.json").write_text("x", encoding="utf-8")
            touched = service.trials.recover_interrupted()
            self.assertEqual(touched, [])
            self.assertFalse(bad.exists())
            self.assertTrue((Path(tmp) / "trials" / "T-BROKEN.json.broken").exists())
            self.assertFalse((Path(tmp) / "trials" / ".tmp-abc.json").exists())

    def test_no_show_and_block_change_later_plans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp))
            plan = schedule(service.ledger.state, DAY, mode="global", now=DAY_START)
            confirm_plan(service, plan.to_dict())
            state = service.ledger.state
            target = sorted(
                (b for b in state.booked_exams(DAY) if b.status == "SCHEDULED"),
                key=lambda b: b.start(),
            )[0]
            service.mark_no_show(target.exam_id)
            after = service.snapshot(DAY)
            status = {e["id"]: e["status"] for e in after["exams"]}
            self.assertEqual(status[target.exam_id], "NO_SHOW")
            # 爽约释放的时段可被新的插单使用, 且不会被排到爽约块上
            aid = service._addon_pool(service.ledger.state)[0]
            service.add_exam({**aid, "requested_date": DAY}, by="测试")
            replanned = service.compute(DAY, "global", planner="插单")
            self.assertEqual(replanned["metrics"]["unplaced"], 0)
            addon = replanned["bookings"][aid["id"]]
            for slot in addon["slots"].values():
                for other_id, booking in replanned["bookings"].items():
                    if other_id == aid["id"]:
                        continue
                    for other in booking["slots"].values():
                        if other["resource_id"] != slot["resource_id"]:
                            continue
                        self.assertFalse(
                            other["start"] < slot["end"] and slot["start"] < other["end"]
                        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
