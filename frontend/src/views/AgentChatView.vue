<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ChatDotRound, Delete, InfoFilled, Plus, Refresh, View } from '@element-plus/icons-vue'
import { formatDateTime as formatTime } from '../utils/datetime'
import { useProjectStore } from '../projectStore'
import { useAgentChatStore } from '../stores/agentChatStore'
import ChatPanel from '../components/agent/ChatPanel.vue'
import SkillPicker from '../components/agent/SkillPicker.vue'
import CitationList from '../components/CitationList.vue'
import { listCitations, type TaskCitation } from '../api/tasks'
import type { AgentConversationItem, AgentSaveAsDraftResult } from '../api/agent'
import type { ModelConfig } from '../api/models'

/**
 * Agent 对话页（spec §3.6-3）。
 * 左列会话历史 + 中列消息流；顶部技能/模型收成一行摘要（展开可改当前会话配置），
 * 右栏会话信息与转存后的引用溯源收进抽屉，默认隐藏。
 *
 * 所有流式状态都在 agentChatStore：MainLayout.vue:279 的 router-view :key = projects.state.currentId，
 * 切项目会销毁本视图；store 内 setProjectId() 负责 teardown，视图卸载只 releaseView()（有进行中的轮次
 * 就继续 2s 轮询兜底，不打断后台 agent）。刷新恢复靠 ?conversation= 查询参数 + SSE 首帧 snapshot。
 */

const route = useRoute()
const router = useRouter()
const projects = useProjectStore()
const store = useAgentChatStore()

const createDialogVisible = ref(false)
const creating = ref(false)
const createTitle = ref('')
const createSkill = ref<string | null>(null)
const createModel = ref('')
const citations = ref<TaskCitation[]>([])
const citationsLoading = ref(false)
const savedTaskId = ref<number | null>(null)

// 顶部摘要行的展开编辑与右栏抽屉；与新建弹窗的 createSkill/createModel 完全隔离。
const configExpanded = ref(false)
const infoDrawerVisible = ref(false)
const currentSkillDraft = ref<string | null>(null)
const currentModelDraft = ref('')
const configSaving = ref(false)

// 会话历史栏的收起/展开（纯视图状态，localStorage 记忆）。
const listCollapsed = ref(localStorage.getItem('casegen:agent-list-collapsed') === '1')

function toggleListCollapsed() {
  listCollapsed.value = !listCollapsed.value
  localStorage.setItem('casegen:agent-list-collapsed', listCollapsed.value ? '1' : '0')
}

/** 项目上下文：query 优先，回退 projectStore，与 TaskDetailView.vue:224-231 同口径。 */
const projectId = computed<number | null>(() => {
  const fromQuery = Number(Array.isArray(route.query.project_id) ? route.query.project_id[0] : route.query.project_id)
  if (Number.isInteger(fromQuery) && fromQuery > 0) return fromQuery
  if (projects.state.currentId) return projects.state.currentId
  const stored = Number(localStorage.getItem('casegen:last-project-id'))
  return Number.isInteger(stored) && stored > 0 ? stored : null
})

const spaceId = computed<number | undefined>(() => {
  const raw = Array.isArray(route.query.space_id) ? route.query.space_id[0] : route.query.space_id
  const value = Number(raw)
  return Number.isInteger(value) && value > 0 ? value : undefined
})

const conversations = computed(() => store.conversations)
const current = computed(() => store.currentConversation)

const modelOptions = computed(() =>
  store.models.map((item) => ({ id: item.id, name: item.name, model_name: item.model_name })),
)

/** 展开区模型候选：沿用 SkillPicker 的「name/model_name」口径。 */
const modelCandidates = computed(() =>
  store.models.map((item: ModelConfig) => ({
    label: `${item.name}（${item.model_name}）`,
    value: item.name ? `${item.name}/${item.model_name}` : item.model_name,
  })),
)

/**
 * 切换会话 / PATCH 成功后把草稿同步为服务端最新值。用字符串键值比较，避免状态帧、
 * version 变化等与配置无关的更新把用户尚未保存的草稿冲掉。
 */
