<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleClose, Document, Promotion } from '@element-plus/icons-vue'
import { importRequirementDoc } from '../../api/requirements'
import { AGENT_MESSAGE_MAX_CHARS } from '../../api/agent'

/**
 * 对话输入框 + 需求文档投喂（一体化悬浮输入舱）。
 * dropzone 与文档导入范式复用自 WorkbenchView.vue：
 * importRequirementDoc 解析 .md/.txt/.pdf/.docx，返回 text 追加进输入框（超长按 20000 字截断并提示）。
 * 另外后端对单条消息正文有 32000 字硬上限（AgentMessageCreate.max_length），
 * 导入截断后仍可能超限，所以发送前再拦一次，避免白跑一次 422。
 */

const props = defineProps<{
  disabled?: boolean
  sending?: boolean
  busy?: boolean
  placeholder?: string
  projectId?: number | null
}>()

const emit = defineEmits<{
  send: [content: string]
}>()

const MAX_IMPORT_CHARS = 20000

const draft = ref('')
const importingDoc = ref(false)
const docDragOver = ref(false)
const docFileInput = ref<HTMLInputElement | null>(null)
const sourceFilename = ref('')

/** 超出后端上限（AGENT_MESSAGE_MAX_CHARS）就禁止发送，字数条转红。 */
const overLimit = computed(() => draft.value.length > AGENT_MESSAGE_MAX_CHARS)

const canSend = computed(
  () => !props.disabled && !props.sending && !overLimit.value && Boolean(draft.value.trim()),
)

const charCount = computed(() => draft.value.length)

const charCountText = computed(() =>
  overLimit.value
    ? `${charCount.value} / ${AGENT_MESSAGE_MAX_CHARS} 字（超出上限，请删减）`
    : charCount.value > 0
      ? `${charCount.value} / ${AGENT_MESSAGE_MAX_CHARS} 字`
      : 'Ctrl / ⌘ + Enter 发送，Enter 换行；支持拖拽或导入需求文档',
)

watch(
  () => props.busy,
  (busy) => {
    if (busy) docDragOver.value = false
  },
)

function pickImportFile() {
  docFileInput.value?.click()
}

function onImportFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (file) void importDoc(file)
}

function onDropImport(event: DragEvent) {
  docDragOver.value = false
  const file = event.dataTransfer?.files?.[0]
  if (file) void importDoc(file)
}

async function importDoc(file: File) {
  importingDoc.value = true
  try {
    const result = await importRequirementDoc(file, props.projectId ?? null)
    let text = result.text
    if (text.length > MAX_IMPORT_CHARS) {
      text = text.slice(0, MAX_IMPORT_CHARS)
      ElMessage.warning(
        `已截断至 ${MAX_IMPORT_CHARS} 字，原文共 ${result.char_count} 字，请人工补齐关键内容`,
      )
    }
    if (!draft.value.trim()) {
      draft.value = text
    } else {
      let action = 'replace'
      try {
        action = 'append'
        await ElMessageBox.confirm(
          `已解析「${result.title}」共 ${result.char_count} 字。输入框已有内容，如何处理？`,
          '导入需求文档',
          {
            confirmButtonText: '追加',
            cancelButtonText: '覆盖',
            distinguishCancelAndClose: true,
            type: 'warning',
          },
        )
      } catch (choice) {
        if (choice === 'cancel') action = 'replace'
        else return
      }
      draft.value =
        action === 'append'
          ? `${draft.value}\n\n${text}`.slice(0, MAX_IMPORT_CHARS * 2)
          : text
    }
    sourceFilename.value = file.name
    ElMessage.success(`已导入 ${result.char_count} 字，请检查后发送`)
  } catch (error) {
    ElMessage.error(`导入需求文档失败：${(error as Error).message}`)
  } finally {
    importingDoc.value = false
  }
}

function clearAttachment() {
  sourceFilename.value = ''
}

function setDraft(text: string) {
  draft.value = text
}

function submit() {
  if (!canSend.value) return
  const content = draft.value.trim()
  if (!content) return
  if (overLimit.value) {
    ElMessage.warning(
      `消息超出 ${AGENT_MESSAGE_MAX_CHARS} 字上限（当前 ${charCount.value} 字），请删减后再发送`,
    )
    return
  }
  emit('send', content)
  draft.value = ''
  sourceFilename.value = ''
}

function onKeydown(event: KeyboardEvent) {
  // Ctrl/Cmd + Enter 发送；Enter 换行（长需求文档常含换行，避免误发）。
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault()
    submit()
  }
}

defineExpose({ submit, draft, setDraft })
</script>

