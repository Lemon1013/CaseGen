<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { listModels, type ModelConfig } from '../api/models'
import { listPrompts, type PromptTemplate } from '../api/prompts'
import { createTask, generateTask, optimizeRequirement } from '../api/tasks'
import { listCases, type TestCaseItem } from '../api/cases'
import { listWikiSpaces, type WikiSpace } from '../api/wikiSpaces'
import { listRequirements, type RequirementItem } from '../api/requirements'
import { chooseSpace, rememberAndRoute, spaceIdFromQuery } from '../utils/wikiSpace'

const router = useRouter()
const route = useRoute()
const submitting = ref(false)
const optimizing = ref(false)
const loadingReferences = ref(false)
const models = ref<ModelConfig[]>([])
const prompts = ref<PromptTemplate[]>([])
const spaces = ref<WikiSpace[]>([])
const currentSpace = ref<WikiSpace | null>(null)
const requirements = ref<RequirementItem[]>([])
const referenceCases = ref<TestCaseItem[]>([])
const selectedReferenceIds = ref<number[]>([])
const referenceSearch = ref('')
const referenceTab = ref('library')
const questions = ref<string[]>([])
const showQuestions = ref(false)

type GenerationGranularity = 'compact' | 'standard' | 'detailed'

const granularityOptions: Array<{
  value: GenerationGranularity
  label: string
  amount: string
  coverage: string
  suitable: string
  recommended?: boolean
}> = [
  {
    value: 'compact',
    label: '精简',
    amount: '2–3 条/功能点',
    coverage: '只展开最高优先级流程和阻断性异常',
    suitable: '适合快速评审、需求早期',
  },
  {
    value: 'standard',
    label: '标准',
    amount: '5–8 条/功能点',
    coverage: '主要流程、常见异常和关键边界',
    suitable: '适合日常需求、常规回归',
    recommended: true,
  },
  {
    value: 'detailed',
    label: '全面',
    amount: '10+ 条/功能点',
    coverage: '状态组合、异常链路和深层边界',
    suitable: '适合核心链路、高风险发布',
  },
]

const dimensionOptions: Array<[string, string]> = [
  ['positive', '正向'], ['negative', '反向'], ['boundary', '边界'],
  ['permission', '权限'], ['security', '安全'], ['compatibility', '兼容性'],
  ['performance', '性能'], ['recovery', '恢复'], ['usability', '可用性'],
]
const dimensionLabelMap = new Map(dimensionOptions)
const form = reactive({
  title: '', description: '', focusText: '', model_id: null as number | null,
  prompt_template_id: null as number | null, auto_review: false,
  wiki_space_id: null as number | null, requirement_id: null as number | null,
  generation_granularity: 'standard' as GenerationGranularity,
  test_dimensions: ['positive', 'negative', 'boundary'] as string[], reference_text: '',
})
const selectedCases = computed(() => referenceCases.value.filter((row) => selectedReferenceIds.value.includes(row.id)))
const referenceChars = computed(() => selectedCases.value.reduce((n, row) => n + row.content_md.length, 0) + form.reference_text.length)
const selectedGranularity = computed(() => granularityOptions.find((item) => item.value === form.generation_granularity) || granularityOptions[1])
const dimensionSummary = computed(() => form.test_dimensions.map((value) => dimensionLabelMap.get(value) || value).join('、') || '未选择')

function tags(text: string) { return text.split(/[,，\s]+/).map((x) => x.trim()).filter(Boolean).slice(0, 30) }

async function searchReferences() {
  loadingReferences.value = true
  try {
    referenceCases.value = await listCases({ keyword: referenceSearch.value, status: 'active' })
  } catch (e) { ElMessage.error(`加载参考用例失败：${(e as Error).message}`) }
  finally { loadingReferences.value = false }
}

