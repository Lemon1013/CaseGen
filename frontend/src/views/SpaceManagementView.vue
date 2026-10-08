<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { createProject, deleteProject, updateProject, type Project } from '../api/projects'
import {
  getExternalWikiConfig,
  saveExternalWikiConfig,
  testExternalWikiConnection,
  type ExternalWikiConfig,
  type ExternalWikiProjectItem,
} from '../api/externalWiki'
import { useAuthStore } from '../authStore'
import { useProjectStore } from '../projectStore'

const router = useRouter()
const auth = useAuthStore()
const projects = useProjectStore()
const isAdmin = computed(() => auth.user?.role === 'admin')
const dialog = ref(false)
const editing = ref<Project | null>(null)
const saving = ref(false)
const form = reactive({ name: '', slug: '', description: '' })

// External wiki dialog states
const wikiDialog = ref(false)
const currentWikiProject = ref<Project | null>(null)
const wikiLoading = ref(false)
const wikiSaving = ref(false)
const testingConnection = ref(false)
const externalProjectsList = ref<ExternalWikiProjectItem[]>([])
const wikiForm = reactive<ExternalWikiConfig>({
  project_id: 0,
  name: '外部LLM知识库',
  base_url: 'http://127.0.0.1:8091',
  external_project_id: '',
  external_project_name: '',
  top_k: 6,
  timeout_sec: 3.0,
  weight: 1.0,
  use_synonyms: true,
  enabled: true,
})

function openForm(row?: Project) {
  editing.value = row || null
  Object.assign(form, row ? { name: row.name, slug: row.slug, description: row.description } : { name: '', slug: '', description: '' })
  dialog.value = true
}

async function openExternalWiki(row: Project) {
  currentWikiProject.value = row
  wikiDialog.value = true
  wikiLoading.value = true
  externalProjectsList.value = []
  try {
    const cfg = await getExternalWikiConfig(row.id)
    Object.assign(wikiForm, cfg)
    if (wikiForm.external_project_id && wikiForm.external_project_name) {
      externalProjectsList.value = [
        { id: wikiForm.external_project_id, name: wikiForm.external_project_name },
      ]
    }
    if (wikiForm.base_url) {
      void runTestConnection(false)
    }
  } catch (err) {
    ElMessage.error(`加载外部知识库配置失败：${(err as Error).message}`)
  } finally {
    wikiLoading.value = false
  }
}

async function runTestConnection(showMessage = true) {
  if (!currentWikiProject.value) return
  testingConnection.value = true
  try {
    const res = await testExternalWikiConnection(currentWikiProject.value.id, {
      base_url: wikiForm.base_url.trim(),
      timeout_sec: wikiForm.timeout_sec,
    })
    if (res.success) {
      externalProjectsList.value = res.projects
      if (showMessage) {
        ElMessage.success(res.message || '连接外部知识库成功')
      }
    } else {
      if (showMessage) {
        ElMessage.warning(res.message || '连接失败')
      }
    }
  } catch (err) {
    if (showMessage) {
      ElMessage.error(`连接测试失败: ${(err as Error).message}`)
    }
  } finally {
    testingConnection.value = false
  }
}

function onExternalProjectChange(val: string) {
  const selected = externalProjectsList.value.find((p) => p.id === val)
  if (selected) {
    wikiForm.external_project_name = selected.name
  }
}

async function saveWikiConfig() {
  if (!currentWikiProject.value) return
  wikiSaving.value = true
  try {
    const saved = await saveExternalWikiConfig(currentWikiProject.value.id, {
      name: wikiForm.name.trim() || '外部LLM知识库',
      base_url: wikiForm.base_url.trim(),
      external_project_id: wikiForm.external_project_id.trim(),
      external_project_name: wikiForm.external_project_name.trim(),
      top_k: wikiForm.top_k,
      timeout_sec: wikiForm.timeout_sec,
      weight: wikiForm.weight,
      use_synonyms: wikiForm.use_synonyms,
      enabled: wikiForm.enabled,
    })
    Object.assign(wikiForm, saved)
    wikiDialog.value = false
    ElMessage.success('外部知识库配置已保存')
  } catch (err) {
    ElMessage.error(`保存失败：${(err as Error).message}`)
  } finally {
    wikiSaving.value = false
  }
}

async function refresh() {
  try { await projects.refresh(router) } catch (error) { ElMessage.error((error as Error).message) }
}

async function save() {
  if (!form.name.trim()) return
  saving.value = true
  try {
    if (editing.value) await updateProject(editing.value.id, { name: form.name.trim(), description: form.description.trim() })
    else await createProject({ name: form.name.trim(), slug: form.slug.trim() || undefined, description: form.description.trim() })
    dialog.value = false
    await refresh()
    ElMessage.success('空间已保存')
  } catch (error) { ElMessage.error((error as Error).message) } finally { saving.value = false }
}

async function toggleStatus(row: Project) {
  const next = row.status === 'active' ? 'archived' : 'active'
  try {
    await updateProject(row.id, { status: next })
    await refresh()
    ElMessage.success(next === 'active' ? '空间已恢复' : '空间已归档')
  } catch (error) { ElMessage.error((error as Error).message) }
}

