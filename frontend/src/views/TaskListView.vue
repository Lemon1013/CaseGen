<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { formatDateTime as formatTime } from '../utils/datetime'
import {
  deleteTask,
  generateTask,
  listTasks,
  statusLabel,
  statusTagType,
  type TaskItem,
  updateTaskModel,
} from '../api/tasks'
import { listModels, type ModelConfig } from '../api/models'

const router = useRouter()
const loading = ref(false)
const deletingId = ref<number | null>(null)
const retryingId = ref<number | null>(null)
const tasks = ref<TaskItem[]>([])
const models = ref<ModelConfig[]>([])
const retryModelIds = ref<Record<number, number | null>>({})

async function load() {
  loading.value = true
  try {
    const [taskRows, modelRows] = await Promise.all([
      listTasks(),
      listModels().catch(() => [] as ModelConfig[]),
    ])
    tasks.value = taskRows
    models.value = modelRows
    const defaultModel = modelRows.find((model) => model.is_default)?.id ?? modelRows[0]?.id ?? null
    for (const task of taskRows) {
      if (task.status === 'failed' && retryModelIds.value[task.id] == null) {
        retryModelIds.value[task.id] = task.model_id ?? defaultModel
      }
    }
  } catch (e) {
    ElMessage.error(`加载任务失败：${(e as Error).message}`)
  } finally {
    loading.value = false
  }
}

async function retryWithModel(row: TaskItem) {
  const modelId = retryModelIds.value[row.id]
  if (modelId == null) {
    ElMessage.warning('请先选择重试模型')
    return
  }
  retryingId.value = row.id
  try {
    await updateTaskModel(row.id, modelId)
    const updated = await generateTask(row.id)
    if (updated.status === 'failed') {
      ElMessage.error(`已切换模型，但重试仍失败：${updated.error_message || '未知错误'}`)
    } else {
      ElMessage.success('已切换模型并提交重试')
    }
    await load()
  } catch (e) {
    ElMessage.error(`切换模型重试失败：${(e as Error).message}`)
  } finally {
    retryingId.value = null
  }
}

function openDetail(row: TaskItem) {
  router.push(`/tasks/${row.id}`)
}

async function handleDelete(row: TaskItem) {
  try {
    await ElMessageBox.confirm(
      `确认删除任务 #${row.id}「${row.title || '未命名'}」？相关草稿、评审与事件将一并删除，且不可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  deletingId.value = row.id
  try {
    await deleteTask(row.id)
    ElMessage.success('任务已删除')
    await load()
  } catch (e) {
    ElMessage.error(`删除失败：${(e as Error).message}`)
  } finally {
    deletingId.value = null
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1 class="page-title">任务列表</h1>
        <p class="page-subtitle">跟踪生成进度、草稿版本与最新评审分数</p>
      </div>
      <div class="page-actions">
        <el-button type="primary" :icon="Plus" @click="router.push('/')">新建任务</el-button>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
    </div>

    <el-table
      v-loading="loading"
      :data="tasks"
      stripe
      empty-text="暂无任务，去工作台创建第一条"
      class="task-table"
      @row-click="openDetail"
    >
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
      <el-table-column label="Wiki 空间" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.wiki_space_name || `空间 #${row.wiki_space_id}` }}
        </template>
      </el-table-column>
      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="statusTagType(row.status)" size="small" effect="light">
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
          <span v-else class="muted">-</span>
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
      <el-table-column label="操作" width="230" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click.stop="openDetail(row)">详情</el-button>
          <el-popover
            v-if="row.status === 'failed'"
            placement="bottom-end"
            :width="310"
            trigger="click"
          >
            <template #reference>
              <el-button link type="warning" @click.stop>换模型重试</el-button>
            </template>
            <div class="retry-model" @click.stop>
              <el-select v-model="retryModelIds[row.id]" placeholder="选择模型" style="width: 100%">
                <el-option
                  v-for="model in models"
                  :key="model.id"
                  :label="`${model.name} · ${model.model_name}${model.is_default ? '（默认）' : ''}`"
                  :value="model.id"
                />
              </el-select>
              <el-button
                type="primary"
                :loading="retryingId === row.id"
                :disabled="!models.length"
                @click="retryWithModel(row)"
              >
                保存并重新生成
              </el-button>
            </div>
          </el-popover>
          <el-button
            link
            type="danger"
            :loading="deletingId === row.id"
            @click.stop="handleDelete(row)"
          >
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.task-table :deep(.el-table__row) {
  cursor: pointer;
}

.muted {
  color: var(--cg-text-muted);
}

.retry-model {
  display: grid;
  gap: 10px;
}
</style>
