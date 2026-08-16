<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import MarkdownView from '../components/MarkdownView.vue'
import TaskTimeline from '../components/TaskTimeline.vue'
import CitationList from '../components/CitationList.vue'
import ReviewCard from '../components/ReviewCard.vue'
import { formatDateTime as formatTime } from '../utils/datetime'
import {
  applyPrompt,
  finalizeTask,
  generateTask,
  getTask,
  listCitations,
  listDrafts,
  listEvents,
  listReviews,
  listRevisions,
  optimizePromptTask,
  regenerateTask,
  reviewTask,
  shouldPollTaskStatus,
  statusLabel,
  statusTagType,
  taskStreamUrl,
  type ApplyPromptMode,
  type CaseDraft,
  type PromptRevision,
  type ReviewResult,
  type TaskCitation,
  type TaskEvent,
  type TaskItem,
  type TaskStreamPayload,
  updateTaskModel,
  confirmRetrievalCheckpoint,
  getRetrievalCheckpoint,
  getTestPointCheckpoint,
  editTestPoints,
  confirmTestPoints,
  getTaskCoverage,
  type RetrievalCheckpoint,
  type TestPointCheckpoint,
  type TestPointItem,
  type CoverageSummary,
} from '../api/tasks'
import { listModels, type ModelConfig } from '../api/models'

const route = useRoute()
const router = useRouter()

const loading = ref(false)
const acting = ref(false)
const task = ref<TaskItem | null>(null)
const drafts = ref<CaseDraft[]>([])
const events = ref<TaskEvent[]>([])
const reviews = ref<ReviewResult[]>([])
const revisions = ref<PromptRevision[]>([])
const citations = ref<TaskCitation[]>([])
const checkpoint = ref<RetrievalCheckpoint | null>(null)
const selectedCitationIds = ref<number[]>([])
const supplementalText = ref('')
const confirmingCheckpoint = ref(false)
const testPointCheckpoint = ref<TestPointCheckpoint | null>(null)
const testPoints = ref<TestPointItem[]>([])
const confirmingTestPoints = ref(false)
const coverage = ref<CoverageSummary | null>(null)
const models = ref<ModelConfig[]>([])
const activeDraftTab = ref('')

const applyDialogVisible = ref(false)
const applyMode = ref<ApplyPromptMode>('task_temp')
const selectedRevision = ref<PromptRevision | null>(null)
const alsoRegenerate = ref(true)
const editedPromptContent = ref('')
const retryModelId = ref<number | null>(null)
const livePreviewText = ref('')
const liveStageMessage = ref('')
const liveStageStatus = ref('')
const liveStreamError = ref('')
const liveStreamConnected = ref(false)
const liveStreamConnecting = ref(false)

let pollTimer: number | null = null
let liveStreamSource: EventSource | null = null
let liveStreamTaskId: number | null = null
let liveStreamReconnectTimer: number | null = null
let liveStreamReconnectAttempts = 0
let livePreviewDeltaBuffer = ''
let livePreviewBufferTaskId: number | null = null
let livePreviewFlushFrame: number | null = null
let postGenerationConfirmTimer: number | null = null
let postGenerationConfirmTaskId: number | null = null
let postGenerationConfirmAttempts = 0
let postGenerationConfirmEpoch = 0
let dataRequestSequence = 0
let latestAppliedDataRequest = 0
let refreshInFlightSequence: number | null = null
let loadSequence = 0

const LIVE_STREAM_STATUSES = new Set(['retrieving', 'generating_test_points', 'generating', 'regenerating'])
const LIVE_STREAM_MAX_RECONNECTS = 3
const POST_GENERATION_CONFIRM_DELAY_MS = 400
const POST_GENERATION_CONFIRM_ATTEMPTS = 10

const taskId = computed(() => Number(route.params.id))

const isBusy = computed(() =>
  shouldPollTaskStatus(task.value?.status),
)

const isLiveGeneration = computed(() =>
  Boolean(task.value && task.value.id === taskId.value && LIVE_STREAM_STATUSES.has(task.value.status)),
)

const showLivePreview = computed(() =>
  Boolean(livePreviewText.value || liveStreamConnected.value || liveStreamConnecting.value),
)

const showEmptyCitationBanner = computed(() => {
  if (!task.value) return false
  const s = task.value.status
  return (
    citations.value.length === 0 &&
    ['generated', 'reviewed', 'finalized', 'regenerating'].includes(s)
  )
})

const pendingRevisions = computed(() =>
  revisions.value.filter((r) => r.status === 'pending'),
)

const latestReview = computed(
  () => task.value?.latest_review || reviews.value[0] || null,
)

const highlightFinal = computed(() => {
  if (!task.value || task.value.status === 'finalized') return false
  const r = latestReview.value
  if (!r) return false
  return Boolean(r.payload?.ready_for_final) || r.score >= 80
})

function formatCoverage(value: number): string {
  return `${value}%`
}

function nextDataRequest(): number {
  dataRequestSequence += 1
  return dataRequestSequence
}

function canApplyDataRequest(id: number, sequence: number): boolean {
  return id === taskId.value && sequence >= latestAppliedDataRequest
}

function markDataRequestApplied(sequence: number) {
  latestAppliedDataRequest = sequence
}

function invalidatePendingDataReads() {
  markDataRequestApplied(nextDataRequest())
}

function applyTaskUpdate(updated: TaskItem) {
  invalidatePendingDataReads()
  task.value = updated
}

function hydrateRetrievalEditor(fresh: RetrievalCheckpoint) {
  const changed = !checkpoint.value
    || checkpoint.value.id !== fresh.id
    || checkpoint.value.version !== fresh.version
  checkpoint.value = fresh
  if (!changed) return
  selectedCitationIds.value = fresh.selected_citation_ids.length
    ? [...fresh.selected_citation_ids]
    : fresh.candidate_citations.map((item) => item.id)
  supplementalText.value = fresh.supplemental_text || ''
}

