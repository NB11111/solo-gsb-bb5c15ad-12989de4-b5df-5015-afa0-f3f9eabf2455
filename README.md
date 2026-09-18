# 仪器校准漂移补偿 API

基于 **FastAPI + SQLite** 的仪器校准服务：客户端在各时点提交参考值与同点多次读数，服务标记异常读数、计算校准偏差，并在相邻时点间线性插值偏差，对待校正读数施加漂移补偿。

## 快速开始（单条命令）

```bash
docker compose up --build
```

- **API 地址：http://localhost:8000**
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

SQLite 数据落在命名卷 `calibration-data`（容器内 `/data/calibration.db`），容器重启数据不丢。

## 校准规则

1. **观测登记**：每次提交参考值 `reference`、容差 `tolerance`、同一时点 `timestamp` 的至少 3 次读数。
2. **异常判定**：读数偏离该组**中位数**超过 `tolerance` 即标记为异常（原始读数仍完整保留）。
3. **校准偏差**：`bias = reference − 有效读数（剔除异常后）的中位数`。
4. **漂移补偿**：在相邻观测时点间对 `bias` 做**线性插值**，`校正读数 = 原始读数 + 插值偏差`。
5. **区间重算**：新增观测只重算与其相邻的区间（旧区间被新时点拆分）；修订观测则在单个事务内重建原位置与新位置受影响的相邻区间，历史区间与原始数据不变。
6. **乐观并发**：每次修订必须携带当前 `revision`，版本过期或目标时点冲突均返回 409；成功后版本号 +1，观测 ID 不变，修订前内容存入修订历史。
7. **覆盖范围**：查询时点超出 `[最早观测, 最晚观测]` 时拒绝（HTTP 400）；恰落在观测点上时直接使用该点偏差。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/observations` | 登记校准观测，返回偏差与异常明细（201） |
| GET | `/observations` | 列出全部观测（含原始读数、异常标记与当前 revision） |
| PUT | `/observations/{id}` | 修订观测（携带当前 revision），原 ID 不变（200） |
| GET | `/observations/{id}/revisions` | 按版本倒序查看修订前快照 |
| GET | `/intervals` | 列出当前插值区间 |
| POST | `/corrections` | 对指定时点的读数做漂移补偿 |
| GET | `/health` | 健康检查 |

时间戳支持 Unix 秒（数字）或 ISO 8601 字符串（如 `2026-09-18T08:00:00Z`），缺省为服务器当前时间。

### 登记观测

```bash
curl -X POST http://localhost:8000/observations \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T08:00:00Z", "reference": 100.0,
       "tolerance": 0.5, "readings": [99.0, 99.2, 98.8, 150.0]}'
```

响应（`150.0` 偏离组中位数 `99.1` 超差，判为异常；`bias = 100.0 − 99.0 = 1.0`）：

```json
{
  "id": 1, "timestamp": 1789718400.0, "timestamp_iso": "2026-09-18T08:00:00+00:00",
  "reference": 100.0, "tolerance": 0.5, "group_median": 99.1, "bias": 1.0, "revision": 1,
  "readings": [
    {"value": 99.0, "is_outlier": false}, {"value": 99.2, "is_outlier": false},
    {"value": 98.8, "is_outlier": false}, {"value": 150.0, "is_outlier": true}
  ],
  "outliers": [150.0]
}
```

### 修订观测

修订请求沿用 `reference` / `tolerance` / `readings` / `timestamp` 字段，并额外携带客户端持有的当前 `revision`（首次登记响应中的版本号，从 1 开始）。服务在**单个事务**内完成：保存修订前快照、用新输入重新判定异常值并重算偏差、重建原位置与新位置受影响的相邻插值区间；观测 **ID 保持不变**，成功后 `revision` 加 1。

`timestamp` 可以改到新时点（移动观测），也可以保持原值（就地修订）。

```bash
curl -X PUT http://localhost:8000/observations/1 \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T08:00:00Z", "reference": 100.5,
       "tolerance": 0.5, "readings": [99.0, 99.2, 98.8], "revision": 1}'
