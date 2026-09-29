"""放宽审计 HTTP 端到端：真实套接字上的状态码、幂等/冲突/拒绝语义、
证书独立重算、页面入口与按编号重读。"""

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


INF = {
    "audit_id": "src-1",
    "variables": ["I1", "I2"],
    "constraints": [
        {"coeffs": [1, 0], "b": 0, "stable": True, "label": "上界"},
        {"coeffs": [-2, 0], "b": -1, "stable": False, "label": "下界"},
    ],
}  # x<=0, x>=1/2 矛盾；t*=1/2, lex 全稳定第一行 d0=1/2


def make_source(server, payload=INF):
    s, b = req(server, "POST", "/api/audits", payload)
    assert s == 201 and b["result"]["status"] == "infeasible"
    return b


def post_repair(server, repair_id="fix-1", source_id="src-1"):
    return req(server, "POST", "/api/repairs",
               {"repair_id": repair_id, "source_audit_id": source_id})


def independently_verify(server, body, payload):
    res = body["result"]
    cons = payload["constraints"]
    m, n = len(cons), len(payload["variables"])

    currents = {k: Fraction(v) for k, v in res["currents"].items()}
    total = Fraction(0)
    for i, row in enumerate(res["constraints"]):
        d = Fraction(row["relaxation"])
        assert d >= 0
        total += d
        b = Fraction(cons[i]["b"])
        assert Fraction(row["new_b"]) == b + d
        ax = sum(Fraction(cons[i]["coeffs"][j]) * currents[payload["variables"][j]]
                 for j in range(n))
        assert b + d - ax == Fraction(row["new_residual"]) >= 0
    assert total == Fraction(res["total_relaxation"])

    mus = [Fraction(x["value"]) for x in res["dual"]["multipliers"]]
    lhs = [Fraction(0) for _ in range(n)]
    rhs = Fraction(0)
    for i, term in enumerate(res["dual"]["terms"]):
        mu = mus[i]
        assert Fraction(term["multiplier"]) == mu
        assert 0 <= mu <= 1
        for j in range(n):
            lhs[j] += Fraction(term["weighted_coeffs"][j])
            assert Fraction(term["weighted_coeffs"][j]) == \
                mu * Fraction(cons[i]["coeffs"][j])
        assert Fraction(term["weighted_rhs"]) == mu * Fraction(cons[i]["b"])
        rhs += Fraction(term["weighted_rhs"])
    assert all(v == 0 for v in lhs)
    assert Fraction(res["dual"]["rhs_sum"]) == rhs
    assert Fraction(res["dual"]["neg_rhs_sum"]) == -rhs == total


def test_repair_from_infeasible_source(server):
    make_source(server)
    s, body = post_repair(server)
    assert s == 201 and body["replayed"] is False
    assert body["source_audit_id"] == "src-1"
    # 来源 Farkas 证书按 (μ=2,1) 合并为 0 <= -1（证书不归一化，原样冻结）
    assert body["source"]["evidence"]["combined_relation"] == "0 <= -1"
    res = body["result"]
    assert res["status"] == "repaired"
    assert res["total_relaxation"] == "1/2"
    assert res["stable_order"] == [0]
    assert [c["relaxation"] for c in res["constraints"]] == ["1/2", "0"]
    independently_verify(server, body, INF)


def test_repair_replay_is_200_same_record(server):
    make_source(server)
    s, b1 = post_repair(server)
    assert s == 201
    s, b2 = post_repair(server)
    assert s == 200 and b2["replayed"] is True
    assert b2["fingerprint"] == b1["fingerprint"]
    assert b2["created_at"] == b1["created_at"]
    assert b2["result"] == b1["result"]


def test_get_repair_by_id(server):
    make_source(server)
    s, b = post_repair(server)
    s2, body = req(server, "GET", "/api/repairs/fix-1")
    assert s2 == 200 and body["replayed"] is True
    assert body["fingerprint"] == b["fingerprint"]
    assert body["source"]["constraints"][1]["index"] == 1


