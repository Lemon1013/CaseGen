<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { createExample, createPlatform, createVariant, deleteExample, deletePlatform, deleteVariant, listPlatforms, updateExample, updatePlatform, updateVariant, type ExampleVariant, type PlatformExample, type PlatformProfile } from '../api/platformData'
import { useProjectStore } from '../projectStore'

const projects = useProjectStore()
const spaceId = projects.defaultWikiSpaceId
const platforms = ref<PlatformProfile[]>([])
const platformId = ref<number>(), variantId = ref<number>()
const platformDialog = ref(false), variantDialog = ref(false), exampleDialog = ref(false)
const editingPlatform = ref<PlatformProfile>(), editingVariant = ref<ExampleVariant>(), editingExample = ref<PlatformExample>()
const platformForm = reactive({ name: '', description: '', artifact_topology: 'combined' })
const variantForm = reactive({ name: '', applicability: '' })
const exampleForm = reactive({ kind: 'combined', name: '', media_type: 'application/json', content: '' })
const currentPlatform = computed(() => platforms.value.find((item) => item.id === platformId.value))
const currentVariant = computed(() => currentPlatform.value?.variants.find((item) => item.id === variantId.value))

async function load() {
  if (!spaceId.value) { platforms.value = []; return }
  platforms.value = await listPlatforms(spaceId.value)
  if (!platforms.value.some((item) => item.id === platformId.value)) platformId.value = platforms.value[0]?.id
  if (!currentPlatform.value?.variants.some((item) => item.id === variantId.value)) variantId.value = currentPlatform.value?.variants[0]?.id
}
async function init() {
  try {
    await load()
  } catch (error) {
    ElMessage.error(`加载平台管理数据失败：${(error as Error).message}`)
  }
}
function openPlatform(row?: PlatformProfile) { editingPlatform.value = row; Object.assign(platformForm, row ? { name: row.name, description: row.description, artifact_topology: row.artifact_topology } : { name: '', description: '', artifact_topology: 'combined' }); platformDialog.value = true }
async function savePlatform() { if (!spaceId.value || !platformForm.name.trim()) return; try { if (editingPlatform.value) await updatePlatform(editingPlatform.value.id, spaceId.value, platformForm); else await createPlatform({ wiki_space_id: spaceId.value, ...platformForm }); platformDialog.value=false; await load(); ElMessage.success('平台已保存') } catch(e){ ElMessage.error((e as Error).message) } }
async function removePlatform(row: PlatformProfile) { if(!spaceId.value)return; try{await ElMessageBox.confirm(`删除平台「${row.name}」及其示例类型？`,'确认删除',{type:'warning'});await deletePlatform(row.id,spaceId.value);await load();ElMessage.success('平台已删除')}catch(e){if(e!=='cancel')ElMessage.error((e as Error).message)} }
function openVariant(row?: ExampleVariant) { editingVariant.value=row;Object.assign(variantForm,row?{name:row.name,applicability:row.applicability}:{name:'',applicability:''});variantDialog.value=true }
async function saveVariant(){if(!spaceId.value||!platformId.value||!variantForm.name.trim())return;try{if(editingVariant.value)await updateVariant(platformId.value,editingVariant.value.id,spaceId.value,variantForm);else await createVariant(platformId.value,spaceId.value,variantForm);variantDialog.value=false;await load();ElMessage.success('示例类型已保存')}catch(e){ElMessage.error((e as Error).message)}}
async function removeVariant(row:ExampleVariant){if(!spaceId.value||!platformId.value)return;try{await ElMessageBox.confirm(`删除示例类型「${row.name}」及其示例？`,'确认删除',{type:'warning'});await deleteVariant(platformId.value,row.id,spaceId.value);variantId.value=undefined;await load()}catch(e){if(e!=='cancel')ElMessage.error((e as Error).message)}}
function openExample(row?:PlatformExample){editingExample.value=row;Object.assign(exampleForm,row?{kind:row.kind,name:row.name,media_type:row.media_type,content:row.content}:{kind:'combined',name:'',media_type:'application/json',content:''});exampleDialog.value=true}
async function saveExample(){if(!spaceId.value||!platformId.value||!variantId.value||!exampleForm.content.trim())return;try{if(editingExample.value)await updateExample(platformId.value,variantId.value,editingExample.value.id,spaceId.value,exampleForm);else await createExample(platformId.value,variantId.value,spaceId.value,exampleForm);exampleDialog.value=false;await load();ElMessage.success('示例已保存')}catch(e){ElMessage.error((e as Error).message)}}
async function removeExample(row:PlatformExample){if(!spaceId.value||!platformId.value||!variantId.value)return;try{await ElMessageBox.confirm(`删除示例「${row.name||row.kind}」？`,'确认删除',{type:'warning'});await deleteExample(platformId.value,variantId.value,row.id,spaceId.value);await load()}catch(e){if(e!=='cancel')ElMessage.error((e as Error).message)}}
onMounted(init)
</script>

