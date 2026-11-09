# CaseGen Agent 对话页「呈现效果」重构 — 实施方案

**日期：** 2026-11-05
**状态：** P1–P3 已实施（2026-11-05；仅改 4 个视图/组件；`npm run build` exit 0）。思考折叠按 §7 A 档未做（需后端 thinking 分段，另起任务）。
**范围：** 仅前端视觉 / 布局 / 信息层级重构，使 Agent 对话页呈现「三栏通顶、去卡片、单行头部、悬浮输入舱」的专业工作台观感
**技术底座：** Vue 3.5 + Element Plus 2.14 + markdown-it 14 + Pinia 4 + 现有 `--cg-*` CSS 变量；**不引入 Tailwind / 任何新 UI 库**
**硬前提：** 复用现有真实 store `frontend/src/stores/agentChatStore.ts`（937 行，已接 SSE 与后端 API），**不新建 store、不改后端接口**（思考折叠见 §7 单独标注）

---

## 0. 一句话

保留 `agentChatStore.ts` 的全量状态与所有交互能力不动，只在 **AgentChatView / ChatPanel / ChatMessage / ChatComposer** 四个视图/组件上做「布局重排 + CSS 去边框 + 头部三合一 + 输入舱换皮」，把视觉呈现换成评审认可的那一套。

---

## 1. 目标与非目标

### 1.1 目标（可验收的现象）
1. 全局顶栏里不再与页内重复出现「Agent 对话 / 选择专家技能…」——页内 `.page-header` 删除，标题只留一处。
2. 主工作区呈现三栏通顶：左侧深色导航（沿用 MainLayout 既有 `el-aside`）｜中栏 260px 会话列表（新建/刷新置顶）｜右栏自适应对话区。
3. 去掉外层卡片套娃：会话列表与对话区不再被 `el-card` 的边框+padding 层层包裹，竖直阅读区占比 ≥ 70%。
4. 对话区头部压成**单行**：会话标题 + 技能胶囊 + 模型胶囊 + `[会话信息与溯源]` `[修改配置]`。
5. 消息流在 `max-width` 居中列内阅读；长回复默认 **Markdown 正常渲染**（不再折叠成纯文本）。
6. 底部输入改为**悬浮输入舱**（圆角+边框+焦点环+独立发送按钮），紧贴工作区底部而非贴浏览器边。
7. 状态条不再「本轮完成 + 未连接」自相矛盾：空闲时隐藏状态条；仅 busy 或有错误时出现。

### 1.2 非目标 / 明确不做
- 不新增/替换 UI 库，不引入 Tailwind、react-markdown、postcss。
- 不新建 `stores/chat.ts` 之类的平行 store，**不写 mock 数据、不用 setTimeout 假回复**。
- 不改 `api/agent.ts`、不改后端 SSE 协议（§7 的思考折叠除外，且为可选增量）。
- 不在 ChatPanel 内重画左侧深色导航（MainLayout 已有，重画会出现双导航）。
- 不改输入语义：**保留 `Ctrl/⌘+Enter 发送、Enter 换行`**，保留字数上限与文档导入。
- 不建议开启 markdown-it `html:true`（XSS）；沿用现有 `MarkdownView.vue` 的 `html:false`。

---

## 2. 技术约束（硬约束清单）

| 约束 | 依据 |
|---|---|
| 无 Tailwind，无 `@tailwindcss/typography`，无 `tailwind.config`/`postcss` | `frontend/` 目录实测无配置；package.json 无依赖 |
| 样式一律走 `--cg-*` CSS 变量 + 作用域 SCSS/CSS | `docs/superpowers/specs/2026-07-25-frontend-ui-refresh-design.md` 视觉语言 |
| 状态全在 `agentChatStore.ts`；视图只做展示与调用 | 该 store 注释明确「必须放 store 不能放组件」以扛 MainLayout `:key` 重建 |
| markdown 用现有 `MarkdownView.vue`（html:false + 链接拦截） | 安全，勿另起炉灶 |
| 桌面优先，<1200px 时中栏可折叠 | 无移动端历史包袱 |

---

## 3. 视觉与布局终态

### 3.1 三栏骨架（示意）
```
┌────────────┬──────────────┬───────────────────────────────────────────┐
│ MainLayout │ 会话列表 260px│ 对话区 (flex:1)                            │
│ el-aside   │ (新建/刷新置顶)│ ┌ 单行头部: 标题 ·技能 ·模型 [溯源][配置] ┐ │
│ 深色导航    │ 会话卡片      │ │ 消息流 (max-width 900px 居中)          │ │
│ (已存在)    │ …            │ │  …                                     │ │
│            │              │ └ 悬浮输入舱 ────────────────────────────┘ │
└────────────┴──────────────┴───────────────────────────────────────────┘
```

### 3.2 关键视觉规则
- **去边框**：`.agent-layout` 的 `el-card` 用 `:deep(.el-card)` 覆盖为 `border:none; box-shadow:none; border-radius:0`；区域间用 `--cg-bg` 浅底与 1px `--cg-border` 竖向分隔线区分，而非卡片外框。
- **通顶**：`.agent-layout` 与两栏 `height:100%`；对话区 body 改 `flex:1; min-height:0; display:flex`，删除 `calc(100vh-200px)` 魔法高度。
- **单行头部**：把现 `.config-summary` 的「当前配置」行合并进 `chat-block` 的 `el-card #header`；技能/模型用 `el-tag`(`size=small, effect=plain`) 胶囊；`[溯源][修改配置]` 为 `el-button link`。
- **消息流居中**：`.panel-scroll` 内新增 `.msg-column{max-width:900px;margin:0 auto}`。
- **悬浮输入舱**：`ChatComposer` 外层加 `border:1px solid var(--cg-border); border-radius:12px; box-shadow; :focus-within` 焦点环；与消息区之间留 12–16px 间距。
- **字体统一**：`.meta-skill / .header-model / .model-name` 去掉 `--cg-font-mono`，统一无衬线；仅代码块/日志保留等宽。

