<script setup lang="ts">
import type { DistributionBarItem } from './formatters'

withDefaults(
  defineProps<{
    title: string
    hint?: string
    items: DistributionBarItem[]
    unit?: string
    tone?: 'task' | 'priority' | 'platform' | 'operation'
    showPercent?: boolean
    emptyText?: string
    loading?: boolean
  }>(),
  {
    hint: '',
    unit: '个',
    tone: 'task',
    showPercent: true,
    emptyText: '暂无数据',
    loading: false,
  },
)
</script>

<template>
  <el-card v-loading="loading" shadow="never" class="distribution-card">
    <template #header>
      <div class="card-head">
        <strong>{{ title }}</strong>
        <span v-if="hint" class="card-hint">{{ hint }}</span>
      </div>
    </template>

    <div v-if="items.length" class="bar-list">
      <div v-for="item in items" :key="item.label" class="bar-row">
        <div class="bar-head">
          <span class="bar-name">{{ item.label }}</span>
          <span class="bar-count">{{ item.count }} {{ unit }}</span>
        </div>
        <span v-if="showPercent && item.percent" class="bar-percent">{{ item.percent }}</span>
        <div class="bar-track">
          <div class="bar-fill" :class="`bar-fill--${tone}`" :style="{ width: item.width }"></div>
        </div>
      </div>
    </div>

    <el-empty v-else-if="!loading" :description="emptyText" :image-size="48" />
  </el-card>
</template>

<style scoped>
.distribution-card {
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
.bar-fill--operation {
  background: var(--cg-accent-brand);
}

.bar-fill--priority {
  background: var(--cg-primary);
}

.bar-fill--platform {
  background: var(--cg-accent-brand-deep);
}

@media (max-width: 560px) {
  .distribution-card :deep(.el-card__body) {
    padding: 2px 14px 10px;
  }
}
</style>
