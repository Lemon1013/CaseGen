<script setup lang="ts">
import { computed, onMounted, ref, type Component } from 'vue'
import { ElMessage } from 'element-plus'
import { CircleCheck, Collection, Document, InfoFilled, Refresh, Tickets } from '@element-plus/icons-vue'
import { getDashboardOverview, type DashboardOverview } from '../api/dashboard'

const TASK_GROUP_ITEMS = [
  { key: 'draft', label: '草稿' },
  { key: 'waiting_confirmation', label: '待确认' },
  { key: 'in_progress', label: '进行中' },
  { key: 'completed', label: '已完成' },
  { key: 'failed', label: '失败' },
]

const TASK_STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
  retrieving: '检索中',
  waiting_confirmation: '待确认',
  awaiting_confirmation: '待确认',
  generating_test_points: '生成测试点中',
  awaiting_test_point_confirmation: '待确认测试点',
  generating: '生成中',
  generated: '已生成',
  reviewing: '评审中',
  reviewed: '已评审',
  optimizing: '优化中',
  regenerating: '重新生成中',
  finalized: '已定稿',
  failed: '失败',
}

const OPERATION_LABELS: Record<string, string> = {
  import: '导入',
  create: '创建',
  update: '编辑',
  edit: '编辑',
  archive: '归档',
  restore: '恢复',
  export: '导出',
}

const DOCUMENT_STATUS_LABELS: Record<string, string> = {
  pending: '待处理',
  pending_ingest: '待摄入',
  processing: '处理中',
  parsing: '解析中',
  ready: '就绪',
  queued: '排队中',
  running: '处理中',
  success: '成功',
  success_with_warnings: '成功（有警告）',
  completed: '已完成',
  cancelled: '已取消',
  failed: '失败',
  archived: '已归档',
  uploaded: '已上传',
}

const WIKI_STATUS_LABELS: Record<string, string> = {
  published: '已发布',
  draft: '草稿',
  archived: '已归档',
  pending: '待审核',
}

const PLATFORM_STATUS_LABELS: Record<string, string> = {
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  pending: '待处理',
}

const REVIEW_STATUS_LABELS: Record<string, string> = {
  pending: '待处理',
  approved: '已批准',
  rejected: '已拒绝',
  merged: '已合并',
  resolved: '已解决',
}

const CASE_STATUS_LABELS: Record<string, string> = {
  active: '启用',
  archived: '已归档',
}

const ACTIVITY_STATUS_LABELS: Record<string, Record<string, string>> = {
  task: TASK_STATUS_LABELS,
  review: REVIEW_STATUS_LABELS,
  case: CASE_STATUS_LABELS,
  document: DOCUMENT_STATUS_LABELS,
}

const ACTIVITY_LABELS: Record<string, string> = {
  task: '任务',
  document: '文档',
  review: '审核',
  case: '用例',
}

const ACTIVITY_ICONS: Record<string, Component> = {
  task: Tickets,
  document: Document,
  review: CircleCheck,
  case: Collection,
}

const BAND_ORDER = ['0-59', '60-69', '70-79', '80-89', '90-100']
const PRIORITIES = ['P0', 'P1', 'P2']

const loading = ref(false)
const error = ref('')
const overview = ref<DashboardOverview | null>(null)

