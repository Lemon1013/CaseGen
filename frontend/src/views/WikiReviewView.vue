<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { formatDateTime as formatTime } from '../utils/datetime'
import {
  approveWikiReview,
  getWikiDiff,
  getWikiReview,
  listWikiReviews,
  listWikiRevisions,
  rejectWikiReview,
  rollbackWikiPage,
  type WikiDiff,
  type WikiReview,
  type WikiReviewDetail,
  type WikiRevision,
} from '../api/wiki'

type ReviewFilter = 'pending' | 'all'

const loading = ref(false)
const router = useRouter()
const detailLoading = ref(false)
const actionLoading = ref(false)
const revisionsLoading = ref(false)
const revisionDiffLoading = ref(false)
const reviews = ref<WikiReview[]>([])
const selectedId = ref<number | null>(null)
const detail = ref<WikiReviewDetail | null>(null)
const revisions = ref<WikiRevision[]>([])
const revisionDiff = ref<WikiDiff | null>(null)
const reviewFilter = ref<ReviewFilter>('pending')
const kindFilter = ref('')
const diffTab = ref('unified')
const reviewedBy = ref('')
const decisionReason = ref('')

const latestRevision = computed(() =>
  revisions.value.length ? revisions.value[revisions.value.length - 1] : null,
)

function statusLabel(status: string) {
  if (status === 'pending') return '待审核'
  if (status === 'approved') return '已批准'
  if (status === 'rejected') return '已拒绝'
  return status || '未知'
}

function statusType(status: string) {
  if (status === 'approved') return 'success'
  if (status === 'rejected') return 'danger'
  if (status === 'pending') return 'warning'
  return 'info'
}

function operationLabel(operation: string | null | undefined) {
  if (operation === 'create') return '新建页面'
  if (operation === 'update') return '更新页面'
  if (operation === 'rollback') return '回滚版本'
  return operation || '未标注'
}

function kindLabel(kind: string) {
  if (kind === 'create') return '新页面'
  if (kind === 'update') return '页面更新'
  if (kind === 'merge') return '合并候选'
  return kind || 'Wiki 候选'
}

function contentOrEmpty(content: string | null | undefined) {
  return content?.trim() || '（暂无内容）'
}

