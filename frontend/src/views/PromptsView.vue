<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument, InfoFilled, Lock, Plus } from '@element-plus/icons-vue'
import { ApiError } from '../api/client'
import {
  createPrompt,
  deletePrompt,
  forkPrompt,
  isEnginePromptType,
  listPrompts,
  PROMPT_TYPE_OPTIONS,
  updatePrompt,
  type PromptTemplate,
  type PromptVisibility,
} from '../api/prompts'
import { useAuthStore } from '../authStore'

type ScopeFilter = 'all' | 'system' | 'mine' | 'shared' | 'private'

const auth = useAuthStore()
const isAdmin = computed(() => auth.isAdmin)
const currentUserId = computed(() => auth.user?.id ?? null)
const currentUsername = computed(() => auth.user?.username ?? null)

const loading = ref(false)
const saving = ref(false)
const deleting = ref(false)
const forking = ref(false)
const prompts = ref<PromptTemplate[]>([])
const filterType = ref<string>('')
const scopeFilter = ref<ScopeFilter>('all')
const selectedId = ref<number | null>(null)
const conflictMessage = ref('')
const isCreateMode = ref(false)

const form = reactive({
  name: '',
  type: 'generate',
  visibility: 'private' as PromptVisibility,
  content: '',
  is_active: true,
})

function isOwnPrompt(row: PromptTemplate): boolean {
  if (row.owner_user_id != null && currentUserId.value != null) {
    return row.owner_user_id === currentUserId.value
  }
  if (row.owner_username && currentUsername.value) {
    return row.owner_username === currentUsername.value
  }
  return row.visibility === 'private'
}

function canEditPrompt(row: PromptTemplate | null): boolean {
  if (!row) return false
  if (typeof row.can_edit === 'boolean') return row.can_edit
  if (isAdmin.value) return row.visibility !== 'private' || isOwnPrompt(row)
  return row.type === 'generate' && isOwnPrompt(row)
}

function canDeletePrompt(row: PromptTemplate | null): boolean {
  if (!row) return false
  if (typeof row.can_delete === 'boolean') return row.can_delete
  return canEditPrompt(row)
}

function canForkPrompt(row: PromptTemplate | null): boolean {
  if (!row) return false
  if (typeof row.can_fork === 'boolean') return row.can_fork
  return row.type === 'generate'
}

function visibilityBadgeText(row: PromptTemplate): string {
  if (row.visibility === 'system') return '系统预置'
  if (row.visibility === 'shared') {
    return row.owner_username ? `团队共享 · @${row.owner_username}` : '团队共享'
  }
  return '我的私有'
}

function visibilityBadgeClass(visibility: PromptVisibility): string {
  if (visibility === 'system') return 'vis-badge--system'
  if (visibility === 'shared') return 'vis-badge--shared'
  return 'vis-badge--private'
}

function typeLabel(type: string): string {
  return PROMPT_TYPE_OPTIONS.find((o) => o.value === type)?.label ?? type
}

function shortTypeLabel(type: string): string {
  const full = typeLabel(type)
  return full.replace(/\s*\([^)]+\)\s*$/, '')
}

function matchesScope(row: PromptTemplate, scope: ScopeFilter): boolean {
  if (scope === 'all') return true
  if (scope === 'system') return row.visibility === 'system'
  if (scope === 'shared') return row.visibility === 'shared'
  if (scope === 'private') return row.visibility === 'private'
  return isOwnPrompt(row)
}

const scopeTabs = computed<Array<{ value: ScopeFilter; label: string; count: number }>>(() => {
  const rows = prompts.value
  if (isAdmin.value) {
    return [
      { value: 'all', label: '全部', count: rows.length },
      { value: 'system', label: '系统预置', count: rows.filter((r) => matchesScope(r, 'system')).length },
      { value: 'shared', label: '团队共享', count: rows.filter((r) => matchesScope(r, 'shared')).length },
      { value: 'private', label: '我的私有', count: rows.filter((r) => matchesScope(r, 'private')).length },
    ]
  }
  return [
    { value: 'all', label: '全部', count: rows.length },
    { value: 'system', label: '系统预置', count: rows.filter((r) => matchesScope(r, 'system')).length },
    { value: 'mine', label: '我的模板', count: rows.filter((r) => matchesScope(r, 'mine')).length },
    { value: 'shared', label: '团队共享', count: rows.filter((r) => matchesScope(r, 'shared')).length },
  ]
})

