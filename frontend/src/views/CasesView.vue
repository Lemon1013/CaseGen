<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Clock, Download, Refresh } from '@element-plus/icons-vue'
import { api, apiBlob } from '../api/client'
import {
  archiveCase,
  casesExportUrl,
  listCases,
  listCaseLogs,
  restoreCase,
  updateCase,
  type TestCaseItem,
  type TestCaseOperationLog,
} from '../api/cases'
import { formatDateTime as formatTime } from '../utils/datetime'

interface RequirementSummary {
  id: number
  title: string
  description: string
}

const loading = ref(false)
const cases = ref<TestCaseItem[]>([])
const requirements = ref<RequirementSummary[]>([])
const includeArchived = ref(false)
const keyword = ref('')
const statusFilter = ref<'active' | 'archived' | ''>('')
const selectedIds = ref<Set<number>>(new Set())
const selected = computed(() => cases.value.filter((row) => selectedIds.value.has(row.id)))
const editing = ref<TestCaseItem | null>(null)
const editContent = ref('')
const editTitle = ref('')
const saving = ref(false)
const logs = ref<TestCaseOperationLog[]>([])
const logCase = ref<TestCaseItem | null>(null)
const logsLoading = ref(false)
const logsVisible = computed({
  get: () => logCase.value !== null,
  set: (visible: boolean) => {
    if (!visible) logCase.value = null
  },
})
const editorVisible = computed({
  get: () => editing.value !== null,
  set: (visible: boolean) => {
    if (!visible) editing.value = null
  },
})

const requirementTitle = (id: number) =>
  requirements.value.find((row) => row.id === id)?.title || `需求 #${id}`

const groupedCases = computed(() => {
  const groups = new Map<number, TestCaseItem[]>()
  for (const row of cases.value) {
    const group = groups.get(row.requirement_id) || []
    group.push(row)
    groups.set(row.requirement_id, group)
  }
  return [...groups.entries()].sort((a, b) => a[0] - b[0])
})

async function load() {
  loading.value = true
  try {
    const [caseRows, reqRows] = await Promise.all([
      listCases({
        include_archived: includeArchived.value,
        keyword: keyword.value,
        status: statusFilter.value,
      }),
      api<RequirementSummary[]>('/api/requirements'),
    ])
    cases.value = caseRows
    requirements.value = reqRows
    selectedIds.value = new Set([...selectedIds.value].filter((id) => caseRows.some((item) => item.id === id)))
  } catch (err) {
    ElMessage.error(`加载用例失败：${(err as Error).message}`)
  } finally {
    loading.value = false
  }
}

async function openLogs(row: TestCaseItem) {
  logCase.value = row
  logsLoading.value = true
  try {
    logs.value = await listCaseLogs(row.id)
  } catch (err) {
    logs.value = []
    ElMessage.error(`加载操作日志失败：${(err as Error).message}`)
  } finally {
    logsLoading.value = false
  }
}

function openEditor(row: TestCaseItem) {
  editing.value = row
  editTitle.value = row.title
  editContent.value = row.content_md
}

async function saveEditor() {
  if (!editing.value) return
  if (!editContent.value.trim()) {
    ElMessage.warning('用例内容不能为空')
    return
  }
  saving.value = true
  try {
    const updated = await updateCase(editing.value.id, {
      title: editTitle.value,
      content_md: editContent.value,
      expected_revision: editing.value.revision,
    })
    const index = cases.value.findIndex((item) => item.id === updated.id)
    if (index >= 0) cases.value[index] = updated
    editing.value = null
    ElMessage.success('已保存当前用例，操作日志已记录')
  } catch (err) {
    ElMessage.error(`保存失败：${(err as Error).message}`)
    await load()
  } finally {
    saving.value = false
  }
}

