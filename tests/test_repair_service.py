"""放宽审计服务与冻结存储：来源校验、幂等重放、改换来源冲突、
来源不存在/并非无解拒绝、并发首提唯一、原审计与已冻结修复不变。"""

from __future__ import annotations

import json
import threading

import pytest

from app.repair_storage import RepairConflictError, RepairStore
from app.service import (
    AuditService,
    SourceNotFoundError,
    SourceNotInfeasibleError,
)
from app.storage import AuditStore


def make_service(tmp_path):
    store = AuditStore(str(tmp_path / "data"))
    repair_store = RepairStore(str(tmp_path / "data"))
    return AuditService(store, repair_store)


INF1 = {
    "audit_id": "src-1",
    "variables": ["x"],
    "constraints": [
        {"coeffs": [1], "b": 0, "stable": True},
        {"coeffs": [-1], "b": -1, "stable": False},
    ],
}
INF2 = {
    "audit_id": "src-2",
    "variables": ["x"],
    "constraints": [{"coeffs": [0], "b": -1, "stable": True}],
}
FEAS = {
    "audit_id": "src-feas",
    "variables": ["x"],
    "constraints": [{"coeffs": [1], "b": 1}],
}


def test_repair_first_submit_freezes_snapshot(tmp_path):
    svc = make_service(tmp_path)
    svc.audit(INF1)
    rec, replayed = svc.repair(
        {"repair_id": "fix-1", "source_audit_id": "src-1"}
    )
    assert replayed is False
    assert rec["kind"] == "min-rhs-relaxation"
    assert rec["source_audit_id"] == "src-1"
    # 来源快照冻结了规范变量、约束顺序与原证据
    src = rec["source"]
    assert src["variables"] == ["x"]
    assert [c["index"] for c in src["constraints"]] == [0, 1]
    assert src["constraints"][0]["stable"] is True
    assert src["evidence"]["status"] == "infeasible"
    assert src["evidence"]["combined_relation"] == "0 <= -1"
    # 结果内容
    res = rec["result"]
    assert res["status"] == "repaired"
    assert res["total_relaxation"] == "1"
    assert res["stable_order"] == [0]
    assert [c["relaxation"] for c in res["constraints"]] == ["0", "1"]


def test_repair_replay_same_source_returns_same_record(tmp_path):
    svc = make_service(tmp_path)
    svc.audit(INF1)
    r1, _ = svc.repair({"repair_id": "fix-1", "source_audit_id": "src-1"})
    r2, replayed = svc.repair({"repair_id": "fix-1", "source_audit_id": "src-1"})
    assert replayed is True
    assert r1 == r2
    assert r1["created_at"] == r2["created_at"]


