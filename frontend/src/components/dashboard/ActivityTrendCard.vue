<script setup lang="ts">
import { computed } from 'vue'
import type { TrendPoint } from '../../api/dashboard'
import { finite } from './formatters'

const props = defineProps<{
  trend: TrendPoint[]
  loading: boolean
}>()

function formatShortDate(raw: string): string {
  if (!raw) return ''
  if (raw.length >= 10 && raw[4] === '-' && raw[7] === '-') return raw.slice(5, 10)
  return raw.length > 10 ? raw.slice(0, 10) : raw
}

const points = computed(() => {
  if (!Array.isArray(props.trend)) return []
  return props.trend.map((point) => ({
    date: typeof point?.date === 'string' ? point.date : '',
    tasks: Math.max(0, finite(point?.tasks)),
    cases: Math.max(0, finite(point?.cases)),
    agent_conversations: Math.max(0, finite(point?.agent_conversations)),
  }))
})

const trendMax = computed(() =>
  points.value.reduce(
    (max, point) => Math.max(max, point.tasks, point.cases, point.agent_conversations),
    0,
  ),
)

const trendAxisLabels = computed(() => {
  const list = points.value
  if (!list.length) return []
  const indexes = Array.from(new Set([0, Math.floor((list.length - 1) / 2), list.length - 1]))
  return indexes.map((index) => formatShortDate(list[index].date))
})

function trendBarHeight(value: number): string {
  if (!Number.isFinite(value) || value <= 0 || trendMax.value <= 0) return '0%'
  return `${Math.max((value / trendMax.value) * 100, 2).toFixed(2)}%`
}

function trendTitle(point: { date: string; tasks: number; cases: number; agent_conversations: number }): string {
  return `${point.date || '日期未知'}：任务 ${point.tasks}，用例 ${point.cases}，Agent 对话 ${point.agent_conversations}`
}
</script>

<template>
  <el-card v-loading="loading" shadow="never" class="trend-card">
    <template #header>
      <div class="card-head">
        <strong>近 14 天新增趋势</strong>
        <span class="card-hint">任务 / 用例 / Agent 对话</span>
      </div>
    </template>

    <template v-if="points.length">
      <div class="trend-legend">
        <span class="legend-item"><i class="legend-dot legend-dot--tasks"></i>任务</span>
        <span class="legend-item"><i class="legend-dot legend-dot--cases"></i>用例</span>
        <span class="legend-item"><i class="legend-dot legend-dot--agent"></i>Agent 对话</span>
      </div>

      <div class="trend-plot">
        <div
          v-for="(point, index) in points"
          :key="`${point.date}-${index}`"
          class="trend-column"
          :title="trendTitle(point)"
        >
          <div class="trend-bars">
            <span class="trend-bar trend-bar--tasks" :style="{ height: trendBarHeight(point.tasks) }"></span>
            <span class="trend-bar trend-bar--cases" :style="{ height: trendBarHeight(point.cases) }"></span>
            <span class="trend-bar trend-bar--agent" :style="{ height: trendBarHeight(point.agent_conversations) }"></span>
          </div>
        </div>
      </div>

      <div class="trend-axis">
        <span v-for="(label, index) in trendAxisLabels" :key="`${label}-${index}`">{{ label }}</span>
      </div>
    </template>

    <el-empty v-else-if="!loading" description="暂无趋势数据" :image-size="48" />
  </el-card>
</template>

<style scoped>
.trend-card {
  min-width: 0;
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

.legend-dot--agent {
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

.trend-bar--agent {
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
</style>
