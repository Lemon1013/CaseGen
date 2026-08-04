<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { UploadRequestOptions } from 'element-plus'
import { Refresh, UploadFilled } from '@element-plus/icons-vue'
import { formatDateTime as formatTime } from '../utils/datetime'
import {
  cancelIngestJob,
  getDocumentPreview,
  getIngestJob,
  ingestDocument,
  listDocumentChunks,
  listDocuments,
  listIngestJobs,
  uploadDocument,
  type DocumentDiagnostics,
  type DocumentItem,
  type DocumentPreview,
  type IngestJob,
  type IngestStep,
  type SourceChunk,
} from '../api/documents'

const loading = ref(false)
const route = useRoute()
const documents = ref<DocumentItem[]>([])
const jobs = ref<Record<number, IngestJob | undefined>>({})
const jobErrors = ref<Record<number, string>>({})
const actionLoading = ref<Record<number, boolean>>({})
const qualityPreviews = ref<Record<number, DocumentPreview | undefined>>({})
const qualityErrors = ref<Record<number, string>>({})
const qualityLoading = ref<Record<number, boolean>>({})
const pollTimers = new Map<number, number>()
const pollingDocuments = new Set<number>()
const qualityRequests = new Map<number, Promise<void>>()

const chunkDrawerVisible = ref(false)
const chunkLoading = ref(false)
const chunkDocument = ref<DocumentItem | null>(null)
const chunks = ref<SourceChunk[]>([])
const previewDrawerVisible = ref(false)
const previewLoading = ref(false)
const previewDocument = ref<DocumentItem | null>(null)
const previewContent = ref<DocumentPreview | null>(null)

const activeJobStatuses = new Set(['queued', 'running'])
const terminalJobStatuses = new Set(['success', 'failed', 'cancelled'])

function setJob(documentId: number, job: IngestJob | undefined) {
  jobs.value = { ...jobs.value, [documentId]: job }
}

function setActionLoading(documentId: number, value: boolean) {
  actionLoading.value = { ...actionLoading.value, [documentId]: value }
}

function setJobError(documentId: number, value: string) {
  jobErrors.value = { ...jobErrors.value, [documentId]: value }
}

function setQualityPreview(documentId: number, value: DocumentPreview | undefined) {
  qualityPreviews.value = { ...qualityPreviews.value, [documentId]: value }
}

function setQualityError(documentId: number, value: string) {
  qualityErrors.value = { ...qualityErrors.value, [documentId]: value }
}

function setQualityLoading(documentId: number, value: boolean) {
  qualityLoading.value = { ...qualityLoading.value, [documentId]: value }
}

function errorMessage(error: unknown, fallback: string) {
  const raw = error instanceof Error ? error.message : String(error || '')
  let detail = raw
  try {
    const payload = JSON.parse(raw) as { detail?: unknown; message?: unknown }
    detail = String(payload.detail || payload.message || raw)
  } catch {
    // The shared API client may already have received a plain-text error.
  }

  if (/unsupported file extension/i.test(detail)) {
    return '文件类型不支持，请上传 md、txt、pdf 或 docx 文件。'
  }
  if (/file exceeds max size/i.test(detail)) {
    return '文件超过上传大小限制，请压缩文件或拆分后再上传。'
  }
  if (/source file not found|not found on disk/i.test(detail)) {
    return '找不到源文件，请重新上传后再试。'
  }
  if (/parse failed/i.test(detail)) {
    return `文档解析失败：${detail.replace(/^parse failed:\s*/i, '')}。请检查文件是否损坏或需要 OCR。`
  }
  if (/quality|ocr|乱码|替换字符|未提取到正文/i.test(detail)) {
    return `解析质量不足：${detail}。请检查原文或先进行 OCR。`
  }
  return detail || fallback
}

function isActiveJob(job: IngestJob | undefined | null) {
  return !!job && activeJobStatuses.has(job.status)
}

