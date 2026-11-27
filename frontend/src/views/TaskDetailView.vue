<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useProjectStore } from '../projectStore'
import { getSession } from '../api/auth'
import { listModels, type ModelConfig } from '../api/models'
import {
  getRetrievalCheckpoint, getTask, getTaskCoverage, getTestPointCheckpoint,
  listCitations, listDrafts, listEvents, listReviews, listRevisions,
  shouldPollTaskStatus, statusLabel, statusTagType, taskStreamUrl,
  type CaseDraft, type CoverageSummary, type PromptRevision, type RetrievalCheckpoint,
  type ReviewResult, type TaskCitation, type TaskEvent, type TaskItem, type TestPointCheckpoint,
} from '../api/tasks'
import { useSseStream, type SseStreamPayload } from '../composables/useSseStream'
import { useDraftDepthSync } from '../composables/useDraftDepthSync'
import { useTaskActions } from '../composables/useTaskActions'
import { formatDateTime as formatTime } from '../utils/datetime'
import TaskRetrievalConfirmCard from '../components/task/TaskRetrievalConfirmCard.vue'
import TaskTestPointsEditorCard from '../components/task/TaskTestPointsEditorCard.vue'
import TaskDraftViewerCard from '../components/task/TaskDraftViewerCard.vue'
import TaskSidebarPanel from '../components/task/TaskSidebarPanel.vue'
import TaskActionDialogs from '../components/task/TaskActionDialogs.vue'

const route = useRoute()
const router = useRouter()
const projects = useProjectStore()

const LIVE_STREAM_STATUSES = new Set(['retrieving', 'generating_test_points', 'generating', 'regenerating'])
const POST_GEN_CONFIRM_DELAY_MS = 400
const POST_GEN_CONFIRM_MAX_ATTEMPTS = 10

const loading = ref(false)
const task = ref<TaskItem | null>(null)
const drafts = ref<CaseDraft[]>([])
const events = ref<TaskEvent[]>([])
const reviews = ref<ReviewResult[]>([])
const revisions = ref<PromptRevision[]>([])
const citations = ref<TaskCitation[]>([])
const checkpoint = ref<RetrievalCheckpoint | null>(null)
const testPointCheckpoint = ref<TestPointCheckpoint | null>(null)
const coverage = ref<CoverageSummary | null>(null)
const models = ref<ModelConfig[]>([])
const activeDraftTab = ref('')
const draftViewMode = ref<'structured' | 'markdown'>('structured')

const livePreviewText = ref('')
const liveStageMessage = ref('')
const liveStageStatus = ref('')
const liveStreamError = ref('')

let pollTimer: number | null = null
let liveStreamTaskId: number | null = null
let streamAuthFailed = false
let postGenTimer: number | null = null
let postGenTaskId: number | null = null
let postGenAttempts = 0
let postGenEpoch = 0
let dataRequestSequence = 0
let latestAppliedDataRequest = 0
let refreshInFlightSequence: number | null = null
let loadSequence = 0
let isUnmounted = false

const taskId = computed(() => Number(route.params.id))

const projectId = computed(() => {
  const fromQuery = Number(route.query.project_id)
  if (Number.isInteger(fromQuery) && fromQuery > 0) return fromQuery
  if (projects.state.currentId) return projects.state.currentId
  const stored = Number(localStorage.getItem('casegen:last-project-id'))
  return Number.isInteger(stored) && stored > 0 ? stored : null
})

const isBusy = computed(() => shouldPollTaskStatus(task.value?.status))
const isLiveGeneration = computed(() =>
  Boolean(task.value && task.value.id === taskId.value && LIVE_STREAM_STATUSES.has(task.value.status)),
)
const showEmptyCitationBanner = computed(() =>
  Boolean(
    task.value
    && citations.value.length === 0
    && ['generated', 'reviewed', 'finalized', 'regenerating'].includes(task.value.status),
  ),
)
const pendingRevisions = computed(() => revisions.value.filter((r) => r.status === 'pending'))
const latestReview = computed(() => task.value?.latest_review || reviews.value[0] || null)
const highlightFinal = computed(() => {
  if (!task.value || task.value.status === 'finalized') return false
  const review = latestReview.value
  return Boolean(review && (review.payload?.ready_for_final || review.score >= 80))
})