---

## 4. 组件改造清单（逐文件）

### 4.1 `frontend/src/views/AgentChatView.vue`（1003 行，改动最集中）
- **删** `.page-header` 整块（h1 + 副标题 + 右上「刷新/新建对话」）。
- **移**「刷新」「+ 新建对话」按钮到 `list-block` 头部（会话列表正上方，视线与新建记录同侧）。
- **并** `.config-summary` 与 `chat-block` 的 `el-card #header` 合并为单行头部：
  - 左：`current.title` + `<el-tag>{{ current.skill_name }}</el-tag>` + `<el-tag type=info>{{ current.model }}</el-tag>`
  - 右：`<el-button link>会话信息与溯源</el-button>`（仍触发原 drawer）、`<el-button link>修改配置</el-button>`（仍展开原 `config-editor`）
  - `config-editor` 保留原表单与 `saveCurrentConfig` 逻辑，改为头部下方可展开区或 `el-popover`。
- **保** 原 `el-drawer`（会话信息与溯源 + `CitationList`）、`el-dialog`（新建对话 + `SkillPicker`）原样不动。
- **CSS**：
  - `.agent-page` 去掉顶部大 padding/gap；
  - `.agent-layout` 保持 `display:grid; grid-template-columns:260px minmax(0,1fr); gap:0; height:100%`；
  - `:deep(.list-block .el-card), :deep(.chat-block .el-card){border:none;box-shadow:none;border-radius:0}`；
  - 删除 `.chat-block .el-card__body{height:calc(100vh-200px);…}`，改 `.chat-block .el-card__body{flex:1;min-height:0;display:flex;flex-direction:column}`。
- **保** 会话卡片状态样式：`.active` 用浅底+左缘强调条替代绿色粗边框；`closed` 置灰；保留 `meta-skill` 但改无衬线。

### 4.2 `frontend/src/components/agent/ChatPanel.vue`（300 行）
- `.panel-status`：仅当 `busy` 或存在 `streamError` 时渲染；空闲时整条隐藏（消除「本轮完成＋未连接」矛盾）。保留 `el-progress`（`busy && progress.active`）、`streamError` / `savingDraftError` 的 `el-alert`。
- `.panel-scroll`：内层包 `.msg-column`（max-width 900px 居中）；**保留 `stickToBottom` 自动滚底与 `onScroll` 逻辑不动**。
- 保留 `ChatComposer` 的 `extra-actions`：`中止本轮`（`cancelTurn`）、`转存为用例草稿`（`saveAsDraft`）。

### 4.3 `frontend/src/components/agent/ChatMessage.vue`
- **改默认**：`collapseThreshold` 默认由 600 → `0`（永不自动折叠），让 assistant 回复默认走 `MarkdownView` 渲染；折叠改为可选「收起长回复」交互（若保留，preview 也必须用 `MarkdownView`，不得纯文本）。
- **保留**性能守则：仅 `streaming` 气泡实时渲染 markdown，历史消息按需。
- **思考折叠预留**：新增可选 prop `thought?: string`，有值时渲染原生 `<details class="think">`；**无值不渲染**（不伪造）。是否真有值见 §7。

### 4.4 `frontend/src/components/agent/ChatComposer.vue`（300 行）
- **只改壳**：外层包一层胶囊容器（border + radius + `:focus-within` 焦点环），工具栏提示内移到底部同一行，发送按钮维持 primary。
- **逻辑零改**：`Ctrl/⌘+Enter 发送、Enter 换行`、`AGENT_MESSAGE_MAX_CHARS` 字数拦截、`.md/.txt/.pdf/.docx` 文档导入、拖拽导入、`disabled`（`isBusy || 已关闭`）全部原样。

### 4.5 `frontend/src/components/MarkdownView.vue`（复用，不改或仅微调样式）
- 继续 `html:false`、外链/站内链接拦截；表格/列表/加粗已由 markdown-it 内置支持。
- 需要的话仅补 scoped 排版样式（`h3/margin/hr` 等），**不新建 `MarkdownRenderer.vue`，不设 `html:true`,不用 `typography`**。

### 4.6 `frontend/src/layouts/MainLayout.vue`（可选，默认不动）
- 默认：全局顶栏保留（其 `pageTitle/desc` 来自 `router/index.ts:47` 的 meta）。
- 若需求方要「100% 通顶、无全局顶栏」：给 agent 路由加 `meta.chromeless=true`，MainLayout 用 `v-if` 对 agent 路由隐藏 topbar（低风险单点改动）。**这是二选一，不默认做。**

### 4.7 主题 / 全局样式
- 补充少量 `--cg-*` token（会话列表浅底、竖分隔线、消息列宽）到现有主题文件（`frontend/src/styles/…` / 组件 scoped）。
- **不改** `--el-color-primary` 等既有覆盖。

---

## 5. 状态与数据映射（复用 `agentChatStore`，零新建 store）

