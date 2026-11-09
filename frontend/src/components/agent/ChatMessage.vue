<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { CopyDocument, UserFilled } from '@element-plus/icons-vue'
import MarkdownView from '../MarkdownView.vue'
import CaseResultCard from './CaseResultCard.vue'
import { parseAgentCaseGroups, stripAgentCaseJson } from './parseAgentCases'
import { formatDateTime } from '../../utils/datetime'
import type { DraftPointGroup } from '../../api/tasks'

/**
 * 单条对话气泡。
 *
 * 性能守则（spec §3.6）：MarkdownView 每次 content 变化都会全量重 parse + 整块 v-html 替换，
 * 长对话累积是 O(n²) ⇒ 只有 streaming=true 的那条实时渲染 Markdown；
 * 历史 assistant 消息默认折叠成纯文本摘要（不做 markdown parse），展开时才渲染一次。
 */

const props = defineProps<{
  role: 'user' | 'assistant'
  content: string
  createdAt?: string
  streaming?: boolean
  pending?: boolean
  /** 折叠阈值（字符）；超出则历史消息默认收起。0 = 不折叠。 */
  collapseThreshold?: number
  /** 是否展示「转存为用例草稿」入口（只对含用例 JSON 的 assistant 消息有意义）。 */
  canSaveDraft?: boolean
  savingDraft?: boolean
  /** 预留：Agent 思考过程文本；无值时不渲染，不伪造内容。 */
  thought?: string
}>()

const emit = defineEmits<{
  saveDraft: []
}>()

const expanded = ref(false)
const showCases = ref(false)

const displayContent = computed(() =>
  props.role === "assistant" ? stripAgentCaseJson(props.content || "") : (props.content || ""),
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

/** 只在展开或流式时解析用例 JSON，避免每条历史消息都做正则 + JSON.parse。 */
const caseGroups = computed<DraftPointGroup[] | null>(() => {
  if (props.role !== 'assistant') return null
  if (!props.content) return null
  if (props.streaming) return null
  if (isCollapsed.value) return null
  if (!showCases.value) return null
  return parseAgentCaseGroups(props.content)
})

const hasCases = computed(() => {
  if (props.role !== 'assistant' || !props.content) return false
  return props.content.includes('```json')
})

const isUser = computed(() => props.role === 'user')

function toggleExpanded() {
  expanded.value = !expanded.value
}

async function copyContent() {
  const payload = displayContent.value
  if (!payload) {
    ElMessage.warning('这条消息没有可复制的内容')
    return
  }
  try {
    await navigator.clipboard.writeText(payload)
    ElMessage.success('消息内容已复制到剪贴板')
  } catch {
    const textarea = document.createElement('textarea')
    textarea.value = payload
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    document.execCommand('copy')
    document.body.removeChild(textarea)
    ElMessage.success('消息内容已复制到剪贴板')
  }
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
      <details v-if="thought" class="thought-block" :open="streaming">
        <summary class="thought-summary">
          <span class="thought-label">思考过程</span>
          <span class="thought-hint">{{ streaming ? '思考中…' : '已深度思考' }}</span>
        </summary>
        <div class="thought-body">
          <MarkdownView :content="thought" />
        </div>
      </details>
      <div
        v-if="displayContent || streaming || isUser"
        class="bubble-body"
        :class="{ 'is-user-body': isUser }"
      >
        <MarkdownView v-if="streaming || (!isUser && !isCollapsed)" :content="displayContent" />
        <pre v-else-if="isUser" class="plain-text">{{ content }}</pre>
        <div v-else class="collapsed-preview">
          <MarkdownView class="preview-text" :content="previewText" />
          <el-button size="small" link type="primary" @click="toggleExpanded">展开全文</el-button>
        </div>
        <div v-if="streaming && !content" class="streaming-placeholder">
          <span class="dot" />
          <span class="dot" />
          <span class="dot" />
        </div>
      </div>
      <div v-if="!isUser && !isCollapsed" class="bubble-actions">
        <el-button v-if="collapsible && expanded" size="small" link @click="toggleExpanded">收起</el-button>
        <el-button size="small" link class="copy-action" @click="copyContent">
          <el-icon><CopyDocument /></el-icon>
          <span>复制</span>
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
          v-if="canSaveDraft && !streaming"
          size="small"
          type="primary"
          plain
          :loading="savingDraft"
          @click="emit('saveDraft')"
        >
          转存为用例草稿
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
  gap: 10px;
  padding: 10px 0;
}

.chat-message.is-user {
  flex-direction: row-reverse;
}

.avatar-col {
  flex-shrink: 0;
}

.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  font-size: 12px;
  font-weight: 700;
  font-family: var(--cg-font-sans);
}

.avatar-user {
  background: var(--cg-surface-muted, #f4f4f5);
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
  margin-bottom: 8px;
  padding: 6px 10px;
  border: 1px solid var(--cg-border, #e4e4e7);
  border-radius: var(--cg-radius-sm, 6px);
  background: var(--cg-surface-muted, #f4f4f5);
  color: var(--cg-text-muted, #71717a);
  font-size: 12.5px;
  line-height: 1.6;
}

.thought-summary {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  list-style: none;
  color: var(--cg-text-muted, #71717a);
}

.thought-summary::-webkit-details-marker {
  display: none;
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
  padding-top: 6px;
}

.bubble-body {
  max-width: 100%;
  border: 1px solid var(--cg-border, #e4e4e7);
  border-radius: var(--cg-radius, 8px);
  background: var(--cg-surface, #ffffff);
  padding: 10px 14px;
  font-size: 13.5px;
  line-height: 1.65;
  color: var(--cg-text, #09090b);
  overflow-wrap: anywhere;
}

.is-user .bubble-body,
.bubble-body.is-user-body {
  background: var(--cg-primary-soft-2, rgba(16, 185, 129, 0.08));
  border-color: var(--cg-focus-ring, rgba(16, 185, 129, 0.2));
  max-width: 82%;
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
  gap: 4px;
  align-items: center;
  padding: 2px 0;
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
  gap: 6px;
  flex-wrap: wrap;
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
</style>