const { draftScrollStyle, scheduleSyncDraftDepth } = useDraftDepthSync([
  draftViewMode,
  () => activeDraftTab.value,
  () => drafts.value.length,
])

function nextDataRequest(): number {
  dataRequestSequence += 1
  return dataRequestSequence
}

function canApplyDataRequest(id: number, sequence: number): boolean {
  if (isUnmounted) return false
  return id === taskId.value && sequence >= latestAppliedDataRequest
}

function markDataRequestApplied(sequence: number) {
  latestAppliedDataRequest = sequence
}

function invalidatePendingDataReads() {
  markDataRequestApplied(nextDataRequest())
}

function applyTaskUpdate(updated: TaskItem): boolean {
  if (isUnmounted || updated.id !== taskId.value) return false
  invalidatePendingDataReads()
  task.value = updated
  return true
}

function applyTestPointCheckpointUpdate(saved: TestPointCheckpoint): boolean {
  if (isUnmounted || saved.task_id !== taskId.value) return false
  invalidatePendingDataReads()
  testPointCheckpoint.value = saved
  return true
}

async function fetchTaskBundle(id: number) {
  const [t, d, e, rev, revs, c] = await Promise.all([
    getTask(id, projectId.value),
    listDrafts(id, projectId.value),
    listEvents(id, projectId.value),
    listReviews(id, projectId.value),
    listRevisions(id, projectId.value),
    listCitations(id, projectId.value),
  ])
  const retrievalFresh = t.status === 'awaiting_confirmation'
    ? await getRetrievalCheckpoint(id, projectId.value)
    : null
  const testPointFresh = t.status === 'awaiting_test_point_confirmation'
    ? await getTestPointCheckpoint(id, projectId.value).catch(() => null)
    : null
  const coverageFresh = await getTaskCoverage(id, projectId.value).catch(() => null)
  return { t, d, e, rev, revs, c, retrievalFresh, testPointFresh, coverageFresh }
}

function applyBundle(bundle: Awaited<ReturnType<typeof fetchTaskBundle>>, keepActiveIfPresent: boolean) {
  task.value = bundle.t
  drafts.value = bundle.d
  events.value = bundle.e
  reviews.value = bundle.rev
  revisions.value = bundle.revs
  citations.value = bundle.c
  checkpoint.value = bundle.t.status === 'awaiting_confirmation' ? bundle.retrievalFresh : null
  testPointCheckpoint.value = bundle.t.status === 'awaiting_test_point_confirmation' ? bundle.testPointFresh : null
  coverage.value = bundle.coverageFresh
  if (!bundle.d.length) return
  if (!keepActiveIfPresent) {
    if (!activeDraftTab.value) activeDraftTab.value = String(bundle.d[0].id)
  } else if (!bundle.d.some((x) => String(x.id) === activeDraftTab.value)) {
    activeDraftTab.value = String(bundle.d[0].id)
  }
}

async function loadAll() {
  if (isUnmounted) return
  const id = taskId.value
  if (!id || Number.isNaN(id)) return
  const requestSequence = nextDataRequest()
  const currentLoadSequence = ++loadSequence
  loading.value = true
  try {
    const [bundle, modelRows] = await Promise.all([
      fetchTaskBundle(id),
      listModels().catch(() => [] as ModelConfig[]),
    ])
    if (!canApplyDataRequest(id, requestSequence)) return
    markDataRequestApplied(requestSequence)
    applyBundle(bundle, false)
    models.value = modelRows
    nextTick(scheduleSyncDraftDepth)
  } catch (err) {
    if (canApplyDataRequest(id, requestSequence)) {
      ElMessage.error(`加载任务失败：${(err as Error).message}`)
    }
  } finally {
    if (!isUnmounted && id === taskId.value && currentLoadSequence === loadSequence) loading.value = false
  }
}

