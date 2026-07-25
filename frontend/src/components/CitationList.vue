<script setup lang="ts">
export interface CitationItem {
  id?: number | null
  title: string
  path: string
  score?: number
  snippet?: string
  wiki_page_id?: number | null
}

withDefaults(
  defineProps<{
    citations?: CitationItem[]
    count?: number
  }>(),
  {
    citations: () => [],
    count: 0,
  },
)
</script>

<template>
  <div class="citation-list">
    <div class="header">
      <span class="title">Wiki 引用</span>
      <el-tag size="small" type="info">{{ count || citations.length }} 条</el-tag>
    </div>

    <el-empty
      v-if="!(count || citations.length)"
      description="暂无引用"
      :image-size="56"
    />

    <div v-else-if="citations.length" class="items">
      <div v-for="(c, idx) in citations" :key="c.id ?? `${c.path}-${idx}`" class="item">
        <div class="item-title">
          <span>{{ c.title || c.path || `引用 #${idx + 1}` }}</span>
          <span v-if="c.score != null" class="score">{{ c.score.toFixed(2) }}</span>
        </div>
        <div v-if="c.path" class="item-path">{{ c.path }}</div>
        <div v-if="c.snippet" class="item-snippet">{{ c.snippet }}</div>
      </div>
    </div>

    <div v-else class="summary">
      任务已关联 {{ count }} 条 Wiki 引用（详情见生成流水中的检索结果）。
    </div>
  </div>
</template>

<style scoped>
.citation-list {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px;
  background: #fafafa;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.title {
  font-weight: 600;
}

.items {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.item {
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 8px 10px;
}

.item-title {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-weight: 600;
  font-size: 13px;
}

.score {
  color: #909399;
  font-weight: 400;
  font-size: 12px;
}

.item-path,
.item-snippet {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
}

.item-snippet {
  color: #606266;
}

.summary {
  font-size: 13px;
  color: #606266;
  line-height: 1.5;
}
</style>