function isTerminalJob(job: IngestJob | undefined | null) {
  return !!job && terminalJobStatuses.has(job.status)
}

function currentJob(row: DocumentItem) {
  return jobs.value[row.id]
}

async function loadDocumentsOnly() {
  documents.value = await listDocuments()
}

async function loadQualityPreview(row: DocumentItem) {
  const existing = qualityRequests.get(row.id)
  if (existing) {
    await existing
    return
  }

  setQualityLoading(row.id, true)
  const request = getDocumentPreview(row.id, 50000)
    .then((preview) => {
      setQualityPreview(row.id, preview)
      setQualityError(row.id, '')
    })
    .catch((error: unknown) => {
      setQualityError(row.id, errorMessage(error, '解析质量详情暂时不可用'))
    })
    .finally(() => {
      setQualityLoading(row.id, false)
      qualityRequests.delete(row.id)
    })
  qualityRequests.set(row.id, request)
  await request
}

function loadQualityPreviews(rows: DocumentItem[]) {
  return Promise.all(rows.map((row) => loadQualityPreview(row)))
}

async function restoreJobs(rows: DocumentItem[]) {
  let allJobs: IngestJob[]
  try {
    allJobs = await listIngestJobs()
  } catch (error: unknown) {
    ElMessage.warning(`摄入任务加载失败：${errorMessage(error, '请点击刷新重试')}`)
    return
  }

  const visibleDocuments = new Set(rows.map((row) => row.id))
  const latestByDocument = new Map<number, IngestJob>()
  for (const job of allJobs) {
    if (visibleDocuments.has(job.document_id) && !latestByDocument.has(job.document_id)) {
      latestByDocument.set(job.document_id, job)
    }
  }

  for (const [documentId, job] of latestByDocument) {
    setJob(documentId, job)
    if (isActiveJob(job)) {
      pollJob(documentId, job.id)
    } else {
      clearPoll(documentId)
    }
  }
}

async function load() {
  loading.value = true
  try {
    const rows = await listDocuments()
    documents.value = rows
    void loadQualityPreviews(rows)
    await restoreJobs(rows)
  } catch (error: unknown) {
    ElMessage.error(`加载文档失败：${errorMessage(error, '请检查后端服务是否正常')}`)
  } finally {
    loading.value = false
  }
}

async function customUpload(options: UploadRequestOptions) {
  try {
    const file = options.file as File
    const document = await uploadDocument(file)
    ElMessage.success(`上传成功：${file.name}，已完成基础解析`)
    options.onSuccess?.(document as never)
    await load()
  } catch (error: unknown) {
    const message = errorMessage(error, '请检查文件后重试')
    ElMessage.error(`上传失败：${message}`)
    options.onError?.(new Error(message) as never)
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
    case 'cancelled':
      return 'info'
    default:
      return ''
  }
}

function statusLabel(status: string) {
  const map: Record<string, string> = {
    parsed: '已解析',
    ingesting: '摄入中',
    ready: '已就绪',
    failed: '解析失败',
    cancelled: '已取消',
  }
  return map[status] || status || '未知'
}

function jobStatusTagType(status: string) {
  if (status === 'success') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'cancelled') return 'info'
  return 'warning'
}

function jobStatusLabel(status: string) {
  const map: Record<string, string> = {
    queued: '排队中',
    running: '处理中',
    success: '已完成',
    failed: '失败',
    cancelled: '已取消',
  }
  return map[status] || status || '未知'
}

function stageLabel(stage: string) {
  const map: Record<string, string> = {
    queued: '排队等待',
    parsing: '解析原文',
    recalling: '召回候选',
    analyzing: '分析内容',
    writing: '生成 Wiki',
    applying: '写入 Wiki',
    indexing: '更新索引',
    ready: '处理完成',
    failed: '处理失败',
    cancelled: '已取消',
  }
  return map[stage] || stage || '处理中'
}

function progressValue(progress: number) {
  return Math.max(0, Math.min(100, Number(progress) || 0))
}

