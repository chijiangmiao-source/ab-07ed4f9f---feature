"""最小上界放宽审计测试：

* 放宽后系统可行、逐约束新余量非负且精确，放宽量非负；
* 有界对偶证书逐项满足 0≤μ≤1、Σμ·a=0、−Σμ·b=Σd（独立重算）；
* 小算例上枚举细密有理网格上的全部放宽向量（FM 消元独立判可行），
  独立验证“先最小化总和、再按稳定约束标识序字典序最小”；
* 随机无解小系统上交叉核验；
* 无稳定约束时仅最小化总和；大整数任意精度；字典序平局被正确打破。
"""

from __future__ import annotations

import itertools
import random
from fractions import Fraction

import pytest

from app.models import verify_relaxation_certificate
from app.relaxation import solve_relaxation
from app.simplex import Constraint
from app.simplex import solve as solve_feasibility

from fourier_motzkin import fm_feasible


def C(coeffs, b, label=None):
    return Constraint(tuple(coeffs), b, label=label)


def assert_repair(names, cons, stables):
    # 前置：来源必须无解
    assert solve_feasibility(names, cons).__class__.__name__ == "InfeasibleResult"

    r = solve_relaxation(names, cons, stables)
    m, n = len(cons), len(names)
    d = [row.relaxation for row in r.rows]
    assert all(di >= 0 for di in d)
    # 放宽后可行 + 新余量精确
    for i, con in enumerate(cons):
        ax = sum(con.coeffs[j] * r.currents[names[j]] for j in range(n))
        assert ax <= con.b + d[i]
        assert r.rows[i].new_b == con.b + d[i]
        assert r.rows[i].new_residual == con.b + d[i] - ax
        assert r.rows[i].stable == stables[i]
    assert sum(d) == r.total_relaxation
    # 对偶证书逐项核验
    assert len(r.multipliers) == m
    for i, mu in enumerate(r.multipliers):
        assert 0 <= mu <= 1
        t = r.dual_terms[i]
        assert t.multiplier == mu
        assert list(t.weighted_coeffs) == [mu * a for a in cons[i].coeffs]
        assert t.weighted_rhs == mu * cons[i].b
        assert t.neg_weighted_rhs == -mu * cons[i].b
    for j in range(n):
        assert r.dual_lhs[j] == sum(
            r.multipliers[i] * cons[i].coeffs[j] for i in range(m)
        ) == 0
    rhs_sum = sum(r.multipliers[i] * cons[i].b for i in range(m))
    assert rhs_sum == r.rhs_weighted_sum
    assert -rhs_sum == r.total_relaxation
    # 稳定序 = stable=True 的约束按原索引升序
    assert r.stable_order == [i for i, s in enumerate(stables) if s]
    # 独立证书核验（与求解器无关，仅按记录数据重算）
    payload = {
        "variables": list(names),
        "constraints": [
            {
                "coeffs": list(con.coeffs),
                "b": con.b,
                "label": con.label,
                "stable": stables[i],
            }
            for i, con in enumerate(cons)
        ],
    }
    verify_relaxation_certificate(payload, r.as_dict())
    return r


def fm_relaxed_feasible(cons, d) -> bool:
    """FM 判定 a·x <= b+d 是否可行（与单纯形独立的算法）。"""
    return fm_feasible(
        [list(con.coeffs) for con in cons],
        [Fraction(con.b) + d[i] for i, con in enumerate(cons)],
    )


def assert_lex_optimal_by_enumeration(cons, stables, r, denom=6):
    """枚举分母为 denom、非负分量的放宽向量独立验证两级最优。

    * 最优解 d* 自身可行（FM）；
    * 网格上不存在总和 < t* 的可行向量；
    * 总和 = t* 的可行向量中，不存在稳定子向量严格更小（字典序）者。
    """
    m = len(cons)
    t = r.total_relaxation
    k_hi = denom * (int(t) + 1)
    d_opt = [row.relaxation for row in r.rows]
    stable_order = r.stable_order

    assert fm_relaxed_feasible(cons, d_opt)
    for ks in itertools.product(range(k_hi + 1), repeat=m):
        cand = [Fraction(k, denom) for k in ks]
        s = sum(cand)
        if not fm_relaxed_feasible(cons, cand):
            continue
        assert s >= t, f"存在总和更小的可行放宽: {cand} 和 {s} < {t}"
        if s == t and stable_order:
            key = tuple(cand[i] for i in stable_order)
            opt_key = tuple(d_opt[i] for i in stable_order)
            assert not (key < opt_key), (
                f"存在字典序更优的可行放宽: {cand} (稳定序 {stable_order})"
            )