const filteredPrompts = computed(() =>
  prompts.value.filter((row) => matchesScope(row, scopeFilter.value)),
)

const selected = computed(() =>
  prompts.value.find((p) => p.id === selectedId.value) ?? null,
)

const canEditSelected = computed(() => canEditPrompt(selected.value))
const canDeleteSelected = computed(() => canDeletePrompt(selected.value))
const canForkSelected = computed(() => !isCreateMode.value && canForkPrompt(selected.value))
const isReadonly = computed(() => !isCreateMode.value && selected.value !== null && !canEditSelected.value)

const isCurrentEngineType = computed(() => isEnginePromptType(form.type))

const isGoverningOthersShared = computed(() =>
  Boolean(
    !isCreateMode.value &&
      isAdmin.value &&
      selected.value &&
      selected.value.visibility === 'shared' &&
      !isOwnPrompt(selected.value),
  ),
)

const visibilityOptions = computed<Array<{ value: PromptVisibility; label: string; disabled?: boolean }>>(() => {
  if (isCurrentEngineType.value) {
    return [{ value: 'system', label: '系统预置（引擎类锁定）', disabled: false }]
  }
  if (!isAdmin.value) {
    return [
      { value: 'private', label: '仅自己可见（私有）' },
      { value: 'shared', label: '团队共享' },
    ]
  }
  return [
    {
      value: 'system',
      label: '系统预置',
      disabled: isGoverningOthersShared.value,
    },
    { value: 'shared', label: '团队共享' },
    {
      value: 'private',
      label: '仅自己可见（私有）',
      disabled: isGoverningOthersShared.value,
    },
  ]
})

const pageSubtitle = computed(() =>
  isAdmin.value
    ? '管理系统预置、团队共享与个人生成模板，以及 6 类流水线引擎提示词；generate 支持多条启用，引擎类仅保留一条启用'
    : '查看系统预置与团队共享的生成提示词，或创建与管理个人专属生成模板',
)

const readonlyBannerTitle = computed(() => {
  if (!selected.value) return ''
  if (selected.value.visibility === 'system') {
    return '系统预置模板仅支持只读查看；点击右上角「复制为我的模板」可基于此模板创建个人副本并修改'
  }
  const author = selected.value.owner_username ? `（@${selected.value.owner_username}）` : ''
  return `团队共享模板${author}仅作者与管理员可编辑；点击右上角「复制为我的模板」可派生个人版本`
})

function syncFormFromPrompt(row: PromptTemplate) {
  form.name = row.name
  form.type = row.type
  form.visibility = isEnginePromptType(row.type) ? 'system' : (row.visibility || 'system')
  form.content = row.content
  form.is_active = row.is_active
}

function selectPrompt(row: PromptTemplate) {
  conflictMessage.value = ''
  isCreateMode.value = false
  selectedId.value = row.id
  syncFormFromPrompt(row)
}

function startCreate() {
  conflictMessage.value = ''
  isCreateMode.value = true
  selectedId.value = null
  form.name = ''
  const nextType = isAdmin.value ? (filterType.value || 'generate') : 'generate'
  form.type = nextType
  if (isEnginePromptType(nextType)) {
    form.visibility = 'system'
  } else if (!isAdmin.value) {
    form.visibility = scopeFilter.value === 'shared' ? 'shared' : 'private'
  } else if (scopeFilter.value === 'private') {
    form.visibility = 'private'
  } else if (scopeFilter.value === 'shared') {
    form.visibility = 'shared'
  } else {
    form.visibility = 'system'
  }
  form.content = ''
  form.is_active = true
}

function cancelCreate() {
  conflictMessage.value = ''
  isCreateMode.value = false
  if (filteredPrompts.value.length > 0) {
    selectPrompt(filteredPrompts.value[0])
  } else {
    selectedId.value = null
  }
}