function sourceLabel(documentId: number, chunkIds: number[], clauses: string[]) {
  const parts = [`文档 #${documentId}`]
  if (chunkIds.length) parts.push(`原文块 ${chunkIds.map((id) => `#${id}`).join('、')}`)
  if (clauses.length) parts.push(`条款 ${clauses.join('、')}`)
  return parts.join(' · ')
}

function errorDetail(error: unknown) {
  const raw = error instanceof Error ? error.message : String(error)
  try {
    const parsed = JSON.parse(raw) as { detail?: unknown }
    if (typeof parsed.detail === 'string') return parsed.detail
  } catch {
    // The API client may receive a plain-text error body.
  }
  return raw.replace(/^Error:\s*/i, '').trim()
}

function actionableError(action: string, error: unknown) {
  const detailText = errorDetail(error)
  const lowerDetail = detailText.toLowerCase()
  if (detailText.includes('Only pending review items')) {
    return `${action}失败：该审核项已被其他人处理，请刷新列表确认最新状态。`
  }
  if (detailText.includes('Wiki review item not found')) {
    return `${action}失败：审核项已不存在，请刷新列表。`
  }
  if (detailText.includes('Wiki page not found')) {
    return `${action}失败：关联 Wiki 页面不存在，请刷新审核详情。`
  }
  if (detailText.includes('Wiki revision not found')) {
    return `${action}失败：目标 revision 不存在，请刷新版本列表后重试。`
  }
  if (detailText.includes('Merge candidates require manual review')) {
    return `${action}失败：合并候选不能直接批准，请先人工处理候选内容。`
  }
  if (lowerDetail.includes('candidate') || detailText.includes('候选')) {
    return `${action}失败：候选内容或来源证据不完整，请检查详情后重试。`
  }
  return `${action}失败：${detailText || '服务暂时不可用'}。请刷新后重试。`
}

async function loadReviews() {
  loading.value = true
  try {
    reviews.value = await listWikiReviews({
      status: reviewFilter.value === 'pending' ? 'pending' : undefined,
      kind: kindFilter.value || undefined,
    })
    const stillExists = selectedId.value != null && reviews.value.some((item) => item.id === selectedId.value)
    const nextId = stillExists ? selectedId.value : reviews.value[0]?.id ?? null
    selectedId.value = nextId
    if (nextId != null) {
      await loadReviewDetail(nextId)
    } else {
      detail.value = null
      revisions.value = []
      revisionDiff.value = null
    }
  } catch (error) {
    ElMessage.error(actionableError('加载审核列表', error))
  } finally {
    loading.value = false
  }
}

async function selectReview(row: WikiReview) {
  if (row.id === selectedId.value && detail.value) return
  selectedId.value = row.id
  await loadReviewDetail(row.id)
}

async function loadReviewDetail(id: number) {
  detailLoading.value = true
  revisionDiff.value = null
  diffTab.value = 'unified'
  decisionReason.value = ''
  try {
    const loaded = await getWikiReview(id)
    if (selectedId.value !== id) return
    detail.value = loaded
    if (loaded.page_id != null) {
      await loadRevisions(loaded.page_id)
    } else {
      revisions.value = []
    }
  } catch (error) {
    detail.value = null
    revisions.value = []
    ElMessage.error(actionableError('加载审核详情', error))
  } finally {
    detailLoading.value = false
  }
}

async function loadRevisions(pageId: number) {
  revisionsLoading.value = true
  try {
    revisions.value = await listWikiRevisions(pageId)
  } catch (error) {
    revisions.value = []
    ElMessage.error(actionableError('加载页面版本', error))
  } finally {
    revisionsLoading.value = false
  }
}

async function approve() {
  const current = detail.value
  if (!current || current.status !== 'pending') return
  try {
    await ElMessageBox.confirm(
      `确认批准候选「${current.reason_detail.page_key || current.new_candidate.title || `审核项 #${current.id}`}」？批准后会写入 Wiki，并生成新的 revision。`,
      '批准确认',
      { type: 'warning', confirmButtonText: '批准并写入', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  actionLoading.value = true
  try {
    await approveWikiReview(current.id, {
      reviewed_by: reviewedBy.value.trim() || undefined,
      decision_reason: decisionReason.value.trim() || undefined,
    })
    ElMessage.success('审核已批准，Wiki 页面已更新')
    await loadReviews()
  } catch (error) {
    ElMessage.error(actionableError('批准审核项', error))
  } finally {
    actionLoading.value = false
  }
}

async function reject() {
  const current = detail.value
  if (!current || current.status !== 'pending') return
  const reason = decisionReason.value.trim()
  if (!reason) {
    ElMessage.warning('请先填写拒绝理由，便于后续追踪和重新处理')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认拒绝候选「${current.reason_detail.page_key || current.new_candidate.title || `审核项 #${current.id}`}」？`,
      '拒绝确认',
      { type: 'warning', confirmButtonText: '确认拒绝', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  actionLoading.value = true
  try {
    await rejectWikiReview(current.id, {
      reviewed_by: reviewedBy.value.trim() || undefined,
      decision_reason: reason,
    })
    ElMessage.success('审核项已拒绝')
    await loadReviews()
  } catch (error) {
    ElMessage.error(actionableError('拒绝审核项', error))
  } finally {
    actionLoading.value = false
  }
}

async function compareRevision(revision: WikiRevision) {
  const pageId = detail.value?.page_id
  const current = latestRevision.value
  if (pageId == null || current == null) return
  revisionDiffLoading.value = true
  try {
    revisionDiff.value = await getWikiDiff(pageId, {
      from_revision: revision.revision,
      to_revision: current.revision,
    })
    diffTab.value = 'revision'
  } catch (error) {
    ElMessage.error(actionableError('加载版本 Diff', error))
  } finally {
    revisionDiffLoading.value = false
  }
}

async function rollback(revision: WikiRevision) {
  const pageId = detail.value?.page_id
  const current = latestRevision.value
  if (pageId == null || current == null) return
  if (revision.revision === current.revision) {
    ElMessage.info('当前版本无需回滚')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认回滚到 revision ${revision.revision}？系统会保留现有历史，并创建一个新的回滚 revision。此操作会立即影响 Wiki 检索结果。`,
      '回滚二次确认',
      { type: 'warning', confirmButtonText: '确认回滚', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  actionLoading.value = true
  try {
    const created = await rollbackWikiPage(pageId, {
      revision: revision.revision,
      reason: `人工回滚到 revision ${revision.revision}`,
      reviewed_by: reviewedBy.value.trim() || undefined,
    })
    ElMessage.success(`回滚完成，已创建 revision ${created.revision}`)
    await loadRevisions(pageId)
    revisionDiff.value = null
    diffTab.value = 'unified'
    await loadReviewDetail(selectedId.value as number)
  } catch (error) {
    ElMessage.error(actionableError('回滚页面', error))
  } finally {
    actionLoading.value = false
  }
}

onMounted(loadReviews)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1 class="page-title">Wiki 审核 / 版本</h1>
        <p class="page-subtitle">核对候选内容、来源证据和版本差异，再决定是否写入知识库</p>
      </div>
      <div class="page-actions">
        <el-button :loading="loading" @click="loadReviews">刷新列表</el-button>
      </div>
    </div>

    <div class="filters panel">
      <el-radio-group v-model="reviewFilter" @change="loadReviews">
        <el-radio-button value="pending">待审核</el-radio-button>
        <el-radio-button value="all">全部</el-radio-button>
      </el-radio-group>
      <el-select
        v-model="kindFilter"
        clearable
        placeholder="按候选类型筛选"
        style="width: 180px"
        @change="loadReviews"
      >
        <el-option label="新页面" value="create" />
        <el-option label="页面更新" value="update" />
        <el-option label="合并候选" value="merge" />
      </el-select>
      <span class="filter-count">共 {{ reviews.length }} 条审核记录</span>
    </div>

    <div class="layout">
      <div class="review-list panel" v-loading="loading">
        <el-scrollbar height="690px">
          <div
            v-for="row in reviews"
            :key="row.id"
            class="review-item"
            :class="{ active: row.id === selectedId }"
            @click="selectReview(row)"
          >
            <div class="review-item-title">
              <span>{{ row.reason || `审核项 #${row.id}` }}</span>
              <el-tag :type="statusType(row.status)" size="small" effect="light">
                {{ statusLabel(row.status) }}
              </el-tag>
            </div>
            <div class="review-item-meta">
              <el-tag size="small" effect="plain">{{ kindLabel(row.kind) }}</el-tag>
              <span v-if="row.page_id != null">页面 #{{ row.page_id }}</span>
              <span v-if="row.job_id != null">任务 #{{ row.job_id }}</span>
            </div>
            <div class="review-item-time">更新于 {{ formatTime(row.updated_at) }}</div>
            <div v-if="row.decision_reason" class="review-item-reason">
              处理意见：{{ row.decision_reason }}
            </div>
          </div>
          <el-empty v-if="!reviews.length" description="暂无符合条件的审核记录" :image-size="90" />
        </el-scrollbar>
      </div>

      <div class="detail-panel panel panel-surface" v-loading="detailLoading">
        <template v-if="detail">
          <div class="detail-header">
            <div>
              <div class="detail-kicker">审核项 #{{ detail.id }} · {{ kindLabel(detail.kind) }}</div>
              <h2>{{ detail.reason_detail.page_key || detail.new_candidate.title || '未命名候选' }}</h2>
            </div>
            <div class="detail-actions">
              <el-tag :type="statusType(detail.status)" effect="light">
                {{ statusLabel(detail.status) }}
              </el-tag>
              <el-button
                v-if="detail.status === 'pending'"
                type="danger"
                plain
                :loading="actionLoading"
                @click="reject"
              >
                拒绝
              </el-button>
              <el-button
                v-if="detail.status === 'pending'"
                type="primary"
                :disabled="detail.kind === 'merge'"
                :loading="actionLoading"
                @click="approve"
              >
                {{ detail.kind === 'merge' ? '合并候选需人工处理' : '批准并写入' }}
              </el-button>
            </div>
          </div>

          <div v-if="detail.status === 'pending'" class="decision-form">
            <el-input v-model="reviewedBy" placeholder="审核人（可选）" style="width: 180px" />
            <el-input
              v-model="decisionReason"
              :placeholder="detail.status === 'pending' ? '审核意见；拒绝时必填' : '审核意见'"
              clearable
            />
          </div>

          <el-descriptions :column="2" border size="small" class="summary-table">
            <el-descriptions-item label="候选标题">
              {{ detail.new_candidate.title || '未提供' }}
            </el-descriptions-item>
            <el-descriptions-item label="操作">
              {{ operationLabel(detail.reason_detail.operation) }}
            </el-descriptions-item>
            <el-descriptions-item label="页面类型">
              {{ detail.new_candidate.type || '未标注' }}
            </el-descriptions-item>
            <el-descriptions-item label="领域">
              {{ detail.new_candidate.domain || '未标注' }}
            </el-descriptions-item>
            <el-descriptions-item label="审核时间">
              {{ formatTime(detail.reviewed_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="审核人">
              {{ detail.reviewed_by || '未记录' }}
            </el-descriptions-item>
          </el-descriptions>

          <section class="section-block">
            <div class="section-title">结构化理由</div>
            <div class="reason-summary">{{ detail.reason_detail.summary || '未提供摘要' }}</div>
            <div v-if="detail.reason_detail.risk_flags.length" class="risk-list">
              <span class="label">风险标记：</span>
              <el-tag
                v-for="risk in detail.reason_detail.risk_flags"
                :key="risk"
                type="warning"
                size="small"
                effect="plain"
              >
                {{ risk }}
              </el-tag>
            </div>
            <div v-else class="muted">未标记额外风险</div>
          </section>

          <section class="section-block">
            <div class="section-title">来源证据</div>
            <div v-if="detail.source_evidence.length" class="source-grid">
              <div v-for="source in detail.source_evidence" :key="source.document_id" class="source-card">
                <div class="source-card-title">{{ sourceLabel(source.document_id, source.chunk_ids, source.clauses) }}</div>
                <div class="source-card-meta">
                  <span v-if="source.chunk_ids.length">原文块：{{ source.chunk_ids.join('、') }}</span>
                  <span v-if="source.clauses.length">条款：{{ source.clauses.join('、') }}</span>
                  <span v-if="!source.chunk_ids.length && !source.clauses.length">仅关联文档</span>
                </div>
                <el-button
                  type="primary"
                  link
                  size="small"
                  @click="router.push({ path: '/documents', query: { document: source.document_id } })"
                >
                  查看完整原文
                </el-button>
              </div>
            </div>
            <div v-else class="muted">暂无结构化来源证据，批准前建议补充来源。</div>
          </section>

          <section class="section-block">
            <div class="section-heading">
              <div class="section-title">内容 Diff</div>
              <el-tag v-if="detail.diff.changed" type="warning" size="small" effect="plain">有变更</el-tag>
              <el-tag v-else type="info" size="small" effect="plain">无内容变更</el-tag>
            </div>
            <el-tabs v-model="diffTab">
              <el-tab-pane label="统一 Diff" name="unified">
                <pre class="code-block diff-block">{{ detail.diff.text || '（没有可显示的 Diff）' }}</pre>
              </el-tab-pane>
              <el-tab-pane label="旧版" name="old">
                <pre class="code-block">{{ contentOrEmpty(detail.old_version?.content_md) }}</pre>
              </el-tab-pane>
              <el-tab-pane label="候选" name="candidate">
                <pre class="code-block">{{ contentOrEmpty(detail.new_candidate.content_md) }}</pre>
              </el-tab-pane>
              <el-tab-pane v-if="revisionDiff" label="版本对比" name="revision">
                <div class="revision-diff-meta">
                  revision {{ revisionDiff.from_revision ?? '-' }} → {{ revisionDiff.to_revision ?? '-' }}
                </div>
                <pre class="code-block diff-block">{{ revisionDiff.text || '（没有可显示的 Diff）' }}</pre>
              </el-tab-pane>
            </el-tabs>
          </section>

          <section v-if="detail.page_id != null" class="section-block revisions-block">
            <div class="section-heading">
              <div>
                <div class="section-title">页面 revisions</div>
                <div class="muted">回滚会创建新版本，不会删除历史记录。</div>
              </div>
              <el-tag v-if="latestRevision" size="small" effect="plain">当前 revision {{ latestRevision.revision }}</el-tag>
            </div>
            <el-table v-loading="revisionsLoading" :data="revisions" size="small" empty-text="暂无版本记录">
              <el-table-column prop="revision" label="版本" width="70" />
              <el-table-column label="操作" width="100">
                <template #default="{ row }">
                  {{ operationLabel(row.operation) }}
                </template>
              </el-table-column>
              <el-table-column prop="reason" label="变更理由" min-width="180" show-overflow-tooltip />
              <el-table-column label="创建时间" width="150">
                <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
              </el-table-column>
              <el-table-column label="操作" width="170" fixed="right">
                <template #default="{ row }">
                  <el-button
                    link
                    type="primary"
                    :loading="revisionDiffLoading"
                    @click="compareRevision(row)"
                  >
                    与当前比较
                  </el-button>
                  <el-button
                    link
                    type="danger"
                    :disabled="row.revision === latestRevision?.revision"
                    :loading="actionLoading"
                    @click="rollback(row)"
                  >
                    回滚
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </section>
        </template>
        <el-empty v-else description="选择左侧审核项查看详情" :image-size="110" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  background: var(--cg-surface-muted);
}

.filter-count,
.muted {
  color: var(--cg-text-muted);
  font-size: 12px;
}

.filter-count {
  margin-left: auto;
}

.layout {
  display: grid;
  grid-template-columns: minmax(300px, 0.82fr) minmax(520px, 1.8fr);
  gap: 16px;
  align-items: start;
}

.review-list,
.detail-panel {
  min-height: 690px;
}

.review-item {
  padding: 12px;
  margin-bottom: 8px;
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius-sm);
  background: #fff;
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s;
}

.review-item:hover,
.review-item.active {
  border-color: var(--cg-border-strong);
  box-shadow: var(--cg-shadow);
}

.review-item.active {
  background: linear-gradient(90deg, rgba(79, 124, 255, 0.07), #fff 55%);
}

.review-item-title,
.review-item-meta,
.detail-header,
.detail-actions,
.section-heading,
.risk-list {
  display: flex;
  align-items: center;
}

.review-item-title,
.detail-header,
.section-heading {
  justify-content: space-between;
  gap: 10px;
}

.review-item-title {
  color: var(--cg-text);
  font-weight: 600;
  line-height: 1.4;
}

.review-item-meta {
  gap: 8px;
  margin-top: 8px;
  color: var(--cg-text-secondary);
  font-size: 12px;
}

.review-item-time,
.review-item-reason {
  margin-top: 6px;
  color: var(--cg-text-muted);
  font-size: 12px;
  line-height: 1.5;
}

.review-item-reason {
  color: var(--cg-text-secondary);
}

.detail-header {
  margin-bottom: 16px;
}

.detail-header h2 {
  margin: 4px 0 0;
  font-size: 20px;
  overflow-wrap: anywhere;
}

.detail-kicker {
  color: var(--cg-text-muted);
  font-size: 12px;
}

.detail-actions {
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.decision-form {
  display: grid;
  grid-template-columns: 180px minmax(220px, 1fr);
  gap: 8px;
  margin-bottom: 14px;
}

.summary-table {
  margin-bottom: 18px;
}

.section-block {
  padding: 16px 0;
  border-top: 1px solid var(--cg-border);
}

.section-title {
  margin-bottom: 10px;
  color: var(--cg-text);
  font-size: 14px;
  font-weight: 700;
}

.reason-summary {
  padding: 10px 12px;
  border-radius: var(--cg-radius-sm);
  background: var(--cg-primary-soft);
  color: var(--cg-text-secondary);
  line-height: 1.6;
  white-space: pre-wrap;
}

.risk-list {
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.risk-list .label {
  color: var(--cg-text-secondary);
  font-size: 12px;
}

.source-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 8px;
}

.source-card {
  padding: 10px 12px;
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius-sm);
  background: var(--cg-surface-muted);
}

.source-card-title {
  color: var(--cg-text);
  font-size: 13px;
  font-weight: 600;
}

.source-card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  margin-top: 6px;
  color: var(--cg-text-muted);
  font-size: 12px;
  line-height: 1.5;
}

.code-block {
  min-height: 180px;
  max-height: 440px;
  margin: 0;
  padding: 14px;
  overflow: auto;
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius-sm);
  background: #0b1220;
  color: #e5e7eb;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.diff-block {
  color: #dbeafe;
}

.revision-diff-meta {
  margin-bottom: 8px;
  color: var(--cg-text-muted);
  font-size: 12px;
}

.revisions-block {
  padding-bottom: 0;
}

@media (max-width: 1100px) {
  .layout {
    grid-template-columns: 1fr;
  }

  .review-list,
  .detail-panel {
    min-height: auto;
  }
}

@media (max-width: 640px) {
  .filters,
  .decision-form {
    grid-template-columns: 1fr;
    align-items: stretch;
  }

  .filters {
    flex-wrap: wrap;
  }

  .filter-count {
    width: 100%;
    margin-left: 0;
  }

  .detail-header {
    align-items: flex-start;
    flex-direction: column;
  }

  .detail-actions {
    justify-content: flex-start;
  }
}
</style>
