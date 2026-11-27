<script setup lang="ts">
import { computed, ref } from 'vue'
import { View } from '@element-plus/icons-vue'
import MarkdownView from './MarkdownView.vue'
import { readKnowledgeSourceFile } from '../api/knowledgeSources'
import { useProjectStore } from '../projectStore'

export interface CitationItem {
  id?: number | null
  title: string
  path: string
  score?: number
  snippet?: string
  wiki_page_id?: number | null
  citation_type?: string
  source_chunk_id?: number | null
  knowledge_source_id?: number | null
  file_path?: string | null
  external_metadata?: Record<string, unknown>
  content_excerpt?: string
  clause_ids?: string[]
  anchor_clause?: string | null
  heading_path?: string | null
  headingPath?: string
  evidence_snippet?: string
  evidenceSnippet?: string
  available?: boolean
  legacy?: boolean
  legacy_reason?: string | null
}

const props = withDefaults(
  defineProps<{
    citations?: CitationItem[]
    count?: number
    projectId?: number | null
  }>(),
  {
    citations: () => [],
    count: 0,
    projectId: null,
  },
)

const projects = useProjectStore()
const drawerVisible = ref(false)
const loading = ref(false)
const activeIndex = ref(0)
const activeCitation = ref<CitationItem | null>(null)
const externalContent = ref('')
const externalContentNotice = ref('')
const showFullContent = ref(false)
let fetchSeq = 0

const total = computed(() => props.count || props.citations.length)

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

function citationSourceInfo(c: CitationItem | null | undefined) {
  const sourceName = readMetaString(c?.external_metadata, 'source_name')
  const providerType = readMetaString(c?.external_metadata, 'provider_type')
  const providerName = providerLabel(providerType)
  return { sourceName, providerType, providerName }
}

function isExternal(c: CitationItem | null | undefined) {
  return c?.citation_type === 'external'
}

function typeLabel(c: CitationItem) {
  if (c.legacy) return '历史引用'
  if (isExternal(c)) return '外部知识'
  return '知识引用'
}

function sourceBadgeLabel(c: CitationItem): string {
  if (c.legacy) return '历史引用'
  if (isExternal(c)) {
    const { sourceName, providerName } = citationSourceInfo(c)
    if (sourceName) return sourceName
    if (providerName) return providerName
  }
  return typeLabel(c)
}

function sourceDisplayText(c: CitationItem): string {
  const { sourceName, providerName } = citationSourceInfo(c)
  if (sourceName && providerName) return `${sourceName}（${providerName}）`
  if (sourceName) return sourceName
  if (providerName) return providerName
  return '—'
}

function typeTagType(c: CitationItem): 'success' | 'primary' | 'warning' | 'info' {
  if (c.legacy) return 'warning'
  if (isExternal(c)) {
    const { providerType } = citationSourceInfo(c)
    if (providerType === 'weknora') return 'success'
    if (providerType === 'llm_wiki_django') return 'primary'
    if (providerType === 'lexiang') return 'info'
    return 'warning'
  }
  return 'primary'
}

function openCitation(c: CitationItem, idx: number) {
  ++fetchSeq
  activeIndex.value = idx
  activeCitation.value = c
  drawerVisible.value = true
  externalContent.value = ''
  externalContentNotice.value = ''
  showFullContent.value = false
  loading.value = false
}

const isWeKnoraCitation = computed(
  () => citationSourceInfo(activeCitation.value).providerType === 'weknora',
)

const isLexiangCitation = computed(
  () => citationSourceInfo(activeCitation.value).providerType === 'lexiang',
)

const activeSourceUrl = computed(() => {
  const url = readMetaString(activeCitation.value?.external_metadata, 'source_url')
  return /^https?:\/\//i.test(url) ? url : ''
})

const activeFilePages = computed<number[]>(() => {
  const raw = activeCitation.value?.external_metadata?.file_pages
  if (!Array.isArray(raw)) return []
  return raw
    .map((item) => (typeof item === 'number' ? item : Number(item)))
    .filter((num) => Number.isInteger(num) && num > 0)
})

