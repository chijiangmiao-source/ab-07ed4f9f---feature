"""HTTP 入口（仅标准库）。

路由:
  GET  /                     浏览器页面
  GET  /healthz              健康检查 -> {"status":"ok"}
  POST /api/audits           提交审计
       201 首次冻结; 200 同载荷重放; 409 同号不同载荷; 400 载荷非法
  GET  /api/audits/<id>      按审计编号读取冻结结果; 404 不存在
  POST /api/repairs          在已冻结的无解审计上发起最小上界放宽修复
       201 首次冻结; 200 同来源同标识重放;
       400 载荷非法; 404 来源不存在;
       409 来源并非无解 / 同标识改换来源
  GET  /api/repairs/<id>     按修复编号读取冻结结果; 404 不存在
"""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .models import PayloadError
from .service import AuditService, IdConflictError, RepairConflictError, RepairRejected
from .storage import AuditStore

_DATA_DIR = os.environ.get("AUDIT_DATA_DIR", "/data")
_HOST = os.environ.get("AUDIT_HOST", "0.0.0.0")
_PORT = int(os.environ.get("AUDIT_PORT", "8080"))
_MAX_BODY = 4 * 1024 * 1024

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "static", "index.html"), "r", encoding="utf-8") as f:
    INDEX_HTML = f.read()


class Handler(BaseHTTPRequestHandler):
    service: AuditService = None  # 由 make_server 注入（类属性）

    def log_message(self, fmt, *args):  # 静默默认访问日志，健康检查不刷屏
        if os.environ.get("AUDIT_ACCESS_LOG"):
            super().log_message(fmt, *args)

    # ---- 工具 ----
    def _send_json(self, status: int, obj: dict, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_html(HTTPStatus.OK, INDEX_HTML)
        elif path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok", "service": "coil-audit"})
        elif path.startswith("/api/audits/"):
            audit_id = path[len("/api/audits/"):]
            if not audit_id or "/" in audit_id:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            record = self.service.fetch(audit_id)
            if record is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "not_found", "audit_id": audit_id},
                )
            else:
                self._send_json(HTTPStatus.OK, {"replayed": True, **record})
        elif path.startswith("/api/repairs/"):
            repair_id = path[len("/api/repairs/"):]
            if not repair_id or "/" in repair_id:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            record = self.service.fetch_repair(repair_id)
            if record is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "not_found", "repair_id": repair_id},
                )
            else:
                self._send_json(HTTPStatus.OK, {"replayed": True, **record})
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/audits", "/api/repairs"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > _MAX_BODY:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_payload", "detail": "请求体缺失或过大"},
            )
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_payload", "detail": f"JSON 解析失败: {exc}"},
            )
            return

        if path == "/api/repairs":
            self._handle_repair(payload)
            return

        try:
            record, replayed = self.service.audit(payload)
        except PayloadError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_payload", "detail": str(exc)},
            )
            return
        except IdConflictError as exc:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "error": "audit_id_conflict",
                    "detail": "同一审计编号已绑定不同载荷；原证据保持不变",
                    "audit_id": exc.existing["audit_id"],
                    "existing_fingerprint": exc.existing["fingerprint"],
                    "existing_created_at": exc.existing["created_at"],
                    "new_fingerprint": exc.new_fingerprint,
                },
            )
            return

        self._send_json(
            HTTPStatus.OK if replayed else HTTPStatus.CREATED,
            {"replayed": replayed, **record},
        )

    def _handle_repair(self, payload) -> None:
        try:
            record, replayed = self.service.repair(payload)
        except PayloadError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_payload", "detail": str(exc)},
            )
            return
        except RepairRejected as exc:
            if exc.code == "source_not_found":
                audit_id = (
                    payload.get("audit_id")
                    if isinstance(payload, dict) else None
                )
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": exc.code, "detail": exc.detail,
                     "audit_id": audit_id},
                )
            else:
                # source_not_infeasible / source_corrupt：与来源当前状态冲突
                body = {"error": exc.code, "detail": exc.detail}
                if exc.source is not None:
                    body.update(
                        audit_id=exc.source["audit_id"],
                        source_status=exc.source["result"].get("status"),
                        source_fingerprint=exc.source["fingerprint"],
                    )
                self._send_json(HTTPStatus.CONFLICT, body)
            return
        except RepairConflictError as exc:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "error": "repair_id_conflict",
                    "detail": "同一修复标识已绑定不同来源；已冻结修复保持不变",
                    "repair_id": exc.existing["repair_id"],
                    "bound_audit_id": exc.existing["source"]["audit_id"],
                    "bound_source_fingerprint": exc.existing["source"]["fingerprint"],
                    "requested_binding_fingerprint": exc.new_fingerprint,
                },
            )
            return

        self._send_json(
            HTTPStatus.OK if replayed else HTTPStatus.CREATED,
            {"replayed": replayed, **record},
        )


def make_server(host: str = _HOST, port: int = _PORT, data_dir: str = _DATA_DIR):
    store = AuditStore(data_dir)
    service = AuditService(store)
    Handler.service = service
    return ThreadingHTTPServer((host, port), Handler)


def main():
    server = make_server()
    host, port = server.server_address[:2]
    print(f"coil-audit listening on http://{host}:{port} data={_DATA_DIR}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
