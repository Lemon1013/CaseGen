import { getCurrentInstance, onUnmounted, ref } from 'vue'

/**
 * 与 tasks 侧同协议的 SSE 连接管理（spec docs/superpowers/specs/2026-11-04-dsh-agent-integration-design.md §3.6-4）。
 *
 * 协议要点（与 GET /api/tasks/{id}/stream 对齐）：
 * - 首帧必为全量 snapshot ⇒ 刷新 / 重连可恢复，不依赖 Last-Event-ID 续传；
 * - 事件名 snapshot / status / delta / reset / retry / notice / completed / failed；
 * - delta 用 requestAnimationFrame 合帧追加，避免每帧一次全量 Markdown 重 parse；
 * - 断连线性退避重连 attempt * 1000ms，上限 3 次；
 * - 心跳是后端 SSE 注释行 ": keep-alive"，EventSource 自动忽略，前端无需处理；
 * - 组件卸载（onUnmounted）必须 close()，重进靠 snapshot 恢复。
 *
 * 独立编写，不重构 TaskDetailView.vue（2514 行且带用户未提交 WIP，抽取共用会冲突）；
 * 参考其 openLiveStream / scheduleLiveStreamReconnect / isCurrentLiveStream / queueLivePreviewDelta
 * 的实现范式。TaskDetailView 迁移留作 WIP 合并后的独立任务。
 */

export interface SseProgress {
  current?: number
  total?: number
  label?: string
}

/**
 * SSE 帧 payload（与 tasks 侧 TaskStreamPayload 同构，另有索引签名透传后端扩展键，
 * 如 agent 侧 failed / snapshot 帧的 category）。
 *
 * ⚠️ 终态判定**只看 terminal，不要看 status**：tasks 侧 failed 帧的 status 是 'failed'，
 * 但 agent 侧终态帧的 status 是 'active'（AgentConversation.status 只有 active/busy/closed，
 * 会话终态即回到空闲）。本 composable 一律用 terminal 分流，调用方也应遵守。
 */
export interface SseStreamPayload {
  status?: string
  message?: string
  /** snapshot / completed 帧的全量正文。 */
  text?: string
  /** delta 帧的增量文本。 */
  delta?: string
  /** snapshot 帧的全量思考过程；reasoning 帧携带增量 delta。 */
  reasoning?: string
  /** reasoning 帧在 tool_call 边界置 true：清空已临时显示的正文预览。 */
  reset_answer?: boolean
  sequence?: number
  terminal?: 'completed' | 'failed' | null
  attempt?: number
  progress?: SseProgress | null
  /** failed 帧的机器可读原因；调用方可据此拒绝重连（如 unauthorized / closed）。 */
  reason?: string | null
  [key: string]: unknown
}

export type SseEventName =
  | 'snapshot'
  | 'status'
  | 'delta'
  | 'reasoning'
  | 'reset'
  | 'retry'
  | 'notice'
  | 'completed'
  | 'failed'

