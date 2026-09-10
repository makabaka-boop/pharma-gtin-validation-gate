# 药品包装码收货校验服务

Python 3.12 + FastAPI 纯后端服务。收货扫描批量提交包装码，服务对**原始字符串**逐件判定，
不做任何去空白、去连字符或全角转半角处理，避免“只看十四位数字”导致的录入差错流入追溯链路。

## 规则

1. 请求体必须是 **1～100 个字符串**组成的 JSON 数组。
2. 每项必须**恰为 14 个 ASCII 数字**（`0`–`9`）。
   - 首尾或内部空白、连字符 `-`、全角数字（`０`–`９`）及其他字符一律不转换、不剥离，
     记为 `format_error`。
   - 长度少于或多于 14 位同样记为 `format_error`，此时计算校验位为 `null`。
3. 格式合法的代码按 GTIN-14 规则复算校验位：
   - 前 13 位**从左到右**依次乘权重 `3, 1, 3, 1, …`（奇数位 ×3，偶数位 ×1）；
   - 校验位固定为 `(10 - (加权和 % 10)) % 10`
     （末尾再取模 10，使加权和为 10 的整倍数时校验位是 `0` 而不是 `10`）；
   - 末位等于复算值记 `valid`，否则记 `checksum_mismatch`。
4. 结果**保留输入顺序和重复项**，逐件返回原值、计算出的校验位和状态。
5. 空数组、超过 100 项、成员不是字符串、请求体不是数组、JSON 损坏或缺失：
   整体返回 **422**，由 Pydantic 给出结构化错误，且**不返回部分 `results`**。
6. 结构合法的请求即使包含（甚至全是）无效代码，也返回 **200**。

## 收货对账规则

仓库先按采购单创建收货单，再分批提交扫描码。服务在逐码校验的同时累计实收数量：

1. `POST /receipts` 创建收货单：提交唯一业务单号 `order_no` 与至少一条明细，
   每条明细为**合法 GTIN-14 + 正整数计划量**（上限为 SQLite 有符号 64 位整数
   最大值 `2^63 - 1`）。
   - 明细 GTIN 校验位不符或格式非法、同一请求中 GTIN 重复、计划量不是正整数
     （`0`、负数、`true`、`1.5`、`"2"` 等）或超出 64 位整数范围：**整体 422**，
     不留任何残单。
   - 业务单号含 `/`：**422**（单号须能作为单个 URL 路径段寻址，否则建单后
     无法查询或扫描）。
   - 业务单号比较时忽略首尾空白：`PO-1` 与 `  PO-1  ` 是同一收货单，查询与
     扫描按同一单号寻址；视觉相同的空白变体按重复单号处理：**409**。
2. `POST /receipts/{order_no}/scans` 向收货单提交 1～100 个扫描码（裸数组，边界与
   `/codes/verify` 相同）。响应在逐码结论之外，对每个**校验通过**的代码追加
   `reconciliation` 对账结论，并在**同一 SQLite 事务中按输入顺序**递增实收量：
   - `matched`：该 GTIN 在采购计划内，递增后实收量 ≤ 计划量；
   - `excess`：计划内但递增后实收量 > 计划量（超收）；
   - `unplanned`：GTIN 不在采购计划内（计划外商品，`planned_qty` 为 `null`，
     再次扫描仍计为 unplanned 并继续累计）。
3. `format_error` / `checksum_mismatch` 的项目**不入账、不产生对账结论**
   （`reconciliation` 为 `null`），原逐码校验结果仍按顺序完整返回。
4. 不存在的收货单（建单、扫描、查询）返回 **404**。
5. 扫描期间任何存储失败都会**回滚整批计数**并返回 **503**（无部分 `results`）；
   重试同一批得到确定结果——就像失败的请求从未发生。
6. 应用启动时以 `CREATE TABLE IF NOT EXISTS` 幂等创建 `receipts` 与
   `receipt_items` 两张表，对已有数据库文件重复启动安全。数据库路径由
   `RECEIPT_DB_PATH` 环境变量覆盖（默认工作目录下 `receipts.db`）。

## 可复算示例

有效代码 `07300040109316`（校验位应为 6）：