function finite(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function formatCount(value: unknown): string {
  return Math.max(0, finite(value)).toLocaleString('zh-CN')
}

function percentRatio(count: number, total: number): number {
  if (!Number.isFinite(total) || total <= 0) return 0
  return Math.min(Math.max(count / total, 0), 1)
}

function ratioLabel(ratio: number): string {
  if (!Number.isFinite(ratio) || ratio <= 0) return '0%'
  return (Math.min(ratio, 1) * 100).toFixed(1) + '%'
}

function ratioWidth(ratio: number): string {
  if (!Number.isFinite(ratio) || ratio <= 0) return '0%'
  return (Math.min(ratio, 1) * 100).toFixed(2) + '%'
}

function countWidth(count: number, max: number): string {
  if (!Number.isFinite(max) || max <= 0 || count <= 0) return '0%'
  return Math.max((count / max) * 100, 2).toFixed(2) + '%'
}

function withRatios(items: Array<{ label: string; count: number }>) {
  const total = items.reduce((sum, item) => sum + item.count, 0)
  return items.map((item) => {
    const ratio = percentRatio(item.count, total)
    return { ...item, percent: ratioLabel(ratio), width: ratioWidth(ratio) }
  })
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

function operationLabel(operation: string): string {
  return OPERATION_LABELS[operation] || operation || '未知'
}

function documentStatusLabel(status: string): string {
  return DOCUMENT_STATUS_LABELS[status] || status
}

function wikiStatusLabel(status: string): string {
  return WIKI_STATUS_LABELS[status] || status
}

function platformStatusLabel(status: string): string {
  return PLATFORM_STATUS_LABELS[status] || status
}

function activityLabel(type: string): string {
  return ACTIVITY_LABELS[type] || type || '其他'
}

function activityIcon(type: string): Component {
  return ACTIVITY_ICONS[type] || InfoFilled
}

function activityStatusLabel(type: string, status: string): string {
  if (!status) return '—'
  return ACTIVITY_STATUS_LABELS[type]?.[status] || status
}

function formatDateTime(raw: string | null | undefined): string {
  if (!raw) return '—'
  const date = new Date(raw)
  if (Number.isNaN(date.getTime())) return '—'
  return pad(date.getMonth() + 1) + '-' + pad(date.getDate()) + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes())
}

function formatShortDate(raw: string): string {
  if (!raw) return ''
  if (raw.length >= 10 && raw[4] === '-' && raw[7] === '-') return raw.slice(5, 10)
  const date = new Date(raw)
  if (Number.isNaN(date.getTime())) return raw.length > 10 ? raw.slice(0, 10) : raw
  return pad(date.getMonth() + 1) + '-' + pad(date.getDate())
}

const counts = computed(() => overview.value?.counts ?? null)
const quality = computed(() => overview.value?.review_quality ?? null)

const statCards = computed(() => {
  const current = counts.value
  return [
    {
      label: '用户',
      value: formatCount(current?.users?.total),
      hint:
        '活跃 ' +
        formatCount(current?.users?.active) +
        ' · 管理员 ' +
        formatCount(current?.users?.admins) +
        ' · 30 天 +' +
        formatCount(current?.users?.new_30d),
    },
    {
      label: '项目',
      value: formatCount(current?.projects?.total),
      hint: '公开 ' + formatCount(current?.projects?.public) + ' · 30 天 +' + formatCount(current?.projects?.new_30d),
    },
    {
      label: '文档',
      value: formatCount(current?.documents?.total),
      hint: '待摄入 ' + formatCount(current?.documents?.pending_ingest),
    },
    {
      label: 'Wiki 页面',
      value: formatCount(current?.wiki?.pages),
      hint: '待审核 ' + formatCount(current?.wiki?.pending_reviews),
    },
    {
      label: '任务',
      value: formatCount(current?.tasks?.total),
      hint: '',
    },
    {
      label: '用例',
      value: formatCount(current?.cases?.total),
      hint: '活跃 ' + formatCount(current?.cases?.active) + ' · 归档 ' + formatCount(current?.cases?.archived),
    },
    {
      label: '平台渲染',
      value: formatCount(current?.platform?.runs),
      hint: '产物 ' + formatCount(current?.platform?.artifacts),
    },
    {
      label: '数据池',
      value: formatCount(current?.data_pools?.total),
      hint: '版本 ' + formatCount(current?.data_pools?.revisions),
    },
  ]
})

const taskGroups = computed(() => {
  const source = overview.value?.counts?.tasks?.by_group
  const items = TASK_GROUP_ITEMS.map((group) => {
    const match = Array.isArray(source) ? source.find((item) => item?.group === group.key) : undefined
    return { label: group.label, count: Math.max(0, finite(match?.count)) }
  })
  return withRatios(items)
})

const priorityGroups = computed(() => {
  const source = overview.value?.counts?.cases?.by_priority
  const items = PRIORITIES.map((priority) => {
    const match = Array.isArray(source) ? source.find((item) => item?.priority === priority) : undefined
    return { label: priority, count: Math.max(0, finite(match?.count)) }
  })
  return withRatios(items)
})

const documentStatuses = computed(() => {
  const source = overview.value?.counts?.documents?.by_status
  const grouped = new Map<string, number>()
  if (Array.isArray(source)) {
    source.forEach((item) => {
      if (!item || typeof item.status !== 'string') return
      const label = documentStatusLabel(item.status)
      grouped.set(label, (grouped.get(label) ?? 0) + Math.max(0, finite(item.count)))
    })
  }
  const items = Array.from(grouped, ([label, count]) => ({ label, count }))
  return withRatios(items)
})

const wikiStatuses = computed(() => {
  const source = overview.value?.counts?.wiki?.by_status
  const grouped = new Map<string, number>()
  if (Array.isArray(source)) {
    source.forEach((item) => {
      if (!item || typeof item.status !== 'string') return
      const label = wikiStatusLabel(item.status)
      grouped.set(label, (grouped.get(label) ?? 0) + Math.max(0, finite(item.count)))
    })
  }
  const items = Array.from(grouped, ([label, count]) => ({ label, count }))
  return withRatios(items)
})

const platformStatuses = computed(() => {
  const source = overview.value?.counts?.platform?.by_status
  const grouped = new Map<string, number>()
  if (Array.isArray(source)) {
    source.forEach((item) => {
      if (!item || typeof item.status !== 'string') return
      const label = platformStatusLabel(item.status)
      grouped.set(label, (grouped.get(label) ?? 0) + Math.max(0, finite(item.count)))
    })
  }
  const items = Array.from(grouped, ([label, count]) => ({ label, count }))
  return withRatios(items)
})

const caseOperations = computed(() => {
  const source = overview.value?.case_operations
  const grouped = new Map<string, number>()
  if (Array.isArray(source)) {
    source.forEach((item) => {
      if (!item || typeof item.operation !== 'string' || !item.operation) return
      const label = operationLabel(item.operation)
      grouped.set(label, (grouped.get(label) ?? 0) + Math.max(0, finite(item.count)))
    })
  }
  const items = Array.from(grouped, ([label, count]) => ({ label, count }))
  const max = items.reduce((value, item) => Math.max(value, item.count), 0)
  return items.map((item) => ({ ...item, width: countWidth(item.count, max) }))
})

const trend = computed(() => {
  const source = overview.value?.trend
  if (!Array.isArray(source)) return []
  return source.map((point) => ({
    date: typeof point?.date === 'string' ? point.date : '',
    tasks: Math.max(0, finite(point?.tasks)),
    cases: Math.max(0, finite(point?.cases)),
    documents: Math.max(0, finite(point?.documents)),
  }))
})

const trendMax = computed(() => trend.value.reduce((max, point) => Math.max(max, point.tasks, point.cases, point.documents), 0))

const trendAxisLabels = computed(() => {
  const points = trend.value
  if (!points.length) return []
  const indexes = Array.from(new Set([0, Math.floor((points.length - 1) / 2), points.length - 1]))
  return indexes.map((index) => formatShortDate(points[index].date))
})

const recentActivity = computed(() => {
  const source = overview.value?.recent_activity
  return Array.isArray(source) ? source.slice(0, 6) : []
})

const avgScoreText = computed(() => {
  const value = quality.value?.avg_score
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(1) : '—'
})

const hasAvgScore = computed(() => avgScoreText.value !== '—')
const coverageText = computed(() => (finite(quality.value?.coverage) * 100).toFixed(1) + '%')

const reviewedText = computed(() => {
  const generated = finite(quality.value?.generated_tasks)
  if (generated === 0) return '0 / 0'
  return finite(quality.value?.reviewed_tasks) + ' / ' + generated
})

const scoreBands = computed(() => {
  const source = quality.value?.score_bands
  return BAND_ORDER.map((band) => {
    const match = Array.isArray(source) ? source.find((item) => item?.band === band) : undefined
    const ratio = Math.min(Math.max(finite(match?.ratio), 0), 1)
    return {
      band,
      count: Math.max(0, finite(match?.count)),
      percent: ratioLabel(ratio),
      width: ratioWidth(ratio),
    }
  })
})

const updatedAtLabel = computed(() => {
  const raw = overview.value?.generated_at
  if (!raw) return ''
  const date = new Date(raw)
  if (Number.isNaN(date.getTime())) return ''
  return '更新于 ' + pad(date.getHours()) + ':' + pad(date.getMinutes()) + ':' + pad(date.getSeconds())
})

function trendBarHeight(value: number): string {
  if (!Number.isFinite(value) || value <= 0 || trendMax.value <= 0) return '0%'
  return Math.max((value / trendMax.value) * 100, 2).toFixed(2) + '%'
}

function trendTitle(point: { date: string; tasks: number; cases: number; documents: number }): string {
  return (point.date || '日期未知') + '：任务 ' + point.tasks + '，用例 ' + point.cases + '，文档 ' + point.documents
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    overview.value = await getDashboardOverview()
  } catch (e) {
    error.value = '数据看板加载失败：' + (e as Error).message
    ElMessage.error(error.value)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page dashboard-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">数据看板</h1>
        <p class="page-subtitle">全站运营与用例生成质量概览</p>
      </div>
      <div class="page-actions">
        <span v-if="updatedAtLabel" class="updated-at">{{ updatedAtLabel }}</span>
        <el-button type="primary" :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="error"
      class="dashboard-alert"
      :title="error"
      type="error"
      show-icon
      :closable="false"
    />

    <div class="dashboard-content">
      <section v-loading="loading" class="stat-grid" aria-label="全站运营指标">
        <el-card v-for="stat in statCards" :key="stat.label" shadow="never" class="stat-card">
          <div class="stat-label">{{ stat.label }}</div>
          <div class="stat-value">{{ stat.value }}</div>
          <div v-if="stat.hint" class="stat-hint">{{ stat.hint }}</div>
        </el-card>
      </section>

      <section class="distribution-grid">
        <el-card v-loading="loading" shadow="never" class="distribution-card">
          <template #header>
            <div class="card-head">
              <strong>任务状态</strong>
              <span class="card-hint">共 {{ formatCount(counts?.tasks?.total) }} 个</span>
            </div>
          </template>
          <div class="bar-list">
            <div v-for="item in taskGroups" :key="item.label" class="bar-row">
              <div class="bar-head">
                <span class="bar-name">{{ item.label }}</span>
                <span class="bar-count">{{ item.count }} 个</span>
              </div>
              <span class="bar-percent">{{ item.percent }}</span>
              <div class="bar-track">
                <div class="bar-fill bar-fill--task" :style="{ width: item.width }"></div>
              </div>
            </div>
          </div>
        </el-card>

        <el-card v-loading="loading" shadow="never" class="distribution-card">
          <template #header>
            <div class="card-head">
              <strong>用例优先级</strong>
              <span class="card-hint">共 {{ formatCount(counts?.cases?.total) }} 个</span>
            </div>
          </template>
          <div class="bar-list">
            <div v-for="item in priorityGroups" :key="item.label" class="bar-row">
              <div class="bar-head">
                <span class="bar-name">{{ item.label }}</span>
                <span class="bar-count">{{ item.count }} 个</span>
              </div>
              <span class="bar-percent">{{ item.percent }}</span>
              <div class="bar-track">
                <div class="bar-fill bar-fill--priority" :style="{ width: item.width }"></div>
              </div>
            </div>
          </div>
        </el-card>

        <el-card v-loading="loading" shadow="never" class="distribution-card">
          <template #header>
            <div class="card-head">
              <strong>文档摄入状态</strong>
              <span class="card-hint">共 {{ formatCount(counts?.documents?.total) }} 份</span>
            </div>
          </template>
          <div v-if="documentStatuses.length" class="bar-list">
            <div v-for="item in documentStatuses" :key="item.label" class="bar-row">
              <div class="bar-head">
                <span class="bar-name">{{ item.label }}</span>
                <span class="bar-count">{{ item.count }} 份</span>
              </div>
              <span class="bar-percent">{{ item.percent }}</span>
              <div class="bar-track">
                <div class="bar-fill bar-fill--document" :style="{ width: item.width }"></div>
              </div>
            </div>
          </div>
          <el-empty v-else-if="!loading" description="暂无文档状态" :image-size="48" />
        </el-card>

        <el-card v-loading="loading" shadow="never" class="distribution-card">
          <template #header>
            <div class="card-head">
              <strong>Wiki 页面状态</strong>
              <span class="card-hint">共 {{ formatCount(counts?.wiki?.pages) }} 个</span>
            </div>
          </template>
          <div v-if="wikiStatuses.length" class="bar-list">
            <div v-for="item in wikiStatuses" :key="item.label" class="bar-row">
              <div class="bar-head">
                <span class="bar-name">{{ item.label }}</span>
                <span class="bar-count">{{ item.count }} 个</span>
              </div>
              <span class="bar-percent">{{ item.percent }}</span>
              <div class="bar-track">
                <div class="bar-fill bar-fill--wiki" :style="{ width: item.width }"></div>
              </div>
            </div>
          </div>
          <el-empty v-else-if="!loading" description="暂无 Wiki 页面" :image-size="48" />
        </el-card>

        <el-card v-loading="loading" shadow="never" class="distribution-card">
          <template #header>
            <div class="card-head">
              <strong>平台渲染状态</strong>
              <span class="card-hint">共 {{ formatCount(counts?.platform?.runs) }} 次</span>
            </div>
          </template>
          <div v-if="platformStatuses.length" class="bar-list">
            <div v-for="item in platformStatuses" :key="item.label" class="bar-row">
              <div class="bar-head">
                <span class="bar-name">{{ item.label }}</span>
                <span class="bar-count">{{ item.count }} 次</span>
              </div>
              <span class="bar-percent">{{ item.percent }}</span>
              <div class="bar-track">
                <div class="bar-fill bar-fill--platform" :style="{ width: item.width }"></div>
              </div>
            </div>
          </div>
          <el-empty v-else-if="!loading" description="暂无平台渲染" :image-size="48" />
        </el-card>
      </section>

      <section class="two-column-grid">
        <el-card v-loading="loading" shadow="never" class="review-card">
          <template #header>
            <div class="card-head">
              <strong>评审质量</strong>
              <span class="card-hint">按最新草稿的有效评审统计</span>
            </div>
          </template>
          <div class="quality-metrics">
            <div class="quality-metric">
              <span class="metric-label">平均分</span>
              <span class="metric-value" :class="{ 'metric-value--empty': !hasAvgScore }">
                {{ avgScoreText }}<small v-if="hasAvgScore" class="metric-unit">/100</small>
              </span>
            </div>
            <div class="quality-metric">
              <span class="metric-label">覆盖率</span>
              <span class="metric-value">{{ coverageText }}</span>
            </div>
            <div class="quality-metric">
              <span class="metric-label">已评审 / 已生成</span>
              <span class="metric-value metric-value--compact">{{ reviewedText }}</span>
            </div>
          </div>
          <div class="bar-list">
            <div v-for="band in scoreBands" :key="band.band" class="bar-row">
              <div class="bar-head">
                <span class="bar-name">{{ band.band }}</span>
                <span class="bar-count">{{ band.count }} 个</span>
              </div>
              <span class="bar-percent">{{ band.percent }}</span>
              <div class="bar-track">
                <div class="bar-fill bar-fill--review" :style="{ width: band.width }"></div>
              </div>
            </div>
          </div>
        </el-card>

        <el-card v-loading="loading" shadow="never" class="trend-card">
          <template #header>
            <div class="card-head">
              <strong>近 14 天新增趋势</strong>
              <span class="card-hint">任务 / 用例 / 文档</span>
            </div>
          </template>
          <template v-if="trend.length">
            <div class="trend-legend">
              <span class="legend-item"><i class="legend-dot legend-dot--tasks"></i>任务</span>
              <span class="legend-item"><i class="legend-dot legend-dot--cases"></i>用例</span>
              <span class="legend-item"><i class="legend-dot legend-dot--documents"></i>文档</span>
            </div>
            <div class="trend-plot">
              <div
                v-for="(point, index) in trend"
                :key="point.date + '-' + index"
                class="trend-column"
                :title="trendTitle(point)"
              >
                <div class="trend-bars">
                  <span class="trend-bar trend-bar--tasks" :style="{ height: trendBarHeight(point.tasks) }"></span>
                  <span class="trend-bar trend-bar--cases" :style="{ height: trendBarHeight(point.cases) }"></span>
                  <span class="trend-bar trend-bar--documents" :style="{ height: trendBarHeight(point.documents) }"></span>
                </div>
              </div>
            </div>
            <div class="trend-axis">
              <span v-for="(label, index) in trendAxisLabels" :key="label + '-' + index">{{ label }}</span>
            </div>
          </template>
          <el-empty v-else-if="!loading" description="暂无趋势数据" :image-size="48" />
        </el-card>
      </section>

      <section class="bottom-grid">
        <el-card v-loading="loading" shadow="never" class="operations-card">
          <template #header>
            <div class="card-head">
              <strong>近 7 天用例操作</strong>
              <span class="card-hint">按操作类型汇总</span>
            </div>
          </template>
          <div v-if="caseOperations.length" class="operation-list">
            <div v-for="item in caseOperations" :key="item.label" class="operation-row">
              <div class="operation-head">
                <span class="bar-name">{{ item.label }}</span>
                <span class="bar-count">{{ item.count }} 次</span>
              </div>
              <div class="bar-track">
                <div class="bar-fill bar-fill--operation" :style="{ width: item.width }"></div>
              </div>
            </div>
          </div>
          <el-empty v-else-if="!loading" description="暂无操作记录" :image-size="48" />
        </el-card>

        <el-card v-loading="loading" shadow="never" class="activity-card">
          <template #header>
            <div class="card-head">
              <strong>最近活动</strong>
              <span class="card-hint">最近 6 条</span>
            </div>
          </template>
          <div v-if="recentActivity.length" class="activity-list">
            <div v-for="item in recentActivity" :key="item.type + '-' + item.id" class="activity-item">
              <div class="activity-icon">
                <el-icon :size="16"><component :is="activityIcon(item.type)" /></el-icon>
              </div>
              <div class="activity-main">
                <div class="activity-title">{{ item.title || '未命名活动' }}</div>
                <div class="activity-meta">
                  <el-tag size="small" effect="plain">{{ activityLabel(item.type) }}</el-tag>
                  <span class="activity-project">{{ item.project_name || '未关联项目' }}</span>
                  <span class="activity-time">{{ formatDateTime(item.created_at) }}</span>
                  <el-tag size="small" effect="plain" type="info">{{ activityStatusLabel(item.type, item.status) }}</el-tag>
                </div>
              </div>
            </div>
          </div>
          <el-empty v-else-if="!loading" description="暂无最近活动" :image-size="48" />
        </el-card>
      </section>

      <p class="dashboard-note">统计口径：每个任务取最新草稿的有效评审；未解析评审不计入。</p>
    </div>
  </div>
</template>

<style scoped>
.dashboard-page {
  min-width: 0;
}

.dashboard-content {
  min-width: 0;
}

.updated-at {
  color: var(--cg-text-muted);
  font-size: 12.5px;
  white-space: nowrap;
}

.dashboard-alert {
  margin-bottom: 16px;
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 16px;
}

.stat-card {
  min-width: 0;
}

.stat-card :deep(.el-card__body) {
  min-height: 104px;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.stat-label {
  color: var(--cg-text-muted);
  font-size: 12.5px;
}

.stat-value {
  color: var(--cg-text);
  font-family: var(--cg-font-mono);
  font-size: 26px;
  font-weight: 700;
  line-height: 1.2;
  letter-spacing: -0.02em;
  overflow-wrap: anywhere;
}

.stat-hint {
  margin-top: auto;
  color: var(--cg-text-muted);
  font-size: 12px;
  line-height: 1.45;
}

.distribution-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  grid-auto-flow: row;
  gap: 16px;
  margin-bottom: 16px;
}

.distribution-card,
.review-card,
.trend-card,
.operations-card,
.activity-card {
  min-width: 0;
}

.distribution-card :deep(.el-card__body) {
  padding: 2px 18px 10px;
}

.card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  color: var(--cg-text);
}

.card-hint {
  color: var(--cg-text-muted);
  font-size: 12px;
}

.bar-list {
  display: flex;
  flex-direction: column;
}

.bar-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 6px 10px;
  align-items: baseline;
  padding: 8px 0;
  border-bottom: 1px solid var(--cg-border);
}

