# CaseGen 前端视觉升级 — 设计规格

**日期：** 2026-07-25  
**状态：** 已实现（前端 polish）  
**范围：** 前端视觉 / 布局壳层 / 页面 polish（不改业务 API 与生成逻辑）  
**档位：** B 专业工作台 + 更亮更科技向配色

---

## 1. 目标与成功标准

### 目标
将当前「Element 默认灰蓝 + 朴素侧栏」升级为**金融/交易所场景下的专业 AI 工作台**，观感更亮、更科技，信息层次更清晰，同时保持现有路由、接口与功能不变。

### 成功标准
1. 打开任意主页面，能明显感到品牌色与层次（不再是默认 Element 样板）。
2. 导航、页头、主操作、状态色全站一致。
3. 工作台 / 任务 / 文档 / Wiki / 提示词 / 模型均完成视觉 polish。
4. `npm run build` 通过；不破坏现有交互与 API 调用。

### 非目标
- 不做登录 / RBAC / 暗色模式切换（可预留 CSS 变量）。
- 不换 UI 库、不重写业务逻辑、不改后端。
- 不做复杂图表大盘或动效库引入。

---

## 2. 视觉语言

| Token | 值 | 用途 |
|-------|-----|------|
| `--cg-primary` | `#4F7CFF` | 主操作、链接、选中 |
| `--cg-primary-2` | `#8B5CF6` | 渐变辅色、点缀 |
| `--cg-accent` | `#12B886` | 成功 / 完成 / 正向强调 |
| `--cg-danger` | `#F03E3E` | 失败 / 错误 |
| `--cg-warning` | `#F59F00` | 警告 / 待处理 |
| `--cg-info` | `#4F7CFF` | 进行中 |
| `--cg-sidebar` | `#070B14` | 侧栏底 |
| `--cg-bg` | `#EEF2FF` 倾向的浅灰蓝 | 主区背景 |
| `--cg-surface` | `#FFFFFF` | 卡片 |
| `--cg-border` | `rgba(15, 23, 42, 0.08)` | 细边框 |
| `--cg-text` | `#0F172A` | 主文字 |
| `--cg-text-muted` | `#64748B` | 次要文字 |
| `--cg-radius` | `12px` | 卡片圆角 |
| `--cg-radius-sm` | `8px` | 控件圆角 |
| `--cg-shadow` | soft multi-layer | 卡片阴影 |

**主按钮：** 电蓝 → 紫微渐变（`linear-gradient(135deg, #4F7CFF, #8B5CF6)`），hover 略提亮。  
**字体：** 保持中文系统栈（PingFang SC / Microsoft YaHei 等）。  
**Element Plus：** 通过 CSS 变量覆盖 `--el-color-primary` 等，不 fork 组件库。

---

## 3. 布局壳层

### 3.1 侧栏（`MainLayout`）
- 宽约 **220px**，背景 `#070B14`。
- 顶部 **2px 电蓝→紫渐变条**。
- Logo 区：`CaseGen` + 小号副标「AI 测试用例平台」；左侧小圆点发光。
- 菜单项：图标（`@element-plus/icons-vue`）+ 文案；选中项左侧指示条 + 半透明电蓝底。
- 路由不变：`/` 工作台、`/tasks`、`/documents`、`/wiki`、`/prompts`、`/models`。

### 3.2 顶栏
- 高度约 56–64px，白底 + 底边框。
- 左侧：当前页标题 + 一行 muted 说明（可由路由 meta 提供）。
- 右侧：弱提示「默认模型：xxx」（拉取 models 列表中 `is_default`；无则隐藏）。

### 3.3 主内容区
- 背景：浅灰蓝 + **极淡径向高光**（不抢内容）。
- 内边距 24px；统一 `.page` 白卡片（圆角、边框、阴影）。

---

## 4. 跨页组件模式

