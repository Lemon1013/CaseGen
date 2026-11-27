<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { RetrievalCheckpoint, TaskCitation } from '../../api/tasks'

export interface RetrievalConfirmPayload {
  selectedCitationIds: number[]
  supplementalText: string
}

const props = defineProps<{
  checkpoint: RetrievalCheckpoint
  confirming: boolean
}>()

const emit = defineEmits<{
  (e: 'confirm', payload: RetrievalConfirmPayload): void
}>()

const selectedCitationIds = ref<number[]>([])
const supplementalText = ref('')
const expandedCitations = reactive(new Set<number>())

watch(
  () => `${props.checkpoint.id}:${props.checkpoint.version}`,
  () => {
    expandedCitations.clear()
    selectedCitationIds.value = props.checkpoint.selected_citation_ids.length
      ? [...props.checkpoint.selected_citation_ids]
      : props.checkpoint.candidate_citations.map((item) => item.id)
    supplementalText.value = props.checkpoint.supplemental_text || ''
  },
  { immediate: true },
)

const allowEmptyConfirm = computed(() => (
  props.checkpoint.candidate_citations.length === 0
  || (props.checkpoint.knowledge_status != null && props.checkpoint.knowledge_status !== 'ready')
))

const confirmDisabled = computed(() => (
  !allowEmptyConfirm.value
  && !selectedCitationIds.value.length
  && !supplementalText.value.trim()
))

function toggleCitation(id: number) {
  if (expandedCitations.has(id)) expandedCitations.delete(id)
  else expandedCitations.add(id)
}

function readMetaString(meta: Record<string, unknown> | undefined, key: string): string {
  const val = meta?.[key]
  return typeof val === 'string' ? val.trim() : ''
}

function providerLabel(providerType: string): string {
  if (!providerType) return ''
  if (providerType === 'weknora') return 'WeKnora'
  if (providerType === 'llm_wiki_django') return 'LLM Wiki'
  if (providerType === 'lexiang') return '腾讯乐享'
  return providerType
}

function citationSourceInfo(item: TaskCitation) {
  const sourceName = readMetaString(item.external_metadata, 'source_name')
  const providerType = readMetaString(item.external_metadata, 'provider_type')
  const providerName = providerLabel(providerType)
  return { sourceName, providerType, providerName }
}

function citationBadgeLabel(item: TaskCitation): string {
  if (item.legacy) return '历史引用'
  if (item.citation_type === 'external') {
    const { sourceName, providerName } = citationSourceInfo(item)
    if (sourceName && providerName) return `${providerName} · ${sourceName}`
    if (sourceName) return sourceName
    if (providerName) return providerName
    return '外部知识'
  }
  return '知识引用'
}

function citationBadgeType(item: TaskCitation): 'success' | 'primary' | 'warning' | 'info' {
  if (item.legacy) return 'warning'
  if (item.citation_type === 'external') {
    const { providerType } = citationSourceInfo(item)
    if (providerType === 'weknora') return 'success'
    if (providerType === 'llm_wiki_django') return 'primary'
    if (providerType === 'lexiang') return 'info'
    return 'warning'
  }
  return 'primary'
}

function citationMetaType(item: TaskCitation): string {
  if (item.legacy) return '历史引用'
  if (item.citation_type === 'external') {
    const { sourceName, providerName } = citationSourceInfo(item)
    if (sourceName && providerName) return `来源：${sourceName}（${providerName}）`
    if (sourceName) return `来源：${sourceName}`
    if (providerName) return `来源：${providerName}`
    return '外部知识'
  }
  return item.citation_type || '知识'
}

function handleConfirm() {
  if (props.confirming) return
  if (confirmDisabled.value) {
    ElMessage.warning('至少选择一条引用或填写补充上下文')
    return
  }
  emit('confirm', {
    selectedCitationIds: [...selectedCitationIds.value],
    supplementalText: supplementalText.value,
  })
}
</script>

