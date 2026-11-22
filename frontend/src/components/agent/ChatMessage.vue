<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowRight, CopyDocument, UserFilled } from '@element-plus/icons-vue'
import MarkdownView from '../MarkdownView.vue'
import CaseResultCard from './CaseResultCard.vue'
import { parseAgentCaseGroups, stripAgentCaseJson } from './parseAgentCases'
import { copyText } from '../../utils/clipboard'
import { formatDateTime } from '../../utils/datetime'
import type { DraftPointGroup } from '../../api/tasks'
import type { AgentAttachmentItem } from '../../api/agent'

/**
 * 单条对话气泡。
 *
 * 性能守则（spec §3.6）：MarkdownView 每次 content 变化都会全量重 parse + 整块 v-html 替换，
 * 长对话累积是 O(n²) ⇒ 只有 streaming=true 的那条实时渲染 Markdown；
 * 历史 assistant 消息按 collapseThreshold 控制折叠（默认 0 全量 Markdown 渲染）。
 */

const props = defineProps<{
  messageId: string
  role: 'user' | 'assistant'
  content: string
  createdAt?: string
  streaming?: boolean
  pending?: boolean
  /** 折叠阈值（字符）；超出则历史消息默认收起。0 = 不折叠。 */
  collapseThreshold?: number
  /** 会话忙碌或已关闭时禁用入库操作。 */
  saveDisabled?: boolean
  /** Agent 思考过程文本；无值时不渲染，不伪造内容。 */
  thought?: string
  attachments?: AgentAttachmentItem[]
}>()

const emit = defineEmits<{
  saveToCases: [messageId: string]
}>()

const expanded = ref(false)
const showCases = ref(false)
const thoughtOpen = ref(Boolean(props.streaming))

watch(
  () => props.streaming,
  (isStreaming, wasStreaming) => {
    if (isStreaming && !wasStreaming) thoughtOpen.value = true
    else if (!isStreaming && wasStreaming) thoughtOpen.value = false
  },
)

function onThoughtToggle(event: Event) {
  const target = event.target as HTMLDetailsElement | null
  if (target) thoughtOpen.value = target.open
}

const displayContent = computed(() =>
  props.role === 'assistant' ? stripAgentCaseJson(props.content || '') : (props.content || ''),
)

const threshold = computed(() => props.collapseThreshold ?? 0)
const collapsible = computed(
  () => !props.streaming && threshold.value > 0 && displayContent.value.length > threshold.value,
)
const isCollapsed = computed(() => collapsible.value && !expanded.value)

const previewText = computed(() => {
  if (!isCollapsed.value) return ''
  return `${displayContent.value.slice(0, threshold.value).trimEnd()}…`
})

/** 仅在非流式时单次解析用例 JSON，供按钮显隐、数量统计与结构化卡片复用。 */
const parsedCaseGroups = computed<DraftPointGroup[] | null>(() => {
  if (props.role !== 'assistant' || !props.content || props.streaming) return null
  return parseAgentCaseGroups(props.content)
})

const hasCases = computed(() => Boolean(parsedCaseGroups.value && parsedCaseGroups.value.length > 0))
const caseCount = computed(() =>
  (parsedCaseGroups.value ?? []).reduce((sum, group) => sum + (group.cases?.length || 0), 0),
)
const caseGroups = computed<DraftPointGroup[] | null>(() => {
  if (!showCases.value || isCollapsed.value) return null
  return parsedCaseGroups.value
})
const canSaveCases = computed(
  () =>
    props.role === 'assistant' &&
    !props.streaming &&
    !props.messageId.startsWith('local-') &&
    hasCases.value,
)

const isUser = computed(() => props.role === 'user')

function formatAttachmentSize(size: number): string {
  if (size < 1024 * 1024) return String(Math.max(1, Math.round(size / 1024)) + ' KB')
  return String((size / 1024 / 1024).toFixed(2) + ' MB')
}

function toggleExpanded() {
  expanded.value = !expanded.value
}

