"""HTTP 层集成测试: 真实启动标准库服务器, 走完「选日期 → 插单 → 冲突 → 确认 → 回放」。

运行:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.server import make_server  # noqa: E402

DAY = "2026-11-18"


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.httpd = make_server(0, "127.0.0.1", Path(cls._tmp.name))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path: str) -> dict:
        with urlopen(self.url(path), timeout=20) as resp:
            return json.loads(resp.read())

    def post(self, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body or {}).encode()
        req = Request(self.url(path), data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def post_error(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        try:
            self.post(path, body)
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())
        raise AssertionError("请求本应失败")

    # -- tests -----------------------------------------------------------
    def test_static_page_and_bootstrap(self) -> None:
        with urlopen(self.url("/"), timeout=10) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("检查排程台", html)
        self.assertIn("不提供任何医疗诊断", html)
        data = self.get("/api/bootstrap")
        self.assertGreater(data["snapshot"]["seq"], 0)
        self.assertTrue(data["snapshot"]["dates"])
        self.assertIn("limits", data)

    def test_path_traversal_is_blocked(self) -> None:
        code, payload = self.post_error("/api/exams", {"id": "X"})
        self.assertEqual(code, 400)
        self.assertIn("error", payload)
        try:
            urlopen(self.url("/static/../../app/server.py"), timeout=10)
        except HTTPError as exc:
            self.assertIn(exc.code, (400, 404))
        else:
            raise AssertionError("目录穿越必须被拒绝")

    def test_trial_then_confirm_updates_ledger(self) -> None:
        boot = self.get("/api/bootstrap?date=" + DAY)
        seq_before = boot["snapshot"]["seq"]
        trial = self.post("/api/trials", {"date": DAY, "mode": "global", "planner": "甲"})
        self.assertEqual(trial["status"], "complete")
        self.assertEqual(self.get("/api/bootstrap?date=" + DAY)["snapshot"]["seq"], seq_before,
                         "试算不得写入正式台账")
        self.assertGreater(len(trial["bookings"]), 0)
        out = self.post(f"/api/trials/{trial['id']}/confirm", {"planner": "甲"})
        self.assertEqual(out["confirmed_seq"], seq_before + 1)
        snapshot = self.get("/api/snapshot?date=" + DAY)
        self.assertEqual(snapshot["seq"], seq_before + 1)
        self.assertEqual(snapshot["last_confirm"]["seq"], out["confirmed_seq"])

    def test_confirm_rejected_after_resource_change(self) -> None:
        trial = self.post("/api/trials", {"date": DAY, "mode": "global", "planner": "甲"})
        self.post(f"/api/resources/EQ-CT-1/block",
                  {"date": DAY, "start": "10:00", "end": "11:00", "reason": "测试故障"})
        code, payload = self.post_error(f"/api/trials/{trial['id']}/confirm", {})
        self.assertEqual(code, 409)
        kinds = {c["kind"] for c in payload["conflicts"]}
        self.assertIn("resource_version", kinds)
        # 资源已变更, 新试算可以正常确认
        fresh = self.post("/api/trials", {"date": DAY, "mode": "global", "planner": "甲"})
        self.assertEqual(self.post(f"/api/trials/{fresh['id']}/confirm", {})["bookings"],
                         len(fresh["bookings"]))

    def test_addon_and_conflicts_are_specific(self) -> None:
        self.post("/api/exams", {
            "id": "ADD-API-1", "label": "加急 腹部 MR (虚构)", "template_id": "TPL-MR",
            "requested_date": DAY, "priority": 1, "kind": "addon",
            "prep_done": True, "due_by": "12:00",
        })
        self.post("/api/resources/EQ-MR-1/block",
                  {"date": DAY, "start": "07:00", "end": "16:00", "reason": "整日故障"})
        self.post("/api/resources/EQ-MR-2/block",
                  {"date": DAY, "start": "07:00", "end": "16:00", "reason": "整日故障"})
        trial = self.post("/api/trials", {"date": DAY, "mode": "global", "planner": "乙"})
        self.assertGreater(trial["metrics"]["unplaced"], 0)
        unplaced = {u["exam_id"]: u for u in trial["unplaced"]}
        self.assertIn("ADD-API-1", unplaced)
        self.assertIn("MR 扫描", unplaced["ADD-API-1"]["reason"])
        # MR 设备整日被封锁: 要么判定为资源不可用, 要么在剩余窗口内无可行时段
        self.assertIn(
            unplaced["ADD-API-1"]["conflict_kind"],
            ("resource_unavailable", "no_feasible_slot"),
        )
        # 冲突列表与未排列表一致, 不会悄悄塞进不合法时段
        for booking in trial["bookings"].values():
            for slot in booking["slots"].values():
                self.assertNotIn(slot["resource_id"], ("EQ-MR-1", "EQ-MR-2"))

    def test_replay_does_not_mutate(self) -> None:
        latest = self.get("/api/bootstrap?date=" + DAY)["snapshot"]["seq"]
        replay = self.get(f"/api/replay?date={DAY}&upto=5")
        self.assertLessEqual(replay["current_seq"], 5)
        self.assertEqual(self.get("/api/health")["seq"], latest)

    def test_parallel_trials_are_isolated(self) -> None:
        seq_before = self.get("/api/health")["seq"]
        out = self.post("/api/trials/parallel", {
            "date": DAY,
            "specs": [
                {"mode": "global", "planner": "甲"},
                {"mode": "rolling", "planner": "乙", "cascade": 2},
                {"mode": "global", "planner": "丙", "tie_break": "stability"},
            ],
        })
        self.assertEqual(len(out["runs"]), 3)
        ids = {run["result"]["id"] for run in out["runs"]}
        self.assertEqual(len(ids), 3)
        for run in out["runs"]:
            self.assertIsNone(run["error"])
            self.assertEqual(run["result"]["base_version"], seq_before)
        self.assertEqual(self.get("/api/health")["seq"], seq_before, "并行试算不得写台账")

    def test_interrupt_and_recover(self) -> None:
        code, payload = self.post_error("/api/demo/interrupt", {"date": DAY, "mode": "global"})
        self.assertEqual(code, 409)
        trial_id = payload["trial_id"]
        rec = self.post("/api/recover", {})
        self.assertIn(trial_id, rec["trials_marked_interrupted"])
        trial = self.get(f"/api/trials/{trial_id}")
        self.assertEqual(trial["status"], "interrupted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
