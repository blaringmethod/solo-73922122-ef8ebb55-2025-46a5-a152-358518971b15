# 质检影像缺帧核对 API

质检影像缺档时，用于判断缺档该找**采集端**补登记，还是找**影像库**补归档的核对服务。
FastAPI + SQLite，全部以**闭区间号段**为单位存储与计算，帧号可跨十亿且不逐帧展开。

## 业务模型

- **批次**：登记每批影像应有的帧号范围 `[frame_start, frame_end]`，首尾包含。
- **清单**：采集端（`collector`）与影像库（library`）各凭全局唯一的清单号，
  对一个**连续号段**做三种操作：
  - 登记（POST `/manifests`）
  - 更正（PUT `/manifests/{manifest_no}`），只允许改号段，批次/侧别不变
  - 撤回（POST `/manifests/withdraw`），撤回后同侧其他清单覆盖的帧仍算有记录
- **核对**（GET `/batches/{id}/reconcile`）返回四类**最大连续段**：
  `both_present`（双方齐全）、`only_collector`（仅采集端有 → 找影像库补归档）、
  `only_library`（仅影像库有 → 找采集端补登记）、`neither_present`（双方皆缺）。
- **按帧追查**（GET `/batches/{id}/frames/{frame_no}`）：返回该帧状态及两侧命中的清单号。

判定口径：`only_collector` 说明采集端已登记、影像库缺档 → 找影像库补归档；
`only_library` 相反 → 找采集端补登记；`neither_present` 两侧都要补。

## 关键设计

| 需求 | 实现 |
| --- | --- |
| 帧号跨十亿、不逐帧展开 | 帧号以 64 位整数存号段（`seg_start/seg_end` 闭区间），算法复杂度只与号段数有关 |
| 改撤只更新受影响号段 | `coverage` 表缓存每侧已合并覆盖段；写操作只对新旧号段涉及的区间做局部重建（`refresh_region`），不重扫整批清单 |
| 清单与核对结果同时生效 | 每次写操作在单个 `BEGIN IMMEDIATE` 事务内同时更新清单表与覆盖段表，异常即回滚 |
| 越界/入库失败保留原状 | 越界在事务内先校验抛 422；任何错误回滚，已提交数据不变；帧号超 int64 在入参层拦截 |
| 撤回不影响同侧重叠清单 | 覆盖段由全部 `active` 清单实时合并得出，撤回的清单标 `withdrawn` 不参与合并 |
| 最大连续段 | 号段合并时首尾相邻（`start <= prev_end + 1`）即合并，核对结果天然为最大连续段 |

### 三张表

- `batches`：批次号 + 应有帧号范围
- `manifests`：清单号、批次、侧别、状态（active/withdrawn）、号段
- `coverage`：按批次+侧别缓存的合并覆盖段（增量维护）

号段运算见 `app/intervals.py`（合并 / 交集 / 差集，纯函数，零第三方依赖）。

## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务： http://localhost:8000
# 文档： http://localhost:8000/docs   （Swagger UI，可直接演示缺帧核对）
```

SQLite 数据文件在命名卷 `qc-data`（容器内 `/data/qc.db`），WAL 模式。

### 本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/batches` | 登记批次应有帧号范围 |
| GET | `/batches/{batch_id}` | 查看批次 |
| POST | `/manifests` | 登记清单号段（自动越界校验） |
| PUT | `/manifests/{manifest_no}` | 更正号段（局部增量更新） |
| POST | `/manifests/withdraw` | 撤回清单（幂等冲突返回 409） |
| GET | `/manifests/{manifest_no}` | 查看清单 |
| GET | `/batches/{batch_id}/manifests` | 批次清单列表（可按 side 过滤） |
| GET | `/batches/{batch_id}/reconcile` | 四类最大连续段核对结果 |
| GET | `/batches/{batch_id}/frames/{frame_no}` | 按帧追查两侧清单号 |
| GET | `/health` | 健康检查 |

### 快速演示

```bash
python scripts/demo.py            # 默认打 http://127.0.0.1:8000
pytest -q                         # 18 个测试
```

## 错误约定

- `404`：批次/清单不存在
- `409`：批次号/清单号重复、撤回已撤回清单、更正已撤回清单
- `422`：号段非法（起点大于终点）、越出批次范围、帧号超 64 位整数
- 所有写错误都在提交前拦截，库内状态保持调用前原状

## 目录结构

```
app/
  intervals.py   # 号段合并/交集/差集纯函数
  db.py          # SQLite 表结构、事务、覆盖段增量维护、核对查询
  main.py        # FastAPI 路由与入参校验
tests/           # 号段算法 + API 端到端测试
scripts/demo.py  # 标准库演示脚本（容器内可直接跑）
Dockerfile, docker-compose.yml, requirements.txt
```
