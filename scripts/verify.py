"""verify 单次容器入口。

依次完成:
  1. 代码测试 (pytest)；
  2. HTTP 冒烟 (健康检查 / 页面 / 未知编号 404)；
  3. 用“相加得 0 <= -1”的约束组经真实 HTTP 提交核对不可行证书；
     独立重算 mu >= 0、Σmu*a = 0、Σmu*b < 0，并核对幂等(200)与冲突(409)；
  4. 对无解来源经真实 HTTP 发起最小上界放宽审计，独立重算
     d>=0、放宽后可行（新余量非负）、0<=μ<=1、Σμ*a=0、-Σμ*b=Σd，
     并核对稳定序字典序、幂等(200)、改换来源(409)与按编号重读。

任一步失败立即以非零退出码退出；全部成功退出 0。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from fractions import Fraction

WEB_URL = os.environ.get("WEB_URL", "http://web:8080")
TIMEOUT_S = float(os.environ.get("VERIFY_TIMEOUT_S", "30"))


def step(title: str):
    print(f"\n=== verify: {title} ===", flush=True)


def http(method: str, path: str, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(WEB_URL + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def wait_healthy() -> None:
    step("等待 web 健康")
    deadline = time.time() + TIMEOUT_S
    last = None
    while time.time() < deadline:
        try:
            status, body = http("GET", "/healthz")
            if status == 200 and body.get("status") == "ok":
                print("healthz OK:", body)
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.5)
    raise SystemExit(f"web 在 {TIMEOUT_S}s 内未就绪: {last}")


def run_pytest() -> None:
    step("代码测试 pytest")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=os.environ.get("VERIFY_SRC_DIR", "/srv"),
    )
    if proc.returncode != 0:
        raise SystemExit(f"pytest 失败，退出码 {proc.returncode}")


def http_smoke() -> None:
    step("HTTP 冒烟")
    status, body = http("GET", "/healthz")
    assert status == 200 and body["status"] == "ok", body

    with urllib.request.urlopen(WEB_URL + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    assert resp.status == 200 and "单纯形" in html

    status, _ = http("GET", "/api/audits/no-such-id-verify")
    assert status == 404, f"未知编号应为 404，实际 {status}"
    print("健康检查 / 页面 / 404 全部符合预期")


def check_certificate(audit_id: str, payload: dict, expect_rhs: str) -> dict:
    status, body = http("POST", "/api/audits", payload)
    assert status == 201, (status, body)
    res = body["result"]
    assert res["status"] == "infeasible", res

    terms = res["terms"]
    n = len(payload["variables"])
    lhs = [Fraction(0) for _ in range(n)]
    rhs = Fraction(0)
    for t in terms:
        mu = Fraction(t["multiplier"])
        assert mu >= 0, "乘子必须非负"
        assert len(t["weighted_coeffs"]) == n
        assert Fraction(t["weighted_rhs"]) == mu * Fraction(t["b"])
        for j in range(n):
            lhs[j] += Fraction(t["weighted_coeffs"][j])
        rhs += Fraction(t["weighted_rhs"])
    assert all(v == 0 for v in lhs), f"左侧合并必须全为 0: {lhs}"
    assert rhs < 0, f"右侧合并必须严格为负: {rhs}"
    assert res["combined_rhs"] == expect_rhs == str(rhs)
    assert res["combined_relation"] == f"0 <= {expect_rhs}"
    print(f"[{audit_id}] 证书核验通过: mu>=0, Σmu·a=0, Σmu·b={rhs}")

    # 幂等：完全相同载荷重放 -> 200 同一记录
    status2, body2 = http("POST", "/api/audits",
                          json.loads(json.dumps(payload)))
    assert status2 == 200 and body2["replayed"] is True
    assert body2["fingerprint"] == body["fingerprint"]
    assert body2["created_at"] == body["created_at"]
    print(f"[{audit_id}] 重放返回同一冻结记录 (200)")

    # 冲突：同编号改载荷 -> 409，原证据不变
    changed = json.loads(json.dumps(payload))
    changed["constraints"][0]["b"] = changed["constraints"][0]["b"] + 1
    status3, body3 = http("POST", "/api/audits", changed)
    assert status3 == 409, (status3, body3)
    status4, body4 = http("GET", f"/api/audits/{audit_id}")
    assert status4 == 200 and body4["result"]["combined_rhs"] == expect_rhs
    print(f"[{audit_id}] 改动载荷冲突 409，原证据保持不变")
    return res


def check_relaxation(source_id: str, payload: dict, repair_id: str,
                     expect_total: str, expect_d: list[str],
                     expect_stable_order: list[int]) -> dict:
    """经真实 HTTP 对无解来源发起放宽审计，并独立重算全部证书条件。"""
    # 来源必须已冻结且无解
    s0, src = http("GET", f"/api/audits/{source_id}")
    assert s0 == 200 and src["result"]["status"] == "infeasible"
    src_fp = src["fingerprint"]
    src_relation = src["result"]["combined_relation"]

    status, body = http(
        "POST", "/api/repairs",
        {"repair_id": repair_id, "source_audit_id": source_id},
    )
    assert status == 201, (status, body)
    assert body["replayed"] is False
    assert body["source_audit_id"] == source_id
    assert body["source_fingerprint"] == src_fp
    # 冻结快照保留原证据且不改写来源结论
    snap = body["source"]
    assert snap["evidence"]["combined_relation"] == src_relation
    assert [c["index"] for c in snap["constraints"]] == list(
        range(len(payload["constraints"]))
    )

    res = body["result"]
    assert res["status"] == "repaired"
    assert res["stable_order"] == expect_stable_order
    cons = payload["constraints"]
    n = len(payload["variables"])

    # ---- 独立重算: 放宽量非负、放宽后可行、新余量精确 ----
    currents = {k: Fraction(v) for k, v in res["currents"].items()}
    total = Fraction(0)
    for i, row in enumerate(res["constraints"]):
        d = Fraction(row["relaxation"])
        assert d >= 0, f"放宽量 d_{i} 必须非负"
        assert row["relaxation"] == expect_d[i]
        total += d
        b = Fraction(cons[i]["b"])
        assert Fraction(row["new_b"]) == b + d
        ax = sum(
            Fraction(cons[i]["coeffs"][j]) * currents[payload["variables"][j]]
            for j in range(n)
        )
        residual = b + d - ax
        assert residual >= 0, f"约束 {i} 放宽后仍不可行"
        assert Fraction(row["new_residual"]) == residual
    assert total == Fraction(expect_total) == Fraction(res["total_relaxation"])

    # ---- 独立重算有界对偶: 0<=μ<=1、Σμ·a=0、-Σμ·b = 最小放宽总量 ----
    dual = res["dual"]
    mus = [Fraction(x["value"]) for x in dual["multipliers"]]
    assert len(mus) == len(cons)
    lhs = [Fraction(0) for _ in range(n)]
    rhs = Fraction(0)
    for i, term in enumerate(dual["terms"]):
        mu = mus[i]
        assert Fraction(term["multiplier"]) == mu
        assert 0 <= mu <= 1, f"μ_{i}={mu} 越出 [0,1]"
        w = [Fraction(s) for s in term["weighted_coeffs"]]
        assert w == [mu * Fraction(a) for a in cons[i]["coeffs"]]
        assert Fraction(term["weighted_rhs"]) == mu * Fraction(cons[i]["b"])
        assert Fraction(term["neg_weighted_rhs"]) == -mu * Fraction(cons[i]["b"])
        for j in range(n):
            lhs[j] += w[j]
        rhs += mu * Fraction(cons[i]["b"])
    assert all(v == 0 for v in lhs), f"Σμ·a 必须为 0: {lhs}"
    assert [Fraction(s) for s in dual["lhs_sum"]] == lhs
    assert Fraction(dual["rhs_sum"]) == rhs
    assert Fraction(dual["neg_rhs_sum"]) == -rhs == total
    print(f"[{repair_id}] 放宽证书核验通过: d={expect_d}, Σd={total}, "
          f"0≤μ≤1, Σμ·a=0, −Σμ·b={-rhs}")

    # 幂等重放 -> 200 同一冻结记录
    status2, body2 = http(
        "POST", "/api/repairs",
        {"repair_id": repair_id, "source_audit_id": source_id},
    )
    assert status2 == 200 and body2["replayed"] is True
    assert body2["fingerprint"] == body["fingerprint"]
    assert body2["created_at"] == body["created_at"]

    # 按编号重读
    status3, body3 = http("GET", f"/api/repairs/{repair_id}")
    assert status3 == 200 and body3["result"] == res

    # 来源原审计保持不变
    status4, body4 = http("GET", f"/api/audits/{source_id}")
    assert status4 == 200 and body4["fingerprint"] == src_fp
    print(f"[{repair_id}] 重放 200 / 按编号重读 / 来源原证据不变 全部通过")
    return res


def check_repair_rejection() -> None:
    # 来源不存在 -> 404，不落盘
    status, body = http(
        "POST", "/api/repairs",
        {"repair_id": "verify-fix-missing", "source_audit_id": "no-such-source"},
    )
    assert status == 404 and body["error"] == "source_not_found", (status, body)
    assert http("GET", "/api/repairs/verify-fix-missing")[0] == 404

    # 可行来源 -> 409 source_not_infeasible
    status, body = http(
        "POST", "/api/repairs",
        {"repair_id": "verify-fix-feas", "source_audit_id": "verify-feasible"},
    )
    assert status == 409 and body["error"] == "source_not_infeasible", (status, body)
    assert http("GET", "/api/repairs/verify-fix-feas")[0] == 404

    # 同一修复标识改换来源 -> 409，已冻结修复不变
    status, body = http(
        "POST", "/api/repairs",
        {"repair_id": "verify-fix-half", "source_audit_id": "verify-combo"},
    )
    assert status == 409 and body["error"] == "repair_id_conflict", (status, body)
    assert body["bound_source_audit_id"] == "verify-fix-half-src"
    assert body["requested_source_audit_id"] == "verify-combo"
    status, body = http("GET", "/api/repairs/verify-fix-half")
    assert status == 200 and body["source_audit_id"] == "verify-fix-half-src"
    print("来源缺失 404 / 可行来源 409 / 改换来源 409 且已冻结修复不变")


def main() -> int:
    run_pytest()
    wait_healthy()
    http_smoke()

    step("用相加得 0 <= -1 的约束组核对证书")
    # 组 1: 单条 0·x <= -1，乘子 1，直接合并为 0 <= -1
    check_certificate(
        "verify-zero-minus-one",
        {
            "audit_id": "verify-zero-minus-one",
            "variables": ["I1"],
            "constraints": [
                {"coeffs": [0], "b": -1, "stable": True}
            ],
        },
        "-1",
    )
    # 组 2: x <= 0 与 -x <= -1，乘子 1+1 合并为 0 <= -1
    check_certificate(
        "verify-combo",
        {
            "audit_id": "verify-combo",
            "variables": ["I1", "I2"],
            "constraints": [
                {"coeffs": [1, 0], "b": 0, "stable": False},
                {"coeffs": [-1, 0], "b": -1, "stable": True},
            ],
        },
        "-1",
    )

    step("可行系统冒烟: 1 <= x <= 2")
    p = {
        "audit_id": "verify-feasible",
        "variables": ["I1"],
        "constraints": [
            {"coeffs": [1], "b": 2},
            {"coeffs": [-1], "b": -1},
        ],
    }
    status, body = http("POST", "/api/audits", p)
    assert status == 201 and body["result"]["status"] == "feasible"
    x = Fraction(body["result"]["currents"]["I1"])
    assert 1 <= x <= 2
    for m in body["result"]["margins"]:
        assert Fraction(m["residual"]) >= 0
    print("可行解与精确余量核验通过")

    step("经真实接口重算矛盾组的最小放宽量与有界对偶等式")
    # 矛盾组 A（分数修复量）: x<=0 与 -2x<=-1（x>=1/2），第 1 条稳定。
    # 最小放宽总量 t*=1/2；稳定序 [0] 字典序下 d=(1/2, 0)；μ=(1, 1/2)。
    half_src = {
        "audit_id": "verify-fix-half-src",
        "variables": ["I1"],
        "constraints": [
            {"coeffs": [1], "b": 0, "stable": True},
            {"coeffs": [-2], "b": -1, "stable": False},
        ],
    }
    status, body = http("POST", "/api/audits", half_src)
    assert status == 201 and body["result"]["status"] == "infeasible"
    check_relaxation(
        "verify-fix-half-src", half_src, "verify-fix-half",
        expect_total="1/2", expect_d=["1/2", "0"],
        expect_stable_order=[0],
    )

    # 矛盾组 B（上面的 verify-combo: x<=0, -x<=-1），仅第 2 条稳定。
    # t*=1；稳定序 [1] 字典序下 d_1=0，放宽全部落在非稳定第 1 条: d=(1,0)。
    combo_src = {
        "audit_id": "verify-combo",
        "variables": ["I1", "I2"],
        "constraints": [
            {"coeffs": [1, 0], "b": 0, "stable": False},
            {"coeffs": [-1, 0], "b": -1, "stable": True},
        ],
    }
    check_relaxation(
        "verify-combo", combo_src, "verify-combo-fix",
        expect_total="1", expect_d=["1", "0"],
        expect_stable_order=[1],
    )

    step("放宽审计拒绝语义：来源缺失 / 并非无解 / 改换来源")
    check_repair_rejection()

    print("\nVERIFY OK: 代码测试、镜像运行与 HTTP 冒烟、精确证书与最小放宽对偶全部通过",
          flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"VERIFY FAILED: {exc}", flush=True)
        sys.exit(1)
