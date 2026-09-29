"""请求载荷的整数级校验与规范化。

只接受 JSON 整数（拒绝布尔、浮点、字符串数字），
从源头杜绝浮点近似进入审计。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

from .simplex import Constraint

MAX_VARS = 8
MAX_CONSTRAINTS = 48
MAX_ID_LEN = 128
MAX_LABEL_LEN = 200
MAX_INT_BITS = 4096  # 防恶意超大整数
_ID_RE = re.compile(r"^[\w.@\-]+$", re.UNICODE)


class PayloadError(ValueError):
    """载荷非法；不得产生或覆盖任何审计结论。"""


@dataclass(frozen=True)
class AuditPayload:
    audit_id: str
    variables: tuple[str, ...]
    constraints: tuple[Constraint, ...]
    stable_flags: tuple[bool, ...]

    def canonical(self) -> dict:
        """用于幂等比较的规范结构（只含已校验的原始数据）。"""
        return {
            "audit_id": self.audit_id,
            "variables": list(self.variables),
            "constraints": [
                {
                    "coeffs": list(c.coeffs),
                    "b": c.b,
                    "label": c.label,
                    "stable": s,
                }
                for c, s in zip(self.constraints, self.stable_flags)
            ],
        }


def _is_plain_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _bounded_int(v, what: str) -> int:
    if not _is_plain_int(v):
        raise PayloadError(f"{what} 必须是整数")
    if abs(v).bit_length() > MAX_INT_BITS:
        raise PayloadError(f"{what} 超出允许的整数范围")
    return v


def _is_bool(v) -> bool:
    return isinstance(v, bool)


def parse_payload(obj) -> AuditPayload:
    if not isinstance(obj, dict):
        raise PayloadError("请求体必须是 JSON 对象")

    audit_id = _validate_id(obj.get("audit_id"), "audit_id")

    raw_vars = obj.get("variables")
    if not isinstance(raw_vars, list) or not (1 <= len(raw_vars) <= MAX_VARS):
        raise PayloadError(f"变量个数必须在 1 到 {MAX_VARS} 之间")
    variables: list[str] = []
    seen: set[str] = set()
    for k, v in enumerate(raw_vars):
        if not isinstance(v, str) or not v.strip():
            raise PayloadError(f"第 {k + 1} 个变量名必须是非空字符串")
        name = v.strip()
        if name in seen:
            raise PayloadError(f"变量名重复: {name}")
        seen.add(name)
        variables.append(name)

    raw_cons = obj.get("constraints")
    if not isinstance(raw_cons, list) or not (1 <= len(raw_cons) <= MAX_CONSTRAINTS):
        raise PayloadError(f"约束条数必须在 1 到 {MAX_CONSTRAINTS} 之间")

    n = len(variables)
    constraints: list[Constraint] = []
    stable_flags: list[bool] = []
    for k, item in enumerate(raw_cons):
        if not isinstance(item, dict):
            raise PayloadError(f"第 {k + 1} 条约束必须是对象")
        coeffs_raw = item.get("coeffs")
        if not isinstance(coeffs_raw, list) or len(coeffs_raw) != n:
            raise PayloadError(
                f"第 {k + 1} 条约束的 coeffs 长度必须等于变量个数 {n}"
            )
        coeffs = tuple(
            _bounded_int(a, f"第 {k + 1} 条约束第 {j + 1} 个系数")
            for j, a in enumerate(coeffs_raw)
        )
        b = _bounded_int(item.get("b"), f"第 {k + 1} 条约束右端 b")
        if all(a == 0 for a in coeffs) and b < 0:
            # 合法但单独即构成 0 <= b < 0 的不可行证据，放行交给求解器
            pass
        label = item.get("label")
        if label is not None:
            if not isinstance(label, str) or len(label) > MAX_LABEL_LEN:
                raise PayloadError(
                    f"第 {k + 1} 条约束 label 必须是不超过 {MAX_LABEL_LEN} 字符的字符串"
                )
        stable = item.get("stable", False)
        if not _is_bool(stable):
            raise PayloadError(f"第 {k + 1} 条约束 stable 标识必须是布尔值")
        constraints.append(Constraint(coeffs=coeffs, b=b, label=label))
        stable_flags.append(stable)

    return AuditPayload(
        audit_id=audit_id,
        variables=tuple(variables),
        constraints=tuple(constraints),
        stable_flags=tuple(stable_flags),
    )


def _validate_id(raw, what: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise PayloadError(f"{what} 必须是非空字符串")
    rid = raw.strip()
    if len(rid) > MAX_ID_LEN or not _ID_RE.match(rid):
        raise PayloadError(
            f"{what} 仅允许字母、数字、点、下划线、连字符与 @，且不超过 "
            f"{MAX_ID_LEN} 个字符"
        )
    return rid


def parse_repair_request(obj) -> tuple[str, str]:
    """解析最小上界放宽审计请求: {"repair_id", "source_audit_id"}。

    来源的变量/约束/证据一律以已冻结的原审计记录为准，本请求不携带任何
    数值载荷，杜绝借修复通道改写来源。
    """
    if not isinstance(obj, dict):
        raise PayloadError("请求体必须是 JSON 对象")
    repair_id = _validate_id(obj.get("repair_id"), "repair_id")
    source_audit_id = _validate_id(obj.get("source_audit_id"), "source_audit_id")
    return repair_id, source_audit_id


def verify_relaxation_certificate(source_payload: dict, result: dict) -> None:
    """仅依据冻结来源载荷与返回结果，独立核验放宽解与有界对偶证书。

    弱对偶保证: 任何满足 Aᵀμ=0、0≤μ≤1 的 μ 都是最小放宽总量的下界；
    一旦 −μᵀb 与实际 Σd 相等，总量最优即被证明，无需重新求解。
    """
    assert result["status"] == "repaired"
    src_cons = source_payload["constraints"]
    variables = source_payload["variables"]
    m, n = len(src_cons), len(variables)
    rows = result["constraints"]
    assert len(rows) == m
    currents = {k: Fraction(v) for k, v in result["currents"].items()}
    assert set(currents) == set(variables)

    total = Fraction(0)
    for i, (row, src) in enumerate(zip(rows, src_cons)):
        assert row["index"] == i
        d = Fraction(row["relaxation"])
        assert d >= 0, f"放宽量 d_{i} 为负"
        total += d
        b = Fraction(src["b"])
        assert Fraction(row["b"]) == b
        assert Fraction(row["new_b"]) == b + d
        ax = sum(
            (Fraction(src["coeffs"][j]) * currents[variables[j]]
             for j in range(n)),
            Fraction(0),
        )
        residual = b + d - ax
        assert residual >= 0, f"约束 {i} 放宽后仍被违反"
        assert Fraction(row["new_residual"]) == residual, (
            f"约束 {i} 新余量不一致"
        )
    assert total == Fraction(result["total_relaxation"])

    dual = result["dual"]
    multipliers = [Fraction(x["value"]) for x in dual["multipliers"]]
    assert len(multipliers) == m
    lhs = [Fraction(0) for _ in range(n)]
    rhs = Fraction(0)
    for i, (term, src) in enumerate(zip(dual["terms"], src_cons)):
        assert term["index"] == i
        mu = multipliers[i]
        assert Fraction(term["multiplier"]) == mu
        assert 0 <= mu <= 1, f"对偶乘子 μ_{i}={mu} 越出 [0,1]"
        assert Fraction(term["weighted_rhs"]) == mu * Fraction(src["b"])
        assert Fraction(term["neg_weighted_rhs"]) == -mu * Fraction(src["b"])
        w = [Fraction(s) for s in term["weighted_coeffs"]]
        assert w == [mu * Fraction(a) for a in src["coeffs"]]
        for j in range(n):
            lhs[j] += w[j]
        rhs += mu * Fraction(src["b"])
    assert all(v == 0 for v in lhs), f"对偶左侧 Aᵀμ 必须为 0: {lhs}"
    assert [Fraction(s) for s in dual["lhs_sum"]] == lhs
    assert Fraction(dual["rhs_sum"]) == rhs
    # 弱对偶等式: 可行对偶达到原问题总量 => 总量最小
    assert Fraction(dual["neg_rhs_sum"]) == -rhs == total
    # 稳定标识序必须是来源 stable=True 的约束按原索引升序
    expect_order = [i for i, c in enumerate(src_cons) if c.get("stable", False)]
    assert result["stable_order"] == expect_order


def verify_certificate(result: dict) -> None:
    """对返回的不可行证书做独立核验，供测试与 verify 容器复用。

    仅依据证书自身给出的逐项数据重算，不调用求解器。
    """
    assert result["status"] == "infeasible"
    terms = result["terms"]
    n = len(result["combined_lhs"])
    lhs = [Fraction(0) for _ in range(n)]
    rhs = Fraction(0)
    for t in terms:
        mu = Fraction(t["multiplier"])
        assert mu >= 0, "乘子必须非负"
        w = [Fraction(s) for s in t["weighted_coeffs"]]
        assert len(w) == n
        # weighted_rhs 必须等于 mu * b
        assert Fraction(t["weighted_rhs"]) == mu * Fraction(t["b"]), (
            "逐项加权右端不一致"
        )
        for j in range(n):
            lhs[j] += w[j]
        rhs += Fraction(t["weighted_rhs"])
    assert all(v == 0 for v in lhs), f"左侧合并必须为 0，实际 {lhs}"
    assert rhs < 0, f"右侧合并必须严格为负，实际 {rhs}"
    assert [Fraction(s) for s in result["combined_lhs"]] == lhs
    assert Fraction(result["combined_rhs"]) == rhs
    assert result["combined_relation"] == "0 <= " + result["combined_rhs"]
