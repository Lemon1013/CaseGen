import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  agentStreamUrl,
  cancelAgentTurn,
  closeAgentConversation,
  createAgentConversation,
  createIdempotencyKey,
  getAgentConversation,
  isAgentDisabled,
  isAgentUnavailable,
  isConversationMissing,
  listAgentConversations,
  listAgentSkills,
  normalizeProjectId,
  saveAgentConversationAsDraft,
  sendAgentMessage,
  updateAgentConversation,
  type AgentConversationItem,
  type AgentMessageItem,
  type AgentProjectId,
  type AgentSaveAsDraftResult,
  type AgentSkillItem,
  type AgentStreamPayload,
} from '../api/agent'
import { listModels, type ModelConfig } from '../api/models'
import { getSession } from '../api/auth'
import { useSseStream, type SseStreamPayload } from '../composables/useSseStream'

/**
 * Agent 对话状态（spec §3.6-5）。
 *
 * **必须放 store 不能放组件**：frontend/src/layouts/MainLayout.vue:279 的 router-view 用
 * :key = projects.state.currentId 绑定，切项目会销毁整个视图并打断 SSE。把会话列表、当前会话、
 * 消息数组、流式 buffer、连接状态放这里，视图重建后靠 snapshot 首帧无缝恢复；
 * 组件卸载不 close 流（closeOnUnmount: false），agent 轮次在后台继续把正文缓冲进 store。
 */

export interface AgentDisplayMessage {
  id: string
  conversation_id: string
  role: 'user' | 'assistant'
  content: string
  /** 思考过程（模型 reasoning + tool_call 前的过场说明）。 */
  thought?: string
  created_at: string
  /** 本地乐观消息（尚未拿到服务端 id）。 */
  pending?: boolean
  /** 正在流式的 assistant 气泡：只有它用 MarkdownView 实时渲染（性能守则）。 */
  streaming?: boolean
}

export interface AgentStageProgress {
  current: number
  total: number
  percent: number
  label: string
  /** true = 有确定进度；false = 用不定进度条或纯文案。 */
  determinate: boolean
  active: boolean
}

function errorMessage(error: unknown): string {
  const detail = (error as { detail?: string } | null)?.detail
  if (typeof detail === 'string' && detail) return detail
  const message = (error as { message?: string } | null)?.message
  return typeof message === 'string' && message ? message : '操作失败，请稍后重试'
}

/** spec §3.6：优先读结构化 progress，回退正则从 message 文本抠 [n/m]，都拿不到用不定进度条。 */
const PROGRESS_PATTERN = /\[(\d+)\s*\/\s*(\d+)\]/

export function resolveStageProgress(
  payload: SseStreamPayload | null,
  stageMessage: string,
): AgentStageProgress {
  const structured = (payload?.progress ?? null) as
    | { current?: number; total?: number; label?: string }
    | null
  const current = Number(structured?.current)
  const total = Number(structured?.total)
  if (structured && Number.isFinite(current) && Number.isFinite(total) && total > 0) {
    const safeCurrent = Math.max(0, Math.min(current, total))
    return {
      current: safeCurrent,
      total,
      percent: Math.min(100, Math.round((safeCurrent / total) * 100)),
      label: structured.label || stageMessage,
      determinate: true,
      active: true,
    }
  }
  const match = PROGRESS_PATTERN.exec(stageMessage || '')
  if (match) {
    const fallbackCurrent = Number(match[1])
    const fallbackTotal = Number(match[2])
    return {
      current: fallbackCurrent,
      total: fallbackTotal,
      percent:
        fallbackTotal > 0 ? Math.min(100, Math.round((fallbackCurrent / fallbackTotal) * 100)) : 0,
      label: stageMessage,
      determinate: fallbackTotal > 0,
      active: true,
    }
  }
  return { current: 0, total: 0, percent: 0, label: stageMessage, determinate: false, active: false }
}

/** DSH 首帧可达 5–8 分钟（spec §6）：连接后长时间无 delta 时用它安抚用户，绝不自动重试 prompt。 */
const STARTING_HINT = 'Agent 启动中，首次响应可能需要几分钟…'

