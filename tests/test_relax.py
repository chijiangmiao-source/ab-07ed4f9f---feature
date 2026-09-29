"""最小上界放宽求解器测试。

除常规断言外，使用与单纯形完全独立的 Fourier–Motzkin 消元
（``tests/fourier_motzkin.py``）严格复核稳定标识序的字典序最小性：
在“Σd = T、前缀坐标固定”的面上，消去其余全部变量，直接读出
d_k 的精确最小值必须等于求解器返回值。
"""

from __future__ import annotations

import random
from fractions import Fraction

import pytest

from app.relax import relax
from app.simplex import Constraint
from tests.fourier_motzkin import fm_feasible


def C(coeffs, b, label=None):
    return Constraint(tuple(coeffs), b, label)


def verify_relaxation(variables, constraints, stable_flags, result=None):
    """独立复核：原语可行、弱对偶下界、对偶等式；返回 result。"""
    r = result if result is not None else relax(
        variables, constraints, stable_flags
    )
    n, m = len(variables), len(constraints)
    assert r.lex_order == [i for i, s in enumerate(stable_flags) if s]

    # 1. 原语可行：a_i·x ≤ b_i + d_i，d_i ≥ 0
    for i, con in enumerate(constraints):
        it = r.items[i]
        assert it.increment >= 0
        lhs = sum(
            Fraction(con.coeffs[j]) * r.currents[name]
            for j, name in enumerate(variables)
        )
        assert lhs <= Fraction(con.b) + it.increment
        assert it.new_b == Fraction(con.b) + it.increment
        assert it.residual == Fraction(con.b) + it.increment - lhs
    assert sum(it.increment for it in r.items) == r.total

    # 2. 逐项独立重算对偶证书
    assert len(r.multipliers) == m
    lhs = [Fraction(0) for _ in range(n)]
    neg_rhs = Fraction(0)
    for i, (con, lam) in enumerate(zip(constraints, r.multipliers)):
        assert Fraction(0) <= lam <= Fraction(1)
        term = r.dual_terms[i]
        assert term.multiplier == lam
        assert term.coeffs == tuple(lam * Fraction(a) for a in con.coeffs)
        assert term.rhs == lam * Fraction(con.b)
        for j, a in enumerate(con.coeffs):
            lhs[j] += lam * Fraction(a)
        neg_rhs -= lam * Fraction(con.b)
    assert all(v == 0 for v in lhs)  # Aᵀλ = 0
    assert neg_rhs == r.total       # −λᵀb = T
    assert r.dual_lhs == lhs and r.neg_dual_rhs == neg_rhs

    # 3. 弱对偶独立给出下界：任何可行放宽必有 Σd′ ≥ −λᵀb = T
    #    （λᵀAx′ = 0 ≤ λᵀb + Σλ_i d_i′ ≤ λᵀb + Σd_i′）
    return r


def fm_min_coordinate(constraints, total: Fraction,
                      fixed: dict[int, Fraction], target: int) -> Fraction:
    """在 {A X − D ≤ b, D ≥ 0, ΣD = total, D_i = v (i∈fixed)} 上，
    用 Fourier–Motzkin 消元独立求出 D_target 的精确最小值。

    与求解器完全不同的算法：把 target 列放在最后，消去其余全部
    （自由）变量后，剩余单变量不等式 α·t ≤ β，α<0 给出下界 t ≥ β/α，
    最大者即最小值（−D_target ≤ 0 保证至少有下界 0）。
    """
    n = len(constraints[0].coeffs)
    m = len(constraints)
    d_cols = [i for i in range(m) if i != target] + [target]
    ncols = n + m

    def xcol(j):
        return j

    def dcol(i):
        return n + d_cols.index(i)

    rows: list[tuple[list[Fraction], Fraction]] = []
    for i0, con in enumerate(constraints):
        v = [Fraction(0)] * ncols
        for j, a in enumerate(con.coeffs):
            v[xcol(j)] = Fraction(a)
        v[dcol(i0)] = Fraction(-1)  # a·x − D_i ≤ b_i
        rows.append((v, Fraction(con.b)))
    for i in range(m):  # D_i ≥ 0
        v = [Fraction(0)] * ncols
        v[dcol(i)] = Fraction(-1)
        rows.append((v, Fraction(0)))
    for sign in (1, -1):  # ΣD = total
        v = [Fraction(0)] * ncols
        for i in range(m):
            v[dcol(i)] = Fraction(sign)
        rows.append((v, Fraction(sign * total)))
    for i, val in fixed.items():  # D_i = val
        for sign in (1, -1):
            v = [Fraction(0)] * ncols
            v[dcol(i)] = Fraction(sign)
            rows.append((v, Fraction(sign * val)))

    # 依次消去除最后一列（target）外的全部变量
    for _ in range(ncols - 1):
        pos, zero, neg = [], [], []
        for v, rhs in rows:
            c = v[0]
            if c > 0:
                pos.append((v, rhs))
            elif c < 0:
                neg.append((v, rhs))
            else:
                zero.append((v[1:], rhs))
        new = list(zero)
        for vp, bp in pos:
            for vn, bn in neg:
                cp, cn = vp[0], vn[0]
                f1, f2 = 1 / cp, -1 / cn
                new.append((
                    [f1 * vp[k] + f2 * vn[k] for k in range(1, len(vp))],
                    f1 * bp + f2 * bn,
                ))
        rows = new

    lower = Fraction(0)
    for v, rhs in rows:
        alpha = v[0]
        if alpha == 0:
            assert rhs >= 0, "前缀固定面不可行（返回解本应证明其可行）"
        elif alpha < 0:
            lower = max(lower, rhs / alpha)
    return lower


