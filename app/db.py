"""SQLite 存取层：批次、清单号段与引用计数式覆盖步进段。

设计要点：
- 帧号以 64 位整数存储（可跨十亿），存储单位是号段而不是帧。
- ``coverage`` 表是**步进段**（step map）：同一 ``(batch_id, side)`` 下
  互不重叠的号段，``cnt`` 表示该段被多少张有效清单覆盖（>= 1）。
  相邻段的 ``cnt`` 必不相同（相同即已合并），段数不超过 2*清单数+1。
- 登记/更正/撤回清单 = 对对应号段做 +1/-1 增量维护（``coverage_add`` /
  ``coverage_remove``）。定位窗口只需若干次主键索引点查（O(log n)），
  实际读写的行数只随与号段相交的步进段数及必要的并段边界数增长，
  与同侧无关清单的数量无关；更正时新旧号段互不相交则分别处理，
  两段之间的无关记录不会被读取。
- 核对时把步进段按相邻关系合并即得该侧最大连续覆盖段，再求四类段。
- 所有写操作在单个事务（BEGIN IMMEDIATE）内完成，越界或入库失败
  自动回滚，清单与覆盖段（核对结果）同时生效。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Dict, List, Optional, Sequence, Tuple

from . import intervals

DEFAULT_DB_PATH = os.environ.get(
    "QC_DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "qc.db")
)

VALID_SIDES = ("collector", "library")

# SQLite INTEGER 为 64 位有符号整数；e+1 等中间值只留在 Python 侧，从不下发 SQL
INT64_MAX = 2**63 - 1

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
    cnt       INTEGER NOT NULL CHECK (cnt >= 1),
    PRIMARY KEY (batch_id, side, seg_start),
    CHECK (seg_end >= seg_start)
);
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
    """建表；旧库（coverage 无 cnt 列）先在事务内升级为步进段结构。"""
    conn = connect(db_path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(coverage)").fetchall()]
        if cols and "cnt" not in cols:
            # 旧库升级：清单表结构未变，覆盖段按原清单重建，升级后可直接核对/改撤
            with transaction(conn):
                _migrate_legacy_coverage(conn)
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _migrate_legacy_coverage(conn: sqlite3.Connection) -> None:
    """把旧的"已合并覆盖段"表重建为引用计数步进段。

    旧表只存合并后的段，反推不出每帧被几张清单覆盖，因此直接以
    manifests 表中的有效清单为准全量重建（一次性），与原库数据等价。
    """
    conn.execute("ALTER TABLE coverage RENAME TO coverage_legacy")
    conn.execute(
        "CREATE TABLE coverage ("
        " batch_id  TEXT NOT NULL REFERENCES batches(batch_id),"
        " side      TEXT NOT NULL CHECK (side IN ('collector', 'library')),"
        " seg_start INTEGER NOT NULL,"
        " seg_end   INTEGER NOT NULL,"
        " cnt       INTEGER NOT NULL CHECK (cnt >= 1),"
        " PRIMARY KEY (batch_id, side, seg_start),"
        " CHECK (seg_end >= seg_start))"
    )
    rows = conn.execute(
        "SELECT batch_id, side, seg_start, seg_end FROM manifests "
        "WHERE status='active' ORDER BY batch_id, side, seg_start"
    ).fetchall()
    for r in rows:
        _apply_delta(conn, r["batch_id"], r["side"], r["seg_start"], r["seg_end"], 1)
    conn.execute("DROP TABLE coverage_legacy")


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


# ---------- 覆盖步进段（核对结果缓存，引用计数增量维护） ----------

def coverage_segments(
    conn: sqlite3.Connection, batch_id: str, side: str
) -> List[Tuple[int, int]]:
    """该侧全部覆盖步进段（未按相邻合并）；核对时由 intervals.merge 合并。"""
    rows = conn.execute(
        "SELECT seg_start, seg_end FROM coverage "
        "WHERE batch_id=? AND side=? ORDER BY seg_start",
        (batch_id, side),
    ).fetchall()
    return [(r["seg_start"], r["seg_end"]) for r in rows]


def _read_window(
    conn: sqlite3.Connection, batch_id: str, side: str, start: int, end: int
) -> List[Tuple[int, int, int]]:
    """读取维护 [start, end] 所需的连续步进段窗口。

    窗口 = 受影响段序列（含 start 的段、或 start 之后第一段，直到 end）
    + 左右各一段相邻段（切分定位与并段边界）。常数次主键索引定位
    O(log n)，读出的行数只随与 [start, end] 相交的段数增长，
    与无关段数量无关。
    """
    rows: List[Tuple[int, int, int]] = []
    probe = conn.execute(
        "SELECT seg_start, seg_end, cnt FROM coverage "
        "WHERE batch_id=? AND side=? AND seg_start<=? "
        "ORDER BY seg_start DESC LIMIT 1",
        (batch_id, side, start),
    ).fetchone()
    if probe is not None and probe["seg_end"] >= start:
        # probe 覆盖 start，是受影响第一段；其左侧相邻段是并段边界候选
        run_start = probe["seg_start"]
        pred = conn.execute(
            "SELECT seg_start, seg_end, cnt FROM coverage "
            "WHERE batch_id=? AND side=? AND seg_start<? "
            "ORDER BY seg_start DESC LIMIT 1",
            (batch_id, side, run_start),
        ).fetchone()
        if pred is not None:
            rows.append((pred["seg_start"], pred["seg_end"], pred["cnt"]))
    else:
        # probe 不存在或完全落在 start 之前：它本身就是左边界候选；
        # 受影响第一段是 start 之后的第一段（可能不存在）
        if probe is not None:
            rows.append((probe["seg_start"], probe["seg_end"], probe["cnt"]))
        nxt = conn.execute(
            "SELECT seg_start FROM coverage "
            "WHERE batch_id=? AND side=? AND seg_start>? "
            "ORDER BY seg_start LIMIT 1",
            (batch_id, side, start),
        ).fetchone()
        run_start = nxt["seg_start"] if nxt is not None else None
    if run_start is not None:
        mid = conn.execute(
            "SELECT seg_start, seg_end, cnt FROM coverage "
            "WHERE batch_id=? AND side=? AND seg_start>=? AND seg_start<=? "
            "ORDER BY seg_start",
            (batch_id, side, run_start, end),
        ).fetchall()
        rows.extend((r["seg_start"], r["seg_end"], r["cnt"]) for r in mid)
        if end < INT64_MAX:
            succ = conn.execute(
                "SELECT seg_start, seg_end, cnt FROM coverage "
                "WHERE batch_id=? AND side=? AND seg_start>? "
                "ORDER BY seg_start LIMIT 1",
                (batch_id, side, end),
            ).fetchone()
            if succ is not None:
                rows.append((succ["seg_start"], succ["seg_end"], succ["cnt"]))
    return rows


def _apply_delta(
    conn: sqlite3.Connection, batch_id: str, side: str, start: int, end: int, delta: int
) -> Dict[str, int]:
    """对覆盖步进段在 [start, end] 上做 +1/-1 增量，返回读写行数统计。

    扫描线只在窗口内推进：切分边界、增减计数、删除零计数段、合并
    计数相同的相邻段；写回时只写真正发生变化的行。
    """
    if delta not in (1, -1):
        raise ValueError(f"非法覆盖增量: {delta}")

    rows = _read_window(conn, batch_id, side, start, end)

    # 事件点：段起点 +cnt，段终点后一位 -cnt；外加本次增量的 [start, end]
    events: List[Tuple[int, int]] = [(start, delta), (end + 1, -delta)]
    for seg_start, seg_end, cnt in rows:
        events.append((seg_start, cnt))
        events.append((seg_end + 1, -cnt))
    events.sort()

    new_rows: List[Tuple[int, int, int]] = []
    cur = 0
    i, n = 0, len(events)
    while i < n:
        pos = events[i][0]
        j = i
        while j < n and events[j][0] == pos:
            cur += events[j][1]
            j += 1
        if cur < 0:
            # 有效清单的号段必然已被 +1 过，正常流程不会触发；
            # 一旦触发说明数据不一致，抛错回滚保持原状
            raise RuntimeError(
                f"覆盖计数出现负值: {batch_id}/{side} 在帧 {pos} 附近"
            )
        nxt = events[j][0] if j < n else None
        if cur >= 1 and nxt is not None and pos <= INT64_MAX:
            if new_rows and new_rows[-1][2] == cur and new_rows[-1][1] + 1 == pos:
                new_rows[-1] = (new_rows[-1][0], nxt - 1, cur)
            else:
                new_rows.append((pos, nxt - 1, cur))
        i = j

    # 差量写回：只写发生变化的行
    writes = 0
    old_by_start = {a: (b, c) for a, b, c in rows}
    new_starts = set()
    for a, b, c in new_rows:
        new_starts.add(a)
        old = old_by_start.get(a)
        if old is None:
            conn.execute(
                "INSERT INTO coverage (batch_id, side, seg_start, seg_end, cnt) "
                "VALUES (?, ?, ?, ?, ?)",
                (batch_id, side, a, b, c),
            )
            writes += 1
        elif old != (b, c):
            conn.execute(
                "UPDATE coverage SET seg_end=?, cnt=? "
                "WHERE batch_id=? AND side=? AND seg_start=?",
                (b, c, batch_id, side, a),
            )
            writes += 1
    for a, _, _ in rows:
        if a not in new_starts:
            conn.execute(
                "DELETE FROM coverage WHERE batch_id=? AND side=? AND seg_start=?",
                (batch_id, side, a),
            )
            writes += 1
    return {"rows_read": len(rows), "rows_written": writes}


def coverage_add(
    conn: sqlite3.Connection, batch_id: str, side: str, start: int, end: int
) -> Dict[str, int]:
    """登记/更正后的新号段：覆盖计数 +1。"""
    return _apply_delta(conn, batch_id, side, start, end, 1)


def coverage_remove(
    conn: sqlite3.Connection, batch_id: str, side: str, start: int, end: int
) -> Dict[str, int]:
    """撤回/更正掉的旧号段：覆盖计数 -1；同侧其他清单覆盖的帧仍有记录。"""
    return _apply_delta(conn, batch_id, side, start, end, -1)


# ---------- 核对 ----------

def reconcile(
    conn: sqlite3.Connection, batch_id: str
) -> dict:
    batch = get_batch(conn, batch_id)
    if batch is None:
        raise LookupError(f"批次不存在: {batch_id}")
    b_start, b_end = batch["frame_start"], batch["frame_end"]
    batch_range: Sequence[Tuple[int, int]] = [(b_start, b_end)]

    # 步进段按相邻关系合并后即该侧最大连续覆盖段
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