<template>
  <div class="chat-composer">
    <div
      class="composer-dropzone"
      :class="{ 'drag-over': docDragOver, 'is-disabled': disabled, 'is-over-limit': overLimit }"
      @dragover.prevent="docDragOver = !disabled"
      @dragleave.prevent="docDragOver = false"
      @drop.prevent="onDropImport"
    >
      <div v-if="sourceFilename" class="source-file-row">
        <span class="source-file-name">
          <el-icon><Document /></el-icon>
          <span>{{ sourceFilename }}</span>
        </span>
        <el-button size="small" link type="danger" @click="clearAttachment">
          <el-icon><CircleClose /></el-icon>
          <span>移除</span>
        </el-button>
      </div>

      <el-input
        v-model="draft"
        type="textarea"
        :autosize="{ minRows: 2, maxRows: 7 }"
        resize="none"
        :disabled="disabled"
        :placeholder="placeholder || '描述你的测试设计需求，Agent 会多轮对话拆解测试点并生成结构化用例…'"
        @keydown="onKeydown"
      />

      <div class="composer-bar">
        <div class="composer-bar-left">
          <el-button
            size="small"
            plain
            class="import-doc-btn"
            :loading="importingDoc"
            :disabled="disabled"
            @click="pickImportFile"
          >
            <el-icon><Document /></el-icon>
            <span>从文档导入</span>
          </el-button>
          <span class="hint" :class="{ 'char-count-over': overLimit }">{{ charCountText }}</span>
        </div>

        <div class="composer-actions">
          <slot name="extra-actions" />
          <el-button
            type="primary"
            size="small"
            :disabled="!canSend"
            :loading="sending"
            class="send-button"
            @click="submit"
          >
            <el-icon><Promotion /></el-icon>
            <span>发送</span>
          </el-button>
        </div>
      </div>
    </div>

    <input
      ref="docFileInput"
      class="import-file-input"
      type="file"
      accept=".md,.txt,.pdf,.docx"
      @change="onImportFileChange"
    />
  </div>
</template>

<style scoped>
.chat-composer {
  width: 100%;
  max-width: 900px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
}

.composer-dropzone {
  width: 100%;
  border: 1px solid var(--cg-border, #e4e4e7);
  border-radius: 14px;
  background: var(--cg-surface, #ffffff);
  padding: 10px 14px 10px;
  box-shadow: 0 2px 10px rgba(9, 9, 11, 0.04);
  transition: border-color 0.18s ease, box-shadow 0.18s ease, background-color 0.18s ease;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.composer-dropzone:hover:not(.is-disabled) {
  border-color: var(--cg-border-strong, #d4d4d8);
}

.composer-dropzone:focus-within {
  border-color: var(--cg-accent-brand, #10b981);
  box-shadow: 0 0 0 3px var(--cg-focus-ring, rgba(16, 185, 129, 0.18)),
    0 4px 14px rgba(9, 9, 11, 0.05);
}

.composer-dropzone.is-over-limit {
  border-color: var(--el-color-danger, #f43f5e);
}

.composer-dropzone.drag-over {
  border-color: var(--cg-accent-brand, #10b981);
  background: var(--cg-accent-brand-soft, #ecfdf5);
}

/* 覆盖 theme.css 中对 .el-textarea__inner 的全局 !important 边框阴影，消除双层绿框 */
.composer-dropzone :deep(.el-textarea__inner),
.composer-dropzone :deep(.el-textarea__inner:hover),
.composer-dropzone :deep(.el-textarea__inner:focus) {
  border: none !important;
  box-shadow: none !important;
  outline: none !important;
  background: transparent !important;
  padding: 2px 0 !important;
  font-family: var(--cg-font-sans);
  font-size: 13.5px;
  line-height: 1.65;
  color: var(--cg-text, #09090b);
}

.composer-dropzone.is-disabled {
  opacity: 0.65;
  background: var(--cg-surface-muted, #f4f4f5);
}

.import-file-input {
  display: none;
}

.source-file-row {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 4px 10px;
  border-radius: 6px;
  background: var(--cg-surface-muted, #f4f4f5);
  border: 1px solid var(--cg-border, #e4e4e7);
  align-self: flex-start;
  max-width: 100%;
}

.source-file-name {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--cg-text-secondary, #52525b);
  font-family: var(--cg-font-sans);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding-top: 6px;
  border-top: 1px dashed rgba(228, 228, 231, 0.7);
  flex-wrap: wrap;
}

.composer-bar-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex-wrap: wrap;
}

.import-doc-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border-radius: 8px;
}

.hint {
  color: var(--cg-text-muted, #71717a);
  font-size: 12px;
  font-weight: 400;
  line-height: 1.4;
}

.char-count-over {
  color: var(--el-color-danger, #f43f5e);
  font-weight: 600;
}

.composer-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}

.send-button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border-radius: 8px;
  padding: 6px 14px;
}
</style>