def fm_min_total(constraints) -> Fraction:
    """FM 独立求 min ΣD s.t. AX−D≤b, D≥0：追加自由变量 t 与 ΣD−t≤0，
    消去其余全部变量后从 α·t ≤ β（α<0）读出 t 的精确下界。"""
    n = len(constraints[0].coeffs)
    m = len(constraints)
    ncols = n + m + 1
    rows: list[tuple[list[Fraction], Fraction]] = []
    for i0, con in enumerate(constraints):
        v = [Fraction(a) for a in con.coeffs]
        v += [Fraction(-1 if i == i0 else 0) for i in range(m)]
        v.append(Fraction(0))
        rows.append((v, Fraction(con.b)))
    for i in range(m):
        v = [Fraction(0)] * ncols
        v[n + i] = Fraction(-1)
        rows.append((v, Fraction(0)))
    v = [Fraction(0)] * n + [Fraction(1)] * m + [Fraction(-1)]  # ΣD−t≤0
    rows.append((v, Fraction(0)))

    for _ in range(ncols - 1):
        pos, zero, neg = [], [], []
        for vec, rhs in rows:
            c = vec[0]
            if c > 0:
                pos.append((vec, rhs))
            elif c < 0:
                neg.append((vec, rhs))
            else:
                zero.append((vec[1:], rhs))
        new = list(zero)
        for vp, bp in pos:
            for vn, bn in neg:
                cp, cn = vp[0], vn[0]
                f1, f2 = 1 / cp, -1 / cn
                new.append((
                    [f1 * vp[k] + f2 * vn[k] for k in range(1, len(vp))],
                    f1 * bp + f2 * bn,
                ))
        rows = new

    lower = Fraction(0)
    for vec, rhs in rows:
        if vec[0] < 0:
            lower = max(lower, rhs / vec[0])
    return lower


# ---- 基础算例（手算期望向量） ----
def test_zero_leq_minus_one():
    r = verify_relaxation(["x"], [C([0], -1)], [True])
    assert r.total == 1
    assert [it.increment for it in r.items] == [1]
    assert r.multipliers == [1]
    assert r.currents == {"x": 0}


def test_combo_stable_first_forces_burden_onto_second():
    cons = [C([1], 0), C([-1], -1)]
    r = verify_relaxation(["x"], cons, [True, False])
    assert r.total == 1
    assert [it.increment for it in r.items] == [0, 1]
    assert r.multipliers == [1, 1]
    assert r.currents == {"x": 0}


def test_combo_stable_second_keeps_second_tight():
    cons = [C([1], 0), C([-1], -1)]
    r = verify_relaxation(["x"], cons, [False, True])
    assert [it.increment for it in r.items] == [1, 0]
    assert r.currents == {"x": 1}


def test_no_stable_flags_lex_order_empty():
    cons = [C([1], 0), C([-1], -1)]
    r = verify_relaxation(["x"], cons, [False, False])
    assert r.lex_order == []
    assert r.total == 1
    # 无字典序偏好时返回 Bland 给出的某个最优面 BFS
    assert sum(it.increment for it in r.items) == 1


def test_duplicate_contradiction_relaxes_both():
    cons = [C([0], -1), C([0], -1)]
    r = verify_relaxation(["x"], cons, [True, False])
    assert r.total == 2
    assert [it.increment for it in r.items] == [1, 1]
    assert r.multipliers == [1, 1]