async function refreshLight(force = false): Promise<boolean> {
  if (isUnmounted) return false
  const id = taskId.value
  if (!id || Number.isNaN(id)) return false
  if (refreshInFlightSequence !== null && !force) return false
  const requestSequence = nextDataRequest()
  refreshInFlightSequence = requestSequence
  try {
    const bundle = await fetchTaskBundle(id)
    if (!canApplyDataRequest(id, requestSequence)) return false
    markDataRequestApplied(requestSequence)
    applyBundle(bundle, true)
    return true
  } catch {
    return false
  } finally {
    if (refreshInFlightSequence === requestSequence) refreshInFlightSequence = null
  }
}

const {
  acting,
  confirmingCheckpoint,
  confirmingTestPoints,
  applyDialogVisible,
  selectedRevision,
  retryModelDialogVisible,
  finalizeConflictDialogVisible,
  finalizeConflicts,
  pendingFinalizePendingCount,
  resetDialogState,
  confirmCheckpoint,
  saveTestPoints,
  confirmTestPointStage,
  onGenerate,
  onReview,
  onOptimize,
  onStopGeneration,
  onRegenerate,
  onFinalize,
  confirmFinalizeOverwrite,
  onRetryFailed,
  onSwitchModelAndRetry,
  openApplyDialog,
  confirmApply,
} = useTaskActions({
  taskId,
  projectId,
  task,
  drafts,
  events,
  revisions,
  pendingRevisions,
  checkpoint,
  testPointCheckpoint,
  activeDraftTab,
  applyTaskUpdate,
  applyTestPointCheckpointUpdate,
  invalidatePendingDataReads,
  refreshLight,
})

function startPolling() {
  stopPolling()
  if (isUnmounted) return
  pollTimer = window.setInterval(() => {
    if (isUnmounted) {
      stopPolling()
      return
    }
    if (shouldPollTaskStatus(task.value?.status)) void refreshLight()
  }, 2000)
}

