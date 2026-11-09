<script setup lang="ts">
import { computed } from 'vue'
import { parse as parseJson5 } from 'json5'
import MarkdownView from './MarkdownView.vue'

const props = defineProps<{ content: string; mediaType: string }>()

interface TreeNode { id: string; label: string; children?: TreeNode[] }
interface TreeState { nodes: number; nextId: number; truncated: boolean }

const mime = computed(() => props.mediaType.split(';', 1)[0].trim().toLowerCase())
const isJson = computed(() => mime.value === 'application/json' || mime.value === 'text/json' || mime.value.endsWith('+json'))
const isCsv = computed(() => mime.value === 'text/csv' || mime.value === 'application/csv')
const isMarkdown = computed(() => mime.value === 'text/markdown' || mime.value === 'text/x-markdown')

function nextTreeId(state: TreeState) {
  const id = `node-${state.nextId}`
  state.nextId += 1
  return id
}

function treeValue(value: unknown, depth: number, state: TreeState): TreeNode[] {
  if (depth > 20 || state.nodes >= 2000) {
    state.truncated = true
    return [{ id: nextTreeId(state), label: '<truncated>' }]
  }
  if (Array.isArray(value)) {
    const output: TreeNode[] = []
    for (let index = 0; index < value.length; index += 1) {
      if (state.nodes >= 2000) { state.truncated = true; output.push({ id: nextTreeId(state), label: '<truncated>' }); break }
      state.nodes += 1
      output.push({ id: nextTreeId(state), label: `[${index}]`, children: treeValue(value[index], depth + 1, state) })
    }
    return output
  }
  if (value !== null && typeof value === 'object') {
    const output: TreeNode[] = []
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      if (state.nodes >= 2000) { state.truncated = true; output.push({ id: nextTreeId(state), label: '<truncated>' }); break }
      state.nodes += 1
      output.push({ id: nextTreeId(state), label: key, children: treeValue(item, depth + 1, state) })
    }
    return output
  }
  return [{ id: nextTreeId(state), label: String(value) }]
}

const jsonPreview = computed(() => {
  let value: unknown
  let relaxed = false
  try {
    value = JSON.parse(props.content)
  } catch {
    try {
      value = parseJson5(props.content)
      relaxed = true
    } catch {
      return { data: [] as TreeNode[], truncated: false, relaxed: false, error: 'JSON 解析失败，以下显示原始内容。' }
    }
  }
  const state: TreeState = { nodes: 1, nextId: 1, truncated: false }
  const data = [{ id: 'node-0', label: 'JSON', children: treeValue(value, 0, state) }]
  return { data, truncated: state.truncated, relaxed, error: '' }
})

function parseCsv(content: string) {
  const text = content.replace(/^\uFEFF/, '')
  const rows: string[][] = []
  let row: string[] = [], field = '', inQuotes = false, closedQuote = false, truncated = false
  const pushField = () => { if (row.length < 100) row.push(field); else truncated = true; field = '' }
  const pushRow = () => { pushField(); if (rows.length < 201) rows.push(row); else truncated = true; row = [] }

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index]
    if (inQuotes) {
      if (char === '"' && text[index + 1] === '"') { field += '"'; index += 1 }
      else if (char === '"') { inQuotes = false; closedQuote = true }
      else field += char
      continue
    }
    if (closedQuote && char !== ',' && char !== '\r' && char !== '\n') return { rows: [], truncated: false, error: 'CSV 解析失败：结束引号后存在无效字符。' }
    if (char === '"') {
      if (field) return { rows: [], truncated: false, error: 'CSV 解析失败：未加引号的字段中包含双引号。' }
      inQuotes = true
    } else if (char === ',') {
      pushField(); closedQuote = false
    } else if (char === '\r' || char === '\n') {
      pushRow(); closedQuote = false
      if (char === '\r' && text[index + 1] === '\n') index += 1
    } else field += char
  }
  if (inQuotes) return { rows: [], truncated: false, error: 'CSV 解析失败：存在未闭合的双引号。' }
  if (text && !/[\r\n]$/.test(text)) pushRow()
  return { rows, truncated, error: '' }
}

const csvPreview = computed(() => {
  const parsed = parseCsv(props.content)
  const headers = (parsed.rows[0] || []).map((header, index) => header || `第 ${index + 1} 列`)
  return { ...parsed, headers, data: parsed.rows.slice(1, 201) }
})
</script>

<template>
  <div class="example-preview">
    <template v-if="isJson">
      <el-alert v-if="jsonPreview.error" :title="jsonPreview.error" type="warning" :closable="false"/>
      <el-alert v-if="!jsonPreview.error&&jsonPreview.relaxed" title="按宽松 JSON 语法预览，保存仍保留原文" type="info" :closable="false"/>
      <el-alert v-if="!jsonPreview.error&&jsonPreview.truncated" title="JSON 结构超过 20 层或 2000 个节点，预览已截断；原始内容不受影响。" type="warning" :closable="false"/>
      <pre v-if="jsonPreview.error">{{content}}</pre>
      <el-tree v-else :data="jsonPreview.data" node-key="id" :default-expanded-keys="['node-0']"/>
    </template>
    <template v-else-if="isCsv">
      <el-alert v-if="csvPreview.error" :title="csvPreview.error" type="warning" :closable="false"/>
      <el-alert v-else-if="csvPreview.truncated" title="CSV 预览最多显示 200 个数据行 × 100 列，超出部分已截断；原始内容不受影响。" type="warning" :closable="false"/>
      <pre v-if="csvPreview.error">{{content}}</pre>
      <div v-else class="table-scroll"><el-table :data="csvPreview.data" border><el-table-column v-for="(header,index) in csvPreview.headers" :key="index" :label="header" min-width="140"><template #default="scope">{{scope.row[index]}}</template></el-table-column></el-table></div>
    </template>
    <MarkdownView v-else-if="isMarkdown" :content="content"/>
    <pre v-else>{{content}}</pre>
  </div>
</template>

<style scoped>
.example-preview{max-width:100%;padding:12px;overflow:auto;background:var(--cg-surface-muted);border-radius:8px}.example-preview .el-alert{margin-bottom:12px}.example-preview pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.table-scroll{max-width:100%;overflow-x:auto}
</style>