export const useAgentChatStore = defineStore('agentChat', () => {
  // ---- 项目与会话列表 ----
  const projectId = ref<number | null>(null)
  const conversations = ref<AgentConversationItem[]>([])
  const conversationsLoading = ref(false)
  const conversationsError = ref('')
  const currentId = ref<string | null>(null)
  const detailLoading = ref(false)
  const detailError = ref('')

  // ---- 消息与流式 buffer ----
  const messagesByConversation = ref<Record<string, AgentMessageItem[]>>({})
  const streamingText = ref('')
  const streamingThought = ref('')
  const streamingActive = ref(false)
  const stageMessage = ref('')
  /**
   * 后端 status 帧/snapshot 帧的 status 键，仅记录不用于 UI 判断。
   * ⚠️ agent 侧终态帧的 status 是 'active'（会话回到空闲，AgentConversation.status 只有
   * active/busy/closed），**不是 'failed'** —— 与 tasks 侧不同，是后端刻意的。
   * 所以判断终态只看 payload.terminal；会话忙闲看 DB 的 conversation.status。
   */
  const stageStatus = ref('')
  const stageProgressPayload = ref<SseStreamPayload | null>(null)
  const streamError = ref('')
  const reconnectAttempt = ref(0)
  const reconnectMax = ref(3)

  // ---- 动作态 ----
  const sending = ref(false)
  const cancelling = ref(false)
  const savingDraft = ref(false)
  const savingDraftError = ref('')
  const lastSaveResult = ref<AgentSaveAsDraftResult | null>(null)

  // ---- 技能与模型 ----
  const skills = ref<AgentSkillItem[]>([])
  const skillsLoading = ref(false)
  const skillsError = ref('')
  const models = ref<ModelConfig[]>([])
  const modelsLoading = ref(false)

  // ---- 功能可用性（spec §7：AGENT_ENABLED=false 时 /api/agent/* 返回 410）----
  const agentAvailable = ref(true)
  const agentDisabledNotice = ref('')

  let pollTimer: number | null = null
  let startingHintTimer: number | null = null
  let streamGeneration = 0
  /**
   * 本地记录的「用户已请求中止」标志，作为 failed 帧 category 的第二道兜底。
   * 主路径已闭环：后端 AgentStreamBroker（backend/app/services/agent_stream.py:50-202）
   * 把 category 注入 failed 帧 payload，并且在 terminal==='failed' 的 **snapshot** 上也带
   * （_enrich_snapshot_locked:81-93）⇒ 即使取消进行中刷新页面，首帧 snapshot 也能识别为
   * 用户中止，不会误弹红色错误条。本地标志与 wire category 结论一致，保留只为防御
   * 后端回退（category 缺失时仍能区分取消与真失败）。
   */
  let cancelRequested = false

  const currentConversation = computed<AgentConversationItem | null>(() => {
    const id = currentId.value
    if (!id) return null
    return conversations.value.find((item) => item.id === id) ?? null
  })

  const isBusy = computed(() => currentConversation.value?.status === 'busy' || streamingActive.value)

  const persistedMessages = computed<AgentMessageItem[]>(() => {
    const id = currentId.value
    if (!id) return []
    return messagesByConversation.value[id] ?? []
  })

  /** 视图渲染用的消息序列：持久化消息 + 正在流式的 assistant 气泡（只有最后一条走 MarkdownView）。 */
  const displayMessages = computed<AgentDisplayMessage[]>(() => {
    const id = currentId.value
    const base: AgentDisplayMessage[] = (id ? (messagesByConversation.value[id] ?? []) : []).map(
      (item) => ({
        id: item.id,
        conversation_id: item.conversation_id,
        role: item.role === 'assistant' ? 'assistant' : 'user',
        content: item.content,
        thought: item.reasoning || '',
        created_at: item.created_at,
      }),
    )
    if (streamingActive.value || streamingText.value || streamingThought.value) {
      base.push({
        id: 'streaming-assistant',
        conversation_id: id ?? '',
        role: 'assistant',
        content: streamingText.value,
        thought: streamingThought.value,
        created_at: new Date().toISOString(),
        streaming: true,
      })
    }
    return base
  })

  const stageProgress = computed<AgentStageProgress>(() =>
    resolveStageProgress(stageProgressPayload.value, stageMessage.value),
  )

  const canSend = computed(
    () => Boolean(currentId.value) && !sending.value && !cancelling.value && !isBusy.value,
  )

  function setProjectId(value: AgentProjectId | null | undefined) {
    const next = normalizeProjectId(value)
    if (next === projectId.value) return
    projectId.value = next
    // 切项目：当前会话不属于新项目，收掉流与列表（MainLayout 也会因 router-view key 重建视图）。
    teardownStream('project-switch')
    stopPolling()
    conversations.value = []
    currentId.value = null
    messagesByConversation.value = {}
    resetStage()
  }

  function resetStage() {
    streamingText.value = ''
    streamingThought.value = ''
    streamingActive.value = false
    stageMessage.value = ''
    stageStatus.value = ''
    stageProgressPayload.value = null
    streamError.value = ''
    reconnectAttempt.value = 0
    cancelRequested = false
  }

  /**
   * 只丢弃「本地流式副本」，不动 stageMessage / streamError —— 那两条文案还要给用户看。
   *
   * 用于终态帧永远不会到达的路径：重连被拒时 scheduleReconnect 走 teardown + finish('refused')，
   * finalizeTurn / failTurn 都不会被调用。此时若不清 streamingActive，isBusy 会永久为真：
   * 输入框禁用、displayMessages 一直挂着一条 stale 的流式气泡、canSaveDraft 恒 false，
   * 而 AgentChatView 没有任何手动解卡控件，用户只能切会话或刷新页面。
   *
   * 内容不会丢：后端 _finish_turn 在轮次结束（含取消）时就把 assistant 全文落库，
   * 权威历史由 GET /api/agent/conversations/{id} 整体替换（见 refreshConversation）。
   */
  function clearStreamingResidue() {
    cancelRequested = false
    streamingText.value = ''
    streamingThought.value = ''
    streamingActive.value = false
  }

  // ---- SSE（连接管理委托 composable，状态落在 store 里以便跨视图重建存活）----
  const stream = useSseStream({
    closeOnUnmount: false,
    url: () => {
      const id = currentId.value
      if (!id) return null
      return agentStreamUrl(id, projectId.value)
    },
    isActive: () => {
      const conversation = currentConversation.value
      // 详情尚未加载时不武断判定为非活跃（打开会话即连流，靠 snapshot 恢复）。
      if (!conversation) return true
      return conversation.status === 'busy'
    },
    onOpen: () => {
      reconnectAttempt.value = 0
      streamError.value = ''
      if (!stageMessage.value) stageMessage.value = STARTING_HINT
      clearStartingHintTimer()
    },
    onSnapshot: (payload) => {
      applyStage(payload)
      const text = extractStreamText(payload)
      if (typeof text === 'string') {
        streamingText.value = text
        if (text) streamingActive.value = true
      }
      streamingThought.value =
        typeof payload.reasoning === 'string' ? payload.reasoning : ''
      if (payload.terminal === 'completed') void finalizeTurn(payload)
      else if (payload.terminal === 'failed') void failTurn(payload)
    },
    onDelta: (chunk, payload) => {
      streamingActive.value = true
      streamingText.value += chunk
      if (payload.message || payload.status || payload.progress) applyStage(payload)
    },
    onReasoning: (payload) => {
      const chunk = typeof payload.delta === 'string' ? payload.delta : ''
      if (chunk) streamingThought.value += chunk
      // tool_call 边界：正文预览只是过场说明，清空后最终答案从干净正文重新累积。
      if (payload.reset_answer === true) streamingText.value = ''
      if (chunk || payload.reset_answer === true) streamingActive.value = true
      if (payload.message || payload.status || payload.progress) applyStage(payload)
    },
    onStatus: (payload) => {
      streamError.value = ''
      applyStage(payload)
    },
    onReset: (payload) => {
      streamingText.value = typeof payload.text === 'string' ? payload.text : ''
      streamingThought.value = ''
      streamingActive.value = false
      streamError.value = ''
      applyStage(payload)
      scheduleStartingHint()
    },
    onRetry: (payload) => applyStage(payload, '模型请求正在重试…'),
    onNotice: (payload) => applyStage(payload),
    onCompleted: (payload) => void finalizeTurn(payload),
    onFailed: (payload) => void failTurn(payload),
    onReconnecting: (attempt, max) => {
      reconnectAttempt.value = attempt
      reconnectMax.value = max
      stageMessage.value = `实时连接中断，正在重连（${attempt}/${max}）…`
    },
    onReconnectExhausted: () => {
      stageMessage.value = '实时连接已断开，仍会通过自动刷新更新对话状态'
      startPolling()
    },
    onClosed: () => {
      clearStartingHintTimer()
    },
    /**
     * spec §3.6：SSE 的 401 表现为 onerror，直接重连会白跑 3 次。
     * 重连前先探一次登录态与会话状态；未登录 / 已非 busy / 已关闭 ⇒ 拒绝重连。
     */
    beforeReconnect: async () => {
      const id = currentId.value
      if (!id) return false
      try {
        await getSession()
      } catch {
        streamError.value = '登录状态已失效，请重新登录后继续对话'
        // getSession 走 /api/auth/ 前缀，client.ts:74/:102 的 401 拦截会跳过它
        // ⇒ 这里不会自动跳登录页，也没有终态帧会到达，必须自己清掉流式残留。
        clearStreamingResidue()
        return false
      }
      try {
        const detail = await getAgentConversation(id, projectId.value)
        applyConversation(detail)
        if (detail.status !== 'busy') {
          void refreshConversation(id)
          return false
        }
        return true
      } catch (error) {
        // 404 / 410 不是瞬时网络抖动：重连 3 次也不会好，只会白等 3 秒再卡住。
        // 会话已被删（URL 里 ?conversation= 过期）⇒ 清掉这一条并提示重选。
        if (isConversationMissing(error)) {
          handleConversationGone(id)
          return false
        }
        // 服务端关掉了 AGENT_ENABLED ⇒ 整页降级，别再重连。
        if (isAgentDisabled(error)) {
          markAgentUnavailable()
          return false
        }
        // 其余（网络抖动 / 5xx / 409）不阻断重连。
        return true
      }
    },
  })

  const streamConnected = stream.connected
  const streamConnecting = stream.connecting

  function extractStreamText(payload: SseStreamPayload): string | null {
    if (typeof payload.text === 'string') return payload.text
    const content = (payload as AgentStreamPayload & { content?: unknown }).content
    if (typeof content === 'string') return content
    if (typeof payload.delta === 'string') return payload.delta
    return null
  }

  /**
   * failed 帧的分类（后端契约：AgentFailureCategory，14 值）。
   * wire 上没带 category 时回退本地 cancelRequested 标志 ⇒ 返回 'cancelled'；
   * 都没有则返回空串（调用方按普通失败处理）。
   */
  function failureCategory(payload: SseStreamPayload): string {
    const category = (payload as AgentStreamPayload).category
    if (typeof category === 'string' && category) return category
    return cancelRequested ? 'cancelled' : ''
  }

  /**
   * 终态帧的正文（尤其 cancel：category=cancelled 时 text = 已产出的部分正文）。
   *
   * 取 wire 与本地 buffer 中**更长**的那个，不是无条件优先 wire：
   * - 后端 failed 帧的 text 被 max_event_text_chars(**16384**) 截断（task_stream.py:358），
   *   而 broker 累积上限是 max_preview_chars(**256000**) ⇒ 长回答被取消时 wire 反而更短；
   *   本地 buffer 累积了全部 delta，取它能少丢内容。
   * - 反向也成立：completed 帧带 text 时 useSseStream 会 cancelPendingFlush() 丢掉最后一批
   *   rAF 未合帧的 delta ⇒ 此时 wire 比本地更长，取 wire 才完整。
   * 两者是同一条流的前缀关系，取长者即取更全者。刷新恢复（本地 buffer 为空）时自然回落到 wire。
   * 最终仍会被随后的权威历史（GET /conversations/{id}）整体替换。
   */
  function extractPartialText(payload: SseStreamPayload): string {
    const wire = extractStreamText(payload)
    const local = streamingText.value
    if (typeof wire !== 'string' || !wire) return local
    return wire.length >= local.length ? wire : local
  }

  function applyStage(payload: SseStreamPayload, fallback = '') {
    if (payload.status) stageStatus.value = payload.status
    if (payload.message) {
      stageMessage.value = payload.message
      clearStartingHintTimer()
    } else if (fallback) {
      stageMessage.value = fallback
    }
    if (payload.progress !== undefined) stageProgressPayload.value = payload
    if (!stageMessage.value && streamingActive.value) stageMessage.value = STARTING_HINT
  }

  function scheduleStartingHint() {
    clearStartingHintTimer()
    startingHintTimer = window.setTimeout(() => {
      startingHintTimer = null
      if (!stageMessage.value) stageMessage.value = STARTING_HINT
    }, 1500)
  }

  function clearStartingHintTimer() {
    if (startingHintTimer !== null) {
      window.clearTimeout(startingHintTimer)
      startingHintTimer = null
    }
  }

  function applyConversation(conversation: AgentConversationItem) {
    const index = conversations.value.findIndex((item) => item.id === conversation.id)
    if (index >= 0) conversations.value.splice(index, 1, conversation)
    else conversations.value.unshift(conversation)
  }

  function setMessages(conversationId: string, messages: AgentMessageItem[]) {
    messagesByConversation.value[conversationId] = [...messages].sort((a, b) =>
      String(a.created_at).localeCompare(String(b.created_at)),
    )
  }

  function pushLocalMessage(message: AgentDisplayMessage) {
    const conversationId = message.conversation_id || currentId.value
    if (!conversationId) return
    const list = messagesByConversation.value[conversationId] ?? []
    messagesByConversation.value[conversationId] = [
      ...list,
      {
        id: message.id,
        conversation_id: conversationId,
        role: message.role,
        content: message.content,
        reasoning: message.thought || '',
        created_at: message.created_at,
      },
    ]
  }

  /**
   * 用服务端持久化的消息替换乐观气泡（拿到真实 id 与 created_at）。
   * POST /messages 返回 AgentMessageAccepted{conversation, message, duplicated}，
   * 所以不必等 refreshConversation 就能把 local-user-* 换成权威记录。
   */
  function replaceLocalMessage(conversationId: string, localId: string, message: AgentMessageItem) {
    const list = messagesByConversation.value[conversationId]
    if (!Array.isArray(list)) return
    const index = list.findIndex((item) => item.id === localId)
    if (index >= 0) list.splice(index, 1, message)
    else list.push(message)
  }

  function teardownStream(reason: 'manual' | 'terminal' | 'project-switch' | 'dispose' = 'manual') {
    streamGeneration += 1
    clearStartingHintTimer()
    if (reason === 'dispose') stream.close('unmounted')
    else stream.close('manual')
  }

  /** 打开实时流（会话 busy 或刚发出消息时调用）。 */
  function openStream() {
    const id = currentId.value
    if (!id) return
    streamGeneration += 1
    stream.open()
    scheduleStartingHint()
  }

  function stopPolling() {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer)
      pollTimer = null
    }
  }

  /** spec §3.2 兼容清单第 7 条：GET 详情是轮询兜底权威端点（tasks 侧同为 2s setInterval）。 */
  function startPolling() {
    if (pollTimer !== null) return
    pollTimer = window.setInterval(() => {
      const id = currentId.value
      if (!id) {
        stopPolling()
        return
      }
      if (!stream.connected.value && !stream.connecting.value) void refreshConversation(id)
    }, 2000)
  }

  async function refreshConversation(id: string) {
    if (!id) return
    try {
      const detail = await getAgentConversation(id, projectId.value)
      applyConversation(detail)
      setMessages(detail.id, detail.messages)
      if (detail.id === currentId.value && detail.status !== 'busy') {
        stopPolling()
        // 终态帧只走 SSE：重连耗尽（onReconnectExhausted）或被拒（beforeReconnect 返回 false）
        // 之后，finalizeTurn / failTurn 永远不会被调用，本地 streamingActive 就没人清。
        // 而 isBusy 依赖 streamingActive ⇒ 会话永久卡在「忙」：输入框禁用、displayMessages
        // 一直挂着一条 stale 的流式气泡，只能靠切会话或刷新页面解套。
        // 权威状态已说本轮没在跑，就以它为准清掉本地流式残留。
        //
        // 内容不会丢：后端 _finish_turn 在轮次结束（含取消）时就把 assistant 正文落库，
        // 上面 setMessages 已用权威历史整体替换；这里清的只是重复的流式副本。
        //
        // 双重守卫的第二个条件（!connected && !connecting）挡住刚发消息的乐观窗口：
        // sendMessage 先 openStream() 再 void refreshConversation(id)，此时 connecting=true，
        // 不能让一次抢跑的响应把乐观置上的 streamingActive 清掉。
        if (!stream.connected.value && !stream.connecting.value) {
          if (streamingActive.value || streamingText.value) {
            clearStreamingResidue()
            if (stageMessage.value.includes('实时连接')) {
              stageMessage.value = '本轮回复已在服务端结束，对话历史已同步'
            }
          }
        }
      }
      agentAvailable.value = true
    } catch (error) {
      if (isAgentDisabled(error)) markAgentUnavailable()
      else if (isConversationMissing(error)) handleConversationGone(id)
      else if (detailError.value === '' && currentId.value === id) detailError.value = errorMessage(error)
    }
  }

  function markAgentUnavailable() {
    agentAvailable.value = false
    agentDisabledNotice.value = 'Agent 对话未启用：服务端未开启 AGENT_ENABLED，请联系管理员后再使用'
    teardownStream('manual')
    stopPolling()
    // 收流之后终态帧不可能再到达，本地流式副本必须一并丢弃（见 clearStreamingResidue）。
    // 放在这里而不是各调用点：6 处调用全部会收流停轮询，语义一致。
    clearStreamingResidue()
  }

  /**
   * 单会话 404：这条会话已不存在（URL 里的 ?conversation= 过期，或后端已删除）。
   * 只清掉这一条与当前选中态，不能连带判定整个 Agent 能力未启用。
   */
  function handleConversationGone(id: string) {
    teardownStream('manual')
    stopPolling()
    conversations.value = conversations.value.filter((item) => item.id !== id)
    delete messagesByConversation.value[id]
    if (currentId.value === id) {
      currentId.value = null
      streamingText.value = ''
      streamingActive.value = false
      resetStage()
    }
    detailError.value = '该会话不存在或已被删除，请重新选择会话'
  }

  async function loadConversations() {
    conversationsLoading.value = true
    conversationsError.value = ''
    try {
      conversations.value = await listAgentConversations(projectId.value)
      agentAvailable.value = true
    } catch (error) {
      if (isAgentUnavailable(error)) {
        markAgentUnavailable()
        conversations.value = []
      } else {
        conversationsError.value = errorMessage(error)
      }
    } finally {
      conversationsLoading.value = false
    }
  }

  async function loadSkills() {
    if (skills.value.length) return
    skillsLoading.value = true
    skillsError.value = ''
    try {
      skills.value = await listAgentSkills(projectId.value)
    } catch (error) {
      if (isAgentUnavailable(error)) markAgentUnavailable()
      else skillsError.value = errorMessage(error)
    } finally {
      skillsLoading.value = false
    }
  }

  async function loadModels() {
    if (models.value.length) return
    modelsLoading.value = true
    try {
      models.value = await listModels()
    } catch {
      models.value = []
    } finally {
      modelsLoading.value = false
    }
  }

  async function createConversation(body: { title?: string; skill_name?: string | null; model?: string | null }) {
    const conversation = await createAgentConversation(body, projectId.value)
    applyConversation(conversation)
    messagesByConversation.value[conversation.id] = []
    currentId.value = conversation.id
    resetStage()
    detailError.value = ''
    return conversation
  }

  /**
   * 修改【当前会话】的技能/模型配置（顶部摘要行展开后保存）。
   * PATCH 返回体即完整 AgentConversationItem，直接 applyConversation 让界面立即刷新；
   * 不传 expected_version —— 轮次开始/结束都会 +1，传了会把正常保存变成 409。
   * 后端每回合重新读库，所以新配置从【下一条消息】起生效；DSH 网关原生支持会话内切模型，
   * 不需要重建会话、不需要清 dsh_session_id。
   */
  async function updateConversationConfig(skillName: string | null, model: string | null) {
    const conversation = currentConversation.value
    if (!conversation) throw new Error('尚未选择会话，无法修改配置')
    const updated = await updateAgentConversation(
      conversation.id,
      { skill_name: skillName, model },
      projectId.value,
    )
    applyConversation(updated)
    return updated
  }

  async function selectConversation(id: string) {
    if (!id) return
    const alreadyCurrent = id === currentId.value
    if (alreadyCurrent && !detailError.value) {
      void refreshConversation(id)
      return
    }
    teardownStream('manual')
    stopPolling()
    currentId.value = id
    detailError.value = ''
    streamingText.value = ''
    streamingThought.value = ''
    streamingActive.value = false
    streamError.value = ''
    stageMessage.value = ''
    stageStatus.value = ''
    stageProgressPayload.value = null
    detailLoading.value = true
    try {
      const detail = await getAgentConversation(id, projectId.value)
      applyConversation(detail)
      setMessages(detail.id, detail.messages)
      agentAvailable.value = true
      if (detail.status === 'busy') {
        // busy ⇒ 连流恢复（首帧 snapshot 全量正文）；非活跃状态后端会返回 409。
        openStream()
        startPolling()
      }
    } catch (error) {
      if (isAgentDisabled(error)) markAgentUnavailable()
      else if (isConversationMissing(error)) handleConversationGone(id)
      else detailError.value = errorMessage(error)
    } finally {
      detailLoading.value = false
    }
  }

  async function sendMessage(content: string) {
    const text = content.trim()
    const id = currentId.value
    if (!text || !id) return
    sending.value = true
    streamError.value = ''
    cancelRequested = false
    // 幂等键只生成一次：同一个键既做本地气泡 id，也进请求体，
    // 否则重发时后端查不到重，会当成两条新消息。
    const idempotencyKey = createIdempotencyKey()
    const localId = `local-user-${idempotencyKey}`
    pushLocalMessage({
      id: localId,
      conversation_id: id,
      role: 'user',
      content: text,
      created_at: new Date().toISOString(),
      pending: true,
    })
    // 乐观标记 busy，避免重复提交。
    const conversation = currentConversation.value
    if (conversation) applyConversation({ ...conversation, status: 'busy' })
    streamingText.value = ''
    streamingThought.value = ''
    streamingActive.value = true
    try {
      // 不回传 expected_version：乐观锁是可选项，P1 没有并发编辑 UI，
      // 传了只会在版本号过期时把正常发送变成 409。
      const accepted = await sendAgentMessage(
        id,
        { content: text, idempotency_key: idempotencyKey },
        projectId.value,
      )
      // duplicated=true 表示幂等命中（同键重发），后端已存在的消息即权威记录。
      if (accepted?.message?.id) replaceLocalMessage(id, localId, accepted.message)
      if (accepted?.conversation) applyConversation(accepted.conversation)
      openStream()
      startPolling()
      // 再用服务端权威历史整体替换（消息顺序 / 其它端点写入）。
      void refreshConversation(id)
    } catch (error) {
      const list = messagesByConversation.value[id] ?? []
      messagesByConversation.value[id] = list.filter((item) => item.id !== localId)
      streamingActive.value = false
      streamingText.value = ''
      streamingThought.value = ''
      if (conversation) applyConversation({ ...conversation, status: 'active' })
      streamError.value = errorMessage(error)
      if (isAgentDisabled(error)) markAgentUnavailable()
      else if (isConversationMissing(error)) handleConversationGone(id)
      throw error
    } finally {
      sending.value = false
    }
  }

  async function finalizeTurn(payload: SseStreamPayload) {
    cancelRequested = false
    const id = currentId.value
    const finalText = extractPartialText(payload)
    teardownStream('terminal')
    // 必须在 teardownStream 之后捕获：teardownStream 内部会自增 streamGeneration，
    // 若在它之前捕获，下面的守卫会恒为真，成功路径永远提前 return、状态写不回前端。
    const generation = streamGeneration
    stopPolling()
    clearStartingHintTimer()
    applyStage(payload, '本轮回复完成')
    if (finalText) {
      pushLocalMessage({
        id: `local-assistant-${Date.now()}`,
        conversation_id: id ?? '',
        role: 'assistant',
        content: finalText,
        thought: streamingThought.value,
        created_at: new Date().toISOString(),
      })
    }
    streamingText.value = ''
    streamingThought.value = ''
    streamingActive.value = false
    if (id) {
      try {
        const detail = await getAgentConversation(id, projectId.value)
        if (generation !== streamGeneration) return
        // completed 终态意味本轮已结束：即便后端此刻仍回 busy，也强制落 active，
        // 避免将来后端提交顺序变化时客户端永久卡在「生成中」。
        const resolved = detail.status === 'busy' ? { ...detail, status: 'active' as const } : detail
        applyConversation(resolved)
        setMessages(resolved.id, resolved.messages)
      } catch {
        const conversation = currentConversation.value
        if (conversation) applyConversation({ ...conversation, status: 'active' })
      }
    }
  }

  /**
   * failed 终态帧。按后端契约分两类：
   * - category='cancelled'：用户主动中止，**不是错误** ⇒ 不写 streamError
   *   （否则 ChatPanel 的 el-alert type=error 会弹红条），DB 侧 status 已回到 active；
   * - 其它（unauthorized / timeout / stream_error …）：写 streamError，message 是后端出的中文，可直接展示。
   * 两类都保留已产出正文（failed 帧的 text 由后端持久化后带回），并用权威历史覆盖。
   */
  async function failTurn(payload: SseStreamPayload) {
    const id = currentId.value
    const category = failureCategory(payload)
    const cancelled = category === 'cancelled'
    const partial = extractPartialText(payload)
    cancelRequested = false
    teardownStream('terminal')
    stopPolling()
    clearStartingHintTimer()
    applyStage(payload, cancelled ? '本轮回复已中止' : '本轮回复失败')
    streamError.value = cancelled
      ? ''
      : (typeof payload.message === 'string' && payload.message) || 'Agent 本轮执行失败，请稍后重试'
    if (partial) {
      pushLocalMessage({
        id: `local-assistant-${Date.now()}`,
        conversation_id: id ?? '',
        role: 'assistant',
        content: cancelled
          ? `${partial}\n\n> 本轮回复已中止，以上内容可能不完整。`
          : `${partial}\n\n> 本轮回复中断，以上内容可能不完整。`,
        thought: streamingThought.value,
        created_at: new Date().toISOString(),
      })
    }
    streamingText.value = ''
    streamingThought.value = ''
    streamingActive.value = false
    if (id) await refreshConversation(id)
  }

  async function cancelTurn() {
    const id = currentId.value
    if (!id) return
    cancelling.value = true
    try {
      const result = await cancelAgentTurn(id, projectId.value)
      // 后端契约：cancel 返回 AgentCancelOut{conversation, cancelled, message}，
      // 且**随后一定发 failed 帧（category=cancelled）再关流** ⇒ 这里不关流，
      // 保持连接等终态帧，同时开轮询兜底（万一连接已断）。
      cancelRequested = true
      stageMessage.value = result?.message || '已请求中止本轮回复…'
      if (result?.conversation) applyConversation(result.conversation)
      startPolling()
    } catch (error) {
      streamError.value = errorMessage(error)
    } finally {
      cancelling.value = false
    }
  }

  async function closeConversation(id?: string) {
    const target = id ?? currentId.value
    if (!target) return
    await closeAgentConversation(target, projectId.value)
    const conversation = conversations.value.find((item) => item.id === target)
    if (conversation) applyConversation({ ...conversation, status: 'closed' })
    if (target === currentId.value) {
      teardownStream('manual')
      stopPolling()
      resetStage()
    }
    await loadConversations()
  }

  async function saveAsDraft() {
    const id = currentId.value
    if (!id) return null
    savingDraft.value = true
    savingDraftError.value = ''
    lastSaveResult.value = null
    try {
      const result = await saveAgentConversationAsDraft(id, projectId.value)
      lastSaveResult.value = result
      return result
    } catch (error) {
      savingDraftError.value = errorMessage(error)
      throw error
    } finally {
      savingDraft.value = false
    }
  }

  /** 视图卸载时调用：只在离开 Agent 页且没有进行中的轮次时收流，避免打断后台 agent。 */
  function releaseView() {
    if (streamingActive.value || currentConversation.value?.status === 'busy') {
      startPolling()
      return
    }
    teardownStream('manual')
    stopPolling()
  }

  /** 彻底释放（切项目 / 退出登录）。 */
  function dispose() {
    teardownStream('dispose')
    stopPolling()
    resetStage()
    conversations.value = []
    currentId.value = null
    messagesByConversation.value = {}
  }

  return {
    // state
    projectId,
    conversations,
    conversationsLoading,
    conversationsError,
    currentId,
    detailLoading,
    detailError,
    messagesByConversation,
    streamingText,
    streamingThought,
    streamingActive,
    stageMessage,
    stageStatus,
    streamError,
    reconnectAttempt,
    reconnectMax,
    sending,
    cancelling,
    savingDraft,
    savingDraftError,
    lastSaveResult,
    skills,
    skillsLoading,
    skillsError,
    models,
    modelsLoading,
    agentAvailable,
    agentDisabledNotice,
    streamConnected,
    streamConnecting,
    // getters
    currentConversation,
    isBusy,
    persistedMessages,
    displayMessages,
    stageProgress,
    canSend,
    // actions
    setProjectId,
    loadConversations,
    loadSkills,
    loadModels,
    createConversation,
    selectConversation,
    updateConversationConfig,
    sendMessage,
    cancelTurn,
    closeConversation,
    saveAsDraft,
    refreshConversation,
    openStream,
    releaseView,
    dispose,
    resetStage,
  }
})
