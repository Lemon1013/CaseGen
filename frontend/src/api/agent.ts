import { api } from './client'

/**
 * Agent 对话（DSH 集成，spec docs/superpowers/specs/2026-11-04-dsh-agent-integration-design.md §3.1-§3.2）。
 *
 * 约定：
 * - 走 api() 封装 ⇒ 自动带 X-CSRF-Token、credentials include、自动注入 project_id query。
 * - 唯一例外是 EventSource（SSE）：浏览器 EventSource 不能设 header，也不吃 client.ts 的
 *   自动注入，所以 agentStreamUrl 手工拼 project_id（同 api/tasks.ts:331 taskStreamUrl 范式）。
 * - 响应形状已按 backend/app/schemas/agent.py 校准（见文件尾 AGENT_CONTRACT_NOTES）；
 *   归一化函数保留兼容分支，避免后端微调时前端直接崩。
 */

/** 会话状态（不进 GenerationTask 状态机，因此不需要改 api/tasks.ts 的 IN_PROGRESS_STATUSES 等四处硬编码）。 */
export type AgentConversationStatus = 'active' | 'busy' | 'closed'

/**
 * 单条用户消息正文上限，对齐 backend/app/schemas/agent.py:66
 * `AgentMessageCreate.content: str = Field(min_length=1, max_length=32_000)`。
 * 超限会被后端 422 拒掉，前端先拦，避免白跑一次请求。
 */
export const AGENT_MESSAGE_MAX_CHARS = 32_000

/**
 * 请求侧一律传 number（Project.id 与 client.ts 自动注入都是数字），响应侧后端契约为字符串
 * （backend/app/schemas/agent.py:35 `project_id: str`）；两者都接受，比较时先归一化。
 */
export type AgentProjectId = string | number

export interface AgentConversationItem {
  id: string
  project_id: AgentProjectId
  title: string
  dsh_session_id: string | null
  skill_name: string | null
  model: string | null
  workspace_dir: string
  status: AgentConversationStatus
  /** 本轮已请求取消（协作式取消的中间态，后端 AgentConversationOut.cancel_requested）。 */
  cancel_requested?: boolean
  /** 乐观锁版本号（AgentConversationOut.version），回传 expected_version 可让过期写入返回 409。 */
  version?: number
  /** 已持久化消息条数（AgentConversationOut.message_count）。 */
  message_count?: number
  created_at: string
  updated_at: string
}

export type AgentMessageRole = 'user' | 'assistant'

export interface AgentMessageItem {
  id: string
  conversation_id: string
  role: AgentMessageRole
  /** 仅干净最终正文（无 tool 参数/结果痕迹）。 */
  content: string
  /** 思考过程（模型 reasoning + 过场说明）；与正文分开存储。 */
  reasoning?: string
  created_at: string
}

export interface AgentSkillItem {
  /** .agents/skills/<name>/SKILL.md frontmatter 的 name，kebab-case。 */
  name: string
  description: string
  /** 仓库相对路径（AgentSkillOut.path），仅调试用，UI 不渲染。 */
  path?: string
}

/** 结构化进度（spec §3.6：优先读它，回退正则从 message 抠 [n/m]，都拿不到用不定进度条）。 */
export interface AgentStreamProgress {
  current?: number
  total?: number
  label?: string
}

/**
 * failed 帧的机器可读分类（后端契约确认，取自 backend/app/services/agent_gateway.py
 * 的 _CATEGORY_MESSAGE 键集合）。文案由后端出且可直接展示给用户。
 * - cancelled：用户主动取消，不是错误，UI 不应报错色；
 * - unauthorized：DSH 鉴权失败 / 熔断，重连无意义。
 */
export type AgentFailureCategory =
  | 'unknown'
  | 'configuration'
  | 'unauthorized'
  | 'forbidden'
  | 'not_found'
  | 'conflict'
  | 'busy'
  | 'timeout'
  | 'network_error'
  | 'http_error'
  | 'stream_error'
  | 'bad_response'
  | 'permission_denied'
  | 'cancelled'

/**
 * 与 api/tasks.ts 的 TaskStreamPayload 同构，另加 failed 帧的 category。
 *
 * wire 键名已与后端对齐（backend/app/services/agent_stream.py:13-28 钉死，
 * 容量与事件语义继承 backend/app/services/task_stream.py）：
 * - snapshot：{stream_id, sequence, status, text, terminal, truncated}（可选 message）⇒ 全量正文键是 text；
 *   且**当且仅当 terminal==='failed' 时额外带 category**（completed 的 snapshot 不带）。
 * - delta：{delta} ⇒ 增量键是 delta；
 * - completed：{status, message, truncated}，仅当正文 <= 16384 字才附 text ⇒ 不带 text 时走 flushDelta；
 * - failed：{status, message, text, truncated, category}，category 一定存在（未知时为 'unknown'）。
 *
 * ⚠️ 与 tasks 侧的刻意差异：**agent 终态帧的 status 是 'active'（会话回到空闲），不是 'failed'**，
 * 因为 AgentConversation.status 只有 active/busy/closed 三态。判断终态**只看 terminal，不要看 status**。
 * 后端不发结构化 progress（agent 步骤数不定），前端一律走不定进度条。
 */
