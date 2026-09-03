<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { createProject, deleteProject, updateProject, type Project } from '../api/projects'
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

function openForm(row?: Project) {
  editing.value = row || null
  Object.assign(form, row ? { name: row.name, slug: row.slug, description: row.description } : { name: '', slug: '', description: '' })
  dialog.value = true
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
        <el-table-column v-if="isAdmin" label="操作" width="230" fixed="right"><template #default="{ row }"><el-button link @click="openForm(row)">编辑</el-button><el-button link @click="toggleStatus(row)">{{ row.status === 'active' ? '归档' : '恢复' }}</el-button><el-button link type="danger" @click="remove(row)">删除</el-button></template></el-table-column>
      </el-table>
    </el-card>
    <el-dialog v-model="dialog" :title="editing ? '编辑空间' : '新建空间'" width="520px">
      <el-form label-position="top"><el-form-item label="名称"><el-input v-model="form.name" /></el-form-item><el-form-item label="Slug"><el-input v-model="form.slug" :disabled="!!editing" placeholder="可留空自动生成" /></el-form-item><el-form-item label="说明"><el-input v-model="form.description" type="textarea" :rows="4" /></el-form-item></el-form>
      <template #footer><el-button @click="dialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>.actions{display:flex;gap:10px}</style>