.bar-row:last-child {
  border-bottom: none;
}

.bar-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.bar-name {
  color: var(--cg-text);
  font-size: 13px;
  font-weight: 600;
  overflow-wrap: anywhere;
}

.bar-count {
  color: var(--cg-text-muted);
  font-size: 11.5px;
  white-space: nowrap;
}

.bar-percent {
  color: var(--cg-text-secondary);
  font-family: var(--cg-font-mono);
  font-size: 11.5px;
  white-space: nowrap;
}

.bar-track {
  grid-column: 1 / -1;
  height: 9px;
  overflow: hidden;
  border: 1px solid var(--cg-border);
  border-radius: 999px;
  background: var(--cg-surface-muted);
}

.bar-fill {
  height: 100%;
  border-radius: inherit;
  transition: width 0.25s ease;
}

.bar-fill--task,
.bar-fill--review,
.bar-fill--operation {
  background: var(--cg-accent-brand);
}

.bar-fill--priority {
  background: var(--cg-primary);
}

.bar-fill--document {
  background: var(--cg-warning);
}

.bar-fill--wiki {
  background: var(--cg-primary-2);
}

.bar-fill--platform {
  background: var(--cg-accent-brand-deep);
}

.two-column-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}

.quality-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  padding: 4px 0 12px;
  margin-bottom: 4px;
  border-bottom: 1px solid var(--cg-border);
}