export interface AgentStreamPayload {
  status?: string
  message?: string
  /** snapshot / completed 帧的全量 assistant 正文（仅 completed 且正文 <= 16384 字时才带）。 */
  text?: string
  /** delta 帧的增量文本。 */
  delta?: string
  /** snapshot 帧的全量思考过程；reasoning 帧也会携带增量 delta。 */
  reasoning?: string
  /** reasoning 帧在 tool_call 边界置 true：清空已临时显示的正文预览。 */
  reset_answer?: boolean
  /** broker 的流代次（int，见 task_stream.py:51）；重连后可用来识别流是否被重建。 */
  stream_id?: number
  sequence?: number
  terminal?: 'completed' | 'failed' | null
  truncated?: boolean
  attempt?: number
  /** 后端确认不发；保留字段以兼容 tasks 侧同构 payload，读到才用。 */
  progress?: AgentStreamProgress | null
  /**
   * 失败分类，见 AgentFailureCategory。failed 帧一定带（未知时 'unknown'）；
   * snapshot 帧仅当 terminal==='failed' 时带 ⇒ 「取消进行中刷新页面」也能正确识别为用户中止。
   */
  category?: AgentFailureCategory | string | null
  /** failed 帧的机器可读原因（后端目前只发 category，保留兼容）。 */
  reason?: string | null
}

export type AgentStreamEventName =
  | 'snapshot'
  | 'status'
  | 'delta'
  | 'reasoning'
  | 'reset'
  | 'retry'
  | 'notice'
  | 'completed'
  | 'failed'

export interface AgentConversationDetail extends AgentConversationItem {
  messages: AgentMessageItem[]
}

export interface AgentCreateConversationBody {
  title?: string
  skill_name?: string | null
  model?: string | null
}

export interface AgentSendMessageBody {
  /** 上限 AGENT_MESSAGE_MAX_CHARS（32000），且不可为空白（min_length=1）。 */
  content: string
  /** 客户端生成，服务端查重（幂等）；上限 200 字。 */
  idempotency_key: string
  /** 乐观锁：回传会话当前 version，过期写入返回 409（AgentMessageCreate.expected_version）。 */
  expected_version?: number
}

/** POST /messages 的返回体，对齐 AgentMessageAccepted（schemas/agent.py:72）。
 *  轮次跑在后台线程，所以这里只拿到已持久化的用户消息与当时的会话态，不是 assistant 回答；
 *  duplicated=true 表示幂等命中（同一 idempotency_key 重复提交）。 */
export interface AgentMessageAccepted {
  conversation: AgentConversationItem
  message: AgentMessageItem
  duplicated?: boolean
}

/** POST /cancel 的返回体，对齐 AgentCancelOut（schemas/agent.py:84）。
 *  取消是协作式的：真正的终态帧（failed + category=cancelled）随后才到，
 *  所以收到 200 后前端保持 SSE 连接等终态帧，不立刻关流。 */
export interface AgentCancelResult {
  conversation: AgentConversationItem
  cancelled: boolean
  message: string
}

/** DELETE /conversations/{id} 的返回体，对齐 AgentDeleteOut（schemas/agent.py:90）。 */
export interface AgentDeleteResult {
  id: string
  status: string
  message: string
}

/** PATCH /conversations/{id} 的请求体，对齐 AgentConversationUpdate（schemas/agent.py:22）。
 *  返回 AgentConversationItem。P1 UI 暂未用到改标题 / 换技能，先留类型位。 */
export interface AgentUpdateConversationBody {
  title?: string
  skill_name?: string | null
  model?: string | null
  expected_version?: number
}

/**
 * POST /api/agent/conversations/{id}/save-as-draft 的返回体，
 * 对齐 backend/app/schemas/agent.py:103 `AgentSaveAsDraftOut`（解析失败 422）。
 *
 * spec §3.5 方案 a：虚拟 Requirement + shell GenerationTask(status=generated) + CaseDraft，
 * 所以一定有 task_id（number，非 uuid）⇒ 可直接跳任务详情并用 CitationList(task_id) 拉溯源。
 * 注意：这里没有 points_with_cases —— 用例结构是解析 agent 输出的 fenced JSON 得到的，
 * 见 components/agent/parseAgentCases.ts。
 */
