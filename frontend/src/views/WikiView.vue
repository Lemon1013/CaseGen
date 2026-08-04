<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import MarkdownView from '../components/MarkdownView.vue'
import {
  getSourceChunk,
  getWikiIndex,
  getWikiPage,
  listWikiPages,
  retrieveWiki,
  type RetrieveHit,
  type WikiPage,
} from '../api/wiki'

type ListItem = {
  id: number | null
  title: string
  path: string
  page_type: string
  tags: string[]
  score?: number
  snippet: string
  citation_type: 'wiki' | 'source'
  source_chunk_id?: number | null
  content?: string | null
  start_char?: number | null
  end_char?: number | null
  clause_ids?: string[]
  anchor_clause?: string | null
  page_key?: string | null
  domain?: string | null
  status?: string | null
  revision?: number | null
  source_document_id?: number | null
  explain?: Record<string, unknown> | null
  key: string
}

const route = useRoute()
const router = useRouter()

const loading = ref(false)
const pages = ref<WikiPage[]>([])
const query = ref('')
const searching = ref(false)
const hits = ref<RetrieveHit[] | null>(null)
const domainFilter = ref('')
const typeFilter = ref('')
const statusFilter = ref('')
const sourceFilter = ref('')
const tagFilter = ref('')
const retrievalMode = ref('')
const selectedKey = ref<string | null>(null)
const previewTitle = ref('Wiki Index')
const previewContent = ref('')
const previewKind = ref<'index' | 'wiki' | 'source'>('index')
const previewMeta = ref('')
const previewLoading = ref(false)
const previewDocumentId = ref<number | null>(null)

function isSourceHit(h: {
  citation_type?: string
  page_type?: string
  source_chunk_id?: number | null
  path?: string
}) {
  return (
    h.citation_type === 'source' ||
    h.page_type === 'source_chunk' ||
    !!h.source_chunk_id ||
    (h.path || '').startsWith('source://')
  )
}

function itemKey(kind: 'wiki' | 'source', id: number | null | undefined) {
  return `${kind}:${id ?? 'none'}`
}

const allItems = computed<ListItem[]>(() => {
  if (hits.value) {
    return hits.value.map((h) => {
      const source = isSourceHit(h)
      const kind = source ? 'source' : 'wiki'
      const id = source ? h.source_chunk_id ?? h.id : h.id
      return {
        id,
        title: h.title,
        path: h.path,
        page_type: h.page_type || (source ? 'source_chunk' : 'page'),
        tags: h.tags || [],
        score: h.score,
        snippet: h.snippet || '',
        citation_type: kind,
        source_chunk_id: h.source_chunk_id ?? (source ? h.id : null),
        content: h.content,
        start_char: h.start_char,
        end_char: h.end_char,
        clause_ids: h.clause_ids || [],
        anchor_clause: h.anchor_clause,
        page_key: h.page_key,
        domain: h.domain,
        status: h.status,
        revision: h.revision,
        source_document_id: h.source_document_id,
        explain: h.explain,
        key: itemKey(kind, id),
      }
    })
  }
  return pages.value.map((p) => ({
    id: p.id,
    title: p.title,
    path: p.path,
    page_type: p.page_type,
    tags: p.tags,
    score: undefined as number | undefined,
    snippet: '',
    citation_type: 'wiki' as const,
    source_chunk_id: null,
    content: null,
    page_key: p.page_key,
    domain: p.domain,
    status: p.status,
    revision: p.revision,
    source_document_id: p.source_document_id,
    key: itemKey('wiki', p.id),
  }))
})

const filterOptions = computed(() => ({
  domains: [...new Set(pages.value.map((page) => page.domain).filter(Boolean))].sort(),
  types: [...new Set(pages.value.map((page) => page.page_type).filter(Boolean))].sort(),
  statuses: [...new Set(pages.value.map((page) => page.status).filter(Boolean))].sort(),
  sources: [...new Set(pages.value.map((page) => page.source_document_id).filter((id) => id != null))].sort((a, b) => Number(a) - Number(b)),
  tags: [...new Set(pages.value.flatMap((page) => page.tags || []))].sort(),
}))

