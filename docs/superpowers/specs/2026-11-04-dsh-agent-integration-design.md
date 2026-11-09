# CaseGen 接入 DSH Agent 能力 — 设计规格

**日期：** 2026-11-04
**状态：** 已审阅通过（v2.1，需求方确认 §3.5 入库采用方案 a；实施分支 `feat/dsh-agent-integration`）；**P1 已实施完成（2026-11-04）：前后端代码 + 57 条后端测试，全量 `pytest -q` 759 passed（702 基线 + 57 新增），`npm run build` 绿，真实 DSH 联调（gateway 三步建会话 + SSE 全链路）通过**
**阶段：** Phase 0–4（增量演进）
**成功标准：** 新增 Agent 对话入口可多轮生成用例并转存入库进入现有人工确认流；现有流水线可分阶段 agent 化且用户不感知中间过程；win10 部署目标机（含 DSH 运行时）可完整使用；`pytest -q` **702 passed** 基线与 `npm run build` 全程保持绿。

**修订记录：** v1 初稿（基于 DSH webapi 实测 + lead 自查）；v2 并入 backend_explorer 全量报告，收敛了「对话产物入库路径」开放项，新增 chat hook 占用禁忌、调用量级闸门、win10 env 加载坑、依赖零新增约束、SSE 兼容清单与实现范式。

---

## 1. 背景与目标

### 1.1 问题

CaseGen 现有用例生成是确定性多阶段流水线（`task_pipeline.py`），格式契约严格但灵活性不足：用户无法以自然语言多轮对话方式引导生成、无法按需选择「专家角色」、流水线外的长尾需求（临时补一批边界用例、针对某个接口聊出测试思路）没有入口。

后端调研已确认：**CaseGen 完全没有任何多轮对话 / 消息历史 / chat session 概念**（唯一的 "Session" 是 cookie 鉴权会话 `AuthSession`，entities.py:833）；每次 LLM 调用都是无状态的一次性 messages 列表，由当前阶段在 Python 里重新拼装。⇒ 接入 agent 对话是**纯增量**，无存量语义冲突。

### 1.2 目标

直接复用本机 DSH（DeepSeek Harness）的 agent 能力（已装 dsh-webapi 插件，服务 http://127.0.0.1:31800/api/v1），不自造 agent 轮子：

1. **线 A（增量）**：新增「Agent 对话」页——用户选择专家技能（如测试专家），与 agent 多轮对话灵活生成用例；产出转存为用例草稿并进入现有人工确认/入库流程。
2. **线 B（绞杀者）**：现有流水线的个别低频阶段逐段替换为 agent 调用；中间思考过程、工具调用一律不暴露给用户。

### 1.3 非目标

- 不把现有 task_pipeline 整体替换为自由 agent（格式契约、人工检查点、耗时都不可控）。
- 不在前端实现任何 DSH 直连（浏览器永不接触 31800 端口）。
- 不做 agent 执行过程可视化（思考链、工具调用轨迹展示）——协议预留 `kind` 字段即可，UI 不做。
- 不引入 WebSocket（现有 SSE 复用面更大；`vite.config.ts` proxy 也未开 `ws: true`）。
- 不做多 DSH 实例负载均衡（单机单实例，容量问题 P4 压测后再说）。
- **不 agent 化 Wiki 摄入链**（该链完全没有 SSE/TaskEvent，只有 `IngestJob.stage/progress/step_log_json` 轮询，进度协议要另建，本期不碰）。
- **不新增任何后端运行时依赖**（见 §2 底座约束）。

### 1.4 关键决策摘要（已与需求方确认 + 调研收敛）

| 决策点 | 结论 | 理由 |
|--------|------|------|
| 调用拓扑 | CaseGen 后端是唯一 DSH 调用方 | 「过程不外露」必须在后端过滤（reasoning/tool_call 事件不透传）；鉴权体系不混用（CaseGen 只有 cookie session，无机器用 Bearer 通道）；DSH 只绑 127.0.0.1 不出本机 |
| win10 部署 | **目标机有 DSH 运行时**（已确认） | 部署文档补 DSH 安装 + dsh-webapi 配置章节；`AGENT_ENABLED` 开关保留但默认 true |
| 对话产出形态 | **转存为用例草稿进现有确认/入库流**（已确认） | agent 输出 `DraftPointGroup[]`/`DraftCaseItem[]` 同构 JSON，现有结构化渲染、引用溯源零改动复用 |
| 对话产物入库路径 | **建虚拟 Requirement + 专用导入路径，不复用 finalize_task 状态机** | `TestCase.requirement_id` NOT NULL 且有 FK；`finalize_task` 依赖测试点检查点链，对话场景没有（详见 §3.5） |
| 专家技能载体 | 仓库内 `.agents/skills/`（project 技能），**该目录当前不存在，P2 新建** | 随仓库版本管理、随部署分发、受 PR 评审管控；不用 agentPreset（DSH 全局配置，分发成本高）；不用 PromptTemplate type='agent_skill'（每轮全量占上下文，SKILL.md 由 agent 按需加载） |
| 会话工作目录 | `data/agent-workspace/<conversation_id>/` 每会话独立 | 已实测：DSH `findProjectRoot` 向上找 `.git`，cwd 在 data 深处仍正确解析技能根到仓库 `.agents/skills/` —— 隔离与仓库内技能兼得 |
| 流式协议 | 沿用 TaskStreamBroker 事件语义（snapshot/delta/status/notice/retry/reset/completed/failed） | 前端已有成熟消费实现（TaskDetailView 8 个监听器 + rAF 合帧），抽 composable 共用 |
| 流水线改造方式 | 保留编排器，单阶段替换 + env 开关 `PIPELINE_AGENT_STAGES` | 可随时回退；契约测试守门 |
| **agent 接入不占用 chat hook 槽位** | 线 B 用显式参数/配置开关，**绝不设置模块级 `_PIPELINE_CHAT_FN` 等钩子** | `_force_sync`（api/tasks.py:178）在钩子非 None 时把生成改为**请求内同步执行**，会改变 60+ 测试文件的执行路径 |
| 线 B 试点范围 | 只允许**每任务调用次数为个位数**的阶段（supplement/review），加 `AGENT_STAGE_MAX_CALLS` 闸门 | `scenario_coverage` MAX_CALLS=192、生产 point_by_point 恒 True ⇒ 高频阶段换 agent 轮次会让总耗时数量级放大 |

---

## 2. 总体架构

```
浏览器 ──(cookie session + CSRF，现有体系)──> CaseGen FastAPI(8000，单进程)
                                                │
                       ┌────────────────────────┼────────────────────────┐
                       │                        │                        │
              agent_gateway.py           task_pipeline.py          现有 llm.py
              (同步 httpx，新增)          (阶段级 agent 调用)        (OpenAI 兼容)
                       │                        │                        │
                       └───────────┬────────────┴──────────┬─────────────┘
                                   │                       │
                        DSH webapi 127.0.0.1:31800   (P0: /chat/completions
                        /sessions/* 会话面             也走同一端口)
                                   │
                        agent 进程 (cwd=data/agent-workspace/<conv_id>,
                        技能根=仓库 .agents/skills/)
```