| UI 元素 | 数据来源（现有） |
|---|---|
| 会话列表 | `store.conversations`（`computed`，见 AgentChatView） |
| 当前会话/标题/技能/模型 | `store.currentConversation`（`.title / .skill_name / .model`） |
| 当前 id / 选中态 | `store.currentId` |
| 消息流 | `store.displayMessages`（`AgentDisplayMessage[]`，含 `streaming` 标记） |
| 流式正文 | `store.streamingText`（已并入 displayMessages） |
| busy / 进度 | `store.isBusy`、`store.stageProgress` |
| 连接态 | `store.streamConnected`（`useSseStream`） |
| 错误 | `store.streamError`、`store.savingDraftError`、`store.conversationsError` 等 |
| 技能下拉 | `store.skills` / `store.loadSkills()` |
| 模型 | `store.models` / `store.loadModels()` |
| 新建会话 | `store.createConversation(...)` |
| 发送 | `store.sendMessage(content)` |
| 中止 | `store.cancelTurn()` |
| 转存草稿 | `store.saveAsDraft()` |
| 改配置 | `store.updateConversationConfig(...)` |
| 选择/关闭/刷新 | `store.selectConversation(id)` / `store.closeConversation(id)` / `store.refreshConversation(id)` |

---

## 6. 功能保全对照表（旧能力 → 新落点，逐条核对，禁止丢失）

| 能力 | 旧位置 | 新落点 |
|---|---|---|
| SSE 流式输出 | store | 不动 |
| 中止本轮 | ChatComposer extra-actions | 保留 |
| 转存为用例草稿 | ChatPanel/ChatComposer | 保留 |
| 阶段进度条 | ChatPanel `el-progress` | 保留（busy 时） |
| streamError/savingDraftError | ChatPanel alerts | 保留 |
| 会话信息与溯源抽屉 | AgentChatView drawer | 保留，入口移头部 |
| 修改配置/保存 | config-editor | 保留，入口移头部 |
| 新建对话弹窗/SkillPicker | AgentChatView dialog | 保留 |
| 字数上限拦截 | ChatComposer | 保留 |
| 文档导入/拖拽 | ChatComposer | 保留 |
| 已关闭态禁用 | ChatComposer `disabled` | 保留 |
| 自动滚底 | ChatPanel `stickToBottom` | 保留 |
| Ctrl/⌘+Enter 发送 | ChatComposer | 保留（不改） |
| Markdown 渲染 | MarkdownView | 复用，且长回复不再纯文本折叠 |

---

## 7. 思考折叠的后端依赖（唯一开放项）

现状：`AgentDisplayMessage` 只有 `content` 字段，思考/工具日志与中文正文混在同一 content 流（`agentChatStore.ts:39-49` 实锤）。因此：

- **A 档（推荐，纯前端，本轮可做）**：**不做真实思维链折叠**。ChatMessage 预留 `thought?` prop 但后端不给值就当没这功能；先把「去卡片 / 三栏 / 单行头 / 悬浮舱 / Markdown 默认渲染」五件事落地。零后端改动、零风险。
- **B 档（需后端配合，单列需求）**：在 agent 流里新增 thinking 分段标记（`agent_gateway.py` / `agent_stream.py` 输出 `kind:'thinking'` 或独立字段），store 的 `AgentDisplayMessage` 增 `thought?`，ChatMessage 才能显示 `<details>` 折叠框。**须另起后端任务，不与本次视觉重构捆绑。**

> 结论：本轮按 A 档交付；B 档作为独立后续。避免像外部方案那样「假设字段存在」导致折叠框永不显示。

---

## 8. 分阶段实施与验收

- **P1 布局骨架**（AgentChatView）：删页内 header、头部三合一、按钮归位、去卡片边框、通顶高度。验收：标题只出现一次；无卡片套娃；阅读区 ≥70%。
- **P2 对话区**（ChatPanel/ChatComposer）：状态条按需显示、消息列居中、输入舱换皮。验收：空闲无矛盾状态；输入舱圆角+焦点环+发送按钮。
- **P3 渲染**（ChatMessage/MarkdownView）：长回复默认 Markdown，字体统一。验收：`---`/`##`/`**` 正确渲染，无裸 Markdown。
- **P4 回归**：全交互手测（新建/切换/发送/流式/中止/转存/溯源/改配置/导入/字数/关闭），`npm run build` 绿。

**统一验证命令：** `cd frontend && npm run build`（vue-tsc 类型检查 + 生产构建）。

---

## 9. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 无 Tailwind，纯手写 CSS 量大 | 全部收敛到 `--cg-*` token + 少量 scoped 类，不散落魔法值 |
| `el-card` 内部 DOM padding 顽固 | 用 `:deep(.el-card__body)` 定向重置，避免全局污染 |
| 改动波及共享布局 | MainLayout 默认不动，仅在需求方要求时加 `chromeless` 单点开关 |
| 误删功能 | 按 §6 对照表逐条勾验；`npm run build` + 手测 |
| 回滚 | 纯视图层改动，`git revert` 单 PR 即可；不涉数据/接口 |

---

## 10. 结论

这套方案**采纳外部方案的「呈现效果」，但拒绝其代码**：不建假 store、不引入 Tailwind、不开 `html:true`、不复制侧栏、不丢交互。落地范围就是四个文件（AgentChatView / ChatPanel / ChatMessage / ChatComposer）+ 复用 MarkdownView + 少量主题样式，全部基于 `agentChatStore` 现有状态与 Element Plus 现有组件。

---

## 11. P1 布局修复（2026-11-05 追加）

首次实施后实测发现两处布局缺陷，已修复并由 headless Chrome 复测：