function parseStepLog(job: IngestJob | undefined | null): IngestStep[] {
  if (!job?.step_log_json) return []
  try {
    const value: unknown = JSON.parse(job.step_log_json)
    if (!Array.isArray(value)) return []
    return value.filter(
      (item): item is IngestStep => typeof item === 'object' && item !== null,
    )
  } catch {
    return []
  }
}

function latestStep(job: IngestJob | undefined | null) {
  const steps = parseStepLog(job)
  return steps[steps.length - 1]
}

function stepText(step: IngestStep | undefined) {
  if (!step) return ''
  return String(step.message || step.step || '')
}

function stepTime(step: IngestStep | undefined) {
  if (!step?.at) return ''
  return formatTime(step.at)
}

function jobErrorText(job: IngestJob | undefined | null) {
  if (!job) return ''
  if (job.error_message) return errorMessage(job.error_message, '任务失败')
  if (job.status === 'failed') return '摄入失败，请查看任务日志并点击“重试”；若反复失败，请检查解析质量。'
  return ''
}

async function startIngest(row: DocumentItem, force = false) {
  setActionLoading(row.id, true)
  setJobError(row.id, '')
  try {
    const job = await ingestDocument(row.id, force)
    setJob(row.id, job)
    if (isActiveJob(job)) {
      ElMessage.info(`${force ? '已提交重试' : '已开始摄入'}，任务 #${job.id}`)
      pollJob(row.id, job.id)
    } else {
      await finishJob(row.id, job, true)
    }
  } catch (error: unknown) {
    const message = errorMessage(error, '请稍后重试')
    setJobError(row.id, message)
    ElMessage.error(`${force ? '重试' : '摄入'}失败：${message}`)
  } finally {
    setActionLoading(row.id, false)
  }
}

async function finishJob(documentId: number, job: IngestJob, notify: boolean) {
  setJob(documentId, job)
  clearPoll(documentId)
  setActionLoading(documentId, false)

  if (job.status === 'success') {
    setJobError(documentId, '')
    if (notify) ElMessage.success(`文档 #${documentId} 摄入完成`)
  } else if (job.status === 'failed') {
    const message = jobErrorText(job)
    setJobError(documentId, message)
    if (notify) ElMessage.error(message)
  } else if (job.status === 'cancelled') {
    setJobError(documentId, '')
    if (notify) ElMessage.info(`文档 #${documentId} 的摄入任务已取消`)
  }

  try {
    await loadDocumentsOnly()
    const row = documents.value.find((item) => item.id === documentId)
    if (row) await loadQualityPreview(row)
  } catch (error: unknown) {
    setJobError(documentId, errorMessage(error, '任务已结束，但刷新文档状态失败'))
  }
}

async function refreshJob(documentId: number, jobId: number) {
  if (pollingDocuments.has(documentId)) return
  pollingDocuments.add(documentId)
  try {
    const job = await getIngestJob(jobId)
    setJob(documentId, job)
    if (isTerminalJob(job)) {
      await finishJob(documentId, job, true)
    }
  } catch (error: unknown) {
    setJobError(documentId, `任务进度刷新失败，将自动重试：${errorMessage(error, '网络异常')}`)
  } finally {
    pollingDocuments.delete(documentId)
  }
}

function pollJob(documentId: number, jobId: number) {
  clearPoll(documentId)
  const timer = window.setInterval(() => {
    void refreshJob(documentId, jobId)
  }, 2000)
  pollTimers.set(documentId, timer)
  void refreshJob(documentId, jobId)
}

function clearPoll(documentId: number) {
  const timer = pollTimers.get(documentId)
  if (timer != null) {
    clearInterval(timer)
    pollTimers.delete(documentId)
  }
}

