<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  createPrompt,
  listPrompts,
  PROMPT_TYPE_OPTIONS,
  updatePrompt,
  type PromptTemplate,
} from '../api/prompts'

const loading = ref(false)
const saving = ref(false)
const prompts = ref<PromptTemplate[]>([])
const filterType = ref<string>('')
const selectedId = ref<number | null>(null)

const form = reactive({
  name: '',
  type: 'generate',
  content: '',
  is_active: true,
})

const isCreateMode = ref(false)

const selected = computed(() =>
  prompts.value.find((p) => p.id === selectedId.value) ?? null,
)

async function load() {
  loading.value = true
  try {
    prompts.value = await listPrompts(filterType.value || undefined)
    if (selectedId.value && !prompts.value.some((p) => p.id === selectedId.value)) {
      selectedId.value = null
    }
    if (!selectedId.value && prompts.value.length > 0 && !isCreateMode.value) {
      selectPrompt(prompts.value[0])
    }
  } catch (e) {
    ElMessage.error(`加载提示词失败：${(e as Error).message}`)
  } finally {
    loading.value = false
  }
}

function selectPrompt(row: PromptTemplate) {
  isCreateMode.value = false
  selectedId.value = row.id
  form.name = row.name
  form.type = row.type
  form.content = row.content
  form.is_active = row.is_active
}

function startCreate() {
  isCreateMode.value = true
  selectedId.value = null
  form.name = ''
  form.type = filterType.value || 'generate'
  form.content = ''
  form.is_active = true
}

async function save() {
  if (!form.name.trim() || !form.content.trim()) {
    ElMessage.warning('请填写名称与内容')
    return
  }
  saving.value = true
  try {
    if (isCreateMode.value) {
      const created = await createPrompt({
        name: form.name.trim(),
        type: form.type,
        content: form.content,
        is_active: form.is_active,
      })
      ElMessage.success('已创建提示词')
      isCreateMode.value = false
      await load()
      selectPrompt(created)
    } else if (selectedId.value) {
      const updated = await updatePrompt(selectedId.value, {
        name: form.name.trim(),
        content: form.content,
        is_active: form.is_active,
      })
      ElMessage.success('已保存')
      await load()
      selectPrompt(updated)
    }
  } catch (e) {
    ElMessage.error(`保存失败：${(e as Error).message}`)
  } finally {
    saving.value = false
  }
}

async function setActive() {
  if (!selectedId.value) return
  saving.value = true
  try {
    const updated = await updatePrompt(selectedId.value, { is_active: true })
    ElMessage.success('已设为启用')
    await load()
    selectPrompt(updated)
  } catch (e) {
    ElMessage.error(`操作失败：${(e as Error).message}`)
  } finally {
    saving.value = false
  }
}

function typeLabel(type: string) {
  return PROMPT_TYPE_OPTIONS.find((o) => o.value === type)?.label ?? type
}

watch(filterType, () => {
  selectedId.value = null
  isCreateMode.value = false
  load()
})

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1>提示词管理</h1>
      <div class="header-actions">
        <el-select
          v-model="filterType"
          clearable
          placeholder="按类型筛选"
          style="width: 220px"
        >
          <el-option
            v-for="opt in PROMPT_TYPE_OPTIONS"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </el-select>
        <el-button type="primary" @click="startCreate">新建提示词</el-button>
      </div>
    </div>

    <div class="layout" v-loading="loading">
      <div class="list-panel">
        <el-table
          :data="prompts"
          highlight-current-row
          empty-text="暂无提示词"
          height="100%"
          @row-click="selectPrompt"
        >
          <el-table-column prop="name" label="名称" min-width="120" />
          <el-table-column label="类型" width="120">
            <template #default="{ row }">
              {{ row.type }}
            </template>
          </el-table-column>
          <el-table-column label="版本" prop="version" width="70" />
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag v-if="row.is_active" type="success" size="small">启用</el-tag>
              <el-tag v-else type="info" size="small">停用</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div class="editor-panel">
        <template v-if="isCreateMode || selected">
          <div class="editor-title">
            <span>{{ isCreateMode ? '新建提示词' : `编辑：${selected?.name}` }}</span>
            <span v-if="selected" class="meta">
              v{{ selected.version }} · {{ typeLabel(selected.type) }}
            </span>
          </div>
          <el-form label-width="80px">
            <el-form-item label="名称" required>
              <el-input v-model="form.name" />
            </el-form-item>
            <el-form-item label="类型" required>
              <el-select v-model="form.type" :disabled="!isCreateMode" style="width: 100%">
                <el-option
                  v-for="opt in PROMPT_TYPE_OPTIONS"
                  :key="opt.value"
                  :label="opt.label"
                  :value="opt.value"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="内容" required>
              <el-input
                v-model="form.content"
                type="textarea"
                :rows="16"
                placeholder="提示词正文（支持 Markdown）"
              />
            </el-form-item>
            <el-form-item label="启用">
              <el-switch v-model="form.is_active" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="saving" @click="save">保存</el-button>
              <el-button
                v-if="selected && !selected.is_active"
                :loading="saving"
                @click="setActive"
              >
                设为启用
              </el-button>
            </el-form-item>
          </el-form>
        </template>
        <el-empty v-else description="选择左侧提示词，或新建一条" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  gap: 12px;
  flex-wrap: wrap;
}

.page-header h1 {
  margin: 0;
}

.header-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}

.layout {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) minmax(360px, 1.4fr);
  gap: 16px;
  min-height: 520px;
}

.list-panel,
.editor-panel {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px;
  background: #fafafa;
  min-height: 480px;
}

.editor-panel {
  background: #fff;
}

.editor-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  font-weight: 600;
}

.meta {
  color: #909399;
  font-size: 13px;
  font-weight: 400;
}

@media (max-width: 960px) {
  .layout {
    grid-template-columns: 1fr;
  }
}
</style>
