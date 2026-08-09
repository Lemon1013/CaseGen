<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  archiveWikiSpace,
  createWikiSpace,
  listWikiSpaces,
  updateWikiSpace,
  type WikiSpace,
} from '../api/wikiSpaces'

const spaces = ref<WikiSpace[]>([])
const loading = ref(false)
const dialogVisible = ref(false)
const editing = ref<WikiSpace | null>(null)
const form = reactive({ name: '', slug: '', description: '' })
const activeCount = computed(() => spaces.value.filter((space) => space.status === 'active').length)

async function load() {
  loading.value = true
  try {
    spaces.value = await listWikiSpaces()
  } catch (error) {
    ElMessage.error(`加载 Wiki 空间失败：${(error as Error).message}`)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  Object.assign(form, { name: '', slug: '', description: '' })
  dialogVisible.value = true
}

function openEdit(space: WikiSpace) {
  editing.value = space
  Object.assign(form, { name: space.name, slug: space.slug, description: space.description })
  dialogVisible.value = true
}

async function save() {
  if (!form.name.trim()) {
    ElMessage.warning('请填写空间名称')
    return
  }
  try {
    if (editing.value) {
      await updateWikiSpace(editing.value.id, {
        name: form.name.trim(),
        description: form.description.trim(),
      })
      ElMessage.success('空间已更新')
    } else {
      await createWikiSpace({
        name: form.name.trim(),
        slug: form.slug.trim() || undefined,
        description: form.description.trim(),
      })
      ElMessage.success('空间已创建')
    }
    dialogVisible.value = false
    await load()
  } catch (error) {
    ElMessage.error(`保存空间失败：${(error as Error).message}`)
  }
}

async function archive(space: WikiSpace) {
  try {
    await ElMessageBox.confirm(
      `归档「${space.name}」后将不能上传、摄入或创建新任务，但历史内容仍可读取。继续吗？`,
      '确认归档',
      { type: 'warning' },
    )
    await archiveWikiSpace(space.id)
    ElMessage.success('空间已归档')
    await load()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(`归档失败：${(error as Error).message}`)
    }
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-header space-header">
      <div>
        <h1 class="page-title">Wiki 空间</h1>
        <p class="page-subtitle">每个项目拥有独立的文档、Wiki 页面、审核和检索边界。</p>
      </div>
      <div class="header-actions">
        <el-tag type="info">活动空间 {{ activeCount }}</el-tag>
        <el-button type="primary" @click="openCreate">新建空间</el-button>
      </div>
    </div>

    <el-card shadow="never" class="space-card">
      <el-table v-loading="loading" :data="spaces" row-key="id">
        <el-table-column label="空间" min-width="220">
          <template #default="{ row }">
            <div class="space-name">{{ row.name }}</div>
            <div class="space-slug">/{{ row.slug }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="说明" min-width="260" show-overflow-tooltip />
        <el-table-column label="统计" width="270">
          <template #default="{ row }">
            <span>{{ row.document_count }} 文档</span>
            <span class="stat-gap">{{ row.page_count }} 页面</span>
            <span class="stat-gap">{{ row.pending_review_count }} 待审核</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'active' ? 'success' : 'info'">
              {{ row.status === 'active' ? '活动' : '已归档' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button
              v-if="row.status === 'active' && row.slug !== 'default'"
              link
              type="warning"
              @click="archive(row)"
            >
              归档
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑 Wiki 空间' : '新建 Wiki 空间'" width="520px">
      <el-form label-width="80px" @submit.prevent>
        <el-form-item label="名称" required>
          <el-input v-model="form.name" maxlength="120" placeholder="例如：交易规则项目" />
        </el-form-item>
        <el-form-item v-if="!editing" label="Slug">
          <el-input v-model="form.slug" maxlength="64" placeholder="留空则根据名称生成" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" type="textarea" :rows="4" maxlength="2000" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.space-header { align-items: center; }
.header-actions { display: flex; align-items: center; gap: 12px; }
.space-card { border-radius: 14px; }
.space-name { font-weight: 650; color: var(--cg-text); }
.space-slug { color: var(--cg-text-muted); font-size: 12px; margin-top: 3px; }
.stat-gap { margin-left: 14px; }
</style>