const activeFilePagesText = computed(() =>
  activeFilePages.value.length ? `第 ${activeFilePages.value.join(', ')} 页` : '',
)

const canLoadFullContent = computed(() => {
  const c = activeCitation.value
  if (!c || !isExternal(c) || c.legacy || isLexiangCitation.value) return false
  const pid = props.projectId ?? projects.state.currentId
  return Boolean(pid && c.knowledge_source_id != null && c.file_path)
})

const contentSectionTitle = computed(() => {
  const c = activeCitation.value
  if (!c) return '引用内容'
  if (c.legacy) return '历史摘录'
  if (isExternal(c)) {
    if (showFullContent.value && externalContent.value) {
      return isWeKnoraCitation.value ? '完整上下文' : '外部知识完整正文'
    }
    return '命中片段'
  }
  return '引用内容'
})

const toggleFullContentLabel = computed(() => {
  if (showFullContent.value) return '收起，返回命中片段'
  return isWeKnoraCitation.value ? '查看完整上下文' : '查看完整原文'
})

async function toggleFullContent() {
  const c = activeCitation.value
  if (!c || !canLoadFullContent.value) return

  if (showFullContent.value) {
    showFullContent.value = false
    return
  }

  if (externalContent.value) {
    externalContentNotice.value = ''
    showFullContent.value = true
    return
  }

  const pid = props.projectId ?? projects.state.currentId
  if (!pid || c.knowledge_source_id == null || !c.file_path) return

  const seq = ++fetchSeq
  loading.value = true
  externalContentNotice.value = ''
  try {
    const fileRes = await readKnowledgeSourceFile(pid, c.knowledge_source_id, c.file_path)
    if (seq !== fetchSeq) return
    const content = fileRes.content || ''
    if (!content) {
      externalContentNotice.value = isWeKnoraCitation.value
        ? '未获取到更多上下文内容，以下继续展示命中片段。'
        : '远端文件内容为空，以下继续展示命中片段。'
      return
    }
    externalContent.value = content
    showFullContent.value = true
  } catch (e) {
    if (seq !== fetchSeq) return
    const targetText = isWeKnoraCitation.value ? '完整上下文' : '完整原文'
    externalContentNotice.value = `暂无法加载${targetText}，以下继续展示命中片段（${(e as Error).message}）`
  } finally {
    if (seq === fetchSeq) loading.value = false
  }
}

function scoreText(score?: number) {
  if (score == null || Number.isNaN(score)) return ''
  return score.toFixed(1)
}

const bodyText = computed(() => {
  if (!activeCitation.value) return ''
  if (showFullContent.value && isExternal(activeCitation.value) && externalContent.value) {
    return externalContent.value
  }
  return (
    activeCitation.value.evidence_snippet ||
    activeCitation.value.evidenceSnippet ||
    activeCitation.value.content_excerpt ||
    activeCitation.value.snippet ||
    ''
  )
})
</script>

