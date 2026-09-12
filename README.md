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
     升级前已落库的首尾带空白单号保持有效且不被改写：按规范化单号查询/扫描
     命中原单，重复创建同样返回 409；若同一规范化单号同时存在精确行与空白
     旧行，精确行优先，旧行数据保留。
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
   重试同一批得到确定结果——就像失败的请求从未发生。实收量累计达到 SQLite
   有符号 64 位整数上限（`2^63 - 1`）后再次扫描同样按存储失败处理：整批回滚、
   **原计数保留**，返回结构化 **503**（携带 `Retry-After`），计划内与计划外
   商品一致；携带批次键时回滚同样不占用键，可原键重试。
6. 应用启动时以 `CREATE TABLE IF NOT EXISTS` 幂等创建 `receipts` 与
   `receipt_items` 两张表，对已有数据库文件重复启动安全。数据库路径由
   `RECEIPT_DB_PATH` 环境变量覆盖（默认工作目录下 `receipts.db`）。
7. 可选请求头 `Idempotency-Key`（1～128 个字符且至少含一个非空白字符，比较时
   忽略首尾空白）让同一批扫描**至多入账一次**，用于仓库终端在提交后丢失响应的
   重试场景：
   - 服务在**同一 SQLite 事务**内完成计数递增并写入 `scan_batches` 批次记录
     （随启动迁移幂等创建，主键为规范化单号 + 批次键），记录保存规范化单号、
     **原始扫描数组**与**完整的 200 响应**；
   - 相同收货单 + 相同批次键 + **完全相同的数组**（内容与顺序均一致）再次提交：
     直接返回首次的 200 响应，**不重复计数**；
   - 相同批次键携带**内容或顺序不同**的数组：**409**，原记录保留、计数不变；
   - 空键、纯空白键或超过 128 字符的键：**422**（Pydantic 结构化 `detail`），
     不产生任何入账；
   - 事务失败（含注入的存储故障）既不增加数量也**不占用批次键**，同一键可
     立即重试；
   - 未携带批次键的请求不记录批次，继续按每次请求逐次入账。

## 冷链评估规则

药品完成收货后，质量人员为收货单登记一条冷链记录：唯一评估号、允许温区与
按时间严格递增的采样点。服务把**连续越界采样归并为异常区段**，按相邻点做
**梯形积分**，得出可复查的运输温控结论，并将原始采样与摘要一并持久化：

1. `POST /cold-chain-assessments` 创建评估。请求体：
   - `assessment_id`：唯一评估号（1～128 字符，忽略首尾空白比较，空白变体
     按重复处理；不得含 `/`，否则无法作为单个 URL 路径段寻址）；
   - `order_no`：已存在的收货单号（按收货单同一规范化规则寻址，含升级前
     落库的空白旧单）；
   - `min_temp` / `max_temp`：允许温区（°C），必须有限且
     `min_temp < max_temp`；边界上的温度视为**区内**；
   - `samples`：2～10000 个采样点，每点为 `recorded_at`（ISO-8601，必须带
     时区偏移）与 `temperature`（有限数值）。`recorded_at` 必须**严格递增**
     （同一瞬间的不同时区写法也算相等），首末跨度**不得超过七天**（恰好
     七天允许）。
   - 所有温度数值（温区边界与采样值）还必须在 **±10¹⁰⁰ °C** 之内：超出该
     数量级的值会让偏差与梯形积分溢出为无穷大，无法得出确定结论，因此
     整体按 **422** 拒绝，而不是在计算中途报错。
2. 越界（温度在温区之外）的相邻采样归并为一个异常区段；每个区段给出
   起止时刻、持续分钟数、偏离温区的**度分钟**（相邻点间偏差按梯形积分）、
   采样点数与峰值偏差。单个孤立越界点构成持续 0 分钟、度分钟为 0 的区段。
   摘要汇总样本数、总跨度、越界点数、区段数、累计持续分钟与累计度分钟
   （恰为各区段显示值之和），结论为 `compliant`（全程合规）或
   `excursion`（存在越界）。所有计算值**按分钟保留两位小数**。