function hydrateTestPointEditor(fresh: TestPointCheckpoint) {
  const changed = !testPointCheckpoint.value
    || testPointCheckpoint.value.id !== fresh.id
    || testPointCheckpoint.value.version !== fresh.version
  testPointCheckpoint.value = fresh
  if (changed) testPoints.value = fresh.points.map((item) => ({ ...item }))
}

function syncCheckpointEditors(
  status: string,
  retrievalFresh: RetrievalCheckpoint | null,
  testPointFresh: TestPointCheckpoint | null,
) {
  if (status === 'awaiting_confirmation' && retrievalFresh) {
    hydrateRetrievalEditor(retrievalFresh)
  } else {
    checkpoint.value = null
    selectedCitationIds.value = []
    supplementalText.value = ''
  }

  if (status === 'awaiting_test_point_confirmation' && testPointFresh) {
    hydrateTestPointEditor(testPointFresh)
  } else {
    testPointCheckpoint.value = null
    testPoints.value = []
  }
}

async function loadAll() {
  const id = taskId.value
  if (!id || Number.isNaN(id)) return
  const requestSequence = nextDataRequest()
  const currentLoadSequence = ++loadSequence
  loading.value = true
  try {
    const [t, d, e, rev, revs, c, modelRows] = await Promise.all([
      getTask(id),
      listDrafts(id),
      listEvents(id),
      listReviews(id),
      listRevisions(id),
      listCitations(id),
      listModels().catch(() => [] as ModelConfig[]),
    ])
    const retrievalFresh = t.status === 'awaiting_confirmation'
      ? await getRetrievalCheckpoint(id)
      : null
    const testPointFresh = t.status === 'awaiting_test_point_confirmation'
      ? await getTestPointCheckpoint(id).catch(() => null)
      : null
    const coverageFresh = await getTaskCoverage(id).catch(() => null)
    if (!canApplyDataRequest(id, requestSequence)) return
    markDataRequestApplied(requestSequence)
    task.value = t
    drafts.value = d
    events.value = e
    reviews.value = rev
    revisions.value = revs
    citations.value = c
    syncCheckpointEditors(t.status, retrievalFresh, testPointFresh)
    coverage.value = coverageFresh
    models.value = modelRows
    retryModelId.value =
      t.model_id ?? modelRows.find((model) => model.is_default)?.id ?? modelRows[0]?.id ?? null
    if (!activeDraftTab.value && d.length) {
      activeDraftTab.value = String(d[0].id)
    }
  } catch (err) {
    if (canApplyDataRequest(id, requestSequence)) {
      ElMessage.error(`加载任务失败：${(err as Error).message}`)
    }
  } finally {
    if (id === taskId.value && currentLoadSequence === loadSequence) loading.value = false
  }
}

async function confirmCheckpoint() {
  if (!task.value || !checkpoint.value || confirmingCheckpoint.value) return
  if (!selectedCitationIds.value.length && !supplementalText.value.trim()) {
    ElMessage.warning('至少选择一条引用或填写补充上下文')
    return
  }
  confirmingCheckpoint.value = true
  try {
    const updated = await confirmRetrievalCheckpoint(task.value.id, {
      selected_citation_ids: selectedCitationIds.value,
      supplemental_text: supplementalText.value,
      expected_version: checkpoint.value.version,
      idempotency_key: `task-${task.value.id}-checkpoint-${checkpoint.value.id}-v${checkpoint.value.version}`,
    })
    applyTaskUpdate(updated)
    await refreshLight(true)
  } catch (err) {
    ElMessage.error(`确认失败：${(err as Error).message}`)
  } finally {
    confirmingCheckpoint.value = false
  }
}

async function refreshLight(force = false): Promise<boolean> {
  const id = taskId.value
  if (!id || Number.isNaN(id)) return false
  if (refreshInFlightSequence !== null && !force) return false
  const requestSequence = nextDataRequest()
  refreshInFlightSequence = requestSequence
  try {
    const [t, d, e, rev, revs, c] = await Promise.all([
      getTask(id),
      listDrafts(id),
      listEvents(id),
      listReviews(id),
      listRevisions(id),
      listCitations(id),
    ])
    const retrievalFresh = t.status === 'awaiting_confirmation'
      ? await getRetrievalCheckpoint(id)
      : null
    const testPointFresh = t.status === 'awaiting_test_point_confirmation'
      ? await getTestPointCheckpoint(id).catch(() => null)
      : null
    const coverageFresh = await getTaskCoverage(id).catch(() => null)
    if (!canApplyDataRequest(id, requestSequence)) return false
    markDataRequestApplied(requestSequence)
    task.value = t
    drafts.value = d
    events.value = e
    reviews.value = rev
    revisions.value = revs
    citations.value = c
    syncCheckpointEditors(t.status, retrievalFresh, testPointFresh)
    coverage.value = coverageFresh
    if (d.length && !d.some((x) => String(x.id) === activeDraftTab.value)) {
      activeDraftTab.value = String(d[0].id)
    }
    return true
  } catch {
    // keep polling; surface hard errors on manual actions
    return false
  } finally {
    if (refreshInFlightSequence === requestSequence) refreshInFlightSequence = null
  }
}

function addTestPoint() {
  const next = testPoints.value.length + 1
  testPoints.value.push({
    id: -Date.now(), task_id: taskId.value, checkpoint_id: testPointCheckpoint.value?.id || 0,
    stable_key: `TP-${String(next).padStart(3, '0')}`, title: '', verification_goal: '',
    dimension: task.value?.test_dimensions?.[0] || 'positive', priority: 'P1', sort_order: next - 1,
    is_selected: true, is_excluded: false, citation_ids: [], created_at: '', updated_at: '',
  })
}

