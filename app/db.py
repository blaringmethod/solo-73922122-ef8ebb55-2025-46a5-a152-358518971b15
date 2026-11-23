"""SQLite 存取层：批次、清单号段与增量维护的覆盖段。

设计要点：
- 帧号以 64 位整数存储（可跨十亿），存储单位是号段而不是帧。
- ``coverage`` 表缓存每侧已合并的覆盖段，登记/更正/撤回清单时
  只对受影响区间做局部重算，不重扫整批清单。
- 所有写操作在单个事务（BEGIN IMMEDIATE）内完成，越界或入库失败
  自动回滚，清单与覆盖段（核对结果）同时生效。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import List, Optional, Sequence, Tuple

from . import intervals

DEFAULT_DB_PATH = os.environ.get(
    "QC_DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "qc.db")
)

VALID_SIDES = ("collector", "library")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT PRIMARY KEY,
    frame_start INTEGER NOT NULL,
    frame_end   INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (frame_end >= frame_start)
);

CREATE TABLE IF NOT EXISTS manifests (
    manifest_no TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL REFERENCES batches(batch_id),
    side        TEXT NOT NULL CHECK (side IN ('collector', 'library')),
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'withdrawn')),
    seg_start   INTEGER NOT NULL,
    seg_end     INTEGER NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (seg_end >= seg_start)
);
CREATE INDEX IF NOT EXISTS idx_manifests_lookup
    ON manifests (batch_id, side, status, seg_start, seg_end);

CREATE TABLE IF NOT EXISTS coverage (
    batch_id  TEXT NOT NULL REFERENCES batches(batch_id),
    side      TEXT NOT NULL CHECK (side IN ('collector', 'library')),
    seg_start INTEGER NOT NULL,
    seg_end   INTEGER NOT NULL,
    PRIMARY KEY (batch_id, side, seg_start)
);
CREATE INDEX IF NOT EXISTS idx_coverage_lookup
    ON coverage (batch_id, side, seg_start, seg_end);
"""


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # 每个请求通过依赖获取独立连接；FastAPI 线程池可能在不同工作线程上
    # 运行端点与依赖，关闭同线程限制（连接不跨请求共享，无并发共用问题）。
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """立即加写锁的事务，异常即回滚，保证原子生效。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------- 批次 ----------

def get_batch(conn: sqlite3.Connection, batch_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
    ).fetchone()


def create_batch(conn: sqlite3.Connection, batch_id: str, start: int, end: int) -> None:
    conn.execute(
        "INSERT INTO batches (batch_id, frame_start, frame_end) VALUES (?, ?, ?)",
        (batch_id, start, end),
    )


# ---------- 清单 ----------

def get_manifest(conn: sqlite3.Connection, manifest_no: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM manifests WHERE manifest_no=?", (manifest_no,)
    ).fetchone()


def insert_manifest(
    conn: sqlite3.Connection, manifest_no: str, batch_id: str, side: str, start: int, end: int
) -> None:
    conn.execute(
        "INSERT INTO manifests (manifest_no, batch_id, side, seg_start, seg_end) "
        "VALUES (?, ?, ?, ?, ?)",
        (manifest_no, batch_id, side, start, end),
    )


def update_manifest_segment(
    conn: sqlite3.Connection, manifest_no: str, start: int, end: int
) -> None:
    conn.execute(
        "UPDATE manifests SET seg_start=?, seg_end=?, updated_at=datetime('now') "
        "WHERE manifest_no=?",
        (start, end, manifest_no),
    )


def mark_manifest_withdrawn(conn: sqlite3.Connection, manifest_no: str) -> None:
    conn.execute(
        "UPDATE manifests SET status='withdrawn', updated_at=datetime('now') "
        "WHERE manifest_no=?",
        (manifest_no,),
    )


def active_manifests_in(
    conn: sqlite3.Connection, batch_id: str, side: str, start: int, end: int
) -> List[sqlite3.Row]:
    """取与 [start, end] 相交的有效（未撤回）清单，走索引区间扫描。"""
    return conn.execute(
        "SELECT * FROM manifests "
        "WHERE batch_id=? AND side=? AND status='active' "
        "AND seg_start <= ? AND seg_end >= ? "
        "ORDER BY seg_start",
        (batch_id, side, end, start),
    ).fetchall()


# ---------- 覆盖段（核对结果缓存，增量维护） ----------

def coverage_segments(
    conn: sqlite3.Connection, batch_id: str, side: str
) -> List[Tuple[int, int]]:
    rows = conn.execute(
        "SELECT seg_start, seg_end FROM coverage "
        "WHERE batch_id=? AND side=? ORDER BY seg_start",
        (batch_id, side),
    ).fetchall()
    return [(r["seg_start"], r["seg_end"]) for r in rows]


def refresh_region(
    conn: sqlite3.Connection, batch_id: str, side: str, region_start: int, region_end: int
) -> None:
    """仅在受影响区间内重建该侧覆盖段，不重扫整批清单。

    重建范围向两侧各扩 1 帧，使与受影响区间首尾相邻的旧覆盖段一并
    被吸收，重新合并成最大连续段。
    """
    lo, hi = region_start - 1, region_end + 1

    old_rows = conn.execute(
        "SELECT seg_start, seg_end FROM coverage "
        "WHERE batch_id=? AND side=? AND seg_start <= ? AND seg_end >= ? "
        "ORDER BY seg_start",
        (batch_id, side, hi, lo),
    ).fetchall()
    if old_rows:
        lo = min(lo, min(r["seg_start"] for r in old_rows))
        hi = max(hi, max(r["seg_end"] for r in old_rows))
        conn.execute(
            "DELETE FROM coverage "
            "WHERE batch_id=? AND side=? AND seg_start <= ? AND seg_end >= ?",
            (batch_id, side, hi, lo),
        )

    rows = active_manifests_in(conn, batch_id, side, lo, hi)
    pieces: List[Tuple[int, int]] = [
        (max(r["seg_start"], lo), min(r["seg_end"], hi)) for r in rows
    ]
    for seg in intervals.merge(pieces):
        conn.execute(
            "INSERT INTO coverage (batch_id, side, seg_start, seg_end) VALUES (?, ?, ?, ?)",
            (batch_id, side, seg[0], seg[1]),
        )


# ---------- 核对 ----------

def reconcile(
    conn: sqlite3.Connection, batch_id: str
) -> dict:
    batch = get_batch(conn, batch_id)
    if batch is None:
        raise LookupError(f"批次不存在: {batch_id}")
    b_start, b_end = batch["frame_start"], batch["frame_end"]
    batch_range: Sequence[Tuple[int, int]] = [(b_start, b_end)]

    collector = intervals.merge(coverage_segments(conn, batch_id, "collector"))
    library = intervals.merge(coverage_segments(conn, batch_id, "library"))

    both = intervals.intersect(collector, library)
    only_collector = intervals.subtract(collector, library)
    only_library = intervals.subtract(library, collector)
    covered = intervals.merge(both + only_collector + only_library)
    neither = intervals.subtract(batch_range, covered)

    def clip(segs):
        return [
            {"frame_start": s, "frame_end": e}
            for s, e in intervals.intersect(segs, batch_range)
        ]

    return {
        "batch_id": batch_id,
        "expected_range": {"frame_start": b_start, "frame_end": b_end},
        "both_present": clip(both),
        "only_collector": clip(only_collector),
        "only_library": clip(only_library),
        "neither_present": clip(neither),
    }


def trace_frame(conn: sqlite3.Connection, batch_id: str, frame_no: int) -> dict:
    batch = get_batch(conn, batch_id)
    if batch is None:
        raise LookupError(f"批次不存在: {batch_id}")
    if not (batch["frame_start"] <= frame_no <= batch["frame_end"]):
        raise ValueError(
            f"帧号 {frame_no} 越界，批次范围为 "
            f"[{batch['frame_start']}, {batch['frame_end']}]"
        )

    def manifests_of(side):
        rows = active_manifests_in(conn, batch_id, side, frame_no, frame_no)
        return [
            {
                "manifest_no": r["manifest_no"],
                "seg_start": r["seg_start"],
                "seg_end": r["seg_end"],
            }
            for r in rows
        ]

    collector_manifests = manifests_of("collector")
    library_manifests = manifests_of("library")
    in_collector = bool(collector_manifests)
    in_library = bool(library_manifests)
    if in_collector and in_library:
        status = "both_present"
    elif in_collector:
        status = "only_collector"
    elif in_library:
        status = "only_library"
    else:
        status = "neither_present"

    return {
        "batch_id": batch_id,
        "frame_no": frame_no,
        "status": status,
        "collector_manifests": collector_manifests,
        "library_manifests": library_manifests,
    }