watch(
  () => `${current.value?.id ?? ''}|${current.value?.skill_name ?? ''}|${current.value?.model ?? ''}`,
  () => {
    currentSkillDraft.value = current.value?.skill_name ?? null
    currentModelDraft.value = current.value?.model ?? ''
  },
  { immediate: true },
)

/** 会话自身状态（active/busy/closed），不进任务状态机 ⇒ 无需改 tasks.ts 的四处硬编码集合。 */
function conversationStatusLabel(status: string): string {
  if (status === 'busy') return '生成中'
  if (status === 'closed') return '已关闭'
  if (status === 'active') return '空闲'
  return status || '未知'
}

function conversationStatusTagType(status: string): 'success' | 'warning' | 'info' | 'danger' {
  if (status === 'busy') return 'warning'
  if (status === 'closed') return 'info'
  if (status === 'active') return 'success'
  return 'info'
}

async function loadAll() {
  store.setProjectId(projectId.value)
  await Promise.all([store.loadConversations(), store.loadSkills(), store.loadModels()])
}

async function loadCitationsForTask(taskId: number | null) {
  savedTaskId.value = taskId
  citations.value = []
  if (!taskId) return
  citationsLoading.value = true
  try {
    citations.value = await listCitations(taskId, projectId.value)
  } catch {
    citations.value = []
  } finally {
    citationsLoading.value = false
  }
}

/**
 * 后端返回 AgentSaveAsDraftOut{task_id, draft_id, requirement_id, imported_count, case_keys, message}
 * （spec §3.5 方案 a：虚拟 Requirement + shell GenerationTask + CaseDraft）。
 * task_id 是 number，可直接跳任务详情并用 CitationList(task_id) 拉溯源。
 */
function onDraftSaved(result: AgentSaveAsDraftResult) {
  const taskId = typeof result.task_id === 'number' ? result.task_id : null
  const imported = typeof result.imported_count === 'number' ? result.imported_count : 0
  const fallback = taskId
    ? `已转存 ${imported} 条用例草稿（任务 #${taskId}）`
    : '已转存为用例草稿'
  ElMessage.success(result.message || fallback)
  void loadCitationsForTask(taskId)
  void store.loadConversations()
}

function openTaskDetail(taskId: number | null) {
  if (!taskId) return
  void router.push({ name: 'task-detail', params: { id: String(taskId) }, query: { ...route.query } })
}

function toggleConfig() {
  configExpanded.value = !configExpanded.value
}

/** el-select / el-input 的清空值可能是 undefined，统一收敛成 null/''。 */
function onSkillDraftChange(value: unknown) {
  currentSkillDraft.value = value == null || value === '' ? null : String(value)
}

function onModelDraftChange(value: unknown) {
  currentModelDraft.value = value == null ? '' : String(value)
}

async function saveCurrentConfig() {
  if (!current.value || store.isBusy) return
  configSaving.value = true
  try {
    await store.updateConversationConfig(
      currentSkillDraft.value || null,
      currentModelDraft.value.trim() || null,
    )
    ElMessage.success('配置已保存，下一条消息起生效')
    configExpanded.value = false
  } catch (error) {
    ElMessage.error(`保存配置失败：${(error as Error).message}`)
  } finally {
    configSaving.value = false
  }
}

function openCreateDialog() {
  createTitle.value = ''
  createSkill.value = null
  createModel.value = ''
  createDialogVisible.value = true
}

async function submitCreate() {
  creating.value = true
  try {
    const conversation = await store.createConversation({
      title: createTitle.value.trim() || undefined,
      skill_name: createSkill.value || null,
      model: createModel.value.trim() || null,
    })
    createDialogVisible.value = false
    await store.loadConversations()
    await store.selectConversation(conversation.id)
    syncQuery(conversation.id)
    citations.value = []
    savedTaskId.value = null
  } catch (error) {
    ElMessage.error(`创建会话失败：${(error as Error).message}`)
  } finally {
    creating.value = false
  }
}

async function onSelect(conversation: AgentConversationItem) {
  if (conversation.id === store.currentId) return
  try {
    await store.selectConversation(conversation.id)
    syncQuery(conversation.id)
    citations.value = []
    savedTaskId.value = null
  } catch (error) {
    ElMessage.error(`载入会话失败：${(error as Error).message}`)
  }
}

