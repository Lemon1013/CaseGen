<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import ExampleContentPreview from '../components/ExampleContentPreview.vue'
import { createExample, createPlatform, createVariant, deleteExample, deletePlatform, deleteVariant, listPlatforms, updateExample, updatePlatform, updateVariant, type ExampleVariant, type PlatformExample, type PlatformProfile } from '../api/platformData'
import { useProjectStore } from '../projectStore'

const projects = useProjectStore()
const spaceId = projects.defaultWikiSpaceId
const platforms = ref<PlatformProfile[]>([])
const platformId = ref<number>(), variantId = ref<number>()
const variantPlatformId = ref<number>()
const platformDialog = ref(false), variantDialog = ref(false), exampleDialog = ref(false)
const previewDialog = ref(false), previewExample = ref<PlatformExample>()
const exampleMode = ref<'edit'|'preview'>('edit')
const editingPlatform = ref<PlatformProfile>(), editingVariant = ref<ExampleVariant>(), editingExample = ref<PlatformExample>()
const platformForm = reactive({ name: '', description: '', artifact_topology: 'combined' })
const variantForm = reactive({ name: '', applicability: '' })
const exampleForm = reactive({ kind: 'combined', name: '', media_type: 'text/csv', content: '' })
const mediaTypeOptions = [
  { label: 'JSON', value: 'application/json' },
  { label: 'CSV', value: 'text/csv' },
  { label: 'Markdown', value: 'text/markdown' },
  { label: '纯文本', value: 'text/plain' },
  { label: 'Robot Framework', value: 'text/robotframework' },
]
const currentPlatform = computed(() => platforms.value.find((item) => item.id === platformId.value))
const exampleKindOptions = computed(() => {
  const options = [
    { value: 'case', label: 'case·用例' },
    { value: 'data', label: 'data·数据' },
    { value: 'relation', label: 'relation·可选：独立用例—数据关联表' },
    { value: 'combined', label: 'combined·用例数据一体' },
    { value: 'attachment', label: 'attachment·可选：辅助文件' },
  ]
  if (currentPlatform.value?.artifact_topology === 'separated') return options.filter((item) => item.value !== 'combined')
  if (currentPlatform.value?.artifact_topology === 'combined') return options.filter((item) => ['combined', 'attachment'].includes(item.value))
  return options
})
const topologyKindHint = computed(() => ({
  separated: '分离式平台使用 case + data；relation、attachment 可选。',
  combined: '一体式平台使用 combined；attachment 可选。',
  hybrid: '混合式平台可使用 combined，或 case + data；relation、attachment 可选。',
})[currentPlatform.value?.artifact_topology || 'hybrid'])

async function load() {
  if (!spaceId.value) { platforms.value = []; return }
  platforms.value = await listPlatforms(spaceId.value)
  if (!platforms.value.some((item) => item.id === platformId.value)) platformId.value = platforms.value[0]?.id
  if (!currentPlatform.value?.variants.some((item) => item.id === variantId.value)) variantId.value = undefined
}
async function init() {
  try {
    await load()
  } catch (error) {
    ElMessage.error(`加载平台管理数据失败：${(error as Error).message}`)
  }
}
function selectPlatform() { variantId.value = undefined }
function openPlatform(row?: PlatformProfile) { editingPlatform.value = row; Object.assign(platformForm, row ? { name: row.name, description: row.description, artifact_topology: row.artifact_topology } : { name: '', description: '', artifact_topology: 'combined' }); platformDialog.value = true }
async function savePlatform() { if (!spaceId.value || !platformForm.name.trim()) return; try { const saved=editingPlatform.value?await updatePlatform(editingPlatform.value.id,spaceId.value,platformForm):await createPlatform({wiki_space_id:spaceId.value,...platformForm});platformId.value=saved.id;platformDialog.value=false;await load();ElMessage.success('平台已保存') } catch(e){ ElMessage.error((e as Error).message) } }
async function removePlatform(row: PlatformProfile) { if(!spaceId.value)return; try{await ElMessageBox.confirm(`删除平台「${row.name}」及其示例类型？`,'确认删除',{type:'warning'});await deletePlatform(row.id,spaceId.value);await load();ElMessage.success('平台已删除')}catch(e){if(e!=='cancel')ElMessage.error((e as Error).message)} }
function openVariant(row?: ExampleVariant) { editingVariant.value=row;variantPlatformId.value=row?.platform_id??platformId.value;Object.assign(variantForm,row?{name:row.name,applicability:row.applicability}:{name:'',applicability:''});variantDialog.value=true }
async function saveVariant(){if(!spaceId.value||!variantPlatformId.value||!variantForm.name.trim())return;try{if(editingVariant.value)await updateVariant(variantPlatformId.value,editingVariant.value.id,spaceId.value,variantForm);else await createVariant(variantPlatformId.value,spaceId.value,variantForm);platformId.value=variantPlatformId.value;variantId.value=undefined;variantDialog.value=false;await load();ElMessage.success('示例类型已保存')}catch(e){ElMessage.error((e as Error).message)}}
async function removeVariant(row:ExampleVariant){if(!spaceId.value||!platformId.value)return;try{await ElMessageBox.confirm(`删除示例类型「${row.name}」及其示例？`,'确认删除',{type:'warning'});await deleteVariant(platformId.value,row.id,spaceId.value);await load()}catch(e){if(e!=='cancel')ElMessage.error((e as Error).message)}}
function openExample(row?:PlatformExample,targetVariantId?:number){editingExample.value=row;variantId.value=row?.variant_id??targetVariantId;exampleMode.value='edit';Object.assign(exampleForm,row?{kind:row.kind,name:row.name,media_type:row.media_type,content:row.content}:{kind:currentPlatform.value?.artifact_topology==='separated'?'case':'combined',name:'',media_type:'text/csv',content:''});exampleDialog.value=true}
function openPreview(row:PlatformExample){previewExample.value=row;previewDialog.value=true}
async function saveExample(){if(!spaceId.value||!platformId.value||!variantId.value||!exampleForm.content.trim())return;try{if(editingExample.value)await updateExample(platformId.value,variantId.value,editingExample.value.id,spaceId.value,exampleForm);else await createExample(platformId.value,variantId.value,spaceId.value,exampleForm);exampleDialog.value=false;await load();ElMessage.success('示例已保存')}catch(e){ElMessage.error((e as Error).message)}}
async function removeExample(row:PlatformExample){if(!spaceId.value||!platformId.value)return;try{await ElMessageBox.confirm(`删除示例「${row.name||row.kind}」？`,'确认删除',{type:'warning'});await deleteExample(platformId.value,row.variant_id,row.id,spaceId.value);await load()}catch(e){if(e!=='cancel')ElMessage.error((e as Error).message)}}
onMounted(init)
</script>