1. **高度链未闭合**：页面被消息内容撑到约 7900px，消息区无内滚、输入区掉到页面最底部。
   修复：`MainLayout.vue` 对 `route.name === 'agent-chat'` 追加 `.body--fill`（`height:100vh;overflow:hidden`，`.main-content` 改 flex column），并把 `.agent-page` / `.agent-layout` / `.chat-block .el-card__body` / `.chat-panel` / `.panel-scroll` 逐层改为 `flex:1 + min-height:0`，`.panel-composer` 固定 `flex:0 0 auto`。
2. **左栏标题竖排**：`.list-header-title` 被压到 52px，文字逐字换行。
   修复：标题容器 `flex:0 0 auto; min-width:0`，内部 `span{white-space:nowrap}`。

复测（1440×1000）：`document.documentElement.scrollHeight = 1000`（等于视口，整页不再滚动）；`.panel-scroll` clientHeight 638 / scrollHeight 7719（仅消息区内部滚动）；输入舱 top≈797、bottom≈968（始终在视口内可见）；左栏「会话历史」单行 54×20px，左右两栏同高（97–968）且各自独立滚动。截图见 `docs/superpowers/plans/screenshots/agent-chat-fixed-top.png` / `-fixed-bottom.png`。

---

## 12. 侧栏收起/展开（2026-11-05 追加）

需求：左侧深色主导航与 Agent 页「会话历史」栏各自独立可收起/展开，状态持久化，纯视图层、不动 store/接口。

**实现落点**
- `frontend/src/layouts/MainLayout.vue`：`navCollapsed`（`localStorage['casegen:nav-collapsed']`）控制 `.sidebar--collapsed`（宽 68px，仅图标）与 `.body--nav-collapsed`（`margin-left:68px`，保留 `body--fill`）；`.sidebar-collapse` 按钮位于导航底部（Expand/Fold 图标 + 「收起导航」文案）；收起态隐藏文字/子菜单/页脚，分组按钮点击时自动展开导航再展开分组；`@media(max-width:900px)` 下 `margin-left:0`。
- `frontend/src/views/AgentChatView.vue`：`listCollapsed`（`localStorage['casegen:agent-list-collapsed']`）控制 `.layout--list-collapsed`（`grid-template-columns:0 minmax(0,1fr)`、隐藏 `.list-block`）；列表头新增「收起会话列表」按钮，对话头部在收起态显示 `.list-expand-btn` 展开按钮。

**回归修复（task-6）**：新增收起按钮后 288px 列宽不够，列表头「新建对话」被裁切（实测 `.block-header` clientWidth 263 / scrollWidth 297）。列宽改为 `clamp(332px, 24vw, 368px) minmax(0,1fr)`，`@media(max-width:1280px)` 为 `clamp(324px, 26vw, 348px) minmax(0,1fr)`；收起态 `0 minmax(0,1fr)` 不变。

**复测（1440×1000，headless Chrome）**：主导航 228→68px、`.body` margin-left 228→68px、导航文字隐藏，刷新页面后仍保持 68px；会话列表展开态 `345.6px / 810.4px`、头部 clientWidth == scrollWidth == 321、actions 右界 == header 右界 589、「新建对话」完整显示；列表收起态 `0px / 1156px`、`.list-block` display:none、展开按钮出现，再点展开恢复 `345.6px / 810.4px`；`document.scrollHeight` 各态均为 1000（不整页滚动）。截图见 `docs/superpowers/plans/screenshots/agent-chat-sidebars-default.png`、`-nav-collapsed.png`、`-list-collapsed.png`。

> 注：上面「列表头收起按钮 + 对话头展开按钮」和「`.list-block` display:none」是 §12 初版实现；后续按用户反馈改为平滑动画和单一固定开关，现状态以 §13 为准。

## 13. 侧栏动画与固定开关（2026-11-05 追加）

> 注：本节 `.list-toggle-float` 单一固定开关方案已于 §16 按 m00716 反馈替换为标题旁小图标按钮；平滑动画部分仍有效。

**需求**：m00414「对话左侧栏的收起展开动画太生硬了」；m00424「而且 按钮在收起和展开时最好在一个位置，方便用户点击」。

**实现落点（仅 `frontend/src/views/AgentChatView.vue`）**
- 平滑动画：`.agent-layout` 加 `transition: grid-template-columns 260ms cubic-bezier(0.22,1,0.36,1)`；`.layout--list-collapsed .list-block` 去掉 `display:none`，改 `opacity:0; visibility:hidden; pointer-events:none; border-right-color:transparent!important; border-right-width:0; transition: opacity 140ms ease, border-right-color 220ms ease, visibility 0s linear 260ms`；`.list-block` 常驻 `transition: opacity 180ms ease 40ms, border-right-color 220ms ease, visibility 0s linear 0s`；header/body 各加 `min-width:300px` 防止收起过程文字重排；`@media(prefers-reduced-motion:reduce)` 关闭过渡。
- 固定开关：在 `.agent-page` 内、`.agent-layout` 前放唯一 `.list-toggle-float` 按钮（`position:absolute; left:-24px; top:6px; z-index:6; 24×34px`，图标 `Fold`/`Expand` 随 `listCollapsed` 切换，带 `:title`/`:aria-label`/`:aria-expanded`）；删除列表头原「收起」按钮与对话头 `.list-expand-btn`。`.agent-page` 改 `position:relative; overflow:visible`（内部三块仍各自 `overflow:hidden`）。`@media(max-width:900px)` 下 `left:-16px; top:4px; width:22px; height:32px`。

