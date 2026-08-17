<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, ArrowRight, Clock, Download, Refresh } from '@element-plus/icons-vue'
import MarkdownView from '../components/MarkdownView.vue'
import { api, apiBlob } from '../api/client'
import {
  archiveCase,
  casesExportUrl,
  getCase,
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
const priorityFilter = ref<'P0' | 'P1' | 'P2' | ''>('')
const selectedIds = ref<Set<number>>(new Set())
const collapsedRequirementIds = ref<Set<number>>(new Set())
const selected = computed(() => cases.value.filter((row) => selectedIds.value.has(row.id)))
const editing = ref<TestCaseItem | null>(null)
const editContent = ref('')
const editTitle = ref('')
const editPriority = ref<'P0' | 'P1' | 'P2'>('P1')
const saving = ref(false)
const logs = ref<TestCaseOperationLog[]>([])
const logCase = ref<TestCaseItem | null>(null)
const logsLoading = ref(false)
const detailCase = ref<TestCaseItem | null>(null)
const detailLoading = ref(false)
const detailVisible = computed({
  get: () => detailCase.value !== null,
  set: (visible: boolean) => {
    if (!visible) detailCase.value = null
  },
})
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

const allGroupsCollapsed = computed(
  () => groupedCases.value.length > 0
    && groupedCases.value.every(([requirementId]) => collapsedRequirementIds.value.has(requirementId)),
)

function isGroupCollapsed(requirementId: number) {
  return collapsedRequirementIds.value.has(requirementId)
}

function toggleGroup(requirementId: number) {
  const next = new Set(collapsedRequirementIds.value)
  if (next.has(requirementId)) next.delete(requirementId)
  else next.add(requirementId)
  collapsedRequirementIds.value = next
}

function collapseAllGroups() {
  collapsedRequirementIds.value = new Set(
    groupedCases.value.map(([requirementId]) => requirementId),
  )
}

function expandAllGroups() {
  collapsedRequirementIds.value = new Set()
}

async function load() {
  loading.value = true
  try {
    const [caseRows, reqRows] = await Promise.all([
      listCases({
        include_archived: includeArchived.value,
        keyword: keyword.value,
        status: statusFilter.value,
        priority: priorityFilter.value,
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

async function openDetail(row: TestCaseItem) {
  detailCase.value = row
  detailLoading.value = true
  try {
    detailCase.value = await getCase(row.id)
  } catch (err) {
    ElMessage.error(`加载用例详情失败：${(err as Error).message}`)
  } finally {
    detailLoading.value = false
  }
}

function onRowClick(row: TestCaseItem, column: { type?: string; label?: string }) {
  if (column?.type === 'selection' || column?.label === '操作') return
  void openDetail(row)
}

function editFromDetail() {
  if (!detailCase.value) return
  openEditor(detailCase.value)
}

function logsFromDetail() {
  if (!detailCase.value) return
  void openLogs(detailCase.value)
}

function openEditor(row: TestCaseItem) {
  editing.value = row
  editTitle.value = row.title
  editPriority.value = (row.priority === 'P0' || row.priority === 'P2') ? row.priority : 'P1'
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
      priority: editPriority.value,
      expected_revision: editing.value.revision,
    })
    const index = cases.value.findIndex((item) => item.id === updated.id)
    if (index >= 0) cases.value[index] = updated
    if (detailCase.value?.id === updated.id) detailCase.value = updated
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
    if (detailCase.value?.id === updated.id) detailCase.value = updated
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
        <el-select v-model="priorityFilter" clearable placeholder="优先级" style="width: 120px" @change="load">
          <el-option label="P0 高风险" value="P0" />
          <el-option label="P1 主要" value="P1" />
          <el-option label="P2 补充" value="P2" />
        </el-select>
        <el-checkbox v-model="includeArchived" @change="load">显示已归档</el-checkbox>
        <el-button
          :disabled="!groupedCases.length"
          @click="allGroupsCollapsed ? expandAllGroups() : collapseAllGroups()"
        >
          {{ allGroupsCollapsed ? '全部展开' : '全部折叠' }}
        </el-button>
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
          <button
            type="button"
            class="group-toggle"
            :aria-expanded="!isGroupCollapsed(requirementId)"
            @click="toggleGroup(requirementId)"
          >
            <el-icon class="group-toggle-icon">
              <ArrowRight v-if="isGroupCollapsed(requirementId)" />
              <ArrowDown v-else />
            </el-icon>
            <strong>{{ requirementTitle(requirementId) }}</strong>
            <span class="group-count">{{ rows.length }} 条用例</span>
          </button>
          <div class="group-actions">
            <span v-if="isGroupCollapsed(requirementId)" class="collapsed-hint">点击展开</span>
            <el-button link type="primary" @click.stop="exportRequirement(requirementId)">导出需求</el-button>
          </div>
        </div>
      </template>
      <el-table
        v-show="!isGroupCollapsed(requirementId)"
        :data="rows"
        stripe
        class="case-table"
        @row-click="onRowClick"
        @selection-change="onSelectionChange(rows, $event)"
      >
        <el-table-column type="selection" width="48" />
        <el-table-column prop="case_key" label="编号" width="130">
          <template #default="{ row }">
            <el-button link type="primary" class="case-link" @click.stop="openDetail(row)">
              {{ row.case_key }}
            </el-button>
          </template>
        </el-table-column>
        <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <el-button link class="case-title-link" @click.stop="openDetail(row)">
              {{ row.title }}
            </el-button>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'archived' ? 'info' : 'success'" size="small">
              {{ row.status === 'archived' ? '已归档' : '当前' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="优先级" width="90">
          <template #default="{ row }">
            <el-tag :type="row.priority === 'P0' ? 'danger' : row.priority === 'P1' ? 'warning' : 'info'" size="small">
              {{ row.priority || 'P1' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源" min-width="150">
          <template #default="{ row }">
            <span v-if="row.source_task_id">
              任务 #{{ row.source_task_id }} · 草稿 v{{ row.source_draft_version || '-' }}
            </span>
            <span v-else class="muted">手工创建</span>
          </template>
        </el-table-column>
        <el-table-column label="更新" width="170">
          <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openEditor(row)">编辑</el-button>
            <el-button link type="info" :icon="Clock" @click.stop="openLogs(row)">日志</el-button>
            <el-button link type="warning" @click.stop="toggleArchive(row)">
              {{ row.status === 'archived' ? '恢复' : '归档' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-drawer
      v-model="detailVisible"
      size="min(920px, 82vw)"
      destroy-on-close
      class="case-detail-drawer"
    >
      <template #header>
        <div v-if="detailCase" class="detail-header">
          <div>
            <div class="detail-key">{{ detailCase.case_key }}</div>
            <h2>{{ detailCase.title }}</h2>
          </div>
          <el-tag :type="detailCase.status === 'archived' ? 'info' : 'success'">
            {{ detailCase.status === 'archived' ? '已归档' : '当前' }}
          </el-tag>
        </div>
      </template>

      <el-skeleton v-if="detailLoading" :rows="8" animated />
      <template v-else-if="detailCase">
        <el-descriptions :column="2" border class="detail-meta">
          <el-descriptions-item label="所属需求" :span="2">
            {{ requirementTitle(detailCase.requirement_id) }}
          </el-descriptions-item>
          <el-descriptions-item label="当前修订">v{{ detailCase.revision }}</el-descriptions-item>
          <el-descriptions-item label="优先级">
            <el-tag :type="detailCase.priority === 'P0' ? 'danger' : detailCase.priority === 'P1' ? 'warning' : 'info'" size="small">
              {{ detailCase.priority || 'P1' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="来源">
            <router-link v-if="detailCase.source_task_id" :to="`/tasks/${detailCase.source_task_id}`">
              任务 #{{ detailCase.source_task_id }} · 草稿 v{{ detailCase.source_draft_version || '-' }}
            </router-link>
            <span v-else>手工创建</span>
          </el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ formatTime(detailCase.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="更新时间">{{ formatTime(detailCase.updated_at) }}</el-descriptions-item>
        </el-descriptions>

        <div class="detail-actions">
          <el-button type="primary" @click="editFromDetail">编辑当前用例</el-button>
          <el-button :icon="Clock" @click="logsFromDetail">查看操作日志</el-button>
        </div>

        <el-divider content-position="left">用例内容</el-divider>
        <div class="detail-content">
          <MarkdownView :content="detailCase.content_md" />
        </div>
      </template>
    </el-drawer>

    <el-dialog v-model="editorVisible" title="编辑当前用例" width="760px" destroy-on-close>
      <el-form label-width="72px" @submit.prevent>
        <el-form-item label="标题"><el-input v-model="editTitle" /></el-form-item>
        <el-form-item label="优先级">
          <el-radio-group v-model="editPriority">
            <el-radio value="P0">P0</el-radio>
            <el-radio value="P1">P1</el-radio>
            <el-radio value="P2">P2</el-radio>
          </el-radio-group>
        </el-form-item>
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

.group-toggle {
  display: inline-flex;
  align-items: center;
  min-width: 0;
  padding: 4px 0;
  border: 0;
  color: inherit;
  background: transparent;
  cursor: pointer;
  font: inherit;
  text-align: left;
}

.group-toggle:hover strong {
  color: var(--el-color-primary);
}

.group-toggle:focus-visible {
  border-radius: 4px;
  outline: 2px solid var(--el-color-primary-light-5);
  outline-offset: 3px;
}

.group-toggle-icon {
  flex: 0 0 auto;
  margin-right: 8px;
  color: var(--cg-text-muted);
}

.group-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.collapsed-hint {
  color: var(--cg-text-muted);
  font-size: 12px;
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

.detail-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding-right: 12px;
}

.detail-header h2 {
  margin: 4px 0 0;
  color: var(--cg-text);
  font-size: 20px;
  line-height: 1.4;
}

.detail-key {
  color: var(--el-color-primary);
  font-size: 13px;
  font-weight: 700;
}

.detail-meta {
  margin-bottom: 16px;
}

.detail-actions {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

.detail-content {
  padding: 4px 8px 24px;
}
</style>