export interface AgentSaveAsDraftResult {
  task_id: number
  draft_id: number
  requirement_id: number
  /** 实际导入的用例条数。 */
  imported_count: number
  case_keys: string[]
  message: string
}

export const AGENT_STREAM_EVENTS: AgentStreamEventName[] = [
  'snapshot',
  'status',
  'delta',
  'reasoning',
  'reset',
  'retry',
  'notice',
  'completed',
  'failed',
]

function httpStatus(error: unknown): number | null {
  const status = (error as { status?: number } | null)?.status
  return typeof status === 'number' ? status : null
}

/** 410 Gone = 服务端显式关闭 AGENT_ENABLED（spec §7），前端应隐藏入口并给出说明。 */
export function isAgentDisabled(error: unknown): boolean {
  return httpStatus(error) === 410
}

/**
 * 集合端点（会话列表 / 技能列表）上的降级判定：410 表示功能关闭，
 * 404 表示后端还没注册 /api/agent/* 路由（开发期后端未交付）。
 */
export function isAgentUnavailable(error: unknown): boolean {
  return isAgentDisabled(error) || httpStatus(error) === 404
}

/**
 * 单个会话端点上的 404 只代表「这条会话不存在或已删除」
 * （例如 URL 里 ?conversation= 过期），绝不能据此判定整个 Agent 能力未启用。
 */
export function isConversationMissing(error: unknown): boolean {
  return httpStatus(error) === 404
}

function _projectQuery(projectId?: AgentProjectId | null): string {
  if (projectId === null || projectId === undefined || projectId === '') return ''
  return `?project_id=${encodeURIComponent(String(projectId))}`
}

/** 把 number|string 统一成数字 project_id（拿不到返回 null）。 */
export function normalizeProjectId(value: AgentProjectId | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null
}

/** 列表响应可能是裸数组或 {items,total} 包裹；归一化取数组。 */
export function normalizeConversationList(payload: unknown): AgentConversationItem[] {
  if (Array.isArray(payload)) return payload as AgentConversationItem[]
  const items = (payload as { items?: unknown } | null)?.items
  if (Array.isArray(items)) return items as AgentConversationItem[]
  return []
}

/** 技能响应可能是裸数组、{items} 或 {skills}。 */
export function normalizeSkillList(payload: unknown): AgentSkillItem[] {
  const candidate = Array.isArray(payload)
    ? payload
    : (payload as { items?: unknown; skills?: unknown } | null)?.items ??
      (payload as { skills?: unknown } | null)?.skills
  if (!Array.isArray(candidate)) return []
  return candidate
    .map((raw) => {
      if (typeof raw === 'string') return { name: raw, description: '' }
      const item = raw as Partial<AgentSkillItem>
      const name = typeof item.name === 'string' ? item.name : ''
      if (!name) return null
      return { name, description: typeof item.description === 'string' ? item.description : '' }
    })
    .filter((item): item is AgentSkillItem => item !== null)
}

/**
 * 详情响应可能是 {...conversation, messages} 扁平，也可能 {conversation, messages} 嵌套。
 */
export function normalizeConversationDetail(payload: unknown): AgentConversationDetail {
  const source = (payload as { conversation?: unknown } | null)?.conversation ?? payload
  const base = (source ?? {}) as AgentConversationDetail
  const rawMessages =
    (payload as { messages?: unknown } | null)?.messages ??
    (base as { messages?: unknown } | null)?.messages
  return { ...base, messages: Array.isArray(rawMessages) ? (rawMessages as AgentMessageItem[]) : [] }
}

export function listAgentConversations(projectId?: AgentProjectId | null) {
  return api<unknown>(`/api/agent/conversations${_projectQuery(projectId)}`).then(normalizeConversationList)
}