**验证（Lead，headless Chrome 1440×1000）**
- `cd frontend && npm run build` → exit 0（vue-tsc + vite build，✓ built in 1.90s；仅既有 chunk 体积提示）。
- 展开/收起两态 `.list-toggle-float` 的 `getBoundingClientRect()` 完全相同：`x=232, y=103, w=24, h=34`（`x = 228 body margin + 28 main padding − 24`，`y = 97 agent-page top + 6`）。页面只有一个开关（`.list-expand-btn` 0 个、列表头旧按钮 0 个）。
- 点击后：grid `345.6px / 810.4px → 0px / 1156px`，`.list-block` `opacity:0 / visibility:hidden / pointer-events:none`，再点恢复 `345.6px / 810.4px`；`document.scrollHeight=1000` 不整页滚动。
- 1280：按钮 `x=232,y=103` 在视口内；900：按钮 `x=228,y=176,w=22,h=32`（agent-page x=244）在视口内且可正常收起。
- 截图：`docs/superpowers/plans/screenshots/agent-chat-toggle-fixed-expanded.png`、`-collapsed.png`。

## 14. 主导航开关固定（2026-11-05 追加）

**需求**：m00522「最左侧的tab导航栏也要加上按钮位置固定」。与 §13 会话列表开关同理：收起/展开时按钮坐标保持一致，避免用户追着按钮点。

**实现落点（仅 `frontend/src/layouts/MainLayout.vue`）**
- `.sidebar-footer` 固定占位：`flex:0 0 auto; height:68px; box-sizing:border-box; overflow:hidden`；收起态 footer 由 `display:none` 改为 `visibility:hidden`（保留 68px 高度），因此按钮的 y 不随页脚消失而下移。
- `.sidebar-collapse` 从整行按钮改为固定起点：`align-self:flex-start; flex:0 0 auto; display:flex; gap:10px; width:auto; height:36px; margin:0 16px 12px; padding:0 12px; border-radius:8px`；收起态 `justify-content:flex-start; margin-left:16px; margin-right:0; padding:0 12px`（宽度随文字隐藏自然收缩，左边界与图标不动）。
- 模板补 `:aria-label`（展开/收起），逻辑、`localStorage` 持久化、路由、`body--nav-collapsed`、`body--fill` 与动画均未改动。

**验证（Lead，headless Chrome 1440×1000 真实页面）**
- `cd frontend && npm run build` → exit 0（✓ built in 1.39s）。
- 展开态：`.sidebar-collapse` rect `x=16, y=884, w=104.02, h=36`；图标 `x=28, y=893, 18×18`；aside 228px、`.body` margin-left 228px；footer `y=932, h=68, bottom=1000`，页脚文字 bottom 983 未被裁切。
- 收起态：按钮 rect `x=16, y=884, w=42, h=36`；图标 `x=28, y=893, 18×18`；aside 68px、margin-left 68px；footer `y=932, h=68` 且 `visibility:hidden`。
- 两态对比：x/y/h 与图标 rect 完全一致，仅宽度随文案隐藏收缩；再展开恢复 228px；`document.scrollHeight=1000` 不整页滚动。
- 截图：`docs/superpowers/plans/screenshots/agent-chat-nav-toggle-fixed-expanded.png`、`-collapsed.png`。

## 15. 修复主导航展开瞬间的滚动条白条闪烁（2026-11-05 追加）

**现象**：收起态点开主导航的一瞬间，左侧深色栏会闪过一道白条，随后消失。

**根因（Lead 定位，非布局缝隙）**：`.nav` 仅写了 `overflow-y: auto`，按 CSS 规范 `overflow-x` 的计算值也会变成 `auto`。从收起态展开时 `.sidebar--collapsed` 被瞬间移除，导航文字立即参与布局，但侧栏宽度仍在 68px → 228px 过渡中，文字在 68px 宽度里逐字换行，导致 `.nav` 内容高度骤增并横向溢出；浏览器随即为 `.nav` 画出垂直/水平滚动条（浅色轨道 + `#a1a1aa` 滑块），约 30–100ms 后宽度足够、内容不再溢出，滚动条消失 —— 视觉上就是「一道白条闪过」。实测过渡帧：`.nav` `scrollHeight=806 / clientHeight=627`、`offsetWidth-clientWidth=8`、`offsetHeight-clientHeight=8`。

**修复（仅 `frontend/src/layouts/MainLayout.vue` CSS）**
- L382-391 `.nav`：保留 `overflow-y:auto`，新增 `overflow-x:hidden`，禁止横向滚动条。
- L393-397 新增：`.nav-item span, .nav-group-toggle span, .nav-group-title { white-space: nowrap; }`，过渡期间文字不再换行撑高导航。

**验证（Lead，headless Chrome 1440×1000 真实页面）**
- `cd frontend && npm run build` → exit 0（✓ built in 1.53s）。
- 连续 6 次「收起→展开」，在 15/30/60ms 采样：`nav.offsetWidth-clientWidth=0`、`nav.offsetHeight-clientHeight=0`，`scrollHeight==clientHeight`，无纵向/横向滚动条；19 帧 screencast 内未检出任何近白色滚动条竖列。
- 不动逻辑、`localStorage`、路由、`body--fill`/`body--nav-collapsed` 与动画时长。

## 16. 会话列表开关改为标题旁小图标（2026-11-05 追加）

**需求**：m00716「对话的左边栏 现在的按钮太突兀了，单独占了一列，我想改成图片中这个样子，不一定非要收起和展开两个状态都在一个位置」。