<template>
  <el-card shadow="never" class="block checkpoint-card">
    <template #header>检索结果确认</template>
    <p class="meta">请确认将用于生成的知识引用，可取消不相关条目；也可补充背景上下文后继续。</p>

    <el-alert
      v-if="checkpoint.knowledge_status === 'not_configured'"
      type="warning"
      show-icon
      :closable="false"
      class="checkpoint-status-alert"
      title="该项目未配置知识库，可联系管理员添加"
      :description="checkpoint.knowledge_message || '确认后仍可继续后续测试点生成，您也可以在下方填写补充背景上下文。'"
    />
    <el-alert
      v-else-if="checkpoint.knowledge_status === 'no_hits'"
      type="info"
      show-icon
      :closable="false"
      class="checkpoint-status-alert"
      title="未检索到相关内容"
      :description="checkpoint.knowledge_message || '外部知识库未命中相关条目，可填写补充背景或直接确认继续生成测试点。'"
    />
    <el-alert
      v-if="checkpoint.knowledge_status === 'source_error' || checkpoint.source_warnings?.length"
      type="warning"
      show-icon
      :closable="false"
      class="checkpoint-status-alert"
      :title="checkpoint.knowledge_status === 'source_error' ? '外部知识库检索异常' : '部分外部知识库检索告警'"
    >
      <template #default>
        <p v-if="checkpoint.knowledge_message" class="checkpoint-alert-msg">
          {{ checkpoint.knowledge_message }}
        </p>
        <ul v-if="checkpoint.source_warnings?.length" class="checkpoint-warnings-list">
          <li
            v-for="(warn, idx) in checkpoint.source_warnings"
            :key="`${warn.source_id ?? 's'}-${idx}`"
          >
            <strong v-if="warn.source_name">{{ warn.source_name }}</strong>
            <span v-if="warn.category">（{{ warn.category }}）</span>
            <span v-if="warn.source_name || warn.category">：</span>
            {{ warn.message }}
          </li>
        </ul>
        <p class="checkpoint-alert-msg">您仍可基于已有命中结果或补充上下文确认并继续生成测试点。</p>
      </template>
    </el-alert>

    <el-checkbox-group
      v-if="checkpoint.candidate_citations.length"
      v-model="selectedCitationIds"
      class="checkpoint-list"
    >
      <el-checkbox
        v-for="item in checkpoint.candidate_citations"
        :key="item.id"
        :value="item.id"
        class="checkpoint-item"
      >
        <span class="checkpoint-title">
          <el-tag
            size="small"
            :type="citationBadgeType(item)"
            effect="plain"
            style="margin-right: 6px"
          >
            {{ citationBadgeLabel(item) }}
          </el-tag>
          {{ item.title }}
        </span>
        <span class="checkpoint-meta">
          {{ citationMetaType(item) }} · score {{ item.score.toFixed(3) }} · {{ item.file_path || item.path }}
        </span>
        <span
          class="checkpoint-snippet"
          :class="{ expanded: expandedCitations.has(item.id) }"
        >
          {{ item.snippet || item.content_excerpt }}
        </span>
        <el-button
          v-if="(item.snippet || item.content_excerpt || '').length >= 120"
          class="checkpoint-expand"
          link
          type="primary"
          @click.stop.prevent="toggleCitation(item.id)"
        >
          {{ expandedCitations.has(item.id) ? '收起' : '展开全文' }}
        </el-button>
      </el-checkbox>
    </el-checkbox-group>

    <el-input
      v-model="supplementalText"
      type="textarea"
      :rows="4"
      maxlength="10000"
      show-word-limit
      placeholder="补充上下文（可选）"
    />

    <div class="checkpoint-actions">
      <el-button
        type="primary"
        :loading="confirming"
        :disabled="confirmDisabled"
        @click="handleConfirm"
      >
        确认并继续生成测试点
      </el-button>
    </div>
  </el-card>
</template>

<style scoped>
.block {
  margin-bottom: 16px;
  border-radius: var(--cg-radius) !important;
}

.block :deep(.el-card__header) {
  font-weight: 700;
  border-bottom-color: var(--cg-border);
}

.meta {
  color: var(--cg-text-muted);
  font-size: 12px;
}

.checkpoint-list {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 10px;
  max-height: clamp(260px, calc(100vh - 420px), 540px);
  overflow-y: auto;
  overflow-x: hidden;
  margin: 12px 0;
  min-width: 0;
}

.checkpoint-status-alert {
  margin: 10px 0 12px;
}

.checkpoint-alert-msg {
  margin: 4px 0 0;
  font-size: 12.5px;
  line-height: 1.5;
}

.checkpoint-warnings-list {
  margin: 6px 0;
  padding-left: 18px;
  font-size: 12.5px;
  line-height: 1.6;
}

.checkpoint-actions {
  margin-top: 12px;
}

.checkpoint-item {
  display: flex;
  align-items: flex-start;
  width: 100%;
  min-width: 0;
  min-height: 32px;
  height: auto;
  margin-right: 0;
  box-sizing: border-box;
  white-space: normal;
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius-sm);
  padding: 10px;
}

.checkpoint-item :deep(.el-checkbox__input) {
  flex: 0 0 auto;
  margin-top: 2px;
}

.checkpoint-item :deep(.el-checkbox__label) {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-width: 0;
  max-width: 100%;
  line-height: 1.45;
  white-space: normal;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.checkpoint-title,
.checkpoint-meta,
.checkpoint-snippet {
  display: block;
  max-width: 100%;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.checkpoint-title {
  font-weight: 700;
}

.checkpoint-meta {
  color: var(--cg-text-muted);
  font-size: 12px;
}

.checkpoint-snippet {
  color: var(--cg-text-secondary);
  font-size: 12px;
  margin-top: 4px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.6;
  word-break: break-word;
}

.checkpoint-snippet.expanded {
  display: -webkit-box;
  -webkit-line-clamp: unset;
  max-width: 720px;
  line-height: 1.7;
  word-break: break-word;
}

.checkpoint-expand {
  align-self: flex-start;
  margin-top: 2px;
  height: auto;
  padding: 0;
  font-size: 12px;
  line-height: 1.5;
}

@media (max-width: 900px) {
  .checkpoint-list {
    max-height: none;
    overflow: visible;
  }
}
</style>