def test_zero_leq_minus_one_single_row():
    # 0 <= -1：唯一修法 d=1，μ=1
    r = assert_repair(["x"], [C([0], -1)], [True])
    assert [x.relaxation for x in r.rows] == [1]
    assert r.multipliers == [1]
    assert r.total_relaxation == 1
    assert r.rows[0].new_b == 0 and r.rows[0].new_residual == 0


def test_interval_gap_relaxation_sum():
    # x<=0, -x<=-1（即 x>=1）：总缺口 1
    r = assert_repair(["x"], [C([1], 0), C([-1], -1)], [False, False])
    assert r.total_relaxation == 1
    assert r.multipliers == [1, 1]
    assert r.stable_order == []


def test_lex_prefers_relaxing_nonstable():
    # 仅第 1 条稳定：平局时把放宽推给非稳定约束
    cons = [C([1], 0), C([-1], -1)]
    r = assert_repair(["x"], cons, [True, False])
    assert r.total_relaxation == 1
    assert [x.relaxation for x in r.rows] == [0, 1]
    assert_lex_optimal_by_enumeration(cons, [True, False], r)


def test_lex_both_stable_picks_lower_index_zero():
    # 两条都稳定：字典序要求 d_0 最小 -> (0, 1)
    cons = [C([1], 0), C([-1], -1)]
    r = assert_repair(["x"], cons, [True, True])
    assert [x.relaxation for x in r.rows] == [0, 1]
    assert_lex_optimal_by_enumeration(cons, [True, True], r)


def test_lex_order_follows_constraint_index_not_flag_order():
    # 仅索引 1 稳定：只对 d_1 做字典序
    cons = [C([1, 0], 1), C([0, 1], 1), C([-1, -1], -3)]
    r = assert_repair(["x", "y"], cons, [False, True, False])
    assert r.total_relaxation == 1
    assert r.rows[1].relaxation == 0
    assert_lex_optimal_by_enumeration(cons, [False, True, False], r)


def test_lex_all_three_stable():
    cons = [C([1, 0], 1), C([0, 1], 1), C([-1, -1], -3)]
    r = assert_repair(["x", "y"], cons, [True, True, True])
    # 最优面 d0+d1+d2=1；全稳定字典序 -> (0,0,1)
    assert [x.relaxation for x in r.rows] == [0, 0, 1]
    assert_lex_optimal_by_enumeration(cons, [True, True, True], r)


def test_equality_gap_two():
    # x<=-1 与 x>=1：缺口 2
    cons = [C([1], -1), C([-1], -1)]
    r = assert_repair(["x"], cons, [True, False])
    assert r.total_relaxation == 2
    assert [x.relaxation for x in r.rows] == [0, 2]
    assert r.currents["x"] == -1
    assert_lex_optimal_by_enumeration(cons, [True, False], r)


def test_fractional_relaxation_and_multiplier():
    # x<=0 与 -2x<=-1（x>=1/2）：2d_0+d_2>=1，最小和 t*=1/2，
    # 全稳定字典序下 d_0=1/2、d_1=0；对偶 μ=(1, 1/2) 严格落在界内
    cons = [C([1], 0), C([-2], -1)]
    r = assert_repair(["x"], cons, [True, True])
    assert r.total_relaxation == Fraction(1, 2)
    assert [x.relaxation for x in r.rows] == [Fraction(1, 2), 0]
    assert r.multipliers == [1, Fraction(1, 2)]
    assert_lex_optimal_by_enumeration(cons, [True, True], r)


def test_fractional_relaxation_three_halves():
    # 2x<=-1（x<=-1/2）与 -x<=-1（x>=1）：d_0+2d_1>=3，
    # t*=3/2，全稳定字典序 d=(0,3/2)，μ=(1/2,1)
    cons = [C([2], -1), C([-1], -1)]
    r = assert_repair(["x"], cons, [True, True])
    assert r.total_relaxation == Fraction(3, 2)
    assert [x.relaxation for x in r.rows] == [0, Fraction(3, 2)]
    assert r.multipliers == [Fraction(1, 2), 1]
    assert_lex_optimal_by_enumeration(cons, [True, True], r)


