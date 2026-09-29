"""审计编排: 校验 -> 精确 Phase-I 求解 -> 冻结/重放；
以及最小上界放宽审计: 读取已冻结的无解来源 -> 精确两阶段字典序求解 -> 冻结。"""

from __future__ import annotations

from .models import (
    AuditPayload,
    parse_payload,
    parse_repair_request,
    verify_relaxation_certificate,
)
from .relaxation import solve_relaxation
from .repair_storage import RepairConflictError, RepairStore
from .simplex import Constraint, solve
from .storage import AuditStore, IdConflictError, fingerprint

__all__ = [
    "AuditService",
    "IdConflictError",
    "RepairConflictError",
    "SourceNotFoundError",
    "SourceNotInfeasibleError",
    "SourceTamperedError",
]


class SourceNotFoundError(LookupError):
    """来源审计编号不存在（从未冻结）。"""


class SourceNotInfeasibleError(ValueError):
    """来源审计存在但结论不是“无解”，不允许发起放宽。"""


class SourceTamperedError(RuntimeError):
    """来源记录载荷与冻结指纹不符，拒绝在其之上发起修复。"""


class AuditService:
    def __init__(self, store: AuditStore, repair_store: RepairStore | None = None):
        self.store = store
        self.repair_store = repair_store

    def audit(self, raw_payload) -> tuple[dict, bool]:
        """返回 (冻结记录, 是否为重放命中)。载荷非法时抛 ValueError，不写任何记录。"""
        parsed: AuditPayload = parse_payload(raw_payload)
        result = solve(parsed.variables, parsed.constraints).as_dict()
        result["stable_flags"] = list(parsed.stable_flags)
        record, replayed = self.store.submit(
            parsed.audit_id, parsed.canonical(), result
        )
        return record, replayed

    def fetch(self, audit_id: str) -> dict | None:
        return self.store.get(audit_id)

    def fetch_repair(self, repair_id: str) -> dict | None:
        if self.repair_store is None:
            return None
        return self.repair_store.get(repair_id)

    def repair(self, raw_request) -> tuple[dict, bool]:
        """发起/重放最小上界放宽审计。

        * 请求只允许携带 repair_id 与 source_audit_id，来源的变量、约束顺序、
          稳定标识与原证据全部取自冻结记录，调用方无法改写；
        * 来源不存在 / 并非无解 / 载荷指纹不自洽 -> 拒绝，不写任何修复记录；
        * 同一 repair_id 改换来源 -> RepairConflictError，已冻结修复不变。
        """
        if self.repair_store is None:
            raise RuntimeError("修复存储未初始化")
        repair_id, source_audit_id = parse_repair_request(raw_request)

        existing_repair = self.repair_store.get(repair_id)
        if existing_repair is not None:
            # 已冻结：同一来源重放即使来源文件缺失也返回自包含快照；
            # 任何改换来源（含同编号但指纹被篡改）都先于求解被拒绝。
            if source_audit_id != existing_repair["source_audit_id"]:
                source = self.store.get(source_audit_id)
                raise RepairConflictError(
                    existing_repair,
                    source_audit_id,
                    fingerprint(source["payload"]) if source is not None else "",
                )
            source = self.store.get(source_audit_id)
            if source is not None:
                payload_fp = fingerprint(source["payload"])
                if payload_fp != source["fingerprint"]:
                    raise SourceTamperedError(source_audit_id)
                if payload_fp != existing_repair["source_fingerprint"]:
                    raise RepairConflictError(
                        existing_repair,
                        source_audit_id,
                        payload_fp,
                    )
            return existing_repair, True

        source = self.store.get(source_audit_id)
        if source is None:
            raise SourceNotFoundError(source_audit_id)
        if fingerprint(source["payload"]) != source["fingerprint"]:
            raise SourceTamperedError(source_audit_id)
        source_result = source["result"]
        if source_result.get("status") != "infeasible":
            raise SourceNotInfeasibleError(source_audit_id)

        payload = source["payload"]
        variables = tuple(payload["variables"])
        src_cons = payload["constraints"]
        constraints = tuple(
            Constraint(
                coeffs=tuple(item["coeffs"]),
                b=item["b"],
                label=item.get("label"),
            )
            for item in src_cons
        )
        stable_flags = tuple(bool(item.get("stable", False)) for item in src_cons)

        result = solve_relaxation(variables, constraints, stable_flags).as_dict()
        # 落盘前用独立核验（不调用求解器）复核放宽解与有界对偶等式
        verify_relaxation_certificate(payload, result)

        source_snapshot = {
            "audit_id": source["audit_id"],
            "fingerprint": source["fingerprint"],
            "created_at": source["created_at"],
            "variables": list(variables),
            "constraints": [
                {
                    "index": i,
                    "coeffs": list(item["coeffs"]),
                    "b": item["b"],
                    "label": item.get("label"),
                    "stable": bool(item.get("stable", False)),
                }
                for i, item in enumerate(src_cons)
            ],
            "evidence": source_result,
        }
        record = {
            "source": source_snapshot,
            "result": result,
        }
        frozen, replayed = self.repair_store.submit(
            repair_id,
            source_audit_id,
            source["fingerprint"],
            record,
        )
        return frozen, replayed