async function load(
  preferId?: number | null,
  options?: { fallbackScope?: ScopeFilter },
) {
  loading.value = true
  try {
    const queryType = isAdmin.value ? (filterType.value || undefined) : 'generate'
    const rows = await listPrompts(queryType)
    prompts.value = isAdmin.value ? rows : rows.filter((row) => row.type === 'generate')

    if (isCreateMode.value) return

    const targetId = preferId ?? selectedId.value
    const exactMatch = targetId != null
      ? prompts.value.find((p) => p.id === targetId) ?? null
      : null

    if (exactMatch) {
      if (!matchesScope(exactMatch, scopeFilter.value) && options?.fallbackScope) {
        scopeFilter.value = options.fallbackScope
      }
      if (matchesScope(exactMatch, scopeFilter.value)) {
        selectPrompt(exactMatch)
        return
      }
    }

    if (filteredPrompts.value.length > 0) {
      selectPrompt(filteredPrompts.value[0])
    } else {
      selectedId.value = null
    }
  } catch (e) {
    ElMessage.error(`加载提示词失败：${(e as Error).message}`)
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!isCreateMode.value && !canEditSelected.value) return
  if (!form.name.trim() || !form.content.trim()) {
    ElMessage.warning('请填写名称与内容')
    return
  }
  if (isGoverningOthersShared.value && form.visibility !== 'shared') {
    ElMessage.warning('正在治理他人的团队共享模板，不可将其修改为私有或系统预置')
    return
  }

  saving.value = true
  conflictMessage.value = ''
  const resolvedVisibility: PromptVisibility = isEnginePromptType(form.type)
    ? 'system'
    : form.visibility

  try {
    if (isCreateMode.value) {
      const created = await createPrompt({
        name: form.name.trim(),
        type: isAdmin.value ? form.type : 'generate',
        content: form.content,
        is_active: form.is_active,
        visibility: resolvedVisibility,
      })
      ElMessage.success('已创建提示词')
      isCreateMode.value = false
      await load(created.id, { fallbackScope: 'all' })
    } else if (selectedId.value) {
      const updated = await updatePrompt(selectedId.value, {
        name: form.name.trim(),
        content: form.content,
        is_active: form.is_active,
        visibility: resolvedVisibility,
      })
      ElMessage.success('已保存')
      await load(updated.id, { fallbackScope: 'all' })
    }
  } catch (e) {
    const conflict = promptConflictMessage(e)
    if (conflict) {
      conflictMessage.value = conflict
      ElMessage.error(conflict)
      await load()
      return
    }
    ElMessage.error(`保存失败：${(e as Error).message}`)
  } finally {
    saving.value = false
  }
}

async function setActive() {
  if (!selectedId.value || !canEditSelected.value) return
  saving.value = true
  conflictMessage.value = ''
  try {
    const updated = await updatePrompt(selectedId.value, { is_active: true })
    ElMessage.success(selected.value?.type === 'generate' ? '已设为启用' : '已设为唯一启用')
    await load(updated.id)
  } catch (e) {
    const conflict = promptConflictMessage(e)
    if (conflict) {
      conflictMessage.value = conflict
      ElMessage.error(conflict)
      await load()
      return
    }
    ElMessage.error(`操作失败：${(e as Error).message}`)
  } finally {
    saving.value = false
  }
}

async function handleFork() {
  const current = selected.value
  if (!current || !canForkPrompt(current)) return
  forking.value = true
  conflictMessage.value = ''
  try {
    const forked = await forkPrompt(current.id, { visibility: 'private' })
    ElMessage.success(`已复制为个人模板「${forked.name}」`)
    await load(forked.id, { fallbackScope: isAdmin.value ? 'private' : 'mine' })
  } catch (e) {
    ElMessage.error(`复制模板失败：${(e as Error).message}`)
  } finally {
    forking.value = false
  }
}

function promptConflictMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 409) {
    return error.message || '该类型已有启用中的提示词。请先停用同类型的其他模板，再启用当前模板。'
  }
  return ''
}