def test_four_row_system_total_and_bounds():
    cons = [
        C([1, 1], 0),    # x+y<=0
        C([-1, 0], -1),  # x>=1
        C([0, -1], -1),  # y>=1
        C([-1, -1], -3), # x+y>=3
    ]
    r = assert_repair(["x", "y"], cons, [False, False, False, False])
    assert r.total_relaxation == 3
    assert all(0 <= mu <= 1 for mu in r.multipliers)
    assert -sum(r.multipliers[i] * cons[i].b for i in range(4)) == 3


def test_huge_integers_exact_relaxation():
    z = 10**60
    cons = [C([z], 0), C([-z], -z)]  # x<=0, x>=1
    r = assert_repair(["x"], cons, [True, True])
    assert r.total_relaxation == z
    assert [x.relaxation for x in r.rows] == [0, z]
    assert r.currents["x"] == 0


def test_free_variable_can_be_negative():
    # x <= -5 与 x >= 7：缺口 12；全稳定 (0,12), x=-5
    cons = [C([1], -5), C([-1], -7)]
    r = assert_repair(["x"], cons, [True, True])
    assert [x.relaxation for x in r.rows] == [0, 12]
    assert r.currents["x"] == -5


@pytest.mark.parametrize("seed", range(60))
def test_fuzz_infeasible_systems_certificate(seed):
    """随机无解小系统：放宽解可行（FM 独立判定）、证书成立；小者再枚举。

    无解性由“同一线性式的上界/下界相冲突”保证，再追加随机约束。
    """
    rng = random.Random(2000 + seed)
    n = rng.randint(1, 3)
    # 矛盾对: p·x <= b_hi 与 -p·x <= -b_lo，取 b_hi < b_lo
    p = [rng.randint(-2, 2) for _ in range(n)]
    if all(v == 0 for v in p):
        p[0] = 1
    b_hi = rng.randint(-2, 2)
    b_lo = b_hi + rng.randint(1, 3)
    A = [list(p), [-v for v in p]]
    b = [b_hi, -b_lo]
    extra = rng.randint(0, 3)
    for _ in range(extra):
        A.append([rng.randint(-2, 2) for _ in range(n)])
        b.append(rng.randint(-3, 3))
    m = len(A)
    assert not fm_feasible(A, b), "构造出的系统必须无解"
    names = [f"x{j}" for j in range(n)]
    cons = [C(row, rhs) for row, rhs in zip(A, b)]
    stables = [rng.random() < 0.5 for _ in range(m)]
    r = assert_repair(names, cons, stables)
    d = [row.relaxation for row in r.rows]
    assert fm_relaxed_feasible(cons, d)
    if m <= 3 and r.total_relaxation <= 3 and any(stables):
        assert_lex_optimal_by_enumeration(cons, stables, r, denom=4)


@pytest.mark.parametrize(
    "raw,st,expect_t,expect_d",
    [
        # 两条纯矛盾 0<=-1 与 0<=-2：各自独立需要放宽
        (([(0,), (0,)], [-1, -2]), [True, True], 3, [1, 2]),
        # 重复的矛盾行（触发人工列留基/冗余行清理路径）
        (([(1,), (1,), (-1,), (-1,)], [0, 0, -1, -1]),
         [True, False, True, False], 2, [0, 0, 1, 1]),
        # 0<=0 重言式 + 矛盾
        (([(0,), (1,), (-1,)], [0, 0, -1]),
         [True, True, True], 1, [0, 0, 1]),
        # 等式 x=0 与 x>=1，外加无关行 y<=5
        (([(1, 0), (-1, 0), (-1, 0), (0, 1)], [0, 0, -1, 5]),
         [True, True, False, False], 1, [0, 0, 1, 0]),
    ],
)
def test_degenerate_and_duplicate_rows(raw, st, expect_t, expect_d):
    coeffs, bs = raw
    names = [f"x{j}" for j in range(len(coeffs[0]))]
    cons = [C(list(a), b) for a, b in zip(coeffs, bs)]
    r = assert_repair(names, cons, st)
    assert r.total_relaxation == expect_t
    assert [x.relaxation for x in r.rows] == expect_d


def test_as_dict_strings_exact():
    import json
    r = assert_repair(["x"], [C([1], 0), C([-1], -1)], [True, False])
    d = r.as_dict()
    s = json.dumps(d, ensure_ascii=False)
    assert d["status"] == "repaired"
    assert d["total_relaxation"] == "1"
    assert d["dual"]["bounds"] == "0 <= multiplier <= 1"
    assert all(isinstance(v, str) for v in d["dual"]["lhs_sum"])
    assert "1" in s
