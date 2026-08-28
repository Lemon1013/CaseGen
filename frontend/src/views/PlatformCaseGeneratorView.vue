<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { listWikiSpaces, type WikiSpace } from '../api/wikiSpaces'
import { listDataPools, listPlatforms, listPlatformSemanticCases, renderExampleDirect, type DataPool, type PlatformProfile, type RenderRun, type SemanticCase } from '../api/platformData'
import { rememberedSpaceId, rememberAndRoute, spaceIdFromQuery } from '../utils/wikiSpace'

const route=useRoute(),router=useRouter()
const spaces=ref<WikiSpace[]>([]),spaceId=ref<number>(),platforms=ref<PlatformProfile[]>([]),pools=ref<DataPool[]>([]),cases=ref<SemanticCase[]>([])
const active=ref(0),inputMode=ref<'library'|'markdown'>('library'),selectedCases=ref<number[]>([]),externalMarkdown=ref(''),fileName=ref('')
const platformId=ref<number>(),variantId=ref<number>(),selectedRevisions=ref<number[]>([]),keywords=ref<string[]>([])
const includeDataValues=ref(false),dataSampleLimit=ref(10),rendering=ref(false),result=ref<RenderRun>(),error=ref('')
const currentPlatform=computed(()=>platforms.value.find(p=>p.id===platformId.value))
const currentVariant=computed(()=>currentPlatform.value?.variants.find(v=>v.id===variantId.value))
const inputReady=computed(()=>inputMode.value==='library'?selectedCases.value.length>0:externalMarkdown.value.trim().length>0)
const platformReady=computed(()=>!!currentPlatform.value&&!!currentVariant.value&&!!currentVariant.value.examples.length)
const summary=computed(()=>[
  {label:'输入',value:inputMode.value==='library'?`${selectedCases.value.length} 条现有语义用例`:`外部 Markdown · ${externalMarkdown.value.length} 字符`},
  {label:'平台',value:currentPlatform.value?.name||'未选择'},
  {label:'示例类型',value:currentVariant.value?.name||'未选择'},
  {label:'数据池',value:`${selectedRevisions.value.length} 个版本`},
  {label:'关键字',value:keywords.value.join('、')||'无'},
  {label:'数据值',value:includeDataValues.value?`发送最多 ${dataSampleLimit.value} 条样例`:'默认不发送'},
])
async function load(){if(!spaceId.value)return;[platforms.value,pools.value,cases.value]=await Promise.all([listPlatforms(spaceId.value),listDataPools(spaceId.value),listPlatformSemanticCases(spaceId.value)])}
function clearSelections(){active.value=0;selectedCases.value=[];externalMarkdown.value='';fileName.value='';platformId.value=undefined;variantId.value=undefined;selectedRevisions.value=[];keywords.value=[];includeDataValues.value=false;result.value=undefined;error.value=''}
async function init(){try{spaces.value=(await listWikiSpaces()).filter(s=>s.status==='active');const requested=spaceIdFromQuery(route.query)||rememberedSpaceId();if(requested&&spaces.value.some(s=>s.id===requested)){spaceId.value=requested;await load()}}catch(error){ElMessage.error(`加载平台生成数据失败：${(error as Error).message}`)}}
async function changeSpace(id:number){clearSelections();await rememberAndRoute(router,id,'/platform-case-generate');await load()}
function choosePlatform(){variantId.value=currentPlatform.value?.variants[0]?.id}
async function readMarkdown(event:Event){const input=event.target as HTMLInputElement;const file=input.files?.[0];if(!file)return;if(!file.name.toLowerCase().endsWith('.md')){ElMessage.warning('请选择 .md 文件');input.value='';return}if(file.size>200000){ElMessage.warning('Markdown 文件不能超过 200 KB');return}externalMarkdown.value=await file.text();fileName.value=file.name}
function next(){if(active.value===0&&!inputReady.value)return ElMessage.warning('请先选择语义用例或提供 Markdown');if(active.value===1&&!platformReady.value)return ElMessage.warning('请选择含示例的平台和示例类型');active.value=Math.min(3,active.value+1)}
function safeDownloadName(filename:string){const basename=filename.replace(/\\/g,'/').split('/').pop()||'artifact.txt';const cleaned=basename.replace(/[\u0000-\u001f\u007f]/g,'_').slice(0,240);return !cleaned||cleaned==='.'||cleaned==='..'?'artifact.txt':cleaned}
function downloadArtifact(artifact:RenderRun['artifacts'][number]){const blob=new Blob([artifact.content],{type:artifact.media_type||'text/plain'});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=safeDownloadName(artifact.filename);link.click();URL.revokeObjectURL(url)}
async function generate(){if(!spaceId.value||!platformId.value||!variantId.value||!inputReady.value)return;rendering.value=true;error.value='';result.value=undefined;try{result.value=await renderExampleDirect({wiki_space_id:spaceId.value,platform_id:platformId.value,variant_id:variantId.value,test_case_ids:inputMode.value==='library'?selectedCases.value:[],external_case_markdown:inputMode.value==='markdown'?externalMarkdown.value:undefined,keywords:keywords.value,data_pool_revision_ids:selectedRevisions.value,include_data_values:includeDataValues.value,data_sample_limit:dataSampleLimit.value});ElMessage.success('平台用例已生成')}catch(e){error.value=(e as Error).message;ElMessage.error(error.value)}finally{rendering.value=false}}
onMounted(init)
</script>