async function remove(row: Project) {
  try {
    await ElMessageBox.confirm(`永久删除空空间「${row.name}」？此操作不可撤销。`, '删除空间', { type: 'warning', confirmButtonText: '删除' })
    await deleteProject(row.id)
    await refresh()
    ElMessage.success('空间已删除')
  } catch (error) { if (error !== 'cancel') ElMessage.error((error as Error).message) }
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div><h1 class="page-title">空间管理</h1><p class="page-subtitle">空间是全站项目边界；知识库绑定请前往“知识库管理”。</p></div>
      <div class="actions"><el-button @click="refresh">刷新</el-button><el-button v-if="isAdmin" type="primary" @click="openForm()">新建空间</el-button></div>
    </div>
    <el-card shadow="never">
      <el-table :data="projects.state.projects" row-key="id">
        <el-table-column prop="name" label="名称" min-width="150" />
        <el-table-column prop="slug" label="Slug" min-width="130" />
        <el-table-column prop="description" label="说明" min-width="200" show-overflow-tooltip />
        <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.status === 'active' ? 'success' : 'info'">{{ row.status === 'active' ? '使用中' : '已归档' }}</el-tag></template></el-table-column>
        <el-table-column label="默认私有知识库" min-width="160"><template #default="{ row }">{{ row.private_spaces.find((item: Project['private_spaces'][number]) => item.id === row.default_wiki_space_id)?.name || '未设置' }}</template></el-table-column>
        <el-table-column label="公共库" width="90"><template #default="{ row }">{{ row.shared_spaces.filter((item: Project['shared_spaces'][number]) => item.enabled).length }}</template></el-table-column>
        <el-table-column v-if="isAdmin" label="操作" width="280" fixed="right"><template #default="{ row }"><el-button link @click="openForm(row)">编辑</el-button><el-button link type="primary" @click="openExternalWiki(row)">外部知识库</el-button><el-button link @click="toggleStatus(row)">{{ row.status === 'active' ? '归档' : '恢复' }}</el-button><el-button link type="danger" @click="remove(row)">删除</el-button></template></el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialog" :title="editing ? '编辑空间' : '新建空间'" width="520px">
      <el-form label-position="top"><el-form-item label="名称"><el-input v-model="form.name" /></el-form-item><el-form-item label="Slug"><el-input v-model="form.slug" :disabled="!!editing" placeholder="可留空自动生成" /></el-form-item><el-form-item label="说明"><el-input v-model="form.description" type="textarea" :rows="4" /></el-form-item></el-form>
      <template #footer><el-button @click="dialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存</el-button></template>
    </el-dialog>

    <el-dialog
      v-model="wikiDialog"
      :title="`外部知识库配置 · ${currentWikiProject?.name || ''}`"
      width="600px"
      destroy-on-close
    >
      <div v-loading="wikiLoading">
        <el-alert
          type="info"
          :closable="false"
          title="基于 docs/llm-wiki-reference 规范对接外部知识库服务"
          description="配置后，CaseGen 在用例生成的检索阶段将并行检索该外部知识库，并以 [E#] 编号注入提示词与知识引用中。"
          style="margin-bottom: 16px"
        />
        <el-form label-position="top">
          <el-form-item label="服务名称">
            <el-input v-model="wikiForm.name" placeholder="外部LLM知识库" />
          </el-form-item>
          <el-form-item label="Base URL (服务地址)">
            <div style="display: flex; gap: 8px; width: 100%">
              <el-input
                v-model="wikiForm.base_url"
                placeholder="http://127.0.0.1:8091"
                style="flex: 1"
              />
              <el-button
                type="primary"
                plain
                :loading="testingConnection"
                @click="runTestConnection(true)"
              >
                测试连接
              </el-button>
            </div>
          </el-form-item>
          <el-form-item label="关联外部知识项目">
            <el-select
              v-model="wikiForm.external_project_id"
              placeholder="请先测试连接并选择外部项目"
              filterable
              style="width: 100%"
              @change="onExternalProjectChange"
            >
              <el-option
                v-for="proj in externalProjectsList"
                :key="proj.id"
                :label="`${proj.name} (${proj.id})`"
                :value="proj.id"
              />
            </el-select>
          </el-form-item>
          <div style="display: flex; gap: 16px">
            <el-form-item label="检索 Top-K" style="flex: 1">
              <el-input-number v-model="wikiForm.top_k" :min="1" :max="50" style="width: 100%" />
            </el-form-item>
            <el-form-item label="超时时间 (秒)" style="flex: 1">
              <el-input-number
                v-model="wikiForm.timeout_sec"
                :min="0.5"
                :max="60"
                :step="0.5"
                style="width: 100%"
              />
            </el-form-item>
            <el-form-item label="融合权重" style="flex: 1">
              <el-input-number
                v-model="wikiForm.weight"
                :min="0"
                :max="10"
                :step="0.1"
                style="width: 100%"
              />
            </el-form-item>
          </div>
          <div style="display: flex; gap: 32px; margin-top: 8px">
            <el-form-item label="同义词扩展 (useSynonyms)">
              <el-switch v-model="wikiForm.use_synonyms" />
            </el-form-item>
            <el-form-item label="启用外部知识检索">
              <el-switch v-model="wikiForm.enabled" />
            </el-form-item>
          </div>
        </el-form>
      </div>
      <template #footer>
        <el-button @click="wikiDialog = false">取消</el-button>
        <el-button type="primary" :loading="wikiSaving" @click="saveWikiConfig">保存配置</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.actions {
  display: flex;
  gap: 10px;
}
</style>
