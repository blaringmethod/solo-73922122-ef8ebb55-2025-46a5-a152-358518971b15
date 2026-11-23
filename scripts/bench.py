"""改撤读写量自测：无关清单增多时，新旧实现的读写行数对比。

用法：python scripts/bench.py
直接操作 SQLite 临时库，精确统计行数；旧实现的区域重建逻辑按
git 历史中的 refresh_region 复刻（old_refresh_region）。
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.intervals import merge


# ---------- 旧实现（修复前 refresh_region，仅用于对比） ----------

def old_refresh_region(conn, batch_id, side, region_start, region_end):
    lo, hi = region_start - 1, region_end + 1
    old_rows = conn.execute(
        "SELECT seg_start, seg_end FROM coverage "
        "WHERE batch_id=? AND side=? AND seg_start <= ? AND seg_end >= ? "
        "ORDER BY seg_start",
        (batch_id, side, hi, lo),
    ).fetchall()
    if old_rows:
        lo = min(lo, min(r[0] for r in old_rows))
        hi = max(hi, max(r[1] for r in old_rows))
        conn.execute(
            "DELETE FROM coverage WHERE batch_id=? AND side=? "
            "AND seg_start <= ? AND seg_end >= ?",
            (batch_id, side, hi, lo),
        )
    rows = conn.execute(
        "SELECT * FROM manifests WHERE batch_id=? AND side=? AND status='active' "
        "AND seg_start <= ? AND seg_end >= ? ORDER BY seg_start",
        (batch_id, side, hi, lo),
    ).fetchall()
    pieces = [(max(r[4], lo), min(r[5], hi)) for r in rows]
    for seg in merge(pieces):
        conn.execute(
            "INSERT INTO coverage (batch_id, side, seg_start, seg_end) VALUES (?, ?, ?, ?)",
            (batch_id, side, seg[0], seg[1]),
        )
    return lo, hi, len(rows)


def old_index_scanned(conn, batch_id, side, hi):
    """旧清单查询 seg_start<=hi 的索引区间扫描实际读过的行数。"""
    return conn.execute(
        "SELECT COUNT(*) FROM manifests WHERE batch_id=? AND side=? "
        "AND status='active' AND seg_start<=?",
        (batch_id, side, hi),
    ).fetchone()[0]


# ---------- 场景构造 ----------

def fresh_conn(tmp, name):
    path = os.path.join(tmp, name)
    db.init_db(path)
    return db.connect(path)


def make_legacy_coverage(conn):
    """把覆盖表换成修复前的旧结构（无 cnt），用于复刻旧实现。"""
    with db.transaction(conn):
        conn.execute("DROP TABLE coverage")
        conn.execute(
            "CREATE TABLE coverage ("
            " batch_id TEXT, side TEXT, seg_start INTEGER, seg_end INTEGER,"
            " PRIMARY KEY (batch_id, side, seg_start))"
        )


def build_chain(conn, n, target):
    """同侧清单连成一片 [1,n]，另有一张一帧清单 T 压在 target 上。"""
    with db.transaction(conn):
        db.create_batch(conn, "B", 1, max(n, 10**11))
        ms = [(f"M{i}", "B", "collector", i, i) for i in range(1, n + 1)]
        ms.append(("T", "B", "collector", target, target))
        conn.executemany(
            "INSERT INTO manifests (manifest_no, batch_id, side, seg_start, seg_end) "
            "VALUES (?,?,?,?,?)", ms)
        # 旧式覆盖段（已合并）
        conn.execute(
            "INSERT INTO coverage (batch_id, side, seg_start, seg_end) VALUES ('B','collector',1,?)",
            (n,))
    return ms


def build_chain_steps(conn, n, target):
    """同上，但覆盖表为新的步进段结构。"""
    with db.transaction(conn):
        db.create_batch(conn, "B", 1, max(n, 10**11))
        ms = [(f"M{i}", "B", "collector", i, i) for i in range(1, n + 1)]
        ms.append(("T", "B", "collector", target, target))
        conn.executemany(
            "INSERT INTO manifests (manifest_no, batch_id, side, seg_start, seg_end) "
            "VALUES (?,?,?,?,?)", ms)
        conn.executemany(
            "INSERT INTO coverage (batch_id, side, seg_start, seg_end, cnt) "
            "VALUES ('B','collector',?,?,?)",
            [(1, target - 1, 1), (target, target, 2), (target + 1, n, 1)],
        )


def build_disjoint(conn, n):
    """n 张互不相交的一帧清单。"""
    with db.transaction(conn):
        db.create_batch(conn, "B", 1, 10 * n + 10)
        conn.executemany(
            "INSERT INTO manifests (manifest_no, batch_id, side, seg_start, seg_end) "
            "VALUES (?,?,?,?,?)",
            [(f"M{i}", "B", "collector", 10 * i + 10, 10 * i + 10) for i in range(n)])


def build_disjoint_steps(conn, n):
    with db.transaction(conn):
        db.create_batch(conn, "B", 1, 10 * n + 10)
        conn.executemany(
            "INSERT INTO manifests (manifest_no, batch_id, side, seg_start, seg_end) "
            "VALUES (?,?,?,?,?)",
            [(f"M{i}", "B", "collector", 10 * i + 10, 10 * i + 10) for i in range(n)])
        conn.executemany(
            "INSERT INTO coverage (batch_id, side, seg_start, seg_end, cnt) "
            "VALUES ('B','collector',?,?,1)",
            [(10 * i + 10, 10 * i + 10) for i in range(n)])


def line(scenario, n, old_rows, old_ms, new_stats, new_ms):
    return (f"| {scenario} | {n:,} | {old_rows:,} | {old_ms:,.1f} | "
            f"{new_stats['rows_read']} | {new_stats['rows_written']} | {new_ms:,.2f} |")


def main():
    print("| 场景 | 批内清单数 | 旧实现读清单行数 | 旧耗时 ms | 新实现读行数 | 新实现写行数 | 新耗时 ms |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    tmp = tempfile.mkdtemp(prefix="qc-bench-")
    for n in (10_000, 100_000):
        target = n // 2

        # 场景 A：同侧清单连成一片，撤回一帧清单
        conn = fresh_conn(tmp, f"a-old-{n}.db")
        make_legacy_coverage(conn)
        build_chain(conn, n, target)
        t0 = time.perf_counter()
        with db.transaction(conn):
            db.mark_manifest_withdrawn(conn, "T")
            lo, hi, _ = old_refresh_region(conn, "B", "collector", target, target)
        old_ms = (time.perf_counter() - t0) * 1000
        old_rows = old_index_scanned(conn, "B", "collector", hi)
        conn.close()

        conn = fresh_conn(tmp, f"a-new-{n}.db")
        build_chain_steps(conn, n, target)
        t0 = time.perf_counter()
        with db.transaction(conn):
            db.mark_manifest_withdrawn(conn, "T")
            stats = db.coverage_remove(conn, "B", "collector", target, target)
        new_ms = (time.perf_counter() - t0) * 1000
        conn.close()
        print(line("A 撤回一帧(连片)", n, old_rows, old_ms, stats, new_ms))

        # 场景 B：许多不相交清单排在前面，撤回末尾一帧清单
        conn = fresh_conn(tmp, f"b-old-{n}.db")
        make_legacy_coverage(conn)
        build_disjoint(conn, n)
        conn.execute(
            "INSERT INTO coverage (batch_id, side, seg_start, seg_end) "
            "SELECT 'B','collector',seg_start,seg_end FROM manifests")
        conn.commit()
        last = 10 * (n - 1) + 10
        t0 = time.perf_counter()
        with db.transaction(conn):
            db.mark_manifest_withdrawn(conn, f"M{n - 1}")
            lo, hi, _ = old_refresh_region(conn, "B", "collector", last, last)
        old_ms = (time.perf_counter() - t0) * 1000
        old_rows = old_index_scanned(conn, "B", "collector", hi)
        conn.close()

        conn = fresh_conn(tmp, f"b-new-{n}.db")
        build_disjoint_steps(conn, n)
        t0 = time.perf_counter()
        with db.transaction(conn):
            db.mark_manifest_withdrawn(conn, f"M{n - 1}")
            stats = db.coverage_remove(conn, "B", "collector", last, last)
        new_ms = (time.perf_counter() - t0) * 1000
        conn.close()
        print(line("B 撤回一帧(前面不相交)", n, old_rows, old_ms, stats, new_ms))

        # 场景 C：清单移到远处，两段之间隔着连片清单
        conn = fresh_conn(tmp, f"c-old-{n}.db")
        make_legacy_coverage(conn)
        build_chain(conn, n, target)
        t0 = time.perf_counter()
        with db.transaction(conn):
            db.update_manifest_segment(conn, "T", 10**11, 10**11)
            for s, e in merge([(target, target), (10**11, 10**11)]):
                old_refresh_region(conn, "B", "collector", s, e)
        old_ms = (time.perf_counter() - t0) * 1000
        # 旧实现两次区域重建的清单扫描量（各自 seg_start<=hi 全扫）
        old_rows = old_index_scanned(conn, "B", "collector", n) + \
            old_index_scanned(conn, "B", "collector", 10**11 + 1)
        conn.close()

        conn = fresh_conn(tmp, f"c-new-{n}.db")
        build_chain_steps(conn, n, target)
        t0 = time.perf_counter()
        with db.transaction(conn):
            st1 = db.coverage_remove(conn, "B", "collector", target, target)
            db.update_manifest_segment(conn, "T", 10**11, 10**11)
            st2 = db.coverage_add(conn, "B", "collector", 10**11, 10**11)
            stats = {k: st1[k] + st2[k] for k in st1}
        new_ms = (time.perf_counter() - t0) * 1000
        conn.close()
        print(line("C 更正移到远处", n, old_rows, old_ms, stats, new_ms))


if __name__ == "__main__":
    main()