<template>
  <div class="citation-list">
    <div class="header">
      <span class="title">知识引用</span>
      <el-tag size="small" type="info" effect="plain">{{ total }} 条</el-tag>
    </div>

    <el-empty v-if="!total" description="暂无引用" :image-size="56" />

    <div v-else-if="citations.length" class="items">
      <button
        v-for="(c, idx) in citations"
        :key="c.id ?? `${c.path}-${idx}`"
        type="button"
        class="item"
        @click="openCitation(c, idx)"
      >
        <div class="item-top">
          <span class="ref-badge">[{{ idx + 1 }}]</span>
          <el-tag size="small" :type="typeTagType(c)" effect="plain" class="type-tag">
            {{ sourceBadgeLabel(c) }}
          </el-tag>
          <el-tag
            v-if="c.anchor_clause"
            size="small"
            type="warning"
            effect="plain"
            class="type-tag"
          >
            {{ c.anchor_clause }}
          </el-tag>
          <span class="item-title">{{ c.title || c.path || `引用 #${idx + 1}` }}</span>
          <span v-if="c.score != null" class="score">{{ scoreText(c.score) }}</span>
        </div>
        <div v-if="c.clause_ids?.length" class="item-clauses">
          <el-tag
            v-for="cid in c.clause_ids.slice(0, 8)"
            :key="cid"
            size="small"
            effect="plain"
            style="margin-right: 4px"
          >
            {{ cid }}
          </el-tag>
        </div>
        <div v-if="c.file_path || c.path" class="item-path">{{ c.file_path || c.path }}</div>
        <div v-if="c.snippet || c.content_excerpt" class="item-snippet">
          {{ c.content_excerpt || c.snippet }}
        </div>
        <div class="item-action">
          <el-icon :size="14"><View /></el-icon>
          <span>{{ c.legacy ? '点击查看历史摘录' : isExternal(c) ? '点击查看命中片段' : '点击查看引用详情' }}</span>
        </div>
      </button>
    </div>

    <div v-else class="summary">
      任务已关联 {{ total }} 条引用（详情见生成流水中的检索结果）。
    </div>

    <el-drawer
      v-model="drawerVisible"
      :title="
        activeCitation
          ? `引用 [${activeIndex + 1}] · ${typeLabel(activeCitation)} · ${activeCitation.title || ''}`
          : '引用详情'
      "
      size="52%"
      destroy-on-close
    >
      <div v-loading="loading" class="drawer-body">
        <el-alert
          v-if="externalContentNotice"
          :title="externalContentNotice"
          type="info"
          :closable="false"
          show-icon
        />

        <div v-if="activeCitation" class="meta-block">
          <div class="meta-row">
            <span class="meta-label">类型</span>
            <span class="meta-value">
              <el-tag size="small" :type="typeTagType(activeCitation)" effect="plain">
                {{ typeLabel(activeCitation) }}
              </el-tag>
            </span>
          </div>
          <div v-if="isExternal(activeCitation)" class="meta-row">
            <span class="meta-label">知识来源</span>
            <span class="meta-value">{{ sourceDisplayText(activeCitation) }}</span>
          </div>
          <el-alert
            v-if="activeCitation.legacy"
            :title="activeCitation.legacy_reason || '该引用来自旧任务，目标已不可用，以下保留历史摘录。'"
            type="warning"
            :closable="false"
            show-icon
          />
          <div class="meta-row">
            <span class="meta-label">路径</span>
            <span class="meta-value mono">{{ activeCitation.file_path || activeCitation.path || '—' }}</span>
          </div>
          <div class="meta-row">
            <span class="meta-label">相关分</span>
            <span class="meta-value">{{ scoreText(activeCitation.score) || '—' }}</span>
          </div>
          <div
            v-if="activeCitation.anchor_clause || activeCitation.clause_ids?.length"
            class="meta-row"
          >
            <span class="meta-label">条款</span>
            <span class="meta-value">
              <el-tag
                v-if="activeCitation.anchor_clause"
                size="small"
                type="warning"
                effect="plain"
                style="margin-right: 4px"
              >
                锚定 {{ activeCitation.anchor_clause }}
              </el-tag>
              <el-tag
                v-for="cid in activeCitation.clause_ids || []"
                :key="cid"
                size="small"
                effect="plain"
                style="margin-right: 4px"
              >
                {{ cid }}
              </el-tag>
            </span>
          </div>
          <div v-if="isExternal(activeCitation)" class="meta-row">
            <span class="meta-label">章节路径</span>
            <span class="meta-value">{{ activeCitation.heading_path || activeCitation.headingPath || '—' }}</span>
          </div>
          <div v-if="activeFilePagesText" class="meta-row">
            <span class="meta-label">页码</span>
            <span class="meta-value">{{ activeFilePagesText }}</span>
          </div>
        </div>

        <div class="content-box">
          <div class="section-label">
            {{ contentSectionTitle }}
          </div>
          <MarkdownView v-if="bodyText" :content="bodyText" />
          <el-empty v-else-if="!loading" description="无正文内容" :image-size="64" />
          <div v-if="canLoadFullContent || activeSourceUrl" class="content-actions">
            <span v-if="activeSourceUrl && activeFilePagesText" class="source-pages-hint">
              {{ activeFilePagesText }}
            </span>
            <a
              v-if="activeSourceUrl"
              :href="activeSourceUrl"
              target="_blank"
              rel="noopener noreferrer"
              class="el-button el-button--primary el-button--small is-plain source-link-btn"
            >
              打开原文档
            </a>
            <el-button
              v-if="canLoadFullContent"
              size="small"
              :type="showFullContent ? 'default' : 'primary'"
              plain
              :loading="loading"
              @click="toggleFullContent"
            >
              {{ toggleFullContentLabel }}
            </el-button>
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.citation-list {
  border: 1px solid var(--cg-border, #ebeef5);
  border-radius: var(--cg-radius-sm, 8px);
  padding: 12px;
  background: var(--cg-surface-muted, #f8faff);
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.title {
  font-weight: 700;
  color: var(--cg-text, #0f172a);
}

.items {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 250px;
  overflow-y: auto;
  overflow-x: hidden;
  padding-right: 4px;
  scrollbar-width: thin;
  scrollbar-color: var(--cg-border-strong, #cbd5e1) transparent;
}

.items::-webkit-scrollbar {
  width: 5px;
}

.items::-webkit-scrollbar-track {
  background: transparent;
}

.items::-webkit-scrollbar-thumb {
  background-color: var(--cg-border-strong, #cbd5e1);
  border-radius: 4px;
}

.item {
  display: block;
  width: 100%;
  text-align: left;
  background: #fff;
  border: 1px solid var(--cg-border, #ebeef5);
  border-radius: 8px;
  padding: 10px 12px;
  cursor: pointer;
  transition: border-color 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease;
  font: inherit;
  color: inherit;
}

.item:hover {
  border-color: rgba(16, 185, 129, 0.45);
  box-shadow: 0 4px 14px rgba(16, 185, 129, 0.1);
  transform: translateY(-1px);
}

.item-top {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.ref-badge {
  flex-shrink: 0;
  font-weight: 700;
  font-family: var(--cg-font-mono);
  font-size: 12px;
  color: #18181b;
  background: #f4f4f5;
  border: 1px solid #e4e4e7;
  border-radius: 4px;
  padding: 1px 6px;
  line-height: 1.5;
}

.type-tag {
  flex-shrink: 0;
}

.item-title {
  flex: 1;
  font-weight: 600;
  font-size: 13px;
  line-height: 1.4;
  color: var(--cg-text, #0f172a);
}

.score {
  flex-shrink: 0;
  color: var(--cg-text-muted, #909399);
  font-size: 12px;
}

.item-path {
  margin-top: 6px;
  margin-left: 36px;
  font-size: 11px;
  color: var(--cg-text-muted, #94a3b8);
  word-break: break-all;
}

.item-snippet {
  margin-top: 6px;
  margin-left: 36px;
  font-size: 12px;
  color: var(--cg-text-secondary, #475569);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.item-action {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-top: 8px;
  margin-left: 36px;
  font-size: 12px;
  color: var(--cg-primary);
  font-weight: 500;
}

.summary {
  font-size: 13px;
  color: var(--cg-text-secondary, #606266);
  line-height: 1.5;
}

.drawer-body {
  min-height: 200px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.meta-block {
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid var(--cg-border, #ebeef5);
  background: var(--cg-surface-muted, #f8faff);
}

.meta-row {
  display: flex;
  gap: 12px;
  margin-bottom: 8px;
  font-size: 13px;
}

.meta-label {
  flex: 0 0 56px;
  color: var(--cg-text-muted, #64748b);
}

.meta-value {
  flex: 1;
  min-width: 0;
  color: var(--cg-text, #0f172a);
  word-break: break-word;
}

.meta-value.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

.section-label {
  font-size: 12px;
  font-weight: 700;
  color: var(--cg-text-muted, #64748b);
  margin-bottom: 8px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.content-box {
  border: 1px solid var(--cg-border, #ebeef5);
  border-radius: 10px;
  padding: 12px 14px;
  background: #fff;
}

.content-actions {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--cg-border, #ebeef5);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

.source-pages-hint {
  font-size: 12px;
  color: var(--cg-text-muted, #64748b);
}

.source-link-btn {
  text-decoration: none;
}
</style>
