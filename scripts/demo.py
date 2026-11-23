"""缺帧核对关键操作演示脚本（仅用标准库，容器内可直接运行）。

用法：python scripts/demo.py [base_url]
默认 http://127.0.0.1:8000
"""

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, payload=None, expect=(200, 201)):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
            ok = resp.status in expect
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode())
        ok = False
        resp_status = e.code
    else:
        resp_status = resp.status
    flag = "OK " if ok else "ERR"
    print(f"[{flag}] {method} {path} -> {resp_status}")
    print("      " + json.dumps(body, ensure_ascii=False))
    return ok, body


def main() -> int:
    print(f"== 演示目标：{BASE} ==")
    call("GET", "/health")

    # 1. 登记批次应有帧号范围 1..100（首尾包含）
    call("POST", "/batches", {"batch_id": "B-DEMO", "frame_start": 1, "frame_end": 100})

    # 2. 采集端登记两个相邻号段（应合并为最大连续段）
    call("POST", "/manifests", {
        "manifest_no": "C-001", "batch_id": "B-DEMO", "side": "collector",
        "seg_start": 1, "seg_end": 40})
    call("POST", "/manifests", {
        "manifest_no": "C-002", "batch_id": "B-DEMO", "side": "collector",
        "seg_start": 41, "seg_end": 60})
    # 3. 影像库登记号段
    call("POST", "/manifests", {
        "manifest_no": "L-001", "batch_id": "B-DEMO", "side": "library",
        "seg_start": 21, "seg_end": 70})

    # 4. 按批核对四类最大连续段
    call("GET", "/batches/B-DEMO/reconcile")

    # 5. 按帧追查两侧清单号
    call("GET", "/batches/B-DEMO/frames/27")
    call("GET", "/batches/B-DEMO/frames/65")

    # 6. 更正清单 C-001：1-40 -> 5-40，只更新受影响号段
    call("PUT", "/manifests/C-001", {"seg_start": 5, "seg_end": 40})
    call("GET", "/batches/B-DEMO/reconcile")

    # 7. 撤回 C-002：同侧 C-001 覆盖的帧仍算采集端有记录
    call("POST", "/manifests/withdraw", {"manifest_no": "C-002"})
    call("GET", "/batches/B-DEMO/reconcile")
    call("GET", "/batches/B-DEMO/frames/45")

    # 8. 越界登记必须被拒，数据保持原状
    call("POST", "/manifests", {
        "manifest_no": "X-BAD", "batch_id": "B-DEMO", "side": "collector",
        "seg_start": 90, "seg_end": 120})
    call("GET", "/batches/B-DEMO/reconcile")

    # 9. 十亿级号段：存储与计算均不逐帧展开
    call("POST", "/batches", {
        "batch_id": "B-BIG", "frame_start": 0, "frame_end": 2_000_000_000})
    call("POST", "/manifests", {
        "manifest_no": "BC-1", "batch_id": "B-BIG", "side": "collector",
        "seg_start": 0, "seg_end": 1_999_999_999})
    call("POST", "/manifests", {
        "manifest_no": "BL-1", "batch_id": "B-BIG", "side": "library",
        "seg_start": 1, "seg_end": 2_000_000_000})
    call("GET", "/batches/B-BIG/reconcile")
    print("== 演示完成 ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