async function loadOptions() {
  try {
    const [modelsRows, promptRows, spaceRows, reqRows] = await Promise.all([
      listModels(), listPrompts('generate'), listWikiSpaces(), listRequirements().catch(() => [] as RequirementItem[]),
    ])
    models.value = modelsRows
    prompts.value = promptRows.filter((row) => row.is_active)
    form.model_id = modelsRows.find((row) => row.is_default)?.id ?? null
    form.prompt_template_id = prompts.value[0]?.id ?? null
    spaces.value = spaceRows
    requirements.value = reqRows
    currentSpace.value = chooseSpace(spaceRows, spaceIdFromQuery(route.query))
    form.wiki_space_id = currentSpace.value?.id ?? null
    if (currentSpace.value && spaceIdFromQuery(route.query) !== currentSpace.value.id) await rememberAndRoute(router, currentSpace.value.id, '/')
    await searchReferences()
  } catch (e) { ElMessage.error(`加载选项失败：${(e as Error).message}`) }
}

function selectRequirement(id: number | null) {
  const row = requirements.value.find((item) => item.id === id)
  if (!row) return
  form.title = row.title; form.description = row.description; form.focusText = row.focus_tags.join(', ')
}

function changeSpace(id: number) {
  currentSpace.value = spaces.value.find((row) => row.id === id) || null
  form.wiki_space_id = currentSpace.value?.id ?? null
  void rememberAndRoute(router, id, '/')
}

async function optimize() {
  if (!form.title.trim() || !form.description.trim()) return ElMessage.warning('请先填写标题和需求描述')
  optimizing.value = true
  try {
    const result = await optimizeRequirement({ title: form.title, description: form.description, focus_tags: tags(form.focusText), model_id: form.model_id })
    form.title = result.title; form.description = result.description; questions.value = result.questions; showQuestions.value = true
    ElMessage.success('已返回可编辑的需求优化建议')
  } catch (e) { ElMessage.error(`需求优化失败：${(e as Error).message}`) }
  finally { optimizing.value = false }
}

async function submit() {
  if (!form.title.trim() || !form.description.trim()) return ElMessage.warning('请填写标题和需求描述')
  if (!form.wiki_space_id || currentSpace.value?.status !== 'active') return ElMessage.warning('请选择活动 Wiki 空间')
  if (!form.test_dimensions.length) return ElMessage.warning('至少选择一个测试维度')
  if (referenceChars.value > 30000) return ElMessage.warning('参考用例快照总长度不能超过 30000 字符')
  submitting.value = true
  try {
    const task = await createTask({
      requirement_id: form.requirement_id, title: form.title.trim(), description: form.description.trim(),
      focus_tags: tags(form.focusText), model_id: form.model_id, prompt_template_id: form.prompt_template_id,
      auto_review: form.auto_review, wiki_space_id: form.wiki_space_id, generation_granularity: form.generation_granularity,
      test_dimensions: form.test_dimensions, reference_case_ids: selectedReferenceIds.value, reference_text: form.reference_text.trim(),
    })
    await generateTask(task.id, { auto_review: form.auto_review }).catch((e) => ElMessage.warning(`启动生成失败：${(e as Error).message}`))
    await router.push(`/tasks/${task.id}`)
  } catch (e) { ElMessage.error(`创建任务失败：${(e as Error).message}`) }
  finally { submitting.value = false }
}

onMounted(loadOptions)
</script>

