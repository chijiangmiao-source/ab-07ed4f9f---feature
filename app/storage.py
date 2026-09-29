"""审计冻结记录存储。

同一 audit_id + 完全相同载荷 -> 返回同一记录（幂等）；
同一 audit_id + 不同载荷     -> 冲突，原记录保持不变；
非法载荷在写入前即被拒绝，不会残留或覆盖任何结论。

每条记录一个 JSON 文件，临时文件 + fsync + 原子 rename 落盘，
进程内以锁串行化“检查-写入”，避免并发首次提交产生双结论。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone


class IdConflictError(Exception):
    def __init__(self, existing: dict, new_fingerprint: str):
        self.existing = existing
        self.new_fingerprint = new_fingerprint
        super().__init__("audit_id 已绑定不同载荷")


class RepairConflictError(Exception):
    """同一修复标识已绑定（不同）来源；已冻结修复保持不变。"""

    def __init__(self, existing: dict, new_fingerprint: str):
        self.existing = existing
        self.new_fingerprint = new_fingerprint
        super().__init__("repair_id 已绑定不同来源审计")


def canonical_json(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def fingerprint(payload: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()


class AuditStore:
    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, audit_id: str) -> str:
        safe = hashlib.sha256(audit_id.encode("utf-8")).hexdigest()
        return os.path.join(self.data_dir, f"{safe}.json")

    def _repair_path(self, repair_id: str) -> str:
        safe = hashlib.sha256(repair_id.encode("utf-8")).hexdigest()
        return os.path.join(self.data_dir, f"repair-{safe}.json")

    def get(self, audit_id: str) -> dict | None:
        path = self._path(audit_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def submit(self, audit_id: str, payload_canonical: dict, result: dict) -> tuple[dict, bool]:
        """返回 (记录, 是否为重放)。冲突时抛 :class:`IdConflictError`。"""
        new_fp = fingerprint(payload_canonical)
        with self._lock:
            existing = self.get(audit_id)
            if existing is not None:
                if existing["fingerprint"] != new_fp:
                    raise IdConflictError(existing, new_fp)
                return existing, True

            record = {
                "audit_id": audit_id,
                "created_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "fingerprint": new_fp,
                "payload": payload_canonical,
                "result": result,
                "method": "phase-I-simplex/exact-rational/Bland",
            }
            self._write_atomic(audit_id, record)
            return record, False

    def _write_atomic(self, audit_id: str, record: dict) -> None:
        path = self._path(audit_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    # ---- 最小上界放宽修复（独立文件；从不改写来源审计） ----
    def get_repair(self, repair_id: str) -> dict | None:
        path = self._repair_path(repair_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def submit_repair(self, repair_id: str, source_snapshot: dict,
                      result: dict) -> tuple[dict, bool]:
        """冻结一条放宽修复。

        ``source_snapshot`` 只包含对来源审计的只读引用与快照（编号、指纹、
        规范变量、约束顺序、稳定标识与原证据摘要），本方法绝不写来源文件。
        同标识重放要求绑定同一来源（编号与指纹均相同），否则抛
        :class:`RepairConflictError`。
        """
        source_key = {
            "audit_id": source_snapshot["audit_id"],
            "fingerprint": source_snapshot["fingerprint"],
        }
        new_fp = fingerprint(source_key)
        with self._lock:
            existing = self.get_repair(repair_id)
            if existing is not None:
                if existing["source_binding_fingerprint"] != new_fp:
                    raise RepairConflictError(existing, new_fp)
                return existing, True

            record = {
                "kind": "min-rhs-relaxation",
                "repair_id": repair_id,
                "created_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "source_binding_fingerprint": new_fp,
                "source": source_snapshot,
                "result": result,
                "method": (
                    "exact-rational-two-phase-simplex/Bland;"
                    "min-sum-then-lex-by-stable-flag-order"
                ),
            }
            path = self._repair_path(repair_id)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return record, False
