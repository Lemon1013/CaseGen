<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { createProject, deleteProject, updateProject, type Project } from '../api/projects'
import {
  createKnowledgeSource,
  deleteKnowledgeSource,
  listKnowledgeProviderTypes,
  listKnowledgeSources,
  testKnowledgeSource,
  updateKnowledgeSource,
  type KnowledgeSourceOut,
  type ProviderFieldSpec,
  type ProviderTypeInfo,
  type RemoteScope,
} from '../api/knowledgeSources'
import { useAuthStore } from '../authStore'
import { useProjectStore } from '../projectStore'

const router = useRouter()
const auth = useAuthStore()
const projects = useProjectStore()
const isAdmin = computed(() => auth.isAdmin)

const dialog = ref(false)
const editing = ref<Project | null>(null)
const saving = ref(false)
const publicSavingId = ref<number | null>(null)
const form = reactive({ name: '', slug: '', description: '' })

// 1:N 多异构外部知识源管理抽屉状态
const sourceDrawer = ref(false)
const currentSourceProject = ref<Project | null>(null)
const sourcesLoading = ref(false)
const sourceSaving = ref(false)
const testingConnection = ref(false)
const togglingSourceId = ref<number | null>(null)
const deletingSourceId = ref<number | null>(null)
const providerTypes = ref<ProviderTypeInfo[]>([])
const knowledgeSources = ref<KnowledgeSourceOut[]>([])
const editingSource = ref<KnowledgeSourceOut | null>(null)
const remoteScopes = ref<RemoteScope[]>([])

const sourceForm = reactive({
  name: '外部知识库',
  provider_type: 'llm_wiki_django',
  base_url: 'http://127.0.0.1:8091',
  auth_type: 'none',
  config: {} as Record<string, unknown>,
  credentials: {} as Record<string, unknown>,
  weight: 1.0,
  priority: 100,
  enabled: true,
})

const AUTH_TYPE_LABELS: Record<string, string> = {
  none: '无认证',
  django_session: '账号密码登录',
  bearer: 'Bearer Token',
  api_key: 'API Key',
  oauth2_client_credentials: 'AppKey / AppSecret (OAuth2)',
}

const DEFAULT_BASE_URLS: Record<string, string> = {
  llm_wiki_django: 'http://127.0.0.1:8091',
  weknora: 'http://localhost:8080',
  lexiang: 'https://lxapi.lexiangla.com',
}

function defaultBaseUrlFor(providerType: string): string {
  return DEFAULT_BASE_URLS[providerType] ?? ''
}

const baseUrlPlaceholder = computed(
  () => defaultBaseUrlFor(sourceForm.provider_type) || 'https://example.com',
)

const currentProvider = computed<ProviderTypeInfo | null>(
  () => providerTypes.value.find((item) => item.provider_type === sourceForm.provider_type) || providerTypes.value[0] || null,
)

const supportedAuthTypes = computed<string[]>(() => {
  const list = currentProvider.value?.capabilities?.supported_auth_types
  return list && list.length ? list : ['none']
})

const activeCredentialFields = computed<ProviderFieldSpec[]>(() => {
  const map = currentProvider.value?.credential_fields_by_auth || {}
  return map[sourceForm.auth_type] || []
})

const supportsRemoteScopes = computed(() =>
  Boolean(currentProvider.value?.capabilities?.supports_remote_scopes),
)

const externalProjectIdField = computed<ProviderFieldSpec | null>(
  () => (currentProvider.value?.config_fields || []).find((f) => f.key === 'external_project_id') || null,
)

const otherConfigFields = computed<ProviderFieldSpec[]>(() => {
  const fields = currentProvider.value?.config_fields || []
  if (!supportsRemoteScopes.value) return fields
  return fields.filter((f) => f.key !== 'external_project_id' && f.key !== 'external_project_name')
})

const scopeOptions = computed<RemoteScope[]>(() => {
  const list = [...remoteScopes.value]
  const currentId = String(sourceForm.config.external_project_id || '').trim()
  if (currentId && !list.some((item) => item.id === currentId)) {
    list.unshift({
      id: currentId,
      name: String(sourceForm.config.external_project_name || currentId),
      description: '',
      path: '',
      raw: {},
    })
  }
  return list
})