**设计决定**：取消 §13 的 `.list-toggle-float` 左侧浮列方案；展开态把开关放进列表头标题旁，收起态在对话头左侧出现展开按钮；两态不要求同一坐标。

**实现（仅 `frontend/src/views/AgentChatView.vue`）**
- 模板删除 `button.list-toggle-float`（原 L337-346）；CSS 删除 `.list-toggle-float` 及其 `@media(max-width:900px)` 覆盖（原 L788-818 / L1071-1076）。
- 展开态：`button.list-collapse-btn` 放在 `.list-header-title` 内、数量 `el-tag` 之后；`el-tooltip`（`effect="dark" placement="bottom" :show-after="200"`）文案「隐藏侧边栏」。
- 收起态：`button.list-expand-btn` 放在 `.chat-header-main` 最前，`v-if="listCollapsed"`；`el-tooltip` 文案「展开侧边栏」。
- 两按钮共用内联面板 SVG：`viewBox="0 0 16 16"` 15×15、`rect x=2 y=2.5 width=12 height=11 rx=2` + `path d="M6.5 2.5v11"`；CSS `box-sizing:border-box; width:28px; height:24px; border-radius:6px; padding:0`，浅边框/`--cg-surface`/`--cg-text-muted`，hover/focus-visible 与原一致。
- 保留 `listCollapsed` 逻辑、localStorage key `casegen:agent-list-collapsed`、grid 260ms 过渡、list-block 淡出、reduced-motion；`Fold`/`Expand` 已从 import 移除；`.list-header-actions` gap 恢复 6px。

**验证（Lead，headless Chrome 1440×1000 / 1280×900）**
- `cd frontend && npm run build` → exit 0（✓ built in 1.70s）。
- 1440 展开：header `clientWidth==scrollWidth==345`；`.list-header-actions` 170px 宽，右界 588.6 == header 右界 600.6−12；收起按钮 `x=356.7,y=109,w=28,h=24` 紧邻「会话历史 1」；`list-toggle-float=false`；`document.scrollHeight=1000`。
- 1280 展开：header `332==332`、actions 170px、收起按钮 `x=356.7,w=28`，无水平溢出。
- 收起态：`.list-block opacity:0/visibility:hidden`；对话头最左 `.list-expand-btn x=268,y=107,w=28,h=24`，点击恢复列表；均无裁切。
- 悬浮提示：hover 后 `.el-popper` 显示「隐藏侧边栏」(84×32, dark)。
- 截图：`docs/superpowers/plans/screenshots/agent-chat-list-toggle-icon-expanded-1440.png`、`-collapsed-1440.png`、`-tooltip.png`、`agent-chat-list-toggle-icon-expanded-1280.png`。

## 17. 修复会话列表刷新按钮加载态宽度抖动（2026-11-05 追加）

**现象**：点击左侧「会话历史」列表头的「刷新」按钮时，按钮外框会瞬间变宽一下。

**根因（Lead 定位）**：刷新按钮把图标写在默认插槽里（`<el-icon><Refresh /></el-icon>`）；Element Plus 的 ElButton 在 `loading=true` 时会**额外注入**一个 loading 图标，而默认插槽图标仍在，形成「双图标」。Lead 用 CDP 800ms 网络延迟在 1440×1000 真实页面实测：空闲 64.02px → 加载中 80.02px（+16px）。

**修复（仅 `frontend/src/views/AgentChatView.vue` L370-377）**
- 改为属性式图标：`<el-button size="small" :icon="Refresh" :loading="store.conversationsLoading" class="list-refresh-btn" @click="refreshConversations">刷新</el-button>`。
- 去掉默认插槽里的 `<el-icon><Refresh /></el-icon>` 与单独 `<span>刷新</span>`；`Refresh` 原已导入，无新增依赖，逻辑/布局/动画均未改。

**验证（Lead，headless Chrome 1440×1000 真实 /agent 页面）**
- `cd frontend && npm run build` → exit 0（✓ built in 1.97s）。
- 空闲宽度 64.02px；加载中 64.02px；结束后 64.02px；**差值 0px**；加载态连续 46 帧宽度恒为 64.02px。
- 列表头 `clientWidth==scrollWidth==321`，`.list-header-actions` 右界 588.59 == 列表头右界 588.59，无水平溢出。

## 18. 思考过程展示（2026-11-05 追加）

**需求**：m00928「好的 现在关注下思考内容展示的问题」；用户 m01118 选择「全量折叠」：后端采集 DSH reasoning 与调工具前的过场说明作为思考过程持久化，前端用 DeepSeek 式折叠块展示，最终答案独立呈现，刷新后仍在。经用户认可，放宽原 spec 的「reasoning 不落库 / 不透传 / 不进日志」三不约束。