const displayList = computed(() => allItems.value.filter((item) => {
  if (domainFilter.value && item.domain !== domainFilter.value) return false
  if (typeFilter.value && item.page_type !== typeFilter.value) return false
  if (statusFilter.value && item.status !== statusFilter.value) return false
  if (sourceFilter.value && String(item.source_document_id ?? '') !== sourceFilter.value) return false
  if (tagFilter.value && !item.tags.includes(tagFilter.value)) return false
  return true
}))

const groupedItems = computed(() => {
  const groups = new Map<string, ListItem[]>()
  for (const item of displayList.value) {
    const domain = item.citation_type === 'source' ? '原文证据' : item.domain || '未分类'
    const key = `${domain} / ${item.page_type || 'page'}`
    groups.set(key, [...(groups.get(key) || []), item])
  }
  return [...groups.entries()].map(([label, items]) => ({ label, items }))
})

function highlightedSnippet(snippet: string) {
  const escaped = (snippet || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  return escaped
    .replace(/&lt;mark&gt;/g, '<mark>')
    .replace(/&lt;\/mark&gt;/g, '</mark>')
}

function expansionLabel(explain?: Record<string, unknown> | null) {
  if (explain?.algorithm !== 'one_hop_expansion') return ''
  const from = String(explain.from_page_key || '')
  const reasons = Array.isArray(explain.reasons) ? explain.reasons.join('、') : ''
  return `关联自 ${from || '高分页面'}${reasons ? `（${reasons}）` : ''}`
}

async function loadPages() {
  loading.value = true
  try {
    pages.value = await listWikiPages()
  } catch (e) {
    ElMessage.error(`加载 Wiki 页面失败：${(e as Error).message}`)
  } finally {
    loading.value = false
  }
}

async function loadIndex() {
  previewLoading.value = true
  try {
    const index = await getWikiIndex()
    selectedKey.value = null
    previewKind.value = 'index'
    previewDocumentId.value = null
    previewMeta.value = ''
    previewTitle.value = 'Wiki Index'
    previewContent.value = index.content || '# Wiki Index\n\n（空）'
  } catch (e) {
    ElMessage.error(`加载索引失败：${(e as Error).message}`)
  } finally {
    previewLoading.value = false
  }
}

function formatSourceMarkdown(opts: {
  title: string
  text: string
  documentId?: number | null
  chunkIndex?: number | null
  startChar?: number | null
  endChar?: number | null
  path?: string
}) {
  const lines = [
    `# ${opts.title || '原文块'}`,
    '',
    '> 来源：原文块（无损摘录，非 Wiki 摘要）',
  ]
  if (opts.documentId != null) {
    lines.push(`> 文档 ID：${opts.documentId}`)
  }
  if (opts.chunkIndex != null) {
    lines.push(`> 块序号：#${opts.chunkIndex}`)
  }
  if (opts.startChar != null && opts.endChar != null) {
    lines.push(`> 字符范围：${opts.startChar}–${opts.endChar}`)
  }
  if (opts.path) {
    lines.push(`> 路径：\`${opts.path}\``)
  }
  lines.push('', '---', '', opts.text || '（无内容）')
  return lines.join('\n')
}

async function openItem(item: ListItem) {
  if (item.citation_type === 'source') {
    await openSourceChunk(item)
  } else {
    await openWikiPage(item.id, item.title)
  }
}

async function openWikiPage(id: number | null | undefined, fallbackTitle?: string) {
  if (id == null) {
    ElMessage.warning('该结果没有可打开的页面 ID')
    return
  }
  previewLoading.value = true
  selectedKey.value = itemKey('wiki', id)
  previewKind.value = 'wiki'
  previewDocumentId.value = null
  previewMeta.value = ''
  try {
    const page = await getWikiPage(id)
    previewTitle.value = page.title || fallbackTitle || `页面 #${id}`
    previewContent.value = page.content || '（无内容）'
  } catch (e) {
    ElMessage.error(`加载页面失败：${(e as Error).message}`)
  } finally {
    previewLoading.value = false
  }
}

async function openSourceChunk(item: ListItem) {
  const chunkId = item.source_chunk_id ?? item.id
  if (chunkId == null) {
    ElMessage.warning('该结果没有可打开的原文块 ID')
    return
  }
  previewLoading.value = true
  selectedKey.value = itemKey('source', chunkId)
  previewKind.value = 'source'
  try {
    // Prefer live API; fall back to retrieve payload content/snippet
    try {
      const chunk = await getSourceChunk(chunkId)
      previewTitle.value = chunk.title || item.title || `原文块 #${chunkId}`
      previewDocumentId.value = chunk.document_id
      previewMeta.value = `文档 #${chunk.document_id} · 块 #${chunk.chunk_index} · ${chunk.start_char}–${chunk.end_char}`
      previewContent.value = formatSourceMarkdown({
        title: chunk.title || item.title,
        text: chunk.text,
        documentId: chunk.document_id,
        chunkIndex: chunk.chunk_index,
        startChar: chunk.start_char,
        endChar: chunk.end_char,
        path: item.path,
      })
    } catch {
      const text = item.content || item.snippet || ''
      if (!text) throw new Error('原文块加载失败')
      previewTitle.value = item.title || `原文块 #${chunkId}`
      previewDocumentId.value = item.source_document_id ?? null
      previewMeta.value = '原文摘录（来自检索结果）'
      previewContent.value = formatSourceMarkdown({
        title: item.title,
        text,
        startChar: item.start_char,
        endChar: item.end_char,
        path: item.path,
      })
    }
  } catch (e) {
    ElMessage.error(`加载原文失败：${(e as Error).message}`)
  } finally {
    previewLoading.value = false
  }
}

async function search() {
  const q = query.value.trim()
  if (!q) {
    hits.value = null
    return
  }
  searching.value = true
  try {
    const res = await retrieveWiki(q, 20)
    hits.value = res.hits
    retrievalMode.value = res.retrieval_mode || ''
    if (!res.hits.length) {
      ElMessage.info('未检索到相关页面')
    } else {
      // Auto-open first hit so user sees content immediately
      const first = displayList.value[0]
      if (first) await openItem(first)
    }
  } catch (e) {
    ElMessage.error(`检索失败：${(e as Error).message}`)
  } finally {
    searching.value = false
  }
}

function clearSearch() {
  query.value = ''
  hits.value = null
  retrievalMode.value = ''
}

function clearFilters() {
  domainFilter.value = ''
  typeFilter.value = ''
  statusFilter.value = ''
  sourceFilter.value = ''
  tagFilter.value = ''
}

/** Normalize wiki path for matching index.md links to DB pages. */
function normalizeWikiPath(raw: string) {
  let s = raw.trim()
  try {
    s = decodeURIComponent(s)
  } catch {
    // keep raw if malformed percent-encoding
  }
  s = s.replace(/\\/g, '/').replace(/^\.\//, '')
  // Strip origin if a full URL somehow appears
  try {
    if (/^https?:\/\//i.test(s)) {
      s = new URL(s).pathname
    }
  } catch {
    // ignore
  }
  s = s.replace(/^\/+/, '')
  // Drop query/hash
  s = s.split('?')[0].split('#')[0]
  return s
}

function basenamesEqual(a: string, b: string) {
  const ba = a.split('/').pop() || a
  const bb = b.split('/').pop() || b
  return ba === bb
}

/**
 * Index / markdown links: ``/wiki?page={id}`` or legacy ``pages/foo.md``.
 * Always open in the right pane — never full-page navigate off the SPA.
 */
async function onIndexLinkClick(href: string) {
  const raw = (href || '').trim()
  if (!raw) return

  // /wiki?page=123  or  wiki?page=123  or bare ?page=123
  let pageFromQuery: number | null = null
  try {
    const asUrl = raw.includes('://')
      ? new URL(raw)
      : new URL(raw, 'http://local.invalid/')
    const q = asUrl.searchParams.get('page')
    if (q && /^\d+$/.test(q)) pageFromQuery = Number(q)
  } catch {
    const m = raw.match(/[?&]page=(\d+)/)
    if (m) pageFromQuery = Number(m[1])
  }
  if (pageFromQuery != null && pageFromQuery > 0) {
    await openWikiPage(pageFromQuery)
    return
  }

  const norm = normalizeWikiPath(raw)
  if (!norm || norm === 'wiki') return

  if (!pages.value.length) {
    await loadPages()
  }

  const match =
    pages.value.find((p) => normalizeWikiPath(p.path || '') === norm) ||
    pages.value.find((p) => basenamesEqual(normalizeWikiPath(p.path || ''), norm)) ||
    pages.value.find((p) => {
      const pp = normalizeWikiPath(p.path || '')
      return pp.endsWith(norm) || norm.endsWith(pp)
    })

  if (match?.id != null) {
    await openWikiPage(match.id, match.title)
    return
  }

  ElMessage.warning(`未找到对应 Wiki 页面：${norm}`)
}

async function openFromRouteQuery() {
  const raw = route.query.page
  const pageId = Number(Array.isArray(raw) ? raw[0] : raw)
  if (!Number.isFinite(pageId) || pageId <= 0) return false
  await openWikiPage(pageId)
  return true
}

onMounted(async () => {
  await loadPages()
  const opened = await openFromRouteQuery()
  if (!opened) {
    await loadIndex()
  }
})

watch(
  () => route.query.page,
  async () => {
    const opened = await openFromRouteQuery()
    if (!opened && route.path === '/wiki') {
      // keep current preview if query cleared
    }
  },
)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1 class="page-title">Wiki 浏览</h1>
        <p class="page-subtitle">检索知识页与原文块，确认生成可用的业务上下文</p>
      </div>
      <div class="page-actions">
        <el-input
          v-model="query"
          clearable
          placeholder="输入关键词检索 Wiki / 原文"
          style="width: 280px"
          @keyup.enter="search"
        />
        <el-button type="primary" :loading="searching" @click="search">检索</el-button>
        <el-button v-if="hits" @click="clearSearch">显示全部</el-button>
        <el-button @click="loadIndex">查看 Index</el-button>
        <el-button type="warning" plain @click="router.push('/wiki/reviews')">审核中心</el-button>
        <el-button @click="loadPages">刷新列表</el-button>
      </div>
    </div>

    <div class="filter-bar">
      <el-select v-model="domainFilter" clearable placeholder="领域" style="width: 150px">
        <el-option v-for="value in filterOptions.domains" :key="value" :label="value" :value="value" />
      </el-select>
      <el-select v-model="typeFilter" clearable placeholder="页面类型" style="width: 140px">
        <el-option v-for="value in filterOptions.types" :key="value" :label="value" :value="value" />
      </el-select>
      <el-select v-model="statusFilter" clearable placeholder="状态" style="width: 130px">
        <el-option v-for="value in filterOptions.statuses" :key="value" :label="value" :value="value" />
      </el-select>
      <el-select v-model="sourceFilter" clearable placeholder="来源文档" style="width: 140px">
        <el-option v-for="value in filterOptions.sources" :key="value" :label="`文档 #${value}`" :value="String(value)" />
      </el-select>
      <el-select v-model="tagFilter" clearable filterable placeholder="标签" style="width: 150px">
        <el-option v-for="value in filterOptions.tags" :key="value" :label="value" :value="value" />
      </el-select>
      <el-button @click="clearFilters">清除筛选</el-button>
      <el-tag v-if="retrievalMode" effect="plain" type="info">
        {{ retrievalMode === 'fts5_hybrid' ? 'FTS5 混合检索' : '兼容检索' }}
      </el-tag>
    </div>

    <div class="layout">
      <div class="list-panel panel" v-loading="loading">
        <div class="list-title">
          {{ hits ? `检索结果（${displayList.length}）` : `页面列表（${displayList.length}）` }}
        </div>
        <el-scrollbar height="560px">
          <section v-for="group in groupedItems" :key="group.label" class="wiki-group">
            <div class="group-title">{{ group.label }}（{{ group.items.length }}）</div>
            <div
              v-for="item in group.items"
              :key="item.key"
              class="page-item"
              :class="{ active: selectedKey === item.key }"
              @click="openItem(item)"
            >
              <div class="item-title">{{ item.title || item.path }}</div>
              <div class="item-meta">
                <el-tag
                  size="small"
                  :type="item.citation_type === 'source' ? 'success' : 'info'"
                  effect="plain"
                >
                  {{ item.citation_type === 'source' ? '原文' : item.page_type || 'Wiki' }}
                </el-tag>
                <el-tag v-if="item.status" size="small" effect="plain">{{ item.status }}</el-tag>
                <el-tag v-if="item.revision" size="small" effect="plain">r{{ item.revision }}</el-tag>
                <el-tag
                  v-if="item.anchor_clause"
                  size="small"
                  type="warning"
                  effect="plain"
                >
                  锚定 {{ item.anchor_clause }}
                </el-tag>
                <el-tag
                  v-for="cid in (item.clause_ids || []).slice(0, 4)"
                  :key="cid"
                  size="small"
                  effect="plain"
                >
                  {{ cid }}
                </el-tag>
                <span v-if="item.score != null" class="score">score {{ item.score.toFixed(2) }}</span>
              </div>
              <div class="item-path">{{ item.page_key || item.path }}</div>
              <div v-if="expansionLabel(item.explain)" class="relation-hint">
                {{ expansionLabel(item.explain) }}
              </div>
              <div
                v-if="item.snippet"
                class="item-snippet"
                v-html="highlightedSnippet(item.snippet)"
              />
              <div v-if="item.tags?.length" class="item-tags">
                <el-tag
                  v-for="tag in item.tags"
                  :key="tag"
                  size="small"
                  effect="plain"
                  style="margin-right: 4px"
                >
                  {{ tag }}
                </el-tag>
              </div>
            </div>
          </section>
          <el-empty v-if="!displayList.length" description="暂无 Wiki 页面" :image-size="80" />
        </el-scrollbar>
      </div>

      <div class="preview-panel panel panel-surface" v-loading="previewLoading">
        <div class="preview-header">
          <div class="preview-title">{{ previewTitle }}</div>
          <el-tag
            v-if="previewKind === 'source'"
            size="small"
            type="success"
            effect="light"
          >
            原文
          </el-tag>
          <el-tag
            v-else-if="previewKind === 'wiki'"
            size="small"
            type="primary"
            effect="light"
          >
            Wiki
          </el-tag>
          <el-button
            v-if="previewKind === 'source' && previewDocumentId != null"
            type="primary"
            link
            @click="router.push({ path: '/documents', query: { document: previewDocumentId } })"
          >
            打开完整原文
          </el-button>
        </div>
        <div v-if="previewMeta" class="preview-meta">{{ previewMeta }}</div>
        <el-scrollbar height="540px">
          <MarkdownView :content="previewContent" @link-click="onIndexLinkClick" />
        </el-scrollbar>
      </div>
    </div>
  </div>
</template>

<style scoped>
.filter-bar {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin: -4px 0 18px;
  padding: 12px;
  border-radius: var(--cg-radius);
  background: var(--cg-surface-muted);
  border: 1px solid var(--cg-border);
}

.layout {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) minmax(360px, 1.5fr);
  gap: 16px;
}

.wiki-group + .wiki-group {
  margin-top: 14px;
}

.group-title {
  margin: 2px 2px 8px;
  color: var(--cg-text-muted);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.list-title,
.preview-title {
  font-weight: 700;
  color: var(--cg-text);
}

.list-title {
  margin-bottom: 12px;
}

.preview-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.preview-title {
  flex: 1;
  min-width: 0;
}

.preview-meta {
  font-size: 12px;
  color: var(--cg-text-muted);
  margin-bottom: 10px;
}

.page-item {
  position: relative;
  padding: 12px 12px 12px 14px;
  border-radius: var(--cg-radius-sm);
  background: #fff;
  border: 1px solid var(--cg-border);
  margin-bottom: 8px;
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s, transform 0.15s;
}

.page-item:hover {
  border-color: rgba(79, 124, 255, 0.35);
  box-shadow: var(--cg-shadow);
}

.page-item.active {
  border-color: var(--cg-border-strong);
  box-shadow: 0 0 0 1px rgba(79, 124, 255, 0.12), var(--cg-shadow);
  background: linear-gradient(90deg, rgba(79, 124, 255, 0.06), #fff 40%);
}

.page-item.active::before {
  content: "";
  position: absolute;
  left: 0;
  top: 10px;
  bottom: 10px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--cg-gradient-brand);
}

.item-title {
  font-weight: 600;
  margin-bottom: 4px;
  color: var(--cg-text);
}

.item-meta {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 4px;
}

.score {
  font-size: 12px;
  color: var(--cg-text-muted);
}

.item-path,
.item-snippet {
  font-size: 12px;
  color: var(--cg-text-muted);
  margin-top: 2px;
}

.item-snippet :deep(mark) {
  padding: 0 2px;
  border-radius: 3px;
  background: #fff0a8;
  color: #7a4d00;
}

.relation-hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--cg-primary);
}

.item-snippet {
  color: var(--cg-text-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.item-tags {
  margin-top: 6px;
}

@media (max-width: 960px) {
  .layout {
    grid-template-columns: 1fr;
  }
}
</style>
