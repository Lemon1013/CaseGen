# 现货交易 — 限价单余额校验业务与接口规则

> 样例文档，供 CaseGen Demo：上传 → Wiki 编译 → 需求「现货限价单余额不足」生成用例。

## 1. 文档说明

本文档描述现货（Spot）交易中 **限价买单（LIMIT BUY）** 在下单前的资金/余额校验规则，以及相关 REST 接口约定、错误码与边界条件。测试设计应覆盖：余额充足放行、余额不足拒绝、冻结与可用余额区分、精度与并发等场景。

## 2. 业务术语

| 术语 | 说明 |
|------|------|
| 现货账户 | 用户在现货业务线下的资产账户，按币种分户 |
| 可用余额 (available) | 可立即用于下单、划转的金额，不含冻结 |
| 冻结余额 (frozen) | 已挂单或进行中操作占用的金额 |
| 限价买单 | 指定价格买入；下单时按「价格 × 数量」预估占用报价币（如 USDT） |
| 报价币 / 基础币 | 交易对 `BASE/QUOTE`，如 `BTC/USDT` 中 BTC 为基础币，USDT 为报价币 |
| 名义金额 (notional) | 买单预估占用 = `price * quantity`（未含手续费时） |
| 手续费 | 默认 maker/taker 费率从可用余额额外预留或成交时扣减，以接口字段为准 |

## 3. 核心业务规则：余额不足应拒绝下单

### 3.1 判定公式（限价买单）

对交易对 `BASE/QUOTE` 提交限价买单时：

1. 计算预估占用报价币：  
   `required_quote = price * quantity`
2. 若开启「预留手续费」：  
   `required_quote = required_quote * (1 + fee_rate)`  
   其中 `fee_rate` 取当前用户适用 taker 费率上限（保守预留）。
3. 读取现货账户 `QUOTE` 币种的 **可用余额** `available_quote`（**不得**使用总余额 total = available + frozen）。
4. **当 `available_quote < required_quote` 时，系统必须拒绝下单**，不得创建订单、不得新增冻结。
5. 当 `available_quote >= required_quote` 时，下单成功后将 `required_quote` 从 available 转入 frozen（挂单冻结）。

### 3.2 明确禁止的行为

- 禁止使用「总余额」代替「可用余额」做校验（已冻结部分不可再次占用）。
- 禁止在余额不足时创建「部分成交意图」的挂单；本业务线限价单为全额预占模式。
- 禁止余额不足时返回成功码或异步延迟失败而不写拒绝原因。
- 禁止因余额不足产生负可用或超卖冻结。

### 3.3 限价卖单（对照，避免混淆）

限价卖单校验的是 **基础币可用余额** `available_base >= quantity`，与买单报价币校验相互独立。  
**「现货限价单余额不足」在买单语境下指报价币可用不足；在卖单语境下指基础币可用不足。** 用例需写明方向。

## 4. 接口约定

### 4.1 下单接口

- **Method / Path:** `POST /api/v1/spot/order`
- **Content-Type:** `application/json`
- **鉴权:** Header `X-API-KEY` + 签名（Demo 可只校验 key 存在）

#### 请求体字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `symbol` | string | 是 | 交易对，如 `BTCUSDT` |
| `side` | string | 是 | `BUY` / `SELL` |
| `type` | string | 是 | 本规则关注 `LIMIT` |
| `price` | string | 限价必填 | 十进制字符串，避免浮点误差 |
| `quantity` | string | 是 | 下单数量 |
| `timeInForce` | string | 否 | 默认 `GTC` |
| `clientOrderId` | string | 否 | 客户端幂等键，最长 64 |

#### 成功响应（HTTP 200）

```json
{
  "code": 0,
  "msg": "OK",
  "data": {
    "orderId": "10086001",
    "clientOrderId": "demo-001",
    "symbol": "BTCUSDT",
    "side": "BUY",
    "type": "LIMIT",
    "price": "60000",
    "quantity": "0.01",
    "status": "NEW",
    "frozenQuote": "600.00"
  }
}
```

#### 余额不足响应（HTTP 400 或业务码，见错误码表）

```json
{
  "code": 10001,
  "msg": "INSUFFICIENT_BALANCE",
  "data": {
    "asset": "USDT",
    "available": "100.00",
    "required": "600.00",
    "side": "BUY",
    "symbol": "BTCUSDT"
  }
}
```