3. 原始采样与摘要在**同一事务**中写入 `cold_chain_assessments` 与
   `cold_chain_samples` 两表（随启动迁移幂等创建，外键关联现有收货单）。
   `GET /cold-chain-assessments/{assessment_id}` 返回与创建时**同一份**
   确定性文档。
4. 评估号重复：**409**；收货单不存在：**404**；温区无效、采样点不足、
   时间未严格递增、跨度超过七天或温度数值超出 ±10¹⁰⁰：**422**（Pydantic
   结构化 `detail`）。任何失败请求都**不留记录**——评估号可原样重新提交。

## 货架期复核规则

药品入库后需按生产批号与有效期安排上架。库管员为**已存在的收货单**提交一次
货架期复核：唯一复核号、复核日期、最短可售天数，以及各 GTIN 的批号、数量与
失效日期，服务返回货架期处置清单：

1. `POST /shelf-life-reviews` 创建复核。请求体：
   - `review_id`：唯一复核号（1～128 字符，忽略首尾空白比较，空白变体按重复
     处理；不得含 `/`，否则无法作为单个 URL 路径段寻址）；
   - `order_no`：已存在的收货单号（按收货单同一规范化规则寻址，含升级前落库
     的空白旧单）；
   - `review_date`：复核日期（ISO-8601 日历日 `YYYY-MM-DD`）；
   - `min_sellable_days`：最短可售天数门槛，**0～3650** 的整数；
   - `items`：1～100 个 GTIN 条目（不得重复），每条携带 1～100 个批次：
     `batch_no`（1～128 字符且至少含一个非空白字符，同一 GTIN 内不得重复）、
     `quantity`（正整数，上限为 SQLite 有符号 64 位整数最大值）、
     `expiry_date`（日历日）。
2. 服务按**自然日**计算剩余天数 `remaining_days = expiry_date − review_date`，
   并依次标记处置结论：小于零 → `expired`；达到零但低于门槛 → `short_dated`；
   达到或超过门槛 → `usable`（复核当天到期的批次剩余 0 天，不算 expired；门槛
   为 0 时未过期批次一律 usable）。每个 GTIN 内批次按**失效日期、批号**稳定
   排序返回，GTIN 条目本身保持提交顺序。
3. 参与复核的 GTIN 必须已在该收货单**入账**——计划外已入账商品（`planned_qty`
   为 `null`）与计划内已扫码商品均可；从未出现在收货单上的 GTIN，以及**计划内
   但尚未扫码入账**（实收为 0）的 GTIN 都视为未入账商品，返回 **404**。同一
   GTIN 的批次数量合计不得超过该 GTIN 的**已实收量**，超出按 **422** 拒绝。
4. 复核、GTIN 条目与批次明细在**同一事务**写入 `shelf_life_reviews`、
   `shelf_life_review_items` 与 `shelf_life_batches` 三表（随启动迁移幂等创建，
   外键关联现有收货单）。实收量按复核时刻**快照**保存，之后继续扫描收货不改变
   已生成的清单；`GET /shelf-life-reviews/{review_id}` 返回与创建时**同一份**
   确定性文档。
5. 复核号重复：**409**；收货单或已入账 GTIN 不存在：**404**；日期格式非法、
   批号重复、数量非正、门槛超出 0～3650 或申报总量超出实收量：**422**（Pydantic
   结构化 `detail`）。任何失败请求都**不留记录**——复核号可原样重新提交。

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
不存在的收货单返回 `404`；存储失败时整批回滚并返回 `503`。实收量已达 SQLite
64 位整数上限的行再次扫描同样返回结构化 `503`（整批回滚、原计数保留），而不是
未处理的 `500`。

携带 `Idempotency-Key: batch-42` 请求头时，该批次至多入账一次：首次提交正常
入账并持久化完整响应；网络中断后以**相同单号 + 相同键 + 完全相同数组**重试，
直接返回首次的 200 响应且不重复计数；同一键携带不同（或顺序不同）的数组返回
`409` 且原记录保留；空键、纯空白键或超过 128 字符返回 `422`；批次回滚（503）
不占用键，可原键重试。不传该头时行为不变，每次请求逐次入账。