function stopPolling() {
  if (pollTimer != null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function cancelPostGenerationConfirmation() {
  postGenEpoch += 1
  if (postGenTimer != null) {
    clearTimeout(postGenTimer)
    postGenTimer = null
  }
  postGenTaskId = null
  postGenAttempts = 0
}

function shouldStopPostGen(status?: string) {
  return [
    'reviewing',
    'optimizing',
    'reviewed',
    'failed',
    'finalized',
    'awaiting_confirmation',
    'awaiting_test_point_confirmation',
  ].includes(status || '')
}

function startPostGenerationConfirmation(id: number) {
  cancelPostGenerationConfirmation()
  if (isUnmounted || id !== taskId.value || task.value?.id !== id || shouldStopPostGen(task.value.status)) return
  const epoch = ++postGenEpoch
  postGenTaskId = id
  const isStale = () =>
    isUnmounted || epoch !== postGenEpoch || postGenTaskId !== id || id !== taskId.value || task.value?.id !== id

  const confirmStatus = async () => {
    if (isStale()) return
    await refreshLight()
    if (isStale()) return
    const status = task.value!.status
    if (status === 'reviewing' || status === 'optimizing') {
      startPolling()
      cancelPostGenerationConfirmation()
      return
    }
    if (shouldStopPostGen(status) || postGenAttempts >= POST_GEN_CONFIRM_MAX_ATTEMPTS) {
      cancelPostGenerationConfirmation()
      return
    }
    postGenAttempts += 1
    postGenTimer = window.setTimeout(() => {
      postGenTimer = null
      void confirmStatus()
    }, POST_GEN_CONFIRM_DELAY_MS)
  }

  postGenTimer = window.setTimeout(() => {
    postGenTimer = null
    void confirmStatus()
  }, POST_GEN_CONFIRM_DELAY_MS)
}

function updateLiveStage(payload: SseStreamPayload, fallback = '') {
  if (payload.status) liveStageStatus.value = payload.status
  if (payload.message) liveStageMessage.value = payload.message
  else if (fallback) liveStageMessage.value = fallback
}

async function handleLiveStreamCompleted(id: number, payload: SseStreamPayload) {
  if (isUnmounted || id !== taskId.value) return
  if (typeof payload.text === 'string') livePreviewText.value = payload.text
  updateLiveStage(payload, '生成完成，正在载入正式草稿…')
  const refreshed = await refreshLight(true)
  if (isUnmounted) return
  if (refreshed && id === taskId.value && !isLiveGeneration.value) {
    livePreviewText.value = ''
    liveStageMessage.value = ''
    liveStageStatus.value = ''
  }
  if (id === taskId.value && task.value?.id === id) startPostGenerationConfirmation(id)
}

async function handleLiveStreamFailed(id: number, payload: SseStreamPayload) {
  if (isUnmounted || id !== taskId.value) return
  cancelPostGenerationConfirmation()
  livePreviewText.value = ''
  updateLiveStage(payload, '生成失败')
  liveStreamError.value = payload.message || task.value?.error_message || '生成失败，请稍后重试'
  await refreshLight(true)
}

const liveStream = useSseStream({
  closeOnUnmount: true,
  url: () => {
    const id = taskId.value
    const streamProjectId = task.value?.project_id ?? projectId.value
    if (!id || Number.isNaN(id) || !streamProjectId) return null
    return taskStreamUrl(id, streamProjectId)
  },
  isActive: () => !isUnmounted && isLiveGeneration.value && liveStreamTaskId === taskId.value,
  onOpen: () => {
    streamAuthFailed = false
    if (!liveStageMessage.value) liveStageMessage.value = '已连接实时生成预览'
  },
  onSnapshot: (payload) => {
    if (typeof payload.text === 'string') livePreviewText.value = payload.text
    updateLiveStage(payload)
  },
  onStatus: (payload) => {
    liveStreamError.value = ''
    updateLiveStage(payload)
  },
  onReset: (payload) => {
    livePreviewText.value = typeof payload.text === 'string' ? payload.text : ''
    liveStreamError.value = ''
    updateLiveStage(payload)
  },
  onDelta: (chunk) => { livePreviewText.value += chunk },
  onRetry: (payload) => updateLiveStage(payload, '模型请求正在重试…'),
  onNotice: (payload) => updateLiveStage(payload),
  onCompleted: (payload) => { void handleLiveStreamCompleted(taskId.value, payload) },
  onFailed: (payload) => { void handleLiveStreamFailed(taskId.value, payload) },
  onReconnecting: (attempt, max) => {
    liveStageMessage.value = `实时预览连接中断，正在重连（${attempt}/${max}）…`
  },
  onReconnectExhausted: () => {
    if (isUnmounted) return
    liveStageMessage.value = '实时预览连接已断开，仍会通过自动刷新更新任务状态'
    if (isBusy.value) startPolling()
  },
  beforeReconnect: async () => {
    const id = taskId.value
    if (isUnmounted || !id || Number.isNaN(id)) return false
    try {
      await getSession()
    } catch {
      if (isUnmounted) return false
      streamAuthFailed = true
      liveStreamError.value = '登录状态已失效，请重新登录后查看实时生成进度'
      liveStageMessage.value = ''
      return false
    }
    if (isUnmounted || id !== taskId.value) return false
    try {
      const fresh = await getTask(id, task.value?.project_id ?? projectId.value)
      if (isUnmounted || id !== taskId.value) return false
      if (!LIVE_STREAM_STATUSES.has(fresh.status)) {
        applyTaskUpdate(fresh)
        void refreshLight(true)
        return false
      }
      return true
    } catch {
      return !isUnmounted
    }
  },
})

const showLivePreview = computed(() =>
  Boolean(
    livePreviewText.value
    || liveStream.connected.value
    || liveStream.connecting.value
    || liveStream.reconnecting.value,
  ),
)

function syncLiveStream() {
  if (isUnmounted) return
  if (!isLiveGeneration.value) {
    if (task.value?.status === 'failed') {
      livePreviewText.value = ''
      liveStreamError.value = task.value?.error_message || liveStreamError.value
    } else if (task.value && !shouldPollTaskStatus(task.value.status)) {
      livePreviewText.value = ''
      liveStageMessage.value = ''
      liveStageStatus.value = ''
    }
    liveStream.close('manual')
    liveStreamTaskId = null
    return
  }
  if (streamAuthFailed) return
  if (liveStreamTaskId !== taskId.value) {
    liveStream.close('manual')
    livePreviewText.value = ''
    liveStageMessage.value = ''
    liveStageStatus.value = ''
    liveStreamError.value = ''
    liveStreamTaskId = taskId.value
    liveStream.open()
    return
  }
  if (!liveStream.connected.value && !liveStream.connecting.value && !liveStream.reconnecting.value) {
    liveStream.open()
  }
}

watch(
  () => task.value?.status,
  () => {
    if (isUnmounted) return
    if (isBusy.value) startPolling()
    else stopPolling()
    if (shouldStopPostGen(task.value?.status)) cancelPostGenerationConfirmation()
    syncLiveStream()
  },
  { immediate: true },
)

watch(taskId, () => {
  if (isUnmounted) return
  invalidatePendingDataReads()
  refreshInFlightSequence = null
  activeDraftTab.value = ''
  streamAuthFailed = false
  cancelPostGenerationConfirmation()
  liveStream.close('manual')
  liveStreamTaskId = null
  livePreviewText.value = ''
  liveStageMessage.value = ''
  liveStageStatus.value = ''
  liveStreamError.value = ''
  resetDialogState()
  task.value = null
  drafts.value = []
  events.value = []
  reviews.value = []
  revisions.value = []
  citations.value = []
  checkpoint.value = null
  testPointCheckpoint.value = null
  coverage.value = null
  void loadAll()
})

onMounted(() => { void loadAll() })

onUnmounted(() => {
  isUnmounted = true
  stopPolling()
  cancelPostGenerationConfirmation()
  liveStream.close('unmounted')
})
</script>

<template>
  <div class="page detail-page" v-loading="loading">
    <div class="page-header">
      <div>
        <div class="back">
          <el-button link type="primary" @click="router.push({ path: '/tasks', query: { ...route.query } })">
            ← 返回场景生成
          </el-button>
        </div>
        <h1 class="page-title">
          <span class="task-spec-id">TASK // #{{ task?.id ?? taskId }}</span>
          <el-tag v-if="task" :type="statusTagType(task.status)" effect="light" style="margin-left: 8px; vertical-align: middle">
            {{ statusLabel(task.status) }}
          </el-tag>
          <el-tag v-if="highlightFinal" type="success" effect="dark" style="margin-left: 8px; vertical-align: middle">
            可终版
          </el-tag>
        </h1>
        <p class="title-line">{{ task?.title || '（无标题）' }}</p>
      </div>
      <div class="page-actions">
        <el-button v-if="task?.status === 'generating'" type="danger" plain :loading="acting" @click="onStopGeneration">
          停止生成
        </el-button>
        <el-button @click="refreshLight()">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="showEmptyCitationBanner"
      type="info"
      show-icon
      :closable="false"
      title="未关联外部知识引用"
      description="当前任务未命中外部知识库条目，测试场景基于需求描述与补充上下文生成。如需知识依据，可联系管理员配置外部知识库或补充需求背景后再重新生成。"
      style="margin-bottom: 16px"
    />

    <TaskRetrievalConfirmCard
      v-if="task?.status === 'awaiting_confirmation' && checkpoint"
      :checkpoint="checkpoint"
      :confirming="confirmingCheckpoint"
      @confirm="confirmCheckpoint"
    />

    <TaskTestPointsEditorCard
      v-if="(task?.status === 'awaiting_test_point_confirmation' || task?.status === 'generating_test_points') && testPointCheckpoint"
      :checkpoint="testPointCheckpoint"
      :task-id="taskId"
      :dimensions="task?.test_dimensions || []"
      :saving="confirmingTestPoints"
      :editable="task?.status === 'awaiting_test_point_confirmation'"
      @save="saveTestPoints"
      @confirm="confirmTestPointStage"
    />

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
        <el-button type="warning" :loading="acting" @click="retryModelDialogVisible = true">换模型并重新生成</el-button>
      </template>
      <template v-else-if="task.status === 'finalized'">
        <el-tag type="success" effect="dark">已终版（只读）</el-tag>
      </template>
      <template v-else-if="isBusy">
        <el-tag type="warning" effect="light">
          {{ liveStageStatus ? `${statusLabel(liveStageStatus)} · ` : '' }}{{ liveStageMessage || '处理中，每 2 秒自动刷新…' }}
        </el-tag>
      </template>

      <el-button v-if="pendingRevisions.length" type="warning" :loading="acting" @click="openApplyDialog()">
        查看并应用 Prompt（{{ pendingRevisions.length }}）
      </el-button>
    </div>

    <el-row :gutter="16" class="main-grid">
      <el-col :xs="24" :md="14" :lg="14" class="left-main-col">
        <el-card shadow="never" class="block">
          <template #header>
            <div class="card-head">
              <span>需求</span>
              <span class="meta">
                提交用户：{{ task?.created_by_display_name || task?.created_by_username || '-' }} · 更新于 {{ formatTime(task?.updated_at || '') }}
              </span>
            </div>
          </template>
          <div class="req-desc">{{ task?.description || '-' }}</div>
          <div v-if="task?.focus_tags?.length" class="tags">
            <el-tag v-for="tag in task.focus_tags" :key="tag" size="small" style="margin-right: 6px">{{ tag }}</el-tag>
          </div>
        </el-card>

        <TaskDraftViewerCard
          :drafts="drafts"
          v-model:active-draft-tab="activeDraftTab"
          v-model:draft-view-mode="draftViewMode"
          :finalized-draft-id="task?.finalized_draft_id"
          :citations="citations"
          :draft-scroll-style="draftScrollStyle"
          :show-live-preview="showLivePreview"
          :live-preview-text="livePreviewText"
          :live-stage-message="liveStageMessage"
          :live-stage-status="liveStageStatus"
        />
      </el-col>

      <el-col :xs="24" :md="10" :lg="10" class="right-main-col">
        <TaskSidebarPanel
          :events="events"
          :coverage="coverage"
          :citations="citations"
          :citation-count="citations.length || task?.citation_count || 0"
          :project-id="task?.project_id ?? projectId"
          :latest-review="latestReview"
          :revisions="revisions"
          :can-apply-revision="task?.status !== 'finalized'"
          @apply-revision="openApplyDialog"
        />
      </el-col>
    </el-row>

    <TaskActionDialogs
      v-model:apply-visible="applyDialogVisible"
      :selected-revision="selectedRevision"
      v-model:retry-model-visible="retryModelDialogVisible"
      :models="models"
      :default-model-id="task?.model_id ?? null"
      :task-error-message="task?.error_message"
      v-model:finalize-conflict-visible="finalizeConflictDialogVisible"
      :finalize-conflicts="finalizeConflicts"
      :pending-items-case-count="pendingFinalizePendingCount"
      :acting="acting"
      @confirm-apply="confirmApply"
      @confirm-retry-model="onSwitchModelAndRetry"
      @confirm-finalize-overwrite="confirmFinalizeOverwrite"
    />
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

.task-spec-id {
  font-family: var(--cg-font-mono);
  letter-spacing: -0.02em;
}

.action-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 18px;
  padding: 12px 14px;
  border-radius: var(--cg-radius);
  border: 1px solid var(--cg-border);
  background: var(--cg-surface-muted);
}

.main-grid {
  margin-top: 4px;
}

.block {
  margin-bottom: 16px;
  border-radius: var(--cg-radius) !important;
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

.meta {
  color: var(--cg-text-muted);
  font-size: 12px;
}

.req-desc {
  white-space: pre-wrap;
  line-height: 1.6;
  color: var(--cg-text);
}

.tags {
  margin-top: 10px;
}
</style>
