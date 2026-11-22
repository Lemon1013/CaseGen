<script setup lang="ts">
import { type Component } from 'vue'
import { Collection, InfoFilled, Tickets } from '@element-plus/icons-vue'
import type { RecentActivity } from '../../api/dashboard'
import {
  caseStatusLabel,
  caseStatusTagType,
  taskStatusLabel,
  taskStatusTagType,
} from '../../utils/caseDictionaries'
import { formatShortDateTime } from '../../utils/datetime'

const ACTIVITY_LABELS: Record<string, string> = {
  task: '任务',
  case: '用例',
}

const ACTIVITY_ICONS: Record<string, Component> = {
  task: Tickets,
  case: Collection,
}

defineProps<{
  items: RecentActivity[]
  loading: boolean
}>()

function activityLabel(type: string): string {
  return ACTIVITY_LABELS[type] || type || '其他'
}

function activityIcon(type: string): Component {
  return ACTIVITY_ICONS[type] || InfoFilled
}

function activityStatusLabel(type: string, status: string): string {
  if (!status) return '—'
  if (type === 'task') return taskStatusLabel(status)
  if (type === 'case') return caseStatusLabel(status)
  return status
}

function activityStatusTagType(type: string, status: string) {
  if (type === 'task') return taskStatusTagType(status) || 'info'
  if (type === 'case') return caseStatusTagType(status)
  return 'info'
}
</script>

<template>
  <el-card v-loading="loading" shadow="never" class="activity-card">
    <template #header>
      <div class="card-head">
        <strong>最近活动</strong>
        <span class="card-hint">最近 6 条任务与用例动态</span>
      </div>
    </template>

    <div v-if="items.length" class="activity-list">
      <div v-for="item in items" :key="`${item.type}-${item.id}`" class="activity-item">
        <div class="activity-icon">
          <el-icon :size="16"><component :is="activityIcon(item.type)" /></el-icon>
        </div>
        <div class="activity-main">
          <div class="activity-title">{{ item.title || '未命名活动' }}</div>
          <div class="activity-meta">
            <el-tag size="small" effect="plain">{{ activityLabel(item.type) }}</el-tag>
            <span class="activity-project">{{ item.project_name || '未关联项目' }}</span>
            <span class="activity-time">{{ formatShortDateTime(item.created_at) }}</span>
            <el-tag size="small" effect="plain" :type="activityStatusTagType(item.type, item.status)">
              {{ activityStatusLabel(item.type, item.status) }}
            </el-tag>
          </div>
        </div>
      </div>
    </div>

    <el-empty v-else-if="!loading" description="暂无最近活动" :image-size="48" />
  </el-card>
</template>

<style scoped>
.activity-card {
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
</style>
