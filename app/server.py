"""检查排程台 — 标准库 HTTP 服务 (无第三方依赖).

启动:
    python3 -m app.server --port 8000
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .engine.domain import DAY_END, DAY_START, DomainError, hm
from .engine.ledger import EventLedger, TrialStore
from .engine.service import ConfirmError, InterruptedCompute, SchedulingService

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = Path(os.environ.get("SCHEDULE_DATA", Path.cwd() / "data"))

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _json_default(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def _num(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise DomainError(f"{name} 必须是整数") from None


def _parse_hm(value: Any, name: str) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not re.match(r"^\d{1,2}:\d{2}$", value):
        raise DomainError(f"{name} 需为 HH:MM")
    h, m = value.split(":")
    minutes = int(h) * 60 + int(m)
    if not (0 <= minutes <= 24 * 60):
        raise DomainError(f"{name} 超出范围")
    return minutes


def _clean_exam(body: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化新增检查字段 (due_by 接受 HH:MM)。"""
    payload = dict(body)
    allowed = {
        "id", "label", "template_id", "requested_date", "priority", "kind",
        "prep_done", "due_by", "notes", "by",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise DomainError("未知字段: " + ", ".join(sorted(unknown)))
    for key in ("id", "label", "template_id", "requested_date"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise DomainError(f"{key} 不能为空")
    payload["requested_date"] = _date(payload["requested_date"])
    payload["priority"] = _num(payload.get("priority", 5), "priority")
    if not 1 <= payload["priority"] <= 5:
        raise DomainError("priority 需在 1-5 之间")
    if payload.get("due_by") in (None, ""):
        payload.pop("due_by", None)
    else:
        payload["due_by"] = _parse_hm(payload["due_by"], "due_by")
    payload["prep_done"] = bool(payload.get("prep_done"))
    payload["kind"] = payload.get("kind") or "scheduled"
    return payload


def _date(value: Any) -> str:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise DomainError("日期需为 YYYY-MM-DD")
    return value


class Api:
    def __init__(self, service: SchedulingService):
        self.service = service
        self.seeded = False
        self.recovery = service.recover()

    def allow_reset(self) -> bool:
        return os.environ.get("SCHEDULE_ALLOW_RESET", "1") != "0"

    def bootstrap(self, query: dict[str, str]) -> dict[str, Any]:
        snap = self.service.snapshot(_date(query.get("date") or _first_date(self.service)))
        return {
            "snapshot": snap,
            "trials": self.service.list_trials(),
            "recovery": self.recovery,
            "limits": {"day_start": DAY_START, "day_end": DAY_END,
                       "day_start_hm": hm(DAY_START), "day_end_hm": hm(DAY_END)},
        }


def _first_date(service: SchedulingService) -> str:
    dates = sorted({e.requested_date for e in service.ledger.state.exams.values()})
    return dates[0] if dates else "2026-11-17"


class Handler(BaseHTTPRequestHandler):
    service: SchedulingService
    api: Api
    server_version = "ScheduleDesk/1.0"

    # -- logging ---------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # 精简日志
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # -- helpers ---------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        length = _num(self.headers.get("Content-Length") or 0, "Content-Length")
        if length > 1_000_000:
            raise DomainError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise DomainError("请求体不是有效 JSON") from None
        if not isinstance(data, dict):
            raise DomainError("请求体必须是对象")
        return data

    def _query(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        return {k: v[-1] for k, v in parse_qs(parsed.query).items()}

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/bootstrap":
                return self._json(200, self.api.bootstrap(self._query()))
            if path == "/api/snapshot":
                q = self._query()
                return self._json(200, self.service.snapshot(_date(q.get("date") or _first_date(self.service))))
            if path == "/api/trials":
                return self._json(200, {"trials": self.service.list_trials()})
            if path.startswith("/api/trials/"):
                trial_id = path.rsplit("/", 1)[-1]
                trial = self.service.get_trial(trial_id)
                if trial is None:
                    return self._json(404, {"error": "试算不存在"})
                return self._json(200, trial)
            if path == "/api/replay":
                q = self._query()
                upto = _num(q["upto"], "upto") if q.get("upto") else None
                return self._json(200, self.service.replay(
                    _date(q.get("date") or _first_date(self.service)), upto=upto))
            if path == "/api/health":
                return self._json(200, {"ok": True, "seq": self.service.ledger.seq})
            if path == "/" or path == "/index.html":
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self._json(404, {"error": "未找到"})
        except DomainError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception:
            traceback.print_exc()
            return self._json(500, {"error": "服务器内部错误"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/trials":
                result = self.service.compute(
                    _date(body.get("date")),
                    body.get("mode", "global"),
                    tie_break=body.get("tie_break", "balanced"),
                    planner=body.get("planner", ""),
                    cascade=_num(body.get("cascade") or 0, "cascade"),
                    tier=body.get("tier", "quick"),
                )
                return self._json(201, result)
            if path == "/api/trials/parallel":
                specs = body.get("specs")
                if not isinstance(specs, list) or not specs:
                    raise DomainError("specs 必须是非空数组")
                if len(specs) > 6:
                    raise DomainError("并行试算最多 6 个")
                return self._json(201, {"runs": self.service.parallel_trials(_date(body.get("date")), specs)})
            if path == "/api/demo/interrupt":
                try:
                    self.service.interrupted_trial(
                        _date(body.get("date")),
                        body.get("mode", "global"),
                        body.get("planner", "演示计划员"),
                    )
                except InterruptedCompute as exc:
                    return self._json(
                        409,
                        {"error": str(exc), "trial_id": exc.trial_id, "interrupted": True},
                    )
                return self._json(200, {"ok": True})
            if path.startswith("/api/trials/") and path.endswith("/confirm"):
                trial_id = path[len("/api/trials/"):-len("/confirm")]
                result = self.service.confirm(
                    trial_id, planner=body.get("planner", ""), note=body.get("note", "")
                )
                return self._json(200, result)
            if path == "/api/exams":
                return self._json(201, self.service.add_exam(_clean_exam(body), by=body.get("by", "")))
            if path == "/api/clock":
                return self._json(200, self.service.set_clock(
                    _date(body.get("date")), _parse_hm(body.get("at"), "at"), by=body.get("by", "")))
            if path == "/api/recover":
                return self._json(200, self.service.recover())
            if path == "/api/demo/reset":
                if not self.api.allow_reset():
                    return self._json(403, {"error": "演示环境已禁用重置"})
                return self._json(200, self.service.reset_demo())
            if path.startswith("/api/exams/"):
                return self._exam_action(path, body)
            if path.startswith("/api/resources/"):
                return self._resource_action(path, body)
            return self._json(404, {"error": "未找到"})
        except ConfirmError as exc:
            return self._json(409, exc.to_dict())
        except InterruptedCompute as exc:
            return self._json(409, {"error": str(exc), "trial_id": exc.trial_id, "interrupted": True})
        except DomainError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception:
            traceback.print_exc()
            return self._json(500, {"error": "服务器内部错误"})

    def _exam_action(self, path: str, body: dict[str, Any]) -> None:
        parts = path.strip("/").split("/")
        # api exams <id> <action>
        if len(parts) != 4:
            return self._json(404, {"error": "未找到"})
        exam_id, action = parts[2], parts[3]
        by = body.get("by", "")
        if action == "prep":
            return self._json(200, self.service.mark_prep(exam_id, done=bool(body.get("done", True)), by=by))
        if action == "no-show":
            return self._json(200, self.service.mark_no_show(exam_id, by=by))
        if action == "cancel":
            return self._json(200, self.service.cancel_exam(exam_id, reason=body.get("reason", ""), by=by))
        if action == "start":
            at = body.get("at")
            return self._json(200, self.service.start_exam(
                exam_id, at=_parse_hm(at, "at") if at is not None else None, by=by))
        if action == "complete":
            at = body.get("at")
            return self._json(200, self.service.complete_exam(
                exam_id, at=_parse_hm(at, "at") if at is not None else None, by=by))
        return self._json(404, {"error": "未知操作"})

    def _resource_action(self, path: str, body: dict[str, Any]) -> None:
        parts = path.strip("/").split("/")
        # api resources <id> <action>
        if len(parts) != 4:
            return self._json(404, {"error": "未找到"})
        rid, action = parts[2], parts[3]
        if action == "block":
            return self._json(200, self.service.break_resource(
                rid, _date(body.get("date")), _parse_hm(body.get("start"), "start"),
                _parse_hm(body.get("end"), "end"), reason=body.get("reason", "设备故障"),
                by=body.get("by", "")))
        if action == "shift":
            return self._json(200, self.service.adjust_shift(
                rid, _date(body.get("date")), _parse_hm(body.get("start"), "start"),
                _parse_hm(body.get("end"), "end"), note=body.get("note", ""), by=body.get("by", "")))
        return self._json(404, {"error": "未知操作"})

    def _static(self, rel: str) -> None:
        if rel in ("", "/"):
            rel = "index.html"
        target = (STATIC / rel).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            return self._json(404, {"error": "未找到"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def build_service(data_dir: Path) -> tuple[SchedulingService, bool]:
    data_dir.mkdir(parents=True, exist_ok=True)
    ledger = EventLedger(data_dir / "ledger.jsonl")
    trials = TrialStore(data_dir / "trials")
    service = SchedulingService(ledger, trials)
    seeded = False
    if ledger.is_empty():
        from .engine.demo import seed_initial_plans, seed_ledger

        seed_ledger(ledger)
        seed_initial_plans(ledger)
        seeded = True
        service.ledger.note("seeded", {"source": "demo", "fictional": True})
    return service, seeded


def make_server(port: int = 8000, host: str = "127.0.0.1", data_dir: Path | None = None) -> ThreadingHTTPServer:
    service, _seeded = build_service(data_dir or DATA)
    api = Api(service)
    handler = type("BoundHandler", (Handler,), {"service": service, "api": api})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查排程台 (虚构演示)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--data", default=str(DATA))
    args = parser.parse_args(argv)
    httpd = make_server(args.port, args.host, Path(args.data))
    print(f"检查排程台: http://{args.host}:{args.port}  (数据目录 {args.data})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