async function copyContent() {
  const payload = displayContent.value || props.thought || ''
  if (!payload) {
    ElMessage.warning('这条消息没有可复制的内容')
    return
  }
  const copied = await copyText(payload)
  if (copied) ElMessage.success('消息内容已复制到剪贴板')
  else ElMessage.warning('复制失败，请手动复制')
}
</script>

<template>
  <div class="chat-message" :class="isUser ? 'is-user' : 'is-assistant'">
    <div class="avatar-col">
      <span class="avatar" :class="isUser ? 'avatar-user' : 'avatar-agent'">
        <el-icon v-if="isUser"><UserFilled /></el-icon>
        <template v-else>AI</template>
      </span>
    </div>
    <div class="bubble-col">
      <div class="bubble-head">
        <span class="role-label">{{ isUser ? '我' : 'Agent' }}</span>
        <span v-if="createdAt" class="time-label">{{ formatDateTime(createdAt) }}</span>
        <el-tag v-if="pending" size="small" type="info" effect="plain">发送中</el-tag>
        <el-tag v-else-if="streaming" size="small" type="warning" effect="plain">生成中</el-tag>
      </div>
      <details
        v-if="thought"
        class="thought-block"
        :open="thoughtOpen"
        @toggle="onThoughtToggle"
      >
        <summary class="thought-summary">
          <el-icon class="thought-chevron"><ArrowRight /></el-icon>
          <span class="thought-label">思考过程</span>
          <span class="thought-hint">{{ streaming ? '思考中…' : '已深度思考' }}</span>
        </summary>
        <div class="thought-body">
          <MarkdownView :content="thought" />
        </div>
      </details>
      <div
        v-if="displayContent || streaming || (isUser && !attachments?.length)"
        class="bubble-body"
        :class="{ 'is-user-body': isUser }"
      >
        <MarkdownView v-if="streaming || (!isUser && !isCollapsed)" :content="displayContent" />
        <pre v-else-if="isUser" class="plain-text">{{ content }}</pre>
        <div v-else class="collapsed-preview">
          <MarkdownView class="preview-text" :content="previewText" />
          <el-button size="small" link type="primary" @click="toggleExpanded">展开全文</el-button>
        </div>
        <div v-if="streaming && !displayContent" class="streaming-placeholder">
          <span class="dot" />
          <span class="dot" />
          <span class="dot" />
        </div>
      </div>
      <div v-if="isUser && attachments?.length" class="attachment-list">
        <el-tag v-for="item in attachments" :key="item.id" size="small" effect="plain">
          {{ item.filename }}（{{ formatAttachmentSize(item.size) }}）
        </el-tag>
      </div>
      <div v-if="!isUser && !isCollapsed && !streaming && (displayContent || thought || hasCases)" class="bubble-actions">
        <el-button v-if="collapsible && expanded" size="small" link @click="toggleExpanded">收起</el-button>
        <el-button v-if="displayContent || thought" size="small" link class="copy-action" @click="copyContent">
          <el-icon><CopyDocument /></el-icon>
          <span>{{ displayContent ? '复制' : '复制思考内容' }}</span>
        </el-button>
        <el-button
          v-if="hasCases"
          size="small"
          link
          type="primary"
          @click="showCases = !showCases"
        >
          {{ showCases ? '收起用例卡片' : '查看结构化用例' }}
        </el-button>
        <el-button
          v-if="canSaveCases"
          size="small"
          type="primary"
          plain
          :disabled="Boolean(saveDisabled)"
          @click="emit('saveToCases', messageId)"
        >
          入库至用例管理（{{ caseCount }}）
        </el-button>
      </div>
      <CaseResultCard
        v-if="caseGroups && caseGroups.length"
        class="bubble-cases"
        :groups="caseGroups"
        :default-expanded-count="1"
      />
    </div>
  </div>
</template>

<style scoped>
.chat-message {
  display: flex;
  gap: 12px;
  padding: 10px 0;
}

.chat-message.is-user {
  flex-direction: row-reverse;
}

.avatar-col {
  flex-shrink: 0;
  padding-top: 2px;
}