### 18.1 后端链路（task-14）
- `backend/app/services/agent_gateway.py`：`AgentEventKind` 新增 `reasoning` 与 `tool_call`；reasoning 分支兼容 `{'delta','seq'}` 与 `{'text'}`，yield `kind='reasoning'`；任何 tool_call 都 yield 一个 `kind='tool_call'` 边界事件（`text` 为空或来自 `_TOOL_NOTICE_WHITELIST` 的固定文案）。工具参数与结果绝不外泄。**关键点**：不能只对白名单工具发边界，否则 `run_code` / `skill` / `session_search` 等常见工具不会切分，英文 commentary 仍混入正文。
- `backend/app/services/agent_stream.py`：`_reasoning` 有序字典（上限 256）；`reasoning(conversation_id, text='', reset_answer=False)` 仅非 terminal 时推送，payload=`{'delta': text}` + 可选 `reset_answer`；快照始终注入非空 reasoning。
- `backend/app/services/agent_turn.py`：`_finish_turn(..., reasoning='')` 将正文与思考分开存库；`flush_reasoning` 阈值 0.15s / 2000 chars；遇到 `kind in ('notice','tool_call')` 时 flush 正文与思考、把已收集的 commentary 前缀 `\n\n` 并入 reasoning 并发送 `reset_answer=True`，同时清空正文缓冲；最终 `content` 只保留最后一段 delta，`reasoning` 为完整思考 + commentary。
- `backend/app/models/entities.py`：`AgentMessage.reasoning: str = ''`；`backend/app/schemas/agent.py`：`AgentMessageOut.reasoning`；`backend/app/db.py`：幂等 `PRAGMA` + `ALTER TABLE agent_messages ADD COLUMN reasoning TEXT DEFAULT ''`；`backend/app/api/agent_chat.py`：stream 兜底 snapshot 加 `'reasoning': ''`。

### 18.2 前端链路（task-15）
- `frontend/src/api/agent.ts`：`AgentMessageItem.reasoning?`、`AgentStreamPayload.reasoning? / reset_answer?`、事件名加 `reasoning`。
- `frontend/src/composables/useSseStream.ts`：增 `onReasoning`；收到 `reset_answer === true` 时先 `cancelPendingFlush()` 再回调，避免旧正文残留。
- `frontend/src/stores/agentChatStore.ts`：`streamingThought`；历史消息 `thought = item.reasoning || ''`；流式显示条件 `streamingActive || streamingText || streamingThought`；所有清理 `streamingText` 的位置同步清 `streamingThought`；终态临时消息携带 `thought`。
- `frontend/src/components/agent/ChatMessage.vue`：`<details v-if="thought" class="thought-block" :open="streaming">`，流式显示「思考中…」，历史默认收起显示「已深度思考」，内容用 `MarkdownView` 渲染；CSS 浅灰底、12.5px、`max-height:320px; overflow:auto`。
- `frontend/src/components/agent/ChatPanel.vue`：向消息传 `thought`，自动滚底同时观察 `streamingText` 与 `streamingThought`。

### 18.3 验证
- 后端全量：`cd backend && NO_PROXY='localhost,127.0.0.1,::1' no_proxy='localhost,127.0.0.1,::1' .venv/bin/python -m pytest -q` → **418 passed, 5 warnings, exit 0**（默认 `NO_PROXY` 含 IPv6 CIDR 会触发 httpx 旧版端口解析错误，与本改动无关）。
- 前端：`cd frontend && npm run build` → exit 0，`✓ built in 1.56s`，仅既有 chunk>500kB 警告；静态扫描无 Tailwind / 新依赖 / `html:true`。
- 真机 SSE 验证：`POST /api/agent/conversations/.../messages` 后订阅 `/stream`，实际序列为 `snapshot(seq5,text='') → delta(英文过场说明) → reasoning(大段思考) → reasoning({delta, reset_answer:true}) → notice(正在查阅资料…) → delta(最终答案) → completed`；最终落库 `content="xinyu-G3-3590"`、`reasoning` 为完整思考，证明正文与思考分离、`reset_answer` 实时清正文、落库生效。
- 真实页面（1440×1000，headless Chrome，`思考展示演示` 会话）：历史 assistant 消息默认 `details.open=false`，摘要「思考过程 · 已深度思考」高 33px；点击后展开，thought 容器 `max-height:320px`、`scrollHeight 5502 / clientHeight 320` 内部滚动，正文完整独立；`document.documentElement.scrollHeight=1000`，整页不滚动。
- 截图：`docs/superpowers/plans/screenshots/agent-chat-thought-demo-collapsed.png`（默认收起）、`agent-chat-thought-demo-expanded.png`（展开思考）。

**已知小瑕疵（不阻断）**：thought-only（正文为空）的历史消息仍显示复制按钮；流式期间用户手动收起 `details` 后不会被强制再次展开。

## 19. 修复新建 Agent 对话「专家技能」下拉浮层超宽（2026-11-05 追加）

**现象**：用户 m01461「这个技能选择也太雷霆了」并附截图：新建 Agent 对话弹窗里点开「专家技能」后，下拉浮层从选择框一路横向铺到屏幕右缘（约 1460px），只显示两个技能名，右侧大片空白。

**根因（Lead 定位）**：`frontend/src/components/agent/SkillPicker.vue` 的技能 `el-option` 使用自定义 `.skill-option` 内容，其中 `.skill-option-desc` 设置 `white-space:nowrap`；`webapp-testing-expert` 的描述约 200 字符、`api-testing-expert` 约 145 字符，把下拉内容的固有宽度撑到约 1400px。同时 Element Plus 默认 `.el-select-dropdown__item` 为 `height:34px; line-height:34px; overflow:hidden`，描述行被裁掉，所以视觉上只剩技能名。

**修复（仅 `frontend/src/components/agent/SkillPicker.vue`）**
- 技能 `el-select` 增加 `fit-input-width` 与 `popper-class="skill-select-popper"`（模型选择器未改）。
- `:global(.skill-select-popper)` 限宽 `max-width: min(420px, calc(100vw - 32px))`。
- `:global(.skill-select-popper .el-select-dropdown__item)`：`height:auto; min-height:34px; line-height:1.35; padding:6px 32px 6px 20px; white-space:normal; box-sizing:border-box`。
- `.skill-option` 与 `.skill-option-desc` 加 `width/max-width:100%; min-width:0`，描述在浮层内单行省略，不再撑宽；业务逻辑、刷新按钮、已选技能说明折叠、模型输入全部未动。

