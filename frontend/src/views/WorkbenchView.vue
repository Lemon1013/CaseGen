<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { listModels, type ModelConfig } from '../api/models'
import { listPrompts, type PromptTemplate } from '../api/prompts'
import { createTask, generateTask } from '../api/tasks'
import { listWikiSpaces, type WikiSpace } from '../api/wikiSpaces'
import { listRequirements, type RequirementItem } from '../api/requirements'
import { chooseSpace, rememberAndRoute, spaceIdFromQuery } from '../utils/wikiSpace'

const router = useRouter()
const route = useRoute()
const submitting = ref(false)
const models = ref<ModelConfig[]>([])
const prompts = ref<PromptTemplate[]>([])
const spaces = ref<WikiSpace[]>([])
const currentSpace = ref<WikiSpace | null>(null)
const requirements = ref<RequirementItem[]>([])

const form = reactive({
  title: '',
  description: '',
  focusText: '',
  model_id: null as number | null,
  prompt_template_id: null as number | null,
  auto_review: false,
  wiki_space_id: null as number | null,
  requirement_id: null as number | null,
})

async function loadOptions() {
  try {
    const [m, p, s, reqs] = await Promise.all([
      listModels(),
      listPrompts('generate'),
      listWikiSpaces(),
      listRequirements().catch(() => [] as RequirementItem[]),
    ])
    models.value = m
    prompts.value = p.filter((item) => item.is_active)
    const defaultModel = m.find((x) => x.is_default)
    if (defaultModel) form.model_id = defaultModel.id
    const activePrompt = prompts.value[0]
    if (activePrompt) form.prompt_template_id = activePrompt.id
    spaces.value = s
    requirements.value = reqs
    currentSpace.value = chooseSpace(s, spaceIdFromQuery(route.query))
    form.wiki_space_id = currentSpace.value?.id || null
    if (currentSpace.value && spaceIdFromQuery(route.query) !== currentSpace.value.id) {
      await rememberAndRoute(router, currentSpace.value.id, '/')
    }
  } catch (e) {
    ElMessage.error(`加载选项失败：${(e as Error).message}`)
  }
}

