<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { UploadRequestOptions } from 'element-plus'
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
      <h1>文档管理</h1>
      <el-button @click="load">刷新</el-button>
    </div>

    <el-upload
      class="uploader"
      drag
      :http-request="customUpload"
      :show-file-list="false"
      accept=".md,.txt,.pdf,.docx,.doc"
    >
      <div class="upload-inner">
        <div class="upload-title">拖拽文件到此处，或点击上传</div>
        <div class="upload-hint">支持 md / txt / pdf / docx，上传后可点击「编译」写入 Wiki</div>
      </div>
    </el-upload>

    <el-table
      v-loading="loading"
      :data="documents"
      stripe
      empty-text="暂无文档"
      style="margin-top: 16px"
    >
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="filename" label="文件名" min-width="180" show-overflow-tooltip />
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="statusTagType(row.status)" size="small">
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="char_count" label="字符数" width="100" />
      <el-table-column label="错误信息" min-width="200">
        <template #default="{ row }">
          <span class="error-text">
            {{ jobErrors[row.id] || row.error_message || '-' }}
          </span>
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
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.page-header h1 {
  margin: 0;
}

.uploader {
  width: 100%;
}

.uploader :deep(.el-upload),
.uploader :deep(.el-upload-dragger) {
  width: 100%;
}

.upload-inner {
  padding: 12px 0;
}

.upload-title {
  font-size: 15px;
  color: #303133;
}

.upload-hint {
  margin-top: 6px;
  font-size: 13px;
  color: #909399;
}

.error-text {
  color: #f56c6c;
  font-size: 13px;
}
</style>
