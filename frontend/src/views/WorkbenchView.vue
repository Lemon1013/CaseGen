<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { listModels, type ModelConfig } from '../api/models'
import { listPrompts, type PromptTemplate } from '../api/prompts'
import { createTask, generateTask, reviewTask } from '../api/tasks'

const router = useRouter()
const submitting = ref(false)
const models = ref<ModelConfig[]>([])
const prompts = ref<PromptTemplate[]>([])

const form = reactive({
  title: '',
  description: '',
  focusText: '',
  model_id: null as number | null,
  prompt_template_id: null as number | null,
  auto_review: false,
})

async function loadOptions() {
  try {
    const [m, p] = await Promise.all([listModels(), listPrompts('generate')])
    models.value = m
    prompts.value = p
    const defaultModel = m.find((x) => x.is_default)
    if (defaultModel) form.model_id = defaultModel.id
    const activePrompt = p.find((x) => x.is_active)
    if (activePrompt) form.prompt_template_id = activePrompt.id
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

async function submit() {
  if (!form.title.trim() || !form.description.trim()) {
    ElMessage.warning('请填写标题和需求描述')
    return
  }
  submitting.value = true
  try {
    const task = await createTask({
      title: form.title.trim(),
      description: form.description.trim(),
      focus_tags: parseFocusTags(form.focusText),
      model_id: form.model_id,
      prompt_template_id: form.prompt_template_id,
      auto_review: form.auto_review,
      run_generate: false,
    })
    ElMessage.success(`任务 #${task.id} 已创建，开始生成…`)
    // Backend generate is synchronous; await so detail shows results when possible.
    try {
      const generated = await generateTask(task.id)
      ElMessage.success('生成完成')
      if (form.auto_review && generated.status === 'generated') {
        await reviewTask(task.id)
        ElMessage.success('自动评审完成')
      }
    } catch (e) {
      ElMessage.warning(`生成/评审返回错误：${(e as Error).message}，请在详情页查看状态`)
    }
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
  <div class="page">
    <div class="page-header">
      <div>
        <h1>工作台</h1>
        <p class="subtitle">填写需求，创建任务并生成测试用例草稿</p>
      </div>
    </div>

    <el-form label-width="110px" class="workbench-form" @submit.prevent>
      <el-form-item label="标题" required>
        <el-input v-model="form.title" placeholder="例如：现货限价单余额不足" maxlength="120" show-word-limit />
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
        <el-switch v-model="form.auto_review" />
        <span class="hint">生成完成后是否自动进入评审（当前在详情页可手动触发）</span>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" size="large" :loading="submitting" @click="submit">
          创建并生成
        </el-button>
        <el-button size="large" @click="router.push('/tasks')">查看任务列表</el-button>
      </el-form-item>
    </el-form>
  </div>
</template>

<style scoped>
.page-header {
  margin-bottom: 8px;
}

.page-header h1 {
  margin: 0 0 6px;
}

.subtitle {
  margin: 0 0 20px;
  color: #909399;
  font-size: 14px;
}

.workbench-form {
  max-width: 760px;
}

.hint {
  margin-left: 12px;
  color: #909399;
  font-size: 13px;
}
</style>