function providerDisplayName(providerType: string): string {
  return providerTypes.value.find((item) => item.provider_type === providerType)?.display_name || providerType
}

function authTypeLabel(authType: string): string {
  return AUTH_TYPE_LABELS[authType] || authType
}

function initConfigDefaults(provider: ProviderTypeInfo | null, existing?: Record<string, unknown>) {
  const next: Record<string, unknown> = {}
  for (const field of provider?.config_fields || []) {
    if (existing && existing[field.key] !== undefined) {
      next[field.key] = existing[field.key]
    } else if (field.default !== undefined) {
      next[field.key] = field.default
    } else if (field.field_type === 'boolean') {
      next[field.key] = false
    } else if (field.field_type === 'number') {
      next[field.key] = 0
    } else {
      next[field.key] = ''
    }
  }
  if (existing) {
    for (const [k, v] of Object.entries(existing)) {
      if (next[k] === undefined) next[k] = v
    }
  }
  sourceForm.config = next
}

function resetSourceForm(providerType?: string) {
  editingSource.value = null
  remoteScopes.value = []
  const targetType = providerType || providerTypes.value[0]?.provider_type || 'llm_wiki_django'
  const provider = providerTypes.value.find((item) => item.provider_type === targetType) || null
  const defaultAuth = provider?.capabilities?.supported_auth_types?.[0] || 'none'
  sourceForm.name = provider ? provider.display_name : '外部知识库'
  sourceForm.provider_type = targetType
  sourceForm.base_url = defaultBaseUrlFor(targetType)
  sourceForm.auth_type = defaultAuth
  sourceForm.credentials = {}
  sourceForm.weight = 1.0
  sourceForm.priority = 100
  sourceForm.enabled = true
  initConfigDefaults(provider)
}

function selectSourceForEdit(row: KnowledgeSourceOut) {
  editingSource.value = row
  remoteScopes.value = []
  sourceForm.name = row.name
  sourceForm.provider_type = row.provider_type
  sourceForm.base_url = row.base_url
  sourceForm.auth_type = row.auth_type || 'none'
  sourceForm.weight = row.weight
  sourceForm.priority = row.priority
  sourceForm.enabled = row.enabled

  const provider = providerTypes.value.find((item) => item.provider_type === row.provider_type) || null
  initConfigDefaults(provider, row.config || {})

  const creds: Record<string, unknown> = {}
  if (row.auth_username) {
    if (row.auth_type === 'oauth2_client_credentials') {
      creds.app_key = row.auth_username
    } else {
      creds.username = row.auth_username
    }
  }
  sourceForm.credentials = creds

  const extId = String(row.config?.external_project_id || '').trim()
  const extName = String(row.config?.external_project_name || '').trim()
  if (extId) {
    remoteScopes.value = [{ id: extId, name: extName || extId, description: '', path: '', raw: {} }]
  }
}

function onProviderTypeChange(nextType: string) {
  const provider = providerTypes.value.find((item) => item.provider_type === nextType) || null
  const supported = provider?.capabilities?.supported_auth_types || ['none']
  if (!supported.includes(sourceForm.auth_type)) {
    sourceForm.auth_type = supported[0] || 'none'
    sourceForm.credentials = {}
  }
  if (!editingSource.value) {
    sourceForm.base_url = defaultBaseUrlFor(nextType)
  }
  remoteScopes.value = []
  initConfigDefaults(provider, editingSource.value ? sourceForm.config : undefined)
}

function onAuthTypeChange() {
  sourceForm.credentials = {}
}

function onScopeChange(val: unknown) {
  const scopeId = String(val || '').trim()
  sourceForm.config.external_project_id = scopeId
  const matched = remoteScopes.value.find((item) => item.id === scopeId)
  if (matched) {
    sourceForm.config.external_project_name = matched.name
  } else {
    sourceForm.config.external_project_name = ''
  }
}

