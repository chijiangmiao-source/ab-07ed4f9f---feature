"""最小上界放宽审计的冻结记录存储（与原审计独立的命名空间）。

同一 repair_id + 同一来源（以来源指纹为准）-> 返回同一记录（幂等）；
同一 repair_id + 改换来源                              -> 冲突，已冻结修复不变；
来源记录本身不在此处写入或修改（原审计与已冻结修复均保持不变）。

落盘方式与 :class:`app.storage.AuditStore` 一致：每编号一个 JSON，
临时文件 + fsync + 原子 rename，进程内锁串行化“检查-写入”。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone

from .storage import fingerprint


class RepairConflictError(Exception):
    """repair_id 已绑定另一个来源（或另一条来源记录）。"""

    def __init__(self, existing: dict, new_source_audit_id: str,
                 new_source_fingerprint: str):
        self.existing = existing
        self.new_source_audit_id = new_source_audit_id
        self.new_source_fingerprint = new_source_fingerprint
        super().__init__("repair_id 已绑定不同来源")


class RepairStore:
    NAMESPACE = "repairs"

    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        self.dir = os.path.join(self.data_dir, self.NAMESPACE)
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, repair_id: str) -> str:
        safe = hashlib.sha256(repair_id.encode("utf-8")).hexdigest()
        return os.path.join(self.dir, f"{safe}.json")

    def get(self, repair_id: str) -> dict | None:
        path = self._path(repair_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def submit(
        self,
        repair_id: str,
        source_audit_id: str,
        source_fingerprint: str,
        content: dict,
    ) -> tuple[dict, bool]:
        """返回 (记录, 是否为重放)。content 的字段合并到记录顶层
        （至少含 source 快照与 result）。改换来源时抛
        :class:`RepairConflictError`。"""
        request_view = {
            "repair_id": repair_id,
            "source_audit_id": source_audit_id,
        }
        new_fp = fingerprint(request_view)
        with self._lock:
            existing = self.get(repair_id)
            if existing is not None:
                if existing["source_fingerprint"] != source_fingerprint:
                    raise RepairConflictError(
                        existing, source_audit_id, source_fingerprint
                    )
                return existing, True

            record = {
                "kind": "min-rhs-relaxation",
                "repair_id": repair_id,
                "source_audit_id": source_audit_id,
                "source_fingerprint": source_fingerprint,
                "created_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "fingerprint": new_fp,
                "request": request_view,
                "method": (
                    "two-phase-lexicographic-simplex/exact-rational/Bland"
                ),
                **content,
            }
            self._write_atomic(repair_id, record)
            return record, False

    def _write_atomic(self, repair_id: str, record: dict) -> None:
        path = self._path(repair_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