<template><div class="page">
  <div class="page-header"><div><h1 class="page-title">平台管理</h1><p class="page-subtitle">先维护平台，再为平台维护可独立选择的示例类型和格式样例。</p></div></div>
  <el-empty v-if="!spaceId" description="当前空间默认知识库配置异常，请到知识库管理检查"/>
  <template v-else><el-card shadow="never"><template #header><div class="head"><strong>1. 平台</strong><el-button type="primary" @click="openPlatform()">新建平台</el-button></div></template><el-table :data="platforms" highlight-current-row @current-change="(row:PlatformProfile)=>{platformId=row?.id;variantId=row?.variants[0]?.id}"><el-table-column prop="name" label="平台"/><el-table-column prop="description" label="说明"/><el-table-column prop="artifact_topology" label="产物拓扑" width="140"/><el-table-column label="操作" width="150"><template #default="{row}"><el-button link @click.stop="openPlatform(row)">编辑</el-button><el-button link type="danger" @click.stop="removePlatform(row)">删除</el-button></template></el-table-column></el-table></el-card>
  <el-card shadow="never" class="section"><template #header><div class="head"><strong>2. 示例类型 <span v-if="currentPlatform">· {{currentPlatform.name}}</span></strong><el-button type="primary" :disabled="!platformId" @click="openVariant()">新建示例类型</el-button></div></template><el-empty v-if="!platformId" description="请先在上方选择平台"/><el-table v-else :data="currentPlatform?.variants||[]" highlight-current-row @current-change="(row:ExampleVariant)=>variantId=row?.id"><el-table-column prop="name" label="示例类型"/><el-table-column prop="applicability" label="适用说明"/><el-table-column label="示例数" width="90"><template #default="{row}">{{row.examples.length}}</template></el-table-column><el-table-column label="操作" width="150"><template #default="{row}"><el-button link @click.stop="openVariant(row)">编辑</el-button><el-button link type="danger" @click.stop="removeVariant(row)">删除</el-button></template></el-table-column></el-table>
  <el-divider content-position="left">示例内容 · {{currentVariant?.name||'未选择'}}</el-divider><div class="head"><span class="hint">kind、媒体类型和内容都会作为该示例类型的参考格式。编辑仅影响后续生成，历史产物不会改变；历史已引用的示例不可删除。</span><el-button :disabled="!variantId" @click="openExample()">新增示例</el-button></div><el-table :data="currentVariant?.examples||[]"><el-table-column prop="name" label="名称"/><el-table-column prop="kind" label="Kind" width="110"/><el-table-column prop="media_type" label="媒体类型" width="180"/><el-table-column prop="content" label="内容预览" show-overflow-tooltip/><el-table-column label="操作" width="150"><template #default="{row}"><el-button link @click="openExample(row)">编辑</el-button><el-button link type="danger" @click="removeExample(row)">删除</el-button></template></el-table-column></el-table></el-card></template>
  <el-dialog v-model="platformDialog" :title="editingPlatform?'编辑平台':'新建平台'"><el-form label-position="top"><el-form-item label="名称"><el-input v-model="platformForm.name"/></el-form-item><el-form-item label="说明"><el-input v-model="platformForm.description" type="textarea"/></el-form-item><el-form-item label="产物拓扑"><el-select v-model="platformForm.artifact_topology"><el-option label="用例/数据分离" value="separated"/><el-option label="用例数据一体" value="combined"/><el-option label="混合" value="hybrid"/></el-select></el-form-item></el-form><template #footer><el-button @click="platformDialog=false">取消</el-button><el-button type="primary" @click="savePlatform">保存</el-button></template></el-dialog>
  <el-dialog v-model="variantDialog" :title="editingVariant?'编辑示例类型':'新建示例类型'"><el-form label-position="top"><el-form-item label="名称"><el-input v-model="variantForm.name"/></el-form-item><el-form-item label="适用说明"><el-input v-model="variantForm.applicability" type="textarea"/></el-form-item></el-form><template #footer><el-button @click="variantDialog=false">取消</el-button><el-button type="primary" @click="saveVariant">保存</el-button></template></el-dialog>
  <el-dialog v-model="exampleDialog" :title="editingExample?'编辑示例':'新增示例'" width="720px"><el-form label-position="top"><el-row :gutter="12"><el-col :span="8"><el-form-item label="Kind"><el-select v-model="exampleForm.kind"><el-option v-for="k in ['case','data','relation','combined','attachment']" :key="k" :label="k" :value="k"/></el-select></el-form-item></el-col><el-col :span="8"><el-form-item label="名称"><el-input v-model="exampleForm.name"/></el-form-item></el-col><el-col :span="8"><el-form-item label="媒体类型"><el-input v-model="exampleForm.media_type" placeholder="text/csv"/></el-form-item></el-col></el-row><el-form-item label="内容"><el-input v-model="exampleForm.content" type="textarea" :rows="14"/></el-form-item></el-form><template #footer><el-button @click="exampleDialog=false">取消</el-button><el-button type="primary" @click="saveExample">保存</el-button></template></el-dialog>
</div></template>
<style scoped>.section{margin-top:18px}.head{display:flex;justify-content:space-between;align-items:center;gap:12px}.hint{color:var(--cg-text-muted);font-size:13px}</style>
