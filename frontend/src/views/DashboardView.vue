<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { getDashboardOverview, type DashboardOverview } from '../api/dashboard'
import { ApiError } from '../api/client'
import { CASE_PRIORITIES } from '../utils/caseDictionaries'
import { formatTimeOnly } from '../utils/datetime'
import DashboardStatGrid from '../components/dashboard/DashboardStatGrid.vue'
import DistributionBarCard from '../components/dashboard/DistributionBarCard.vue'
import ReviewQualityCard from '../components/dashboard/ReviewQualityCard.vue'
import ActivityTrendCard from '../components/dashboard/ActivityTrendCard.vue'
import RecentActivityCard from '../components/dashboard/RecentActivityCard.vue'
import { countWidth, finite, formatCount, withRatios } from '../components/dashboard/formatters'

const TASK_GROUP_ITEMS = [
  { key: 'draft', label: '草稿' },
  { key: 'waiting_confirmation', label: '待确认' },
  { key: 'in_progress', label: '进行中' },
  { key: 'completed', label: '已完成' },
  { key: 'failed', label: '失败' },
]

const OPERATION_LABELS: Record<string, string> = {
  import: '导入',
  create: '创建',
  update: '编辑',
  edit: '编辑',
  archive: '归档',
  restore: '恢复',
  export: '导出',
}

const PLATFORM_STATUS_LABELS: Record<string, string> = {
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  pending: '待处理',
}

const loading = ref(false)
const error = ref('')
const overview = ref<DashboardOverview | null>(null)
let disposed = false

const counts = computed(() => overview.value?.counts ?? null)
const quality = computed(() => overview.value?.review_quality ?? null)

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
  const items = CASE_PRIORITIES.map((priority) => {
    const match = Array.isArray(source) ? source.find((item) => item?.priority === priority) : undefined
    return { label: priority, count: Math.max(0, finite(match?.count)) }
  })
  return withRatios(items)
})

const platformStatuses = computed(() => {
  const source = overview.value?.counts?.platform?.by_status
  const grouped = new Map<string, number>()
  if (Array.isArray(source)) {
    source.forEach((item) => {
      if (!item || typeof item.status !== 'string') return
      const label = PLATFORM_STATUS_LABELS[item.status] || item.status
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
      const label = OPERATION_LABELS[item.operation] || item.operation || '未知'
      grouped.set(label, (grouped.get(label) ?? 0) + Math.max(0, finite(item.count)))
    })
  }
  const items = Array.from(grouped, ([label, count]) => ({ label, count }))
  const max = items.reduce((value, item) => Math.max(value, item.count), 0)
  return items.map((item) => ({ ...item, width: countWidth(item.count, max) }))
})

const trend = computed(() => (Array.isArray(overview.value?.trend) ? overview.value.trend : []))

const recentActivity = computed(() => {
  const source = overview.value?.recent_activity
  return Array.isArray(source) ? source.slice(0, 6) : []
})

const updatedAtLabel = computed(() => {
  const formatted = formatTimeOnly(overview.value?.generated_at)
  return formatted ? `更新于 ${formatted}` : ''
})

async function load() {
  if (disposed) return
  loading.value = true
  error.value = ''
  try {
    const result = await getDashboardOverview()
    if (disposed) return
    overview.value = result
  } catch (e) {
    if (disposed) return
    if (e instanceof ApiError && e.status === 401) return
    error.value = `数据看板加载失败：${(e as Error).message}`
    ElMessage.error(error.value)
  } finally {
    if (!disposed) loading.value = false
  }
}

onMounted(load)

onUnmounted(() => {
  disposed = true
})
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
      <DashboardStatGrid :counts="counts" :loading="loading" />

      <section class="distribution-grid">
        <DistributionBarCard
          title="任务状态"
          :hint="`共 ${formatCount(counts?.tasks?.total)} 个`"
          :items="taskGroups"
          unit="个"
          tone="task"
          :loading="loading"
        />
        <DistributionBarCard
          title="用例优先级"
          :hint="`共 ${formatCount(counts?.cases?.total)} 个`"
          :items="priorityGroups"
          unit="个"
          tone="priority"
          :loading="loading"
        />
        <DistributionBarCard
          title="平台渲染状态"
          :hint="`共 ${formatCount(counts?.platform?.runs)} 次`"
          :items="platformStatuses"
          unit="次"
          tone="platform"
          empty-text="暂无平台渲染"
          :loading="loading"
        />
      </section>

      <section class="two-column-grid">
        <ReviewQualityCard :quality="quality" :loading="loading" />
        <ActivityTrendCard :trend="trend" :loading="loading" />
      </section>

      <section class="bottom-grid">
        <DistributionBarCard
          title="近 7 天用例操作"
          hint="按操作类型汇总"
          :items="caseOperations"
          unit="次"
          tone="operation"
          :show-percent="false"
          empty-text="暂无操作记录"
          :loading="loading"
        />
        <RecentActivityCard :items="recentActivity" :loading="loading" />
      </section>

      <p class="dashboard-note">统计口径：每个任务取最新草稿的有效评审；未解析评审不计入。</p>
    </div>
  </div>
</template>

<style scoped>
.dashboard-page,
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

.distribution-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}

.two-column-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}

.bottom-grid {
  display: grid;
  grid-template-columns: minmax(0, 0.8fr) minmax(0, 1.2fr);
  gap: 16px;
}

.dashboard-note {
  margin: 14px 0 0;
  color: var(--cg-text-muted);
  font-size: 12.5px;
  line-height: 1.6;
}

@media (max-width: 900px) {
  .distribution-grid,
  .two-column-grid,
  .bottom-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 560px) {
  .dashboard-page {
    padding: 18px 14px;
  }
}
</style>