function buildCredentialsPayload(): Record<string, unknown> {
  if (sourceForm.auth_type === 'none') return {}
  const payload: Record<string, unknown> = {}
  for (const field of activeCredentialFields.value) {
    const val = sourceForm.credentials[field.key]
    if (field.field_type === 'password') {
      if (typeof val === 'string' && val !== '') {
        payload[field.key] = val
      }
    } else if (val !== undefined && val !== null) {
      payload[field.key] = typeof val === 'string' ? val.trim() : val
    }
  }
  return payload
}

function buildConfigPayload(): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(sourceForm.config)) {
    if (typeof v === 'string') out[k] = v.trim()
    else out[k] = v
  }
  return out
}

async function loadSourcesDrawerData(projectId: number) {
  sourcesLoading.value = true
  try {
    const [types, sources] = await Promise.all([
      providerTypes.value.length ? Promise.resolve(providerTypes.value) : listKnowledgeProviderTypes(),
      listKnowledgeSources(projectId),
    ])
    providerTypes.value = types
    knowledgeSources.value = sources
  } catch (err) {
    ElMessage.error(`加载外部知识源失败：${(err as Error).message}`)
  } finally {
    sourcesLoading.value = false
  }
}

async function openKnowledgeSources(row: Project) {
  currentSourceProject.value = row
  knowledgeSources.value = []
  editingSource.value = null
  remoteScopes.value = []
  sourceDrawer.value = true
  await loadSourcesDrawerData(row.id)
  if (knowledgeSources.value.length) {
    selectSourceForEdit(knowledgeSources.value[0])
  } else {
    resetSourceForm()
  }
}

async function runTestConnection() {
  if (!currentSourceProject.value) return
  if (!sourceForm.base_url.trim()) {
    ElMessage.warning('请先填写服务地址 (Base URL)')
    return
  }
  testingConnection.value = true
  try {
    const res = await testKnowledgeSource(currentSourceProject.value.id, {
      source_id: editingSource.value?.id ?? null,
      provider_type: sourceForm.provider_type,
      base_url: sourceForm.base_url.trim(),
      auth_type: sourceForm.auth_type,
      config: buildConfigPayload(),
      credentials: buildCredentialsPayload(),
    })
    if (res.ok) {
      remoteScopes.value = res.scopes || []
      ElMessage.success(res.message || `连接成功，拉取到 ${remoteScopes.value.length} 个远端知识库`)
    } else {
      ElMessage.warning(res.message || '连接失败')
    }
  } catch (err) {
    ElMessage.error(`连接测试失败：${(err as Error).message}`)
  } finally {
    testingConnection.value = false
  }
}

async function saveSource() {
  if (!currentSourceProject.value) return
  if (!sourceForm.name.trim()) {
    ElMessage.warning('请填写知识源名称')
    return
  }
  if (!sourceForm.base_url.trim()) {
    ElMessage.warning('请填写服务地址 (Base URL)')
    return
  }
  const projectId = currentSourceProject.value.id
  sourceSaving.value = true
  try {
    const payload = {
      name: sourceForm.name.trim(),
      provider_type: sourceForm.provider_type,
      base_url: sourceForm.base_url.trim(),
      auth_type: sourceForm.auth_type,
      config: buildConfigPayload(),
      credentials: buildCredentialsPayload(),
      weight: sourceForm.weight,
      priority: sourceForm.priority,
      enabled: sourceForm.enabled,
    }
    let saved: KnowledgeSourceOut
    if (editingSource.value) {
      saved = await updateKnowledgeSource(projectId, editingSource.value.id, payload)
      ElMessage.success('知识源已更新')
    } else {
      saved = await createKnowledgeSource(projectId, payload)
      ElMessage.success('知识源已添加')
    }
    await loadSourcesDrawerData(projectId)
    await projects.refresh(router)
    const fresh = knowledgeSources.value.find((item) => item.id === saved.id) || saved
    selectSourceForEdit(fresh)
  } catch (err) {
    ElMessage.error(`保存知识源失败：${(err as Error).message}`)
  } finally {
    sourceSaving.value = false
  }
}

