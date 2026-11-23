"""覆盖步进段维护的正确性与读写量上界测试（直接打 db 层，统计精确到行）。"""

import random

import pytest

import app.db as db


@pytest.fixture()
def conn(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_db(path)
    c = db.connect(path)
    yield c
    c.close()


def _batch(conn, bid="B", start=1, end=10**12):
    db.create_batch(conn, bid, start, end)


def _register(conn, no, bid, side, s, e):
    """模拟登记路径：插清单 + 覆盖 +1，返回覆盖维护的读写统计。"""
    db.insert_manifest(conn, no, bid, side, s, e)
    return db.coverage_add(conn, bid, side, s, e)


def _withdraw(conn, no):
    """模拟撤回路径：标记撤回 + 覆盖 -1，返回覆盖维护的读写统计。"""
    row = db.get_manifest(conn, no)
    db.mark_manifest_withdrawn(conn, no)
    return db.coverage_remove(conn, row["batch_id"], row["side"], row["seg_start"], row["seg_end"])


def _correct(conn, no, s, e):
    """模拟更正路径：旧段 -1、新段 +1，返回两次维护合计的读写统计。"""
    row = db.get_manifest(conn, no)
    st1 = db.coverage_remove(conn, row["batch_id"], row["side"], row["seg_start"], row["seg_end"])
    db.update_manifest_segment(conn, no, s, e)
    st2 = db.coverage_add(conn, row["batch_id"], row["side"], s, e)
    return {k: st1[k] + st2[k] for k in st1}


def _merged_coverage(conn, bid, side):
    from app.intervals import merge
    return merge(db.coverage_segments(conn, bid, side))


# ---------- 读写量上界：无关清单增多时保持常数 ----------

@pytest.mark.parametrize("n", [1_000, 50_000])
def test_withdraw_one_frame_from_contiguous_chain_io_constant(conn, n):
    """同侧清单连成一片：撤回一帧清单，读写量与批次规模无关。"""
    with db.transaction(conn):
        _batch(conn, end=n)
        for i in range(0, n, 100):  # [1,100],[101,200],... 连成 [1,n]
            _register(conn, f"M{i}", "B", "collector", i + 1, i + 100)
        _register(conn, "T", "B", "collector", n // 2, n // 2)  # 只登记一帧
        stats = _withdraw(conn, "T")
    # 窗口只含目标帧所在的步进段及其边界：读 <= 5 行，写 <= 3 行
    assert stats["rows_read"] <= 5, stats
    assert stats["rows_written"] <= 3, stats
    # 同侧链式清单仍覆盖该帧，继续算有记录
    assert _merged_coverage(conn, "B", "collector") == [(1, n)]


@pytest.mark.parametrize("n", [1_000, 50_000])
def test_withdraw_sole_cover_frame_makes_hole_io_constant(conn, n):
    """一帧清单是该帧唯一覆盖：撤回后出现空洞，读写量仍为常数。"""
    half = n // 2
    with db.transaction(conn):
        _batch(conn, end=n)
        _register(conn, "A", "B", "collector", 1, half - 1)
        _register(conn, "B1", "B", "collector", half + 1, n)
        _register(conn, "T", "B", "collector", half, half)  # 唯一覆盖 half 帧
        assert _merged_coverage(conn, "B", "collector") == [(1, n)]
        stats = _withdraw(conn, "T")
    assert stats["rows_read"] <= 5, stats
    assert stats["rows_written"] <= 3, stats
    assert _merged_coverage(conn, "B", "collector") == [(1, half - 1), (half + 1, n)]


@pytest.mark.parametrize("n", [1_000, 50_000])
def test_disjoint_manifests_ahead_are_skipped(conn, n):
    """许多不相交清单排在前面：撤回末尾清单不读前面的记录。"""
    with db.transaction(conn):
        _batch(conn, end=10 * n + 10)
        for i in range(n):  # 互不相交：[10,11],[20,21],...
            _register(conn, f"M{i}", "B", "collector", 10 * i + 10, 10 * i + 11)
        stats = _withdraw(conn, f"M{n - 1}")  # 撤回最后一张（一帧）
    assert stats["rows_read"] <= 3, stats
    assert stats["rows_written"] <= 2, stats
    assert _merged_coverage(conn, "B", "collector") == [
        (10 * i + 10, 10 * i + 11) for i in range(n - 1)
    ]


@pytest.mark.parametrize("n", [1_000, 50_000])
def test_correction_far_move_skips_gap(conn, n):
    """清单移到远处：跳过两段之间无关的记录。"""
    with db.transaction(conn):
        _batch(conn, end=10**12)
        for i in range(n):  # 新旧号段之间隔着 n 张无关清单
            _register(conn, f"G{i}", "B", "collector", 1000 + 10 * i, 1001 + 10 * i)
        _register(conn, "T", "B", "collector", 5, 5)
        stats = _correct(conn, "T", 10**11, 10**11)  # 移到远处
    # 只触及旧段、新段各自窗口；中间的 n 张清单一行都不读
    assert stats["rows_read"] <= 6, stats
    assert stats["rows_written"] <= 4, stats
    cov = _merged_coverage(conn, "B", "collector")
    assert (5, 5) not in cov and cov[-1] == (10**11, 10**11)
    assert len(cov) == n + 1  # 无关清单的段原样保留


def test_register_into_empty_region_io_constant(conn):
    """在空处登记也只触及边界段。"""
    with db.transaction(conn):
        _batch(conn)
        for i in range(2_000):
            _register(conn, f"M{i}", "B", "collector", 10 * i + 10, 10 * i + 11)
        stats = _register(conn, "NEW", "B", "collector", 5, 5)  # 插到最前面的空档
    assert stats["rows_read"] <= 2, stats
    assert stats["rows_written"] <= 1, stats


# ---------- 覆盖正确性 ----------

def test_overlapping_manifests_keep_frames_covered(conn):
    """同侧仍有有效清单覆盖的帧继续算有记录。"""
    with db.transaction(conn):
        _batch(conn, end=100)
        _register(conn, "A", "B", "collector", 1, 50)
        _register(conn, "B1", "B", "collector", 30, 100)
        _register(conn, "C", "B", "library", 1, 100)
        _withdraw(conn, "A")
    r = db.reconcile(conn, "B")
    assert r["both_present"] == [{"frame_start": 30, "frame_end": 100}]
    assert r["only_library"] == [{"frame_start": 1, "frame_end": 29}]
    assert r["only_collector"] == []


def test_merge_boundary_on_add(conn):
    """+1 使相邻段计数相同时必须并段（步进段保持最简）。"""
    with db.transaction(conn):
        _batch(conn, end=100)
        _register(conn, "A", "B", "collector", 1, 10)   # [1,10]x1
        _register(conn, "B1", "B", "collector", 1, 5)   # [1,5]x2 [6,10]x1
        _register(conn, "C", "B", "collector", 6, 10)   # [6,10] 变 x2 -> 与左侧并段
        rows = conn.execute(
            "SELECT seg_start, seg_end, cnt FROM coverage WHERE batch_id='B' ORDER BY seg_start"
        ).fetchall()
    assert [(r["seg_start"], r["seg_end"], r["cnt"]) for r in rows] == [(1, 10, 2)]


def test_split_and_shrink_on_remove(conn):
    """-1 把一段劈开并删除零计数段。"""
    with db.transaction(conn):
        _batch(conn, end=100)
        _register(conn, "A", "B", "collector", 1, 100)
        _register(conn, "T", "B", "collector", 40, 60)
        _withdraw(conn, "T")
        rows = conn.execute(
            "SELECT seg_start, seg_end, cnt FROM coverage WHERE batch_id='B' ORDER BY seg_start"
        ).fetchall()
    assert [(r["seg_start"], r["seg_end"], r["cnt"]) for r in rows] == [(1, 100, 1)]


def test_remove_uncovered_region_raises_and_rolls_back(conn):
    """数据不一致时抛错，事务回滚保持原状。"""
    with db.transaction(conn):
        _batch(conn, end=100)
        _register(conn, "A", "B", "collector", 1, 10)
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            db.coverage_remove(conn, "B", "collector", 50, 60)  # 无覆盖处 -1
    # 回滚后覆盖不变
    assert _merged_coverage(conn, "B", "collector") == [(1, 10)]


def test_random_ops_match_naive_model(conn):
    """随机登记/更正/撤回，与逐帧朴素模型对比四类最大连续段。"""
    from app.intervals import merge as imerge

    rng = random.Random(20261123)
    END = 300
    model = {"collector": {}, "library": {}}  # manifest_no -> (s, e) 仅有效清单

    def naive(side):
        covered = set()
        for s, e in model[side].values():
            covered.update(range(s, e + 1))
        return covered

    def naive_reconcile():
        c, l = naive("collector"), naive("library")
        def segs(frames):
            return [{"frame_start": s, "frame_end": e} for s, e in imerge(
                [(f, f) for f in sorted(frames)])]
        return {
            "both_present": segs(c & l),
            "only_collector": segs(c - l),
            "only_library": segs(l - c),
            "neither_present": segs(set(range(1, END + 1)) - c - l),
        }

    with db.transaction(conn):
        _batch(conn, end=END)
        nos = []
        for step in range(400):
            op = rng.choice(["reg", "reg", "correct", "withdraw"])
            side = rng.choice(["collector", "library"])
            s = rng.randint(1, END)
            e = min(END, s + rng.randint(0, 40))
            if op == "reg":
                no = f"M{step}"
                _register(conn, no, "B", side, s, e)
                model[side][no] = (s, e)
                nos.append(no)
            elif op == "correct" and nos:
                no = rng.choice(nos)
                if no in model["collector"] or no in model["library"]:
                    sd = "collector" if no in model["collector"] else "library"
                    _correct(conn, no, s, e)
                    model[sd][no] = (s, e)
            elif op == "withdraw" and nos:
                no = rng.choice(nos)
                sd = "collector" if no in model["collector"] else (
                    "library" if no in model["library"] else None)
                if sd:
                    _withdraw(conn, no)
                    del model[sd][no]
            got = db.reconcile(conn, "B")
            want = naive_reconcile()
            for key in want:
                assert got[key] == want[key], f"step={step} key={key}\n{got[key]}\n{want[key]}"
        # 每个帧的追查结果也与模型一致
        for f in range(1, END + 1, 17):
            t = db.trace_frame(conn, "B", f)
            in_c = any(s <= f <= e for s, e in model["collector"].values())
            in_l = any(s <= f <= e for s, e in model["library"].values())
            want_status = (
                "both_present" if in_c and in_l else
                "only_collector" if in_c else
                "only_library" if in_l else "neither_present"
            )
            assert t["status"] == want_status, f"frame={f}"


def test_int64_edges_no_overflow(conn):
    """int64 两端边缘：e+1 / s-1 中间值不出库、不溢出。"""
    top = db.INT64_MAX
    with db.transaction(conn):
        _batch(conn, bid="E", start=-(2**63), end=top)
        _register(conn, "A", "E", "collector", top - 9, top)   # 顶到 INT64_MAX
        _register(conn, "B1", "E", "collector", -(2**63), -(2**63) + 9)  # 抵到 INT64_MIN
        _withdraw(conn, "A")
        assert _merged_coverage(conn, "E", "collector") == [(-(2**63), -(2**63) + 9)]
        _register(conn, "C", "E", "collector", top - 4, top)
        assert _merged_coverage(conn, "E", "collector") == [
            (-(2**63), -(2**63) + 9), (top - 4, top)]
