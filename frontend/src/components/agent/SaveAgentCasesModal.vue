<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { listRequirements, type RequirementItem } from '../../api/requirements'
import { ApiError } from '../../api/client'
import type {
  AgentConversationItem,
  AgentMessageItem,
  AgentSaveCasesConflictItem,
  AgentSaveCasesPayload,
  AgentSaveCasesResult,
  AgentSaveCasesTargetMode,
} from '../../api/agent'
import { useAgentChatStore } from '../../stores/agentChatStore'
import { parseAgentCaseGroups } from './parseAgentCases'
import {
  CASE_CONFLICT_STRATEGY_OPTIONS,
  caseStatusLabel,
  type CaseConflictStrategy,
} from '../../utils/caseDictionaries'

const props = defineProps<{
  modelValue: boolean
  projectId: number | null
  conversation: AgentConversationItem | null
  messages: AgentMessageItem[]
  messageId: string | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  saved: [result: AgentSaveCasesResult]
}>()

const store = useAgentChatStore()

const mounted = ref(true)
onUnmounted(() => {
  mounted.value = false
})

// 打开弹窗时锁定上下文快照，防止切项目或切会话瞬间串写。
const snapshotProjectId = ref<number | null>(null)
const snapshotConversationId = ref<string | null>(null)
const snapshotMessageId = ref<string | null>(null)

const targetMode = ref<AgentSaveCasesTargetMode>('existing_requirement')
const selectedRequirementId = ref<number | null>(null)
const conflictStrategy = ref<CaseConflictStrategy>('error')
const newTitle = ref('')
const newDescription = ref('')

const requirements = ref<RequirementItem[]>([])
const requirementsLoading = ref(false)
const submitting = ref(false)
const errorMessage = ref('')
const conflicts = ref<AgentSaveCasesConflictItem[]>([])

const targetMessage = computed<AgentMessageItem | null>(() => {
  const id = snapshotMessageId.value || props.messageId
  if (!id) return null
  return props.messages.find((item) => item.id === id) ?? null
})

const parsedPreview = computed(() => {
  const content = targetMessage.value?.content || ''
  if (!content) return { groupCount: 0, caseCount: 0, caseKeys: [] as string[] }
  const groups = parseAgentCaseGroups(content) ?? []
  const caseKeys: string[] = []
  groups.forEach((group) => {
    group.cases.forEach((item, idx) => {
      const raw = (item.case_key || '').trim()
      caseKeys.push(raw || `TC-${String(caseKeys.length + idx + 1).padStart(3, '0')}`)
    })
  })
  return {
    groupCount: groups.length,
    caseCount: caseKeys.length,
    caseKeys,
  }
})

function resetFormState() {
  errorMessage.value = ''
  conflicts.value = []
  submitting.value = false
}