/** 把当前会话写进 URL，刷新后可恢复（保留 project_id / space_id）。 */
function syncQuery(conversationId: string | null) {
  const query: Record<string, string> = {}
  for (const [key, value] of Object.entries(route.query)) {
    if (value === undefined) continue
    query[key] = Array.isArray(value) ? String(value[0] ?? '') : String(value)
  }
  if (conversationId) query.conversation = conversationId
  else delete query.conversation
  if (query.conversation === (route.query.conversation ?? '')) return
  void router.replace({ query })
}

async function onCloseConversation(conversation: AgentConversationItem) {
  try {
    await ElMessageBox.confirm(
      `关闭会话「${conversation.title || conversation.id}」后将无法继续对话，历史记录仍会保留。确认关闭？`,
      '关闭会话',
      { confirmButtonText: '关闭会话', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await store.closeConversation(conversation.id)
    ElMessage.success('会话已关闭')
    if (conversation.id === store.currentId) syncQuery(null)
  } catch (error) {
    ElMessage.error(`关闭会话失败：${(error as Error).message}`)
  }
}

async function refreshConversations() {
  await store.loadConversations()
  if (store.currentId) await store.refreshConversation(store.currentId)
}

async function refreshSkills() {
  store.skills = []
  await store.loadSkills()
}

watch(projectId, (value) => {
  store.setProjectId(value)
  citations.value = []
  savedTaskId.value = null
  void store.loadConversations()
})

onMounted(async () => {
  await loadAll()
  const requested = Array.isArray(route.query.conversation)
    ? route.query.conversation[0]
    : route.query.conversation
  if (typeof requested === 'string' && requested) {
    await store.selectConversation(requested)
  } else if (conversations.value.length) {
    const first = conversations.value[0]
    await store.selectConversation(first.id)
    syncQuery(first.id)
  }
})

onUnmounted(() => {
  // 不 dispose：切项目由 setProjectId 清理；这里只在没有进行中轮次时收流。
  store.releaseView()
})
</script>

<template>
  <div class="page agent-page">
    <el-alert
      v-if="!store.agentAvailable"
      class="page-alert"
      type="warning"
      show-icon
      :closable="false"
      :title="store.agentDisabledNotice || 'Agent 对话当前不可用'"
    />
    <el-alert
      v-else-if="store.conversationsError"
      class="page-alert"
      type="error"
      show-icon
      :closable="false"
      :title="store.conversationsError"
    />
    <el-alert
      v-else-if="!projectId"
      class="page-alert"
      type="info"
      show-icon
      :closable="false"
      title="尚未选择项目，请先在左侧选择项目后再创建 Agent 会话"
    />
    <el-alert
      v-if="store.detailError"
      class="page-alert"
      type="error"
      show-icon
      :closable="false"
      title="载入会话详情失败"
      :description="store.detailError"
    />
    <el-alert
      v-else-if="store.skillsError"
      class="page-alert"
      type="warning"
      show-icon
      :closable="false"
      title="技能列表载入失败，可稍后重试"
      :description="store.skillsError"
    />

    <div class="agent-layout" :class="{ 'layout--list-collapsed': listCollapsed }">
      <el-card shadow="never" class="block list-block">
        <template #header>
          <div class="block-header">
            <div class="list-header-title">
              <span>会话历史</span>
              <el-tag size="small" type="info" effect="plain">{{ conversations.length }}</el-tag>
              <el-tooltip effect="dark" placement="bottom" :show-after="200" content="隐藏侧边栏">
                <button
                  type="button"
                  class="list-collapse-btn"
                  aria-label="隐藏侧边栏"
                  :aria-expanded="true"
                  @click="toggleListCollapsed"
                >
                  <svg
                    viewBox="0 0 16 16"
                    width="15"
                    height="15"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="1.4"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                    aria-hidden="true"
                  >
                    <rect x="2" y="2.5" width="12" height="11" rx="2" />
                    <path d="M6.5 2.5v11" />
                  </svg>
                </button>
              </el-tooltip>
            </div>
            <div class="list-header-actions">
              <el-button
                size="small"
                :icon="Refresh"
                :loading="store.conversationsLoading"
                class="list-refresh-btn"
                @click="refreshConversations"
              >
                刷新
              </el-button>
              <el-button
                size="small"
                type="primary"
                :disabled="!store.agentAvailable || !projectId"
                @click="openCreateDialog"
              >
                <el-icon><Plus /></el-icon>
                <span>新建对话</span>
              </el-button>
            </div>
          </div>
        </template>
        <div v-if="store.conversationsLoading && !conversations.length" class="list-loading">
          正在载入会话列表…
        </div>
        <el-empty v-else-if="!conversations.length" description="还没有会话，点击右上角新建对话" :image-size="64" />
        <ul v-else class="conversation-list">
          <li
            v-for="conversation in conversations"
            :key="conversation.id"
            class="conversation-item"
            :class="{ active: conversation.id === store.currentId, closed: conversation.status === 'closed' }"
            @click="onSelect(conversation)"
          >
            <div class="conversation-main">
              <div class="conversation-title-row">
                <el-icon class="conversation-icon"><ChatDotRound /></el-icon>
                <span class="conversation-title">{{ conversation.title || '未命名会话' }}</span>
              </div>
              <div class="conversation-meta">
                <el-tag
                  size="small"
                  effect="plain"
                  :type="conversationStatusTagType(conversation.status)"
                >
                  {{ conversationStatusLabel(conversation.status) }}
                </el-tag>
                <span v-if="conversation.skill_name" class="meta-skill">{{ conversation.skill_name }}</span>
                <span class="meta-time">{{ formatTime(conversation.updated_at) }}</span>
              </div>
            </div>
            <el-button
              v-if="conversation.status !== 'closed'"
              size="small"
              link
              type="danger"
              class="conversation-close"
              title="关闭会话"
              @click.stop="onCloseConversation(conversation)"
            >
              <el-icon><Delete /></el-icon>
            </el-button>
          </li>
        </ul>
      </el-card>

      <el-card shadow="never" class="block chat-block">
        <template #header>
          <div class="chat-header">
            <div class="chat-header-main">
              <el-tooltip v-if="listCollapsed" effect="dark" placement="bottom" :show-after="200" content="展开侧边栏">
                <button
                  type="button"
                  class="list-expand-btn"
                  aria-label="展开侧边栏"
                  :aria-expanded="false"
                  @click="toggleListCollapsed"
                >
                  <svg
                    viewBox="0 0 16 16"
                    width="15"
                    height="15"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="1.4"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                    aria-hidden="true"
                  >
                    <rect x="2" y="2.5" width="12" height="11" rx="2" />
                    <path d="M6.5 2.5v11" />
                  </svg>
                </button>
              </el-tooltip>
              <span class="chat-title">{{ current?.title || 'Agent 对话' }}</span>
              <el-tag
                v-if="current?.skill_name"
                size="small"
                effect="plain"
                class="chat-header-tag"
              >
                {{ current.skill_name }}
              </el-tag>
              <el-tag
                v-if="current?.model"
                size="small"
                type="info"
                effect="plain"
                class="chat-header-tag"
              >
                {{ current.model }}
              </el-tag>
            </div>
            <div class="chat-header-actions">
              <el-button link @click="infoDrawerVisible = true">
                <el-icon><InfoFilled /></el-icon>
                <span>会话信息与溯源</span>
              </el-button>
              <el-button link :disabled="!current" @click="toggleConfig">
                {{ configExpanded ? '收起配置' : '修改配置' }}
              </el-button>
            </div>
          </div>
        </template>
        <div v-if="configExpanded && current" id="agent-config-editor" class="config-editor">
          <div class="config-editor-fields">
            <div class="config-field">
              <label class="config-field-label">专家技能</label>
              <el-select
                :model-value="currentSkillDraft"
                placeholder="不指定技能（通用对话）"
                clearable
                :disabled="!store.agentAvailable || !current || store.isBusy"
                :loading="store.skillsLoading"
                class="config-field-control"
                @update:model-value="onSkillDraftChange"
              >
                <el-option
                  v-for="item in store.skills"
                  :key="item.name"
                  :label="item.name"
                  :value="item.name"
                />
              </el-select>
            </div>
            <div class="config-field">
              <label class="config-field-label">模型</label>
              <el-input
                :model-value="currentModelDraft"
                placeholder="provider/model，留空则用后端默认"
                clearable
                :disabled="!store.agentAvailable || !current || store.isBusy"
                class="config-field-control"
                @update:model-value="onModelDraftChange"
              />
            </div>
            <div v-if="modelCandidates.length" class="config-field">
              <label class="config-field-label">已配置模型</label>
              <el-select
                placeholder="从已配置模型选"
                class="config-field-control"
                :disabled="!store.agentAvailable || !current || store.isBusy"
                @update:model-value="onModelDraftChange"
              >
                <el-option
                  v-for="item in modelCandidates"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-select>
            </div>
          </div>
          <div class="config-editor-footer">
            <el-button
              type="primary"
              size="small"
              :loading="configSaving"
              :disabled="!current || store.isBusy"
              @click="saveCurrentConfig"
            >
              保存
            </el-button>
            <span class="config-hint">
              {{ store.isBusy
                ? '本轮回答结束后、下一条消息起生效'
                : '修改后保存，下一条消息起生效（无需新建会话）' }}
            </span>
          </div>
        </div>
        <div v-if="!current" class="chat-placeholder">
          <el-empty description="选择左侧会话，或新建一个对话" :image-size="72" />
        </div>
        <ChatPanel v-else @draft-saved="onDraftSaved" />
      </el-card>


    </div>

    <el-drawer
      v-model="infoDrawerVisible"
      title="会话信息与溯源"
      size="380px"
      destroy-on-close
      class="info-drawer"
    >
      <div class="info-drawer-body">
        <dl v-if="current" class="info-list">
          <div class="info-row">
            <dt>状态</dt>
            <dd>
              <el-tag size="small" effect="plain" :type="conversationStatusTagType(current.status)">
                {{ conversationStatusLabel(current.status) }}
              </el-tag>
            </dd>
          </div>
          <div class="info-row">
            <dt>技能</dt>
            <dd>{{ current.skill_name || '未指定' }}</dd>
          </div>
          <div class="info-row">
            <dt>模型</dt>
            <dd>{{ current.model || '后端默认' }}</dd>
          </div>
          <div class="info-row">
            <dt>DSH 会话</dt>
            <dd class="mono">{{ current.dsh_session_id || '尚未建立' }}</dd>
          </div>
          <div class="info-row">
            <dt>工作目录</dt>
            <dd class="mono">{{ current.workspace_dir || '—' }}</dd>
          </div>
          <div class="info-row">
            <dt>创建时间</dt>
            <dd>{{ formatTime(current.created_at) }}</dd>
          </div>
        </dl>
        <el-empty v-else description="未选择会话" :image-size="56" />

        <div v-if="savedTaskId" class="saved-block">
          <div class="saved-head">
            <el-icon><View /></el-icon>
            <span>已转存为用例草稿</span>
          </div>
          <p class="saved-text">
            草稿挂在生成任务 #{{ savedTaskId }} 下，可在任务详情页走既有的确认与入库流程。
          </p>
          <el-button size="small" type="primary" plain @click="openTaskDetail(savedTaskId)">
            打开任务详情
          </el-button>
        </div>

        <div v-if="citationsLoading" class="citation-block citation-loading">
          正在载入引用溯源…
        </div>
        <div v-else-if="citations.length" class="citation-block">
          <CitationList :citations="citations" :count="citations.length" :space-id="spaceId" />
        </div>
      </div>
    </el-drawer>

    <el-dialog v-model="createDialogVisible" title="新建 Agent 对话" width="560px">
      <el-form label-position="top">
        <el-form-item label="会话标题">
          <el-input
            v-model="createTitle"
            placeholder="留空则由后端按首条消息生成"
            maxlength="120"
            show-word-limit
          />
        </el-form-item>
        <el-form-item label="技能与模型">
          <SkillPicker
            :skills="store.skills"
            :models="modelOptions"
            :model-value="createModel"
            :model-loading="store.skillsLoading || store.modelsLoading"
            @update:model-value="createModel = $event"
            @skill-change="createSkill = $event"
            @refresh="refreshSkills"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">创建并开始对话</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.agent-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex: 1 1 auto;
  height: auto;
  min-height: 0;
  position: relative;
  overflow: visible;
  padding: 0;
  background: transparent;
  border: none;
  border-radius: 0;
  box-shadow: none;
}

.block {
  margin-bottom: 0;
  border-radius: 0 !important;
  background: transparent;
  min-height: 0;
  overflow: hidden;
}

.page-alert {
  padding: 8px 12px;
}

.block-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-height: 28px;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--cg-text, #09090b);
}