function removeTestPoint(index: number) {
  testPoints.value.splice(index, 1)
  testPoints.value.forEach((item, itemIndex) => { item.sort_order = itemIndex })
}

function pointPayload() {
  return testPoints.value.map((item, index) => ({
    stable_key: item.stable_key,
    title: item.title,
    verification_goal: item.verification_goal,
    dimension: item.dimension,
    priority: item.priority,
    sort_order: index,
    is_selected: item.is_selected,
    is_excluded: item.is_excluded,
    citation_ids: item.citation_ids,
  }))
}

async function saveTestPoints() {
  if (!testPointCheckpoint.value) return
  confirmingTestPoints.value = true
  try {
    const saved = await editTestPoints(taskId.value, {
      expected_version: testPointCheckpoint.value.version,
      points: pointPayload(),
    })
    invalidatePendingDataReads()
    testPointCheckpoint.value = saved
    testPoints.value = saved.points.map((item) => ({ ...item }))
    ElMessage.success('测试点编辑已保存')
  } catch (err) {
    ElMessage.error(`保存测试点失败：${(err as Error).message}`)
  } finally {
    confirmingTestPoints.value = false
  }
}

async function confirmTestPointStage() {
  if (!task.value || !testPointCheckpoint.value) return
  confirmingTestPoints.value = true
  try {
    const updated = await confirmTestPoints(task.value.id, {
      expected_version: testPointCheckpoint.value.version,
      idempotency_key: `task-${task.value.id}-test-points-${testPointCheckpoint.value.id}-v${testPointCheckpoint.value.version}`,
      points: pointPayload(),
    })
    applyTaskUpdate(updated)
    await refreshLight(true)
    ElMessage.success('测试点已确认，开始生成完整用例')
  } catch (err) { ElMessage.error(`确认测试点失败：${(err as Error).message}`) }
  finally { confirmingTestPoints.value = false }
}

function startPolling() {
  stopPolling()
  pollTimer = window.setInterval(() => {
    if (shouldPollTaskStatus(task.value?.status)) {
      void refreshLight()
    }
  }, 2000)
}