function buildNewRequirementPrefill(messageId: string) {
  const list = props.messages
  const targetIndex = list.findIndex((item) => item.id === messageId)
  let prevUser: AgentMessageItem | null = null
  const upperBound = targetIndex >= 0 ? targetIndex : list.length
  for (let i = upperBound - 1; i >= 0; i -= 1) {
    if (list[i].role === 'user') {
      prevUser = list[i]
      break
    }
  }

  let attachments = prevUser?.attachments?.map((att) => att.filename).filter(Boolean) ?? []
  if (!attachments.length) {
    const seen = new Set<string>()
    for (let i = 0; i < upperBound; i += 1) {
      if (list[i].role !== 'user') continue
      for (const att of list[i].attachments ?? []) {
        const name = (att.filename || '').trim()
        if (name && !seen.has(name)) {
          seen.add(name)
          attachments.push(name)
        }
      }
    }
  }

  const convTitle = (props.conversation?.title || '').trim()
  const usableConvTitle =
    convTitle && convTitle !== '未命名对话' && convTitle !== '未命名会话' ? convTitle : ''
  const firstUserLine = (prevUser?.content || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find(Boolean) || ''

  newTitle.value = (
    usableConvTitle ||
    firstUserLine.slice(0, 80) ||
    (attachments.length ? `附件：${attachments[0]}`.slice(0, 80) : '') ||
    'Agent 对话需求'
  ).slice(0, 120)

  const summaryParts: string[] = []
  if (usableConvTitle) summaryParts.push(`会话标题：${usableConvTitle}`)
  if (prevUser?.content?.trim()) {
    const snippet = prevUser.content.trim().slice(0, 500)
    summaryParts.push(`用户提问：${snippet}${prevUser.content.trim().length > 500 ? '…' : ''}`)
  }
  if (attachments.length) {
    summaryParts.push(`关联附件：${attachments.join('、')}`)
  }
  newDescription.value = summaryParts.join('\n')
}

async function fetchProjectRequirements(targetProjectId: number) {
  requirementsLoading.value = true
  try {
    const rows = await listRequirements(targetProjectId)
    if (!mounted.value || !props.modelValue || snapshotProjectId.value !== props.projectId) return
    requirements.value = rows
    if (rows.length) {
      const hasSelected = rows.some((item) => item.id === selectedRequirementId.value)
      if (!hasSelected) selectedRequirementId.value = rows[0].id
    } else {
      selectedRequirementId.value = null
      targetMode.value = 'new_requirement'
    }
  } catch (err) {
    if (!mounted.value || !props.modelValue) return
    errorMessage.value = `加载项目需求列表失败：${(err as Error).message}`
  } finally {
    if (mounted.value) requirementsLoading.value = false
  }
}

watch(
  () => props.projectId,
  (nextId, prevId) => {
    if (nextId !== prevId && props.modelValue) {
      resetFormState()
      emit('update:modelValue', false)
    }
  },
)

watch(
  () => props.modelValue,
  (visible) => {
    if (!visible) {
      resetFormState()
      return
    }
    resetFormState()
    snapshotProjectId.value = props.projectId
    snapshotConversationId.value = props.conversation?.id ?? null
    snapshotMessageId.value = props.messageId
    targetMode.value = 'existing_requirement'
    conflictStrategy.value = 'error'
    selectedRequirementId.value = null
    if (props.messageId) {
      buildNewRequirementPrefill(props.messageId)
    }
    if (props.projectId) {
      void fetchProjectRequirements(props.projectId)
    } else {
      requirements.value = []
      targetMode.value = 'new_requirement'
    }
  },
)

watch([targetMode, selectedRequirementId, conflictStrategy], () => {
  errorMessage.value = ''
  if (targetMode.value === 'new_requirement') {
    conflicts.value = []
  }
})

function closeDialog() {
  if (submitting.value) return
  emit('update:modelValue', false)
}

function extractConflicts(error: unknown): AgentSaveCasesConflictItem[] {
  if (!(error instanceof ApiError)) return []
  const raw = error.rawDetail as { conflicts?: unknown } | undefined
  if (!raw || typeof raw !== 'object' || !Array.isArray(raw.conflicts)) return []
  return raw.conflicts as AgentSaveCasesConflictItem[]
}

async function handleSubmit() {
  errorMessage.value = ''
  if (!snapshotConversationId.value || !snapshotMessageId.value || !snapshotProjectId.value) {
    errorMessage.value = '会话或项目上下文失效，请关闭后重试'
    return
  }
  if (snapshotProjectId.value !== props.projectId) {
    emit('update:modelValue', false)
    return
  }
  if (targetMode.value === 'existing_requirement' && !selectedRequirementId.value) {
    errorMessage.value = '请选择要绑定的目标需求'
    return
  }
  if (targetMode.value === 'new_requirement' && !newTitle.value.trim()) {
    errorMessage.value = '请输入新需求标题'
    return
  }

  const payload: AgentSaveCasesPayload =
    targetMode.value === 'existing_requirement'
      ? {
          message_id: snapshotMessageId.value,
          target_mode: 'existing_requirement',
          requirement_id: selectedRequirementId.value,
          conflict_strategy: conflictStrategy.value,
        }
      : {
          message_id: snapshotMessageId.value,
          target_mode: 'new_requirement',
          requirement_title: newTitle.value.trim(),
          requirement_description: newDescription.value.trim(),
          conflict_strategy: 'error',
        }

  submitting.value = true
  try {
    const result = await store.saveMessageCases(payload, {
      conversationId: snapshotConversationId.value,
      projectId: snapshotProjectId.value,
    })
    if (!mounted.value || snapshotProjectId.value !== props.projectId) return
    ElMessage.success(result.message || `已入库 ${result.imported_count} 条用例`)
    emit('saved', result)
    emit('update:modelValue', false)
  } catch (err) {
    if (!mounted.value || snapshotProjectId.value !== props.projectId) return
    const parsedConflicts = extractConflicts(err)
    conflicts.value = parsedConflicts
    errorMessage.value = (err as Error).message || '入库失败，请检查输入后重试'
  } finally {
    if (mounted.value) submitting.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="入库至用例管理"
    width="640px"
    :close-on-click-modal="!submitting"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="save-summary">
      <div class="save-summary-head">
        <span>待入库用例预览：共 <strong>{{ parsedPreview.groupCount }}</strong> 个测试点分组、<strong>{{ parsedPreview.caseCount }}</strong> 条用例</span>
      </div>
      <div v-if="parsedPreview.caseKeys.length" class="save-summary-keys">
        <el-tag
          v-for="key in parsedPreview.caseKeys.slice(0, 12)"
          :key="key"
          size="small"
          effect="plain"
        >
          {{ key }}
        </el-tag>
        <span v-if="parsedPreview.caseKeys.length > 12" class="keys-more">
          等共 {{ parsedPreview.caseKeys.length }} 条
        </span>
      </div>
    </div>

    <el-form label-position="top" class="save-form" @submit.prevent>
      <el-form-item label="归属方式">
        <el-radio-group v-model="targetMode">
          <el-radio value="existing_requirement">绑定当前项目已有需求</el-radio>
          <el-radio value="new_requirement">新建需求并入库</el-radio>
        </el-radio-group>
      </el-form-item>

      <template v-if="targetMode === 'existing_requirement'">
        <el-form-item label="选择目标需求" required>
          <el-select
            v-model="selectedRequirementId"
            filterable
            :loading="requirementsLoading"
            placeholder="搜索或选择当前项目下的已有需求"
            style="width: 100%"
          >
            <el-option
              v-for="item in requirements"
              :key="item.id"
              :label="`#${item.id} ${item.title}`"
              :value="item.id"
            />
          </el-select>
          <div v-if="!requirementsLoading && !requirements.length" class="field-hint">
            当前项目尚无需求，请切换为「新建需求并入库」。
          </div>
        </el-form-item>

        <el-form-item label="重名用例编号处理策略">
          <el-radio-group v-model="conflictStrategy" class="strategy-group">
            <div v-for="option in CASE_CONFLICT_STRATEGY_OPTIONS" :key="option.value" class="strategy-item">
              <el-radio :value="option.value">{{ option.label }}</el-radio>
              <div class="strategy-desc">{{ option.description }}</div>
            </div>
          </el-radio-group>
        </el-form-item>
      </template>

      <template v-else>
        <el-form-item label="新需求标题" required>
          <el-input
            v-model="newTitle"
            maxlength="120"
            show-word-limit
            placeholder="请输入需求标题"
          />
        </el-form-item>
        <el-form-item label="新需求说明（已根据会话与附件自动预填，可编辑）">
          <el-input
            v-model="newDescription"
            type="textarea"
            :rows="4"
            maxlength="4000"
            show-word-limit
            placeholder="可选：补充需求背景、关联文档说明等"
          />
        </el-form-item>
      </template>
    </el-form>

    <el-alert
      v-if="errorMessage"
      type="error"
      show-icon
      :closable="false"
      :title="errorMessage"
      class="save-alert"
    />

    <div v-if="conflicts.length" class="conflict-panel">
      <div class="conflict-tip">
        检测到以下同编号用例已存在。你可以在上方切换为「覆盖更新已有用例」或「跳过已有同编号用例」后重新点击确认入库：
      </div>
      <el-table :data="conflicts" size="small" border max-height="200">
        <el-table-column prop="case_key" label="编号" width="110" />
        <el-table-column prop="existing_title" label="已有用例标题" min-width="150" show-overflow-tooltip />
        <el-table-column label="已有状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.existing_status === 'archived' ? 'info' : 'success'">
              {{ caseStatusLabel(row.existing_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="new_title" label="新生成标题" min-width="150" show-overflow-tooltip />
      </el-table>
    </div>

    <template #footer>
      <el-button :disabled="submitting" @click="closeDialog">取消</el-button>
      <el-button
        type="primary"
        :loading="submitting"
        :disabled="parsedPreview.caseCount === 0"
        @click="handleSubmit"
      >
        确认入库
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.save-summary {
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--cg-surface-subtle, #f8fafc);
  border: 1px solid var(--cg-border, #e2e8f0);
  margin-bottom: 14px;
}

.save-summary-head {
  font-size: 13px;
  color: var(--cg-text, #1e293b);
}

.save-summary-keys {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
  align-items: center;
}

.keys-more {
  font-size: 12px;
  color: var(--cg-text-muted, #64748b);
}

.field-hint {
  margin-top: 6px;
  font-size: 12px;
  color: var(--cg-text-muted, #64748b);
}

.strategy-group {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
  width: 100%;
}

.strategy-item {
  width: 100%;
  padding: 8px 10px;
  border-radius: 6px;
  border: 1px solid var(--cg-border, #e2e8f0);
}

.strategy-desc {
  margin-left: 22px;
  margin-top: 2px;
  font-size: 12px;
  color: var(--cg-text-muted, #64748b);
  line-height: 1.4;
}

.save-alert {
  margin-top: 8px;
}

.conflict-panel {
  margin-top: 12px;
}

.conflict-tip {
  font-size: 12px;
  color: var(--el-color-warning-dark-2, #b88230);
  margin-bottom: 8px;
}
</style>