export const SSE_EVENT_NAMES: SseEventName[] = [
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

export const SSE_MAX_RECONNECTS = 3

export interface SseStreamHandlers {
  /** snapshot：调用方用它整体替换正文（全量恢复）。 */
  onSnapshot?: (payload: SseStreamPayload) => void
  /** delta：已合帧，一次调用携带自上次 flush 以来的全部增量。 */
  onDelta?: (chunk: string, payload: SseStreamPayload) => void
  /** reasoning：思考过程增量；payload.reset_answer 为 true 时调用方需清空正文预览。 */
  onReasoning?: (payload: SseStreamPayload) => void
  onStatus?: (payload: SseStreamPayload) => void
  /** reset：新一轮开始，正文清空（payload.text 若为字符串则以其为准）。 */
  onReset?: (payload: SseStreamPayload) => void
  onRetry?: (payload: SseStreamPayload) => void
  onNotice?: (payload: SseStreamPayload) => void
  onCompleted?: (payload: SseStreamPayload) => void
  onFailed?: (payload: SseStreamPayload) => void
  onOpen?: () => void
  /**
   * 重连前的探测钩子：SSE 的 401 表现为 onerror，会被误当网络错误重连 3 次。
   * 返回 false 表示不要再重连（如已探测到未登录 / 会话已 closed）。
   */
  beforeReconnect?: (attempt: number) => boolean | Promise<boolean>
  onReconnecting?: (attempt: number, max: number) => void
  onReconnectExhausted?: () => void
  /** 连接彻底结束（主动 close、终态、退避用尽、探测拒绝）。 */
  onClosed?: (reason: 'manual' | 'completed' | 'failed' | 'exhausted' | 'refused' | 'unmounted') => void
}

export interface SseStreamOptions extends SseStreamHandlers {
  /** 返回当前要连接的 URL；返回空字符串 / null 表示不该连接。 */
  url: () => string | null | undefined
  /** 仍应处于「活跃」状态的判定；返回 false 时不开流并静默忽略回调。 */
  isActive?: () => boolean
  maxReconnects?: number
  /** 默认线性退避 attempt * 1000ms。 */
  reconnectDelayMs?: (attempt: number) => number
  /** 是否在组件卸载时自动 close（默认 true；在 store 等非组件环境自动降级为 false）。 */
  closeOnUnmount?: boolean
}

export interface SseStreamController {
  open: () => void
  close: (reason?: 'manual' | 'unmounted') => void
  /** 强制把已缓冲的 delta 立即交给 onDelta（终态帧前用）。 */
  flushDelta: () => void
  cancelPendingFlush: () => void
  readonly connected: { value: boolean }
  readonly connecting: { value: boolean }
  readonly attempts: { value: number }
  readonly error: { value: string }
  readonly lastEvent: { value: SseEventName | null }
  readonly closed: { value: boolean }
}

/** EventSource 帧的 data 解析：非 JSON 或非对象一律返回 null（同 parseStreamPayload 范式）。 */
export function parseSsePayload(event: Event): SseStreamPayload | null {
  const data = (event as MessageEvent<string>).data
  if (typeof data !== 'string' || !data) return null
  try {
    const parsed = JSON.parse(data)
    if (!parsed || typeof parsed !== 'object') return null
    return parsed as SseStreamPayload
  } catch {
    return null
  }
}

export function useSseStream(options: SseStreamOptions): SseStreamController {
  const maxReconnects = options.maxReconnects ?? SSE_MAX_RECONNECTS
  const reconnectDelayMs = options.reconnectDelayMs ?? ((attempt: number) => attempt * 1000)

  const connected = ref(false)
  const connecting = ref(false)
  const attempts = ref(0)
  const error = ref('')
  const lastEvent = ref<SseEventName | null>(null)
  const closed = ref(false)

  let source: EventSource | null = null
  /** 世代号：每次 open/close 自增，所有异步回调据此丢弃过期帧。 */
  let generation = 0
  let reconnectTimer: number | null = null
  let deltaBuffer = ''
  let deltaBufferGeneration = 0
  let flushFrame: number | null = null
  let lastDeltaPayload: SseStreamPayload = {}
  let disposed = false

  function isCurrent(gen: number, candidate: EventSource | null): boolean {
    return !disposed && gen === generation && (candidate === null || candidate === source)
  }

  function cancelPendingFlush() {
    if (flushFrame !== null) {
      cancelAnimationFrame(flushFrame)
      flushFrame = null
    }
    deltaBuffer = ''
  }

  function flushDelta() {
    const chunk = deltaBuffer
    const gen = deltaBufferGeneration
    const payload = lastDeltaPayload
    deltaBuffer = ''
    if (flushFrame !== null) {
      cancelAnimationFrame(flushFrame)
      flushFrame = null
    }
    if (!chunk || !isCurrent(gen, null)) return
    options.onDelta?.(chunk, payload)
  }

  /** rAF 合帧：同一帧内到达的多个 delta 只触发一次 onDelta。 */
  function queueDelta(gen: number, payload: SseStreamPayload) {
    if (typeof payload.delta !== 'string' || !payload.delta) return
    if (deltaBufferGeneration !== gen) {
      cancelPendingFlush()
      deltaBufferGeneration = gen
    }
    deltaBuffer += payload.delta
    lastDeltaPayload = payload
    if (flushFrame === null) {
      flushFrame = requestAnimationFrame(() => {
        flushFrame = null
        flushDelta()
      })
    }
  }

  function clearReconnectTimer() {
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
  }

  function finish(reason: 'manual' | 'completed' | 'failed' | 'exhausted' | 'refused' | 'unmounted') {
    closed.value = true
    options.onClosed?.(reason)
  }

  function teardown() {
    clearReconnectTimer()
    cancelPendingFlush()
    if (source) {
      source.close()
      source = null
    }
    connected.value = false
    connecting.value = false
  }

  async function scheduleReconnect(gen: number) {
    if (!isCurrent(gen, null)) return
    if (options.isActive && !options.isActive()) return
    if (attempts.value >= maxReconnects) {
      teardown()
      options.onReconnectExhausted?.()
      finish('exhausted')
      return
    }
    if (options.beforeReconnect) {
      let allowed = true
      try {
        allowed = await options.beforeReconnect(attempts.value + 1)
      } catch {
        allowed = false
      }
      if (!allowed || !isCurrent(gen, null)) {
        teardown()
        finish('refused')
        return
      }
    }
    attempts.value += 1
    const attempt = attempts.value
    const delay = reconnectDelayMs(attempt)
    options.onReconnecting?.(attempt, maxReconnects)
    clearReconnectTimer()
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null
      if (!isCurrent(gen, null)) return
      source = null
      openInternal(gen)
    }, delay)
  }

  function attach(gen: number, candidate: EventSource) {
    const guard = (source_: EventSource) => isCurrent(gen, source_)

    candidate.onopen = () => {
      if (!guard(candidate)) {
        candidate.close()
        return
      }
      connecting.value = false
      connected.value = true
      error.value = ''
      options.onOpen?.()
    }

    candidate.addEventListener('snapshot', (event: Event) => {
      if (!guard(candidate)) return
      lastEvent.value = 'snapshot'
      cancelPendingFlush()
      const payload = parseSsePayload(event) ?? {}
      options.onSnapshot?.(payload)
      // 刷新恢复路径：首帧 snapshot 就可能已是终态。只按 terminal 分流（见类型注释）。
      if (payload.terminal === 'completed') handleCompleted(gen, candidate, payload)
      else if (payload.terminal === 'failed') handleFailed(gen, candidate, payload)
    })

    candidate.addEventListener('status', (event: Event) => {
      if (!guard(candidate)) return
      lastEvent.value = 'status'
      error.value = ''
      options.onStatus?.(parseSsePayload(event) ?? {})
    })

    candidate.addEventListener('delta', (event: Event) => {
      if (!guard(candidate)) return
      lastEvent.value = 'delta'
      const payload = parseSsePayload(event)
      if (!payload) return
      queueDelta(gen, payload)
    })

    candidate.addEventListener('reasoning', (event: Event) => {
      if (!guard(candidate)) return
      lastEvent.value = 'reasoning'
      const payload = parseSsePayload(event)
      if (!payload) return
      // tool_call 边界：先丢弃已排队但尚未 flush 的 delta，避免它们
      // 在 reset_answer 清空正文后又被拼回去。
      if (payload.reset_answer === true) cancelPendingFlush()
      options.onReasoning?.(payload)
    })

    candidate.addEventListener('reset', (event: Event) => {
      if (!guard(candidate)) return
      lastEvent.value = 'reset'
      cancelPendingFlush()
      error.value = ''
      options.onReset?.(parseSsePayload(event) ?? {})
    })

    candidate.addEventListener('retry', (event: Event) => {
      if (!guard(candidate)) return
      lastEvent.value = 'retry'
      options.onRetry?.(parseSsePayload(event) ?? {})
    })

    candidate.addEventListener('notice', (event: Event) => {
      if (!guard(candidate)) return
      lastEvent.value = 'notice'
      options.onNotice?.(parseSsePayload(event) ?? {})
    })

    candidate.addEventListener('completed', (event: Event) => {
      if (!guard(candidate)) return
      handleCompleted(gen, candidate, parseSsePayload(event) ?? {})
    })

    candidate.addEventListener('failed', (event: Event) => {
      if (!guard(candidate)) return
      handleFailed(gen, candidate, parseSsePayload(event) ?? {})
    })

    candidate.onerror = () => {
      if (!guard(candidate)) return
      candidate.close()
      if (source === candidate) source = null
      connected.value = false
      connecting.value = false
      void scheduleReconnect(gen)
    }
  }

  function handleCompleted(gen: number, candidate: EventSource, payload: SseStreamPayload) {
    if (!isCurrent(gen, candidate)) return
    lastEvent.value = 'completed'
    // 终态帧可能自带全量 text；否则把缓冲增量交出去。
    if (typeof payload.text === 'string') {
      cancelPendingFlush()
      options.onCompleted?.(payload)
    } else {
      flushDelta()
      options.onCompleted?.(payload)
    }
    teardown()
    finish('completed')
  }

  function handleFailed(gen: number, candidate: EventSource, payload: SseStreamPayload) {
    if (!isCurrent(gen, candidate)) return
    lastEvent.value = 'failed'
    cancelPendingFlush()
    error.value = typeof payload.message === 'string' ? payload.message : '实时连接失败'
    options.onFailed?.(payload)
    teardown()
    finish('failed')
  }

  function openInternal(gen: number) {
    if (disposed || gen !== generation) return
    if (options.isActive && !options.isActive()) return
    const url = options.url()
    if (!url) return
    if (source) return
    closed.value = false
    connecting.value = true
    const candidate = new EventSource(url)
    source = candidate
    deltaBufferGeneration = gen
    attach(gen, candidate)
  }

  function open() {
    if (disposed) return
    teardown()
    generation += 1
    attempts.value = 0
    error.value = ''
    closed.value = false
    deltaBufferGeneration = generation
    openInternal(generation)
  }

  function close(reason: 'manual' | 'unmounted' = 'manual') {
    if (disposed) return
    generation += 1
    teardown()
    if (reason === 'unmounted') disposed = true
    finish(reason)
  }

  const closeOnUnmount = options.closeOnUnmount ?? true
  if (closeOnUnmount && getCurrentInstance()) {
    onUnmounted(() => {
      close('unmounted')
    })
  }

  return {
    open,
    close,
    flushDelta,
    cancelPendingFlush,
    connected,
    connecting,
    attempts,
    error,
    lastEvent,
    closed,
  }
}