async function cancelJob(row: DocumentItem) {
  const job = currentJob(row)
  if (!job || !isActiveJob(job)) return

  setActionLoading(row.id, true)
  try {
    const updated = await cancelIngestJob(job.id)
    setJob(row.id, updated)
    if (isActiveJob(updated)) {
      setJobError(row.id, '已提交取消请求，当前步骤结束后会停止任务。')
      pollJob(row.id, updated.id)
      ElMessage.info('已提交取消请求，正在等待任务停止')
    } else {
      await finishJob(row.id, updated, true)
    }
  } catch (error: unknown) {
    const message = errorMessage(error, '取消请求未发送成功')
    setJobError(row.id, `${message}，请刷新后确认任务状态。`)
    ElMessage.error(`取消失败：${message}`)
  } finally {
    setActionLoading(row.id, false)
  }
}

function qualityPreview(row: DocumentItem) {
  return qualityPreviews.value[row.id]
}

function qualityDiagnostics(row: DocumentItem): DocumentDiagnostics | undefined {
  return qualityPreview(row)?.diagnostics
}

function diagnosticWarnings(row: DocumentItem) {
  return qualityDiagnostics(row)?.warnings || []
}

function diagnosticErrors(row: DocumentItem) {
  return qualityDiagnostics(row)?.errors || []
}

function qualityLabel(row: DocumentItem) {
  const preview = qualityPreview(row)
  if (preview) {
    if (!preview.quality_ok) return '需检查'
    if (diagnosticWarnings(row).length) return '通过，有提示'
    return '质量通过'
  }
  if (qualityLoading.value[row.id]) return '检查中'
  if (qualityErrors.value[row.id]) return '检查失败'
  if (row.status === 'failed') return '解析失败'
  return row.char_count > 0 ? '基础可用' : '待检查'
}

function qualityTagType(row: DocumentItem) {
  const preview = qualityPreview(row)
  if (preview?.quality_ok && !diagnosticWarnings(row).length) return 'success'
  if (preview?.quality_ok) return 'warning'
  if (preview && !preview.quality_ok) return 'danger'
  if (qualityErrors.value[row.id] || row.status === 'failed') return 'danger'
  return 'info'
}

function qualityNote(row: DocumentItem) {
  if (qualityErrors.value[row.id]) {
    return '后端质量诊断暂时不可用，当前仅显示上传记录中的状态和字符数。'
  }
  return '质量标签来自后端解析诊断，不是模型评分；状态和字符数来自文档上传记录。'
}

function qualityDetail(row: DocumentItem) {
  const preview = qualityPreview(row)
  if (!preview) return ''
  const diagnostic = preview.diagnostics
  const pages = diagnostic.page_count
    ? `，${diagnostic.pages_with_text || 0}/${diagnostic.page_count} 页有文本`
    : ''
  const returned = preview.truncated ? `，预览已截取 ${preview.returned_chars} 字符` : ''
  return `解析正文 ${preview.char_count} 字符${pages}${returned}`
}

function parseClauseIds(raw: string) {
  try {
    const value: unknown = JSON.parse(raw || '[]')
    if (!Array.isArray(value)) return []
    return value.map((item) => String(item)).filter(Boolean)
  } catch {
    return []
  }
}

async function openChunks(row: DocumentItem) {
  chunkDocument.value = row
  chunkDrawerVisible.value = true
  chunkLoading.value = true
  chunks.value = []
  try {
    chunks.value = await listDocumentChunks(row.id)
  } catch (error: unknown) {
    ElMessage.error(`加载原文块失败：${errorMessage(error, '请刷新后重试')}`)
  } finally {
    chunkLoading.value = false
  }
}

async function openOriginalPreview(row: DocumentItem) {
  previewDocument.value = row
  previewDrawerVisible.value = true
  previewLoading.value = true
  try {
    const preview = await getDocumentPreview(row.id, 200000)
    previewContent.value = preview
    setQualityPreview(row.id, preview)
  } catch (error: unknown) {
    previewContent.value = null
    ElMessage.error(`原文预览失败：${errorMessage(error, '请检查源文件是否仍然存在')}`)
  } finally {
    previewLoading.value = false
  }
}