**既有技术底座约束（后端调研核实，直接决定实现选型）**：单进程 FastAPI + SQLite（SQLModel），**无消息队列、无 Redis、无 WebSocket、无 openai SDK、无 alembic、无 pydantic-settings**；SSE 是手写字符串（`task_stream.encode_sse` :483）+ `StreamingResponse`；队列是 SQLite 行；迁移是 `app/db.py` 手写幂等 `_migrate_*` 序列。`requirements.txt` 仅 10 项（fastapi / uvicorn[standard] / sqlmodel / httpx[socks] / pypdf / python-docx / python-multipart / pyyaml / pytest / pytest-asyncio）。

⇒ **agent 接入不得新增运行时依赖**：httpx 已够用（同步 Client，与 `llm.py`/`external_wiki_client.py` 一致），SSE 行解析手写，win10 离线 wheels 目录无需变动。

**事件过滤规则（「过程不外露」的实现位置）**：

| DSH prompt-stream 事件 | 后端处理 |
|---|---|
| `delta{delta,seq}` | 转发为 CaseGen SSE `delta` |
| `reasoning{delta,seq}` | **丢弃**（不落库、不透传、不进日志，仅计数） |
| `tool_call{id,name,arguments}` | **丢弃**；可选映射为匿名化 `notice`（如「正在检索资料…」，按工具名白名单转文案，不含参数） |
| `tool_result{id,name,result}` | **丢弃** |
| `turn_end{reason}` / `done` | 转发为 `completed`（reason=stop 时）或 `failed` |
| `error{message}` | 转发为 `failed` + 安全化 message（复用 `safe_model_error` 思路，见 §8） |
| `': ping'` 注释帧（15s 心跳） | 后端消费，向浏览器自发 `: keep-alive` 心跳保活 |

补充约束：若要把 agent 阶段写进持久时间线 `TaskEvent.detail_json`（entities.py:572，经 `append_event`，task_events.py:11），**只能写匿名化阶段文案**——该表会永久进入 `GET /{id}/events` 与用例审计面，现有约定即如此。

---

## 3. 线 A：Agent 对话

### 3.1 数据模型（backend/app/models/entities.py 追加）

```python
class AgentConversation(SQLModel, table=True):
    id: str                    # uuid（避免 SQLite 整数 id 复用串流问题）
    project_id: str            # 复用现有项目隔离
    title: str                 # 首条用户消息截断生成，可改
    dsh_session_id: str | None # 首条消息时创建 DSH session 后回填
    dsh_workspace_id: str | None # 勘误(实施期实测)：DSH POST /sessions 不收 cwd，工作目录须先 POST /workspaces {path,title} 建 workspace，会话存其 id
    skill_name: str | None     # 专家技能（.agents/skills 目录名）
    model: str | None          # "provider/model"，空则用 DSH 默认
    workspace_dir: str         # data/agent-workspace/<id>
    status: str                # active | busy | closed
    created_at / updated_at

class AgentMessage(SQLModel, table=True):
    id: str
    conversation_id: str       # index
    role: str                  # user | assistant
    content: str               # 仅存干净正文（无 reasoning/tool 痕迹）
    created_at
```

只持久化用户消息与 assistant 最终正文；DSH 侧完整历史（含工具调用）可经 `GET /sessions/:id/history` 排查，是免费审计通道。

建表方式：新表靠 SQLModel `create_all` 自动建即可；**若需给既有表加列，必须新增一个幂等 `_migrate_*` 步骤**挂进 `app/db.py:732 init_db()` 的手写迁移序列（SQLite 无 auto-alter，`_migrate_sqlite_columns` :27 docstring 已明说）。本设计不需要改既有表。

### 3.2 后端 API（新增 backend/app/api/agent_chat.py）

| 方法+路径 | 说明 |
|---|---|
| GET /api/agent/conversations | 列表（当前项目，倒序） |
| POST /api/agent/conversations | 创建 {title?, skill_name?, model?} |
| GET /api/agent/conversations/{id} | 详情 + 消息历史（**轮询兜底权威端点**） |
| DELETE /api/agent/conversations/{id} | **实施决策（P1，覆盖原「仅标记 closed」）**：硬删除本地会话（删消息+会话+broker.discard，best-effort 删对应 DSH workspace/session），busy 时 409。理由：用户主动删自己的会话，硬删更符合直觉且利于隐私；软关闭会残留需 retention 策略。原 `_CLOSED_STATUS`/closed 分支保留作将来归档防御位。 |
| POST /api/agent/conversations/{id}/messages | 发用户消息 {content, idempotency_key}，启动后台转发任务；busy 时 409 |
| **GET /api/agent/conversations/{id}/stream?project_id=** | SSE（EventSource 不能设 header ⇒ 必须 GET+cookie，免 CSRF，照抄 `taskStreamUrl` 拼 query） |
| POST /api/agent/conversations/{id}/cancel | 中止当前轮 → DSH `POST /sessions/:id/cancel` |
| GET /api/agent/skills | 扫描仓库 `.agents/skills/*/SKILL.md` frontmatter（name+description），供选择器 |
| POST /api/agent/conversations/{id}/save-as-draft | 从最近 assistant 消息解析用例 JSON → 落库（见 §3.5）；解析失败 422 + 提示导出 Markdown |

**SSE 端点必须满足的前端兼容清单**（与 `GET /api/tasks/{id}/stream`，api/tasks.py:561-667 对齐；前端 8 个监听器已实现，TaskDetailView.vue:810-870）：

1. 首帧必为全量 `snapshot`（前端 `replaceLivePreview`，刷新/重连可恢复）——docstring :562 明确「The first event is always a complete snapshot」；
2. 事件名沿用 `snapshot / status / delta / reset / retry / notice / completed / failed`；
3. 每帧带 `id: <sequence>`（`encode_sse` :483 产 `id:/event:/data:` 三行 + 空行），但**不依赖 Last-Event-ID 续传**（现有实现是重连即重发 snapshot）；
4. 心跳用 SSE 注释行 `": keep-alive\n\n"`，间隔 15s（`_STREAM_HEARTBEAT_SEC` :131），broker 轮询间隔 0.2s（`_STREAM_POLL_INTERVAL_SEC` :132，用 `await asyncio.sleep` 避免占住 Starlette worker 线程）；
5. `project_id` 走 query（EventSource 不能设 header，api/tasks.ts:332 `_projectQuery`）；GET 免 CSRF 但必须校验 cookie session + 会话归属项目；
6. 断连让 EventSource 自然重连（后端主动 close 即可，前端线性退避 `attempt * 1000`，上限 3 次）；broker 返回 "missing" 时直接关流，让轮询兜底成为权威；
7. 必须有可轮询的持久状态兜底端点（`GET /api/agent/conversations/{id}`），现有 tasks 侧是 `GET /api/tasks/{id}` + 2s setInterval；
8. `completed`/`failed` 终态必须与 DB 一致（参考 `_fail_task` 刻意 post-commit 才发 `task_stream.fail`，task_pipeline.py:1467-1490）；
9. 非活跃状态请求 stream 返回 **409** 而非制造只有心跳的空流（参考 api/tasks.py 的两个 409 分支：「Task status {status!r} has no active generation stream」/「Live generation stream unavailable; use task polling」，注释 "Do not manufacture a stream that can only heartbeat"）；
10. 响应头照抄：`media_type="text/event-stream"` + `Cache-Control: no-store` + `X-Content-Type-Options: nosniff` + `X-Accel-Buffering: no`。