async function toggleSourceEnabled(row: KnowledgeSourceOut, value: unknown) {
  if (!currentSourceProject.value) return
  const next = Boolean(value)
  togglingSourceId.value = row.id
  try {
    const updated = await updateKnowledgeSource(currentSourceProject.value.id, row.id, { enabled: next })
    const idx = knowledgeSources.value.findIndex((item) => item.id === row.id)
    if (idx !== -1) knowledgeSources.value[idx] = updated
    if (editingSource.value?.id === row.id) {
      sourceForm.enabled = updated.enabled
      editingSource.value = updated
    }
    await projects.refresh(router)
    ElMessage.success(next ? '已启用该知识源' : '已停用该知识源')
  } catch (err) {
    ElMessage.error(`更新状态失败：${(err as Error).message}`)
  } finally {
    togglingSourceId.value = null
  }
}

async function removeSource(row: KnowledgeSourceOut) {
  if (!currentSourceProject.value) return
  try {
    await ElMessageBox.confirm(`确认删除知识源「${row.name}」？删除后将不再参与该项目检索。`, '删除知识源', {
      type: 'warning',
      confirmButtonText: '确认删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  const projectId = currentSourceProject.value.id
  deletingSourceId.value = row.id
  try {
    await deleteKnowledgeSource(projectId, row.id)
    ElMessage.success('知识源已删除')
    await loadSourcesDrawerData(projectId)
    await projects.refresh(router)
    if (editingSource.value?.id === row.id) {
      if (knowledgeSources.value.length) selectSourceForEdit(knowledgeSources.value[0])
      else resetSourceForm()
    }
  } catch (err) {
    ElMessage.error(`删除失败：${(err as Error).message}`)
  } finally {
    deletingSourceId.value = null
  }
}

function openForm(row?: Project) {
  editing.value = row || null
  Object.assign(form, row ? { name: row.name, slug: row.slug, description: row.description } : { name: '', slug: '', description: '' })
  dialog.value = true
}

async function refresh() {
  try { await projects.refresh(router) } catch (error) { ElMessage.error((error as Error).message) }
}

async function save() {
  if (!form.name.trim()) return
  saving.value = true
  try {
    if (editing.value) await updateProject(editing.value.id, { name: form.name.trim(), description: form.description.trim() })
    else await createProject({ name: form.name.trim(), slug: form.slug.trim() || undefined, description: form.description.trim() })
    dialog.value = false
    await refresh()
    ElMessage.success('项目已保存')
  } catch (error) { ElMessage.error((error as Error).message) } finally { saving.value = false }
}

async function toggleStatus(row: Project) {
  const next = row.status === 'active' ? 'archived' : 'active'
  try {
    await updateProject(row.id, { status: next })
    await refresh()
    ElMessage.success(next === 'active' ? '项目已恢复' : '项目已归档')
  } catch (error) { ElMessage.error((error as Error).message) }
}

async function togglePublic(row: Project, value: unknown) {
  const next = Boolean(value)
  publicSavingId.value = row.id
  try {
    await updateProject(row.id, { is_public: next })
    await projects.refresh()
    ElMessage.success(next ? '项目已对全员可见' : '项目已对普通用户隐藏')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    publicSavingId.value = null
  }
}

async function remove(row: Project) {
  try {
    await ElMessageBox.confirm(`永久删除空项目「${row.name}」？此操作不可撤销。`, '删除项目', { type: 'warning', confirmButtonText: '删除' })
    await deleteProject(row.id)
    await refresh()
    ElMessage.success('项目已删除')
  } catch (error) { if (error !== 'cancel') ElMessage.error((error as Error).message) }
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1 class="page-title">项目管理</h1>
        <p class="page-subtitle">管理项目基础信息、可见范围与多异构外部知识库连接配置。</p>
      </div>
      <div class="actions">
        <el-button @click="refresh">刷新</el-button>
        <el-button v-if="isAdmin" type="primary" @click="openForm()">新建项目</el-button>
      </div>
    </div>

    <el-card shadow="never">
      <el-table :data="projects.state.projects" row-key="id">
        <el-table-column prop="name" label="名称" min-width="150" />
        <el-table-column prop="slug" label="Slug" min-width="130" />
        <el-table-column prop="description" label="说明" min-width="200" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'active' ? 'success' : 'info'">
              {{ row.status === 'active' ? '使用中' : '已归档' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="外部知识库" width="130">
          <template #default="{ row }">
            <el-tag
              v-if="(row.knowledge_source_count ?? 0) > 0"
              type="success"
              effect="light"
              size="small"
            >
              已配置 {{ row.knowledge_source_count }} 个
            </el-tag>
            <el-tag v-else type="info" effect="plain" size="small">未配置</el-tag>
          </template>
        </el-table-column>
        <el-table-column v-if="isAdmin" label="全员可见" width="100">
          <template #default="{ row }">
            <el-switch :model-value="row.is_public" :loading="publicSavingId === row.id" @change="togglePublic(row, $event)" />
          </template>
        </el-table-column>
        <el-table-column v-if="isAdmin" label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button link @click="openForm(row)">编辑</el-button>
            <el-button link type="primary" @click="openKnowledgeSources(row)">外部知识库</el-button>
            <el-button link @click="toggleStatus(row)">{{ row.status === 'active' ? '归档' : '恢复' }}</el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialog" :title="editing ? '编辑项目' : '新建项目'" width="520px">
      <el-form label-position="top">
        <el-form-item label="名称"><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="Slug"><el-input v-model="form.slug" :disabled="!!editing" placeholder="可留空自动生成" /></el-form-item>
        <el-form-item label="说明"><el-input v-model="form.description" type="textarea" :rows="4" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <el-drawer
      v-model="sourceDrawer"
      :title="`外部知识库管理 · ${currentSourceProject?.name || ''}`"
      size="min(1080px, 94vw)"
      destroy-on-close
    >
      <div v-loading="sourcesLoading" class="ks-layout">
        <section class="ks-list-panel">
          <div class="ks-panel-head">
            <div>
              <h3 class="ks-panel-title">已配置知识源（{{ knowledgeSources.length }}）</h3>
              <p class="ks-panel-desc">支持为同一项目挂载多个异构外部知识源并按权重与优先级融合检索。</p>
            </div>
            <el-button type="primary" size="small" @click="resetSourceForm()">+ 新增知识源</el-button>
          </div>

          <el-empty v-if="!knowledgeSources.length" description="暂无外部知识源，请在右侧添加" :image-size="64" />
          <div v-else class="ks-list">
            <div
              v-for="item in knowledgeSources"
              :key="item.id"
              class="ks-item"
              :class="{ active: editingSource?.id === item.id, disabled: !item.enabled }"
              @click="selectSourceForEdit(item)"
            >
              <div class="ks-item-top">
                <div class="ks-item-title-wrap">
                  <span class="ks-item-name">{{ item.name }}</span>
                  <el-tag size="small" effect="plain" type="primary">{{ providerDisplayName(item.provider_type) }}</el-tag>
                  <el-tag size="small" effect="plain" type="info">{{ authTypeLabel(item.auth_type) }}</el-tag>
                </div>
                <div class="ks-item-switch" @click.stop>
                  <el-switch
                    :model-value="item.enabled"
                    size="small"
                    :loading="togglingSourceId === item.id"
                    @change="toggleSourceEnabled(item, $event)"
                  />
                </div>
              </div>
              <div class="ks-item-url">{{ item.base_url }}</div>
              <div class="ks-item-meta">
                <span>权重 {{ item.weight }} · 优先级 {{ item.priority }}</span>
                <span v-if="item.config?.external_project_name || item.config?.external_project_id">
                  · 远端知识库：{{ item.config.external_project_name || item.config.external_project_id }}
                </span>
              </div>
              <div class="ks-item-actions" @click.stop>
                <el-button link size="small" type="primary" @click="selectSourceForEdit(item)">编辑</el-button>
                <el-button
                  link
                  size="small"
                  type="danger"
                  :loading="deletingSourceId === item.id"
                  @click="removeSource(item)"
                >
                  删除
                </el-button>
              </div>
            </div>
          </div>
        </section>

        <section class="ks-form-panel">
          <div class="ks-panel-head">
            <h3 class="ks-panel-title">{{ editingSource ? `编辑知识源：${editingSource.name}` : '新增知识源' }}</h3>
            <el-button v-if="editingSource" link size="small" @click="resetSourceForm()">切为新增</el-button>
          </div>

          <el-form label-position="top" class="ks-form">
            <el-form-item label="知识库类型" required>
              <el-select v-model="sourceForm.provider_type" style="width: 100%" @change="onProviderTypeChange">
                <el-option
                  v-for="pt in providerTypes"
                  :key="pt.provider_type"
                  :label="pt.display_name"
                  :value="pt.provider_type"
                />
              </el-select>
              <div v-if="currentProvider?.description" class="field-help">{{ currentProvider.description }}</div>
            </el-form-item>

            <el-form-item label="知识源名称" required>
              <el-input v-model="sourceForm.name" maxlength="120" placeholder="例如：产品文档知识库" />
            </el-form-item>

            <el-form-item label="Base URL (服务地址)" required>
              <el-input v-model="sourceForm.base_url" :placeholder="baseUrlPlaceholder" />
            </el-form-item>

            <el-form-item label="认证方式">
              <el-radio-group v-model="sourceForm.auth_type" @change="onAuthTypeChange">
                <el-radio-button v-for="authOpt in supportedAuthTypes" :key="authOpt" :value="authOpt">
                  {{ authTypeLabel(authOpt) }}
                </el-radio-button>
              </el-radio-group>
            </el-form-item>

            <div v-if="activeCredentialFields.length" class="ks-cred-grid">
              <el-form-item
                v-for="field in activeCredentialFields"
                :key="field.key"
                :label="field.label"
                :required="field.required && !(editingSource?.has_credentials && field.field_type === 'password')"
              >
                <el-input
                  v-model="(sourceForm.credentials[field.key] as string)"
                  :type="field.field_type === 'password' ? 'password' : 'text'"
                  :show-password="field.field_type === 'password'"
                  :placeholder="
                    field.field_type === 'password' && editingSource?.has_credentials
                      ? '留空保持原密码不修改'
                      : (field.placeholder || `请输入${field.label}`)
                  "
                  autocomplete="off"
                />
                <div v-if="field.help_text" class="field-help">{{ field.help_text }}</div>
              </el-form-item>
            </div>

            <div class="ks-test-bar">
              <el-button type="primary" plain :loading="testingConnection" @click="runTestConnection">
                测试连接并拉取远端知识库
              </el-button>
              <span v-if="editingSource?.has_credentials && sourceForm.auth_type !== 'none'" class="field-help">
                测试连接将自动复用已保存凭证（如密码留空）
              </span>
            </div>

            <el-form-item v-if="supportsRemoteScopes" label="关联远端知识库 (Scope)">
              <el-select
                :model-value="(sourceForm.config.external_project_id as string) || ''"
                placeholder="请先测试连接并选择远端知识库，或手动输入 ID"
                filterable
                allow-create
                default-first-option
                fit-input-width
                clearable
                style="width: 100%"
                @change="onScopeChange"
              >
                <el-option
                  v-for="scope in scopeOptions"
                  :key="scope.id"
                  :label="scope.name && scope.name !== scope.id ? `${scope.name} (${scope.id})` : scope.id"
                  :value="scope.id"
                />
              </el-select>
              <div v-if="externalProjectIdField?.help_text" class="field-help">
                {{ externalProjectIdField.help_text }}
              </div>
            </el-form-item>

            <div v-if="otherConfigFields.length" class="ks-config-grid">
              <el-form-item
                v-for="field in otherConfigFields"
                :key="field.key"
                :label="field.label"
                :required="Boolean(field.required)"
              >
                <el-switch
                  v-if="field.field_type === 'boolean'"
                  v-model="(sourceForm.config[field.key] as boolean)"
                />
                <el-input-number
                  v-else-if="field.field_type === 'number'"
                  v-model="(sourceForm.config[field.key] as number)"
                  :min="field.key === 'top_k' ? 1 : 0.5"
                  :max="field.key === 'top_k' ? 50 : 120"
                  :step="field.key === 'top_k' ? 1 : 0.5"
                  style="width: 100%"
                />
                <el-select
                  v-else-if="field.field_type === 'select'"
                  v-model="(sourceForm.config[field.key] as string)"
                  style="width: 100%"
                >
                  <el-option
                    v-for="opt in field.options || []"
                    :key="opt.value"
                    :label="opt.label"
                    :value="opt.value"
                  />
                </el-select>
                <el-input
                  v-else
                  v-model="(sourceForm.config[field.key] as string)"
                  :type="field.field_type === 'textarea' ? 'textarea' : 'text'"
                  :placeholder="field.placeholder"
                />
                <div v-if="field.help_text" class="field-help">{{ field.help_text }}</div>
              </el-form-item>
            </div>

            <div class="ks-fusion-grid">
              <el-form-item label="融合权重 (0~10)">
                <el-input-number v-model="sourceForm.weight" :min="0" :max="10" :step="0.1" style="width: 100%" />
              </el-form-item>
              <el-form-item label="检索优先级 (越小越优先)">
                <el-input-number v-model="sourceForm.priority" :min="0" :max="10000" :step="10" style="width: 100%" />
              </el-form-item>
              <el-form-item label="启用该知识源">
                <el-switch v-model="sourceForm.enabled" />
              </el-form-item>
            </div>

            <div class="ks-form-footer">
              <el-button @click="sourceDrawer = false">关闭</el-button>
              <el-button type="primary" :loading="sourceSaving" @click="saveSource">
                {{ editingSource ? '保存修改' : '添加知识源' }}
              </el-button>
            </div>
          </el-form>
        </section>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.actions {
  display: flex;
  gap: 10px;
}

.ks-layout {
  display: grid;
  grid-template-columns: minmax(280px, 0.95fr) minmax(360px, 1.25fr);
  gap: 20px;
  min-height: 480px;
}

.ks-list-panel,
.ks-form-panel {
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius);
  padding: 16px;
  background: var(--cg-surface);
}

.ks-panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.ks-panel-title {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
  color: var(--cg-text);
}

.ks-panel-desc {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--cg-text-muted);
}

.ks-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 620px;
  overflow-y: auto;
}

.ks-item {
  padding: 12px;
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius-sm);
  background: var(--cg-surface-muted);
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}

.ks-item:hover {
  border-color: #10b981;
}

.ks-item.active {
  border-color: #10b981;
  background: #ecfdf5;
}

.ks-item.disabled {
  opacity: 0.72;
}

.ks-item-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}

.ks-item-title-wrap {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.ks-item-name {
  font-weight: 700;
  font-size: 13.5px;
  color: var(--cg-text);
}

.ks-item-url {
  margin-top: 6px;
  font-family: var(--cg-font-mono);
  font-size: 12px;
  color: var(--cg-text-secondary);
  word-break: break-all;
}

.ks-item-meta {
  margin-top: 4px;
  font-size: 12px;
  color: var(--cg-text-muted);
}

.ks-item-actions {
  display: flex;
  gap: 10px;
  margin-top: 6px;
}

.ks-cred-grid,
.ks-config-grid,
.ks-fusion-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 14px;
}

.ks-fusion-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.ks-test-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 16px;
}

.field-help {
  margin-top: 4px;
  font-size: 12px;
  color: var(--cg-text-muted);
  line-height: 1.4;
}

.ks-form-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--cg-border);
}

@media (max-width: 900px) {
  .ks-layout {
    grid-template-columns: 1fr;
  }

  .ks-cred-grid,
  .ks-config-grid,
  .ks-fusion-grid {
    grid-template-columns: 1fr;
  }
}
</style>