onMounted(async () => {
  await load()
  const raw = Array.isArray(route.query.document) ? route.query.document[0] : route.query.document
  const documentId = Number(raw)
  if (Number.isFinite(documentId) && documentId > 0) {
    const row = documents.value.find((item) => item.id === documentId)
    if (row) await openOriginalPreview(row)
  }
})

onUnmounted(() => {
  for (const documentId of pollTimers.keys()) {
    clearPoll(documentId)
  }
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1 class="page-title">文档管理</h1>
        <p class="page-subtitle">上传源文档，查看解析质量，并将内容摄入 Wiki 供生成检索使用</p>
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
      accept=".md,.txt,.pdf,.docx"
    >
      <div class="upload-inner">
        <el-icon class="upload-icon" :size="36"><UploadFilled /></el-icon>
        <div class="upload-title">拖拽文件到此处，或点击上传</div>
        <div class="upload-hint">支持 md / txt / pdf / docx · 上传后自动检查解析质量，再点击「摄入 Wiki」</div>
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
      <el-table-column label="文档状态" width="110">
        <template #default="{ row }">
          <el-tag :type="statusTagType(row.status)" size="small" effect="light">
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="解析质量" min-width="220">
        <template #default="{ row }">
          <div class="quality-cell">
            <div class="quality-line">
              <el-tag :type="qualityTagType(row)" size="small" effect="light">
                {{ qualityLabel(row) }}
              </el-tag>
              <span class="char-count">{{ row.char_count || 0 }} 字符</span>
              <el-icon v-if="qualityLoading[row.id]" class="is-loading"><Refresh /></el-icon>
            </div>
            <el-popover placement="top-start" :width="340" trigger="click">
              <template #reference>
                <el-button type="primary" link size="small">查看质量详情</el-button>
              </template>
              <div class="quality-popover">
                <div class="quality-note">{{ qualityNote(row) }}</div>
                <div v-if="qualityPreview(row)" class="quality-detail">{{ qualityDetail(row) }}</div>
                <div v-if="diagnosticErrors(row).length" class="diagnostic-list error-text">
                  <div v-for="message in diagnosticErrors(row)" :key="message">错误：{{ message }}</div>
                </div>
                <div v-if="diagnosticWarnings(row).length" class="diagnostic-list warning-text">
                  <div v-for="message in diagnosticWarnings(row)" :key="message">提示：{{ message }}</div>
                </div>
                <div v-if="!qualityPreview(row) && qualityErrors[row.id]" class="error-text">
                  {{ qualityErrors[row.id] }}
                </div>
                <div v-if="!diagnosticErrors(row).length && !diagnosticWarnings(row).length && qualityPreview(row)" class="muted">
                  未发现解析诊断问题，可以继续摄入。
                </div>
              </div>
            </el-popover>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="摄入任务" min-width="270">
        <template #default="{ row }">
          <template v-if="currentJob(row)">
            <div class="job-line">
              <el-tag :type="jobStatusTagType(currentJob(row)?.status || '')" size="small" effect="plain">
                {{ jobStatusLabel(currentJob(row)?.status || '') }}
              </el-tag>
              <span class="job-id">#{{ currentJob(row)?.id }}</span>
              <span v-if="currentJob(row)?.cancel_requested" class="cancel-hint">取消中</span>
            </div>
            <div class="job-progress-meta">
              <span>{{ stageLabel(currentJob(row)?.stage || '') }}</span>
              <span>{{ progressValue(currentJob(row)?.progress || 0) }}%</span>
            </div>
            <el-progress
              :percentage="progressValue(currentJob(row)?.progress || 0)"
              :stroke-width="7"
              :show-text="false"
            />
            <div v-if="latestStep(currentJob(row))" class="job-step">
              {{ stepText(latestStep(currentJob(row))) }}
            </div>
            <el-popover v-if="parseStepLog(currentJob(row)).length" placement="top-start" :width="380" trigger="click">
              <template #reference>
                <el-button type="primary" link size="small">
                  查看步骤日志（{{ parseStepLog(currentJob(row)).length }}）
                </el-button>
              </template>
              <div class="step-log-popover">
                <div v-for="(step, index) in parseStepLog(currentJob(row))" :key="`${step.at || ''}-${index}`" class="step-item">
                  <div class="step-item-header">
                    <span>{{ stageLabel(String(step.step || '')) }}</span>
                    <span class="muted">{{ stepTime(step) }}</span>
                  </div>
                  <div>{{ stepText(step) }}</div>
                </div>
              </div>
            </el-popover>
            <div v-if="jobErrors[row.id] || jobErrorText(currentJob(row))" class="error-text job-error">
              {{ jobErrors[row.id] || jobErrorText(currentJob(row)) }}
            </div>
          </template>
          <span v-else-if="row.status === 'ready'" class="muted">已摄入 Wiki（历史任务）</span>
          <span v-else class="muted">尚未摄入 Wiki</span>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="165">
        <template #default="{ row }">
          {{ formatTime(row.updated_at) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="230">
        <template #default="{ row }">
          <div class="row-actions">
            <el-button type="primary" link :loading="previewDocument?.id === row.id && previewLoading" @click="openOriginalPreview(row)">
              原文预览
            </el-button>
            <el-button type="primary" link :loading="chunkDocument?.id === row.id && chunkLoading" @click="openChunks(row)">
              分块预览
            </el-button>
            <el-button
              v-if="isActiveJob(currentJob(row))"
              type="warning"
              link
              :loading="actionLoading[row.id]"
              @click="cancelJob(row)"
            >
              取消
            </el-button>
            <el-button
              v-else-if="currentJob(row)?.status === 'failed' || currentJob(row)?.status === 'cancelled'"
              type="danger"
              link
              :loading="actionLoading[row.id]"
              @click="startIngest(row, true)"
            >
              重试
            </el-button>
            <el-button
              v-else
              type="primary"
              link
              :loading="actionLoading[row.id]"
              :disabled="row.status === 'failed' && !row.char_count"
              @click="startIngest(row)"
            >
              摄入 Wiki
            </el-button>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <el-drawer
      v-model="previewDrawerVisible"
      :title="previewDocument ? `原文预览：${previewDocument.filename}` : '原文预览'"
      size="min(820px, 94vw)"
    >
      <div v-loading="previewLoading" class="original-preview">
        <el-alert
          v-if="previewContent"
          :title="previewContent.truncated ? `原文共 ${previewContent.char_count} 字符，当前展示前 ${previewContent.returned_chars} 字符` : `原文共 ${previewContent.char_count} 字符`"
          :type="previewContent.quality_ok ? 'success' : 'warning'"
          :closable="false"
          show-icon
        />
        <pre v-if="previewContent" class="original-text">{{ previewContent.text || '（未提取到正文）' }}</pre>
        <el-empty v-else-if="!previewLoading" description="原文预览不可用" :image-size="80" />
      </div>
    </el-drawer>

    <el-drawer
      v-model="chunkDrawerVisible"
      :title="chunkDocument ? `原文块预览：${chunkDocument.filename}` : '原文块预览'"
      size="min(720px, 92vw)"
    >
      <div v-loading="chunkLoading" class="chunk-drawer-body">
        <el-alert
          title="以下内容来自源文档分块，保留原文表述；解析质量详情请查看文档列表中的质量卡片。"
          type="info"
          :closable="false"
          show-icon
        />
        <el-empty v-if="!chunkLoading && !chunks.length" description="暂无原文块，请先摄入文档或点击刷新" :image-size="80" />
        <div v-else class="chunk-list">
          <div v-for="chunk in chunks" :key="chunk.id" class="chunk-card">
            <div class="chunk-header">
              <div class="chunk-title">#{{ chunk.chunk_index }} {{ chunk.title || '原文块' }}</div>
              <span class="chunk-range">字符 {{ chunk.start_char }}–{{ chunk.end_char }}</span>
            </div>
            <div v-if="chunk.section" class="chunk-section">{{ chunk.section }}</div>
            <div v-if="chunk.page_start != null" class="chunk-meta">
              页码：{{ chunk.page_start }}<span v-if="chunk.page_end != null && chunk.page_end !== chunk.page_start">–{{ chunk.page_end }}</span>
            </div>
            <div v-if="parseClauseIds(chunk.clause_ids_json).length" class="chunk-tags">
              <el-tag v-for="clause in parseClauseIds(chunk.clause_ids_json)" :key="clause" size="small" effect="plain">
                {{ clause }}
              </el-tag>
            </div>
            <pre class="chunk-text">{{ chunk.text || '（空块）' }}</pre>
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.original-preview {
  min-height: 240px;
}

.original-text {
  margin: 14px 0 0;
  padding: 16px;
  max-height: calc(100vh - 180px);
  overflow: auto;
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius-sm);
  background: var(--cg-surface-muted);
  color: var(--cg-text-secondary);
  font: 13px/1.75 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

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

.quality-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
}

.quality-line,
.job-line,
.job-progress-meta,
.row-actions,
.chunk-header,
.step-item-header {
  display: flex;
  align-items: center;
}

.quality-line,
.job-line {
  gap: 7px;
}

.char-count,
.job-id,
.chunk-range,
.chunk-meta {
  font-size: 12px;
  color: var(--cg-text-muted);
}

.quality-popover {
  font-size: 12px;
  line-height: 1.6;
}

.quality-note {
  color: var(--cg-text-secondary);
  margin-bottom: 6px;
}

.quality-detail {
  color: var(--cg-text);
  margin-bottom: 5px;
}

.diagnostic-list {
  margin-top: 4px;
}

.job-progress-meta {
  justify-content: space-between;
  margin-top: 4px;
  font-size: 12px;
  color: var(--cg-text-secondary);
}

.job-step,
.job-error {
  max-width: 250px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  margin-top: 3px;
}

.cancel-hint {
  color: var(--cg-warning, #e6a23c);
  font-size: 12px;
}

.step-log-popover {
  max-height: 300px;
  overflow-y: auto;
  font-size: 12px;
  line-height: 1.5;
}

.step-item {
  padding: 7px 0;
  border-bottom: 1px solid var(--cg-border);
}

.step-item:first-child {
  padding-top: 0;
}

.step-item:last-child {
  border-bottom: 0;
  padding-bottom: 0;
}

.step-item-header {
  justify-content: space-between;
  color: var(--cg-text);
  font-weight: 600;
}

.row-actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 8px;
  width: 100%;
}

.row-actions :deep(.el-button) {
  justify-content: flex-start;
  margin-left: 0;
  padding: 4px 0;
}

.muted {
  color: var(--cg-text-muted);
}

.error-text {
  color: var(--cg-danger, #f56c6c);
}

.warning-text {
  color: var(--cg-warning, #e6a23c);
}

.chunk-drawer-body {
  min-height: 180px;
}

.chunk-list {
  margin-top: 14px;
}

.chunk-card {
  padding: 14px;
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius-sm);
  margin-bottom: 12px;
  background: var(--cg-surface, #fff);
}

.chunk-header {
  justify-content: space-between;
  gap: 10px;
}

.chunk-title {
  min-width: 0;
  color: var(--cg-text);
  font-weight: 600;
}

.chunk-section,
.chunk-meta {
  margin-top: 5px;
}

.chunk-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 8px;
}

.chunk-text {
  margin: 10px 0 0;
  padding: 11px;
  border-radius: 6px;
  background: rgba(15, 23, 42, 0.035);
  color: var(--cg-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  font: inherit;
  line-height: 1.65;
}

@media (max-width: 960px) {
  .chunk-header {
    align-items: flex-start;
    flex-direction: column;
    gap: 3px;
  }
}
</style>