async function removeSelected() {
  const current = selected.value
  if (!current || !canDeletePrompt(current)) return
  try {
    await ElMessageBox.confirm(
      `确认删除提示词「${current.name}」v${current.version}？此操作不可恢复。${current.is_active ? '当前提示词处于启用状态。' : ''}`,
      '删除提示词',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  deleting.value = true
  try {
    await deletePrompt(current.id)
    selectedId.value = null
    ElMessage.success('提示词已删除')
    await load()
  } catch (error) {
    ElMessage.error(`删除失败：${(error as Error).message}`)
  } finally {
    deleting.value = false
  }
}

watch(
  () => form.type,
  (nextType) => {
    if (isEnginePromptType(nextType)) {
      form.visibility = 'system'
    }
  },
)

watch(filterType, () => {
  if (!isAdmin.value) return
  selectedId.value = null
  isCreateMode.value = false
  void load()
})

watch(scopeFilter, () => {
  if (isCreateMode.value) return
  if (selectedId.value && filteredPrompts.value.some((p) => p.id === selectedId.value)) {
    return
  }
  if (filteredPrompts.value.length > 0) {
    selectPrompt(filteredPrompts.value[0])
  } else {
    selectedId.value = null
  }
})

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="page prompts-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">提示词管理</h1>
        <p class="page-subtitle">{{ pageSubtitle }}</p>
      </div>
      <div class="page-actions">
        <el-select
          v-if="isAdmin"
          v-model="filterType"
          clearable
          placeholder="按类型筛选（全部）"
          style="width: 240px"
        >
          <el-option
            v-for="opt in PROMPT_TYPE_OPTIONS"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </el-select>
        <el-button type="primary" :icon="Plus" @click="startCreate">
          {{ isAdmin ? '新建提示词' : '新建生成模板' }}
        </el-button>
      </div>
    </div>

    <div v-if="isAdmin" class="privacy-notice">
      <el-icon class="privacy-notice-icon"><InfoFilled /></el-icon>
      <span>普通用户的私有模板仅作者本人可见</span>
    </div>

    <div class="layout" v-loading="loading">
      <div class="list-panel panel">
        <div class="scope-tabs" role="tablist" aria-label="模板范围筛选">
          <button
            v-for="tab in scopeTabs"
            :key="tab.value"
            type="button"
            role="tab"
            class="scope-tab"
            :class="{ active: scopeFilter === tab.value }"
            :aria-selected="scopeFilter === tab.value"
            @click="scopeFilter = tab.value"
          >
            <span>{{ tab.label }}</span>
            <span class="scope-count">{{ tab.count }}</span>
          </button>
        </div>

        <div v-if="filteredPrompts.length" class="prompt-card-list">
          <button
            v-for="row in filteredPrompts"
            :key="row.id"
            type="button"
            class="prompt-card"
            :class="{ selected: !isCreateMode && selectedId === row.id }"
            @click="selectPrompt(row)"
          >
            <div class="prompt-card-top">
              <span class="prompt-card-name" :title="row.name">{{ row.name }}</span>
              <el-tag
                :type="row.is_active ? 'success' : 'info'"
                size="small"
                :effect="row.is_active ? 'light' : 'plain'"
              >
                {{ row.is_active ? '启用' : '停用' }}
              </el-tag>
            </div>
            <div class="prompt-card-badges">
              <span class="vis-badge" :class="visibilityBadgeClass(row.visibility)">
                {{ visibilityBadgeText(row) }}
              </span>
              <span v-if="isAdmin" class="type-pill">{{ shortTypeLabel(row.type) }}</span>
              <span class="ver-pill">v{{ row.version }}</span>
            </div>
          </button>
        </div>
        <el-empty v-else description="当前筛选下暂无提示词" :image-size="60" />
      </div>

      <div class="editor-panel panel panel-surface">
        <template v-if="isCreateMode || selected">
          <div class="editor-header">
            <div class="editor-heading">
              <div class="editor-title-row">
                <span class="editor-title-text">
                  {{
                    isCreateMode
                      ? (isAdmin ? '新建提示词' : '新建个人生成模板')
                      : (isReadonly ? selected?.name : `编辑：${selected?.name}`)
                  }}
                </span>
                <span
                  v-if="!isCreateMode && selected"
                  class="vis-badge"
                  :class="visibilityBadgeClass(selected.visibility)"
                >
                  {{ visibilityBadgeText(selected) }}
                </span>
                <span v-if="isReadonly" class="readonly-pill">
                  <el-icon><Lock /></el-icon>
                  只读
                </span>
              </div>
              <div v-if="!isCreateMode && selected" class="meta">
                v{{ selected.version }} · {{ typeLabel(selected.type) }}
              </div>
            </div>

            <div v-if="canForkSelected" class="editor-header-actions">
              <el-button
                :type="isReadonly ? 'primary' : 'default'"
                :plain="!isReadonly"
                :icon="CopyDocument"
                :loading="forking"
                :disabled="saving || deleting"
                @click="handleFork"
              >
                复制为我的模板
              </el-button>
            </div>
          </div>

          <el-alert
            v-if="isReadonly"
            :title="readonlyBannerTitle"
            type="info"
            show-icon
            :closable="false"
            class="editor-alert"
          />

          <el-alert
            v-if="conflictMessage"
            :title="conflictMessage"
            type="warning"
            show-icon
            :closable="false"
            class="editor-alert"
          />

          <el-form label-width="84px" class="prompt-form">
            <el-form-item label="名称" :required="!isReadonly">
              <el-input
                v-model="form.name"
                :readonly="isReadonly"
                :class="{ 'is-readonly-input': isReadonly }"
                placeholder="请输入清晰易辨识的模板名称"
              />
            </el-form-item>

            <el-form-item v-if="isAdmin" label="类型" :required="isCreateMode">
              <el-select v-model="form.type" :disabled="!isCreateMode" style="width: 100%">
                <el-option
                  v-for="opt in PROMPT_TYPE_OPTIONS"
                  :key="opt.value"
                  :label="opt.label"
                  :value="opt.value"
                />
              </el-select>
            </el-form-item>

            <el-form-item label="可见性" :required="!isReadonly">
              <div class="visibility-field">
                <template v-if="isReadonly && selected">
                  <span class="vis-badge" :class="visibilityBadgeClass(selected.visibility)">
                    {{ visibilityBadgeText(selected) }}
                  </span>
                </template>
                <template v-else>
                  <el-radio-group
                    v-model="form.visibility"
                    :disabled="isCurrentEngineType"
                  >
                    <el-radio
                      v-for="opt in visibilityOptions"
                      :key="opt.value"
                      :value="opt.value"
                      :disabled="opt.disabled"
                    >
                      {{ opt.label }}
                    </el-radio>
                  </el-radio-group>
                  <div v-if="isCurrentEngineType" class="field-hint">
                    引擎类提示词固定为「系统预置」，仅管理员可创建与维护。
                  </div>
                  <div v-else-if="isGoverningOthersShared" class="field-hint">
                    正在治理他人的团队共享模板，不可将其修改为私有或系统预置。
                  </div>
                </template>
              </div>
            </el-form-item>

            <el-form-item label="内容" :required="!isReadonly">
              <el-input
                v-model="form.content"
                type="textarea"
                :rows="16"
                :readonly="isReadonly"
                :class="{ 'is-readonly-input': isReadonly }"
                placeholder="提示词正文（支持 Markdown）"
              />
            </el-form-item>

            <el-form-item label="启用状态">
              <template v-if="isReadonly && selected">
                <el-tag :type="selected.is_active ? 'success' : 'info'" size="small">
                  {{ selected.is_active ? '启用中' : '已停用' }}
                </el-tag>
              </template>
              <el-switch v-else v-model="form.is_active" />
            </el-form-item>

            <el-form-item v-if="!isReadonly">
              <el-button type="primary" :loading="saving" :disabled="forking || deleting" @click="save">
                {{ isCreateMode ? '创建模板' : '保存修改' }}
              </el-button>
              <el-button
                v-if="isCreateMode"
                :disabled="saving"
                @click="cancelCreate"
              >
                取消
              </el-button>
              <el-button
                v-if="selected && !selected.is_active && canEditSelected"
                :loading="saving"
                :disabled="forking || deleting"
                @click="setActive"
              >
                设为启用
              </el-button>
              <el-button
                v-if="selected && canDeleteSelected"
                type="danger"
                plain
                :loading="deleting"
                :disabled="saving || forking"
                @click="removeSelected"
              >
                删除
              </el-button>
            </el-form-item>
          </el-form>
        </template>
        <el-empty v-else description="选择左侧提示词查看详情，或新建一条模板" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.privacy-notice {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 14px;
  padding: 6px 12px;
  border-radius: var(--cg-radius-sm);
  border: 1px solid var(--cg-border);
  background: var(--cg-surface-muted);
  color: var(--cg-text-secondary);
  font-size: 12.5px;
}

.privacy-notice-icon {
  color: var(--cg-accent-brand-deep);
  font-size: 14px;
}

.layout {
  display: grid;
  grid-template-columns: minmax(300px, 1fr) minmax(420px, 1.45fr);
  gap: 16px;
  min-height: 540px;
}

.list-panel,
.editor-panel {
  min-height: 520px;
}

.list-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.scope-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.scope-tab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 10px;
  border: 1px solid var(--cg-border);
  border-radius: 999px;
  background: var(--cg-surface);
  color: var(--cg-text-secondary);
  font: inherit;
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
}

.scope-tab:hover {
  border-color: var(--cg-accent-brand);
  color: var(--cg-text);
}

.scope-tab.active {
  background: #09090b;
  border-color: #09090b;
  color: #ffffff;
}

.scope-count {
  font-family: var(--cg-font-mono);
  font-size: 11px;
  padding: 0 5px;
  border-radius: 999px;
  background: rgba(113, 113, 122, 0.14);
  color: inherit;
}

.scope-tab.active .scope-count {
  background: rgba(16, 185, 129, 0.25);
  color: #a7f3d0;
}

.prompt-card-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 620px;
  overflow-y: auto;
  padding-right: 2px;
}