| 位置 i（1-13） | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 数字 dᵢ | 0 | 7 | 3 | 0 | 0 | 0 | 4 | 0 | 1 | 0 | 9 | 3 | 1 |
| 权重（奇 3 偶 1） | 3 | 1 | 3 | 1 | 3 | 1 | 3 | 1 | 3 | 1 | 3 | 1 | 3 |
| 乘积 | 0 | 7 | 9 | 0 | 0 | 0 | 12 | 0 | 3 | 0 | 27 | 3 | 3 |

加权和 = 0+7+9+0+0+0+12+0+3+0+27+3+3 = **64**
校验位 = `(10 - (64 mod 10)) mod 10` = `(10 - 4) mod 10` = **6**，与末位一致 → `valid`。

把末位改成 `0`（`07300040109310`），复算校验位仍是 **6**，与末位不一致 → `checksum_mismatch`。
边界例：`00000000000000` 的加权和为 0，校验位 = `(10 - 0) % 10` = **0** → `valid`。

## 接口

### `POST /codes/verify`

请求：

```json
[
  "07300040109316",
  "07300040109316",
  "07300040109310",
  "0730004010-9316",
  " 07300040109316",
  "０７３０００４０１０９３１６",
  "0730004010931"
]
```

响应 `200`（顺序、重复项原样保留）：

```json
{
  "results": [
    {"code": "07300040109316", "calculated_check_digit": 6, "status": "valid"},
    {"code": "07300040109316", "calculated_check_digit": 6, "status": "valid"},
    {"code": "07300040109310", "calculated_check_digit": 6, "status": "checksum_mismatch"},
    {"code": "0730004010-9316", "calculated_check_digit": null, "status": "format_error"},
    {"code": " 07300040109316", "calculated_check_digit": null, "status": "format_error"},
    {"code": "０７３０００４０１０９３１６", "calculated_check_digit": null, "status": "format_error"},
    {"code": "0730004010931", "calculated_check_digit": null, "status": "format_error"}
  ]
}
```

结构非法时返回 `422`，无 `results` 字段，Pydantic 结构化错误位于 `detail`，例如：

```json
{
  "detail": [
    {
      "type": "too_short",
      "loc": ["body"],
      "msg": "Array should have at least 1 item after validation, not 0"
    }
  ]
}
```

`type` 取值包括 `too_short`（空数组）、`too_long`（超过 100 项）、`string_type`
（成员非字符串或请求体不是数组）、`json_invalid`（JSON 损坏）等。

### `GET /health`

返回 `{"status": "ok"}`，供容器健康检查使用。

### `POST /receipts`

创建收货单。请求：

```json
{
  "order_no": "PO-2026-0001",
  "items": [
    {"gtin": "07300040109316", "planned_qty": 2},
    {"gtin": "00000000000000", "planned_qty": 1}
  ]
}
```

成功 `201`（计划行初始实收量均为 0）：

```json
{
  "order_no": "PO-2026-0001",
  "items": [
    {"gtin": "07300040109316", "planned_qty": 2, "received_qty": 0},
    {"gtin": "00000000000000", "planned_qty": 1, "received_qty": 0}
  ]
}
```

业务单号重复（含首尾空白变体）返回 `409`；非法 GTIN、重复 GTIN、非正整数或
超出 64 位整数范围的数量、空明细、含 `/` 的单号整体返回 `422`（Pydantic 结构化
`detail`，不产生残单）。

### `POST /receipts/{order_no}/scans`

对收货单提交扫描码裸数组。下例中 `07300040109316` 计划量为 2，
`00000000000017`（校验位 7）不在计划内，`07300040109310` 校验位不符，
`0730004010-9316` 格式错误：

```json
[
  "07300040109316",
  "00000000000017",
  "07300040109310",
  "0730004010-9316",
  "07300040109316"
]
```

`200`（逐码结论保序返回；只有合法码携带 `reconciliation`）：

