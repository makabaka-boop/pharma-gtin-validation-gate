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

## Docker Compose

宿主端口由 `API_PORT` 覆盖（容器内部固定 8000，默认宿主 8000）：

```bash
docker compose up -d --build                 # 宿主 8000
API_PORT=9000 docker compose up -d --build   # 宿主 9000
curl -s localhost:${API_PORT:-8000}/health
```

### 一次性验收服务 `verify`

`verify` 复用同一镜像，等待 `api` 健康后对**真实 HTTP 服务**执行端到端验收：
混合批次每件独立复算校验位、核对保序与重复保留、全部非法结构必须 422 且无部分结果，
然后打印每件的放行结论并以 0/1 退出：

```bash
docker compose run --rm verify
```

成功时末尾输出 `ACCEPTANCE PASSED`，并可看到混合批次中每个包装码唯一、保序、可复算的结论表。

## 项目布局

```
app/
  __init__.py
  gtin14.py       # 14 位 ASCII 数字判定与 GTIN-14 校验位公式（无占位实现）
  main.py         # FastAPI 应用：Pydantic 固定请求边界与响应模型
scripts/
  acceptance.py   # 一次性验收：纯标准库访问真实服务并独立复算
tests/
  test_gtin14.py  # 公式与单码规则
  test_api.py     # 接口边界、状态码与保序/重复语义
Dockerfile
docker-compose.yml
requirements.txt
requirements-dev.txt
pyproject.toml
```