验收环境额外识别请求头 `X-Simulate-Storage-Failure: 1`，在首个合法码递增后、
提交前注入一次存储错误以验证整批回滚与重试确定性；该开关仅在设置环境变量
`RECEIPT_ENABLE_FAILURE_INJECTION=1` 时生效（默认关闭，生产环境无头）。

### `GET /receipts/{order_no}`

返回收货单累计状态（计划行 + 扫描中登记的计划外行，计划外行 `planned_qty` 为
`null`）；不存在返回 `404`。

### `POST /cold-chain-assessments`

为已完成收货的收货单登记冷链记录并立即得到温控结论。请求：

```json
{
  "assessment_id": "CC-2026-0001",
  "order_no": "PO-2026-0001",
  "min_temp": 2.0,
  "max_temp": 8.0,
  "samples": [
    {"recorded_at": "2026-09-01T08:00:00Z", "temperature": 5.0},
    {"recorded_at": "2026-09-01T08:30:00Z", "temperature": 9.0},
    {"recorded_at": "2026-09-01T09:00:00Z", "temperature": 10.0},
    {"recorded_at": "2026-09-01T09:30:00Z", "temperature": 6.0},
    {"recorded_at": "2026-09-01T10:00:00Z", "temperature": 1.0},
    {"recorded_at": "2026-09-01T10:30:00Z", "temperature": 0.5},
    {"recorded_at": "2026-09-01T11:00:00Z", "temperature": 4.0}
  ]
}
```

成功 `201`（两个越界区段：高于上限一段、低于下限一段；度分钟按相邻点
梯形积分，如首段 `(1 + 2) / 2 × 30 = 45.0`）：

```json
{
  "assessment_id": "CC-2026-0001",
  "order_no": "PO-2026-0001",
  "min_temp": 2.0,
  "max_temp": 8.0,
  "samples": [
    {"recorded_at": "2026-09-01T08:00:00Z", "temperature": 5.0},
    {"recorded_at": "2026-09-01T08:30:00Z", "temperature": 9.0},
    {"recorded_at": "2026-09-01T09:00:00Z", "temperature": 10.0},
    {"recorded_at": "2026-09-01T09:30:00Z", "temperature": 6.0},
    {"recorded_at": "2026-09-01T10:00:00Z", "temperature": 1.0},
    {"recorded_at": "2026-09-01T10:30:00Z", "temperature": 0.5},
    {"recorded_at": "2026-09-01T11:00:00Z", "temperature": 4.0}
  ],
  "summary": {
    "sample_count": 7,
    "span_minutes": 180.0,
    "out_of_range_samples": 4,
    "segment_count": 2,
    "total_duration_minutes": 60.0,
    "total_degree_minutes": 82.5,
    "conclusion": "excursion",
    "segments": [
      {"start": "2026-09-01T08:30:00Z", "end": "2026-09-01T09:00:00Z",
       "duration_minutes": 30.0, "degree_minutes": 45.0,
       "sample_count": 2, "peak_deviation": 2.0},
      {"start": "2026-09-01T10:00:00Z", "end": "2026-09-01T10:30:00Z",
       "duration_minutes": 30.0, "degree_minutes": 37.5,
       "sample_count": 2, "peak_deviation": 1.5}
    ]
  }
}
```

全程合规时 `segments` 为空、各项合计为 `0.0`、结论为 `compliant`。
评估号重复返回 `409`；收货单不存在返回 `404`；温区无效、采样点不足、
时间未严格递增、跨度超过七天或温度数值超出 ±10¹⁰⁰ 整体返回 `422`
且不留任何记录。

### `GET /cold-chain-assessments/{assessment_id}`

返回与创建时同一份确定性文档（原始采样 + 持久化摘要）；评估号不存在
返回 `404`。

