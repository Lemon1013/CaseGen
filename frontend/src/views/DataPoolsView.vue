<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listWikiSpaces, type WikiSpace } from '../api/wikiSpaces'
import { archiveDataPool, createDataPool, deleteDataPool, listDataPools, type DataPool } from '../api/platformData'

const spaces = ref<WikiSpace[]>([])
const spaceId = ref<number>()
const pools = ref<DataPool[]>([])
const loading = ref(false)
const dialog = ref(false)
const form = reactive({ name: '', description: '', source_kind: 'csv' as 'csv' | 'json' | 'description', content: '', parse_with_llm: false })
const sample = computed(() => form.source_kind === 'csv' ? 'scenario,account,product,price,quantity\nORDER_VALID_001,A001,BTC-USDT,上下五档内价格,2' : form.source_kind === 'json' ? '[{"scenario":"ORDER_VALID_001","price":"上下五档内价格"}]' : '准备一组有效账户和产品，价格使用产品上下五档内价格。')

async function load() {
  if (!spaceId.value) return
  loading.value = true
  try { pools.value = await listDataPools(spaceId.value, true) } catch (e) { ElMessage.error((e as Error).message) } finally { loading.value = false }
}

async function init() {
  spaces.value = (await listWikiSpaces()).filter((item) => item.status === 'active')
  spaceId.value = spaces.value[0]?.id
  await load()
}

function openCreate() {
  Object.assign(form, { name: '', description: '', source_kind: 'csv', content: '', parse_with_llm: false })
  dialog.value = true
}

async function save() {
  if (!spaceId.value || !form.name.trim() || !form.content.trim()) return ElMessage.warning('请填写名称和数据内容')
  try {
    await createDataPool({ wiki_space_id: spaceId.value, ...form })
    dialog.value = false
    ElMessage.success('数据池已导入')
    await load()
  } catch (e) { ElMessage.error(`导入失败：${(e as Error).message}`) }
}

async function archive(row: DataPool) {
  if (!spaceId.value) return
  try {
    await ElMessageBox.confirm(`归档数据池「${row.name}」？`, '确认归档')
    await archiveDataPool(row.id, spaceId.value)
    await load()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error((e as Error).message)
  }
}

async function remove(row: DataPool) {
  if (!spaceId.value) return
  try {
    await ElMessageBox.confirm(`永久删除数据池「${row.name}」及其所有版本？此操作不可恢复。`, '确认永久删除', { type: 'warning' })
    await deleteDataPool(row.id, spaceId.value)
    await load()
    ElMessage.success('数据池已永久删除')
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(`删除失败：${(e as Error).message}`)
  }
}

onMounted(init)
</script>

<template>
  <div class="page">
    <div class="page-header"><div><h1 class="page-title">数据池</h1><p class="page-subtitle">按知识空间管理通用 JSON、CSV 与描述型测试数据，抽象字符串会原样保留。</p></div><div class="actions"><el-select v-model="spaceId" style="width:200px" @change="load"><el-option v-for="s in spaces" :key="s.id" :label="s.name" :value="s.id" /></el-select><el-button type="primary" @click="openCreate">导入数据池</el-button></div></div>
    <el-row :gutter="16" v-loading="loading">
      <el-col v-for="pool in pools" :key="pool.id" :span="12">
        <el-card shadow="never" class="pool-card">
          <template #header><div class="card-title"><strong>{{ pool.name }}</strong><el-tag :type="pool.status === 'active' ? 'success' : 'info'">{{ pool.status }}</el-tag></div></template>
          <p>{{ pool.description || '暂无说明' }}</p>
          <div v-if="pool.latest_revision" class="meta">版本 {{ pool.latest_revision.revision }} · {{ pool.latest_revision.source_kind }} · {{ pool.latest_revision.record_count }} 条</div>
          <el-collapse v-if="pool.latest_revision"><el-collapse-item title="Schema 与记录预览"><pre>{{ JSON.stringify(pool.latest_revision.schema, null, 2) }}</pre><pre>{{ JSON.stringify(pool.latest_revision.records.slice(0, 5), null, 2) }}</pre></el-collapse-item></el-collapse>
          <el-button v-if="pool.status === 'active'" link type="danger" @click="archive(pool)">归档</el-button>
          <el-button v-else-if="pool.status === 'archived'" link type="danger" @click="remove(pool)">删除</el-button>
        </el-card>
      </el-col>
    </el-row>
    <el-empty v-if="!loading && !pools.length" description="当前空间还没有数据池" />
    <el-dialog v-model="dialog" title="导入数据池" width="680px">
      <el-form label-position="top"><el-form-item label="名称"><el-input v-model="form.name" /></el-form-item><el-form-item label="说明"><el-input v-model="form.description" /></el-form-item><el-form-item label="来源类型"><el-radio-group v-model="form.source_kind"><el-radio-button value="csv">CSV</el-radio-button><el-radio-button value="json">JSON</el-radio-button><el-radio-button value="description">描述</el-radio-button></el-radio-group></el-form-item><el-form-item v-if="form.source_kind === 'description'"><el-checkbox v-model="form.parse_with_llm">使用默认模型解析为结构化候选</el-checkbox></el-form-item><el-form-item label="内容"><el-input v-model="form.content" type="textarea" :rows="10" :placeholder="sample" /></el-form-item></el-form>
      <template #footer><el-button @click="dialog=false">取消</el-button><el-button type="primary" @click="save">导入</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>.actions,.card-title{display:flex;gap:12px;align-items:center;justify-content:space-between}.pool-card{margin-bottom:16px}.meta{color:var(--cg-text-muted);font-size:13px;margin:10px 0}pre{white-space:pre-wrap;max-height:260px;overflow:auto;background:var(--cg-bg);padding:10px;border-radius:6px}</style>
