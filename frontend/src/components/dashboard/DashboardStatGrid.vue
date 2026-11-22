<script setup lang="ts">
import { computed } from 'vue'
import type { DashboardCounts } from '../../api/dashboard'
import { formatCount } from './formatters'

const props = defineProps<{
  counts: DashboardCounts | null
  loading: boolean
}>()

const statCards = computed(() => {
  const current = props.counts
  return [
    {
      label: '用户',
      value: formatCount(current?.users?.total),
      hint: `活跃 ${formatCount(current?.users?.active)} · 管理员 ${formatCount(current?.users?.admins)} · 30 天 +${formatCount(current?.users?.new_30d)}`,
    },
    {
      label: '项目',
      value: formatCount(current?.projects?.total),
      hint: `活跃 ${formatCount(current?.projects?.active)} · 公开 ${formatCount(current?.projects?.public)} · 30 天 +${formatCount(current?.projects?.new_30d)}`,
    },
    {
      label: '任务',
      value: formatCount(current?.tasks?.total),
      hint: '核心用例生成流水线累计任务',
    },
    {
      label: '用例',
      value: formatCount(current?.cases?.total),
      hint: `当前 ${formatCount(current?.cases?.active)} · 归档 ${formatCount(current?.cases?.archived)}`,
    },
    {
      label: 'Agent 对话',
      value: formatCount(current?.agent?.conversations),
      hint: `活跃 ${formatCount(current?.agent?.active_conversations)} · 消息 ${formatCount(current?.agent?.messages)} · 30 天 +${formatCount(current?.agent?.new_30d)}`,
    },
    {
      label: '平台渲染',
      value: formatCount(current?.platform?.runs),
      hint: `产物 ${formatCount(current?.platform?.artifacts)}`,
    },
    {
      label: '数据池',
      value: formatCount(current?.data_pools?.total),
      hint: `版本 ${formatCount(current?.data_pools?.revisions)}`,
    },
  ]
})
</script>

<template>
  <section v-loading="loading" class="stat-grid" aria-label="全站运营指标">
    <el-card v-for="stat in statCards" :key="stat.label" shadow="never" class="stat-card">
      <div class="stat-label">{{ stat.label }}</div>
      <div class="stat-value">{{ stat.value }}</div>
      <div v-if="stat.hint" class="stat-hint">{{ stat.hint }}</div>
    </el-card>
  </section>
</template>

<style scoped>
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

@media (max-width: 1100px) {
  .stat-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 900px) {
  .stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 560px) {
  .stat-grid {
    grid-template-columns: 1fr;
  }

  .stat-card :deep(.el-card__body) {
    min-height: 92px;
    padding: 14px 16px;
  }

  .stat-value {
    font-size: 23px;
  }
}
</style>