.quality-metric {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.metric-label {
  color: var(--cg-text-muted);
  font-size: 11.5px;
  line-height: 1.35;
}

.metric-value {
  color: var(--cg-text);
  font-family: var(--cg-font-mono);
  font-size: 21px;
  font-weight: 700;
  line-height: 1.2;
  white-space: nowrap;
}

.metric-value--compact {
  font-size: 17px;
  white-space: normal;
  overflow-wrap: anywhere;
}

.metric-value--empty {
  color: var(--cg-text-muted);
}

.metric-unit {
  margin-left: 2px;
  color: var(--cg-text-muted);
  font-size: 11px;
  font-weight: 500;
}

.trend-legend {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin-bottom: 12px;
  color: var(--cg-text-muted);
  font-size: 12px;
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
}

.legend-dot--tasks {
  background: var(--cg-primary);
}

.legend-dot--cases {
  background: var(--cg-accent-brand);
}

.legend-dot--documents {
  background: var(--cg-warning);
}

.trend-plot {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  height: 150px;
  min-width: 0;
  padding-top: 4px;
  border-bottom: 1px solid var(--cg-border);
  background-image: linear-gradient(to top, var(--cg-border) 1px, transparent 1px);
  background-size: 100% 25%;
}

.trend-column {
  flex: 1 1 0;
  min-width: 0;
  height: 100%;
  display: flex;
  align-items: flex-end;
}

.trend-bars {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  gap: 2px;
}

.trend-bar {
  width: calc((100% - 4px) / 3);
  min-width: 2px;
  max-width: 9px;
  border-radius: 2px 2px 0 0;
  transition: height 0.25s ease;
}

.trend-bar--tasks {
  background: var(--cg-primary);
}

.trend-bar--cases {
  background: var(--cg-accent-brand);
}

.trend-bar--documents {
  background: var(--cg-warning);
}

.trend-axis {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 6px;
  color: var(--cg-text-muted);
  font-family: var(--cg-font-mono);
  font-size: 11px;
}

.bottom-grid {
  display: grid;
  grid-template-columns: minmax(0, 0.8fr) minmax(0, 1.2fr);
  gap: 16px;
}

.operation-list {
  display: flex;
  flex-direction: column;
}

.operation-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 0;
  border-bottom: 1px solid var(--cg-border);
}

