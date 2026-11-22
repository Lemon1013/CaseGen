<script setup lang="ts">
import { computed } from 'vue'
import type { ReviewQuality } from '../../api/dashboard'
import { finite, ratioLabel, ratioWidth } from './formatters'

const BAND_ORDER = ['0-59', '60-69', '70-79', '80-89', '90-100']

const props = defineProps<{
  quality: ReviewQuality | null
  loading: boolean
}>()

const avgScoreText = computed(() => {
  const value = props.quality?.avg_score
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(1) : '—'
})

const hasAvgScore = computed(() => avgScoreText.value !== '—')
const coverageText = computed(() => `${(finite(props.quality?.coverage) * 100).toFixed(1)}%`)

const reviewedText = computed(() => {
  const generated = finite(props.quality?.generated_tasks)
  if (generated === 0) return '0 / 0'
  return `${finite(props.quality?.reviewed_tasks)} / ${generated}`
})

const scoreBands = computed(() => {
  const source = props.quality?.score_bands
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
</script>

<template>
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
          <div class="bar-fill" :style="{ width: band.width }"></div>
        </div>
      </div>
    </div>
  </el-card>
</template>

<style scoped>
.review-card {
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
  background: var(--cg-accent-brand);
  transition: width 0.25s ease;
}

@media (max-width: 420px) {
  .quality-metrics {
    grid-template-columns: 1fr;
    gap: 8px;
  }
}
</style>
