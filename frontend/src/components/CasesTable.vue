<script setup lang="ts">
import { Clock } from '@element-plus/icons-vue'
import type { TestCaseItem } from '../api/cases'
import { formatDateTime as formatTime } from '../utils/datetime'

withDefaults(
  defineProps<{
    rows: TestCaseItem[]
    showRequirement?: boolean
    requirementTitle?: (id: number) => string
  }>(),
  {
    showRequirement: false,
    requirementTitle: (id: number) => `需求 #${id}`,
  },
)

const emit = defineEmits<{
  (e: 'row-click', row: TestCaseItem, column: { type?: string; label?: string }): void
  (e: 'selection-change', rows: TestCaseItem[]): void
  (e: 'detail', row: TestCaseItem): void
  (e: 'edit', row: TestCaseItem): void
  (e: 'logs', row: TestCaseItem): void
  (e: 'toggle-archive', row: TestCaseItem): void
}>()

function formatSubmitter(row: TestCaseItem) {
  return row.created_by_display_name || row.created_by_username || '-'
}

function onRowClick(row: TestCaseItem, column: { type?: string; label?: string }) {
  emit('row-click', row, column)
}

function onSelectionChange(selection: TestCaseItem[]) {
  emit('selection-change', selection)
}
</script>

<template>
  <el-table
    :data="rows"
    stripe
    class="case-table"
    @row-click="onRowClick"
    @selection-change="onSelectionChange"
  >
    <el-table-column type="selection" width="48" />
    <el-table-column prop="case_key" label="编号" width="120">
      <template #default="{ row }">
        <el-button link type="primary" class="case-link" @click.stop="emit('detail', row)">
          {{ row.case_key }}
        </el-button>
      </template>
    </el-table-column>
    <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip>
      <template #default="{ row }">
        <el-button link class="case-title-link" @click.stop="emit('detail', row)">
          {{ row.title }}
        </el-button>
      </template>
    </el-table-column>
    <el-table-column v-if="showRequirement" label="所属需求" min-width="160" show-overflow-tooltip>
      <template #default="{ row }">
        {{ requirementTitle(row.requirement_id) }}
      </template>
    </el-table-column>
    <el-table-column label="状态" width="90">
      <template #default="{ row }">
        <el-tag :type="row.status === 'archived' ? 'info' : 'success'" size="small">
          {{ row.status === 'archived' ? '已归档' : '当前' }}
        </el-tag>
      </template>
    </el-table-column>
    <el-table-column label="优先级" width="84">
      <template #default="{ row }">
        <el-tag :type="row.priority === 'P0' ? 'danger' : row.priority === 'P1' ? 'warning' : 'info'" size="small">
          {{ row.priority || 'P1' }}
        </el-tag>
      </template>
    </el-table-column>
    <el-table-column label="提交用户" width="115" show-overflow-tooltip>
      <template #default="{ row }">{{ formatSubmitter(row) }}</template>
    </el-table-column>
    <el-table-column label="来源" min-width="145" show-overflow-tooltip>
      <template #default="{ row }">
        <span v-if="row.source_task_id">
          任务 #{{ row.source_task_id }} · 草稿 v{{ row.source_draft_version || '-' }}
        </span>
        <span v-else class="muted">直接入库 / 手工创建</span>
      </template>
    </el-table-column>
    <el-table-column label="入库时间" width="165">
      <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
    </el-table-column>
    <el-table-column label="更新时间" width="165">
      <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
    </el-table-column>
    <el-table-column label="操作" width="175" fixed="right">
      <template #default="{ row }">
        <el-button link type="primary" @click.stop="emit('edit', row)">编辑</el-button>
        <el-button link type="info" :icon="Clock" @click.stop="emit('logs', row)">日志</el-button>
        <el-button link type="warning" @click.stop="emit('toggle-archive', row)">
          {{ row.status === 'archived' ? '恢复' : '归档' }}
        </el-button>
      </template>
    </el-table-column>
  </el-table>
</template>

<style scoped>
.muted {
  color: var(--cg-text-muted);
}

.case-table :deep(.el-table__row) {
  cursor: pointer;
}

.case-link,
.case-title-link {
  max-width: 100%;
}

.case-title-link {
  color: var(--cg-text) !important;
}

.case-title-link :deep(span) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