**验收要点：** 余额不足时 `code` 必须为 `10001`（或文档约定等价码），`msg` 含 `INSUFFICIENT_BALANCE` 语义；`data.available` / `data.required` 应回显便于排障；**不得**返回 `orderId`；账户 available/frozen **无变化**。

### 4.2 余额查询接口

- **Method / Path:** `GET /api/v1/spot/account/balance?asset=USDT`
- **成功时**返回：

```json
{
  "code": 0,
  "data": {
    "asset": "USDT",
    "available": "100.00",
    "frozen": "50.00",
    "total": "150.00"
  }
}
```

测试前置：可通过该接口或测试夹具设置 `available`，再调用下单接口验证拒绝逻辑。

## 5. 错误码

| code | 常量名 | 场景 | HTTP 建议 |
|------|--------|------|-----------|
| 0 | OK | 成功 | 200 |
| 10001 | INSUFFICIENT_BALANCE | 可用余额 < 所需占用 | 400 |
| 10002 | INVALID_PRICE | 价格非正、精度超限 | 400 |
| 10003 | INVALID_QUANTITY | 数量非正、低于最小下单量 | 400 |
| 10004 | SYMBOL_NOT_FOUND | 交易对不存在或已下线 | 404 |
| 10005 | DUPLICATE_CLIENT_ORDER_ID | 幂等键冲突 | 409 |
| 10006 | ACCOUNT_FROZEN | 账户风控冻结，禁止交易 | 403 |

余额不足 **仅** 使用 `10001`，不要与 `10006` 混用。

## 6. 边界与精度

1. **相等边界：** `available == required` 应 **允许** 下单（`>=`）。  
2. **差一点不足：** `available = required - 最小精度单位` 应 **拒绝**。  
3. **价格/数量精度：** 按交易对 `tickSize` / `stepSize` 截断或拒绝；非法精度返回 `10002`/`10003`，优先于余额校验顺序可实现为：参数校验 → 余额校验 → 落单。  
4. **最小名义金额：** 若 `price * quantity < minNotional`，返回参数类错误，不进入余额不足分支。  
5. **多币种：** 买单只扣减报价币；不得误扣基础币。  
6. **并发：** 两笔同时下单且合计超过 available 时，至多一笔成功；另一笔 `10001`；不得双冻导致 available 为负。

## 7. 推荐测试场景（供生成用例参考）

### 7.1 主路径 — 余额不足拒绝（BUY LIMIT）

- **标题建议：** 现货限价买单可用余额不足应拒绝下单  
- **前置：** 用户 USDT `available=100`，`frozen=0`；交易对 `BTCUSDT` 可交易  
- **步骤：**  
  1. 查询余额，确认 available=100  
  2. `POST /api/v1/spot/order`，side=BUY，type=LIMIT，price=60000，quantity=0.01（required=600）  
  3. 再次查询余额  
- **预期：**  
  - 响应 code=10001，msg 含 INSUFFICIENT_BALANCE  
  - data.required 约 600，data.available 为 100  
  - 无 orderId；available 仍为 100，frozen 仍为 0  

### 7.2 对照 — 余额刚好足够

- available=600，同价同量限价买 → code=0，status=NEW，frozen 增加 600，available 变为 0  

### 7.3 冻结占用导致不足

- total=600 但 available=100、frozen=500；下单 required=200 → 必须 10001（不能用 total 判断）  

### 7.4 卖单余额不足

- side=SELL，BTC available 不足 quantity → 10001，asset 为 BTC  

### 7.5 参数错误优先

- price 非法时返回 10002，即使余额亦不足，也不强制要求一定先报 10001（以实现顺序文档为准，用例可分别覆盖）  

## 8. 非功能与审计

- 拒绝下单应写审计日志：userId、symbol、side、required、available、code、traceId。  
- 对外响应不得泄露其他用户余额。  
- 限流：同一用户下单接口建议 QPS 上限（Demo 可忽略实现，用例可作可选检查）。

## 9. 与 Wiki / 检索关键词

以下关键词应能检索到本规则相关页：

- 现货限价单  
- 余额不足  
- INSUFFICIENT_BALANCE  
- 可用余额  
- 限价买单冻结  
- 10001  

---

文档版本：demo-1.0  
适用：CaseGen MVP 验收样例
