<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import MarkdownView from '../components/MarkdownView.vue'
import {
  getWikiIndex,
  getWikiPage,
  listWikiPages,
  retrieveWiki,
  type RetrieveHit,
  type WikiPage,
} from '../api/wiki'

const loading = ref(false)
const pages = ref<WikiPage[]>([])
const query = ref('')
const searching = ref(false)
const hits = ref<RetrieveHit[] | null>(null)
const selectedId = ref<number | null>(null)
const previewTitle = ref('Wiki Index')
const previewContent = ref('')
const previewLoading = ref(false)

const displayList = computed(() => {
  if (hits.value) {
    return hits.value.map((h) => ({
      id: h.id,
      title: h.title,
      path: h.path,
      page_type: h.page_type,
      tags: h.tags,
      score: h.score,
      snippet: h.snippet,
    }))
  }
  return pages.value.map((p) => ({
    id: p.id,
    title: p.title,
    path: p.path,
    page_type: p.page_type,
    tags: p.tags,
    score: undefined as number | undefined,
    snippet: '',
  }))
})

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
    selectedId.value = null
    previewTitle.value = 'Wiki Index'
    previewContent.value = index.content || '# Wiki Index\n\n（空）'
  } catch (e) {
    ElMessage.error(`加载索引失败：${(e as Error).message}`)
  } finally {
    previewLoading.value = false
  }
}

async function openPage(id: number | null | undefined, fallbackTitle?: string) {
  if (id == null) {
    ElMessage.warning('该结果没有可打开的页面 ID')
    return
  }
  previewLoading.value = true
  selectedId.value = id
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
    if (!res.hits.length) {
      ElMessage.info('未检索到相关页面')
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
}

onMounted(async () => {
  await Promise.all([loadPages(), loadIndex()])
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1>Wiki 浏览</h1>
      <div class="header-actions">
        <el-input
          v-model="query"
          clearable
          placeholder="输入关键词检索 Wiki"
          style="width: 280px"
          @keyup.enter="search"
        />
        <el-button type="primary" :loading="searching" @click="search">检索</el-button>
        <el-button v-if="hits" @click="clearSearch">显示全部</el-button>
        <el-button @click="loadIndex">查看 Index</el-button>
        <el-button @click="loadPages">刷新列表</el-button>
      </div>
    </div>

    <div class="layout">
      <div class="list-panel" v-loading="loading">
        <div class="list-title">
          {{ hits ? `检索结果（${displayList.length}）` : `页面列表（${displayList.length}）` }}
        </div>
        <el-scrollbar height="560px">
          <div
            v-for="item in displayList"
            :key="`${item.id}-${item.path}`"
            class="page-item"
            :class="{ active: selectedId === item.id }"
            @click="openPage(item.id, item.title)"
          >
            <div class="item-title">{{ item.title || item.path }}</div>
            <div class="item-meta">
              <el-tag size="small" type="info">{{ item.page_type || 'page' }}</el-tag>
              <span v-if="item.score != null" class="score">score {{ item.score.toFixed(2) }}</span>
            </div>
            <div class="item-path">{{ item.path }}</div>
            <div v-if="item.snippet" class="item-snippet">{{ item.snippet }}</div>
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
          <el-empty v-if="!displayList.length" description="暂无 Wiki 页面" :image-size="80" />
        </el-scrollbar>
      </div>

      <div class="preview-panel" v-loading="previewLoading">
        <div class="preview-title">{{ previewTitle }}</div>
        <el-scrollbar height="560px">
          <MarkdownView :content="previewContent" />
        </el-scrollbar>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  gap: 12px;
  flex-wrap: wrap;
}

.page-header h1 {
  margin: 0;
}

.header-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}

.layout {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) minmax(360px, 1.5fr);
  gap: 16px;
}

.list-panel,
.preview-panel {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  background: #fafafa;
  padding: 12px;
}

.preview-panel {
  background: #fff;
}

.list-title,
.preview-title {
  font-weight: 600;
  margin-bottom: 10px;
}

.page-item {
  padding: 10px 12px;
  border-radius: 8px;
  background: #fff;
  border: 1px solid #ebeef5;
  margin-bottom: 8px;
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s;
}

.page-item:hover,
.page-item.active {
  border-color: #409eff;
  box-shadow: 0 0 0 1px rgba(64, 158, 255, 0.15);
}

.item-title {
  font-weight: 600;
  margin-bottom: 4px;
}

.item-meta {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 4px;
}

.score {
  font-size: 12px;
  color: #909399;
}

.item-path,
.item-snippet {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}

.item-snippet {
  color: #606266;
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
