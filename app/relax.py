"""最小上界放宽：精确有理 LP + 稳定标识序字典序优化。

给定原无解系统 a_i·x ≤ b_i（x 为自由变量），求各约束右端的非负有理
放宽量 d_i ≥ 0，使 a_i·x ≤ b_i + d_i 首次可行：

  1. 先最小化总放宽量  T = Σ_i d_i；
  2. 在所有总放宽量恰为 T 的方案中，按稳定约束标识序（stable=True 的约束
     依原约束顺序）字典序最小化放宽向量 (d_{s_1}, d_{s_2}, …)。

标准形（与 ``simplex.py`` 相同的自由变量拆分与 σ 翻号约定）:
  * x_j = u_j − v_j，u_j, v_j ≥ 0；
  * σ_i = sign(b_i)，行乘 σ_i 后右端 |b_i| ≥ 0；
  * 行方程 σ_i a_i·(u−v) − σ_i d_i + σ_i s_i + t_i = σ_i b_i，
    其中 s_i ≥ 0 为（翻号后的）松弛列，t_i 为人工变量；
  * Phase-I 最小化 Σt（Bland 规则，人工列永不重新入基）。放宽模型必然
    可行（x=0、d_i=max(0,-b_i) 即可），Phase-I 终值必为 0；
  * Phase-II 目标 Σd，Bland 规则求得总放宽最小解。

有界对偶证书
============
原问题 min Σd s.t. a_i·x − d_i ≤ b_i, d_i ≥ 0, x 自由 的对偶为

    max −bᵀλ  s.t.  Aᵀλ = 0,  0 ≤ λ_i ≤ 1。

在翻号标准形中，令 ỹ_i 为第 i 行等式乘子、y_i = σ_i ỹ_i，则松弛列与
放宽列的约化成本恰为

    r_{s_i} = λ_i,  r_{d_i} = 1 − λ_i，

故 0 ≤ λ ≤ 1 由最优性自动保证，且 −Σ λ_i b_i = T。代码对乘子区间、
Aᵀλ = 0、−λᵀb = T 及 λ_i + r_{d_i} = 1 全部做运行时断言。

字典序
======
首次最优表中快照对偶乘子（随后续优化不变，因总放宽量 T 已被等式行
固定）。依次加入“Σd = T”与“d_{s_k} = 当前最优值”的规范等式行
（人工变量恒为 0，永不重新入基），逐坐标以 Bland 规则最小化，得到
稳定标识序下的字典序最小放宽向量。

全程 ``fractions.Fraction``，无浮点、无采样、无外部 LP 服务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Sequence

from .simplex import Constraint


class RelaxationError(RuntimeError):
    """理论上不应发生：放宽模型不可行或最小化无界。"""


@dataclass(frozen=True)
class RelaxItem:
    index: int
    label: str | None
    stable: bool
    b: int
    increment: Fraction       # d_i：右端应增加的非负有理量
    new_b: Fraction           # b_i + d_i
    residual: Fraction        # 新余量 (b_i + d_i) − a_i·x（≥ 0）

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "label": self.label,
            "stable": self.stable,
            "b": str(self.b),
            "increment": _frac_str(self.increment),
            "new_b": _frac_str(self.new_b),
            "residual": _frac_str(self.residual),
        }


@dataclass(frozen=True)
class DualTerm:
    index: int
    label: str | None
    stable: bool
    b: int
    multiplier: Fraction
    coeffs: tuple[Fraction, ...]
    rhs: Fraction

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "label": self.label,
            "stable": self.stable,
            "b": str(self.b),
            "multiplier": _frac_str(self.multiplier),
            "weighted_coeffs": [_frac_str(c) for c in self.coeffs],
            "weighted_rhs": _frac_str(self.rhs),
        }


@dataclass
class RelaxResult:
    variables: tuple[str, ...]
    items: list[RelaxItem]
    currents: dict[str, Fraction] = field(default_factory=dict)
    total: Fraction = Fraction(0)
    multipliers: list[Fraction] = field(default_factory=list)
    dual_terms: list[DualTerm] = field(default_factory=list)
    dual_lhs: list[Fraction] = field(default_factory=list)
    neg_dual_rhs: Fraction = Fraction(0)
    lex_order: list[int] = field(default_factory=list)
    pivots: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "status": "relaxed",
            "policy": (
                "min sum of non-negative rational RHS relaxations, then "
                "lexicographic min over stable-flagged constraints in "
                "original constraint order"
            ),
            "lex_order": [
                {"index": i, "stable": True} for i in self.lex_order
            ],
            "total_relaxation": _frac_str(self.total),
            "currents": {name: _frac_str(v) for name, v in self.currents.items()},
            "items": [it.as_dict() for it in self.items],
            "dual": {
                "bounds": "[0, 1]",
                "multipliers": [
                    {"index": i, "value": _frac_str(lam)}
                    for i, lam in enumerate(self.multipliers)
                ],
                "terms": [t.as_dict() for t in self.dual_terms],
                "weighted_lhs": [_frac_str(v) for v in self.dual_lhs],
                "neg_weighted_rhs": _frac_str(self.neg_dual_rhs),
                "total_relaxation": _frac_str(self.total),
                "relation": (
                    "-sum(lambda_i * b_i) = "
                    + _frac_str(self.neg_dual_rhs)
                    + " = total_relaxation"
                ),
            },
            "pivots": dict(self.pivots),
        }


def _frac_str(v: Fraction) -> str:
    if v.denominator == 1:
        return str(v.numerator)
    return f"{v.numerator}/{v.denominator}"


class _Solver:
    def __init__(self, variable_names: Sequence[str],
                 constraints: Sequence[Constraint],
                 stable_flags: Sequence[bool]):
        self.names = list(variable_names)
        self.cons = list(constraints)
        self.stable = list(stable_flags)
        self.n = len(self.names)
        self.m = len(self.cons)

        # 固定列段：u,v | d（放宽量） | s（翻号松弛） | 人工（逐行追加）
        self.d0 = 2 * self.n
        self.s0 = 2 * self.n + self.m
        self.art0 = 2 * self.n + 2 * self.m
        self.ncols = 2 * self.n + 3 * self.m
        self.art_cols = set(range(self.art0, self.ncols))

        sigma = [1 if c.b >= 0 else -1 for c in self.cons]
        self.sigma = sigma
        self.tab = [
            [Fraction(0) for _ in range(self.ncols)] for _ in range(self.m)
        ]
        self.rhs: list[Fraction] = []
        self.basis: list[int] = []
        for i, con in enumerate(self.cons):
            si = sigma[i]
            for j, a in enumerate(con.coeffs):
                self.tab[i][2 * j] = Fraction(si * a)
                self.tab[i][2 * j + 1] = Fraction(-si * a)
            self.tab[i][self.d0 + i] = Fraction(-si)
            self.tab[i][self.s0 + i] = Fraction(si)
            self.tab[i][self.art0 + i] = Fraction(1)
            self.rhs.append(Fraction(si * con.b))
            self.basis.append(self.art0 + i)

        self.pivot_counts = {"phase_i": 0, "artificial_cleanup": 0,
                             "phase_ii": 0, "lexicographic": 0}

    # ---- 通用 Bland 高斯-若当枢轴 ----
    def _pivot(self, entering: int, leaving: int, obj: list[Fraction],
               obj0: Fraction) -> Fraction:
        a = self.tab[leaving][entering]
        for j in range(self.ncols):
            self.tab[leaving][j] /= a
        self.rhs[leaving] /= a
        for r in range(len(self.tab)):
            if r == leaving:
                continue
            factor = self.tab[r][entering]
            if factor == 0:
                continue
            for j in range(self.ncols):
                self.tab[r][j] -= factor * self.tab[leaving][j]
            self.rhs[r] -= factor * self.rhs[leaving]
        factor = obj[entering]
        for j in range(self.ncols):
            obj[j] -= factor * self.tab[leaving][j]
        obj0 += factor * self.rhs[leaving]
        self.basis[leaving] = entering
        return obj0

    def _bland_step(self, obj: list[Fraction], obj0: Fraction,
                    count_key: str) -> Fraction:
        """取一次 Bland 枢轴；无入基列时返回 None 标志由调用方处理。"""
        basis_set = set(self.basis)
        entering = -1
        for col in range(self.ncols):
            if col in basis_set or col in self.art_cols:
                continue
            if obj[col] < 0:
                entering = col
                break
        if entering < 0:
            return None  # type: ignore[return-value]

        leaving = -1
        best_ratio: Fraction | None = None
        for r in range(len(self.tab)):
            a = self.tab[r][entering]
            if a > 0:
                ratio = self.rhs[r] / a
                if (
                    best_ratio is None
                    or ratio < best_ratio
                    or (ratio == best_ratio and self.basis[r] < self.basis[leaving])
                ):
                    best_ratio = ratio
                    leaving = r
        if leaving < 0:
            raise RelaxationError("放宽 LP 目标无界（模型构造有误）")

        self.pivot_counts[count_key] += 1
        return self._pivot(entering, leaving, obj, obj0)

    def _optimize(self, obj: list[Fraction], obj0: Fraction,
                  count_key: str) -> Fraction:
        while True:
            nxt = self._bland_step(obj, obj0, count_key)
            if nxt is None:
                return obj0
            obj0 = nxt

    # ---- Phase I：最小化人工变量之和 ----
    def _phase_i(self) -> None:
        obj = [Fraction(0) for _ in range(self.ncols)]
        obj0 = Fraction(0)
        for i, con in enumerate(self.cons):
            si = self.sigma[i]
            obj0 += Fraction(si * con.b)
            for j, a in enumerate(con.coeffs):
                obj[2 * j] -= Fraction(si * a)
                obj[2 * j + 1] += Fraction(si * a)
            obj[self.d0 + i] += Fraction(si)
            obj[self.s0 + i] -= Fraction(si)

        obj0 = self._optimize(obj, obj0, "phase_i")
        if obj0 != 0:
            raise RelaxationError(
                f"放宽模型 Phase-I 终值 {obj0} 非 0（必然可行的模型不应发生）"
            )

        # 防御性步骤：本表中每行都有专属的单位“逃逸”列（b≥0 行的松弛列、
        # b<0 行的放宽列，约化成本初值为 −1 且该列仅在本行系数非零），
        # 故 Phase-I 最优时人工列必然已全部出基；以下循环在本结构下不可达，
        # 保留它仅为在表结构被改动时给出明确失败点而非静默错误。
        for r in range(len(self.tab)):
            if self.basis[r] not in self.art_cols:
                continue
            entering = -1
            for col in range(self.ncols):
                if col in self.art_cols:
                    continue
                if self.tab[r][col] != 0:
                    entering = col
                    break
            if entering < 0:
                continue  # 0 = 0 冗余行
            # rhs[r] = 0，任意非零枢轴都保持可行性（退化枢轴）
            self.pivot_counts["artificial_cleanup"] += 1
            obj0 = self._pivot(entering, r, obj, obj0)
            if obj0 != 0:
                raise RelaxationError("逐出人工列后 Phase-I 终值改变")

    def _canonical_objective(self, costs: dict[int, Fraction]):
        """按当前基把目标 c·z 化为 w = obj0 + Σ obj_j z_j。"""
        obj = [Fraction(0) for _ in range(self.ncols)]
        for col, c in costs.items():
            obj[col] = c
        obj0 = Fraction(0)
        for r, q in enumerate(self.basis):
            f = obj[q]
            if f == 0:
                continue
            for j in range(self.ncols):
                obj[j] -= f * self.tab[r][j]
            obj0 += f * self.rhs[r]
        return obj, obj0

    def _values(self) -> list[Fraction]:
        values = [Fraction(0) for _ in range(self.ncols)]
        for r, col in enumerate(self.basis):
            values[col] = self.rhs[r]
        return values

    def _add_equality(self, g: dict[int, Fraction], c_value: Fraction) -> None:
        r"""加入由当前 BFS 恰好满足的等式 g·z = c_value（故右端差为 0）。

        新行带一个恒为 0、永不重新入基的人工变量：
            t_new + Σ_j β_j z_j = c_value − g(basis 值) = 0。
        """
        # 追加人工列
        new_art = self.ncols
        for row in self.tab:
            row.append(Fraction(0))
        self.ncols += 1
        self.art_cols.add(new_art)

        basic_const = {q: g.get(q, Fraction(0)) for q in self.basis}
        c0 = sum(
            (basic_const[q] * self.rhs[r] for r, q in enumerate(self.basis)),
            Fraction(0),
        )
        new_row = [Fraction(0) for _ in range(self.ncols)]
        basis_set = set(self.basis)
        for j in range(new_art):  # 新人工列自身系数单独置 1
            if j in basis_set or j in self.art_cols:
                continue
            beta = g.get(j, Fraction(0))
            for r, q in enumerate(self.basis):
                beta -= basic_const[q] * self.tab[r][j]
            new_row[j] = beta
        new_row[new_art] = Fraction(1)
        self.tab.append(new_row)
        self.rhs.append(c_value - c0)
        self.basis.append(new_art)
        if self.rhs[-1] != 0:
            raise RelaxationError("加入的等式行在当前 BFS 处不成立")

    def solve(self) -> RelaxResult:
        self._phase_i()

        # Phase II：最小化 Σ d_i，并在首张最优表快照有界对偶乘子
        costs = {self.d0 + i: Fraction(1) for i in range(self.m)}
        obj, obj0 = self._canonical_objective(costs)
        obj0 = self._optimize(obj, obj0, "phase_ii")
        total = obj0
        if total < 0:
            raise RelaxationError(f"总放宽量为负 {total}")

        multipliers = []
        for i in range(self.m):
            lam = obj[self.s0 + i]  # r_s = λ
            r_d = obj[self.d0 + i]  # r_d = 1 − λ
            if lam < 0 or lam > 1:
                raise RelaxationError(f"对偶乘子越界: λ_{i}={lam}")
            if lam + r_d != 1:
                raise RelaxationError(
                    f"第 {i} 行 λ + r_d = {lam + r_d} ≠ 1"
                )
            multipliers.append(lam)

        # 稳定标识序：先固定 Σd = T，再逐坐标字典序最小化
        lex_order = [i for i, st in enumerate(self.stable) if st]
        if lex_order:
            self._add_equality(
                {self.d0 + i: Fraction(1) for i in range(self.m)}, total
            )
            fixed: list[tuple[int, Fraction]] = []
            for k in lex_order:
                obj, obj0 = self._canonical_objective(
                    {self.d0 + k: Fraction(1)}
                )
                obj0 = self._optimize(obj, obj0, "lexicographic")
                vk = self._values()[self.d0 + k]
                if vk < 0:
                    raise RelaxationError(f"字典序优化中 d_{k} 为负")
                self._add_equality({self.d0 + k: Fraction(1)}, vk)
                fixed.append((k, vk))

        values = self._values()
        currents = {
            name: values[2 * j] - values[2 * j + 1]
            for j, name in enumerate(self.names)
        }
        increments = [values[self.d0 + i] for i in range(self.m)]

        items = []
        for i, con in enumerate(self.cons):
            d_i = increments[i]
            if d_i < 0:
                raise RelaxationError(f"放宽量 d_{i}={d_i} 为负")
            new_b = Fraction(con.b) + d_i
            residual = new_b - sum(
                (Fraction(con.coeffs[j]) * currents[name]
                 for j, name in enumerate(self.names)),
                Fraction(0),
            )
            if residual < 0:
                raise RelaxationError(f"第 {i} 条放宽后仍不可行: 余量 {residual}")
            items.append(
                RelaxItem(
                    index=i,
                    label=con.label,
                    stable=self.stable[i],
                    b=con.b,
                    increment=d_i,
                    new_b=new_b,
                    residual=residual,
                )
            )

        if sum(increments, Fraction(0)) != total:
            raise RelaxationError("字典序优化后总放宽量发生变化")

        # ---- 独立汇总对偶证据并断言 ----
        dual_lhs = [Fraction(0) for _ in range(self.n)]
        dual_rhs = Fraction(0)
        dual_terms = []
        for i, con in enumerate(self.cons):
            lam = multipliers[i]
            coeffs = tuple(lam * Fraction(a) for a in con.coeffs)
            rhs = lam * Fraction(con.b)
            for j in range(self.n):
                dual_lhs[j] += coeffs[j]
            dual_rhs += rhs
            dual_terms.append(
                DualTerm(
                    index=i, label=con.label, stable=self.stable[i],
                    b=con.b, multiplier=lam, coeffs=coeffs, rhs=rhs,
                )
            )
        if any(v != 0 for v in dual_lhs):
            raise RelaxationError(f"对偶左侧 Aᵀλ 非零: {dual_lhs}")
        neg_dual_rhs = -dual_rhs
        if neg_dual_rhs != total:
            raise RelaxationError(
                f"对偶等式 −λᵀb = {neg_dual_rhs} ≠ 总放宽量 {total}"
            )

        return RelaxResult(
            variables=tuple(self.names),
            items=items,
            currents=currents,
            total=total,
            multipliers=multipliers,
            dual_terms=dual_terms,
            dual_lhs=dual_lhs,
            neg_dual_rhs=neg_dual_rhs,
            lex_order=lex_order,
            pivots=dict(self.pivot_counts),
        )


def relax(
    variable_names: Sequence[str],
    constraints: Sequence[Constraint],
    stable_flags: Sequence[bool],
) -> RelaxResult:
    """求最小上界放宽方案与有界对偶证书（全程精确有理数）。"""
    if not 1 <= len(variable_names) <= 8:
        raise ValueError("变量个数必须在 1 到 8 之间")
    if not 1 <= len(constraints) <= 48:
        raise ValueError("约束条数必须在 1 到 48 之间")
    if len(constraints) != len(stable_flags):
        raise ValueError("稳定标识数量必须与约束条数一致")
    return _Solver(variable_names, constraints, stable_flags).solve()