def test_get_unknown_repair_404(server):
    s, body = req(server, "GET", "/api/repairs/nope")
    assert s == 404 and body["error"] == "not_found"


def test_repair_unknown_source_404(server):
    s, body = post_repair(server, source_id="ghost")
    assert s == 404 and body["error"] == "source_not_found"
    assert req(server, "GET", "/api/repairs/fix-1")[0] == 404


def test_repair_feasible_source_409(server):
    p = {"audit_id": "ok", "variables": ["x"],
         "constraints": [{"coeffs": [1], "b": 1}]}
    req(server, "POST", "/api/audits", p)
    s, body = post_repair(server, source_id="ok")
    assert s == 409 and body["error"] == "source_not_infeasible"
    assert req(server, "GET", "/api/repairs/fix-1")[0] == 404


def test_rebind_repair_id_conflicts_and_preserves(server):
    make_source(server)
    make_source(server, {
        "audit_id": "src-2", "variables": ["x"],
        "constraints": [{"coeffs": [0], "b": -2, "stable": True}],
    })
    s, b1 = post_repair(server)
    assert s == 201
    s, body = req(server, "POST", "/api/repairs",
                  {"repair_id": "fix-1", "source_audit_id": "src-2"})
    assert s == 409 and body["error"] == "repair_id_conflict"
    assert body["bound_source_audit_id"] == "src-1"
    assert body["requested_source_audit_id"] == "src-2"
    # 已冻结修复不变
    s, kept = req(server, "GET", "/api/repairs/fix-1")
    assert kept["source_audit_id"] == "src-1"
    assert kept["result"] == b1["result"]
    # 换成不存在的来源也冲突（而非 404）
    s, body = req(server, "POST", "/api/repairs",
                  {"repair_id": "fix-1", "source_audit_id": "ghost"})
    assert s == 409 and body["error"] == "repair_id_conflict"


def test_invalid_repair_payload_400(server):
    make_source(server)
    for bad in (
        {},
        {"repair_id": "fix-1"},
        {"repair_id": "bad/id", "source_audit_id": "src-1"},
        {"repair_id": 3, "source_audit_id": "src-1"},
        "not json object",
    ):
        s, body = req(server, "POST", "/api/repairs", bad)
        assert s == 400 and body["error"] == "invalid_payload", (bad, s, body)
    assert req(server, "GET", "/api/repairs/fix-1")[0] == 404


def test_source_audit_unchanged(server):
    src = make_source(server)
    post_repair(server)
    s, after = req(server, "GET", "/api/audits/src-1")
    assert s == 200
    assert after["fingerprint"] == src["fingerprint"]
    assert after["result"]["status"] == "infeasible"
    assert after["result"]["combined_rhs"] == src["result"]["combined_rhs"]


def test_page_links_repair_entry_from_infeasible(server):
    with urllib.request.urlopen(server + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    assert resp.status == 200
    assert "最小上界放宽审计" in html
    assert "/api/repairs" in html
    assert "repair_id" in html


def test_bad_repair_path_404(server):
    s, _ = req(server, "POST", "/api/repairs/foo", {})
    assert s == 404
    s, _ = req(server, "GET", "/api/repairs/")
    assert s == 404


def test_repair_with_contradiction_group_over_http(server):
    """verify 容器同款矛盾组：经真实接口重算修复量与对偶等式。"""
    p = {
        "audit_id": "grp",
        "variables": ["I1", "I2"],
        "constraints": [
            {"coeffs": [1, 0], "b": 0, "stable": False},
            {"coeffs": [-1, 0], "b": -1, "stable": True},
        ],
    }
    make_source(server, p)
    s, body = req(server, "POST", "/api/repairs",
                  {"repair_id": "grp-fix", "source_audit_id": "grp"})
    assert s == 201
    assert body["result"]["total_relaxation"] == "1"
    independently_verify(server, body, p)
    # 稳定行（索引 1）字典序：d_1=0，放宽落在非稳定行
    assert [c["relaxation"] for c in body["result"]["constraints"]] == ["1", "0"]
