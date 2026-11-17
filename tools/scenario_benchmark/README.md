# 只读下载已有任务的场景快照

[fetch_scenarios.py](<tools/scenario_benchmark/fetch_scenarios.py>) 是独立 Python 3.11+ 标准库 CLI；不导入 CaseGen、不开数据库、不读 `.env`/密钥文件、不安装依赖、不调用 LLM、不创建或生成任务。用途是下载平台**当前测试点**，作为后续离线评价的场景材料；它不是评价器，也不从最终 TestCase 反推场景。本轮验证仅使用临时本地模拟 HTTP 服务，无真实平台采样。

## 已核实的 API 与限制

- 路由前缀是 `/api/tasks`：[API:99](<backend/app/api/tasks.py#L99>)。唯一下载请求为 `GET /api/tasks/{task_id}/test-points`：[API:814–824](<backend/app/api/tasks.py#L814-L824>)。返回**顶层 checkpoint 对象**，不是 `{checkpoint, points}` 包装；不存在分页参数。
- 返回 checkpoint 的 `id/task_id/retrieval_checkpoint_id/attempt/version/status/points/idempotency_key/created_at/updated_at`：[schema:212–222](<backend/app/schemas/tasks.py#L212-L222>)。每个 point 包含 `id/task_id/checkpoint_id/stable_key/title/verification_goal/dimension/priority/sort_order/is_selected/is_excluded/citation_ids/created_at/updated_at`：[schema:193–209](<backend/app/schemas/tasks.py#L193-L209>)。实际序列化：[API:767–805](<backend/app/api/tasks.py#L767-L805>)。这个响应**没有任务的 service status，也没有 module/category**，对应输出为 `null`；若未来响应明确含 `task_status` 或 point `module/category`，才按原值保留。
- 选择最大 attempt：[service:187–192](<backend/app/services/test_points.py#L187-L192>)。points 按 `sort_order, id` 返回，CLI 保留此顺序，包括未选择/排除的点，不自行筛选。
- 人工 PUT 会调用 `write_points` 并增加 version：[API:827–861](<backend/app/api/tasks.py#L827-L861>)。confirm 也会重写 points 后标记 confirmed：[API:875–901](<backend/app/api/tasks.py#L875-L901>)。`write_points` 删除旧 points 后重新写入：[service:291–324](<backend/app/services/test_points.py#L291-L324>)。因此既有 `pending`、`confirmed` 均可下载，**confirmed 或 version=1 都不证明未经人工修改，更不证明首次模型生成原文**。point id 是当前数据库点的身份，重写后的 id 可能变化；`TP-task-point` 只在同一当前 point 身份下稳定。
- 当前 `generation_granularity` 只有 compact/standard/detailed：[schema:28](<backend/app/schemas/tasks.py#L28>)。本工具不提供或改造 `scene_only`。
- 不访问任务生成/confirm/PUT/DELETE、不调用 `/api/cases/export`，不调用最终 cases/drafts、不另发 task status 请求。这里“只读”仅指不写场景业务；已有Cookie的认证过程可能更新last_seen_at、删除过期会话并commit，见 [auth:193–205](<backend/app/services/auth.py#L193-L205>)，不是整个服务请求数据库零写。
- 在已有任务 awaiting_test_point_confirmation、未人工编辑/确认时尽早下载proposal有利于捕捉当前结果，但仍不等于原始LLM响应：解析、修复或fallback可能已发生；本CLI不读取events，不能识别fallback。所有manifest编辑状态保持unknown，不以checkpoint状态或version推断未经修改。

## 运行（由运行者提供地址和已有 id）

下列地址和 `123` 都只是占位示例，不是用户真实服务或 task。`--base-url` 必填，不猜本机端口，也不是 Harness 的 3080。约定填写**不带 `/api` 的服务 root**，允许反向代理 path prefix，例如 `https://casegen.example.invalid/team/casegen`。拒绝 userinfo、query、fragment、非 HTTP(S)、dot segments；task/project id 必须正整数。

```bash
python tools/scenario_benchmark/fetch_scenarios.py --help
python tools/scenario_benchmark/fetch_scenarios.py \
  --base-url http://127.0.0.1:8000 \
  --task-id 123 \
  --output-dir /tmp/casegen-snapshot-123
```

可选：`--category {AUC,AMT,ACC,OPN,CLS,CAN}` 为**用户指定归类**，不是服务已有 taxonomy；默认 `null`。不将 positive/negative/boundary 或 normal/edge 等自动映射为六类。`--timeout 30` 指定有限正数秒的网络 socket 超时与响应读取 elapsed budget；不是整个程序/OS DNS 的严格墙钟截止，极端阻塞读取可能跨过 elapsed budget。响应最大 **8 MiB**。

### 认证与 project scope

当前源码**存在 Cookie 会话认证中间件**：`AUTH_ENABLED` 开启时读取会话 cookie：[main:101–120](<backend/app/main.py#L101-L120>)。任务 API 没有自己的额外 auth 参数，不等于全局公开。任务路径也属于 project-scoped 路径：[projects:120–142](<backend/app/services/projects.py#L120-L142>)，启用 auth 时需要 `project_id` query：[main:140–161](<backend/app/main.py#L140-L161>)，且必须与任务一致：[projects:107–111](<backend/app/services/projects.py#L107-L111>)。本 CLI 不读取配置去猜认证开关/项目 id。需要时显式提供：

```bash
# 运行者事先安全设置 CASEGEN_EXISTING_COOKIE 为已有完整 Cookie header；此处不展示凭据。
python tools/scenario_benchmark/fetch_scenarios.py \
  --base-url https://casegen.example.invalid/team/casegen \
  --task-id 123 --project-id 456 \
  --cookie-env CASEGEN_EXISTING_COOKIE \
  --output-dir /tmp/casegen-snapshot-auth-123
```

`--cookie-env NAME` 仅从指定环境变量读取已有完整 Cookie header。也可选择 `--bearer-token-env NAME`，用于另配支持 Bearer 的反向代理；**当前 CaseGen 中间件不是 Bearer 登录**。两者互斥。不开自动登录、不拿模型 key，不打印变量值或保存发出的请求 headers。空变量、不安全控制字符/非 ASCII header、Bearer 中空白或无有效非空 cookie 的格式会报错；回显完整 auth header 或其中任一 cookie value 均拒绝保存。日志/错误不回显任意服务 body、Location、异常详情或无效参数值。保留 stdlib 正常 TLS 验证，不提供 insecure；**拒绝所有 3xx、不跟随 Location**，提示提供正确地址，避免把 Authorization/Cookie 转送跨站。

urllib 默认代理行为依赖运行者的 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` 等网络环境；本工具不会自行读取 `.env` 或登录。不读取未显式选择的认证变量（网络代理环境是 stdlib 行为）。

## 三个输出文件

目录必须新建或已有空目录；非空目录默认拒绝，无 force。网络、schema、映射、序列化全部完成后才创建输出文件，使用排他创建，不覆盖。正常成功恰有以下三文件：

```text
<output-dir>/
  api_response.json
  scenarios.json
  manifest.json
```

1. `api_response.json`：保存有效 JSON 响应的**收到的原始 bytes**，不重新排版、不重排 keys。manifest 记录收到 bytes 数量和 SHA-256，与保存文件的 hash 一致。要求 UTF-8（允许 BOM），接受 application/json 或 +json。不写发出的 auth headers。当前实际 schema 不包含模型密钥/登录 token；`idempotency_key` 是 API 返回的确认幂等标识，按 schema 原样保留，不当成会话 token。出现明确敏感 key（如 authorization/cookie/password/api_key/access_token/resume_claim_token）或回显所提供凭据时，**拒绝整次导出**，不偷偷 redaction、也不假称修改后 raw 仍原样。业务文本不按任意关键词替换；未来新增敏感字段须审查后再适配，未知字段不能保证绝对不敏感。
2. `scenarios.json`：`{schema_version, source_task_id, scenarios: [...]}`。每条 `id="TP-{task_id}-{point.id}"`；`title` 原样来自 title，`description/test_focus` 原样来自 verification_goal；`text=title + "\n" + verification_goal` 供 judge quote，并声明为原字段拼接而非 LLM 润色。保留 Unicode/空白/业务措辞，不臆造前提、步骤、期望结果或 TestCase。`source_task_id/source_point_id/source_checkpoint_id` 保留身份；`source_metadata` 保留完整 point；未知信息仍在 raw。`dimensions=[原dimension]`，`sub_scenarios=[]`；`source_module/source_category` 无字段则 null。`category` 默认 null，提供参数时 `category_source="user_cli_override"`。
3. `manifest.json`：UTC 抓取完成时间、确切 endpoint（包含显式 project query）、GET、任务/项目/attempt/checkpoint/status/version、retrieval checkpoint、实际返回才记录的 service task status、counts、两数据文件 bytes/SHA-256、字段映射、分类来源与全点保留策略。manifest 不做自引用 hash（明确说明）。固定 `provenance="current_service_snapshot"`、`human_edit_status="unknown"`、`first_generation_unmodified="unknown"`，不假冒首次原始输出。

输出是服务当前快照，可能含人工修改；下载期间服务若并发修改，API 不提供不可变首次生成保证。本 CLI 只保存一次 GET 实际收到的响应。缺 checkpoint（常见 404）、未生成/空 points、缺字段、空 title、非字符串verification_goal、重复 point id、错误类型/关联、JSON 重复 keys、非标准 JSON number、HTML/非 JSON、4xx/5xx、TLS/超时/过大响应都非零退出，且校验失败不创建输出目录。verification_goal 键必须存在且为str，但可为空或仅空白；原样保留到description/test_focus，空值时text=title+换行，标题可独立表达业务场景，不补写描述。空 points 默认当作“无产物，待运行者确认”，不伪装合法零场景成功。参数解析失败退出 2；其他明确错误退出 1；成功退出 0。

## 标准库测试（仅 mock 服务）

测试代码：[test_fetch_scenarios.py](<tools/scenario_benchmark/tests/test_fetch_scenarios.py>)。产物只存在于各测试自有 `tempfile`，不读数据库，不生成客户场景/新评价基准。运行：

```bash
python -B -m unittest discover -s tools/scenario_benchmark/tests -p 'test_*.py' -v
python -m py_compile tools/scenario_benchmark/fetch_scenarios.py tools/scenario_benchmark/tests/test_fetch_scenarios.py
python tools/scenario_benchmark/fetch_scenarios.py --help
```

真实采样还需运行者的 **base URL、已有 task id**；认证启用时另需其对应 **project id 与已有 session cookie**（或代理已有 bearer）。本轮不向真实平台执行上述下载命令。


## 可恢复裁判执行器 v1.1.1

[evaluate_scenarios.py](<tools/scenario_benchmark/evaluate_scenarios.py>) 不导入平台、不创建任务；复用冻结校验器函数（从不调用其写全局QA的main），独立执行模型语义判定。[requirements.txt](<tools/scenario_benchmark/requirements.txt>) 声明Python 3.11+、PyYAML及jsonschema；当前系统 `/usr/bin/python3` 已有这些包，不联网安装。原 fetch CLI/测试保持原SHA和行为。

### 显式执行与输入隔离

所有命令默认 dry-run：仅解析公开provider描述（ID、HTTPS endpoint及凭据引用名）并组装输入/schema/prompt/messages/请求文件，不读取凭据值、无认证、无HTTP；只有 `--execute` 才执行真实裁判请求。dry-run输出目录不得复用为实跑，因为execute参数也是恢复指纹。

```bash
/usr/bin/python3 -B tools/scenario_benchmark/evaluate_scenarios.py single \
  --input INPUT.json --output-dir NEW-RUN --model mimo-v2.6-flash \
  --harness-provider new-api

# 已授权三样本smoke；NEW必须尚不存在
/usr/bin/python3 -B tools/scenario_benchmark/evaluate_scenarios.py calibrate \
  --dataset data/evaluation/scenario-benchmark-v0.1/calibration_samples.json \
  --sample-ids S001 S002 S004 --output-dir NEW-RUN --model mimo-v2.6-flash \
  --harness-provider new-api --workers 2 --max-repairs 2 --execute
```

`calibrate`省略sample-ids选择dataset全体40，但需后阶段另外审核授权；smoke不擅自扩大。只把各sample.input送给裁判，绝不发送包装tags/derivation/expected_judgments；报告阶段独立读取参考。模型判断coverage，代码只做结构检查与确定性统计，不以参考差异重试。

服务采样输出可直接进入评价：

```bash
/usr/bin/python3 -B tools/scenario_benchmark/evaluate_scenarios.py single \
  --scenarios SNAPSHOT/scenarios.json --task-definition TASK-ALL \
  --baseline data/evaluation/bidding-v0.1/baseline.json \
  --output-dir NEW-SNAPSHOT-JUDGE --model mimo-v2.6-flash
```

任务定义只用于选择已有gold原行和范围，生成请求不进入裁判input；无生成API调用。保留服务原字段与current_service_snapshot、human_edit_status=unknown，不假称首次模型输出。空候选在judge输入允许，fetch仍维持其原有空产物拒绝规则。

### 安全认证

[安全模块](<tools/scenario_benchmark/safe_http.py>) 继承审核过的direct runners防护。默认profile为 `HOME/.dsh/profiles/desktop/cordis.patch.yml`，凭据为 `HOME/.dsh/.credentials.yaml`；可用harness-profile选择profile。只匹配唯一provider，并仅解析选中apiKeyEnv对应version1 refs ScalarNode，检查凭据owner-only。密钥只在进程和Authorization头，不打印、不存、不进模型。

可显式 `--base-url https://HOST/v1 --api-key-env EXISTING_KEY_ENV`；仅指定api-key-env也只使用环境变量，缺失绝不自动回退读取secret配置。拒绝HTTP、userinfo/query/fragment及dot路径；TLS校验、拒绝所有redirect、不转发认证；120秒socket/read预算（不是OS DNS全局硬deadline），响应2MiB。记录安全业务响应和selected request-ID响应头，不存任何request/auth headers。credential exact/URL/JSON spelling及auth/Bearer/key模式扫描与redaction，发现echo作为技术失败而不是成功结果。

### wire与修复边界

[版本化prompt](<tools/scenario_benchmark/prompts/evaluator_v1.1.md>)继承原v1.0业务/粒度/引文政策，仅改结构协议并移除无关示例ID。动态wire1.1的baseline_judgments/generated_judgments为真实ID键控对象，properties/required恰全集，additionalProperties=false，每项ID与input_id是const。保存原content，严格拒绝重复JSON键、NaN、非object；先验证wire，再按input顺序把map确定性转为旧schema1.0数组，只改schema_version及容器，不过滤未知ID、不去重、不改coverage/consistency/reason/evidence。mapping记录origin和wire/legacy SHA，原Draft202012+fixed_structure+validate_judgment全部再验证。117主/116内部细分边界不变。

第一样本同时做真实接口smoke，优先json_schema strict；接口接受该参数不代表服务端保证绝对强制Schema，本地全量validator始终不可省略。HTTP 200响应必须具有非空字符串model并与请求model完全相同；缺失或不符作为来源技术错误，不做格式repair，也不计有效结果，其他待请求样本可继续（认证失败例外）。仅明确400/422 unsupported/schema关键字错误允许降到json_object，再prompt。首样本完成后本dataset固定模式，无额外探测请求。任意worker收到401均设置共享取消信号：未发起请求的队列返回cancelled_auth_failure，不当missing；已经in-flight的其他请求可能完成，不承诺撤回；401本身不重试。429/5xx/可识别超时传输最多2次backoff（1/2秒），不当能力降级。机器JSON/shape/ID/引用/链接错误最多max-repairs（0..2），即每样本至多3次生成/格式修复，repair给原输出+机器错误+同一input，从不送参考答案。finish=length可在该3次预算内最多一次把8192增到16384，temperature/seed省略，不假称固定。最终技术失败evaluation_failed、exit1，不计missing/成绩。结构合法但与派生参考业务标签有差exit0，明确差异flag，无overall accuracy宣称。

### 可恢复与审计产物

输出目录必须全新，文件exclusive create，无force。每样本独立目录包含input、prompt、动态wire/旧schema、attempt-NNN的messages、http-NN请求/安全响应/时间、原content、wire/legacy、mapping、validation和status。成功状态含usage、response.model、reqID所在原HTTP记录、duration及hash；失败attempt同样保留。session-NNN存provider安全摘要、固定compatibility模式及report.json/report.md。

同一完整命令加 `--resume`，仅在解析后的实际HTTPS `/chat/completions` endpoint、provider公开ID/凭据引用名、输入、源dataset、prompt/schema、模型/参数、validator/executor版本及源码hash一致时允许；相同profile路径内baseURL漂移也在认证或HTTP之前拒绝，不记录凭据值或认证key hash。认证解析得到的endpoint还必须等于预检指纹，传输endpoint在请求前再检查。成功结果重新校验wire/legacy/原content及hash后跳过，零复发。失败/中断只新建attempt，不改旧记录；总生成预算跨resume仍最多3次，耗尽需明确新run。活进程lock拒绝并发恢复；死进程临时lock可经PID检查释放。中断HTTP可能已由服务计费，无法声称服务端exactly-once，attempt-start记录保留且计入预算。

report为确定性字段比较：首次格式成功/修复成功/最终技术失败、coverage逐baseline行一致率（row与sample exact分母）、按参考coverage类分母/预测计数；relation、granularity及两层business_consistency各自分母与差异；引用/链接机器结果、重复成员/复核target签名对照，不逐字比reason。不把派生参考称逐条人审，不输出系统成绩。token统计汇总所有HTTP/repair usage，报告source hashes unchanged。

### 离线测试

[test_evaluate_scenarios.py](<tools/scenario_benchmark/tests/test_evaluate_scenarios.py>)所有API均mock，涵盖40参考的结构映射（非真实40执行）、ID/占位/duplicate/unknown、空候选、多父联合与partial、业务标签差不重试、fakequote/refs/links有界修复、兼容降级、401/429/5xx/timeout、finish length预算、恢复skip/指纹拒绝/原产物篡改拒绝、wrapper隔离/注入、安全echo和采样stage。

```bash
/usr/bin/python3 -B -m unittest discover -s tools/scenario_benchmark/tests -p 'test_*.py' -v
```

程序版本1.1.1不改变wire1.1和业务policy。1.1.0历史smoke及报告保持原证据；升级后版本/源码hash变化，不能用新CLI resume旧run，必须新建运行目录。

## v1.1.3 GPT 最小协议修复

Chat Completions 始终发送标准 `store: false`，不允许 true；要求上游不存储该响应。字段纳入运行指纹，恢复不能更改。业务 policy、prompt、wire/normalization/schema 不变。首样本遇到未兼容的4xx时停止其余队列并保存报告，避免相同协议错误连发。

### 原生 Responses 兼容路线

仅当 Chat 加 store:false 后仍出现同一协议400，本轮采用 `--api-protocol responses`：同HTTPS base的 `/responses`，模型仍 `gpt-6.1-sol`，使用同一 system/user role对象作为 `input`，同一业务prompt/动态schema；`store:false, stream:false, max_output_tokens:8192`，结构约束是 Responses 原生 `text.format`，不是Chat response_format。输出只聚合 assistant message内 output_text，忽略reasoning/工具调用；response.model必须精确匹配。原native body/usage归档，input/output_tokens映射prompt/completion统计。协议、endpoint、token参数及store均进入指纹。与旧MiMo Chat/v1.1.1–2不是严格只换模型A/B。

失败Chat smoke已审计终结后不覆盖，新Responses smoke目录为 `gpt61-smoke-v1.1.3-responses-001`；成功后自动执行授权全40。

针对真实Responses 400 `uniqueItems is not permitted`，可显式 `--responses-schema-compat omit-unique-items`。仅API侧text.format.schema投影删uniqueItems，本地wire.schema、prompt内完整schema、业务validator保持全部唯一性约束；参数纳入指纹。其他未证实字段不猜删。首样本最终技术失败时一律停止其余队列。对应新smoke目录 `gpt61-smoke-v1.1.3-responses-schema1-001`。


## v1.1.4 原生 Responses SSE

在显式授权后使用 `--api-protocol responses --stream --responses-schema-compat omit-unique-items`。仅传输改为 SSE：业务 prompt、完整本地 wire schema、sample.input 和参考均不变。请求仍 `store:false`，`max_output_tokens:8192`，API text.format 投影仅移除 uniqueItems；指纹绑定 stream/protocol/endpoint 和 API schema 投影。

[安全传输](<tools/scenario_benchmark/safe_http.py>) 增量解码 UTF-8，按 SSE 多 data 行/空行/CRLF 处理。只接受真实 response.completed 中完整原 response（不合成 reasoning、工具或部分 output_text），保留原事件文本和解码事件、原 native usage。HTTP200 内 error/response.failed/incomplete、断流/无completed、坏JSON/UTF8、错误model均为技术失败，不计业务缺失，不因参考差异repair。所有原事件总量限2MiB，120秒 socket及剩余read预算，TLS验证且拒绝redirect；保存前扫描选中凭据回显（包含被忽略事件）。

[离线审计](<tools/scenario_benchmark/audit_run.py>) 重解析SSE原事件验证终结/body一致，审计原输入/messages/native请求及 wire→legacy 映射。成功 `--resume` 必须用同一scope和所有指纹，只复核归档，零请求。新增 [SSE测试](<tools/scenario_benchmark/tests/test_responses_sse.py>) 使用mock，不访问真实模型。


## v1.1.5 完整终结事件载体恢复

仅当原 response.completed.output 为真正空列表时，[事件一致性模块](<tools/scenario_benchmark/sse_fidelity.py>)要求created/completed同id同精确model、seq从0连续、唯一completed且无error/failed/incomplete/晚错误、item/part indices与item_id及完成顺序正确、逐segment delta累计与output_text.done/content_part.done/output_item.done文本完全一致；仅复制已完成assistant output_text载体，不纳入reasoning/function/refusal，不改任一业务标签。非空原output存在完整item证据时必须比较一致，不覆盖；缺done/缺终结/半帧/UTF8断流/model或ID冲突/重复done/漏seq一律拒绝。

原body和原SSE不改；derived-response与assembly-manifest独立保存，声明原capture/SSE/completed/input/schema SHA、segment indices/ID/delta与done序号及一致性。不是伪造raw completion。[恢复工具](<tools/scenario_benchmark/recover_responses.py>)offline全程stub禁止urllib与socket：只使用最后实际失败capture和结构证据，拒绝项保留原失败来源；aggregate再次从原事件复核派生fidelity，并从原HTTP复核原有效及新网络，不按派生参考择优。

当前原41HTTP中11例完整终结空output离线恢复，S008/S032真实server_is_overloaded仅独立新run网络恢复；该精确backend code允许沿用现有至多2次transport retry，不算业务格式修复。401或明确账户额度错误立即停止后续队列，不无限重试。不用新版本resume旧成功指纹；新run成功resume已由拒网stub证明skip。

## 现有平台真实测试点评测（独立工具）

[evaluate_platform.py](<tools/scenario_benchmark/evaluate_platform.py>) 只读取**已有任务**的当前测试点，不生成任务，不confirm、不写平台业务库，不从最终case/draft反推。无可确认的CaseGen服务root时，用SQLite URI `mode=ro`、`query_only`、同一读事务读取业务白名单列：task、requirement、当前checkpoint、全部points、引用ID和有限retrieve/test_points事件计数。明确记录 `provenance=current_database_snapshot`、`method=sqlite_read_only`、`human_edit_status=unknown`，不是API GET、不是首次模型原响应。confirmed/version1不作为无人工编辑的证据。

```bash
/usr/bin/python3 -B tools/scenario_benchmark/evaluate_platform.py prepare \
  --database data/meta/app.db --task-id 6 --block-size 5 \
  --output-dir data/evaluation/scenario-benchmark-v0.1/runs/platform-task6-gpt61-001
# execute仅使用上述归档输入；需要已经获得真实外部模型调用授权
/usr/bin/python3 -B tools/scenario_benchmark/evaluate_platform.py execute \
  --run-dir data/evaluation/scenario-benchmark-v0.1/runs/platform-task6-gpt61-001 \
  --model gpt-6.1-sol --harness-provider new-api --api-protocol responses \
  --stream --responses-schema-compat omit-unique-items --workers 2 --execute
```

prepare排他新建目录、保存snapshot/scenarios/source-manifest/protected hashes/inputs/plan，对完整117 gold互斥穷尽分块（每块最多6，默认5），每块始终保留全部7真实候选。gold原行、全六类task_scope和selected_subset由冻结validator验证，再逐块调用旧executor `single --input` dry-run，零凭据/HTTP。真实execute先首AUC块smoke，成功后workers<=2。复用旧安全传输、精确model验证、SSE终结审核、所有本地validator、有界技术/格式修复，不读40校准expected labels，不运行audit_run CLI；离线仅调用 `inspect_run(run, real_records, redactor)`。

`response.incomplete`仍按旧safe_http判技术失败，不自动改8192或安全性。仅对此技术失败，可保留原失败、一次拆成两个<=3-row新run，每个仍含全部候选；小块失败不再递归拆分。语义差异不重跑。已成功子run显式`--resume`复核后skip、零重新发送（源指纹必须一致）。原失败/恢复HTTP及返回usage全部计一次，缺usage不宣称无费用。

报告将任务范围**AUC19主指标**、其余98诊断、完整117诊断分开；后两者不是任务漏测/集合竞价任务合格率或平台总分。逐baseline CSV与逐候选CSV分母分别117/7。全局匹配为原始双向links的跨块union；业务/粒度冲突保留分布与逐块原判，不多数择优。只有全部117有效且无匹配才标全局未匹配待核查，不自动认可合理新增；技术未完成明确incomplete且不算missing。重复仅成员集合去重，非全局重判。原SSE/model/usage/HTTP/机器验证保存在独立evaluations目录，MD/JSON/CSV在platform-session目录；全旧gold/reference/fetch/executor/prompt/schema/历史run哈希保护不修改。


聚合及[对照工具](<tools/scenario_benchmark/compare_models.py>)只离线读取参考并输出各维度、六类及公平有效分母。HTTP与usage按原记录只计一次，11derived零新增；smoke另列，不将final副本重复计token，未返回usage的过载不假称零费用。历史公开源码snapshot核验原run SHA，旧run保护证据保持不动。测试全部使用mock或原归档，不访问真实模型。