.operation-row:last-child {
  border-bottom: none;
}

.operation-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}

.activity-list {
  display: flex;
  flex-direction: column;
}

.activity-item {
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr);
  gap: 10px;
  align-items: start;
  padding: 9px 0;
  border-bottom: 1px solid var(--cg-border);
}

.activity-item:last-child {
  border-bottom: none;
}

.activity-icon {
  width: 30px;
  height: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--cg-border);
  border-radius: 50%;
  background: var(--cg-surface-muted);
  color: var(--cg-accent-brand);
}

.activity-main {
  min-width: 0;
}

.activity-title {
  color: var(--cg-text);
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.activity-meta {
  display: flex;
  align-items: center;
  gap: 5px 8px;
  flex-wrap: wrap;
  margin-top: 4px;
  color: var(--cg-text-muted);
  font-size: 12px;
}

.activity-project {
  overflow-wrap: anywhere;
}

.activity-time {
  font-family: var(--cg-font-mono);
  font-size: 11.5px;
  white-space: nowrap;
}

.dashboard-note {
  margin: 14px 0 0;
  color: var(--cg-text-muted);
  font-size: 12.5px;
  line-height: 1.6;
}

@media (max-width: 1100px) {
  .stat-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 900px) {
  .stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .distribution-grid,
  .two-column-grid,
  .bottom-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 560px) {
  .stat-grid {
    grid-template-columns: 1fr;
  }

  .dashboard-page {
    padding: 18px 14px;
  }

  .stat-card :deep(.el-card__body) {
    min-height: 92px;
    padding: 14px 16px;
  }

  .distribution-card :deep(.el-card__body) {
    padding: 2px 14px 10px;
  }

  .stat-value {
    font-size: 23px;
  }
}

@media (max-width: 420px) {
  .quality-metrics {
    grid-template-columns: 1fr;
    gap: 8px;
  }
}
</style>