def test_fractional_total_and_dual():
    # x+y≤0, x−y≤0, −3x≤−2 → 最小总放宽 4/3，λ_3 = 2/3
    cons = [C([1, 1], 0), C([1, -1], 0), C([-3, 0], -2)]
    r = verify_relaxation(["x", "y"], cons, [True, False, False])
    assert r.total == Fraction(4, 3)
    assert r.multipliers[2] == Fraction(2, 3)
    assert [it.increment for it in r.items] == [0, Fraction(4, 3), 0]


def test_lex_three_stable_coordinates():
    cons = [C([1, 1], 0), C([1, -1], 0), C([-2, 0], -1)]
    r = verify_relaxation(["x", "y"], cons, [True, True, True])
    # 字典序 (d_0,d_1,d_2) 最小 → (0,0,1)
    assert [it.increment for it in r.items] == [0, 0, 1]
    assert r.lex_order == [0, 1, 2]


def test_lex_picks_by_flag_not_index():
    cons = [C([1, 1], 0), C([1, -1], 0), C([-2, 0], -1)]
    r = verify_relaxation(["x", "y"], cons, [False, False, True])
    # 只有 d_2 进入字典序 → 负担全部挪到 d_0
    assert [it.increment for it in r.items] == [1, 0, 0]


def test_two_independent_gaps():
    cons = [
        C([1, 0], 0), C([-1, 0], -1),
        C([0, 1], 0), C([0, -1], -2),
    ]
    r = verify_relaxation(["x", "y"], cons, [True, False, False, True])
    assert r.total == 3
    # 稳定序 [0,3]：d_0 最小为 0（缺口由 d_1=1 承担），d_3 最小为 0（d_2=2）
    assert [it.increment for it in r.items] == [0, 1, 2, 0]


def test_redundant_rows_dual_zero():
    cons = [C([0], 0), C([1], 0), C([-1], -1), C([0], 0)]
    r = verify_relaxation(["x"], cons, [False, True, False, False])
    assert r.multipliers[0] == 0 and r.multipliers[3] == 0
    assert [it.increment for it in r.items] == [0, 0, 1, 0]


def test_huge_integers_exact():
    z = 10**60
    cons = [C([z], z), C([-z], -2 * z)]
    r = verify_relaxation(["x"], cons, [True, False])
    assert r.total == z
    assert [it.increment for it in r.items] == [0, z]
    assert r.currents == {"x": 1}


def test_labels_preserved():
    r = relax(["x"], [C([0], -1, "零行矛盾")], [False])
    assert r.items[0].label == "零行矛盾"
    assert r.dual_terms[0].label == "零行矛盾"


def test_bad_dimensions_rejected():
    with pytest.raises(ValueError):
        relax([], [C([0], -1)], [False])
    with pytest.raises(ValueError):
        relax(["x"], [C([0], -1)], [])


# ---- FM 独立复核字典序最小性（逐坐标读出精确最小值） ----
def test_fm_independently_confirms_lex_minimum():
    variables = ["x", "y"]
    cons = [C([1, 1], 0), C([1, -1], 0), C([-2, 0], -1)]
    flags = (True, False, True)
    r = verify_relaxation(variables, cons, flags)
    d = [it.increment for it in r.items]

    fixed: dict[int, Fraction] = {}
    for k in r.lex_order:
        lo = fm_min_coordinate(cons, r.total, fixed, k)
        assert lo == d[k], f"稳定坐标 {k}: FM 最小 {lo} ≠ 求解器 {d[k]}"
        fixed[k] = d[k]
    assert fm_min_total(cons) == r.total


# ---- 随机小系统交叉验证 ----
@pytest.mark.parametrize("seed", range(40))
def test_fuzz_primal_dual_and_lex(seed):
    rng = random.Random(seed)
    n = rng.randint(1, 3)
    m = rng.randint(2, 6)
    cons = []
    for _ in range(m):
        coeffs = tuple(rng.randint(-2, 2) for _ in range(n))
        cons.append(C(coeffs, rng.randint(-3, 3)))
    flags = tuple(rng.random() < 0.5 for _ in range(m))
    # FM 先独立判定原系统是否无解；只对无解系统放宽
    A0 = [list(c.coeffs) for c in cons]
    b0 = [c.b for c in cons]
    if fm_feasible(A0, b0):
        pytest.skip("随机算例恰好可行")

    names = [f"x{j}" for j in range(n)]
    r = verify_relaxation(names, cons, flags)
    d = [it.increment for it in r.items]

    # FM 严格复核稳定序字典序最小性与总放宽量
    fixed: dict[int, Fraction] = {}
    for k in r.lex_order:
        lo = fm_min_coordinate(cons, r.total, fixed, k)
        assert lo == d[k], (seed, k, lo, d[k])
        fixed[k] = d[k]
    assert fm_min_total(cons) == r.total

