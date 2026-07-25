<script setup lang="ts">
import type { TaskEvent } from '../api/tasks'

defineProps<{
  events: TaskEvent[]
}>()

function formatTime(value: string) {
  if (!value) return ''
  return value.replace('T', ' ').slice(0, 19)
}

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
}

.ev-step {
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 2px;
  text-transform: uppercase;
}

.ev-msg {
  color: #606266;
  font-size: 13px;
}
</style>