<template><div class="page">
 <div class="page-header"><div><h1 class="page-title">生成平台用例</h1><p class="page-subtitle">选择语义输入、单个示例类型和数据上下文，生成可下载的平台产物。</p></div><el-select v-model="spaceId" placeholder="请选择知识空间" style="width:220px" @change="changeSpace"><el-option v-for="s in spaces" :key="s.id" :label="s.name" :value="s.id"/></el-select></div>
 <el-empty v-if="!spaceId" description="请选择知识空间，系统不会自动替你切换项目"/>
 <template v-else><el-card shadow="never"><el-steps :active="active" finish-status="success"><el-step title="01 选取输入"/><el-step title="02 平台与示例类型"/><el-step title="03 数据与关键字"/><el-step title="04 摘要与生成"/></el-steps></el-card>
 <el-card shadow="never" class="stage">
  <section v-show="active===0"><h3>01 选取输入</h3><el-radio-group v-model="inputMode"><el-radio-button value="library">现有语义用例</el-radio-button><el-radio-button value="markdown">本地 Markdown</el-radio-button></el-radio-group><div v-if="inputMode==='library'" class="field"><el-select v-model="selectedCases" multiple filterable placeholder="选择当前空间的语义用例" style="width:100%"><el-option v-for="c in cases" :key="c.id" :label="`${c.case_key} · ${c.title}`" :value="c.id"/></el-select><el-empty v-if="!cases.length" description="当前空间暂无已入库语义用例" :image-size="70"/></div><div v-else class="field"><input id="markdown-file" type="file" accept=".md,text/markdown" class="hidden" @change="readMarkdown"><label for="markdown-file" class="file-button">选择 .md 文件</label><span class="file-name">{{fileName||'未选择文件；内容只在浏览器读取并随生成请求提交'}}</span><el-input v-model="externalMarkdown" type="textarea" :rows="15" placeholder="# 在此预览或编辑 Markdown 用例"/></div></section>
  <section v-show="active===1"><h3>02 选择平台及单个示例类型</h3><el-form label-position="top"><el-form-item label="平台"><el-select v-model="platformId" style="width:100%" @change="choosePlatform"><el-option v-for="p in platforms" :key="p.id" :label="`${p.name} · ${p.artifact_topology}`" :value="p.id"/></el-select></el-form-item><el-form-item label="示例类型"><el-select v-model="variantId" style="width:100%"><el-option v-for="v in currentPlatform?.variants||[]" :key="v.id" :label="`${v.name} · ${v.examples.length} 个示例`" :value="v.id"/></el-select></el-form-item></el-form><el-alert v-if="currentVariant&&!currentVariant.examples.length" title="该示例类型没有示例，请先到平台管理中维护" type="warning" :closable="false"/></section>
  <section v-show="active===2"><h3>03 数据与关键字</h3><el-form label-position="top"><el-form-item label="数据池最新版本（可多选）"><el-select v-model="selectedRevisions" multiple style="width:100%"><el-option v-for="p in pools.filter(p=>p.latest_revision)" :key="p.latest_revision!.id" :label="`${p.name} · v${p.latest_revision!.revision} · ${p.latest_revision!.record_count} 条`" :value="p.latest_revision!.id"/></el-select></el-form-item><el-form-item label="关键字（可选，回车创建）"><el-select v-model="keywords" multiple filterable allow-create default-first-option style="width:100%"/></el-form-item><el-form-item><el-checkbox v-model="includeDataValues">明确允许向模型发送有限数据样例</el-checkbox><el-input-number v-if="includeDataValues" v-model="dataSampleLimit" :min="1" :max="20" class="limit"/><div class="privacy">默认仅发送池名、Schema 和记录数；开启后最多发送 20 条，请确认数据不含敏感信息。</div></el-form-item></el-form></section>
  <section v-show="active===3"><h3>04 摘要与生成</h3><el-descriptions :column="2" border><el-descriptions-item v-for="item in summary" :key="item.label" :label="item.label">{{item.value}}</el-descriptions-item></el-descriptions><el-button type="primary" size="large" class="generate" :loading="rendering" @click="generate">生成平台产物</el-button><el-alert v-if="error" :title="error" type="error" :closable="false" class="field"/><template v-if="result"><el-tag :type="result.status==='completed'?'success':'danger'">{{result.status}}</el-tag><el-alert v-for="warning in result.warnings" :key="warning" :title="warning" type="warning" :closable="false" class="field"/><el-table :data="result.artifacts" class="field"><el-table-column prop="kind" label="Kind" width="110"/><el-table-column prop="filename" label="文件"/><el-table-column prop="media_type" label="媒体类型"/><el-table-column label="操作" width="100"><template #default="{row}"><el-button link type="primary" @click="downloadArtifact(row)">下载</el-button></template></el-table-column></el-table></template></section>
  <div class="nav"><el-button :disabled="active===0" @click="active--">上一步</el-button><el-button v-if="active<3" type="primary" @click="next">下一步</el-button></div>
 </el-card></template>
</div></template>
<style scoped>.stage{margin-top:18px}.field{margin-top:16px}.nav{display:flex;justify-content:flex-end;gap:10px;margin-top:24px}.hidden{display:none}.file-button{display:inline-block;padding:8px 14px;border:1px solid var(--el-color-primary);color:var(--el-color-primary);border-radius:6px;cursor:pointer;margin-bottom:12px}.file-name{margin-left:12px;color:var(--cg-text-muted);font-size:13px}.limit{margin-left:12px}.privacy{width:100%;font-size:12px;color:var(--cg-text-muted);margin-top:6px}.generate{margin-top:20px}</style>
