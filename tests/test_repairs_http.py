"""放宽修复的 HTTP 端到端语义：发起、重读、幂等、拒绝码、来源不变。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from fractions import Fraction

import pytest

from app.web import make_server


@pytest.fixture()
def server(tmp_path):
    httpd = make_server(host="127.0.0.1", port=0,
                        data_dir=str(tmp_path / "data"))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    port = httpd.server_address[1]
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def req(base, method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, data=data, headers=headers,
                               method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


P_INF = {
    "audit_id": "inf-1",
    "variables": ["x", "y"],
    "constraints": [
        {"coeffs": [1, 1], "b": 0, "stable": True, "label": "L1"},
        {"coeffs": [1, -1], "b": 0, "stable": False},
        {"coeffs": [-2, 0], "b": -1, "stable": True},
    ],
}
P_INF2 = {**P_INF, "audit_id": "inf-2"}
P_FEAS = {
    "audit_id": "feas-1",
    "variables": ["x"],
    "constraints": [{"coeffs": [1], "b": 1}],
}


def _create(server, payload):
    status, body = req(server, "POST", "/api/audits", payload)
    assert status == 201, body
    return body


def _verify_body(body):
    res = body["result"]
    assert res["status"] == "relaxed"
    assert body["kind"] == "min-rhs-relaxation"
    m = len(body["source"]["constraint_order"])
    n = len(body["source"]["variables"])

    # 逐约束原语可行 + 新余量
    total = Fraction(0)
    for it in res["items"]:
        d = Fraction(it["increment"])
        assert d >= 0
        assert Fraction(it["new_b"]) == Fraction(it["b"]) + d
        assert Fraction(it["residual"]) >= 0
        total += d
    assert Fraction(res["total_relaxation"]) == total

    # 电流确实满足新约束
    for i, con in enumerate(P_INF["constraints"]):
        lhs = sum(
            Fraction(con["coeffs"][j])
            * Fraction(res["currents"][name])
            for j, name in enumerate(P_INF["variables"])
        )
        assert lhs <= Fraction(res["items"][i]["new_b"])

    # 对偶证书逐项独立重算
    lhs = [Fraction(0) for _ in range(n)]
    neg_rhs = Fraction(0)
    for t in res["dual"]["terms"]:
        lam = Fraction(t["multiplier"])
        assert Fraction(0) <= lam <= Fraction(1)
        assert len(t["weighted_coeffs"]) == n
        assert Fraction(t["weighted_rhs"]) == lam * Fraction(t["b"])
        for j in range(n):
            lhs[j] += Fraction(t["weighted_coeffs"][j])
        neg_rhs -= lam * Fraction(t["b"])
    assert all(v == 0 for v in lhs)
    assert neg_rhs == total
    assert Fraction(res["dual"]["neg_weighted_rhs"]) == total
    assert [
        x["index"] for x in res["lex_order"]
    ] == [i for i, c in enumerate(P_INF["constraints"]) if c.get("stable")]
    return res


def test_repair_create_and_fetch(server):
    _create(server, P_INF)
    status, body = req(server, "POST", "/api/repairs",
                       {"repair_id": "fix-1", "audit_id": "inf-1"})
    assert status == 201, body
    res = _verify_body(body)
    assert body["source"]["audit_id"] == "inf-1"
    # 来源快照冻结了原变量、约束顺序与原证据
    assert body["source"]["variables"] == ["x", "y"]
    assert [c["b"] for c in body["source"]["constraint_order"]] == [0, 0, -1]
    assert body["source"]["original_evidence"]["combined_rhs"] == "-1"
    assert Fraction(res["total_relaxation"]) == 1

    status2, body2 = req(server, "GET", "/api/repairs/fix-1")
    assert status2 == 200 and body2["replayed"] is True
    assert body2["fingerprint"] if "fingerprint" in body2 else True
    assert body2["created_at"] == body["created_at"]
    _verify_body(body2)


def test_repair_replay_identical_is_200(server):
    _create(server, P_INF)
    s1, b1 = req(server, "POST", "/api/repairs",
                 {"repair_id": "fix-1", "audit_id": "inf-1"})
    assert s1 == 201
    s2, b2 = req(server, "POST", "/api/repairs",
                 {"repair_id": "fix-1", "audit_id": "inf-1"})
    assert s2 == 200 and b2["replayed"] is True
    assert b2["created_at"] == b1["created_at"]
    assert b2["result"] == b1["result"]


def test_repair_same_id_different_source_conflicts(server):
    _create(server, P_INF)
    b_inf2 = _create(server, P_INF2)
    s1, _ = req(server, "POST", "/api/repairs",
                {"repair_id": "fix-1", "audit_id": "inf-1"})
    assert s1 == 201
    s2, body = req(server, "POST", "/api/repairs",
                   {"repair_id": "fix-1", "audit_id": "inf-2"})
    assert s2 == 409, body
    assert body["error"] == "repair_id_conflict"
    assert body["bound_audit_id"] == "inf-1"
    # 已冻结修复不变
    s3, b3 = req(server, "GET", "/api/repairs/fix-1")
    assert s3 == 200 and b3["source"]["audit_id"] == "inf-1"
    # 改绑回原来源仍然是幂等重放（不是冲突）
    s4, b4 = req(server, "POST", "/api/repairs",
                 {"repair_id": "fix-1", "audit_id": "inf-1"})
    assert s4 == 200 and b4["created_at"] == b3["created_at"]
    # 第二个来源的原证据也不变
    s5, b5 = req(server, "GET", "/api/audits/inf-2")
    assert s5 == 200 and b5["fingerprint"] == b_inf2["fingerprint"]


def test_repair_source_not_found_404(server):
    status, body = req(server, "POST", "/api/repairs",
                       {"repair_id": "fix-x", "audit_id": "ghost"})
    assert status == 404
    assert body["error"] == "source_not_found"
    assert body["audit_id"] == "ghost"
    assert req(server, "GET", "/api/repairs/fix-x")[0] == 404


def test_repair_feasible_source_conflict(server):
    _create(server, P_FEAS)
    status, body = req(server, "POST", "/api/repairs",
                       {"repair_id": "fix-f", "audit_id": "feas-1"})
    assert status == 409
    assert body["error"] == "source_not_infeasible"
    assert body["source_status"] == "feasible"
    assert req(server, "GET", "/api/repairs/fix-f")[0] == 404
    # 原审计保持可行结论
    s, b = req(server, "GET", "/api/audits/feas-1")
    assert s == 200 and b["result"]["status"] == "feasible"


def test_repair_invalid_payload_400(server):
    _create(server, P_INF)
    for bad in (
        {"repair_id": "", "audit_id": "inf-1"},
        {"audit_id": "inf-1"},
        {"repair_id": "fix", "audit_id": 7},
        "not-json-object",
    ):
        status, body = req(server, "POST", "/api/repairs", bad)
        assert status == 400, (bad, body)
        assert body["error"] == "invalid_payload"


def test_repair_does_not_mutate_source(server):
    src = _create(server, P_INF)
    req(server, "POST", "/api/repairs",
        {"repair_id": "fix-1", "audit_id": "inf-1"})
    s, after = req(server, "GET", "/api/audits/inf-1")
    assert s == 200
    assert after["fingerprint"] == src["fingerprint"]
    assert after["created_at"] == src["created_at"]
    assert after["result"] == src["result"]
    assert "repair" not in json.dumps(after)


def test_repair_unknown_id_404(server):
    assert req(server, "GET", "/api/repairs/nope")[0] == 404


def test_repair_zero_minus_one_minimal(server):
    p = {
        "audit_id": "z-1",
        "variables": ["x"],
        "constraints": [{"coeffs": [0], "b": -1, "stable": True}],
    }
    _create(server, p)
    s, body = req(server, "POST", "/api/repairs",
                  {"repair_id": "fix-z", "audit_id": "z-1"})
    assert s == 201, body
    res = body["result"]
    assert res["total_relaxation"] == "1"
    assert res["items"][0]["increment"] == "1"
    assert res["items"][0]["new_b"] == "0"
    assert res["items"][0]["residual"] == "0"
    assert res["dual"]["multipliers"][0]["value"] == "1"
    assert res["dual"]["relation"].startswith("-sum(lambda_i * b_i) = 1")


def test_replay_after_server_restart(tmp_path):
    data = str(tmp_path / "data")

    def start():
        import app.web as web
        web.Handler.service = None
        httpd = make_server("127.0.0.1", 0, data)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd

    s1 = start()
    base1 = f"http://127.0.0.1:{s1.server_address[1]}"
    req(base1, "POST", "/api/audits", P_INF)
    req(base1, "POST", "/api/repairs",
        {"repair_id": "fix-1", "audit_id": "inf-1"})
    s1.shutdown(); s1.server_close()

    s2 = start()
    base2 = f"http://127.0.0.1:{s2.server_address[1]}"
    status, body = req(base2, "GET", "/api/repairs/fix-1")
    assert status == 200
    assert body["source"]["audit_id"] == "inf-1"
    assert body["result"]["total_relaxation"] == "1"
    s2.shutdown(); s2.server_close()
