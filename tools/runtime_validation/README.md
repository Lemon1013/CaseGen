# Coverage flow 单范围运行验收

工具只使用标准库，不导入应用、不访问数据库、不修改模型/prompt、不开关服务。默认 dry-run 不发 HTTP。实际执行前，由 Lead 保持既有 8001 服务运行；8000 不属于本工具目标。

当前没有真实资料准备或生成效果结果。project3/space2 缺原始回归清单，旧规则doc2的原文完整性也未通过。使用下面独立的 `prepare_sources.py` 经正常上传/ingest准备隔离project，再将输出ID交给场景工具。现有 `validate_coverage_flow.py` 仍只允许场景路径，不自动创建project、上传、ingest或judge。

## 隔离原始资料准备

本轮只实现并离线测试入口，**不要将下面的 execute 示例视作已执行或已授权现在运行**。正常摄入可能调用现有配置的模型；等待 Lead 明确执行。无需重启服务，不修改鉴权、账号、模型、活动prompt、历史project3/doc2或任何benchmark。

固定读取用户既有两源（不复制答案到需求）：

- `/home/xinyu/文档/竞价基本功能测试.md`
- `/home/xinyu/下载/1_上海证券交易所交易规则（2026年修订）.docx`

```bash
backend/.venv/bin/python tools/runtime_validation/prepare_sources.py
# 由 Lead 决定启动真实准备后才执行：
backend/.venv/bin/python tools/runtime_validation/prepare_sources.py --execute
```

默认 dry-run 为零HTTP、不读取stdin或源正文、不创建归档。execute需要正常登录的既有 `CASEGEN_USERNAME` / `CASEGEN_PASSWORD`（或 `--username-env` / `--password-env` 指向现有变量）；支持 `--credentials-stdin` 安全管道的两行输入。不要把密码写进命令行/聊天/会持久记录或回显的终端；工具不读.env、历史凭据、auth_sessions或浏览器cookies，不建账号/重置密码。凭据缺失时在所有HTTP和project创建前明确失败。创建project仍要求服务端正常admin权限，403直接停止。

execute使用唯一名称/slug创建隔离project，从真实响应的 `default_wiki_space_id` / `private_spaces` 绑定private space，再GET核对active/归属/无共享绑定/无有效外部KB；不调用任何共享或外部KB设置接口。multipart上传的 `space_id` 在表单内，`project_id` 在query；响应hash必须与实际上传字节的SHA-256一致。只对本run刚上传并记录的新doc ID各POST一次ingest（不force），按真实job的queued/running状态受限轮询，只有success或success_with_warnings且doc ready才校验chunks。默认1200秒、间隔3秒，deadline最大7200、间隔1–60秒。失败、未知状态、网络/HTTP错误均停止，不自动重试、取消、rechunk或修复数据库。

完整性要求chunks索引、doc/space、起止offset及overlap一致、无gap，可按char_count完整重建；清单必须有 `## 2. 单笔成交金额`。还会GET preview重新解析上传文件，比对完整重建文本的hash/长度和解析质量。preview必须未截断（当前API最大200000字符），超过此边界明确失败。upload原始SHA-256和解析后text hash分别保留，不能相互替代。成功的受限manifest包含project_id/space_id、两来源文件名/size/sha256、doc/job ID、实际job终态、chunk ID/数量、重建hash及场景工具dry-run命令；它不代表模型效果通过，模型调用数为unknown。

归档在新唯一 `data/validation/prepare-sources-<run>/attempt-<id>`，目录0700、文件0600。run根持久保存来源hash、唯一project计划及每个业务写的 `*.started.json` / `*.mapped.json`；文件和目录fsync后才发写请求，返回ID立即写映射。multipart正文、preview/chunk正文不归档，上传只记filename/size/hash。auth响应、身份、密码、cookie/token/header均不落盘；models仅选ID/name/model_name等字段，丢弃api_key；job日志/错误正文不落盘。HTTP错误只记endpoint/status、不存body。

中断后必须使用原run恢复，**不得用一次新的execute冒充重试**：

```bash
backend/.venv/bin/python tools/runtime_validation/prepare_sources.py --execute --resume data/validation/prepare-sources-RUN
```

