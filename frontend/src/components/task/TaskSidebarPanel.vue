<script setup lang="ts">
import TaskTimeline from '../TaskTimeline.vue'
import CitationList from '../CitationList.vue'
import ReviewCard from '../ReviewCard.vue'
import type {
  CoverageSummary,
  PromptRevision,
  ReviewResult,
  TaskCitation,
  TaskEvent,
} from '../../api/tasks'
import { dimensionLabel } from '../../utils/caseDictionaries'

defineProps<{
  events: TaskEvent[]
  coverage: CoverageSummary | null
  citations: TaskCitation[]
  citationCount: number
  projectId: number | null
  latestReview: ReviewResult | null
  revisions: PromptRevision[]
  canApplyRevision: boolean
}>()

const emit = defineEmits<{
  (e: 'apply-revision', revision: PromptRevision): void
}>()

function formatCoverage(value: number): string {
  return `${value}%`
}
</script>

<template>
  <div class="task-sidebar-panel">
    <el-card shadow="never" class="block">
      <template #header>时间线</template>
      <TaskTimeline :events="events" />
    </el-card>

    <el-card v-if="coverage" shadow="never" class="block">
      <template #header>覆盖概览</template>
      <el-progress :percentage="coverage.coverage_percent" :format="formatCoverage" />
      <div class="coverage-stats">
        已覆盖 {{ coverage.covered_test_points }} / {{ coverage.selected_test_points }} 个选中测试点；未覆盖
        {{ coverage.uncovered_test_points }} 个
      </div>
      <el-collapse>
        <el-collapse-item title="测试点明细" name="points">
          <div v-for="point in coverage.points" :key="point.stable_key" class="coverage-row">
            <el-tag size="small" :type="point.covered ? 'success' : 'warning'">
              {{ point.covered ? '已覆盖' : '未覆盖' }}
            </el-tag>
            <span>{{ point.stable_key }} · {{ point.title }}</span>
            <small>{{ point.priority }} · {{ dimensionLabel(point.dimension) }}</small>
          </div>
        </el-collapse-item>
        <el-collapse-item title="引用使用明细" name="citations">
          <div v-for="citation in coverage.citations" :key="citation.citation_id" class="coverage-row">
            <el-tag size="small" :type="citation.used ? 'success' : 'info'">
              {{ citation.used ? '已使用' : '未使用' }}
            </el-tag>
            <span>[{{ citation.citation_id }}] {{ citation.title }}</span>
            <small>{{ citation.test_point_keys.join('、') || '无测试点' }}</small>
          </div>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <el-card shadow="never" class="block">
      <CitationList
        :citations="citations"
        :count="citationCount"
        :project-id="projectId"
      />
    </el-card>

    <el-card shadow="never" class="block">
      <ReviewCard :review="latestReview" />
    </el-card>

    <el-card v-if="revisions.length" shadow="never" class="block">
      <template #header>Prompt 修订</template>
      <div
        v-for="rev in revisions"
        :key="rev.id"
        class="rev-item"
      >
        <div class="rev-head">
          <span>#{{ rev.id }} · {{ rev.status }}</span>
          <el-button
            v-if="rev.status === 'pending' && canApplyRevision"
            link
            type="primary"
            @click="emit('apply-revision', rev)"
          >
            应用
          </el-button>
        </div>
        <div class="rev-preview">
          {{ rev.new_content.slice(0, 180) }}{{ rev.new_content.length > 180 ? '…' : '' }}
        </div>
      </div>
    </el-card>
  </div>
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

.coverage-stats {
  margin: 8px 0 12px;
  font-size: 12.5px;
  color: var(--cg-text-secondary);
}

.coverage-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px dashed var(--cg-border);
  font-size: 12.5px;
}

.coverage-row:last-child {
  border-bottom: none;
}

.coverage-row small {
  color: var(--cg-text-muted);
  margin-left: auto;
}

.rev-item {
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius-sm);
  padding: 8px 10px;
  margin-bottom: 8px;
  background: var(--cg-surface-muted);
}

.rev-head {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  font-weight: 600;
}

.rev-preview {
  margin-top: 6px;
  font-size: 12px;
  color: var(--cg-text-secondary);
  white-space: pre-wrap;
}
</style>