### 3.3 agent_gateway.py（新增 backend/app/services/）

同步 httpx 封装 DSH 会话面，供对话 API 与流水线阶段共用：

- `ensure_session(conv) -> dsh_session_id`：**勘误(实施期 curl 实测)**——DSH `POST /sessions` 请求体为 `{workspaceId?, title?, provider?, model?, reasoningEffort?, agentPreset?}`，**没有 cwd 字段**，工作目录只能来自 workspace。实际流程改为三步：① `POST /workspaces {path: workspace_dir, title}` 建/复用 workspace（path 必须以 AGENT_WORKSPACE_ROOT 为前缀，防目录穿越）；② `POST /sessions {workspaceId, title, model?}`；③ `PUT /sessions/:id/permission {preset:'workspace-write'}`（实测默认 preset 是 danger-full-access，此步必须做；**压降失败必须让 ensure_session 整体失败**，不得带 full-access 继续）。回填 dsh_session_id 与 dsh_workspace_id。
- `stream_prompt(session_id, prompt) -> Iterator[DshEvent]`：`POST /sessions/:id/prompt-stream`，逐事件 yield；read timeout ≥ 600s（首帧可达 5–8 分钟，**绝不自动重试 prompt——重复发送会整轮重跑**）。**勘误(实施期实测线格式)**：SSE 事件为 connected(含 sessionId)/delta(delta,seq)/reasoning/usage/turn_end/done；`done` 的 data 是字面量 `[DONE]` 而非 JSON；`turn_end.reason` 是对象 `{kind:'completed'}`；`usage` 事件为本 spec 未列的新增类型（token 统计，可落库或丢弃，不进 SSE 下游）。同步端点 `POST /sessions/:id/prompt` 返回 `{content, toolCalls, usage}`。
- `prompt_sync(session_id, prompt, timeout_ms) -> {content, tool_calls_count}`：同步端点，流水线阶段用，只取 content。
- `cancel(session_id)`；agent-busy 409 时先 cancel 再重发，或用 `mode:"steer"`。
- 统一解包 `{ok,data,timestamp}` / `{ok:false,error}`；连续 401 两次即熔断并告警（DSH SKILL.md 守则）。
- 会话级互斥：同 conversation 串行（复用 `task_locks.py` 的 `TaskLockRegistry.hold` 模式，:30 每 key 一把 RLock，users==0 回收）；全局并发信号量 `AGENT_MAX_CONCURRENT`（默认 4，P4 压测调优）。**注意：单进程约束下这些只能是进程内机制**（`task_locks.py` docstring 明说 "CaseGen currently runs generation and its live broker in one process"；`task_stream.py:1-7` 明说多 worker 需 Redis broker）。
- 工作目录准备：`ensure_session` 前 mkdir workspace_dir；需要给 agent 的材料（需求文档、wiki 摘录）由后端写入该目录（本地文件即可，cwd 已在其中；大文件才走 `POST /sessions/:id/files`）。

**实现范式（照抄仓库既有约定，不新造模式）**：

- HTTP 客户端骨架抄 `app/services/external_wiki_client.py`（229 行，现成的「调用外部 HTTP 服务」范式）：`_create_client(url, timeout_sec, transport)` :33（显式 `httpx.HTTPTransport(proxy=select_environment_proxy(url), trust_env=True)`，注释说明是为绕开 httpx 的 NO_PROXY mounts bug）、`_http_client_error(exc)` :16（**绝不把原始异常文本插进错误消息**，因可能含带凭据的代理 URL）、失败一律 `logger.warning(...) + return []` 降级。**需补三项**：长 read timeout（它默认只有 3s）、SSE 行解析（它不支持流式）、鉴权 header 注入点（它没有鉴权头，DSH 将来开启 authEnabled 时用）。
- 与 `llm.py:81` 一致使用**同步 httpx.Client**（后端全同步，跑在 Starlette 线程池 / job 线程）；**不要**在 job 线程里起 AsyncClient + `asyncio.run`。
- SSE 端点照抄 api/tasks.py:561 的组合：**handler 是 sync def，内部 `event_stream()` 才是 async generator**，且 generator 内不长开 DB Session（`_load_stream_task_status` :145 docstring "Load only durable stream eligibility and close the DB session eagerly."）。
- 项目归属：`/api/agent` 前缀**不在** `_PROJECT_SCOPED_PREFIXES`（`app/services/projects.py:120-142`）内，不会自动获得 project_id 强校验（受控前缀缺 project_id 会 422 "project_id is required"，不匹配 404 "Resource not found in project"）⇒ 必须自行校验会话归属项目，并在 `validate_request_project_scope`（:91-117，目前只覆盖 `/api/tasks/{id}` 与 `/api/cases/{id}`）补一条 `/api/agent/conversations/{id}` 正则规则。
- CSRF/同源由现有中间件自动覆盖（`main.py:120-138`）：非安全方法必须 Origin/Referer 同源，否则 403 "Cross-origin request blocked" / 403 "Origin or Referer header is required"；可选双提交 `x-csrf-token` vs `casegen_csrf` cookie（不匹配 403 "CSRF validation failed"）。⇒ agent 写操作走 POST 即自动受保护，stream GET 免 CSRF 但仍需 cookie。
- 取消是协作式：agent 轮次必须在调用边界轮询停止标志并主动调 DSH cancel，否则前端「停止」只改 DB 状态而 agent 仍在跑（参考 `_generation_stopped`，task_pipeline.py:75；`cancel_ingest_job` docstring 亦明说 "A running LLM HTTP request is not forcibly interrupted."）。
- 写接口沿用项目惯例：`expected_version` 乐观锁 + 客户端生成 `idempotency_key` 服务端查重（参考 `schemas/tasks.py:160-165 RetrievalCheckpointConfirm`、`api/cases.py:88 _check_revision`）。
- SQLite 写锁：agent 长轮次期间**不要一直持有 Session**（现有代码用「每阶段 commit」缓解，task_pipeline.py:2038）；即取即放。
- 状态机：agent 若要驱动 GenerationTask 状态，必须走 `task_state.transition()`（非法转移由 `_begin_status`，api/tasks.py:185 映射成 409）。对话自身状态（active/busy/closed）不进该状态机。

