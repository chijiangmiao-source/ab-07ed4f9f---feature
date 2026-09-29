"""最小上界放宽审计：任意精度有理数两阶段 + 字典序单纯形。

来源系统（整数不等式 a_i·x <= b_i，x 自由）无解时，工程师为每条原约束
寻找非负有理放宽量 d_i >= 0，使放宽后系统

    a_i·x <= b_i + d_i   (i = 1..m)

首次可行。优化按两级固定优先级进行（结果完全确定，可冻结复算）:

  1. 先最小化全部放宽量之和  t = Σ_i d_i；
  2. 在 t = t* 的最优面上，再按**稳定约束标识序**（stable=True 的约束按
     原约束索引升序）字典序最小化放宽子向量 (d_i)_{i∈S}。
     非稳定约束的放宽量参与第一级总和，但不参与字典序打破平局。

原问题的有界对偶（Farkas）形式为

    t* = max −μᵀb  s.t. Aᵀμ = 0, 0 ≤ μ_i ≤ 1。

实现以等式  a_i·(u−v) − d_i + s_i = b_i 建模（自由变量拆 u−v≥0，
s_i≥0 为松弛变量）。表格构造沿用来源求解器的行翻转：b_i<0 的行整体
乘 −1，记 σ_i = sign(b_i)，翻转后右端 |b_i|≥0、松弛/剩余列系数 σ_i、
人工列系数恒为 +1（初值 |b_i|），所有基列保持标准形（系数 +1、
右端非负），最小比值规则与 Bland 规则与来源 Phase-I 完全一致。
字典序阶段维护多条目标行，入基列必须对全部上级目标行约化成本为 0、
对当前目标行约化成本为负——这精确地把搜索限制在上级最优面内，
无需大数加权，全部运算为 :class:`fractions.Fraction`。

最优表 t 目标行在松弛列 s_i 的约化成本即对偶乘子 μ_i:
  * 最优性给 μ_i ≥ 0；d_i 列约化成本 = 1 − μ_i ≥ 0 给 μ_i ≤ 1；
  * 自由变量 u/v 两列约化成本互为相反数给 Aᵀμ = 0；
  * 强对偶给 −μᵀb = t* = Σd_i*。
代码对全部证书条件做运行时断言，不使用浮点、采样或外部 LP 服务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Sequence

from .simplex import Constraint

_ZERO = Fraction(0)


class RelaxationSolveError(RuntimeError):
    """理论上不应发生: 放宽模型恒可行（d 可取任意大）。"""


@dataclass
class RelaxedRow:
    index: int
    label: str | None
    stable: bool
    b: int
    relaxation: Fraction       # d_i
    new_b: Fraction            # b_i + d_i
    new_residual: Fraction     # (b_i + d_i) − a_i·x = s_i ≥ 0

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "label": self.label,
            "stable": self.stable,
            "b": str(self.b),
            "relaxation": _frac_str(self.relaxation),
            "new_b": _frac_str(self.new_b),
            "new_residual": _frac_str(self.new_residual),
        }


@dataclass
class DualTerm:
    """μ_i 对合并证书 Σ μ_i a_i = 0、−Σ μ_i b_i = t* 的逐项贡献。"""

    index: int
    label: str | None
    b: int
    multiplier: Fraction
    weighted_coeffs: tuple[Fraction, ...]
    weighted_rhs: Fraction        # μ_i·b_i
    neg_weighted_rhs: Fraction    # −μ_i·b_i

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "label": self.label,
            "b": str(self.b),
            "multiplier": _frac_str(self.multiplier),
            "weighted_coeffs": [_frac_str(c) for c in self.weighted_coeffs],
            "weighted_rhs": _frac_str(self.weighted_rhs),
            "neg_weighted_rhs": _frac_str(self.neg_weighted_rhs),
        }


@dataclass
class RelaxationResult:
    currents: dict[str, Fraction] = field(default_factory=dict)
    rows: list[RelaxedRow] = field(default_factory=list)
    multipliers: list[Fraction] = field(default_factory=list)
    dual_terms: list[DualTerm] = field(default_factory=list)
    dual_lhs: list[Fraction] = field(default_factory=list)
    total_relaxation: Fraction = _ZERO
    rhs_weighted_sum: Fraction = _ZERO   # Σ μ_i b_i = −t*
    stable_order: list[int] = field(default_factory=list)
    pivots: int = 0

    def as_dict(self) -> dict:
        t = _frac_str(self.total_relaxation)
        return {
            "status": "repaired",
            "pivots": self.pivots,
            "total_relaxation": t,
            "stable_order": list(self.stable_order),
            "currents": {k: _frac_str(v) for k, v in self.currents.items()},
            "constraints": [r.as_dict() for r in self.rows],
            "dual": {
                "bounds": "0 <= multiplier <= 1",
                "multipliers": [
                    {"index": i, "value": _frac_str(mu)}
                    for i, mu in enumerate(self.multipliers)
                ],
                "terms": [term.as_dict() for term in self.dual_terms],
                "lhs_sum": [_frac_str(v) for v in self.dual_lhs],
                "rhs_sum": _frac_str(self.rhs_weighted_sum),
                "neg_rhs_sum": t,
                "objective": "-sum(multiplier_i * b_i) = " + t,
                "total_relaxation": t,
            },
        }


def _frac_str(v: Fraction) -> str:
    if v.denominator == 1:
        return str(v.numerator)
    return f"{v.numerator}/{v.denominator}"


def solve_relaxation(
    variable_names: Sequence[str],
    constraints: Sequence[Constraint],
    stable_flags: Sequence[bool],
) -> RelaxationResult:
    n = len(variable_names)
    m = len(constraints)
    if not 1 <= n <= 8:
        raise ValueError("变量个数必须在 1 到 8 之间")
    if not 1 <= m <= 48:
        raise ValueError("约束条数必须在 1 到 48 之间")
    if len(stable_flags) != m:
        raise ValueError("稳定标识数量必须与约束条数一致")

    # ---- 列布局: [u_1,v_1,...,u_n,v_n] [松弛/剩余 s m 列] [放宽 d m 列] [人工 k 列]
    # b_i<0 的行整体乘 σ_i=-1（翻转后右端 |b_i|≥0、人工列系数恒 +1）；
    # 翻转行方程为 σ_i a_i·(u−v) + σ_i s_i − σ_i d_i + e_i = σ_i b_i。
    slack0 = 2 * n
    d0 = 2 * n + m
    art0 = 2 * n + 2 * m
    sigma = [1 if con.b >= 0 else -1 for con in constraints]
    art_rows = [i for i, con in enumerate(constraints) if con.b < 0]
    art_col_of = {i: art0 + q for q, i in enumerate(art_rows)}
    total_cols = art0 + len(art_rows)

    tab: list[list[Fraction]] = [
        [Fraction(0) for _ in range(total_cols)] for _ in range(m)
    ]
    rhs: list[Fraction] = []
    basis: list[int] = []
    for i, con in enumerate(constraints):
        s_i = sigma[i]
        for j, a in enumerate(con.coeffs):
            tab[i][2 * j] = Fraction(s_i * a)       # u_j
            tab[i][2 * j + 1] = Fraction(-s_i * a)  # v_j
        tab[i][slack0 + i] = Fraction(s_i)   # b<0 时为剩余变量(-1)
        tab[i][d0 + i] = Fraction(-s_i)      # −σ_i d_i
        if con.b < 0:
            tab[i][art_col_of[i]] = Fraction(1)
            basis.append(art_col_of[i])
        else:
            basis.append(slack0 + i)
        rhs.append(Fraction(s_i * con.b))  # |b_i| ≥ 0

    pivots = 0

    def pivot(
        r: int,
        c: int,
        obj_rows: Sequence[tuple[list[Fraction], list[Fraction]]],
    ) -> None:
        """高斯-若当枢轴；同步消元给定的（目标行, 常数）列表。"""
        nonlocal pivots
        a = tab[r][c]
        for j in range(total_cols):
            tab[r][j] /= a
        rhs[r] /= a
        for r2 in range(len(tab)):
            if r2 == r:
                continue
            f = tab[r2][c]
            if f == 0:
                continue
            for j in range(total_cols):
                tab[r2][j] -= f * tab[r][j]
            rhs[r2] -= f * rhs[r]
        for z, zconst in obj_rows:
            f = z[c]
            if f == 0:
                continue
            for j in range(total_cols):
                z[j] -= f * tab[r][j]
            zconst[0] += f * rhs[r]
        basis[r] = c
        pivots += 1

    def leaving_row(c: int) -> int:
        """Bland 出基: 严格最小比值；并列取基变量列编号最小。"""
        best_r, best_ratio = -1, None
        for r in range(len(tab)):
            a = tab[r][c]
            if a > 0:
                ratio = rhs[r] / a
                if (
                    best_r < 0
                    or ratio < best_ratio
                    or (ratio == best_ratio and basis[r] < basis[best_r])
                ):
                    best_r, best_ratio = r, ratio
        return best_r

    # ---- Phase I: w = Σ 人工变量 ----
    # e_i = σ_i b_i − σ_i a_i·(u−v) − σ_i s_i + σ_i d_i （仅 b_i<0 的行）
    w = [Fraction(0) for _ in range(total_cols)]
    wbox = [Fraction(0)]
    for i in art_rows:
        s_i = sigma[i]
        wbox[0] += Fraction(s_i * constraints[i].b)
        for j, a in enumerate(constraints[i].coeffs):
            w[2 * j] -= Fraction(s_i * a)
            w[2 * j + 1] += Fraction(s_i * a)
        w[slack0 + i] -= Fraction(s_i)
        w[d0 + i] += Fraction(s_i)

    while True:
        basis_set = set(basis)
        enter = -1
        for c in range(art0):  # 人工列永不重新入基
            if c not in basis_set and w[c] < 0:
                enter = c
                break
        if enter < 0:
            break
        r = leaving_row(enter)
        if r < 0:
            raise RelaxationSolveError("Phase-I 人工变量和无界，模型构造有误")
        pivot(r, enter, [(w, wbox)])
    if wbox[0] != 0:
        raise RelaxationSolveError(
            f"放宽模型不可行（w*={wbox[0]}）；放宽量无上界，理论上不应发生"
        )

    # ---- 清理仍留在基中的人工列（取值必为 0）：换出或剔除冗余 e=0 行 ----
    kept_tab: list[list[Fraction]] = []
    kept_rhs: list[Fraction] = []
    kept_basis: list[int] = []
    for r in range(len(tab)):
        if basis[r] < art0:
            kept_tab.append(tab[r])
            kept_rhs.append(rhs[r])
            kept_basis.append(basis[r])
            continue
        if rhs[r] != 0:
            raise RelaxationSolveError("人工变量在基却取正值，枢轴实现有误")
        nonbasic = set(range(art0)) - set(basis)
        enter = next((c for c in sorted(nonbasic) if tab[r][c] != 0), -1)
        if enter < 0:
            # 非人工部分整行为 0：冗余等式 e=0，直接剔除
            continue
        # rhs=0 上枢轴不改变其余行右端，可行性保持
        a = tab[r][enter]
        for j in range(total_cols):
            tab[r][j] /= a
        rhs[r] /= a
        for r2 in range(len(tab)):
            if r2 == r:
                continue
            f = tab[r2][enter]
            if f == 0:
                continue
            for j in range(total_cols):
                tab[r2][j] -= f * tab[r][j]
            rhs[r2] -= f * rhs[r]
        basis[r] = enter
        pivots += 1
        kept_tab.append(tab[r])
        kept_rhs.append(rhs[r])
        kept_basis.append(basis[r])
    tab, rhs, basis = kept_tab, kept_rhs, kept_basis

    def fresh_objective(d_index: int | None) -> tuple[list[Fraction], list[Fraction]]:
        """构造目标行（d_index=None → Σd；否则单个 d_i），并对当前基消元。"""
        z = [Fraction(0) for _ in range(total_cols)]
        box = [Fraction(0)]
        if d_index is None:
            for i in range(m):
                z[d0 + i] = Fraction(1)
        else:
            z[d0 + d_index] = Fraction(1)
        for r in range(len(tab)):
            f = z[basis[r]]
            if f == 0:
                continue
            for j in range(total_cols):
                z[j] -= f * tab[r][j]
            box[0] += f * rhs[r]
        return z, box

    def optimize(
        z: list[Fraction],
        box: list[Fraction],
        prior: Sequence[tuple[list[Fraction], list[Fraction]]],
    ) -> None:
        """在上级目标行 prior 全部约化成本为 0 的列上，Bland 最小化 z。"""
        while True:
            basis_set = set(basis)
            enter = -1
            for c in range(art0):
                if c in basis_set:
                    continue
                if any(pz[c] != 0 for pz, _ in prior):
                    continue
                if z[c] < 0:
                    enter = c
                    break
            if enter < 0:
                return
            r = leaving_row(enter)
            if r < 0:
                raise RelaxationSolveError("放宽总量无界；d≥0 必有下界，不应发生")
            obj_rows = [(z, box), *prior]
            pivot(r, enter, obj_rows)

    # ---- 第一级: 最小化 t = Σd ----
    z0, z0box = fresh_objective(None)
    objective_rows: list[tuple[list[Fraction], list[Fraction]]] = []
    optimize(z0, z0box, objective_rows)
    objective_rows.append((z0, z0box))

    # ---- 第二级: 按稳定约束索引序字典序最小化 ----
    stable_order = [i for i, s in enumerate(stable_flags) if s]
    for di in stable_order:
        zk, zkbox = fresh_objective(di)
        # 只能在所有上级（含 Σd 与已固定的 d_j）最优面内移动
        optimize(zk, zkbox, objective_rows)
        objective_rows.append((zk, zkbox))

    # ---- 读取解 ----
    values = [Fraction(0) for _ in range(total_cols)]
    for r, col in enumerate(basis):
        values[col] = rhs[r]

    currents = {
        name: values[2 * j] - values[2 * j + 1]
        for j, name in enumerate(variable_names)
    }
    d = [values[d0 + i] for i in range(m)]
    slack = [values[slack0 + i] for i in range(m)]
    total = sum(d, _ZERO)
    if any(di < 0 for di in d):
        raise RelaxationSolveError("放宽量为负")
    if total != z0box[0]:
        raise RelaxationSolveError("放宽总量与目标值不一致")

    rows: list[RelaxedRow] = []
    for i, con in enumerate(constraints):
        ax = sum(
            (Fraction(con.coeffs[j]) * currents[name]
             for j, name in enumerate(variable_names)),
            _ZERO,
        )
        new_b = Fraction(con.b) + d[i]
        residual = new_b - ax
        if residual != slack[i]:
            raise RelaxationSolveError(
                f"约束 {i} 新余量与松弛变量不一致: {residual} != {slack[i]}"
            )
        if residual < 0:
            raise RelaxationSolveError(f"约束 {i} 放宽后仍不可行")
        rows.append(RelaxedRow(
            index=i,
            label=con.label,
            stable=bool(stable_flags[i]),
            b=con.b,
            relaxation=d[i],
            new_b=new_b,
            new_residual=residual,
        ))

    # ---- 有界对偶乘子 μ_i = t 目标行松弛列约化成本 ----
    multipliers = [z0[slack0 + i] for i in range(m)]
    for i, mu in enumerate(multipliers):
        if not (0 <= mu <= 1):
            raise RelaxationSolveError(f"对偶乘子 μ_{i}={mu} 越出 [0,1]")
        # d_i 列约化成本 = 1 − μ_i，最优性下非负（基列时双方均为 0）
        rc_d = z0[d0 + i]
        if rc_d != 1 - mu:
            raise RelaxationSolveError(
                f"约束 {i}: d 列约化成本 {rc_d} 与 1−μ={1 - mu} 不一致"
            )
        if rc_d < 0:
            raise RelaxationSolveError(f"约束 {i}: 1−μ_i 为负")

    dual_lhs = [
        sum((multipliers[i] * Fraction(constraints[i].coeffs[j])
             for i in range(m)), _ZERO)
        for j in range(n)
    ]
    if any(v != 0 for v in dual_lhs):
        raise RelaxationSolveError("对偶证书 Aᵀμ 非零")
    rhs_sum = sum(
        (multipliers[i] * Fraction(constraints[i].b) for i in range(m)), _ZERO
    )
    if -rhs_sum != total:
        raise RelaxationSolveError(
            f"对偶目标 −μᵀb={-rhs_sum} 不等于最小放宽总量 {total}"
        )

    dual_terms = [
        DualTerm(
            index=i,
            label=con.label,
            b=con.b,
            multiplier=multipliers[i],
            weighted_coeffs=tuple(
                multipliers[i] * Fraction(a) for a in con.coeffs
            ),
            weighted_rhs=multipliers[i] * Fraction(con.b),
            neg_weighted_rhs=-(multipliers[i] * Fraction(con.b)),
        )
        for i, con in enumerate(constraints)
    ]

    return RelaxationResult(
        currents=currents,
        rows=rows,
        multipliers=multipliers,
        dual_terms=dual_terms,
        dual_lhs=dual_lhs,
        total_relaxation=total,
        rhs_weighted_sum=rhs_sum,
        stable_order=stable_order,
        pivots=pivots,
    )
