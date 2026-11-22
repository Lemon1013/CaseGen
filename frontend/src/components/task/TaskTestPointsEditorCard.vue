<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { TestPointCheckpoint, TestPointItem } from '../../api/tasks'
import { dimensionLabel } from '../../utils/caseDictionaries'

export interface TestPointSubmissionItem {
  stable_key: string
  title: string
  verification_goal: string
  dimension: string
  priority: string
  sort_order: number
  is_selected: boolean
  is_excluded: boolean
  citation_ids: number[]
}

const props = defineProps<{
  checkpoint: TestPointCheckpoint
  taskId: number
  dimensions: string[]
  saving: boolean
  editable: boolean
}>()

const emit = defineEmits<{
  (e: 'save', points: TestPointSubmissionItem[]): void
  (e: 'confirm', points: TestPointSubmissionItem[]): void
}>()

const localPoints = ref<TestPointItem[]>([])
let nextTempId = -1

watch(
  () => `${props.checkpoint.id}:${props.checkpoint.version}`,
  () => {
    localPoints.value = props.checkpoint.points.map((item) => ({
      ...item,
      citation_ids: [...(item.citation_ids || [])],
    }))
  },
  { immediate: true },
)

const dimensionOptions = computed(() => {
  const seen = new Set<string>(props.dimensions)
  for (const point of localPoints.value) {
    if (point.dimension) seen.add(point.dimension)
  }
  return Array.from(seen)
})

function nextStableKey(): string {
  const existingKeys = new Set<string>()
  let maxNum = 0
  for (const point of localPoints.value) {
    existingKeys.add(point.stable_key)
    const match = /^TP-(\d+)$/i.exec(point.stable_key.trim())
    if (match) {
      const num = Number(match[1])
      if (Number.isFinite(num) && num > maxNum) {
        maxNum = num
      }
    }
  }
  let candidateNum = maxNum + 1
  let candidate = `TP-${String(candidateNum).padStart(3, '0')}`
  while (existingKeys.has(candidate)) {
    candidateNum += 1
    candidate = `TP-${String(candidateNum).padStart(3, '0')}`
  }
  return candidate
}

function addPoint() {
  if (!props.editable) return
  const tempId = nextTempId
  nextTempId -= 1
  localPoints.value.push({
    id: tempId,
    task_id: props.taskId,
    checkpoint_id: props.checkpoint.id,
    stable_key: nextStableKey(),
    title: '',
    verification_goal: '',
    dimension: props.dimensions[0] || 'positive',
    priority: 'P1',
    sort_order: localPoints.value.length,
    is_selected: true,
    is_excluded: false,
    citation_ids: [],
    created_at: '',
    updated_at: '',
  })
}

function removePoint(index: number) {
  if (!props.editable) return
  localPoints.value.splice(index, 1)
  localPoints.value.forEach((item, itemIndex) => {
    item.sort_order = itemIndex
  })
}

function buildPayload(): TestPointSubmissionItem[] {
  return localPoints.value.map((item, index) => ({
    stable_key: item.stable_key,
    title: item.title,
    verification_goal: item.verification_goal,
    dimension: item.dimension,
    priority: item.priority,
    sort_order: index,
    is_selected: item.is_selected,
    is_excluded: item.is_excluded,
    citation_ids: [...(item.citation_ids || [])],
  }))
}
</script>

<template>
  <el-card shadow="never" class="block checkpoint-card test-point-card">
    <template #header>
      <div class="card-head">
        <span>测试点确认</span>
        <el-tag type="warning" size="small">先确认测试点，再生成完整用例</el-tag>
      </div>
    </template>

    <p class="meta">可编辑标题、验证目标、维度、优先级、选择状态和引用；删除或排除不需要的测试点。</p>

    <div v-for="(point, index) in localPoints" :key="point.id" class="test-point-editor">
      <div class="test-point-editor-head">
        <strong class="point-key">{{ point.stable_key }}</strong>
        <el-select v-model="point.dimension" :disabled="!editable" style="width: 130px">
          <el-option
            v-for="dimension in dimensionOptions"
            :key="dimension"
            :label="dimensionLabel(dimension)"
            :value="dimension"
          />
        </el-select>
        <el-select v-model="point.priority" :disabled="!editable" style="width: 96px">
          <el-option label="P0" value="P0" />
          <el-option label="P1" value="P1" />
          <el-option label="P2" value="P2" />
        </el-select>
        <el-checkbox v-model="point.is_selected" :disabled="!editable">选中</el-checkbox>
        <el-checkbox v-model="point.is_excluded" :disabled="!editable">排除</el-checkbox>
        <span class="point-head-spacer" />
        <el-button link type="danger" :disabled="!editable" @click="removePoint(index)">删除</el-button>
      </div>
      <el-input v-model="point.title" :disabled="!editable" placeholder="测试点标题" class="point-title-input" />
      <el-input v-model="point.verification_goal" :disabled="!editable" type="textarea" :rows="2" placeholder="验证目标" />
    </div>

    <div class="point-actions">
      <el-button :disabled="!editable" @click="addPoint">新增测试点</el-button>
      <el-button :loading="saving" :disabled="!editable" @click="emit('save', buildPayload())">
        保存编辑
      </el-button>
      <el-button type="primary" :loading="saving" :disabled="!editable" @click="emit('confirm', buildPayload())">
        保存并确认测试点
      </el-button>
    </div>
  </el-card>
</template>

<style scoped>
.block {
  margin-bottom: 16px;
  border-radius: var(--cg-radius) !important;
}

.block :deep(.el-card__header) {
  font-weight: 700;
  border-bottom-color: var(--cg-border);
}

.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.meta {
  color: var(--cg-text-muted);
  font-size: 12px;
}

.test-point-editor {
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius-sm, 6px);
  padding: 12px;
  background: var(--cg-bg-card, #ffffff);
  margin-bottom: 10px;
}

.test-point-editor-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.point-key {
  font-family: var(--cg-font-mono, monospace);
}

.point-head-spacer {
  flex: 1;
}

.point-title-input {
  margin-bottom: 8px;
}

.point-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}
</style>
