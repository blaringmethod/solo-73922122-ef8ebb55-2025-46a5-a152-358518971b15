"""旧库升级：老结构（无 cnt 的合并覆盖段）升级后可直接核对和改撤。"""

import sqlite3

import app.db as db

# 旧版本库的建表语句（coverage 无 cnt 列，存已合并的覆盖段）
LEGACY_SCHEMA = """
CREATE TABLE batches (
    batch_id   TEXT PRIMARY KEY,
    frame_start INTEGER NOT NULL,
    frame_end   INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (frame_end >= frame_start)
);
CREATE TABLE manifests (
    manifest_no TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL REFERENCES batches(batch_id),
    side        TEXT NOT NULL CHECK (side IN ('collector', 'library')),
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'withdrawn')),
    seg_start   INTEGER NOT NULL,
    seg_end     INTEGER NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (seg_end >= seg_start)
);
CREATE INDEX idx_manifests_lookup
    ON manifests (batch_id, side, status, seg_start, seg_end);
CREATE TABLE coverage (
    batch_id  TEXT NOT NULL REFERENCES batches(batch_id),
    side      TEXT NOT NULL CHECK (side IN ('collector', 'library')),
    seg_start INTEGER NOT NULL,
    seg_end   INTEGER NOT NULL,
    PRIMARY KEY (batch_id, side, seg_start)
);
CREATE INDEX idx_coverage_lookup
    ON coverage (batch_id, side, seg_start, seg_end);
"""


def _build_legacy_db(path):
    """按旧结构造库：批次 B1，含有效/已撤回清单与旧式合并覆盖段。"""
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.executemany(
        "INSERT INTO manifests (manifest_no, batch_id, side, status, seg_start, seg_end) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("C1", "B1", "collector", "active", 1, 40),
            ("C2", "B1", "collector", "active", 41, 60),
            ("C3", "B1", "collector", "withdrawn", 1, 100),  # 已撤回，不参与覆盖
            ("L1", "B1", "library", "active", 21, 70),
        ],
    )
    conn.executemany(
        "INSERT INTO coverage (batch_id, side, seg_start, seg_end) VALUES (?, ?, ?, ?)",
        [
            ("B1", "collector", 1, 60),   # 旧式：已合并
            ("B1", "library", 21, 70),
        ],
    )
    conn.execute(
        "INSERT INTO batches (batch_id, frame_start, frame_end) VALUES ('B1', 1, 100)")
    conn.commit()
    conn.close()


def test_legacy_db_upgrades_and_serves_original_manifests(tmp_path):
    path = str(tmp_path / "legacy.db")
    _build_legacy_db(path)

    db.init_db(path)  # 触发升级

    conn = db.connect(path)
    # 旧表已替换为步进段结构
    cols = [r[1] for r in conn.execute("PRAGMA table_info(coverage)").fetchall()]
    assert "cnt" in cols
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name='coverage_legacy'"
    ).fetchone() is None

    # 升级后可直接核对：结果与按原清单手工推算一致
    r = db.reconcile(conn, "B1")
    assert r["both_present"] == [{"frame_start": 21, "frame_end": 60}]
    assert r["only_collector"] == [{"frame_start": 1, "frame_end": 20}]
    assert r["only_library"] == [{"frame_start": 61, "frame_end": 70}]
    assert r["neither_present"] == [{"frame_start": 71, "frame_end": 100}]

    # 可直接改撤：撤回 C2 后，C1 覆盖的帧仍算有记录
    with db.transaction(conn):
        db.mark_manifest_withdrawn(conn, "C2")
        db.coverage_remove(conn, "B1", "collector", 41, 60)
    r = db.reconcile(conn, "B1")
    assert r["both_present"] == [{"frame_start": 21, "frame_end": 40}]
    assert r["only_collector"] == [{"frame_start": 1, "frame_end": 20}]
    assert r["only_library"] == [
        {"frame_start": 41, "frame_end": 70},
    ]
    conn.close()

    # 重复 init_db 幂等，不再触发迁移
    db.init_db(path)
    conn = db.connect(path)
    assert db.reconcile(conn, "B1")["both_present"] == [
        {"frame_start": 21, "frame_end": 40}
    ]
    conn.close()


def test_fresh_db_not_treated_as_legacy(tmp_path):
    path = str(tmp_path / "fresh.db")
    db.init_db(path)
    conn = db.connect(path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(coverage)").fetchall()]
    assert "cnt" in cols
    conn.close()