<template>
  <div class="page workbench-page">
    <div class="page-header"><div><h1 class="page-title">测试设计工作台</h1><p class="page-subtitle">需求 → 策略 → 参考用例 → 证据与测试点 → 完整用例</p></div><el-button @click="router.push('/tasks')">查看任务</el-button></div>
    <div class="step-rail"><div class="step-item active">01 需求与优化<br><small>标题、描述和待确认问题</small></div><div class="step-item">02 测试点确认<br><small>检索确认后的结构化检查点</small></div><div class="step-item">03 用例与覆盖<br><small>生成、评审、终版和追溯</small></div></div>
    <el-row :gutter="16">
      <el-col :xs="24" :lg="15">
        <el-card shadow="never" class="block"><template #header>需求输入与优化</template>
          <el-form label-position="top" @submit.prevent>
            <el-form-item label="已有需求"><el-select v-model="form.requirement_id" clearable filterable style="width:100%" placeholder="可选" @change="selectRequirement"><el-option v-for="row in requirements" :key="row.id" :label="`#${row.id} · ${row.title}`" :value="row.id" /></el-select></el-form-item>
            <el-form-item label="标题" required><el-input v-model="form.title" maxlength="120" show-word-limit /></el-form-item>
            <el-form-item label="需求描述" required><el-input v-model="form.description" type="textarea" :rows="8" maxlength="20000" show-word-limit /></el-form-item>
            <el-form-item label="关注标签"><el-input v-model="form.focusText" placeholder="多个标签用逗号或空格分隔" /></el-form-item>
            <el-button type="primary" plain :loading="optimizing" @click="optimize">AI 优化需求</el-button><span class="hint"> 返回结果可继续手动编辑，提交时才持久化。</span>
          </el-form>
          <el-alert v-if="showQuestions" class="questions" type="warning" :closable="false" title="待确认问题"><ul><li v-for="item in questions" :key="item">{{ item }}</li><li v-if="!questions.length">暂无</li></ul></el-alert>
        </el-card>
        <el-card shadow="never" class="block"><template #header>参考用例（可选）</template><p class="hint">仅参考格式、拆分粒度和表达风格，不作为业务事实。</p>
          <el-tabs v-model="referenceTab"><el-tab-pane label="从用例库选择" name="library"><div class="reference-search"><el-input v-model="referenceSearch" placeholder="搜索用例" @keyup.enter="searchReferences" /><el-button :loading="loadingReferences" @click="searchReferences">搜索</el-button></div><el-checkbox-group v-model="selectedReferenceIds" class="reference-list"><el-checkbox v-for="row in referenceCases" :key="row.id" :value="row.id" class="reference-row"><span>{{ row.case_key }} · {{ row.title }}</span><small>{{ row.priority || 'P1' }} · {{ row.content_md.length }} 字符</small></el-checkbox></el-checkbox-group><el-empty v-if="!referenceCases.length && !loadingReferences" description="暂无可选用例" :image-size="50" /></el-tab-pane><el-tab-pane label="手动输入" name="manual"><el-input v-model="form.reference_text" type="textarea" :rows="8" maxlength="16000" show-word-limit /></el-tab-pane></el-tabs>
          <div class="hint reference-total">已选 {{ selectedCases.length }} 条，快照 {{ referenceChars }} / 30000 字符</div>
          <div class="tags"><el-tag v-for="row in selectedCases" :key="row.id" closable @close="selectedReferenceIds = selectedReferenceIds.filter((id) => id !== row.id)">{{ row.case_key }}</el-tag></div>
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="9">
        <el-card shadow="never" class="block"><template #header>生成策略</template><el-form label-position="top">
          <el-form-item label="生成粒度">
            <div class="granularity-field">
              <div class="granularity-options" role="group" aria-label="生成粒度">
                <button
                  v-for="item in granularityOptions"
                  :key="item.value"
                  type="button"
                  class="granularity-card"
                  :class="{ selected: form.generation_granularity === item.value }"
                  :aria-pressed="form.generation_granularity === item.value"
                  @click="form.generation_granularity = item.value"
                >
                  <span class="granularity-title-row">
                    <strong>{{ item.label }}</strong>
                    <span v-if="item.recommended" class="recommended-badge">推荐</span>
                  </span>
                  <span class="granularity-amount">{{ item.amount }}</span>
                  <span class="granularity-coverage">{{ item.coverage }}</span>
                  <span class="granularity-suitable">{{ item.suitable }}</span>
                </button>
              </div>
              <p class="granularity-guidance" aria-live="polite">
                当前选择：{{ selectedGranularity.label }}（{{ selectedGranularity.amount }}）。{{ selectedGranularity.coverage }}；{{ selectedGranularity.suitable }}。覆盖方向仍由下方测试维度决定。
              </p>
            </div>
          </el-form-item>
          <el-form-item label="通用测试维度"><el-checkbox-group v-model="form.test_dimensions" class="dimensions"><el-checkbox v-for="item in dimensionOptions" :key="item[0]" :value="item[0]">{{ item[1] }}</el-checkbox></el-checkbox-group></el-form-item>
          <el-form-item label="Wiki 空间"><el-select v-model="form.wiki_space_id" style="width:100%" @change="changeSpace"><el-option v-for="row in spaces.filter((item) => item.status === 'active')" :key="row.id" :label="row.name" :value="row.id" /></el-select></el-form-item>
          <el-form-item label="模型"><el-select v-model="form.model_id" clearable style="width:100%"><el-option v-for="row in models" :key="row.id" :label="`${row.name} (${row.model_name})`" :value="row.id" /></el-select></el-form-item>
          <el-form-item label="生成 Prompt"><el-select v-model="form.prompt_template_id" clearable style="width:100%"><el-option v-for="row in prompts" :key="row.id" :label="`${row.name} (v${row.version})`" :value="row.id" /></el-select></el-form-item>
          <el-form-item label="生成后自动评审"><el-switch v-model="form.auto_review" /></el-form-item>
        </el-form></el-card>
        <el-card shadow="never" class="block"><template #header>提交摘要</template><el-descriptions :column="1" border size="small"><el-descriptions-item label="粒度">{{ selectedGranularity.label }}</el-descriptions-item><el-descriptions-item label="维度">{{ dimensionSummary }}</el-descriptions-item><el-descriptions-item label="参考">{{ selectedCases.length }} 条库内 + {{ form.reference_text.trim() ? '手动输入' : '无手动输入' }}</el-descriptions-item><el-descriptions-item label="流程">检索确认 → 测试点确认 → 完整用例</el-descriptions-item></el-descriptions><el-button class="submit" type="primary" size="large" :loading="submitting" @click="submit">创建并开始检索</el-button></el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.step-rail {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}

