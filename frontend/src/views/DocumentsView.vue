<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { UploadRequestOptions } from 'element-plus'
import { Refresh, UploadFilled } from '@element-plus/icons-vue'
import {
  getIngestJob,
  ingestDocument,
  listDocuments,
  uploadDocument,
  type DocumentItem,
} from '../api/documents'

const loading = ref(false)
const documents = ref<DocumentItem[]>([])
const ingestingId = ref<number | null>(null)
const jobErrors = ref<Record<number, string>>({})
const pollTimers = new Map<number, number>()

async function load() {
  loading.value = true
  try {
    documents.value = await listDocuments()
  } catch (e) {
    ElMessage.error(`加载文档失败：${(e as Error).message}`)
  } finally {
    loading.value = false
  }
}

async function customUpload(options: UploadRequestOptions) {
  try {
    const file = options.file as File
    await uploadDocument(file)
    ElMessage.success(`上传成功：${file.name}`)
    options.onSuccess?.({} as never)
    await load()
  } catch (e) {
    const msg = (e as Error).message
    ElMessage.error(`上传失败：${msg}`)
    options.onError?.(new Error(msg) as never)
  }
}

function statusTagType(status: string) {
  switch (status) {
    case 'ready':
      return 'success'
    case 'parsed':
      return 'info'
    case 'ingesting':
      return 'warning'
    case 'failed':
      return 'danger'
    default:
      return ''
  }
}

function statusLabel(status: string) {
  const map: Record<string, string> = {
    parsed: '已解析',
    ingesting: '编译中',
    ready: '已就绪',
    failed: '失败',
  }
  return map[status] || status
}

async function startIngest(row: DocumentItem) {
  ingestingId.value = row.id
  jobErrors.value = { ...jobErrors.value, [row.id]: '' }
  try {
    const job = await ingestDocument(row.id)
    ElMessage.info(`已开始编译，任务 #${job.id}`)
    if (job.status === 'success') {
      ElMessage.success('编译完成')
      await load()
    } else if (job.status === 'failed') {
      jobErrors.value = {
        ...jobErrors.value,
        [row.id]: job.error_message || '编译失败',
      }
      ElMessage.error(job.error_message || '编译失败')
      await load()
    } else {
      pollJob(row.id, job.id)
    }
  } catch (e) {
    const msg = (e as Error).message
    jobErrors.value = { ...jobErrors.value, [row.id]: msg }
    ElMessage.error(`编译失败：${msg}`)
    await load()
  } finally {
    if (!pollTimers.has(row.id)) {
      ingestingId.value = null
    }
  }
}

function pollJob(documentId: number, jobId: number) {
  clearPoll(documentId)
  const timer = window.setInterval(async () => {
    try {
      const job = await getIngestJob(jobId)
      if (job.status === 'success') {
        clearPoll(documentId)
        ingestingId.value = null
        ElMessage.success(`文档 #${documentId} 编译完成`)
        await load()
      } else if (job.status === 'failed') {
        clearPoll(documentId)
        ingestingId.value = null
        jobErrors.value = {
          ...jobErrors.value,
          [documentId]: job.error_message || '编译失败',
        }
        ElMessage.error(job.error_message || '编译失败')
        await load()
      }
    } catch (e) {
      clearPoll(documentId)
      ingestingId.value = null
      jobErrors.value = {
        ...jobErrors.value,
        [documentId]: (e as Error).message,
      }
    }
  }, 2000)
  pollTimers.set(documentId, timer)
}

function clearPoll(documentId: number) {
  const t = pollTimers.get(documentId)
  if (t != null) {
    clearInterval(t)
    pollTimers.delete(documentId)
  }
}

function formatTime(value: string) {
  if (!value) return '-'
  return value.replace('T', ' ').slice(0, 19)
}

onMounted(load)

onUnmounted(() => {
  for (const id of pollTimers.keys()) {
    clearPoll(id)
  }
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1 class="page-title">文档管理</h1>
        <p class="page-subtitle">上传源文档，编译写入 Wiki 供生成检索使用</p>
      </div>
      <div class="page-actions">
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
    </div>

    <el-upload
      class="uploader"
      drag
      :http-request="customUpload"
      :show-file-list="false"
      accept=".md,.txt,.pdf,.docx,.doc"
    >
      <div class="upload-inner">
        <el-icon class="upload-icon" :size="36"><UploadFilled /></el-icon>
        <div class="upload-title">拖拽文件到此处，或点击上传</div>
        <div class="upload-hint">支持 md / txt / pdf / docx · 上传后点击「编译」写入 Wiki</div>
      </div>
    </el-upload>

    <el-table
      v-loading="loading"
      :data="documents"
      stripe
      empty-text="暂无文档，先上传一份业务规则吧"
      style="margin-top: 18px"
    >
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="filename" label="文件名" min-width="180" show-overflow-tooltip />
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="statusTagType(row.status)" size="small" effect="light">
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="char_count" label="字符数" width="100" />
      <el-table-column label="错误信息" min-width="200">
        <template #default="{ row }">
          <el-tooltip
            v-if="jobErrors[row.id] || row.error_message"
            :content="jobErrors[row.id] || row.error_message || ''"
            placement="top"
          >
            <span class="error-text truncate">
              {{ jobErrors[row.id] || row.error_message }}
            </span>
          </el-tooltip>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="170">
        <template #default="{ row }">
          {{ formatTime(row.updated_at) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button
            type="primary"
            link
            :loading="ingestingId === row.id"
            :disabled="row.status === 'failed' && !row.char_count"
            @click="startIngest(row)"
          >
            编译
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.uploader {
  width: 100%;
}

.uploader :deep(.el-upload),
.uploader :deep(.el-upload-dragger) {
  width: 100%;
  border-radius: var(--cg-radius);
  border: 1.5px dashed rgba(79, 124, 255, 0.35);
  background: linear-gradient(
    135deg,
    rgba(79, 124, 255, 0.06),
    rgba(139, 92, 246, 0.05)
  );
  transition: border-color 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
}

.uploader :deep(.el-upload-dragger:hover) {
  border-color: var(--cg-primary);
  box-shadow: 0 0 0 3px rgba(79, 124, 255, 0.1);
}

.upload-inner {
  padding: 18px 0 10px;
}

.upload-icon {
  color: var(--cg-primary);
  margin-bottom: 8px;
}

.upload-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--cg-text);
}

.upload-hint {
  margin-top: 6px;
  font-size: 13px;
  color: var(--cg-text-muted);
}

.truncate {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
}

.muted {
  color: var(--cg-text-muted);
}
</style>