.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  font-size: 12px;
  font-weight: 700;
  font-family: var(--cg-font-sans);
  box-shadow: 0 1px 2px rgba(9, 9, 11, 0.06);
}

.avatar-user {
  background: var(--cg-surface, #ffffff);
  color: var(--cg-text-secondary, #52525b);
  border: 1px solid var(--cg-border, #e4e4e7);
}

.avatar-agent {
  background: var(--cg-primary, #09090b);
  color: var(--cg-text-on-dark, rgba(255, 255, 255, 0.92));
}

.bubble-col {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.is-user .bubble-col {
  align-items: flex-end;
}

.bubble-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--cg-text-muted, #71717a);
}

.is-user .bubble-head {
  flex-direction: row-reverse;
}

.role-label {
  font-weight: 600;
  color: var(--cg-text-secondary, #52525b);
}

.time-label {
  font-family: var(--cg-font-sans);
  font-size: 11px;
}

.thought-block {
  margin-bottom: 4px;
  padding: 8px 12px;
  border: 1px solid var(--cg-border, #e4e4e7);
  border-left: 3px solid var(--cg-accent-brand, #10b981);
  border-radius: var(--cg-radius, 8px);
  background: var(--cg-surface-muted, #f4f4f5);
  color: var(--cg-text-muted, #71717a);
  font-size: 12.5px;
  line-height: 1.6;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}

.thought-summary {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  list-style: none;
  user-select: none;
  color: var(--cg-text-muted, #71717a);
}

.thought-summary::-webkit-details-marker {
  display: none;
}

.thought-chevron {
  font-size: 12px;
  color: var(--cg-text-muted, #71717a);
  transition: transform 0.18s ease;
  flex-shrink: 0;
}

.thought-block[open] .thought-chevron {
  transform: rotate(90deg);
}

.thought-block[open] .thought-summary {
  padding-bottom: 6px;
  margin-bottom: 6px;
  border-bottom: 1px dashed var(--cg-border, #e4e4e7);
}

.thought-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--cg-text-secondary, #52525b);
}

.thought-hint {
  font-size: 11px;
  color: var(--cg-text-muted, #71717a);
}

.thought-body {
  max-height: 320px;
  overflow: auto;
  padding-top: 2px;
}

.bubble-body {
  max-width: 100%;
  border: 1px solid var(--cg-border, #e4e4e7);
  border-radius: 12px;
  background: var(--cg-surface, #ffffff);
  padding: 12px 16px;
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--cg-text, #09090b);
  overflow-wrap: anywhere;
  box-shadow: 0 1px 2px rgba(9, 9, 11, 0.03);
}

.is-user .bubble-body,
.bubble-body.is-user-body {
  background: var(--cg-accent-brand-soft, #ecfdf5);
  border-color: rgba(16, 185, 129, 0.28);
  max-width: 85%;
}

.plain-text {
  margin: 0;
  font-family: inherit;
  font-size: inherit;
  white-space: pre-wrap;
  word-break: break-word;
}

.collapsed-preview {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
}

.preview-text {
  margin: 0;
  color: var(--cg-text-secondary, #52525b);
}

.streaming-placeholder {
  display: inline-flex;
  gap: 5px;
  align-items: center;
  padding: 4px 2px;
}

.streaming-placeholder .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--cg-accent-brand, #10b981);
  animation: agent-dot-pulse 1.2s infinite ease-in-out;
}

.streaming-placeholder .dot:nth-child(2) {
  animation-delay: 0.2s;
}

.streaming-placeholder .dot:nth-child(3) {
  animation-delay: 0.4s;
}

@keyframes agent-dot-pulse {
  0%,
  80%,
  100% {
    opacity: 0.25;
    transform: scale(0.8);
  }
  40% {
    opacity: 1;
    transform: scale(1);
  }
}

.bubble-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding-left: 2px;
}

.copy-action {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--cg-text-muted, #71717a);
}

.bubble-cases {
  margin-top: 4px;
}

.attachment-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
</style>
