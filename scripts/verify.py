"""verify 单次容器入口。

依次完成:
  1. 代码测试 (pytest)；
  2. HTTP 冒烟 (健康检查 / 页面 / 未知编号 404)；
  3. 用“相加得 0 <= -1”的约束组经真实 HTTP 提交核对不可行证书；
     独立重算 mu >= 0、Σmu*a = 0、Σmu*b < 0，并核对幂等(200)与冲突(409)。

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


def check_repair(audit_id: str, repair_id: str, payload: dict,
                 expect_increments: list[str], expect_total: str,
                 expect_currents: dict | None = None) -> None:
    """经真实 HTTP 接口独立重算矛盾组的最小放宽量与对偶等式。"""
    step(f"最小上界放宽修复: {repair_id}")
    status, body = http(
        "POST", "/api/repairs",
        {"repair_id": repair_id, "audit_id": audit_id},
    )
    assert status == 201, (status, body)
    assert body["kind"] == "min-rhs-relaxation"
    res = body["result"]
    n = len(payload["variables"])
    m = len(payload["constraints"])

    # ---- 独立重算原语可行：a·x ≤ b + d，d ≥ 0，逐约束新余量 ----
    total = Fraction(0)
    increments = []
    currents = {name: Fraction(res["currents"][name])
                for name in payload["variables"]}
    for i, (it, con) in enumerate(zip(res["items"], payload["constraints"])):
        d_i = Fraction(it["increment"])
        assert d_i >= 0, f"放宽量必须非负: d_{i}={d_i}"
        assert it["increment"] == expect_increments[i], (
            f"d_{i} 期望 {expect_increments[i]}，实际 {it['increment']}"
        )
        assert Fraction(it["new_b"]) == Fraction(con["b"]) + d_i
        lhs = sum(
            Fraction(con["coeffs"][j]) * currents[name]
            for j, name in enumerate(payload["variables"])
        )
        residual = Fraction(con["b"]) + d_i - lhs
        assert residual >= 0, f"第 {i} 条放宽后仍不可行"
        assert Fraction(it["residual"]) == residual
        total += d_i
        increments.append(d_i)
    assert str(total) == expect_total, f"总放宽量期望 {expect_total}，实际 {total}"
    assert res["total_relaxation"] == expect_total
    if expect_currents:
        for k, v in expect_currents.items():
            assert currents[k] == Fraction(v), (k, currents[k], v)

    # ---- 独立重算有界对偶证书 ----
    lhs = [Fraction(0) for _ in range(n)]
    neg_rhs = Fraction(0)
    for t in res["dual"]["terms"]:
        lam = Fraction(t["multiplier"])
        assert Fraction(0) <= lam <= Fraction(1), f"乘子越界: {lam}"
        assert len(t["weighted_coeffs"]) == n
        assert Fraction(t["weighted_rhs"]) == lam * Fraction(t["b"])
        for j in range(n):
            lhs[j] += Fraction(t["weighted_coeffs"][j])
        neg_rhs -= lam * Fraction(t["b"])
    assert all(v == 0 for v in lhs), f"Σλ·a 必须为 0: {lhs}"
    assert neg_rhs == total, (
        f"对偶等式 −Σλ·b = {neg_rhs} 必须等于最小放宽总量 {total}"
    )
    assert res["dual"]["neg_weighted_rhs"] == str(total)
    assert res["dual"]["weighted_lhs"] == ["0"] * n
    # 稳定标识序 = stable=True 约束的原顺序
    assert [e["index"] for e in res["lex_order"]] == [
        i for i, c in enumerate(payload["constraints"]) if c.get("stable")
    ]
    # 快照冻结来源的变量/约束顺序/原证据
    src = body["source"]
    assert src["variables"] == payload["variables"]
    assert [c["b"] for c in src["constraint_order"]] == [
        c["b"] for c in payload["constraints"]
    ]
    assert src["original_evidence"]["status"] == "infeasible"
    print(f"[{repair_id}] 放宽 {increments} (Σ={total})，"
          f"−Σλ·b={neg_rhs}，λ∈[0,1]、Σλ·a=0 全部独立核验通过")

    # 幂等重放 -> 200 同一冻结修复
    status2, body2 = http(
        "POST", "/api/repairs",
        {"repair_id": repair_id, "audit_id": audit_id},
    )
    assert status2 == 200 and body2["replayed"] is True
    assert body2["created_at"] == body["created_at"]
    assert body2["result"] == res
    print(f"[{repair_id}] 重放返回同一冻结修复 (200)")

    # 同标识改换来源 -> 409，已冻结修复与原审计均不变
    other = "verify-zero-minus-one" if audit_id != "verify-zero-minus-one" \
        else "verify-combo"
    status3, body3 = http(
        "POST", "/api/repairs",
        {"repair_id": repair_id, "audit_id": other},
    )
    assert status3 == 409 and body3["error"] == "repair_id_conflict", body3
    status4, body4 = http("GET", f"/api/repairs/{repair_id}")
    assert status4 == 200
    assert body4["source"]["audit_id"] == audit_id
    assert body4["created_at"] == body["created_at"]
    print(f"[{repair_id}] 改换来源冲突 409，已冻结修复保持不变")


def check_repair_rejections() -> None:
    step("放宽修复的拒绝语义")
    # 来源不存在 -> 404，不落盘
    status, body = http(
        "POST", "/api/repairs",
        {"repair_id": "verify-repair-ghost", "audit_id": "no-such-audit"},
    )
    assert status == 404 and body["error"] == "source_not_found", body
    assert http("GET", "/api/repairs/verify-repair-ghost")[0] == 404
    # 来源并非无解 -> 409，不落盘，原审计不变
    status, body = http(
        "POST", "/api/repairs",
        {"repair_id": "verify-repair-feas", "audit_id": "verify-feasible"},
    )
    assert status == 409 and body["error"] == "source_not_infeasible", body
    assert http("GET", "/api/repairs/verify-repair-feas")[0] == 404
    status, body = http("GET", "/api/audits/verify-feasible")
    assert status == 200 and body["result"]["status"] == "feasible"
    print("来源不存在 404 / 来源可行 409 / 原审计不变 全部符合预期")


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

    step("经真实接口重算矛盾组的最小上界放宽与有界对偶等式")
    # 组 1: 0 <= -1（唯一稳定行）→ d = (1)，Σd = 1，λ = 1
    check_repair(
        "verify-zero-minus-one", "verify-repair-zero",
        {
            "audit_id": "verify-zero-minus-one",
            "variables": ["I1"],
            "constraints": [
                {"coeffs": [0], "b": -1, "stable": True}
            ],
        },
        expect_increments=["1"],
        expect_total="1",
        expect_currents={"I1": "0"},
    )
    # 组 2: x<=0（非稳定）与 -x<=-1（稳定）→ 字典序把 d_稳定 压到 0：
    # d = (1, 0)，x = 1，Σd = 1，λ = (1, 1)
    check_repair(
        "verify-combo", "verify-repair-combo",
        {
            "audit_id": "verify-combo",
            "variables": ["I1", "I2"],
            "constraints": [
                {"coeffs": [1, 0], "b": 0, "stable": False},
                {"coeffs": [-1, 0], "b": -1, "stable": True},
            ],
        },
        expect_increments=["1", "0"],
        expect_total="1",
        expect_currents={"I1": "1", "I2": "0"},
    )

    # 组 3（分数）：x+y<=0, x-y<=0, -3x<=-2 → Σd = 4/3，
    # 稳定序只含第 1 行：d = (0, 4/3, 0)，x=2/3,y=-2/3，λ = (1,1,2/3)
    step("分数放宽量与分数对偶乘子")
    frac_payload = {
        "audit_id": "verify-frac",
        "variables": ["I1", "I2"],
        "constraints": [
            {"coeffs": [1, 1], "b": 0, "stable": True},
            {"coeffs": [1, -1], "b": 0, "stable": False},
            {"coeffs": [-3, 0], "b": -2, "stable": False},
        ],
    }
    status, body = http("POST", "/api/audits", frac_payload)
    assert status == 201 and body["result"]["status"] == "infeasible"
    check_repair(
        "verify-frac", "verify-repair-frac", frac_payload,
        expect_increments=["0", "4/3", "0"],
        expect_total="4/3",
        expect_currents={"I1": "2/3", "I2": "-2/3"},
    )
    check_repair_rejections()

    print("\nVERIFY OK: 代码测试、镜像运行与 HTTP 冒烟、精确证书全部通过", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"VERIFY FAILED: {exc}", flush=True)
        sys.exit(1)