function stopPolling() {
  if (pollTimer != null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function cancelPostGenerationConfirmation() {
  postGenerationConfirmEpoch += 1
  if (postGenerationConfirmTimer != null) {
    clearTimeout(postGenerationConfirmTimer)
    postGenerationConfirmTimer = null
  }
  postGenerationConfirmTaskId = null
  postGenerationConfirmAttempts = 0
}

function shouldStopPostGenerationConfirmation(status?: string) {
  return ['reviewing', 'optimizing', 'reviewed', 'failed', 'finalized'].includes(status || '')
}

function startPostGenerationConfirmation(id: number) {
  cancelPostGenerationConfirmation()
  if (id !== taskId.value || task.value?.id !== id || shouldStopPostGenerationConfirmation(task.value.status)) {
    return
  }
  const epoch = ++postGenerationConfirmEpoch
  postGenerationConfirmTaskId = id

  const confirmStatus = async () => {
    if (
      epoch !== postGenerationConfirmEpoch ||
      postGenerationConfirmTaskId !== id ||
      id !== taskId.value ||
      task.value?.id !== id
    ) {
      return
    }

    await refreshLight()
    if (
      epoch !== postGenerationConfirmEpoch ||
      postGenerationConfirmTaskId !== id ||
      id !== taskId.value ||
      task.value?.id !== id
    ) {
      return
    }

    const status = task.value.status
    if (status === 'reviewing' || status === 'optimizing') {
      startPolling()
      cancelPostGenerationConfirmation()
      return
    }
    if (['reviewed', 'failed', 'finalized'].includes(status)) {
      cancelPostGenerationConfirmation()
      return
    }
    if (postGenerationConfirmAttempts >= POST_GENERATION_CONFIRM_ATTEMPTS) {
      cancelPostGenerationConfirmation()
      return
    }

    postGenerationConfirmAttempts += 1
    postGenerationConfirmTimer = window.setTimeout(() => {
      postGenerationConfirmTimer = null
      void confirmStatus()
    }, POST_GENERATION_CONFIRM_DELAY_MS)
  }

  postGenerationConfirmTimer = window.setTimeout(() => {
    postGenerationConfirmTimer = null
    void confirmStatus()
  }, POST_GENERATION_CONFIRM_DELAY_MS)
}

function isCurrentLiveStream(source: EventSource, id: number) {
  return source === liveStreamSource && id === liveStreamTaskId && id === taskId.value
}

function cancelLivePreviewFlush() {
  if (livePreviewFlushFrame != null) {
    cancelAnimationFrame(livePreviewFlushFrame)
    livePreviewFlushFrame = null
  }
  livePreviewDeltaBuffer = ''
  livePreviewBufferTaskId = null
}

function flushLivePreviewDeltas() {
  if (livePreviewFlushFrame != null) {
    cancelAnimationFrame(livePreviewFlushFrame)
    livePreviewFlushFrame = null
  }
  const delta = livePreviewDeltaBuffer
  const bufferedTaskId = livePreviewBufferTaskId
  livePreviewDeltaBuffer = ''
  livePreviewBufferTaskId = null
  if (
    delta &&
    bufferedTaskId === taskId.value &&
    bufferedTaskId === liveStreamTaskId
  ) {
    livePreviewText.value += delta
  }
}

function replaceLivePreview(text: string) {
  cancelLivePreviewFlush()
  livePreviewText.value = text
}

function clearLivePreview() {
  cancelLivePreviewFlush()
  livePreviewText.value = ''
}

function queueLivePreviewDelta(id: number, delta: string) {
  if (!delta || id !== taskId.value || id !== liveStreamTaskId) return
  if (livePreviewBufferTaskId !== id) {
    cancelLivePreviewFlush()
    livePreviewBufferTaskId = id
  }
  livePreviewDeltaBuffer += delta
  if (livePreviewFlushFrame == null) {
    livePreviewFlushFrame = requestAnimationFrame(() => {
      livePreviewFlushFrame = null
      flushLivePreviewDeltas()
    })
  }
}

function clearLiveStreamConnection(options?: {
  clearPreview?: boolean
  clearStage?: boolean
  resetReconnectAttempts?: boolean
}) {
  if (liveStreamSource) {
    liveStreamSource.close()
    liveStreamSource = null
  }
  if (liveStreamReconnectTimer != null) {
    clearTimeout(liveStreamReconnectTimer)
    liveStreamReconnectTimer = null
  }
  liveStreamTaskId = null
  liveStreamConnected.value = false
  liveStreamConnecting.value = false
  if (options?.resetReconnectAttempts !== false) {
    liveStreamReconnectAttempts = 0
  }
  if (options?.clearPreview) clearLivePreview()
  else cancelLivePreviewFlush()
  if (options?.clearStage) {
    liveStageMessage.value = ''
    liveStageStatus.value = ''
  }
}

function parseStreamPayload(event: Event): TaskStreamPayload | null {
  try {
    const payload = JSON.parse((event as MessageEvent<string>).data) as unknown
    return payload && typeof payload === 'object' ? payload as TaskStreamPayload : null
  } catch {
    return null
  }
}

function updateLiveStage(payload: TaskStreamPayload, fallback = '') {
  if (payload.status) liveStageStatus.value = payload.status
  if (payload.message) liveStageMessage.value = payload.message
  else if (fallback) liveStageMessage.value = fallback
}

async function handleLiveStreamCompleted(
  source: EventSource,
  id: number,
  payload: TaskStreamPayload,
) {
  if (!isCurrentLiveStream(source, id)) return
  if (typeof payload.text === 'string') replaceLivePreview(payload.text)
  else flushLivePreviewDeltas()
  updateLiveStage(payload, '生成完成，正在载入正式草稿…')
  clearLiveStreamConnection({ resetReconnectAttempts: false })
  const refreshed = await refreshLight()
  if (refreshed && id === taskId.value && !isLiveGeneration.value) {
    clearLivePreview()
    liveStageMessage.value = ''
    liveStageStatus.value = ''
  }
  if (id === taskId.value && task.value?.id === id) {
    startPostGenerationConfirmation(id)
  }
}

async function handleLiveStreamFailed(
  source: EventSource,
  id: number,
  payload: TaskStreamPayload,
) {
  if (!isCurrentLiveStream(source, id)) return
  cancelPostGenerationConfirmation()
  clearLivePreview()
  updateLiveStage(payload, '生成失败')
  liveStreamError.value = payload.message || task.value?.error_message || '生成失败，请稍后重试'
  clearLiveStreamConnection({ resetReconnectAttempts: false })
  await refreshLight()
}

function scheduleLiveStreamReconnect(id: number) {
  if (!isLiveGeneration.value || id !== taskId.value) return
  if (liveStreamReconnectAttempts >= LIVE_STREAM_MAX_RECONNECTS) {
    liveStageMessage.value = '实时预览连接已断开，仍会通过自动刷新更新任务状态'
    return
  }
  liveStreamReconnectAttempts += 1
  const attempt = liveStreamReconnectAttempts
  const delay = attempt * 1000
  liveStageMessage.value = `实时预览连接中断，正在重连（${attempt}/${LIVE_STREAM_MAX_RECONNECTS}）…`
  liveStreamReconnectTimer = window.setTimeout(() => {
    liveStreamReconnectTimer = null
    if (isLiveGeneration.value && id === taskId.value && !liveStreamSource) {
      openLiveStream(id)
    }
  }, delay)
}

function openLiveStream(id: number) {
  if (!isLiveGeneration.value || id !== taskId.value || liveStreamSource || liveStreamReconnectTimer != null) {
    return
  }
  if (liveStreamTaskId !== id) {
    liveStreamTaskId = id
    liveStreamReconnectAttempts = 0
  }
  liveStreamConnecting.value = true
  const source = new EventSource(taskStreamUrl(id))
  liveStreamSource = source

  source.onopen = () => {
    if (!isCurrentLiveStream(source, id)) return
    liveStreamConnecting.value = false
    liveStreamConnected.value = true
    if (!liveStageMessage.value) liveStageMessage.value = '已连接实时生成预览'
  }

  source.addEventListener('snapshot', (event) => {
    if (!isCurrentLiveStream(source, id)) return
    const payload = parseStreamPayload(event)
    if (!payload) return
    if (typeof payload.text === 'string') replaceLivePreview(payload.text)
    else flushLivePreviewDeltas()
    updateLiveStage(payload)
    if (payload.terminal === 'completed') {
      void handleLiveStreamCompleted(source, id, payload)
    } else if (payload.terminal === 'failed') {
      void handleLiveStreamFailed(source, id, payload)
    }
  })

  source.addEventListener('status', (event) => {
    if (!isCurrentLiveStream(source, id)) return
    const payload = parseStreamPayload(event)
    if (!payload) return
    liveStreamError.value = ''
    updateLiveStage(payload)
  })

  source.addEventListener('reset', (event) => {
    if (!isCurrentLiveStream(source, id)) return
    const payload = parseStreamPayload(event)
    if (!payload) return
    replaceLivePreview(typeof payload.text === 'string' ? payload.text : '')
    liveStreamError.value = ''
    updateLiveStage(payload)
  })

  source.addEventListener('delta', (event) => {
    if (!isCurrentLiveStream(source, id)) return
    const payload = parseStreamPayload(event)
    if (!payload) return
    if (typeof payload.delta === 'string') queueLivePreviewDelta(id, payload.delta)
  })

  source.addEventListener('retry', (event) => {
    if (!isCurrentLiveStream(source, id)) return
    const payload = parseStreamPayload(event)
    if (!payload) return
    updateLiveStage(payload, '模型请求正在重试…')
  })

  source.addEventListener('notice', (event) => {
    if (!isCurrentLiveStream(source, id)) return
    const payload = parseStreamPayload(event)
    if (!payload) return
    updateLiveStage(payload)
  })

  source.addEventListener('completed', (event) => {
    const payload = parseStreamPayload(event)
    if (payload) void handleLiveStreamCompleted(source, id, payload)
  })

  source.addEventListener('failed', (event) => {
    const payload = parseStreamPayload(event)
    if (payload) void handleLiveStreamFailed(source, id, payload)
  })

  source.onerror = () => {
    if (!isCurrentLiveStream(source, id)) return
    source.close()
    liveStreamSource = null
    liveStreamConnected.value = false
    liveStreamConnecting.value = false
    scheduleLiveStreamReconnect(id)
  }
}

function syncLiveStream() {
  if (!isLiveGeneration.value) {
    const isFailed = task.value?.status === 'failed'
    if (isFailed) {
      clearLivePreview()
      liveStreamError.value = task.value?.error_message || liveStreamError.value
    } else if (task.value && !shouldPollTaskStatus(task.value.status)) {
      clearLivePreview()
      liveStageMessage.value = ''
      liveStageStatus.value = ''
    }
    clearLiveStreamConnection({ resetReconnectAttempts: false })
    return
  }
  if (liveStreamTaskId !== taskId.value) {
    clearLiveStreamConnection({ clearPreview: true, clearStage: true })
    liveStreamError.value = ''
  }
  openLiveStream(taskId.value)
}

async function runAction(
  label: string,
  fn: (id: number) => Promise<TaskItem>,
) {
  if (!task.value) return
  acting.value = true
  try {
    ElMessage.info(`已提交${label}…`)
    const updated = await fn(task.value.id)
    applyTaskUpdate(updated)
    await refreshLight(true)
    // 后台任务：接口快速返回 in-progress，由轮询刷新结果，按钮不必一直转
    if (shouldPollTaskStatus(updated.status)) {
      ElMessage.success(`${label}已在后台执行，状态会自动刷新`)
    } else if (updated.status === 'failed') {
      ElMessage.error(`${label}失败：${updated.error_message || '未知错误'}`)
    } else {
      ElMessage.success(`${label}完成`)
    }
  } catch (e) {
    ElMessage.error(`${label}失败：${(e as Error).message}`)
    await refreshLight(true)
  } finally {
    acting.value = false
  }
}

function onGenerate() {
  return runAction('生成', generateTask)
}

function onReview() {
  return runAction('评审', reviewTask)
}

function onOptimize() {
  return runAction('优化 Prompt', optimizePromptTask)
}

async function onRegenerate() {
  try {
    await ElMessageBox.confirm('再生成将重新检索并覆盖当前生成流程，是否继续？', '再生成确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  return runAction('再生成', regenerateTask)
}

async function onFinalize() {
  if (!task.value) return
  try {
    await ElMessageBox.confirm('确认将当前草稿标记为终版？', '终版确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  const selectedDraftId = activeDraftTab.value ? Number(activeDraftTab.value) : null
  return runAction('终版', (id) => finalizeTask(id, selectedDraftId))
}

async function onRetryFailed() {
  if (!task.value) return
  const last = [...events.value].reverse().find((event) => event.step !== 'model_change')
  const step = last?.step || ''
  if (step.includes('review')) return onReview()
  if (step.includes('optimize')) return onOptimize()
  if (step.includes('regenerat')) return onRegenerate()
  return onGenerate()
}

async function onSwitchModelAndRetry() {
  if (!task.value || retryModelId.value == null) {
    ElMessage.warning('请先选择重试模型')
    return
  }
  acting.value = true
  try {
    const updated = await updateTaskModel(task.value.id, retryModelId.value)
    applyTaskUpdate(updated)
    ElMessage.success('模型已切换，正在重新生成')
  } catch (e) {
    ElMessage.error(`切换模型失败：${(e as Error).message}`)
    acting.value = false
    return
  }
  acting.value = false
  await onGenerate()
}

function openApplyDialog(rev?: PromptRevision) {
  selectedRevision.value = rev || pendingRevisions.value[0] || revisions.value[0] || null
  if (!selectedRevision.value) {
    ElMessage.warning('没有可应用的 Prompt 修订')
    return
  }
  applyMode.value = 'task_temp'
  alsoRegenerate.value = true
  editedPromptContent.value = selectedRevision.value.new_content
  applyDialogVisible.value = true
}

async function confirmApply() {
  if (!task.value || !selectedRevision.value) return
  if (!editedPromptContent.value.trim()) {
    ElMessage.warning('Prompt 内容不能为空')
    return
  }
  acting.value = true
  try {
    const applied = await applyPrompt(task.value.id, {
      revision_id: selectedRevision.value.id,
      mode: applyMode.value,
      content: editedPromptContent.value,
    })
    applyTaskUpdate(applied)
    ElMessage.success(
      applyMode.value === 'global' ? '已全局启用新 Prompt' : '已仅对本任务应用 Prompt',
    )
    applyDialogVisible.value = false
    if (alsoRegenerate.value) {
      const regenerated = await regenerateTask(task.value.id)
      applyTaskUpdate(regenerated)
      ElMessage.success('已触发再生成')
    }
    await refreshLight(true)
  } catch (e) {
    ElMessage.error(`应用失败：${(e as Error).message}`)
  } finally {
    acting.value = false
  }
}

watch(
  () => task.value?.status,
  () => {
    if (isBusy.value) startPolling()
    else stopPolling()
    if (shouldStopPostGenerationConfirmation(task.value?.status)) {
      cancelPostGenerationConfirmation()
    }
    syncLiveStream()
  },
  { immediate: true },
)

watch(taskId, () => {
  invalidatePendingDataReads()
  refreshInFlightSequence = null
  activeDraftTab.value = ''
  cancelPostGenerationConfirmation()
  clearLiveStreamConnection({ clearPreview: true, clearStage: true })
  liveStreamError.value = ''
  task.value = null
  drafts.value = []
  events.value = []
  reviews.value = []
  revisions.value = []
  citations.value = []
  void loadAll()
})

onMounted(async () => {
  await loadAll()
})

onUnmounted(() => {
  stopPolling()
  cancelPostGenerationConfirmation()
  clearLiveStreamConnection({ clearPreview: true })
})
</script>

<template>
  <div class="page detail-page" v-loading="loading">
    <div class="page-header">
      <div>
        <div class="back">
          <el-button link type="primary" @click="router.push('/tasks')">← 返回列表</el-button>
        </div>
        <h1 class="page-title">
          任务 #{{ task?.id ?? taskId }}
          <el-tag
            v-if="task"
            :type="statusTagType(task.status)"
            effect="light"
            style="margin-left: 8px; vertical-align: middle"
          >
            {{ statusLabel(task.status) }}
          </el-tag>
          <el-tag
            v-if="highlightFinal"
            type="success"
            effect="dark"
            style="margin-left: 8px; vertical-align: middle"
          >
            可终版
          </el-tag>
        </h1>
        <p class="title-line">{{ task?.title || '（无标题）' }}</p>
      </div>
      <div class="page-actions">
        <el-button @click="refreshLight()">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="showEmptyCitationBanner"
      type="warning"
      show-icon
      :closable="false"
      title="检索未命中任何 Wiki 页面"
      description="生成已完成但 citations 为空，用例可能缺少知识库依据。建议先编译相关文档，再重新生成。"
      style="margin-bottom: 16px"
    />

    <el-card v-if="task?.status === 'awaiting_confirmation' && checkpoint" shadow="never" class="block checkpoint-card">
      <template #header>检索结果确认</template>
      <p class="meta">请确认将用于生成的知识引用，可取消不相关条目。</p>
      <el-checkbox-group v-model="selectedCitationIds" class="checkpoint-list">
        <el-checkbox v-for="item in checkpoint.candidate_citations" :key="item.id" :label="item.id" class="checkpoint-item">
          <span class="checkpoint-title">{{ item.title }}</span>
          <span class="checkpoint-meta">{{ item.citation_type }} · score {{ item.score.toFixed(3) }} · {{ item.path }}</span>
          <span class="checkpoint-snippet">{{ item.snippet || item.content_excerpt }}</span>
        </el-checkbox>
      </el-checkbox-group>
      <el-input v-model="supplementalText" type="textarea" :rows="4" maxlength="10000" show-word-limit placeholder="补充上下文（可选）" />
      <el-button type="primary" :loading="confirmingCheckpoint" :disabled="!selectedCitationIds.length && !supplementalText.trim()" @click="confirmCheckpoint">确认并生成</el-button>
    </el-card>

    <el-card v-if="(task?.status === 'awaiting_test_point_confirmation' || task?.status === 'generating_test_points') && testPointCheckpoint" shadow="never" class="block checkpoint-card test-point-card">
      <template #header>
        <div class="card-head"><span>测试点确认</span><el-tag type="warning" size="small">先确认测试点，再生成完整用例</el-tag></div>
      </template>
      <p class="meta">可编辑标题、验证目标、维度、优先级、选择状态和引用；删除或排除不需要的测试点。</p>
      <div v-for="(point, index) in testPoints" :key="point.id" class="test-point-editor">
        <div class="test-point-editor-head"><strong>{{ point.stable_key }}</strong><el-button link type="danger" @click="removeTestPoint(index)">删除</el-button></div>
        <el-input v-model="point.title" placeholder="测试点标题" class="point-title-input" />
        <el-input v-model="point.verification_goal" type="textarea" :rows="2" placeholder="验证目标" />
        <div class="point-controls">
          <el-select v-model="point.dimension" style="width: 150px"><el-option v-for="dimension in (task?.test_dimensions || [])" :key="dimension" :label="dimension" :value="dimension" /></el-select>
          <el-select v-model="point.priority" style="width: 110px"><el-option label="P0" value="P0" /><el-option label="P1" value="P1" /><el-option label="P2" value="P2" /></el-select>
          <el-checkbox v-model="point.is_selected">选中</el-checkbox>
          <el-checkbox v-model="point.is_excluded">排除</el-checkbox>
        </div>
      </div>
      <div class="point-actions"><el-button @click="addTestPoint">新增测试点</el-button><el-button :loading="confirmingTestPoints" :disabled="task?.status !== 'awaiting_test_point_confirmation'" @click="saveTestPoints">保存编辑</el-button><el-button type="primary" :loading="confirmingTestPoints" :disabled="task?.status !== 'awaiting_test_point_confirmation'" @click="confirmTestPointStage">保存并确认测试点</el-button></div>
    </el-card>

    <el-alert
      v-if="task?.error_message || liveStreamError"
      type="error"
      show-icon
      :closable="false"
      :title="task?.error_message || liveStreamError"
      style="margin-bottom: 16px"
    />

    <div class="actions action-bar" v-if="task">
      <template v-if="task.status === 'draft'">
        <el-button type="primary" :loading="acting" @click="onGenerate">生成</el-button>
      </template>
      <template v-else-if="task.status === 'generated'">
        <el-button type="primary" :loading="acting" @click="onReview">评审</el-button>
        <el-button :loading="acting" @click="onFinalize">终版</el-button>
      </template>
      <template v-else-if="task.status === 'reviewed'">
        <el-button type="primary" :loading="acting" @click="onOptimize">优化 Prompt</el-button>
        <el-button :loading="acting" @click="onRegenerate">再生成</el-button>
        <el-button type="success" :loading="acting" @click="onFinalize">终版</el-button>
      </template>
      <template v-else-if="task.status === 'failed'">
        <el-button type="danger" :loading="acting" @click="onRetryFailed">重试当前步</el-button>
        <el-button :loading="acting" @click="onGenerate">重新生成</el-button>
        <el-select v-model="retryModelId" placeholder="选择重试模型" style="width: 260px">
          <el-option
            v-for="model in models"
            :key="model.id"
            :label="`${model.name} · ${model.model_name}${model.is_default ? '（默认）' : ''}`"
            :value="model.id"
          />
        </el-select>
        <el-button type="warning" :loading="acting" @click="onSwitchModelAndRetry">
          换模型并重新生成
        </el-button>
      </template>
      <template v-else-if="task.status === 'finalized'">
        <el-tag type="success" effect="dark">已终版（只读）</el-tag>
      </template>
      <template v-else-if="isBusy">
        <el-tag type="warning" effect="light">
          {{ liveStageStatus ? `${statusLabel(liveStageStatus)} · ` : '' }}{{ liveStageMessage || '处理中，每 2 秒自动刷新…' }}
        </el-tag>
      </template>

      <el-button
        v-if="pendingRevisions.length"
        type="warning"
        :loading="acting"
        @click="openApplyDialog()"
      >
        查看并应用 Prompt（{{ pendingRevisions.length }}）
      </el-button>
    </div>

    <el-row :gutter="16" class="main-grid">
      <el-col :xs="24" :lg="14">
        <el-card shadow="never" class="block">
          <template #header>
            <div class="card-head">
              <span>需求</span>
              <span class="meta">更新于 {{ formatTime(task?.updated_at || '') }}</span>
            </div>
          </template>
          <div v-if="task" class="space-banner">
            <span class="meta">Wiki 空间</span>
            <el-tag type="info" effect="plain">{{ task.wiki_space_name || `空间 #${task.wiki_space_id}` }}</el-tag>
          </div>
          <div class="req-desc">{{ task?.description || '-' }}</div>
          <div v-if="task?.focus_tags?.length" class="tags">
            <el-tag
              v-for="tag in task.focus_tags"
              :key="tag"
              size="small"
              style="margin-right: 6px"
            >
              {{ tag }}
            </el-tag>
          </div>
        </el-card>

        <el-card v-if="showLivePreview" shadow="never" class="block live-preview-card">
          <template #header>
            <div class="card-head">
              <span>实时生成预览（未完成）</span>
              <el-tag type="warning" size="small" effect="plain">实时输出</el-tag>
            </div>
          </template>
          <div v-if="liveStageMessage" class="live-preview-status">
            {{ liveStageStatus ? `${statusLabel(liveStageStatus)} · ` : '' }}{{ liveStageMessage }}
          </div>
          <MarkdownView v-if="livePreviewText" :content="livePreviewText" />
          <el-empty v-else description="正在等待模型输出…" :image-size="56" />
        </el-card>

        <el-card shadow="never" class="block">
          <template #header>草稿</template>
          <el-tabs v-if="drafts.length" v-model="activeDraftTab">
            <el-tab-pane
              v-for="d in drafts"
              :key="d.id"
              :label="`v${d.version}`"
              :name="String(d.id)"
            >
              <div class="draft-meta">
                {{ formatTime(d.created_at) }}
                <span v-if="d.prompt_version_ref"> · {{ d.prompt_version_ref }}</span>
                <el-tag
                  v-if="task?.finalized_draft_id === d.id"
                  type="success"
                  size="small"
                  effect="plain"
                  style="margin-left: 8px"
                >
                  已定稿并导入
                </el-tag>
              </div>
              <div class="draft-scroll">
                <MarkdownView :content="d.content_md" />
              </div>
            </el-tab-pane>
          </el-tabs>
          <el-empty v-else description="暂无草稿" :image-size="64" />
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="10">
        <el-card shadow="never" class="block">
          <template #header>时间线</template>
          <TaskTimeline :events="events" />
        </el-card>

        <el-card v-if="coverage" shadow="never" class="block">
          <template #header>覆盖概览</template>
          <el-progress :percentage="coverage.coverage_percent" :format="formatCoverage" />
          <div class="coverage-stats">已覆盖 {{ coverage.covered_test_points }} / {{ coverage.selected_test_points }} 个选中测试点；未覆盖 {{ coverage.uncovered_test_points }} 个</div>
          <el-collapse>
            <el-collapse-item title="测试点明细" name="points">
              <div v-for="point in coverage.points" :key="point.stable_key" class="coverage-row"><el-tag size="small" :type="point.covered ? 'success' : 'warning'">{{ point.covered ? '已覆盖' : '未覆盖' }}</el-tag><span>{{ point.stable_key }} · {{ point.title }}</span><small>{{ point.priority }} · {{ point.dimension }}</small></div>
            </el-collapse-item>
            <el-collapse-item title="引用使用明细" name="citations">
              <div v-for="citation in coverage.citations" :key="citation.citation_id" class="coverage-row"><el-tag size="small" :type="citation.used ? 'success' : 'info'">{{ citation.used ? '已使用' : '未使用' }}</el-tag><span>[{{ citation.citation_id }}] {{ citation.title }}</span><small>{{ citation.test_point_keys.join('、') || '无测试点' }}</small></div>
            </el-collapse-item>
          </el-collapse>
        </el-card>

        <el-card shadow="never" class="block">
          <CitationList
            :citations="citations"
            :count="citations.length || task?.citation_count || 0"
            :space-id="task?.wiki_space_id"
          />
        </el-card>

        <el-card shadow="never" class="block">
          <ReviewCard :review="latestReview" />
        </el-card>

        <el-card v-if="revisions.length" shadow="never" class="block">
          <template #header>Prompt 修订</template>
          <div
            v-for="rev in revisions"
            :key="rev.id"
            class="rev-item"
          >
            <div class="rev-head">
              <span>#{{ rev.id }} · {{ rev.status }}</span>
              <el-button
                v-if="rev.status === 'pending' && task?.status !== 'finalized'"
                link
                type="primary"
                @click="openApplyDialog(rev)"
              >
                应用
              </el-button>
            </div>
            <div class="rev-preview">{{ rev.new_content.slice(0, 180) }}{{ rev.new_content.length > 180 ? '…' : '' }}</div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-dialog
      v-model="applyDialogVisible"
      title="应用优化后的 Prompt"
      width="640px"
      destroy-on-close
    >
      <div v-if="selectedRevision" class="apply-body">
        <div class="apply-meta">修订 #{{ selectedRevision.id }} · {{ selectedRevision.status }}</div>
        <el-input
          v-model="editedPromptContent"
          type="textarea"
          :rows="12"
        />
        <div class="apply-hint">可在确认前手动修改；最终保存的是此处内容。</div>
        <div class="apply-mode">
          <div class="label">应用方式</div>
          <el-radio-group v-model="applyMode">
            <el-radio value="task_temp">仅本任务</el-radio>
            <el-radio value="global">全局启用</el-radio>
          </el-radio-group>
        </div>
        <el-checkbox v-model="alsoRegenerate">应用后立即再生成</el-checkbox>
      </div>
      <template #footer>
        <el-button @click="applyDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="confirmApply">确认应用</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.back {
  margin-bottom: 4px;
}

.title-line {
  margin: 6px 0 0;
  color: var(--cg-text-secondary);
  font-size: 14px;
}

.action-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 18px;
  padding: 12px 14px;
  border-radius: var(--cg-radius);
  border: 1px solid var(--cg-border);
  background: linear-gradient(
    135deg,
    rgba(var(--cg-primary-rgb), 0.06),
    rgba(var(--cg-primary-2-rgb), 0.05)
  );
}

.main-grid {
  margin-top: 4px;
}

.block {
  margin-bottom: 16px;
  border-radius: var(--cg-radius) !important;
}

.draft-scroll {
  min-height: 0;
  max-height: clamp(320px, calc(100vh - 360px), 760px);
  overflow-y: auto;
}

@media (max-width: 900px) {
  .draft-scroll {
    max-height: none;
    overflow: visible;
  }
}

.checkpoint-list {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 10px;
  max-height: clamp(260px, calc(100vh - 420px), 540px);
  overflow-y: auto;
  overflow-x: hidden;
  margin: 12px 0;
  min-width: 0;
}

.checkpoint-item {
  display: flex;
  align-items: flex-start;
  width: 100%;
  min-width: 0;
  min-height: 32px;
  height: auto;
  margin-right: 0;
  box-sizing: border-box;
  white-space: normal;
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius-sm);
  padding: 10px;
}

.checkpoint-item :deep(.el-checkbox__input) {
  flex: 0 0 auto;
  margin-top: 2px;
}

.checkpoint-item :deep(.el-checkbox__label) {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-width: 0;
  max-width: 100%;
  line-height: 1.45;
  white-space: normal;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.checkpoint-title,
.checkpoint-meta,
.checkpoint-snippet {
  display: block;
  max-width: 100%;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.checkpoint-title { font-weight: 700; }
.checkpoint-meta { color: var(--cg-text-muted); font-size: 12px; }
.checkpoint-snippet { color: var(--cg-text-secondary); font-size: 12px; margin-top: 4px; }

@media (max-width: 900px) {
  .checkpoint-list { max-height: none; overflow: visible; }
}

.block :deep(.el-card__header) {
  font-weight: 700;
  border-bottom-color: var(--cg-border);
}

.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.meta,
.draft-meta,
.apply-meta {
  color: var(--cg-text-muted);
  font-size: 12px;
}

.req-desc {
  white-space: pre-wrap;
  line-height: 1.6;
  color: var(--cg-text);
}

.space-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--cg-border);
}

.live-preview-card {
  border-color: rgba(230, 162, 60, 0.45) !important;
}

.live-preview-status {
  margin-bottom: 14px;
  padding: 8px 10px;
  border-radius: var(--cg-radius-sm);
  color: var(--cg-text-secondary);
  font-size: 13px;
  background: var(--cg-surface-muted);
}

.tags {
  margin-top: 10px;
}

.rev-item {
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius-sm);
  padding: 8px 10px;
  margin-bottom: 8px;
  background: var(--cg-surface-muted);
}

.rev-head {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  font-weight: 600;
}

.rev-preview {
  margin-top: 6px;
  font-size: 12px;
  color: var(--cg-text-secondary);
  white-space: pre-wrap;
}

.apply-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.apply-mode .label {
  margin-bottom: 6px;
  font-weight: 600;
}
</style>