export function createAgentConversation(body: AgentCreateConversationBody, projectId?: AgentProjectId | null) {
  return api<AgentConversationItem>(`/api/agent/conversations${_projectQuery(projectId)}`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function getAgentConversation(id: string, projectId?: AgentProjectId | null) {
  return api<unknown>(`/api/agent/conversations/${id}${_projectQuery(projectId)}`).then(
    normalizeConversationDetail,
  )
}

/** 关闭会话（DSH session 不主动删，仅本地标记 closed）。 */
export function closeAgentConversation(id: string, projectId?: AgentProjectId | null) {
  return api<AgentDeleteResult>(`/api/agent/conversations/${id}${_projectQuery(projectId)}`, {
    method: 'DELETE',
  })
}

/** 改会话元信息（标题 / 技能 / 模型），返回更新后的会话。 */
export function updateAgentConversation(
  id: string,
  body: AgentUpdateConversationBody,
  projectId?: AgentProjectId | null,
) {
  return api<AgentConversationItem>(`/api/agent/conversations/${id}${_projectQuery(projectId)}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

/** 发用户消息；busy 时后端返回 409。 */
export function sendAgentMessage(
  id: string,
  body: AgentSendMessageBody,
  projectId?: AgentProjectId | null,
) {
  return api<AgentMessageAccepted>(`/api/agent/conversations/${id}/messages${_projectQuery(projectId)}`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function cancelAgentTurn(id: string, projectId?: AgentProjectId | null) {
  return api<AgentCancelResult>(`/api/agent/conversations/${id}/cancel${_projectQuery(projectId)}`, {
    method: 'POST',
  })
}

/** 从最近 assistant 消息解析用例 JSON 落库（spec §3.5 方案 a）；解析失败 422。 */
export function saveAgentConversationAsDraft(id: string, projectId?: AgentProjectId | null) {
  return api<AgentSaveAsDraftResult>(
    `/api/agent/conversations/${id}/save-as-draft${_projectQuery(projectId)}`,
    { method: 'POST' },
  )
}

export function listAgentSkills(projectId?: AgentProjectId | null) {
  return api<unknown>(`/api/agent/skills${_projectQuery(projectId)}`).then(normalizeSkillList)
}

/**
 * SSE 地址：EventSource 不吃 client.ts 的自动 project_id 注入，必须手工拼 query。
 * 非活跃会话请求 stream 后端返回 409（spec §3.2 兼容清单第 9 条），表现为 onerror。
 */
export function agentStreamUrl(id: string, projectId?: AgentProjectId | null): string {
  return `/api/agent/conversations/${id}/stream${_projectQuery(projectId)}`
}

/** 生成幂等键：客户端去重发送（服务端也会查重）。 */
export function createIdempotencyKey(): string {
  const cryptoObj = typeof crypto !== 'undefined' ? crypto : undefined
  if (cryptoObj && typeof cryptoObj.randomUUID === 'function') return cryptoObj.randomUUID()
  return `agent-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

/**
 * AGENT_CONTRACT_NOTES —— 与 implementer-backend 已对齐的契约结论（权威源：
 * backend/app/schemas/agent.py、backend/app/services/agent_gateway.py、
 * backend/app/services/agent_stream.py、backend/app/services/agent_turn.py、
 * backend/app/services/task_stream.py）：
 * ① GET /conversations 返回**裸数组** AgentConversationOut[]，按 updated_at 倒序（无 items/total 包裹）；
 * ② GET /conversations/{id} 返回**扁平** AgentConversationDetail = {...conversation, messages: []}；
 * ③ GET /skills 返回 AgentSkillOut[]（name / description / path）；
 * ④ snapshot 全量正文键是 **text**；delta 增量键是 **delta**；completed 仅当正文 <= 16384 字才附 text；
 * ⑤ failed 帧带 **category**（14 值，见 AgentFailureCategory，**一定存在**，未知时为 'unknown'），
 *    message 是可直接展示的中文；cancelled = 用户主动取消（不是错误），
 *    unauthorized = DSH 鉴权失败 / 熔断（重连无意义）。
 *    category 由 AgentStreamBroker 的侧表注入（agent_stream.py:97-108），**并且 terminal==='failed'
 *    的 snapshot 也带**（_enrich_snapshot_locked:81-93）⇒ 取消进行中刷新页面仍能正确识别为用户中止；
 * ⑥ POST /messages 返回 AgentMessageAccepted{conversation, message, duplicated}（轮次在后台线程跑）；
 *    POST /cancel 返回 AgentCancelOut{conversation, cancelled, message}，**且随后一定发终态帧再关流**；
 * ⑦ POST /save-as-draft 返回 AgentSaveAsDraftOut{task_id, draft_id, requirement_id, imported_count,
 *    case_keys, message}，task_id 是 **number**（不是 uuid）；
 * ⑧ 后端**不发**结构化 progress（agent 步骤数不定，message 里也不保证有 [n/m]）⇒ 前端一律不定进度条；
 * ⑨ project_id 后端一律 str(...) 归一化比较，query 传 number 或 string 都吃；跨项目一律 404；
 * ⑩ AGENT_ENABLED=false ⇒ 所有 /api/agent/* 返回 410 + 中文 detail（见 isAgentDisabled）；
 * ⑪ PATCH /conversations/{id} 已实现（title / skill_name / model / expected_version）；
 * ⑫ 菜单隐藏（bootstrap 下发开关）是 P2 遗留项，P1 用 410 软降级，lead 已裁决；
 * ⑬ ⚠️ **agent 终态帧的 status 是 'active'**（会话回到空闲，AgentConversation.status 只有
 *    active/busy/closed），**不是 tasks 侧的 'failed'**；terminal 才是 'failed'。
 *    ⇒ 判断终态只看 payload.terminal（useSseStream.ts:268-269、store onSnapshot），**不要看 status**；
 *    前端 stageStatus 仅记录不参与判断。
 */