.prompt-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  padding: 11px 12px;
  border: 1px solid var(--cg-border);
  border-left: 3px solid transparent;
  border-radius: var(--cg-radius-sm);
  background: var(--cg-surface);
  text-align: left;
  font: inherit;
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease, box-shadow 0.15s ease;
}

.prompt-card:hover {
  border-color: #cbd5e1;
  background: #ffffff;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.04);
}

.prompt-card.selected {
  border-color: #18181b;
  border-left-color: #10b981;
  background: #f8fafc;
}

.prompt-card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
}

.prompt-card-name {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--cg-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.prompt-card-badges {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.vis-badge {
  display: inline-flex;
  align-items: center;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 600;
  line-height: 18px;
}

.vis-badge--system {
  background: #18181b;
  color: #f4f4f5;
}

.vis-badge--shared {
  background: #ecfdf5;
  border: 1px solid #a7f3d0;
  color: #065f46;
}

.vis-badge--private {
  background: #f4f4f5;
  border: 1px solid #e4e4e7;
  color: #3f3f46;
}

.type-pill {
  font-size: 11.5px;
  padding: 1px 7px;
  border-radius: 4px;
  background: #f4f4f5;
  color: var(--cg-text-secondary);
}

.ver-pill {
  margin-left: auto;
  font-family: var(--cg-font-mono);
  font-size: 11.5px;
  color: var(--cg-text-muted);
}

.editor-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--cg-border);
  flex-wrap: wrap;
}

.editor-heading {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.editor-title-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.editor-title-text {
  font-size: 16px;
  font-weight: 700;
  color: var(--cg-text);
}

.readonly-pill {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 7px;
  border-radius: 999px;
  background: #fef3c7;
  color: #92400e;
  font-size: 11.5px;
  font-weight: 600;
}

.editor-header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.editor-alert {
  margin-bottom: 14px;
}

.meta {
  color: var(--cg-text-muted);
  font-size: 12.5px;
  font-family: var(--cg-font-mono);
}

.visibility-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
}

.field-hint {
  font-size: 12px;
  color: var(--cg-text-muted);
  line-height: 1.5;
}

.is-readonly-input :deep(.el-input__wrapper),
.is-readonly-input :deep(.el-textarea__inner) {
  background-color: #f8fafc;
  color: var(--cg-text-secondary);
}

@media (max-width: 960px) {
  .layout {
    grid-template-columns: 1fr;
  }

  .prompt-card-list {
    max-height: 360px;
  }
}
</style>