async function toggleArchive(row: TestCaseItem) {
  try {
    await ElMessageBox.confirm(
      row.status === 'archived' ? '恢复这条用例？' : '归档这条用例？',
      row.status === 'archived' ? '恢复确认' : '归档确认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    const updated = row.status === 'archived'
      ? await restoreCase(row.id, row.revision)
      : await archiveCase(row.id, row.revision)
    const index = cases.value.findIndex((item) => item.id === updated.id)
    if (index >= 0) {
      cases.value[index] = updated
      if (!includeArchived.value && updated.status === 'archived') cases.value.splice(index, 1)
    }
    ElMessage.success(updated.status === 'archived' ? '已归档' : '已恢复')
  } catch (err) {
    ElMessage.error(`操作失败：${(err as Error).message}`)
    await load()
  }
}

async function downloadExport(url: string) {
  const blob = await apiBlob(url)
  const objectUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = objectUrl
  anchor.download = 'casegen-cases.md'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(objectUrl)
}

async function exportSelected() {
  if (!selected.value.length) {
    ElMessage.warning('请先勾选要导出的用例')
    return
  }
  try {
    await downloadExport(casesExportUrl({ ids: selected.value.map((row) => row.id) }))
  } catch (err) {
    ElMessage.error(`导出失败：${(err as Error).message}`)
  }
}

async function exportRequirement(requirementId: number) {
  try {
    await downloadExport(casesExportUrl({ requirement_id: requirementId }))
  } catch (err) {
    ElMessage.error(`导出失败：${(err as Error).message}`)
  }
}

function onSelectionChange(groupRows: TestCaseItem[], rows: TestCaseItem[]) {
  const next = new Set(selectedIds.value)
  for (const row of groupRows) next.delete(row.id)
  for (const row of rows) next.add(row.id)
  selectedIds.value = next
}

onMounted(load)
</script>

<template>
  <div class="page cases-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">用例管理</h1>
        <p class="page-subtitle">按需求查看已入库用例，编辑当前内容并导出 Markdown</p>
      </div>
      <div class="page-actions">
        <el-input
          v-model="keyword"
          clearable
          placeholder="搜索编号、标题、正文或需求"
          style="width: 230px"
          @keyup.enter="load"
        />
        <el-select v-model="statusFilter" clearable placeholder="状态" style="width: 120px" @change="load">
          <el-option label="当前" value="active" />
          <el-option label="已归档" value="archived" />
        </el-select>
        <el-checkbox v-model="includeArchived" @change="load">显示已归档</el-checkbox>
        <el-button :icon="Download" :disabled="!selected.length" @click="exportSelected">导出勾选</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="!loading && !cases.length"
      type="info"
      show-icon
      :closable="false"
      title="暂无已入库用例"
      description="在任务详情选择具体草稿并定稿后，用例会按 ## TC-xxx 拆分进入这里。"
      style="margin-bottom: 16px"
    />

    <el-card v-for="[requirementId, rows] in groupedCases" :key="requirementId" shadow="never" class="case-group">
      <template #header>
        <div class="group-header">
          <div>
            <strong>{{ requirementTitle(requirementId) }}</strong>
            <span class="group-count">{{ rows.length }} 条用例</span>
          </div>
          <el-button link type="primary" @click="exportRequirement(requirementId)">导出需求</el-button>
        </div>
      </template>
      <el-table :data="rows" stripe @selection-change="onSelectionChange(rows, $event)">
        <el-table-column type="selection" width="48" />
        <el-table-column prop="case_key" label="编号" width="130" />
        <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'archived' ? 'info' : 'success'" size="small">
              {{ row.status === 'archived' ? '已归档' : '当前' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源" min-width="150">
          <template #default="{ row }">
            <span v-if="row.source_task_id">任务 #{{ row.source_task_id }} · v{{ row.source_draft_id || '-' }}</span>
            <span v-else class="muted">手工创建</span>
          </template>
        </el-table-column>
        <el-table-column label="更新" width="170">
          <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEditor(row)">编辑</el-button>
            <el-button link type="info" :icon="Clock" @click="openLogs(row)">日志</el-button>
            <el-button link type="warning" @click="toggleArchive(row)">
              {{ row.status === 'archived' ? '恢复' : '归档' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="editorVisible" title="编辑当前用例" width="760px" destroy-on-close>
      <el-form label-width="72px" @submit.prevent>
        <el-form-item label="标题"><el-input v-model="editTitle" /></el-form-item>
        <el-form-item label="Markdown"><el-input v-model="editContent" type="textarea" :rows="18" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editing = null">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveEditor">保存并记录差异</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="logsVisible" title="操作日志（仅摘要）" width="820px" destroy-on-close>
      <el-skeleton v-if="logsLoading" :rows="4" animated />
      <el-table v-else :data="logs" stripe max-height="460">
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="operation" label="操作" width="100" />
        <el-table-column prop="operator" label="操作者" width="120" />
        <el-table-column label="差异摘要" min-width="250">
          <template #default="{ row }">
            <span>{{ row.diff_summary || '无正文变化' }}</span>
            <span v-if="row.changed_fields.length" class="log-fields">（{{ row.changed_fields.join('、') }}）</span>
          </template>
        </el-table-column>
        <el-table-column prop="reason" label="原因" min-width="160" show-overflow-tooltip />
      </el-table>
      <el-empty v-if="!logsLoading && !logs.length" description="暂无操作日志" />
    </el-dialog>
  </div>
</template>

<style scoped>
.case-group {
  margin-bottom: 16px;
  border-radius: var(--cg-radius) !important;
}

.group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.group-count {
  margin-left: 10px;
  color: var(--cg-text-muted);
  font-size: 12px;
}

.muted {
  color: var(--cg-text-muted);
}

.log-fields {
  color: var(--cg-text-muted);
  font-size: 12px;
}
</style>
