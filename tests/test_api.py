"""API 端到端测试（TestClient + 临时 SQLite）。"""

import importlib
import os

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("QC_DB_PATH", str(db_file))
    import app.db as db_mod
    import app.main as main_mod
    importlib.reload(db_mod)
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient

    with TestClient(main_mod.app) as c:
        yield c


def _batch(client, bid="B1", start=1, end=100):
    r = client.post("/batches", json={"batch_id": bid, "frame_start": start, "frame_end": end})
    assert r.status_code == 201, r.text


def _manifest(client, no, bid, side, s, e):
    r = client.post(
        "/manifests",
        json={"manifest_no": no, "batch_id": bid, "side": side, "seg_start": s, "seg_end": e},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_full_reconcile_flow(client):
    _batch(client)
    # 采集端：1-40 和 41-60（相邻，应合并）；缺 81-100
    _manifest(client, "C1", "B1", "collector", 1, 40)
    _manifest(client, "C2", "B1", "collector", 41, 60)
    # 影像库：21-70；缺 1-20、81-100
    _manifest(client, "L1", "B1", "library", 21, 70)

    r = client.get("/batches/B1/reconcile").json()
    assert r["both_present"] == [{"frame_start": 21, "frame_end": 60}]
    assert r["only_collector"] == [{"frame_start": 1, "frame_end": 20}]
    assert r["only_library"] == [{"frame_start": 61, "frame_end": 70}]
    assert r["neither_present"] == [{"frame_start": 71, "frame_end": 100}]


def test_withdraw_keeps_other_manifests_coverage(client):
    _batch(client)
    _manifest(client, "C1", "B1", "collector", 1, 50)
    _manifest(client, "C2", "B1", "collector", 30, 100)
    _manifest(client, "L1", "B1", "library", 1, 100)

    r = client.post("/manifests/withdraw", json={"manifest_no": "C1"})
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"

    r = client.get("/batches/B1/reconcile").json()
    # C2 仍覆盖 30-100；1-29 变为双方皆缺（库有、采集端无 => only_library）
    assert r["both_present"] == [{"frame_start": 30, "frame_end": 100}]
    assert r["only_collector"] == []
    assert r["only_library"] == [{"frame_start": 1, "frame_end": 29}]

    # 再次撤回报 409，状态不变
    assert client.post("/manifests/withdraw", json={"manifest_no": "C1"}).status_code == 409


def test_correction_only_affects_related_segments(client):
    _batch(client)
    _manifest(client, "C1", "B1", "collector", 10, 20)
    _manifest(client, "C2", "B1", "collector", 50, 60)
    _manifest(client, "L1", "B1", "library", 1, 100)

    r = client.put("/manifests/C1", json={"seg_start": 25, "seg_end": 35})
    assert r.status_code == 200
    r = client.get("/batches/B1/reconcile").json()
    assert r["both_present"] == [
        {"frame_start": 25, "frame_end": 35},
        {"frame_start": 50, "frame_end": 60},
    ]
    assert r["only_library"] == [
        {"frame_start": 1, "frame_end": 24},
        {"frame_start": 36, "frame_end": 49},
        {"frame_start": 61, "frame_end": 100},
    ]


def test_trace_frame(client):
    _batch(client)
    _manifest(client, "C1", "B1", "collector", 10, 30)
    _manifest(client, "C2", "B1", "collector", 25, 40)
    _manifest(client, "L1", "B1", "library", 20, 35)

    r = client.get("/batches/B1/frames/27").json()
    assert r["status"] == "both_present"
    assert {m["manifest_no"] for m in r["collector_manifests"]} == {"C1", "C2"}
    assert [m["manifest_no"] for m in r["library_manifests"]] == ["L1"]

    r = client.get("/batches/B1/frames/10").json()
    assert r["status"] == "only_collector"

    r = client.get("/batches/B1/frames/50")
    assert r.status_code == 200 and r.json()["status"] == "neither_present"

    r = client.get("/batches/B1/frames/101")
    assert r.status_code == 422


def test_out_of_range_rejected_and_state_unchanged(client):
    _batch(client)
    # 登记越界
    r = client.post(
        "/manifests",
        json={"manifest_no": "X1", "batch_id": "B1", "side": "collector",
              "seg_start": 50, "seg_end": 120},
    )
    assert r.status_code == 422
    # 更正越界：原清单保持不变
    _manifest(client, "C1", "B1", "collector", 10, 20)
    r = client.put("/manifests/C1", json={"seg_start": 0, "seg_end": 20})
    assert r.status_code == 422
    got = client.get("/manifests/C1").json()
    assert got["seg_start"] == 10 and got["seg_end"] == 20
    # 覆盖结果也不受影响
    r = client.get("/batches/B1/reconcile").json()
    assert r["only_collector"] == [{"frame_start": 10, "frame_end": 20}]


def test_duplicate_and_missing_errors(client):
    _batch(client)
    _manifest(client, "C1", "B1", "collector", 1, 10)
    r = client.post(
        "/manifests",
        json={"manifest_no": "C1", "batch_id": "B1", "side": "library",
              "seg_start": 1, "seg_end": 10},
    )
    assert r.status_code == 409
    assert client.get("/batches/NOPE/reconcile").status_code == 404
    assert client.put("/manifests/GONE", json={"seg_start": 1, "seg_end": 2}).status_code == 404


def test_invalid_segment_payload(client):
    _batch(client)
    r = client.post(
        "/manifests",
        json={"manifest_no": "C9", "batch_id": "B1", "side": "collector",
              "seg_start": 10, "seg_end": 1},
    )
    assert r.status_code == 422


def test_billion_range_uses_segments_not_frames(client):
    start, end = 0, 2_000_000_000
    _batch(client, "BIG", start, end)
    _manifest(client, "C1", "BIG", "collector", start, end - 1)
    _manifest(client, "L1", "BIG", "library", start + 1, end)

    r = client.get("/batches/BIG/reconcile").json()
    assert r["both_present"] == [{"frame_start": 1, "frame_end": end - 1}]
    assert r["only_collector"] == [{"frame_start": 0, "frame_end": 0}]
    assert r["only_library"] == [{"frame_start": end, "frame_end": end}]
    assert r["neither_present"] == []

    t = client.get(f"/batches/BIG/frames/{end}").json()
    assert t["status"] == "only_library"


def test_adjacent_manifests_merge_into_maximal_segments(client):
    _batch(client, end=30)
    _manifest(client, "C1", "B1", "collector", 1, 10)
    _manifest(client, "C2", "B1", "collector", 11, 20)
    _manifest(client, "C3", "B1", "collector", 21, 30)
    _manifest(client, "L1", "B1", "library", 1, 30)
    r = client.get("/batches/B1/reconcile").json()
    assert r["both_present"] == [{"frame_start": 1, "frame_end": 30}]


def test_int64_overflow_rejected_before_persistence(client):
    huge = 2**63
    r = client.post("/batches", json={"batch_id": "B64", "frame_start": 0, "frame_end": huge})
    assert r.status_code == 422
    assert client.get("/batches/B64").status_code == 404
    _batch(client, "B64", 0, 100)
    r = client.post(
        "/manifests",
        json={"manifest_no": "C64", "batch_id": "B64", "side": "collector",
              "seg_start": 1, "seg_end": huge},
    )
    assert r.status_code == 422
    assert client.get("/manifests/C64").status_code == 404
    r = client.get(f"/batches/B64/frames/{huge}")
    assert r.status_code == 422