### 3.4 专家技能

- 位置：`.agents/skills/expert-test-designer/SKILL.md` 等，随仓库分发。**该目录当前仓库中不存在（`ls .agents` → 无），P2 需新建并纳入版本控制**。已核实：`git check-ignore -v .agents/skills/x` 无匹配（**不会被忽略**，可安全纳入版本控制），而 `data/agent-workspace/x` 命中 `.gitignore:5 data/`（**工作目录天然被忽略**，agent 产物不会误入版本控制）。**frontmatter 硬约束（实测）：`name` 必须 kebab-case（下划线开头如 `_probe-skill` 会被 `isSkillName` 正则静默跳过）、`description` 必填**，缺任一项技能不可见。
- 技能正文包含：角色设定、方法论、**输出契约**——要求用例以 json fenced block 输出 `DraftPointGroup[]` 同构结构（字段对齐 DraftCaseItem：case_key/title/priority/type/verification_goal/preconditions/test_data/steps[{step_no,action,expected}]/raw_md）。
- 创建会话后每轮 prompt 前缀注入一行纯文本：「本次对话请以 <skill description> 技能为准开展工作。」（DSH agent 经 skill 机制按需加载完整内容，不占每轮上下文。）**注意 CaseGen 现有提示词体系没有任何变量插值机制**（`default_prompts/optimize.md:11` 明文禁止 `{name}`/`{{name}}`/`${name}`；全仓 grep `.format(`/`Template(`/`render_prompt` 确认无插值；persona 只是提示词正文里的自由文本，如 generate.md 首行「# 角色与目标 你是资深金融交易系统测试工程师…」）⇒ 技能注入必须是纯文本拼接，不得引入模板变量。
- `GET /api/agent/skills` 建议 CaseGen 后端自己扫目录（少一次 DSH 依赖，部署机路径确定），而非转发 DSH `GET /skills?root=project&cwd=<workspace>`。
- 上下文注入沿用现有分节协议风格（`_build_messages`，task_pipeline.py:883-946 的事实标准分节标题：`# 需求 [REQ]`、`# Wiki 结构化知识`、`# 原文摘录（Source Chunks）`、`# 已确认测试点`…），并保留其**信任边界范式**：外部/参考内容单独一条 user 消息 + JSON 包裹（:941-945 注释 "a reference body cannot close a delimiter and change the surrounding prompt instructions"）——agent 投喂的材料同样按不可信内容处理。

### 3.5 转存为用例草稿（已确认的主出口）

流程：取最近一条 assistant 消息 → 提取 json fenced block → pydantic 校验 `DraftPointGroup[]`/`DraftCaseItem[]` → 落库 → 前端跳草稿确认页复用现有结构化渲染（el-collapse 卡片、`copyCaseMarkdown(raw_md)`、引用溯源）。

**表约束已核实（v1 的开放项在此收敛）**：

| 约束 | 位置 | 影响 |
|---|---|---|
| `CaseDraft.task_id: int` NOT NULL 且**无 FK 约束** | entities.py:522 | 草稿必须挂一个 task_id |
| `GenerationTask.requirement_id: int` NOT NULL 且**无 FK** | entities.py:310 | 任务必须挂一个 requirement_id |
| `TestCase.requirement_id` NOT NULL 且**有 FK** | entities.py:583 | 入库用例必须挂真实 Requirement |
| `TestCase.source_task_id/draft_id/case_key` **故意不建 FK** | entities.py:583 | 来源追溯字段可留空 |
| `finalize_task` 依赖测试点检查点链（`test_point_checkpoint` 必须 confirmed，且校验每段 "## TC-xxx" 有 P0/P1/P2 与合法测试点 key） | task_pipeline.py:2507 | 对话场景没有检查点链 ⇒ **不能直接复用 finalize_task** |

⇒ **决策：建虚拟 Requirement**（标题取自对话，project_id 同会话）比放宽 NOT NULL 代价小得多（放宽要动 `_migrate_*` + `import_cases_from_draft` + 唯一索引 `uq_test_cases_source_identity`）。入库走**专用导入路径**而非 finalize_task 状态机。**已确认采用方案 a**（m00202 需求方拍板）：

- **方案 a（推荐）**：新建「对话来源」的 shell GenerationTask（status 直接置 `generated`，requirement_id = 虚拟 Requirement）+ CaseDraft（`version=_next_draft_version()`、`content_md`、`prompt_version_ref="agent:<skill>:<conv_id>"`），再复用 `case_management.import_cases_from_draft(session, task, draft)`（:215）落 TestCase；跳过 review/finalize 状态机。白嫖草稿版本历史与现有草稿 UI。
- **方案 b（更轻）**：完全不建 task/draft，直接把解析出的用例按 `POST /api/cases` 语义写 TestCase（requirement_id = 虚拟 Requirement，source_task_id/draft_id 留空）。失去草稿版本历史。

两方案都保留「agent 产出 → 人工确认 → 入库」闭环。注意 `finalize_task` 的设计原则值得继承（注释："A malformed draft must leave the task retryable and must never commit a partial case set"）：**导入必须原子，格式不合法就整体失败且可重试**。

### 3.6 前端

改动清单（3 必需 + 4 新建）：