resume先核对本机两源hash；任一started缺mapped（不确定写请求）在登录前零HTTP停止，提示人工核对，绝不自动创建替代。缺已记录project/doc/job ID也停止；部分上传/未发ingest不会补发。映射齐全才通过正常login/logout，仅GET观察已有ID/状态并追加新attempt；业务写全部拒绝。原run失败请求不可重放，失败job不会自动重试。需Lead人工核对API记录与已产生资源后另行判断下一步；工具不提供自动改映射或清理入口。同run文件锁防并发，所有历史attempt保留。

成功manifest中的project/space ID可用于下节场景工具；应先运行默认dry-run，真实场景生成另由Lead决定。

## 单范围场景验证

从仓库根目录执行（把 PROJECT 和 SPACE 替换为 Lead 确认的实际ID）：

```bash
backend/.venv/bin/python tools/runtime_validation/validate_coverage_flow.py --project-id PROJECT --space-id SPACE
```

正常登录需要 `CASEGEN_USERNAME` 和 `CASEGEN_PASSWORD`（也可用 `--username-env` / `--password-env` 指向已存在的环境变量）。工具不读取历史聊天、会话表、浏览器cookies或默认密码。没有凭据时会明确失败，不创建任务。不要将凭据放命令行或录入会被持久记录的shell命令。`--credentials-stdin` 可从安全管道提供两行 username/password；stdin不是交互隐藏输入，不应直接在会回显的终端键入。

Lead 明确启动单次真实路径后：

```bash
backend/.venv/bin/python tools/runtime_validation/validate_coverage_flow.py --execute --project-id PROJECT --space-id SPACE
```

实际执行应通过管理端的后台job运行并收集结果。默认轮询最多1200秒，间隔3秒；可调整 `--deadline`（最大7200）和 `--poll-interval`（1–60）。不会自动重试HTTP失败或失败任务。单个新任务范围固定为用户KB中的 `2. 单笔成交金额`，需求仅写清单文件、章节和DVP/DST排除指令，不嵌入回归答案。工具先检查原始MD已ready、章节存在、原文chunks连续可重建、空间隔离且无有效外部KB或共享绑定。

输出在新唯一 `data/validation/coverage-flow-<run>/attempt-<id>` 中，目录0700、文件0600：选定的非敏感API元数据、请求/响应、检索确认选择、状态流、coverage metadata、完整场景JSON/CSV、events/citations及outcome。模型响应仅选择ID/name/model_name等字段，丢弃api_key；活动prompt只存ID/hash/version。登录/退出响应、用户名密码、Cookie/CSRF/headers不归档；HTTP错误仅记endpoint/status，不保存不可信错误body。CSV对公式前缀加单引号，JSON保留权威原值。原文和生成内容也可能包含业务敏感信息，因此仍应按内部验收资料管理。

任务到 `awaiting_test_point_confirmation` 后冻结当前pending提案，不确认测试点、不生成用例、不review。`completed=true` 仅表示已到达并保存该场景门；不代表coverage complete或场景质量通过。以coverage metadata中的实际状态/警告/调用数为准；拿不到模型usage/cost时为unknown，不能算0。

恢复既有run：

```bash
backend/.venv/bin/python tools/runtime_validation/validate_coverage_flow.py --execute --project-id PROJECT --space-id SPACE --resume data/validation/coverage-flow-RUN
```

resume只读取根目录已记录taskID并观察，不重复创建、不重发generate或retrieval confirm。若停在retrieval gate会记录缺口并退出；需要Lead人工判断。创建请求已发出但taskID未持久化的情况下自动恢复拒绝，必须先人工核对API/call记录，不能新建替代任务。每次恢复保存新的attempt，保留全部历史。并发运行同一run由文件锁拒绝。

离线测试：

```bash
backend/.venv/bin/pytest tools/runtime_validation/test_validate_coverage_flow.py tools/runtime_validation/test_prepare_sources.py -q
backend/.venv/bin/python -m py_compile tools/runtime_validation/validate_coverage_flow.py tools/runtime_validation/prepare_sources.py tools/runtime_validation/test_validate_coverage_flow.py tools/runtime_validation/test_prepare_sources.py
git diff --check
```
