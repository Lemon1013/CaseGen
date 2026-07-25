<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  listTasks,
  statusLabel,
  statusTagType,
  type TaskItem,
} from '../api/tasks'

const router = useRouter()
const loading = ref(false)
const tasks = ref<TaskItem[]>([])

async function load() {
  loading.value = true
  try {
    tasks.value = await listTasks()
  } catch (e) {
    ElMessage.error(`加载任务失败：${(e as Error).message}`)
  } finally {
    loading.value = false
  }
}

function formatTime(value: string) {
  if (!value) return '-'
  return value.replace('T', ' ').slice(0, 19)
}

function openDetail(row: TaskItem) {
  router.push(`/tasks/${row.id}`)
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1>任务列表</h1>
      <div class="actions">
        <el-button type="primary" @click="router.push('/')">新建任务</el-button>
        <el-button @click="load">刷新</el-button>
      </div>
    </div>

    <el-table
      v-loading="loading"
      :data="tasks"
      stripe
      empty-text="暂无任务"
      @row-click="openDetail"
      class="task-table"
    >
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="statusTagType(row.status)" size="small">
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="引用" width="80">
        <template #default="{ row }">
          {{ row.citation_count }}
        </template>
      </el-table-column>
      <el-table-column label="Draft" width="90">
        <template #default="{ row }">
          {{ row.latest_draft_version != null ? `v${row.latest_draft_version}` : '-' }}
        </template>
      </el-table-column>
      <el-table-column label="评分" width="80">
        <template #default="{ row }">
          <span
            v-if="row.latest_review"
            :class="{ good: row.latest_review.score >= 80 }"
          >
            {{ row.latest_review.score }}
          </span>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="摘要" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.latest_draft_snippet || row.description || '-' }}
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="170">
        <template #default="{ row }">
          {{ formatTime(row.updated_at) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click.stop="openDetail(row)">详情</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.page-header h1 {
  margin: 0;
}

.actions {
  display: flex;
  gap: 8px;
}

.task-table :deep(.el-table__row) {
  cursor: pointer;
}

.good {
  color: #67c23a;
  font-weight: 600;
}
</style>
