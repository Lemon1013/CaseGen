<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleClose, Document, Promotion } from '@element-plus/icons-vue'
import { importRequirementDoc } from '../../api/requirements'
import { AGENT_MESSAGE_MAX_CHARS } from '../../api/agent'

/**
 * 对话输入框 + 需求文档投喂。
 * dropzone 与文档导入范式复用自 WorkbenchView.vue:125-172 / :239-253：
 * importRequirementDoc 解析 .md/.txt/.pdf/.docx，返回 text 追加进输入框（超长按 20000 字截断并提示）。
 * 另外后端对单条消息正文有 32000 字硬上限（AgentMessageCreate.max_length），
 * 导入截断后仍可能超限，所以发送前再拦一次，避免白跑一次 422。
 * 文档只是拼进 prompt 的参考材料，不上传到 agent workspace（spec §3.3 材料投递由后端负责）。
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
    : `${charCount.value} 字`,
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

defineExpose({ submit, draft })
</script>

<template>
  <div class="chat-composer">
    <div class="composer-toolbar">
      <span class="hint">Ctrl / ⌘ + Enter 发送，Enter 换行；可投喂需求文档作为参考材料</span>
      <el-button size="small" plain :loading="importingDoc" :disabled="disabled" @click="pickImportFile">
        <el-icon><Document /></el-icon>
        <span>从文档导入</span>
      </el-button>
    </div>
    <div
      class="composer-dropzone"
      :class="{ 'drag-over': docDragOver, 'is-disabled': disabled }"
      @dragover.prevent="docDragOver = !disabled"
      @dragleave.prevent="docDragOver = false"
      @drop.prevent="onDropImport"
    >
      <el-input
        v-model="draft"
        type="textarea"
        :rows="3"
        resize="none"
        :disabled="disabled"
        :placeholder="placeholder || '描述你的测试设计需求，Agent 会多轮对话产出用例…'"
        @keydown="onKeydown"
      />
      <div v-if="sourceFilename" class="source-file-row">
        <span class="source-file-name">
          <el-icon><Document /></el-icon>
          {{ sourceFilename }}
        </span>
        <el-button size="small" link type="danger" @click="clearAttachment">
          <el-icon><CircleClose /></el-icon>
          移除
        </el-button>
      </div>
    </div>
    <div class="composer-footer">
      <span class="hint" :class="{ 'char-count-over': overLimit }">{{ charCountText }}</span>
      <div class="composer-actions">
        <slot name="extra-actions" />
        <el-button
          type="primary"
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
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.composer-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.composer-toolbar .el-button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.hint {
  color: var(--cg-text-muted, #71717a);
  font-size: 12px;
  font-weight: 400;
}

.char-count-over {
  color: var(--el-color-danger, #f56c6c);
  font-weight: 600;
}

.composer-dropzone {
  width: 100%;
  border: 1px solid var(--cg-border, #e4e4e7);
  border-radius: var(--cg-radius-lg, 12px);
  background: var(--cg-surface, #ffffff);
  padding: 6px 10px;
  transition: border-color 0.15s ease, box-shadow 0.15s ease, background-color 0.15s ease;
}

.composer-dropzone:focus-within {
  border-color: var(--cg-accent-brand, #10b981);
  box-shadow: 0 0 0 3px var(--cg-focus-ring, rgba(16, 185, 129, 0.2));
}

.composer-dropzone.drag-over {
  border-color: var(--cg-accent-brand, #10b981);
  background: var(--cg-accent-brand-soft, #ecfdf5);
}

.composer-dropzone :deep(.el-textarea__inner) {
  border: none;
  box-shadow: none;
  background: transparent;
  padding: 2px;
  font-family: var(--cg-font-sans);
  font-size: 13.5px;
  line-height: 1.6;
}

.composer-dropzone.is-disabled {
  opacity: 0.6;
}

.import-file-input {
  display: none;
}

.source-file-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 6px;
}

.source-file-name {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--cg-text-secondary, #52525b);
  font-family: var(--cg-font-mono);
}

.composer-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.composer-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.send-button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
</style>