### `POST /shelf-life-reviews`

为已入账的收货单提交货架期复核并取得处置清单。请求（`PO-2026-0001` 中
`07300040109316` 已实收 8，`00000000000017` 为计划外已入账 2）：

```json
{
  "review_id": "SLR-2026-0001",
  "order_no": "PO-2026-0001",
  "review_date": "2026-09-12",
  "min_sellable_days": 30,
  "items": [
    {
      "gtin": "07300040109316",
      "batches": [
        {"batch_no": "B-USABLE", "quantity": 3, "expiry_date": "2027-01-01"},
        {"batch_no": "B-EXPIRED", "quantity": 2, "expiry_date": "2026-09-01"},
        {"batch_no": "B-SHORT", "quantity": 2, "expiry_date": "2026-10-01"},
        {"batch_no": "B-EDGE", "quantity": 1, "expiry_date": "2026-10-12"}
      ]
    },
    {
      "gtin": "00000000000017",
      "batches": [
        {"batch_no": "C-1", "quantity": 2, "expiry_date": "2026-09-12"}
      ]
    }
  ]
}
```

成功 `201`（批次按失效日期、批号排序；`received_qty` 为复核时刻实收快照）：

```json
{
  "review_id": "SLR-2026-0001",
  "order_no": "PO-2026-0001",
  "review_date": "2026-09-12",
  "min_sellable_days": 30,
  "items": [
    {
      "gtin": "07300040109316",
      "received_qty": 8,
      "declared_qty": 8,
      "batches": [
        {"batch_no": "B-EXPIRED", "quantity": 2, "expiry_date": "2026-09-01",
         "remaining_days": -11, "disposition": "expired"},
        {"batch_no": "B-SHORT", "quantity": 2, "expiry_date": "2026-10-01",
         "remaining_days": 19, "disposition": "short_dated"},
        {"batch_no": "B-EDGE", "quantity": 1, "expiry_date": "2026-10-12",
         "remaining_days": 30, "disposition": "usable"},
        {"batch_no": "B-USABLE", "quantity": 3, "expiry_date": "2027-01-01",
         "remaining_days": 111, "disposition": "usable"}
      ]
    },
    {
      "gtin": "00000000000017",
      "received_qty": 2,
      "declared_qty": 2,
      "batches": [
        {"batch_no": "C-1", "quantity": 2, "expiry_date": "2026-09-12",
         "remaining_days": 0, "disposition": "short_dated"}
      ]
    }
  ],
  "summary": {
    "gtin_count": 2,
    "batch_count": 5,
    "declared_qty": 10,
    "expired_batches": 1,
    "short_dated_batches": 2,
    "usable_batches": 2
  }
}
```

复核号重复返回 `409`；收货单不存在或 GTIN 未入账（含计划内但尚未扫码、实收为 0
的商品）返回 `404`；日期格式非法、批号重复、数量非正、门槛超出 0～3650 或申报
总量超出实收量整体返回 `422` 且不留任何记录。

### `GET /shelf-life-reviews/{review_id}`

返回与创建时同一份确定性文档（含实收快照与处置结论）；复核号不存在
返回 `404`。

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
  变体单号按同一收货单处理（重复 409、查询/扫描寻址一致）；升级前落库的空白
  旧单仍可按规范化单号查询/扫描、阻止重复建单且原行不被改写；计划内逐码递增
  matched→excess、计划外识别并持续累计、无效码不计数且无对账结论、未知单 404；
  注入存储失败后整批 503 回滚，重试结果确定、先前已提交计数不受影响；实收量
  达到 64 位整数上限后扫描返回结构化 503、整批回滚且原计数保留（计划内与
  计划外行一致）。
- `tests/test_idempotency.py`：`Idempotency-Key` 批次键——响应丢失后相同数组重放
  返回与首次字节一致的 200 且不重复计数；同键不同内容或顺序 409 且原记录保留、
  计数不变；空键/纯空白键/超长键 422、128 字符边界；回滚批次既不计数也不占键，
  同键可重试且之后重放一致；实收量达整数上限时有键批次同样 503 且不占键；
  同一键在不同收货单相互独立；无键扫描仍逐次累计，与有键批次交错正确。
