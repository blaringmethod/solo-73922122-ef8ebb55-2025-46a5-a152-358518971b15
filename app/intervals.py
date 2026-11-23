"""闭区间整数号段的纯函数运算。

所有号段均为 ``(start, end)`` 闭区间，帧号可以是任意 Python 整数
（可远超十亿），运算只与号段数量有关，绝不逐帧展开。
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

Segment = Tuple[int, int]


def merge(segments: Iterable[Segment]) -> List[Segment]:
    """合并互相重叠或首尾相邻的号段，返回按起点排序的最大连续段列表。"""
    ordered = sorted(segments)
    merged: List[Segment] = []
    for start, end in ordered:
        if start > end:
            raise ValueError(f"非法号段: ({start}, {end})")
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        elif end > merged[-1][1]:
            prev_start, _ = merged[-1]
            merged[-1] = (prev_start, end)
    return merged


def intersect(a: Sequence[Segment], b: Sequence[Segment]) -> List[Segment]:
    """两个已合并号段列表的交集，结果仍为最大连续段。"""
    i = j = 0
    out: List[Segment] = []
    while i < len(a) and j < len(b):
        start = max(a[i][0], b[j][0])
        end = min(a[i][1], b[j][1])
        if start <= end:
            out.append((start, end))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def subtract(a: Sequence[Segment], b: Sequence[Segment]) -> List[Segment]:
    """号段集合差 ``a - b``，两个入参均需已合并，结果为最大连续段。"""
    out: List[Segment] = []
    skip = 0
    for start, end in a:
        cur = start
        # 跳过完全落在当前段之前的 b 号段
        while skip < len(b) and b[skip][1] < cur:
            skip += 1
        k = skip
        while k < len(b) and b[k][0] <= end:
            b_start, b_end = b[k]
            if b_start > cur:
                out.append((cur, b_start - 1))
            if b_end >= cur:
                cur = b_end + 1
            if cur > end:
                break
            k += 1
        if cur <= end:
            out.append((cur, end))
    return out