1. `router/index.ts`：children 加 `{ path: 'agent', name: 'agent-chat', component: AgentChatView, meta: { title: 'Agent 对话', description: '选择专家技能，与 Agent 多轮对话生成测试用例' } }`（守卫顺序见 :131-152，requiresAuth 自动生效）。
2. `MainLayout.vue`：`useCaseItems`(:46-51) 加 `{ path: '/agent', label: 'Agent 对话', icon: ChatDotRound }`（排 `/tasks` 后），:4-23 的 `@element-plus/icons-vue` import 加图标；router-link 的 `to` 必须沿用 `projectQuery`(:34-41) 否则丢项目上下文；`allMenuItems`(:76) 自动包含。
3. `views/AgentChatView.vue`（新建）：左列会话历史 + 中列消息流 + 顶部技能/模型选择器；布局沿用 `.page` > `.page-header`(h1.page-title + p.page-subtitle + .actions) + `el-card.block` 与 `--cg-*` 变量（`--cg-primary:#10b981`）；assistant 气泡正文用 `MarkdownView`。
4. `composables/useSseStream.ts`（**新建目录，当前无 composables/**）：实现与 tasks 侧同协议的 SSE 连接管理（snapshot 首帧、rAF 合帧 delta、线性退避重连 `attempt * 1000` 上限 3 次、`onUnmounted` close）。**实施调整（降风险）**：P1 **不重构 `TaskDetailView.vue`**——该文件 2514 行且带用户未提交 WIP（93 行改动），抽取共用会与之冲突；新 composable 参考其 `openLiveStream`(:791-880) / `scheduleLiveStreamReconnect`(:780-788) / `isCurrentLiveStream`(:640) / `queueLivePreviewDelta` rAF 合帧(:681-694) 的实现范式独立编写，AgentChatView 单独使用。TaskDetailView 迁移到该 composable 留作 WIP 合并后的独立任务。
5. `stores/agentChatStore.ts`（**新建目录，当前无 stores/**；Pinia 现仅 `authStore.ts` 一个，`projectStore.ts` 是模块级 reactive 单例）：会话列表、当前消息、流式 buffer、连接状态。**必须放 store 不能放组件**——`MainLayout.vue:279` 的 `router-view :key = projects.state.currentId`，切项目销毁整个视图并打断 SSE；`onUnmounted` 正确 `close()`，重进靠 `snapshot` 恢复。
6. `api/agent.ts`（新建）：遵循 `api()` 封装；stream URL 手工拼 `?project_id=`（`apiBlob` 与 EventSource 都不吃 client.ts 的自动注入）。
7. `components/agent/`：ChatPanel.vue、ChatMessage.vue、ChatComposer.vue（可复用 WorkbenchView 的 dropzone 与 `importRequirementDoc`）、SkillPicker.vue、CaseResultCard.vue（搬 DraftPointGroup/DraftCaseItem 渲染 markup）。`ToolCallCard` 不做（过程不展示）。`CitationList.vue`(554 行) 与 `ReviewCard.vue` 可原样复用于右栏溯源。

**前端约束与守则**：

- **四处硬编码状态集合**：若引入新任务状态，必须同步改 `api/tasks.ts` 的 `IN_PROGRESS_STATUSES`(:304)、`statusLabel`(:505)、`statusTagType`(:524) 与 `TaskDetailView.vue:214` 的 `LIVE_STREAM_STATUSES`。漏改第一处 ⇒ 既不轮询也不连 SSE；漏改 label ⇒ 显示英文原文。写入 implementer 任务书验收项。
- **进度条**：现有 `liveProgressInfo`(:101-110) 靠正则 `/\[(\d+)\/(\d+)\]/` 从 message 文本抠 `[n/m]`，是隐式约定。agent 步骤数不定 ⇒ `TaskStreamPayload`(api/tasks.ts:120-128) 扩展结构化 `progress?: {current, total, label}`，前端优先读它、回退正则、拿不到用不定进度条或纯文案。
- **性能**：`MarkdownView`(163 行，`MarkdownIt({html:false,linkify:true,breaks:true})`，html:false 天然防 XSS) 每次 content 变化**全量重 parse + 整块 v-html 替换**，长对话累积几十 KB 是 O(n²) ⇒ 只对「正在流式的最后一条消息」用 MarkdownView，流结束固化，历史消息折叠。当前依赖无 markdown-it 插件、无 shiki/highlight.js/prismjs，代码块无语法高亮（若需要则要新增前端依赖，本期不做）。
- **401 与断连区分**：SSE 的 401 表现为 `onerror`，现有逻辑当网络错误重连 3 次 ⇒ agent 侧重连前先 `getSession()` 探一次，或后端关闭前发带 reason 的 `failed`。
- **代码规模警戒**：`TaskDetailView.vue` 2514 行、`DocumentsView.vue` 1118 行、`WikiReviewView.vue` 954 行、`CitationList.vue` 554 行——**不要往里继续堆 agent 逻辑**，新功能一律放新文件 + composable。
- **唯一守门**：无 ESLint/Prettier/前端测试，`npm run build`（`vue-tsc -b && vite build`）的类型检查是唯一门槛，任何改动必须通过。

---

## 4. 线 B：流水线 agent 化（绞杀者模式）

### 4.1 原则

- 保留 `task_pipeline.py` 作为编排器与状态机；只把单个阶段的内部实现替换为 `agent_gateway.prompt_sync` 调用。
- env 开关：`PIPELINE_AGENT_STAGES=supplement,review`（空 = 全旧路径）。每阶段独立可回退。
- agent 阶段输出必须过与旧路径相同的 JSON 契约校验（pydantic），不达标记 warning 并可配置回退旧路径重跑一次。
- 对外事件不变：agent 阶段仍发既有 stage 级 status/notice（匿名化文案），前端零改动。
- **⚠️ 不得占用 chat hook 槽位**：`task_pipeline.py:57-63` 与 `api/tasks.py:104-109` 各有一份模块级钩子（`_PIPELINE_CHAT_FN` / `_GENERATE_CHAT_FN` / `_TEST_POINTS_CHAT_FN` / `_REVIEW_CHAT_FN` / `_OPTIMIZE_CHAT_FN`）；只要钩子非 None，`_force_sync`(api/tasks.py:178) 就把创建/生成改为**请求内同步执行**——这是 60+ 测试文件依赖的语义。agent 路径必须走显式参数或 `PIPELINE_AGENT_STAGES` 配置判断，**绝不设置这些钩子**。
- **注入缝选这三个（唯一收口，改造成本最低）**：① `task_pipeline._call_chat(chat_fn, *, model, messages, stage_fn=None, stream=False, on_attempt=None, on_delta=None, on_retry=None) -> str`（:1178，配套 `_resolve_chat_fn` :1165）——生成/评审/优化的唯一收口，已支持三个回调，返回值只要 str（usage 被丢弃）；② `scenario_coverage.run_coverage(..., call, ...)` 的 `call(stage, body)`（messages 由 task_pipeline.py:1573-1624 的 `coverage_messages` 组装）——测试点阶段唯一收口，契约是「给 JSON body，回 JSON 文本」；③ `chat_fn` 显式参数链（`run_generate`/`run_review`/`run_optimize_prompt`/`ingest_document` 全部接受注入）。
- 阶段内 agent 调用不得长持 DB Session；每阶段 commit 的既有约定（task_pipeline.py:2038）保持。
- 回归门：任何线 B 改动必须保持 `pytest -q` 全绿。**实测基线（分支 `feat/dsh-agent-integration`，2026-11-04）：702 passed, 5 warnings in 263.64s**——deploy README 记录的 350 passed 已过期（用户 WIP 新增了大量测试）。**全量跑约 4.5 分钟，implementer 迭代时应先用 `pytest -q tests/test_agent_*.py` 跑增量，仅在交付前跑全量**。`backend/tests/` 覆盖 stream/jobs/locks/stop/gateway/proxy/point-by-point/review/checkpoint/evidence；pytest.ini: pythonpath=., asyncio_mode=auto。

### 4.2 试点阶段选择

| 阶段 | 是否试点 | 理由 |
|---|---|---|
| `coverage_supplement`（定向补漏） | ✅ 第一批 | 「给证据、找缺口、写补丁」，工具增强收益最大；输出契约相对松；每任务仅 1~2 次调用 |
| `review`（评审） | ✅ 第一批 | 本质是找茬 + 建议，agent 可翻知识库对照；`run_review`(:2218) 非流式，单次调用 |
| `coverage_generate`（分组生成） | ❌ 排除 | 强格式契约，agent 自由度是负资产；调用频次高 |
| 逐点 Markdown 生成 | ❌ 排除 | 生产 `point_by_point` 恒为 True（:1946-1960，无 API 字段可控），调用次数 = 测试点数量 |
| 检索 / 证据确认 | ❌ 不动 | `hybrid_retrieve`(:230)/`project_hybrid_retrieve`(:358) **全程无 LLM**（FTS + 词法打分 + 融合 + 一跳扩展），`expand_evidence` 亦确定性无模型调用；已有人工检查点 |

**调用量级闸门（必须做）**：`scenario_coverage.py:16-36` 预算 MAX_CALLS=192、MAX_POINTS=200、MAX_POINTS_PER_TASK=120、MAX_CALL_CHARS=64000、MAX_INPUT_CHARS=4000000。若把高频阶段换成 agent 轮次（首帧数十秒~分钟级），单任务总耗时数量级放大 ⇒ 试点只允许**每任务调用次数为个位数**的阶段，并加 `AGENT_STAGE_MAX_CALLS` 预算闸门（超限即回退旧路径）。

### 4.3 阶段内 agent 上下文

流水线阶段的 agent 调用同样用独立工作目录（`data/agent-workspace/task-<task_id>-<stage>/`），材料（测试点清单、证据摘录、回归清单）由后端预先写入；prompt 要求输出单个 JSON fenced block；只取 content 解析。注意 `_resolve_prompt_by_type` 取的模板在某些阶段只是 provenance（task_pipeline.py:1573-1624 注释 "The legacy template remains provenance, never a stage instruction."），agent 阶段同样以 Python 拼装的 stage 契约为准。

---

## 5. P0：零代码验证

把 DSH OpenAI 兼容端点注册为一条现有 ModelConfig：`base_url=http://127.0.0.1:31800/api/v1`、`api_key` 任意非空、`model_name="provider/model"`（如 `magpie/qwenwork/qwen3.8-max-preview`）、`protocol="chat_completions"`。现有全链路（生成/测试点/评审/优化/Wiki 摄入）立刻可跑，DSH README 亦建议三方集成优先走 /chat/completions。

实施注意（后端调研核实）：

- `chat_completion` 会自动补路径（`llm.py:104 build_chat_completions_url`，裸域名补 `/v1/chat/completions`）；**必须用 `protocol="chat_completions"`**——`responses` 协议走 `build_responses_url`(:124)、把 system 折进 instructions，且**强制 `stream=True`**（:390-391 注释 "Codex / Responses API reverse proxies and channels require stream=true"），与 DSH 端点语义不匹配。
- 验证用现成的 `POST /api/models/{id}/ping`（`api/models_cfg.py:137-160`，`stream=True, max_tokens=32, max_retries=1`，LLMError → HTTP 400）——同时也是「DSH 端点是否支持流式 + max_tokens」的探针。
- 选 model_name 注意特判：以 `deepseek-v4-` 开头会在 wiki 摄入侧强制 `thinking=False` + `response_format={"type":"json_object"}`（`wiki_ingest.py:255`），ping 侧也强制 `thinking=False`。
- 重试/错误语义已就绪：`_RETRYABLE_STATUS={408,429,500,502,503,504}`(:14)、max_retries=3、退避 `backoff_sec*(attempt+1)`(:497)；`LLMError`(:30) 带 category/http_status/attempts/retryable，`_ERROR_CATEGORIES`(:19-23) 含 stream_incomplete（既无 [DONE] 又无 finish_reason 时判为可重试，:342-349 注释 "a clean TCP EOF is not an application-level completion signal"）。
- `ModelConfig.api_key` 明文存 `data/meta/app.db`（表名 `models`，唯一默认走部分索引 `uq_models_single_default WHERE is_default=1`，entities.py:31-36），`GET /api/models` 经 `mask_api_key`(:19) 脱敏；DSH 这条填任意占位值即可，但**该库文件绝不能进 agent 工作目录**（见 §8）。

---

## 6. DSH 工程语义对策表（实测所得）

| 事项 | DSH 语义 | CaseGen 对策 |
|---|---|---|
| 首帧延迟 | 可达 5–8 分钟（模型加载）；重复发 prompt 整轮重跑 | httpx read timeout ≥600s；前端「agent 启动中」文案；**绝不自动重试 prompt** |
| 超时模型 | stall watchdog（流式默认 300s 静默才断），非墙钟 | 长任务持续产出即安全；不用总时长判死；区分「首帧超时」与「静默超时」两种配置 |
| 并发 | 同 session 再发 prompt 报 agent-busy | 会话级互斥 + 全局信号量；cancel 或 `mode:"steer"` 兜底 |
| 心跳 | SSE 每 15s `: ping` 注释帧 | 转发层消费并向浏览器自发 `: keep-alive`（对齐现有 15s） |
| 响应包封 | `{ok,data,timestamp}` / `{ok:false,error}` | gateway 统一解包映射为 CaseGen 错误格式 |
| 端口 | 31800 为固定 standalone 端口（DSH 主 webserver 端口每次启动变） | .env / run.bat 增 `DSH_BASE_URL`，默认 `http://127.0.0.1:31800/api/v1` |
| 鉴权 | 当前 authEnabled=false、apiKeyCount=0、bind 127.0.0.1、adminTokenPath=`~/.dsh/dsh-web-service/admin-token` | 保持现状：DSH 不出本机，CaseGen 后端代理即边界；gateway 预留 header 注入点以备将来开启 |
| 技能 frontmatter | `name` kebab-case + `description` 必填，否则静默跳过 | skills 目录加 CI 校验脚本（P2 顺带） |
| 项目根解析 | `findProjectRoot` 向上找 `.git`，优先 `.agents/skills` 回退 `.dsh/skills` | 会话 cwd 放 `data/agent-workspace/<id>` 仍解析到仓库技能根（已实测验证） |

---

## 7. 配置项汇总（backend/app/config.py + .env.example + deploy/win10/run.bat）

```
DSH_BASE_URL=http://127.0.0.1:31800/api/v1
AGENT_ENABLED=true                  # win10 目标机有 DSH（已确认），默认开；false 时隐藏菜单 + API 410
AGENT_MAX_CONCURRENT=4              # 全局并发 agent 轮次（进程内信号量）
AGENT_WORKSPACE_ROOT=data/agent-workspace
AGENT_FIRST_TOKEN_TIMEOUT_SEC=600   # 首帧等待上限
AGENT_STALL_TIMEOUT_SEC=300         # 对齐 DSH 默认静默阈值
AGENT_STAGE_MAX_CALLS=4             # 线 B 单任务 agent 调用预算闸门
PIPELINE_AGENT_STAGES=              # 线 B 开关，逗号分隔 stage 名，空 = 全旧路径
```

`AGENT_ENABLED=false` 时：前端菜单项隐藏（经 `GET /api/auth/bootstrap` 下发开关）、`/api/agent/*` 返回 410、流水线走旧路径——保证无 DSH 环境可退化运行。

**加载方式与 win10 坑（已核实，必须处理）**：`config.py`(89 行) 是**纯模块级 `os.getenv`**（无 pydantic BaseSettings、无 dotenv 加载、无校验层），.env 靠 `uvicorn --env-file .env` 注入环境变量（`.env.example` 头两行明说 "Copy to .env and pass it to Uvicorn with --env-file .env"）。但 **`deploy/win10/run.bat:24` 的启动命令是 `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`，没有 `--env-file`** ⇒ win10 部署实际只吃 config.py 默认值 + 系统环境变量。因此上述新增项必须：① 在 `config.py` 给出可用默认值（DSH_BASE_URL 默认回环地址、AGENT_ENABLED 默认 true）；② 在 `run.bat` 里显式 `set DSH_BASE_URL=...` 等，或给启动命令补 `--env-file .env`；③ `deploy/win10/README` 写清两种途径。另注意 `.env` 里现有约定是**不放任何模型密钥/base_url**（模型是 DB 行，靠 UI 配置），DSH_BASE_URL 属于服务地址而非密钥，可入 .env。

---

## 8. 安全边界

1. **工作目录隔离**：agent cwd 一律 `data/agent-workspace/<id>`，绝不指向仓库根（DSH preset 文件权限高，指向仓库根 = agent 可改源码、读 SQLite 内各模型明文 API Key）；session 创建后 `PUT /sessions/:id/permission` 压到 workspace-write。
2. **材料单向投喂**：agent 需要的上下文由后端复制进工作目录；agent 产出经 save-as-draft / 契约校验后才进主库。**`data/meta/app.db` 内含各模型明文 API key（`ModelConfig.api_key`），`data/meta` 及任何含它的路径都不得成为 agent cwd 或被复制进工作目录**；工作目录与 `data/meta` 平级隔离。`data/` 下路径天然被 .gitignore 覆盖且由 `ensure_data_dirs()`(config.py:74-89) 随部署创建。
3. **过程数据三不**：不落库、不透传、不进日志（gateway 对 reasoning/tool_result 只计数不留内容；DSH 侧 history 留全量仅供本机排查）。`TaskEvent.detail_json` 只写匿名化阶段文案。
4. **错误消息白名单化**：gateway 错误必须像 `llm.py:44 safe_model_error`（只导出白名单原语，"永不外泄异常文本/端点/代理凭据"，`_http_client_error` :62 注释明确要求）与 `external_wiki_client.py:16` 那样处理，**不得把 DSH 原始响应体/URL/凭据透给前端**。
5. **CSRF/鉴权**：对话写操作走现有 POST + 同源校验（+ 可选双提交 CSRF token）；stream GET 免 CSRF 但必须校验 cookie session + 会话归属项目（`/api/agent` 不在受控前缀内，需自建校验，见 §3.3）。
6. **路径安全**：若 agent 需要读取 CaseGen 托管文件，复用 `app/api/wiki.py` 已有的符号链接/穿越防护范式（`_verify_secure_path` :284），不新写一套；DSH 侧 `GET /files/download` 本身禁 `../`。
7. **win10 部署**：`deploy/win10/README` 增 DSH 运行时安装、dsh-webapi 插件启用、`DSH_BASE_URL` 配置（含 run.bat 无 --env-file 的说明）、模型 provider API key 配置章节；run.bat 启动前探活 `GET /api/v1/presets`，失败则提示设 `AGENT_ENABLED=false`。

---

## 9. 落地顺序与验收

| 阶段 | 内容 | 验收 | 量级 |
|---|---|---|---|
| P0 | DSH 注册为 ModelConfig（protocol=chat_completions） | `POST /api/models/{id}/ping` 通过；现有流水线用 DSH 模型跑通一次生成 | 零代码 |
| P1 | agent_gateway + 两表 + 对话 API + AgentChatView MVP（技能 = prompt 前缀）+ **新建** `composables/useSseStream.ts`（不重构 TaskDetailView，见 §3.6-4）+ agentChatStore | 多轮对话流式回显；刷新可恢复（snapshot 首帧）；cancel 生效；**浏览器端 SSE 抓包全程无 reasoning/tool_call/tool_result 事件**；非活跃状态 stream 返回 409；`/api/agent` 项目归属校验生效；`pytest -q` 保持 702 passed 基线 + `npm run build`（vue-tsc）通过 | 后端 2–3 天，前端 1–2 天 |
| P2 | 新建 `.agents/skills/`（当前不存在）+ 测试专家技能 + save-as-draft 闭环（虚拟 Requirement + 专用导入路径，**方案 a 已确认**）+ skills frontmatter CI 校验 + **bootstrap 下发 agent_enabled**（P1 遗留项：handler 在 backend/app/api/auth.py:91-101 bootstrap() 与 schemas/auth.py:16-18 BootstrapOut，均不在 task-1 write_scopes，P1 跳过；前端软降级 410→agentAvailable=false，P2 闭环后前端再消费开关）| 测试专家技能生成用例 → 转存草稿 → 人工确认 → 入库全流程走通；导入原子性（格式非法整体失败且可重试）；两个回归门保持绿 | 1–2 天 + 技能内容打磨 |
| P3 | 流水线试点 supplement/review + 契约测试 + `PIPELINE_AGENT_STAGES` / `AGENT_STAGE_MAX_CALLS` 开关 | 开关两态输出均过 pydantic 契约；pytest 覆盖 agent 路径（mock gateway，**不访问真实模型**，用 `tmp_app_data` 隔离）；确认未设置任何模块级 chat hook；前端事件兼容、四处状态集合同步 | 2–3 天 |
| P4 | 并发压测 + win10 部署文档与探活 | 4 并发对话稳定；`AGENT_MAX_CONCURRENT` 排队生效；win10 包在全新目标机按文档装通（含 run.bat 环境变量） | 视结果 |

每阶段独立可交付、可回退；P1/P2 纯增量不碰存量链路。按 AGENTS.md 分工：主代理出方案与验收，实现委派 implementer，独立审查委派 reviewer，前后端调查委派对应 explorer。

---

## 10. 风险与开放问题

| 风险 | 缓解 |
|---|---|
| DSH 首帧 5–8 分钟体验差 | 前端「启动中」文案 + 会话预热（进入 Agent 页即 ensure_session）；P4 评估模型常驻 |
| 单机 DSH 并发容量未知；单进程约束下只能进程内排队 | `AGENT_MAX_CONCURRENT` 信号量排队；P4 压测定值；多 worker 扩容需先引入共享 broker（Redis），本期不做 |
| agent 输出 JSON 契约不稳 | 技能内 few-shot 示例 + save-as-draft 严格 pydantic 校验 + 失败降级导出 Markdown；线 B 契约测试守门 + 回退旧路径重跑一次 |
| 高频阶段 agent 化导致耗时数量级放大 | §4.2 排除 coverage_generate 与逐点生成；`AGENT_STAGE_MAX_CALLS` 闸门 |
| 误设模块级 chat hook 使全部任务变同步执行、测试路径漂移 | §4.1 明令禁止；code review + reviewer 独立审查列为验收项 |
| agent 长轮次期间持有 SQLite 写锁 | gateway 调用前后即取即放 Session；参考 `_load_stream_task_status`(api/tasks.py:145) "load then close eagerly"；沿用每阶段 commit |
| SQLite 整数 id 复用导致串流 | 对话用 uuid 主键；任务侧沿用现有 `task_stream.discard(task.id)`(api/tasks.py:530) + `stream_id` 机制 |
| win10 无 --env-file 导致配置不生效 | §7 三路处理（config.py 默认值 + run.bat set + README） |
| DSH/插件升级破坏端点语义 | gateway 单点封装 + P0 的 /chat/completions 兜底路径 |
| DSH 将来开启鉴权 | gateway 预留 header 注入点；密钥走 .env/run.bat，不入 SQLite 明文 |
| Wiki 摄入链无 SSE/TaskEvent | 本期不 agent 化摄入链（§1.3 非目标）；将来需另建进度协议 |
| `.agents/skills/` 部署包遗漏（gitignore 风险已排除，实测未被忽略） | P2 把技能目录纳入 deploy/win10 打包与安装清单，并在验收中确认目标机 `.agents/skills/*/SKILL.md` 存在且可被 `GET /api/agent/skills` 扫到 |
| win10 目标机仓库路径不同导致 `findProjectRoot` 向上找不到 `.git` | 部署方式若非 git clone（如复制目录），`.agents/` 仍在应用根 ⇒ `findProjectRoot` 回退 cwd 后**技能根会解析到 `data/agent-workspace/<id>/.agents/skills`（不存在）**。缓解：gateway 建 session 时把 cwd 设为**仓库根下的 agent 工作目录**并在 P4 验证；或在工作目录内 symlink/复制 `.agents/skills`；或改用 DSH `customSkillDirs` 显式指定绝对路径（需 DSH 侧配置，优先级最低） |

**遗留项已闭环**：§3.5 入库路径确认为方案 a（shell GenerationTask + CaseDraft + import_cases_from_draft）。

---

## 附录 A：本次调研依据

- **DSH webapi 实测**：sessions / prompt-stream / prompt(同步) / events / cancel / history / todos / questions+answers / permission / files(+resumable 分片) / presets / models / skills 端点；SSE 事件类型与 15s ping；stall watchdog 语义；`{ok,data,timestamp}` 包封；SKILL.md 守则（首帧勿判死、agent-busy 处理、401 熔断、固定 standalone 端口）。
- **技能根解析实测**：cwd=`data/agent-workspace/probe-cwd` 时 `GET /skills?root=project` 正确返回仓库 `.agents/skills`（`findProjectRoot` 向上找 `.git`，`skill-utils.js:26-63`）；frontmatter `name` 须 kebab-case（`isSkillName` :21 + `SKILL_NAME_RE`）、`description` 必填，否则 `parseSkillFile`(:68-106) 返回 undefined 被静默跳过；`discoverSkills`(:297) 支持目录版 SKILL.md 与扁平 .md。
- **前端调研（frontend_explorer，29KB 报告）**：SSE 7 事件协议与 rAF 合帧实现（TaskDetailView.vue:791-880, :681-694）、EventSource header 限制、router-view :key 销毁语义（MainLayout.vue:279）、四处状态集合、`points_with_cases` 契约、零聊天 UI 存量、MarkdownView 全量重 parse 风险、vue-tsc 唯一守门、`PromptCreate.type` 是自由 string（备选技能载体）、依赖版本清单。
- **后端调研（backend_explorer 全量报告）**：12 个 router 路由清单与行号；task_pipeline 端到端流水线（检索 → 检索检查点 → 测试点覆盖 → 测试点检查点 → 逐点 Markdown 生成 → 评审 → 优化 → finalize）与 lean 降级路径；`chat_completion` 签名与双协议（llm.py:353-372, :104, :124, :390）；三个注入缝 `_call_chat`(:1178) / `run_coverage(call)` / `chat_fn` 参数链；`_force_sync`(api/tasks.py:178) 与模块级 chat hook 语义；`task_state.py` ALLOWED 状态机全集与 409 映射；三套异步机制（BackgroundTasks + task_jobs 新 Session / DB 租约 daemon 线程 `recover_generation_jobs`:312 + `_consume_resume_claim`:118 + `_heartbeat_resume_claim`:173 / `WikiJobScheduler` 单 worker ThreadPoolExecutor）；`task_locks` 每任务 RLock；协作式停止 `_stop_task_locked`(:1109) 与删除保护 `_delete_task_locked`(:1156)；`TaskStreamBroker` 硬约束与容量参数（max_tasks=128 / max_events_per_task=512 / max_preview_chars=256000 / terminal_ttl_sec=120）；`GET /{id}/stream` 的 sync def + async generator + 两个 409 分支 + 15s `: keep-alive`；9 个内置提示词的 persona 与输出契约、无变量插值（optimize.md:11）、`_resolve_generate_prompt`(:108-130) 三级优先级、`BUNDLED_PROMPT_VERSION=8` 播种不覆盖用户 active 模板；实体表约束（CaseDraft.task_id / GenerationTask.requirement_id NOT NULL 无 FK、TestCase.requirement_id NOT NULL 有 FK）；`db.py` 手写迁移序列（:732 init_db → 8 个 `_migrate_*`）与 create_all 边界、`get_engine`(:16) check_same_thread=False；`config.py` 纯 os.getenv + run.bat 无 --env-file；requirements.txt 10 项与 win10 wheels 实测版本（httpx 0.28.1 / pydantic 2.13.4 等）；CSRF 同源中间件（main.py:120-138）、`_PROJECT_SCOPED_PREFIXES`(projects.py:120-142)、CORS 禁通配（config.py:65-71）；`external_wiki_client.py` 外部 HTTP 调用范式与错误安全约定；`safe_model_error`(llm.py:44)；`scenario_coverage` 预算 MAX_CALLS=192；`case_management` 的 `split_case_draft`(:83)/`import_cases_from_draft`(:215)/`aggregate_draft_cases_by_points`(:499)；tests 回归门（deploy README 记 350 passed 已过期；2026-11-04 于 `feat/dsh-agent-integration` 实测 **702 passed, 5 warnings in 263.64s**）。
