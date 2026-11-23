"""纯号段算法测试。"""

from app.intervals import intersect, merge, subtract


def test_merge_overlap_and_adjacent():
    assert merge([(1, 3), (4, 5), (10, 12), (11, 20)]) == [(1, 5), (10, 20)]
    assert merge([]) == []
    assert merge([(5, 5)]) == [(5, 5)]


def test_intersect():
    a = [(1, 10), (20, 30)]
    b = [(5, 20), (25, 26)]
    assert intersect(a, b) == [(5, 10), (20, 20), (25, 26)]
    assert intersect(a, [(100, 200)]) == []


def test_subtract_basic():
    a = [(1, 100)]
    b = [(10, 20), (30, 40), (90, 100)]
    assert subtract(a, b) == [(1, 9), (21, 29), (41, 89)]


def test_subtract_multi_segment():
    a = [(1, 10), (20, 30), (40, 50)]
    b = [(5, 22), (45, 60)]
    assert subtract(a, b) == [(1, 4), (23, 30), (40, 44)]


def test_subtract_b_covers_a():
    assert subtract([(10, 20)], [(1, 30)]) == []
    assert subtract([(10, 20)], []) == [(10, 20)]


def test_subtract_touching_edges():
    assert subtract([(1, 10)], [(1, 5)]) == [(6, 10)]
    assert subtract([(1, 10)], [(10, 10)]) == [(1, 9)]
    assert subtract([(1, 10)], [(1, 1), (10, 10)]) == [(2, 9)]


def test_big_integers_no_expansion():
    big = 10**12
    a = merge([(0, big)])
    b = merge([(big // 2, big // 2 + 1)])
    assert subtract(a, b) == [(0, big // 2 - 1), (big // 2 + 2, big)]