- `tests/test_cold_chain.py`：全程合规得 `compliant` 且无区段；多个越界区段的
  归并与梯形积分（持续分钟、度分钟、峰值偏差、合计恰为区段之和）及读取一致性；
  孤立越界点的零时长区段；计算值两位小数；不同时区偏移按瞬间比较；评估号重复
  409（含空白变体）、未知收货单 404、未知评估号 404；温区无效/采样点不足/时间
  未严格递增/跨度超七天（恰好七天允许）/裸时区时间/非有限数值整体 422；超出
  ±10¹⁰⁰ 的温度采样或温区边界明确 422（±10¹⁰⁰ 本身仍可计算），不报错 500、
  不占用评估号；失败请求不留残记录（评估号可复用，库中无孤儿采样行）。
- `tests/test_shelf_life.py`：自然日剩余天数与三类处置（expired/short_dated/
  usable，含当天到期、恰好达到门槛与零门槛边界）；GTIN 内按失效日期与批号稳定
  排序、GTIN 条目保持提交顺序；计划外已入账商品可复核；实收量快照在后续扫描后
  保持不变、读取与创建一致；复核号重复 409（含空白变体）、未知收货单或未入账
  GTIN 404（含计划内但尚未扫码入账的商品，扫码后即转为可复核）；日期非法/批号
  重复/数量非正/门槛越界/申报总量超实收整体 422；失败请求不留残记录（复核号
  可复用，库中无孤儿行）。

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
unplanned、确认无效码不计数，并通过存储故障注入验证整批 503 回滚与确定性重试；
再验证幂等批次键：相同键重放响应一致且不重复计数、冲突重用 409 且计数不变、
非法键 422、回滚后同键可重试、无键扫描持续累计；
再登记冷链评估：全程合规结论、多个越界区段梯形积分的独立复算核对、读取与创建
一致的确定性文档、重复评估号 409、未知收货单 404、各类非法请求 422 且不留残记录；
最后从建单与扫描开始提交货架期复核：三类处置与自然日剩余天数的独立复算核对、
GTIN 内失效日期与批号排序、计划外已入账商品参与复核、未入账商品（含计划内未
扫码）404、超量提交 422 且不留残记录、创建结果一致读取，然后以 0/1 退出：

```bash
docker compose run --rm verify
```

成功时末尾输出 `ACCEPTANCE PASSED`，并可看到混合批次中每个包装码唯一、保序、可复算的结论表。

## 项目布局

```
app/
  __init__.py
  gtin14.py       # 14 位 ASCII 数字判定与 GTIN-14 校验位公式（无占位实现）
  cold_chain.py   # 冷链评估领域：越界区段归并、相邻点梯形积分、两位小数摘要
  shelf_life.py   # 货架期复核领域：自然日剩余天数、三类处置标记、批内稳定排序
  storage.py      # SQLite 收货单/明细、冷链评估/采样、幂等批次与货架期复核建表、事务内按序递增、整批回滚
  main.py         # FastAPI 应用：Pydantic 固定请求边界、对账、冷链评估与货架期复核模型及响应
scripts/
  acceptance.py   # 一次性验收：纯标准库访问真实服务并独立复算
tests/
  test_gtin14.py  # 公式与单码规则
  test_api.py     # /codes/verify 接口边界、状态码与保序/重复语义
  test_receipts.py  # 建单/对账/计划外/无效码不计数/事务回滚与确定性重试
  test_idempotency.py  # Idempotency-Key：重放不重复计数、冲突 409、回滚不占键
  test_cold_chain.py  # 冷链评估：合规结论、多区段积分、读取一致、错误信封与无残记录
  test_shelf_life.py  # 货架期复核：三类处置、批内排序、计划外参与、超量 422 与无残记录
Dockerfile
docker-compose.yml
requirements.txt
requirements-dev.txt
pyproject.toml
```