| 模式 | 说明 |
|------|------|
| `PageHeader` | 标题、描述、右侧主操作槽 |
| 状态 Tag | `ready/success` 绿、`failed` 红、`ingesting/running` 蓝、`parsed/draft` 灰/琥珀 |
| 空状态 | 图标 + 一句话 + 可选引导按钮 |
| 加载 | Element skeleton 或 `v-loading`，风格统一 |
| 表格 | 表头浅底、行 hover 淡紫蓝高亮、圆角外包容器 |
| 表单 | 标签清晰、主 CTA 右对齐或页头旁 |

可选实现为小型 Vue 组件（`PageHeader.vue`、`StatusTag.vue`、`EmptyState.vue`），或先用统一 class；优先可复用组件。

---

## 5. 分页面规格

### 5.1 工作台
- 页头：标题 + 副文案。
- 顶部 **三步引导条**：需求填写 → AI 生成 → AI 评审（当前步高亮）。
- 表单置于单卡片；主按钮「创建并生成」使用渐变主色。

### 5.2 任务列表
- 页头 +「新建任务」跳转工作台。
- 状态彩色 Tag；标题可点进详情。
- 空状态：「还没有任务，去工作台创建第一条」。

### 5.3 任务详情（完整 polish）
- 顶区：标题、状态 Tag、操作按钮组（生成/评审等）。
- 时间线组件视觉加强（节点色随状态）。
- 用例草稿 / 评审结果分卡片；分数用大号徽章或环形数字。

### 5.4 文档管理
- 上传区：虚线边框 + 淡渐变 hover，支持扩展名说明。
- 表格状态 badge；失败行展示截断 error + tooltip。

### 5.5 Wiki
- 左列表右预览布局；选中项左侧电蓝指示条。
- 预览区 Markdown 排版：标题层级、代码块、列表间距优化。

### 5.6 提示词 / 模型
- 统一表格 + 对话框圆角与主色按钮。
- 模型页：默认模型 Tag；Ping 成功/失败用状态色反馈。

---

## 6. 文件与实现边界

### 新增 / 重点改动
- `frontend/src/styles/theme.css` — 设计 token + Element 覆盖 + 工具 class  
- `frontend/src/style.css` — 引入 theme、全局 base  
- `frontend/src/layouts/MainLayout.vue` — 侧栏 + 顶栏 + 图标导航  
- `frontend/src/components/PageHeader.vue`（可选）  
- `frontend/src/components/StatusTag.vue`（可选）  
- `frontend/src/components/EmptyState.vue`（可选）  
- 各 `views/*.vue` — class 结构与局部样式 polish  
- `frontend/src/router/index.ts` — `meta.title` / `meta.description`  
- `package.json` — 增加 `@element-plus/icons-vue` 依赖  

### 明确不改
- `backend/**`
- API client 契约
- 任务状态机与生成流程

---

## 7. 验收清单

- [ ] 侧栏/顶栏/主色与设计 token 一致  
- [ ] 六大页面均有页头层次与主操作位置清晰  
- [ ] 状态色在文档/任务中统一  
- [ ] 空列表有友好空状态  
- [ ] 工作台三步引导可见  
- [ ] `npm run build` 成功  
- [ ] 手动点通：工作台创建、文档列表、Wiki 预览、模型 ping  

---

## 8. 风险与缓解

| 风险 | 缓解 |
|------|------|
| Element 深度选择器覆盖不全 | 优先改 CSS 变量；必要时 `:deep` 局部覆盖 |
| 渐变按钮对比度 | 白字 + hover 提亮；禁用态降为灰 |
| 改动面大导致回归 | 不改 script 业务逻辑，只动 template class 与 style |

---

## 9. 决策记录

- 档位：**B 专业工作台**
- 风格：**更亮更科技**（电蓝 + 紫，深侧栏，轻渐变）
- 页面 polish：**全页**（含任务详情与空状态）
- UI 库：继续 Element Plus + 图标包