**验证（Lead 独立，headless Chrome 1440×1000 真实 `/agent?project_id=1` 页面）**
- `cd frontend && npm run build` → exit 0（`✓ built in 1.39s`，仅既有 chunk>500kB 警告）。
- 技能选择框 240×32；打开后浮层 240×117.3，与选择框同宽（误差 0px，未超 420px）。
- 两个选项均可见、各高 51.7px：名称完整，描述被限制在 186px 内以省略号截断（`scrollWidth` 854/1407，视觉不溢出）。
- `document.documentElement.scrollWidth == innerWidth == 1440`，无横向滚动。
- 截图：`docs/superpowers/plans/screenshots/task-16-skill-picker-dropdown-lead-verify.png`（另有 implementer 截图 `task-16-skill-picker-dropdown.png`）。

## 20. 修复「实时思考内容与完成后思考内容不一致」（2026-11-05 追加）

**现象**：User said (m01560):「我已经发现一个问题，实时显示的和最后展示的思考内容不一样呢」。生成过程中，模型调工具前的英文过场（如「I'll start by loading the relevant skill ... Now let me read the source document ...」）显示在主回答气泡里；完成后同一段文字却进入「思考过程 / 已深度思考」折叠块，正文只剩中文最终答案。

**根因（Lead 定位，backend/app/services/agent_turn.py）**：原 delta 分支收到增量就立即 `flush_buffer()` → `agent_stream.delta`，于是工具调用前的 commentary 被当作正文实时显示；直到 `tool_call` / `notice` 边界才把已收集的 commentary 前缀 `\n\n` 并入 `reasoning_parts`，并发送 `agent_stream.reasoning(reset_answer=True)`，前端 `agentChatStore.onReasoning` 收到后清空 `streamingText`，造成「先看到英文正文、完成后又跑到思考块」的瞬时不一致。

**修复（task-17，仅改 `backend/app/services/agent_turn.py`，新增 `backend/tests/test_agent_turn_runner.py`）**
- delta 分支改为只 `collected.append` + `buffer.append`，**不再立即 flush 到正文**；新增 `discard_buffer()` 仅清空缓冲区、不发布。
- `kind in ('notice','tool_call')` 边界：`discard_buffer()` → `flush_reasoning(force=True)` → commentary 非空时 `prefix='\n\n' if reasoning_parts else ''`、`reasoning_parts.append(prefix+commentary)`、`agent_stream.reasoning(text=prefix+commentary, reset_answer=True)`、`collected.clear()`；仅 `event.text` 非空才发 `notice`。
- 循环结束 `flush_buffer(force=True)` + `flush_reasoning(force=True)`；异常/取消路径的 flush 语义不变。
- 测试 `backend/tests/test_agent_turn_runner.py`（145 行，fixture 用 `AgentStreamBroker` + monkeypatch `agent_turn.agent_stream`）：`test_commentary_before_tool_is_not_published_as_answer` 断言 delta 只有 `['final']`、commentary 只在 reasoning、delta 序号晚于 commentary reason、snapshot/persisted `content=='final'`；`test_answer_without_tool_stays_out_of_reasoning` 断言 delta/text 为 `answer`、reasoning 为 `think`。

**验证**
- 后端测试：Lead 独立跑 `NO_PROXY='localhost,127.0.0.1,::1' no_proxy=同值 .venv/bin/python -m pytest tests/test_agent_turn_runner.py tests/test_agent_gateway.py tests/test_agent_stream.py tests/test_agent_api.py -q` → **70 passed, 1 warning, exit 0**；implementer 全量 `pytest -q` → **420 passed, 5 warnings**。
- 真实 SSE 实测（会话 9dc66de39c964f40b5cebbdecfbf48cf）：4.62s snapshot `text_len=0`；8.82s reasoning 1072 字符（英文过场）；随后多次 `notice 正在查阅资料…` 与 reasoning；74.30s **仅 1 个 delta**，长度 4723（最终中文答案），completed `text_len=4721`。事件总计 23：snapshot 1 / status 3 / reasoning 5 / notice 12 / delta 1 / completed 1 ⇒ 生成期不再把 commentary 当正文推送。
- 浏览器实测（第三会话 e95e72b268514aa98c4160877fa13b9d，1440×1000，headless Chrome 9250 + CDP EventSource 钩子）：生成到 20s 时 `window.__esEvents` 收到 `snapshot(seq12,text='') → reasoning → notice×6`，**没有 delta**；DOM `.thought-block` 摘要「思考过程 / 思考中…」、`bodyLen=1219`、`open=true`，主回答气泡 `len=0`（仅打字指示器）⇒ 实时已把英文过场放进思考块，正文保持干净。
- 完成后同一会话：assistant 最终 `content` 长度 2630（中文测试点清单），`reasoning` 长度 10053；DOM `.thought-block` 摘要「思考过程 / 已深度思考」、默认 `open=false`；页面 `scrollHeight=1000=innerHeight`，整页不滚动。
- 截图：`docs/superpowers/plans/screenshots/agent-chat-thought-live-streaming.png`（生成中：思考块展开「思考中…」、正文空）、`agent-chat-thought-live-final-collapsed.png`（完成后：思考块收起「已深度思考」、最终答案独立显示）。

**环境说明**：验证用的 CaseGen 后端 8001（重启后 PID 435371，health=200），Vite dev 5173；8000 为无关服务。临时会话、auth_sessions 与 Chrome 均在验证后清理。




