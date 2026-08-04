<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createModel,
  deleteModel,
  listModels,
  pingModel,
  updateModel,
  type ModelConfig,
} from '../api/models'

const loading = ref(false)
const models = ref<ModelConfig[]>([])
const dialogVisible = ref(false)
const saving = ref(false)
const pingingId = ref<number | null>(null)
const editingId = ref<number | null>(null)

const form = reactive({
  name: '',
  base_url: '',
  api_key: '',
  model_name: '',
  is_default: false,
})

function resetForm() {
  form.name = ''
  form.base_url = ''
  form.api_key = ''
  form.model_name = ''
  form.is_default = false
  editingId.value = null
}

async function load() {
  loading.value = true
  try {
    models.value = await listModels()
  } catch (e) {
    ElMessage.error(`加载模型失败：${(e as Error).message}`)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  resetForm()
  dialogVisible.value = true
}

function openEdit(row: ModelConfig) {
  editingId.value = row.id
  form.name = row.name
  form.base_url = row.base_url
  form.api_key = ''
  form.model_name = row.model_name
  form.is_default = row.is_default
  dialogVisible.value = true
}

async function submit() {
  if (!form.name.trim() || !form.base_url.trim() || !form.model_name.trim()) {
    ElMessage.warning('请填写名称、Base URL 和模型名')
    return
  }
  if (!editingId.value && !form.api_key.trim()) {
    ElMessage.warning('新建时请填写 API Key')
    return
  }

  saving.value = true
  try {
    if (editingId.value) {
      const body: Record<string, unknown> = {
        name: form.name.trim(),
        base_url: form.base_url.trim(),
        model_name: form.model_name.trim(),
        is_default: form.is_default,
      }
      if (form.api_key.trim()) {
        body.api_key = form.api_key.trim()
      }
      await updateModel(editingId.value, body)
      ElMessage.success('已更新模型')
    } else {
      await createModel({
        name: form.name.trim(),
        base_url: form.base_url.trim(),
        api_key: form.api_key.trim(),
        model_name: form.model_name.trim(),
        is_default: form.is_default,
      })
      ElMessage.success('已创建模型')
    }
    dialogVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(`保存失败：${(e as Error).message}`)
  } finally {
    saving.value = false
  }
}

async function handlePing(row: ModelConfig) {
  pingingId.value = row.id
  try {
    const res = await pingModel(row.id)
    ElMessage.success(res.ok ? `连接成功：${res.content || 'ok'}` : '连接失败')
  } catch (e) {
    ElMessage.error(`测试连接失败：${(e as Error).message}`)
  } finally {
    pingingId.value = null
  }
}

async function handleDelete(row: ModelConfig) {
  try {
    await ElMessageBox.confirm(`确认删除模型「${row.name}」？`, '删除确认', {
      type: 'warning',
    })
    await deleteModel(row.id)
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(`删除失败：${(e as Error).message}`)
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1 class="page-title">模型配置</h1>
        <p class="page-subtitle">管理 OpenAI 兼容网关与默认推理模型</p>
      </div>
      <div class="page-actions">
        <el-button type="primary" @click="openCreate">新建模型</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="models" stripe empty-text="暂无模型，先添加一个网关">
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column prop="model_name" label="模型名" min-width="140" />
      <el-table-column prop="base_url" label="Base URL" min-width="200" show-overflow-tooltip />
      <el-table-column prop="api_key" label="API Key" width="120" />
      <el-table-column label="默认" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.is_default" type="success" size="small" effect="dark">默认</el-tag>
          <span v-else class="muted">否</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="260" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button
            link
            type="success"
            :loading="pingingId === row.id"
            @click="handlePing(row)"
          >
            测试连接
          </el-button>
          <el-button link type="danger" @click="handleDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑模型' : '新建模型'"
      width="520px"
      destroy-on-close
    >
      <el-form label-width="100px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="如：DeepSeek / GPT" />
        </el-form-item>
        <el-form-item label="Base URL" required>
          <el-input v-model="form.base_url" placeholder="https://api.example.com/v1" />
        </el-form-item>
        <el-form-item label="API Key" :required="!editingId">
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            :placeholder="editingId ? '留空则不修改' : '请输入 API Key'"
          />
        </el-form-item>
        <el-form-item label="模型名" required>
          <el-input v-model="form.model_name" placeholder="如：deepseek-chat" />
        </el-form-item>
        <el-form-item label="设为默认">
          <el-switch v-model="form.is_default" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.muted {
  color: var(--cg-text-muted);
}
</style>