.list-header-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
  min-width: 0;
}

.list-header-title span {
  white-space: nowrap;
}

.list-header-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
}

.chat-header-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
}

.chat-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--cg-text, #09090b);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-header-tag {
  flex-shrink: 0;
  font-family: var(--cg-font-sans);
}

.chat-header-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

/* 头部下方可展开的配置编辑区 */
.config-editor {
  flex: 0 0 auto;
  padding: 12px 14px;
  border-bottom: 1px solid var(--cg-border, #e4e4e7);
  background: var(--cg-surface-muted, #f4f4f5);
}

.config-editor-fields {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 10px 14px;
}

.config-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.config-field-label {
  font-size: 12px;
  color: var(--cg-text-muted, #71717a);
}

.config-field-control {
  width: 100%;
}

.config-editor-footer {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  flex-wrap: wrap;
}

.config-hint {
  font-size: 12px;
  color: var(--cg-text-muted, #71717a);
}

.agent-layout {
  display: grid;
  grid-template-columns: clamp(332px, 24vw, 368px) minmax(0, 1fr);
  gap: 0;
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
  transition: grid-template-columns 260ms cubic-bezier(0.22, 1, 0.36, 1);
}

.agent-layout.layout--list-collapsed {
  grid-template-columns: 0 minmax(0, 1fr);
}

.layout--list-collapsed .list-block {
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
  border-right-color: transparent !important;
  border-right-width: 0;
  transition: opacity 140ms ease, border-right-color 220ms ease, visibility 0s linear 260ms;
}

.layout--list-collapsed .chat-block {
  grid-column: 2;
}

.list-collapse-btn,
.list-expand-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  width: 28px;
  height: 24px;
  padding: 0;
  border: 1px solid var(--cg-border, #e4e4e7);
  border-radius: 6px;
  background: var(--cg-surface, #fff);
  color: var(--cg-text-muted, #71717a);
  cursor: pointer;
  flex: 0 0 auto;
  transition: background-color 0.15s ease, color 0.15s ease, border-color 0.15s ease;
}

.list-collapse-btn svg,
.list-expand-btn svg {
  display: block;
}

.list-collapse-btn:hover,
.list-expand-btn:hover {
  background: var(--cg-surface-muted, #f4f4f5);
  color: var(--cg-text, #09090b);
  border-color: var(--cg-border-strong, #d4d4d8);
}

.list-collapse-btn:focus-visible,
.list-expand-btn:focus-visible {
  outline: 2px solid var(--cg-focus-ring, #10b981);
  outline-offset: 2px;
}

.list-block,
.chat-block {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  border: none !important;
  box-shadow: none !important;
}

.list-block {
  border-right: 1px solid var(--cg-border, #e4e4e7) !important;
  transition: opacity 180ms ease 40ms, border-right-color 220ms ease, visibility 0s linear 0s;
}

.list-block :deep(.el-card__header),
.chat-block :deep(.el-card__header) {
  flex: 0 0 auto;
  padding: 10px 12px;
  border-bottom: 1px solid var(--cg-border, #e4e4e7);
}

.list-block :deep(.el-card__header) {
  min-width: 300px;
}

.list-block :deep(.el-card__body) {
  flex: 1;
  min-height: 0;
  min-width: 300px;
  padding: 8px;
  overflow-y: auto;
}

.chat-block :deep(.el-card__body) {
  flex: 1;
  min-height: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.chat-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}

.list-loading {
  padding: 12px;
  font-size: 12.5px;
  color: var(--cg-text-muted, #71717a);
}

.conversation-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.conversation-item {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 8px 10px;
  border: 1px solid var(--cg-border, #e4e4e7);
  border-radius: var(--cg-radius-sm, 6px);
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}

.conversation-item:hover {
  border-color: var(--cg-border-strong, #d4d4d8);
  background: var(--cg-surface-muted, #f4f4f5);
}

.conversation-item.active {
  border-color: var(--cg-border-strong, #d4d4d8);
  background: var(--cg-accent-brand-soft, #ecfdf5);
  box-shadow: inset 2px 0 0 var(--cg-accent-brand, #10b981);
}

.conversation-item.closed {
  opacity: 0.65;
}

.conversation-main {
  flex: 1;
  min-width: 0;
}

.conversation-title-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.conversation-icon {
  color: var(--cg-text-muted, #71717a);
  flex-shrink: 0;
}

.conversation-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--cg-text, #09090b);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conversation-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
  flex-wrap: wrap;
}

.meta-skill {
  font-family: var(--cg-font-sans);
  font-size: 11px;
  color: var(--cg-text-secondary, #52525b);
}

.meta-time {
  font-size: 11px;
  color: var(--cg-text-muted, #71717a);
}

.conversation-close {
  flex-shrink: 0;
}

.info-list {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.info-row {
  display: flex;
  gap: 8px;
  align-items: baseline;
}

.info-row dt {
  flex-shrink: 0;
  width: 68px;
  font-size: 12px;
  color: var(--cg-text-muted, #71717a);
}

.info-row dd {
  margin: 0;
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
  color: var(--cg-text, #09090b);
  overflow-wrap: anywhere;
}

.info-row dd.mono {
  font-family: var(--cg-font-mono);
  font-size: 11.5px;
  color: var(--cg-text-secondary, #52525b);
}

.saved-block {
  margin-top: 14px;
  padding: 10px 12px;
  border: 1px solid var(--cg-focus-ring, rgba(16, 185, 129, 0.2));
  border-radius: var(--cg-radius-sm, 6px);
  background: var(--cg-accent-brand-soft, #ecfdf5);
}

.saved-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--cg-accent-brand-deep, #059669);
}

.saved-text {
  margin: 6px 0 8px;
  font-size: 12px;
  line-height: 1.55;
  color: var(--cg-text-secondary, #52525b);
}

.citation-block {
  margin-top: 14px;
}

.citation-loading {
  font-size: 12px;
  color: var(--cg-text-muted, #71717a);
}

/* 抽屉内容沿用页面已有的 .info-* / .saved-* / .citation-* 样式 */
.info-drawer :deep(.el-drawer__body) {
  padding: 0;
}

.info-drawer-body {
  padding: 4px 16px 20px;
}

/* 观感：气泡与输入框更舒展（只改样式，不动功能与 props/emit 契约） */
.chat-block :deep(.bubble-body) {
  padding: 12px 16px;
  border-radius: var(--cg-radius-lg, 12px);
  line-height: 1.7;
}

.chat-block :deep(.is-user .bubble-body),
.chat-block :deep(.bubble-body.is-user-body) {
  max-width: 88%;
}

.chat-block :deep(.panel-scroll) {
  padding: 4px 10px 10px 4px;
}

.chat-block :deep(.chat-composer .el-textarea__inner) {
  border-radius: var(--cg-radius, 8px);
  padding: 10px 12px;
  line-height: 1.6;
}

@media (max-width: 1280px) {
  .agent-layout {
    grid-template-columns: clamp(324px, 26vw, 348px) minmax(0, 1fr);
  }
}

@media (max-width: 900px) {
  .agent-page {
    height: auto;
    overflow: visible;
  }

  .agent-layout {
    grid-template-columns: 1fr;
    height: auto;
    flex: 0 0 auto;
  }

  .list-block,
  .chat-block {
    height: auto;
    min-height: 360px;
  }

  .list-block {
    border-right: none !important;
    border-bottom: 1px solid var(--cg-border, #e4e4e7) !important;
  }

  .chat-header {
    flex-wrap: wrap;
  }

  .info-drawer {
    width: 100%;
  }
}

@media (prefers-reduced-motion: reduce) {
  .agent-layout,
  .list-block {
    transition: none !important;
  }
}
</style>
