<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Document, View } from '@element-plus/icons-vue'
import MarkdownView from './MarkdownView.vue'
import { getWikiPage, type WikiPage } from '../api/wiki'
import { api } from '../api/client'

export interface CitationItem {
  id?: number | null
  title: string
  path: string
  score?: number
  snippet?: string
  wiki_page_id?: number | null
  citation_type?: string
  source_chunk_id?: number | null
  content_excerpt?: string
  clause_ids?: string[]
  anchor_clause?: string | null
  available?: boolean
  legacy?: boolean
  legacy_reason?: string | null
}

interface SourceChunkDetail {
  id: number
  document_id: number
  chunk_index: number
  title: string
  text: string
  start_char: number
  end_char: number
}

const props = withDefaults(
  defineProps<{
    citations?: CitationItem[]
    count?: number
    spaceId?: number
  }>(),
  {
    citations: () => [],
    count: 0,
  },
)

const router = useRouter()
const drawerVisible = ref(false)
const loading = ref(false)
const activeIndex = ref(0)
const activeCitation = ref<CitationItem | null>(null)
const pageDetail = ref<WikiPage | null>(null)
const chunkDetail = ref<SourceChunkDetail | null>(null)

const total = computed(() => props.count || props.citations.length)

function isSource(c: CitationItem | null | undefined) {
  return (c?.citation_type || 'wiki') === 'source' || !!c?.source_chunk_id
}

function typeLabel(c: CitationItem) {
  if (c.legacy) return '历史引用'
  return isSource(c) ? '原文' : 'Wiki'
}

function typeTagType(c: CitationItem): 'success' | 'primary' | 'warning' {
  if (c.legacy) return 'warning'
  return isSource(c) ? 'success' : 'primary'
}

async function openCitation(c: CitationItem, idx: number) {
  activeIndex.value = idx
  activeCitation.value = c
  drawerVisible.value = true
  pageDetail.value = null
  chunkDetail.value = null
  loading.value = true
  try {
    if (isSource(c)) {
      if (c.source_chunk_id != null) {
        chunkDetail.value = await api<SourceChunkDetail>(
          `/api/source-chunks/${c.source_chunk_id}?space_id=${props.spaceId || ''}`,
        )
      }
    } else if (c.wiki_page_id != null) {
      pageDetail.value = await getWikiPage(c.wiki_page_id, props.spaceId)
    }
  } catch (e) {
    ElMessage.error(`加载引用详情失败：${(e as Error).message}`)
  } finally {
    loading.value = false
  }
}

function goWikiPage() {
  const id = pageDetail.value?.id ?? activeCitation.value?.wiki_page_id
  if (id == null) {
    ElMessage.warning('该引用没有可跳转的 Wiki 页面')
    return
  }
  drawerVisible.value = false
  void router.push({
    path: '/wiki',
    query: { page: String(id), space_id: String(props.spaceId || '') },
  })
}

function scoreText(score?: number) {
  if (score == null || Number.isNaN(score)) return ''
  return score.toFixed(1)
}

const bodyText = computed(() => {
  if (chunkDetail.value?.text) return chunkDetail.value.text
  if (pageDetail.value?.content) return pageDetail.value.content
  if (activeCitation.value?.content_excerpt) return activeCitation.value.content_excerpt
  if (activeCitation.value?.snippet) return activeCitation.value.snippet
  return ''
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
        :class="{ 'item-source': isSource(c) }"
        @click="openCitation(c, idx)"
      >
        <div class="item-top">
          <span class="ref-badge">[{{ idx + 1 }}]</span>
          <el-tag size="small" :type="typeTagType(c)" effect="plain" class="type-tag">
            {{ typeLabel(c) }}
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
        <div v-if="c.path" class="item-path">{{ c.path }}</div>
        <div v-if="c.snippet || c.content_excerpt" class="item-snippet">
          {{ c.content_excerpt || c.snippet }}
        </div>
        <div class="item-action">
          <el-icon :size="14"><View /></el-icon>
          <span>{{ c.legacy ? '点击查看历史摘录' : isSource(c) ? '点击查看原文块' : '点击查看 Wiki 全文' }}</span>
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
        <div v-if="activeCitation" class="meta-block">
          <div class="meta-row">
            <span class="meta-label">类型</span>
            <span class="meta-value">
              <el-tag size="small" :type="typeTagType(activeCitation)" effect="plain">
                {{ typeLabel(activeCitation) }}
              </el-tag>
            </span>
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
            <span class="meta-value mono">{{ activeCitation.path || '—' }}</span>
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
          <div v-if="chunkDetail" class="meta-row">
            <span class="meta-label">定位</span>
            <span class="meta-value mono">
              文档 #{{ chunkDetail.document_id }} · 块 #{{ chunkDetail.chunk_index }} ·
              chars {{ chunkDetail.start_char }}–{{ chunkDetail.end_char }}
            </span>
          </div>
          <div v-if="pageDetail" class="meta-row">
            <span class="meta-label">页类型</span>
            <span class="meta-value">
              <el-tag size="small" effect="plain">{{ pageDetail.page_type || '—' }}</el-tag>
            </span>
          </div>
          <div v-if="pageDetail?.tags?.length" class="meta-row">
            <span class="meta-label">标签</span>
            <span class="meta-value tags">
              <el-tag
                v-for="tag in pageDetail.tags"
                :key="tag"
                size="small"
                type="info"
                effect="plain"
              >
                {{ tag }}
              </el-tag>
            </span>
          </div>
          <div v-if="!isSource(activeCitation)" class="meta-actions">
            <el-button
              type="primary"
              plain
              :disabled="!(pageDetail?.id || activeCitation.wiki_page_id)"
              @click="goWikiPage"
            >
              <el-icon style="margin-right: 4px"><Document /></el-icon>
              在 Wiki 页打开
            </el-button>
          </div>
        </div>

        <div class="content-box">
          <div class="section-label">
            {{ isSource(activeCitation) ? '原文内容（无损摘录）' : '页面正文' }}
          </div>
          <template v-if="bodyText">
            <pre v-if="isSource(activeCitation)" class="source-text">{{ bodyText }}</pre>
            <MarkdownView v-else :content="bodyText" />
          </template>
          <el-empty v-else-if="!loading" description="无正文内容" :image-size="64" />
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
  border-color: rgba(var(--cg-primary-rgb), 0.45);
  box-shadow: 0 4px 14px rgba(var(--cg-primary-rgb), 0.1);
  transform: translateY(-1px);
}

.item-source:hover {
  border-color: rgba(18, 184, 134, 0.5);
  box-shadow: 0 4px 14px rgba(18, 184, 134, 0.12);
}

.item-top {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.ref-badge {
  flex-shrink: 0;
  font-weight: 700;
  font-size: 12px;
  color: var(--cg-primary);
  background: rgba(var(--cg-primary-rgb), 0.1);
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

.meta-value.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.meta-actions {
  margin-top: 10px;
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

.source-text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Microsoft YaHei", monospace;
  font-size: 13px;
  line-height: 1.65;
  color: var(--cg-text, #0f172a);
}
</style>
