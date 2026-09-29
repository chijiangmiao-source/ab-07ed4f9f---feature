"""审计编排: 校验 -> 精确 Phase-I 求解 -> 冻结/重放；
无解来源上的最小上界放宽修复 -> 独立冻结/重放。
"""

from __future__ import annotations

from .models import AuditPayload, parse_payload, parse_repair_payload
from .relax import relax
from .simplex import solve
from .storage import AuditStore, IdConflictError, RepairConflictError

__all__ = [
    "AuditService",
    "IdConflictError",
    "RepairConflictError",
    "RepairRejected",
]


class RepairRejected(Exception):
    """放宽请求被拒绝；不产生任何修复记录，来源审计保持不变。"""

    def __init__(self, code: str, detail: str, source: dict | None = None):
        self.code = code
        self.detail = detail
        self.source = source
        super().__init__(detail)


class AuditService:
    def __init__(self, store: AuditStore):
        self.store = store

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
        return self.store.get_repair(repair_id)

    def repair(self, raw_payload) -> tuple[dict, bool]:
        """在已冻结的无解来源审计上发起最小上界放宽。

        拒绝条件（不写任何记录、来源保持不变）:
          * 载荷非法；
          * 来源审计不存在；
          * 来源结论并非无解；
          * 同一修复标识已绑定另一来源（编号或指纹不同）。
        """
        req = parse_repair_payload(raw_payload)
        source = self.store.get(req.audit_id)
        if source is None:
            raise RepairRejected(
                "source_not_found",
                f"来源审计不存在: {req.audit_id}；放宽修复必须从已冻结的无解"
                "审计发起",
            )
        if source["result"].get("status") != "infeasible":
            raise RepairRejected(
                "source_not_infeasible",
                "来源审计的结论并非无解；只有无解结论才允许发起上界放宽",
                source=source,
            )

        # ---- 从冻结记录重建规范变量与约束（顺序即冻结顺序，不重排） ----
        payload = source["payload"]
        parsed: AuditPayload = parse_payload(payload)
        if parsed.audit_id != req.audit_id:
            raise RepairRejected(
                "source_corrupt", "来源冻结记录的标识与请求不一致", source=source
            )

        result = relax(
            parsed.variables, parsed.constraints, parsed.stable_flags
        ).as_dict()
        result["stable_flags"] = list(parsed.stable_flags)

        # 来源快照：只引用、不改写；冻结原变量/约束顺序/稳定标识/原证据
        source_snapshot = {
            "audit_id": source["audit_id"],
            "fingerprint": source["fingerprint"],
            "created_at": source["created_at"],
            "variables": list(parsed.variables),
            "constraint_order": [
                {
                    "index": i,
                    "coeffs": list(con.coeffs),
                    "b": con.b,
                    "label": con.label,
                    "stable": st,
                }
                for i, (con, st) in enumerate(
                    zip(parsed.constraints, parsed.stable_flags)
                )
            ],
            "original_evidence": {
                "status": source["result"]["status"],
                "combined_lhs": source["result"]["combined_lhs"],
                "combined_rhs": source["result"]["combined_rhs"],
                "combined_relation": source["result"]["combined_relation"],
                "multipliers": source["result"]["multipliers"],
            },
        }
        record, replayed = self.store.submit_repair(
            req.repair_id, source_snapshot, result
        )
        return record, replayed
