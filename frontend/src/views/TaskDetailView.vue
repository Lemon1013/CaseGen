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
  IN_PROGRESS_STATUSES,
  listCitations,
  listDrafts,
  listEvents,
  listReviews,
  listRevisions,
  optimizePromptTask,
  regenerateTask,
  reviewTask,
  statusLabel,
  statusTagType,
  type ApplyPromptMode,
  type CaseDraft,
  type PromptRevision,
  type ReviewResult,
  type TaskCitation,
  type TaskEvent,
  type TaskItem,
  updateTaskModel,
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
const models = ref<ModelConfig[]>([])
const activeDraftTab = ref('')

const applyDialogVisible = ref(false)
const applyMode = ref<ApplyPromptMode>('task_temp')
const selectedRevision = ref<PromptRevision | null>(null)
const alsoRegenerate = ref(true)
const editedPromptContent = ref('')
const retryModelId = ref<number | null>(null)

let pollTimer: number | null = null

const taskId = computed(() => Number(route.params.id))

const isBusy = computed(() =>
  task.value ? IN_PROGRESS_STATUSES.has(task.value.status) : false,
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
  const r = latestReview.value
  if (!r) return false
  return Boolean(r.payload?.ready_for_final) || r.score >= 80
})

async function loadAll() {
  if (!taskId.value || Number.isNaN(taskId.value)) return
  loading.value = true
  try {
    const [t, d, e, rev, revs, c, modelRows] = await Promise.all([
      getTask(taskId.value),
      listDrafts(taskId.value),
      listEvents(taskId.value),
      listReviews(taskId.value),
      listRevisions(taskId.value),
      listCitations(taskId.value),
      listModels().catch(() => [] as ModelConfig[]),
    ])
    task.value = t
    drafts.value = d
    events.value = e
    reviews.value = rev
    revisions.value = revs
    citations.value = c
    models.value = modelRows
    retryModelId.value =
      t.model_id ?? modelRows.find((model) => model.is_default)?.id ?? modelRows[0]?.id ?? null
    if (!activeDraftTab.value && d.length) {
      activeDraftTab.value = String(d[0].id)
    }
  } catch (err) {
    ElMessage.error(`加载任务失败：${(err as Error).message}`)
  } finally {
    loading.value = false
  }
}

async function refreshLight() {
  if (!taskId.value || Number.isNaN(taskId.value)) return
  try {
    const [t, d, e, rev, revs, c] = await Promise.all([
      getTask(taskId.value),
      listDrafts(taskId.value),
      listEvents(taskId.value),
      listReviews(taskId.value),
      listRevisions(taskId.value),
      listCitations(taskId.value),
    ])
    task.value = t
    drafts.value = d
    events.value = e
    reviews.value = rev
    revisions.value = revs
    citations.value = c
    if (d.length && !d.some((x) => String(x.id) === activeDraftTab.value)) {
      activeDraftTab.value = String(d[0].id)
    }
  } catch {
    // keep polling; surface hard errors on manual actions
  }
}

function startPolling() {
  stopPolling()
  pollTimer = window.setInterval(() => {
    if (isBusy.value) {
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

async function runAction(
  label: string,
  fn: (id: number) => Promise<TaskItem>,
) {
  if (!task.value) return
  acting.value = true
  try {
    ElMessage.info(`已提交${label}…`)
    const updated = await fn(task.value.id)
    task.value = updated
    await refreshLight()
    // 后台任务：接口快速返回 in-progress，由轮询刷新结果，按钮不必一直转
    if (IN_PROGRESS_STATUSES.has(updated.status)) {
      ElMessage.success(`${label}已在后台执行，状态会自动刷新`)
    } else if (updated.status === 'failed') {
      ElMessage.error(`${label}失败：${updated.error_message || '未知错误'}`)
    } else {
      ElMessage.success(`${label}完成`)
    }
  } catch (e) {
    ElMessage.error(`${label}失败：${(e as Error).message}`)
    await refreshLight()
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

function onRegenerate() {
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
  return runAction('终版', finalizeTask)
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
    task.value = await updateTaskModel(task.value.id, retryModelId.value)
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
    await applyPrompt(task.value.id, {
      revision_id: selectedRevision.value.id,
      mode: applyMode.value,
      content: editedPromptContent.value,
    })
    ElMessage.success(
      applyMode.value === 'global' ? '已全局启用新 Prompt' : '已仅对本任务应用 Prompt',
    )
    applyDialogVisible.value = false
    if (alsoRegenerate.value) {
      await regenerateTask(task.value.id)
      ElMessage.success('已触发再生成')
    }
    await refreshLight()
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
  },
)

watch(taskId, () => {
  activeDraftTab.value = ''
  void loadAll()
})

onMounted(async () => {
  await loadAll()
  if (isBusy.value) startPolling()
})

onUnmounted(stopPolling)
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
        <el-button @click="refreshLight">刷新</el-button>
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

    <el-alert
      v-if="task?.error_message"
      type="error"
      show-icon
      :closable="false"
      :title="task.error_message"
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
        <el-tag type="warning" effect="light">处理中，每 2 秒自动刷新…</el-tag>
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
              </div>
              <MarkdownView :content="d.content_md" />
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
    rgba(79, 124, 255, 0.06),
    rgba(139, 92, 246, 0.05)
  );
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
