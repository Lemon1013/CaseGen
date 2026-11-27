# CaseGen 重构交接（Phase 3 收尾）

- 日期：2026-11-21
- 仓库：/home/xinyu/文档/CODE/CaseGen
- 分支：refactor/v2-clean-architecture
- 权威文档：docs/superpowers/specs/2026-11-20-casegen-refactor-decisions.md、docs/superpowers/plans/2026-11-20-casegen-refactor-plan.md

## 一、已提交
- Phase 0：b46ad63（规划文档）、d2e67dd（schema_migrations、mock wiki、清临时文件）
- Phase 1：2b92bcb（多外部知识库 Provider，内嵌 Wiki 2.0 下线）
- Phase 2：584fb38（提示词分级/Fork、导航收口）、853106d（移除 wiki_analyze/wiki_write）

## 二、Phase 3 现状：代码已完成，未提交
后端：
- task_pipeline.py 2873 行 → 116 行薄门面；新 services/pipeline/：common 352、context 1054、retrieval 360、test_points 283、generation 301、review 378、orchestrator 294
- task_jobs.py +420 行：全局并发信号量、retrieving/reviewing/optimizing 重启恢复、排队拿到槽位后二次核验（_verify_claim_after_slot）
- 新增 stream_broker.py；task_stream.py -504 行改用公共基类
- case_management.py 修复「### 待确认项」解析；list_tasks 解决 N+1 并做私有提示词脱敏

前端：
- TaskDetailView.vue 2457 → 732 行；新增 components/task/（RetrievalConfirm、TestPointsEditor、SidebarPanel、ActionDialogs、DraftViewer），components/common/StructuredCaseTree.vue，utils/caseDictionaries.ts，utils/clipboard.ts，composables/useTaskActions.ts、useDraftDepthSync.ts；useSseStream 升级

审查发现的 2 个阻断缺陷已修复并补测：
1. pipeline/generation.py：单点 LLMError 进入精简重试前未检查停止状态（已加 _generation_stopped）
2. task_jobs.py：排队期间被手动停止，拿到槽位后误复活（已加二次核验）

## 三、验证结果（2026-11-21 19:26 实跑）
- pytest tests/test_pipeline_phase3.py：9 passed
- 全量 pytest：400 passed, 2 failed
  - 2 条失败均为 tests/test_mock_external_wiki.py，原因是 shell 的 NO_PROXY 含 IPv6 CIDR（fd7a:115c:a1e0::/48）导致 httpx 解析报错
  - 清空代理相关环境变量后该文件 14 passed → 属环境问题，不是代码回归
- 前端 npm run build 在 18:43 已 PASS；之后有 3 处轻量清理，建议复跑
- 后端/前端服务当前未启动

## 四、下一步（按顺序）
1. 复跑前端构建：cd frontend && npm run build
2. 只读 reviewer 复核上述两处停止/并发修复与 3 处清理
3. 提交 Phase 3（建议信息）：refactor(pipeline): modularize task pipeline, add recovery/concurrency limit and shared SSE/UI
   - 只提交 Phase 3 相关文件；不要带上 .claude/、.codex/、tools/scenario_benchmark/.qa/、docs 截图/pptx 等无关未跟踪项
4. Phase 4：Agent 对话收口（agent_draft 按 message_id 直入 TestCase、不再创建壳任务、归属弹窗、历史气泡错位、私密会话权限、清理本地 wiki 路径依赖）
5. Phase 5：Dashboard 精简 + 8 小时时区修复 + 老库副本迁移冒烟 + 模块 1 的 25 个路由回归 + tools/ git diff 为空

## 五、红线（不可破）
1. 模块 1（多数据池/平台适配）冻结：platform_data.py、schemas/platform_data.py、11 张表、5 个前端页面、api/platformData.ts 业务逻辑不改；保留 defaultWikiSpaceId、TestCase 读取、ModelConfig、data_description_parse 与 platform_example_render
2. 老数据零丢失：改 app.db 前自动 .bak；旧 Wiki 表只停止读写，严禁 DROP TABLE
3. 隐私：普通用户 private 生成提示词与他人 Agent 会话，admin 也不可查看
4. tools/ 目录整体不动（runtime_validation、scenario_benchmark）

## 六、已定稿决策（不要重开讨论）
- 模块 1：保留并冻结
- 模块 2：流水线为主干，保留双人工检查点、AI 评审、需求优化、单任务 Prompt 优化（禁全局改写）；Agent 做探索入口，产物按消息 ID 直入用例库
- 模块 3：内嵌 Wiki 2.0 全面下线，唯一知识源为外部知识库（多 Provider 插件化，首期 llm_wiki_django：Django session cookie + credentials include + /files/、/file-content/）
- 模块 4：统一「项目管理」，侧边栏 3 组；提示词分级（引擎类 admin，generate 分 system/shared/private，可 Fork）；不做 RBAC
- 模块 5：Dashboard 精简保留，拆组件、统一字典、修时区、优化聚合
- 模块 6：schema_migrations 管迁移，tools/ 不动，mock_external_wiki 对齐 API_README，清仓库残留

## 七、常用命令
- 后端测试：cd backend && .venv/bin/python -m pytest -q
- 跑全量前先摘掉代理环境变量（否则 mock wiki 两条会误报）：
  env -u NO_PROXY -u no_proxy -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy .venv/bin/python -m pytest -q
- 后端服务：cd backend && .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
- 前端：cd frontend && npm run dev
- Mock 外部知识库：backend/mock_external_wiki

## 八、给新会话的开场白
「读 docs/superpowers/handoff-2026-11-21-phase3.md 和两份 superpowers 文档。先复核 Phase 3 剩余两项修复、复跑前端构建，然后提交 Phase 3；提交后进入 Phase 4。严格遵守四条红线。」