```json
{
  "results": [
    {"code": "07300040109316", "calculated_check_digit": 6, "status": "valid",
     "reconciliation": {"conclusion": "matched", "planned_qty": 2,
                        "received_qty": 1}},
    {"code": "00000000000017", "calculated_check_digit": 7, "status": "valid",
     "reconciliation": {"conclusion": "unplanned", "planned_qty": null,
                        "received_qty": 1}},
    {"code": "07300040109310", "calculated_check_digit": 6,
     "status": "checksum_mismatch", "reconciliation": null},
    {"code": "0730004010-9316", "calculated_check_digit": null,
     "status": "format_error", "reconciliation": null},
    {"code": "07300040109316", "calculated_check_digit": 6, "status": "valid",
     "reconciliation": {"conclusion": "matched", "planned_qty": 2,
                        "received_qty": 2}}
  ]
}
```

再扫一次 `07300040109316` 即超收：`"conclusion": "excess"`、`"received_qty": 3`。
不存在的收货单返回 `404`；存储失败时整批回滚并返回 `503`。

验收环境额外识别请求头 `X-Simulate-Storage-Failure: 1`，在首个合法码递增后、
提交前注入一次存储错误以验证整批回滚与重试确定性；该开关仅在设置环境变量
`RECEIPT_ENABLE_FAILURE_INJECTION=1` 时生效（默认关闭，生产环境无头）。

### `GET /receipts/{order_no}`

返回收货单累计状态（计划行 + 扫描中登记的计划外行，计划外行 `planned_qty` 为
`null`）；不存在返回 `404`。

服务启动后还提供交互式文档：`/docs`（Swagger UI）与 `/openapi.json`。

## 本地运行（Python 3.12）

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

- `tests/test_gtin14.py`：校验位公式（3/1 交替权重、模 0 得 0 而非 10）、
  各状态判定以及“空白/连字符/全角数字不转换”。
- `tests/test_api.py`：混合批次保序与重复保留、1/100 边界、空数组/超限/非字符串成员/
  非数组请求体/损坏 JSON 的 422 且无部分结果、健康检查。
- `tests/test_receipts.py`：建单 201/重复 409/非法 GTIN、重复 GTIN 与非正数量整体
  422 且不留残单；单号含 `/` 或计划量超出 SQLite 64 位整数范围整体 422；首尾空白
  变体单号按同一收货单处理（重复 409、查询/扫描寻址一致）；计划内逐码递增
  matched→excess、计划外识别并持续累计、无效码不计数且无对账结论、未知单 404；
  注入存储失败后整批 503 回滚，重试结果确定、先前已提交计数不受影响。

## Docker Compose

宿主端口由 `API_PORT` 覆盖（容器内部固定 8000，默认宿主 8000）：

```bash
docker compose up -d --build                 # 宿主 8000
API_PORT=9000 docker compose up -d --build   # 宿主 9000
curl -s localhost:${API_PORT:-8000}/health
```

### 一次性验收服务 `verify`

`verify` 复用同一镜像，等待 `api` 健康后对**真实 HTTP 服务**执行端到端验收：
混合批次每件独立复算校验位、核对保序与重复保留、全部非法结构必须 422 且无部分结果；
随后建单（含重复单号 409 与非法明细 422）、分两批扫描验证 matched→excess 与
unplanned、确认无效码不计数，并通过存储故障注入验证整批 503 回滚与确定性重试，
然后以 0/1 退出：

```bash
docker compose run --rm verify
```

成功时末尾输出 `ACCEPTANCE PASSED`，并可看到混合批次中每个包装码唯一、保序、可复算的结论表。

## 项目布局

```
app/
  __init__.py
  gtin14.py       # 14 位 ASCII 数字判定与 GTIN-14 校验位公式（无占位实现）
  storage.py      # SQLite 收货单/明细建表、事务内按序递增、整批回滚
  main.py         # FastAPI 应用：Pydantic 固定请求边界、对账模型与响应
scripts/
  acceptance.py   # 一次性验收：纯标准库访问真实服务并独立复算
tests/
  test_gtin14.py  # 公式与单码规则
  test_api.py     # /codes/verify 接口边界、状态码与保序/重复语义
  test_receipts.py  # 建单/对账/计划外/无效码不计数/事务回滚与确定性重试
Dockerfile
docker-compose.yml
requirements.txt
requirements-dev.txt
pyproject.toml
```