.step-item {
  padding: 14px;
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius);
  background: var(--cg-surface);
  font-weight: 700;
}

.step-item.active {
  border-color: var(--el-color-primary);
}

small,
.hint {
  color: var(--cg-text-muted);
  font-size: 12px;
  font-weight: 400;
}

.block {
  margin-bottom: 16px;
  border-radius: var(--cg-radius) !important;
}

.questions {
  margin-top: 14px;
}

.questions ul {
  margin: 0;
  padding-left: 18px;
}

.reference-search {
  display: flex;
  gap: 8px;
}

.reference-search .el-input {
  flex: 1;
}

.reference-list {
  display: grid;
  gap: 8px;
  max-height: 260px;
  overflow: auto;
  margin-top: 12px;
}

.reference-row {
  height: auto;
  padding: 8px;
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius-sm);
  white-space: normal;
}

.reference-row :deep(.el-checkbox__label) {
  display: flex;
  flex-direction: column;
  white-space: normal;
}

.reference-total {
  margin-top: 10px;
}

.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.granularity-field {
  width: 100%;
}

.granularity-options {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  width: 100%;
}

.granularity-card {
  display: flex;
  min-width: 0;
  min-height: 198px;
  padding: 14px;
  flex-direction: column;
  appearance: none;
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius-sm);
  background: var(--cg-surface);
  color: var(--el-text-color-primary);
  cursor: pointer;
  font: inherit;
  text-align: left;
  transition: border-color 0.2s ease, background-color 0.2s ease, box-shadow 0.2s ease;
}

.granularity-card:hover {
  border-color: var(--el-color-primary-light-5);
}

.granularity-card:focus-visible {
  outline: 3px solid var(--el-color-primary-light-5);
  outline-offset: 2px;
}

.granularity-card.selected {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  box-shadow: inset 0 0 0 1px var(--el-color-primary), 0 6px 16px rgb(84 92 255 / 12%);
}

.granularity-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 16px;
}

.recommended-badge {
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--el-color-primary);
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  line-height: 18px;
}

.granularity-amount {
  margin-top: 10px;
  color: var(--el-color-primary);
  font-size: 13px;
  font-weight: 700;
}

.granularity-coverage,
.granularity-suitable {
  margin-top: 8px;
  color: var(--el-text-color-regular);
  font-size: 12px;
  line-height: 1.55;
}

.granularity-suitable {
  margin-top: auto;
  padding-top: 10px;
  color: var(--cg-text-muted);
}

.granularity-guidance {
  margin: 10px 0 0;
  color: var(--cg-text-muted);
  font-size: 12px;
  line-height: 1.6;
}

.dimensions {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
}

.submit {
  width: 100%;
  margin-top: 16px;
}

@media (max-width: 900px) {
  .step-rail {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 640px) {
  .granularity-options {
    grid-template-columns: 1fr;
  }

  .granularity-card {
    min-height: auto;
  }

  .granularity-suitable {
    margin-top: 8px;
    padding-top: 0;
  }
}
</style>