<template><div class="page">
  <div class="page-header"><div><h1 class="page-title">平台管理</h1><p class="page-subtitle">先维护平台，再为平台维护可独立选择的示例类型和格式样例。</p></div></div>
  <el-empty v-if="!spaceId" description="当前空间默认知识库配置异常，请到知识库管理检查"/>
  <template v-else><el-card shadow="never"><template #header><div class="head"><strong>1. 平台</strong><el-button type="primary" @click="openPlatform()">新建平台</el-button></div></template><el-table :data="platforms"><el-table-column prop="name" label="平台"/><el-table-column prop="description" label="说明"/><el-table-column prop="artifact_topology" label="产物拓扑" width="140"/><el-table-column label="操作" width="150"><template #default="{row}"><el-button link @click.stop="openPlatform(row)">编辑</el-button><el-button link type="danger" @click.stop="removePlatform(row)">删除</el-button></template></el-table-column></el-table></el-card>
  <el-card shadow="never" class="section"><template #header><div class="head"><strong>2. 示例类型 <span v-if="currentPlatform">· {{currentPlatform.name}}</span></strong><span>当前平台：<el-select v-model="platformId" :disabled="!platforms.length" placeholder="暂无平台" style="width:260px" @change="selectPlatform"><el-option v-for="p in platforms" :key="p.id" :label="p.name" :value="p.id"/></el-select></span><el-button type="primary" :disabled="!platformId" @click="openVariant()">新建示例类型</el-button></div></template><el-empty v-if="!platformId" description="请先选择平台"/><template v-else><el-table :data="currentPlatform?.variants||[]"><el-table-column prop="name" label="示例类型"/><el-table-column prop="applicability" label="适用说明"/><el-table-column label="示例数" width="90"><template #default="{row}">{{row.examples.length}}</template></el-table-column><el-table-column label="操作" width="150"><template #default="{row}"><el-button link @click.stop="openVariant(row)">编辑</el-button><el-button link type="danger" @click.stop="removeVariant(row)">删除</el-button></template></el-table-column></el-table><el-divider content-position="left">示例内容 · {{currentPlatform?.name}}</el-divider><el-empty v-if="!currentPlatform?.variants.length" description="当前平台暂无示例类型"/><el-collapse v-else><el-collapse-item v-for="variant in currentPlatform?.variants||[]" :key="variant.id" :name="variant.id"><template #title><strong>{{variant.name}} · {{variant.examples.length}} 个示例</strong></template><div class="head"><span class="hint">kind、媒体类型和内容都会作为该示例类型的参考格式。编辑仅影响后续生成，历史产物不会改变；历史已引用的示例不可删除。</span><el-button @click.stop="openExample(undefined,variant.id)">新增示例</el-button></div><el-table :data="variant.examples"><el-table-column prop="name" label="名称"/><el-table-column prop="kind" label="Kind" width="110"/><el-table-column prop="media_type" label="媒体类型" width="180"/><el-table-column label="操作" width="230"><template #default="{row}"><el-button link @click="openExample(row)">编辑</el-button><el-button link @click="openPreview(row)">查看内容</el-button><el-button link type="danger" @click="removeExample(row)">删除</el-button></template></el-table-column></el-table></el-collapse-item></el-collapse></template></el-card></template>
  <el-dialog v-model="platformDialog" :title="editingPlatform?'编辑平台':'新建平台'"><el-form label-position="top"><el-form-item label="名称"><el-input v-model="platformForm.name"/></el-form-item><el-form-item label="说明"><el-input v-model="platformForm.description" type="textarea"/></el-form-item><el-form-item label="产物拓扑"><el-select v-model="platformForm.artifact_topology"><el-option label="用例/数据分离" value="separated"/><el-option label="用例数据一体" value="combined"/><el-option label="混合" value="hybrid"/></el-select></el-form-item></el-form><template #footer><el-button @click="platformDialog=false">取消</el-button><el-button type="primary" @click="savePlatform">保存</el-button></template></el-dialog>
  <el-dialog v-model="variantDialog" :title="editingVariant?'编辑示例类型':'新建示例类型'"><el-form label-position="top"><el-form-item label="所属平台"><el-select v-model="variantPlatformId" :disabled="!!editingVariant" style="width:100%"><el-option v-for="p in platforms" :key="p.id" :label="p.name" :value="p.id"/></el-select></el-form-item><el-form-item label="名称"><el-input v-model="variantForm.name"/></el-form-item><el-form-item label="适用说明"><el-input v-model="variantForm.applicability" type="textarea"/></el-form-item></el-form><template #footer><el-button @click="variantDialog=false">取消</el-button><el-button type="primary" @click="saveVariant">保存</el-button></template></el-dialog>
  <el-dialog v-model="exampleDialog" :title="editingExample?'编辑示例':'新增示例'" width="720px"><el-radio-group v-model="exampleMode" class="dialog-mode"><el-radio-button value="edit">编辑</el-radio-button><el-radio-button value="preview">预览</el-radio-button></el-radio-group><el-form v-if="exampleMode==='edit'" label-position="top"><el-form-item label="示例名称"><el-input v-model="exampleForm.name" placeholder="例如：基金申购用例 CSV"/></el-form-item><el-form-item label="所属示例类型"><el-select v-model="variantId" :disabled="!!editingExample" style="width:100%"><el-option v-for="v in currentPlatform?.variants||[]" :key="v.id" :label="v.name" :value="v.id"/></el-select></el-form-item><el-row :gutter="12"><el-col :span="12"><el-form-item><template #label><span>Kind <el-tooltip :trigger="['hover','focus']" :content="topologyKindHint"><button type="button" class="kind-help" aria-label="查看当前平台 Kind 规则">?</button></el-tooltip></span></template><el-select v-model="exampleForm.kind"><el-option v-for="item in exampleKindOptions" :key="item.value" :label="item.label" :value="item.value"/><el-option v-if="editingExample&&!exampleKindOptions.some((item)=>item.value===exampleForm.kind)" :label="`${exampleForm.kind}（历史值，与当前拓扑不匹配）`" :value="exampleForm.kind" disabled/></el-select></el-form-item></el-col><el-col :span="12"><el-form-item label="媒体类型"><el-select v-model="exampleForm.media_type" style="width:100%"><el-option v-for="item in mediaTypeOptions" :key="item.value" :label="item.label" :value="item.value"/><el-option v-if="editingExample&&!mediaTypeOptions.some((item)=>item.value===exampleForm.media_type)" :label="`${exampleForm.media_type}（历史值）`" :value="exampleForm.media_type" disabled/></el-select></el-form-item></el-col></el-row><el-form-item label="内容"><el-input v-model="exampleForm.content" type="textarea" :rows="14"/></el-form-item></el-form><ExampleContentPreview v-else :content="exampleForm.content" :media-type="exampleForm.media_type"/><template #footer><el-button @click="exampleDialog=false">取消</el-button><el-button type="primary" @click="saveExample">保存</el-button></template></el-dialog>
  <el-dialog v-model="previewDialog" :title="`查看内容 · ${previewExample?.name||previewExample?.kind||''}`" width="80%" style="max-width:900px" destroy-on-close @closed="previewExample=undefined"><ExampleContentPreview v-if="previewExample" :content="previewExample.content" :media-type="previewExample.media_type"/><template #footer><el-button @click="previewDialog=false">关闭</el-button></template></el-dialog>
</div></template>
<style scoped>.section{margin-top:18px}.head{display:flex;justify-content:space-between;align-items:center;gap:12px}.hint{color:var(--cg-text-muted);font-size:13px}.dialog-mode{margin-bottom:16px}.kind-help{width:18px;height:18px;padding:0;border:1px solid var(--cg-border);border-radius:50%;background:transparent;color:var(--cg-text-muted);font-size:12px;line-height:16px;cursor:help}</style>