def test_repair_missing_source_rejected_no_record(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(SourceNotFoundError):
        svc.repair({"repair_id": "fix-x", "source_audit_id": "ghost"})
    assert svc.fetch_repair("fix-x") is None
    # 之后来源出现，同编号可以正常首次提交
    svc.audit(INF1)
    rec, replayed = svc.repair(
        {"repair_id": "fix-x", "source_audit_id": "src-1"}
    )
    assert replayed is False and rec["result"]["total_relaxation"] == "1"


def test_repair_on_feasible_source_rejected(tmp_path):
    svc = make_service(tmp_path)
    svc.audit(FEAS)
    with pytest.raises(SourceNotInfeasibleError):
        svc.repair({"repair_id": "fix-f", "source_audit_id": "src-feas"})
    assert svc.fetch_repair("fix-f") is None


def test_repair_id_rebound_to_other_source_conflicts_and_preserves(tmp_path):
    svc = make_service(tmp_path)
    svc.audit(INF1)
    svc.audit(INF2)
    r1, _ = svc.repair({"repair_id": "fix-1", "source_audit_id": "src-1"})

    with pytest.raises(RepairConflictError) as ei:
        svc.repair({"repair_id": "fix-1", "source_audit_id": "src-2"})
    assert ei.value.new_source_audit_id == "src-2"
    # 已冻结修复保持不变
    kept = svc.fetch_repair("fix-1")
    assert kept["source_audit_id"] == "src-1"
    assert kept["source_fingerprint"] == r1["source_fingerprint"]
    assert kept["result"] == r1["result"]


def test_rebind_to_nonexistent_source_also_conflicts(tmp_path):
    """已冻结的 repair_id 换一个不存在的来源：仍是改换来源冲突（409），
    而不是 404；已冻结修复不变。"""
    svc = make_service(tmp_path)
    svc.audit(INF1)
    svc.repair({"repair_id": "fix-1", "source_audit_id": "src-1"})
    with pytest.raises(RepairConflictError):
        svc.repair({"repair_id": "fix-1", "source_audit_id": "ghost"})
    assert svc.fetch_repair("fix-1")["source_audit_id"] == "src-1"


def test_source_audit_unchanged_after_repair(tmp_path):
    svc = make_service(tmp_path)
    src_rec, _ = svc.audit(INF1)
    svc.repair({"repair_id": "fix-1", "source_audit_id": "src-1"})
    svc.repair({"repair_id": "fix-1", "source_audit_id": "src-1"})
    after = svc.fetch("src-1")
    assert after == src_rec
    assert after["result"]["status"] == "infeasible"


def test_invalid_repair_request_no_record(tmp_path):
    svc = make_service(tmp_path)
    svc.audit(INF1)
    for bad in (
        {},
        {"repair_id": "fix-1"},
        {"source_audit_id": "src-1"},
        {"repair_id": "bad/id", "source_audit_id": "src-1"},
        {"repair_id": 123, "source_audit_id": "src-1"},
        {"repair_id": "fix-1", "source_audit_id": ["src-1"]},
        {"repair_id": "  ", "source_audit_id": "src-1"},
    ):
        with pytest.raises(ValueError):
            svc.repair(bad)
    assert svc.fetch_repair("fix-1") is None


def test_different_repair_ids_same_source_both_freeze(tmp_path):
    svc = make_service(tmp_path)
    svc.audit(INF1)
    r1, rep1 = svc.repair({"repair_id": "fix-a", "source_audit_id": "src-1"})
    r2, rep2 = svc.repair({"repair_id": "fix-b", "source_audit_id": "src-1"})
    assert not rep1 and not rep2
    assert r1["repair_id"] == "fix-a" and r2["repair_id"] == "fix-b"
    assert r1["result"] == r2["result"]
    assert svc.fetch_repair("fix-a") and svc.fetch_repair("fix-b")


def test_repair_fingerprint_tracks_request(tmp_path):
    store = RepairStore(str(tmp_path / "data"))
    rec, _ = store.submit("fix-1", "src-1", "sha256:abc", {"result": {"x": 1}})
    from app.storage import fingerprint
    assert rec["fingerprint"] == fingerprint(
        {"repair_id": "fix-1", "source_audit_id": "src-1"}
    )


def test_concurrent_first_repairs_single_record(tmp_path):
    svc = make_service(tmp_path)
    svc.audit(INF1)
    outcomes = []

    def worker():
        outcomes.append(
            svc.repair({"repair_id": "fix-1", "source_audit_id": "src-1"})
        )

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(outcomes) == 8
    fps = {o[0]["fingerprint"] for o in outcomes}
    assert len(fps) == 1
    assert sum(1 for _, rep in outcomes if rep) == 7


def test_concurrent_rebind_conflict(tmp_path):
    svc = make_service(tmp_path)
    svc.audit(INF1)
    svc.audit(INF2)
    outcomes = []

    def worker(source_id):
        try:
            outcomes.append(("ok", svc.repair(
                {"repair_id": "fix-1", "source_audit_id": source_id}
            )))
        except RepairConflictError:
            outcomes.append(("conflict", None))

    # 先让 src-1 赢，再并发 src-2 若干
    svc.repair({"repair_id": "fix-1", "source_audit_id": "src-1"})
    threads = [
        threading.Thread(target=worker, args=(sid,))
        for sid in ["src-2", "src-2", "src-1"]
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [o[0] for o in outcomes].count("conflict") == 2
    assert svc.fetch_repair("fix-1")["source_audit_id"] == "src-1"


def test_repair_persistence_across_restart(tmp_path):
    data = str(tmp_path / "data")
    store = AuditStore(data)
    svc = AuditService(store, RepairStore(data))
    svc.audit(INF1)
    svc.repair({"repair_id": "fix-1", "source_audit_id": "src-1"})

    svc2 = AuditService(AuditStore(data), RepairStore(data))
    rec = svc2.fetch_repair("fix-1")
    assert rec is not None
    assert rec["source_audit_id"] == "src-1"
    assert rec["result"]["total_relaxation"] == "1"
    # 重放仍然幂等
    r2, replayed = svc2.repair(
        {"repair_id": "fix-1", "source_audit_id": "src-1"}
    )
    assert replayed and r2["fingerprint"] == rec["fingerprint"]


def test_replay_works_even_if_source_file_gone(tmp_path):
    """冻结修复自包含来源快照：重放不依赖来源文件仍在。"""
    import hashlib

    data = str(tmp_path / "data")
    svc = AuditService(AuditStore(data), RepairStore(data))
    svc.audit(INF1)
    frozen, _ = svc.repair({"repair_id": "fix-1", "source_audit_id": "src-1"})
    src_path = (
        tmp_path / "data"
        / (hashlib.sha256(b"src-1").hexdigest() + ".json")
    )
    src_path.unlink()
    rec, replayed = svc.repair(
        {"repair_id": "fix-1", "source_audit_id": "src-1"}
    )
    assert replayed is True and rec == frozen


def test_replay_tampered_source_fingerprint_conflicts(tmp_path):
    """来源文件被篡改（指纹不再匹配载荷）：重放拒绝，已冻结修复不变。"""
    import json
    import os
    from app.service import SourceTamperedError

    svc = make_service(tmp_path)
    svc.audit(INF1)
    frozen, _ = svc.repair({"repair_id": "fix-1", "source_audit_id": "src-1"})

    # 手工把来源文件里的冻结指纹改掉（载荷不变）
    path = svc.store._path("src-1")
    with open(path, encoding="utf-8") as f:
        disk = json.load(f)
    disk["fingerprint"] = "sha256:tampered"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(disk, f)
    os.replace(tmp, path)

    with pytest.raises(SourceTamperedError):
        svc.repair({"repair_id": "fix-1", "source_audit_id": "src-1"})
    assert svc.fetch_repair("fix-1") == frozen


def test_repairs_stored_in_separate_namespace(tmp_path):
    data = str(tmp_path / "data")
    AuditStore(data)
    RepairStore(data)
    entries = sorted(p.name for p in (tmp_path / "data").iterdir())
    assert entries == ["repairs"]