function parseFocusTags(text: string): string[] {
  return text
    .split(/[,，\s]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

function changeSpace(id: number) {
  currentSpace.value = spaces.value.find((space) => space.id === id) || null
  form.wiki_space_id = currentSpace.value?.id || null
  void rememberAndRoute(router, id, '/')
}

function selectRequirement(id: number | null) {
  const requirement = requirements.value.find((item) => item.id === id)
  if (!requirement) return
  form.title = requirement.title
  form.description = requirement.description
  form.focusText = requirement.focus_tags.join(', ')
}

async function submit() {
  if (!form.title.trim() || !form.description.trim()) {
    ElMessage.warning('请填写标题和需求描述')
    return
  }
  if (!form.wiki_space_id || !currentSpace.value || currentSpace.value.status !== 'active') {
    ElMessage.warning('请选择活动 Wiki 空间')
    return
  }
  submitting.value = true
  try {
    // 1) 快速创建任务（不阻塞在 LLM 上）
    const task = await createTask({
      requirement_id: form.requirement_id,
      title: form.title.trim(),
      description: form.description.trim(),
      focus_tags: parseFocusTags(form.focusText),
      model_id: form.model_id,
      prompt_template_id: form.prompt_template_id,
      auto_review: form.auto_review,
      run_generate: false,
      wiki_space_id: form.wiki_space_id,
    })

    // 2) 触发后台生成（接口立即返回 retrieving/generating），不在此页空转等待
    try {
      await generateTask(task.id, { auto_review: form.auto_review })
    } catch (e) {
      ElMessage.warning(
        `启动生成失败：${(e as Error).message}，请到任务详情页重试`,
      )
    }

    ElMessage.success(`任务 #${task.id} 已创建，正在后台生成`)
    await router.push(`/tasks/${task.id}`)
  } catch (e) {
    ElMessage.error(`创建任务失败：${(e as Error).message}`)
  } finally {
    submitting.value = false
  }
}

onMounted(loadOptions)
</script>

<template>
  <div class="page workbench-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">创建生成任务</h1>
        <p class="page-subtitle">描述业务场景，系统将结合 Wiki 知识生成可评审的测试用例</p>
      </div>
    </div>

    <div class="step-rail">
      <div class="step-item">
        <div class="step-index">STEP 01</div>
        <div class="step-label">需求填写</div>
        <div class="step-desc">标题、场景与关注点</div>
      </div>
      <div class="step-item">
        <div class="step-index">STEP 02</div>
        <div class="step-label">AI 生成</div>
        <div class="step-desc">检索 Wiki 并输出草稿</div>
      </div>
      <div class="step-item">
        <div class="step-index">STEP 03</div>
        <div class="step-label">AI 评审</div>
        <div class="step-desc">打分、缺口与 Prompt 迭代</div>
      </div>
    </div>

    <div class="form-shell">
      <el-form label-width="110px" class="workbench-form" @submit.prevent>
        <el-form-item label="标题" required>
          <el-input
            v-model="form.title"
            placeholder="例如：现货限价单余额不足"
            maxlength="120"
            show-word-limit
          />
        </el-form-item>
        <el-form-item label="已有需求">
          <el-select
            v-model="form.requirement_id"
            clearable
            filterable
            placeholder="可复用已有 Requirement（可选）"
            style="width: 100%"
            @change="selectRequirement"
          >
            <el-option
              v-for="requirement in requirements"
              :key="requirement.id"
              :label="`#${requirement.id} · ${requirement.title}`"
              :value="requirement.id"
            />
          </el-select>
          <div class="field-hint">选择后会沿用需求分组，生成任务不会复制 Requirement。</div>
        </el-form-item>
        <el-form-item label="需求描述" required>
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="8"
            placeholder="描述业务场景、前置条件、关注规则等"
          />
        </el-form-item>
        <el-form-item label="关注点">
          <el-input
            v-model="form.focusText"
            placeholder="多个标签用逗号或空格分隔，如：余额校验, 限价单"
          />
        </el-form-item>
        <el-form-item label="Wiki 空间" required>
          <el-select
            v-model="form.wiki_space_id"
            style="width: 100%"
            placeholder="选择活动 Wiki 空间"
            @change="changeSpace"
          >
            <el-option
              v-for="space in spaces.filter((item) => item.status === 'active')"
              :key="space.id"
              :label="`${space.name} · ${space.document_count} 文档 / ${space.page_count} 页面`"
              :value="space.id"
            />
          </el-select>
          <div class="field-hint">任务创建后空间会固定，生成与引用不会跨空间。</div>
        </el-form-item>
        <el-form-item label="模型">
          <el-select v-model="form.model_id" clearable placeholder="使用默认模型" style="width: 100%">
            <el-option
              v-for="m in models"
              :key="m.id"
              :label="`${m.name} (${m.model_name})${m.is_default ? ' · 默认' : ''}`"
              :value="m.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="生成 Prompt">
          <el-select
            v-model="form.prompt_template_id"
            clearable
            placeholder="使用启用中的 generate 模板"
            style="width: 100%"
          >
            <el-option
              v-for="p in prompts"
              :key="p.id"
              :label="`${p.name} (v${p.version})${p.is_active ? ' · 启用' : ''}`"
              :value="p.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="自动评审">
          <div class="switch-row">
            <el-switch v-model="form.auto_review" />
            <span class="hint">生成完成后自动进入评审</span>
          </div>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" size="large" :loading="submitting" @click="submit">
            创建并生成
          </el-button>
          <el-button size="large" @click="router.push('/tasks')">查看生成任务</el-button>
        </el-form-item>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.form-shell {
  max-width: 820px;
  padding: 8px 4px 0;
}

.workbench-form {
  max-width: 760px;
}

.switch-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.hint {
  color: var(--cg-text-muted);
  font-size: 13px;
}

.field-hint {
  margin-top: 5px;
  color: var(--cg-text-muted);
  font-size: 12px;
  line-height: 1.5;
}
</style>
