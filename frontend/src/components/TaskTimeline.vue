<script setup lang="ts">
import type { TaskEvent } from '../api/tasks'
import { formatDateTime as formatTime } from '../utils/datetime'

defineProps<{
  events: TaskEvent[]
}>()

function stepType(step: string): '' | 'success' | 'warning' | 'info' | 'danger' | 'primary' {
  if (step.includes('fail') || step === 'error') return 'danger'
  if (step === 'retrieve') return 'info'
  if (step === 'generate' || step === 'regenerate') return 'primary'
  if (step === 'review') return 'warning'
  if (step === 'optimize' || step === 'finalize') return 'success'
  return ''
}
</script>

<template>
  <div class="timeline-wrap">
    <el-timeline v-if="events.length">
      <el-timeline-item
        v-for="ev in events"
        :key="ev.id"
        :timestamp="formatTime(ev.created_at)"
        :type="stepType(ev.step)"
        placement="top"
      >
        <div class="ev-step">{{ ev.step }}</div>
        <div class="ev-msg">{{ ev.message }}</div>
      </el-timeline-item>
    </el-timeline>
    <el-empty v-else description="暂无流水事件" :image-size="64" />
  </div>
</template>

<style scoped>
.timeline-wrap {
  padding: 4px 8px;
  max-height: 190px;
  overflow-y: auto;
  overflow-x: hidden;
  scrollbar-width: thin;
  scrollbar-color: var(--cg-border-strong, #cbd5e1) transparent;
}

.timeline-wrap::-webkit-scrollbar {
  width: 5px;
}

.timeline-wrap::-webkit-scrollbar-track {
  background: transparent;
}

.timeline-wrap::-webkit-scrollbar-thumb {
  background-color: var(--cg-border-strong, #cbd5e1);
  border-radius: 4px;
}

.timeline-wrap :deep(.el-timeline) {
  padding-left: 2px;
}

.timeline-wrap :deep(.el-timeline-item) {
  padding-bottom: 12px;
}

.timeline-wrap :deep(.el-timeline-item__node) {
  width: 9px;
  height: 9px;
  left: -1px;
}

.timeline-wrap :deep(.el-timeline-item__timestamp) {
  font-size: 11px;
  line-height: 1.2;
  margin-bottom: 4px;
}

@media (max-width: 900px) {
  .timeline-wrap {
    max-height: 220px;
    overflow-y: auto;
  }
}

.ev-step {
  font-weight: 700;
  font-size: 11px;
  margin-bottom: 1px;
  text-transform: uppercase;
  color: var(--cg-text);
  letter-spacing: 0.5px;
}

.ev-msg {
  color: var(--cg-text-secondary);
  font-size: 12px;
  line-height: 1.4;
}
</style>