```

响应为修订后的观测（结构同登记响应），此时 `revision` 为 `2`，`id` 仍为 `1`：

```json
{
  "id": 1, "timestamp": 1789718400.0, "timestamp_iso": "2026-09-18T08:00:00+00:00",
  "reference": 100.5, "tolerance": 0.5, "group_median": 99.0, "bias": 1.5, "revision": 2,
  "readings": [
    {"value": 99.0, "is_outlier": false}, {"value": 99.2, "is_outlier": false},
    {"value": 98.8, "is_outlier": false}
  ],
  "outliers": []
}
```

冲突场景均返回 **409**：携带的 `revision` 已过期（被其他修订抢先），或目标 `timestamp` 已被其他观测占用。观测不存在返回 **404**；输入不合法（读数不足 3 次、全部被判异常等）返回 **422**。

### 查看修订历史

每次成功修订都会保存一份**修订前快照**。按版本倒序（新的在前）返回变更时间、旧输入（参考值、容差、读数与当时的异常标记、时点）以及旧偏差：

```bash
curl http://localhost:8000/observations/1/revisions
```

```json
[
  {
    "revision": 1,
    "changed_at": 1789719000.0,
    "changed_at_iso": "2026-09-18T08:10:00+00:00",
    "old_timestamp": 1789718400.0,
    "old_timestamp_iso": "2026-09-18T08:00:00+00:00",
    "old_reference": 100.0, "old_tolerance": 0.5, "old_group_median": 99.1,
    "old_bias": 1.0,
    "old_readings": [
      {"value": 99.0, "is_outlier": false}, {"value": 99.2, "is_outlier": false},
      {"value": 98.8, "is_outlier": false}, {"value": 150.0, "is_outlier": true}
    ]
  }
]
```

从未被修订过的观测返回空数组 `[]`。

### 漂移补偿

```bash
curl -X POST http://localhost:8000/corrections \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T09:00:00Z", "reading": 98.0}'
```

响应包含校正值、所用区间及两端点观测的异常明细：

```json
{
  "timestamp": 1789722000.0, "timestamp_iso": "2026-09-18T09:00:00+00:00",
  "reading": 98.0, "bias": 2.0, "corrected": 100.0,
  "interval": {"t_start": 1789718400.0, "t_end": 1789725600.0,
               "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
               "bias_start": 1.0, "bias_end": 3.0},
  "endpoints": [
    {"observation_id": 1, "timestamp": 1789718400.0, "timestamp_iso": "2026-09-18T08:00:00+00:00",
     "bias": 1.0, "outliers": [150.0]},
    {"observation_id": 2, "timestamp": 1789725600.0, "timestamp_iso": "2026-09-18T10:00:00+00:00",
     "bias": 3.0, "outliers": []}
  ]
}
```

### 错误码

| 状态码 | 场景 |
| --- | --- |
| 400 | 查询时点超出校准覆盖范围（响应含可用区间） |
| 404 | 修订或查询的观测 ID 不存在 |
| 409 | 同一时点重复登记观测；修订时 revision 过期或目标时点被其他观测占用 |
| 422 | 读数不足 3 次、全部读数被判异常、参数非法 |

## 本地开发

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q          # 运行测试
uvicorn app.main:app --reload       # 本地启动（默认 ./calibration.db）
```

环境变量 `CALIB_DB_PATH` 可覆盖 SQLite 文件路径（容器内默认 `/data/calibration.db`）。

## 项目结构

```
app/
  main.py         # FastAPI 路由
  service.py      # 观测入库、相邻区间重算、补偿查询
  calibration.py  # 纯计算：异常判定、偏差、线性插值
  models.py       # Pydantic 请求/响应模型
  db.py           # SQLite 连接与建表
tests/test_api.py # 端到端 API 测试
Dockerfile / docker-compose.yml
```
