<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../authStore'
import {
  createWikiSpace,
  listWikiSpaces,
  updateWikiSpaceStatus,
  updateWikiSpace,
  type WikiSpace,
  type WikiSpaceStatus,
} from '../api/wikiSpaces'
import { previewArchivedWikiPurge, purgeArchivedWiki, type WikiPurgePreview } from '../api/wiki'

const spaces = ref<WikiSpace[]>([])
const loading = ref(false)
const dialogVisible = ref(false)
const editing = ref<WikiSpace | null>(null)
const form = reactive({ name: '', slug: '', description: '' })
const statusFilter = ref<'all' | WikiSpaceStatus>('all')
const statusChangingId = ref<number | null>(null)
const auth = useAuthStore()
const purgeDialogVisible = ref(false)
const purgePreview = ref<WikiPurgePreview | null>(null)
const purgeLoading = ref(false)
const purgeExecuting = ref(false)
const purgeConfirmation = ref('')
const activeCount = computed(() => spaces.value.filter((space) => space.status === 'active').length)
const archivedCount = computed(() => spaces.value.filter((space) => space.status === 'archived').length)
const visibleSpaces = computed(() =>
  statusFilter.value === 'all'
    ? spaces.value
    : spaces.value.filter((space) => space.status === statusFilter.value),
)

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

async function openPurge() {
  purgeLoading.value = true
  try {
    purgePreview.value = await previewArchivedWikiPurge()
    purgeConfirmation.value = ''
    purgeDialogVisible.value = true
  } catch (error) {
    ElMessage.error(`预览归档清理失败：${(error as Error).message}`)
  } finally {
    purgeLoading.value = false
  }
}

async function executePurge() {
  const preview = purgePreview.value
  if (
    !preview ||
    preview.totals.pages === 0 ||
    preview.unsafe.length > 0 ||
    preview.active_jobs.length > 0 ||
    purgeConfirmation.value !== preview.confirmation_text
  ) return
  purgeExecuting.value = true
  try {
    const result = await purgeArchivedWiki({ scope: preview.scope, plan_hash: preview.plan_hash, confirmation_text: purgeConfirmation.value })
    ElMessage.success(`已清理 ${result.counts.pages} 个归档页面`)
    purgeDialogVisible.value = false
    await load()
  } catch (error) {
    const message = (error as Error).message
    ElMessage.error(message.includes('409') ? '清理计划已变化或存在活动任务，请重新预览后再试' : `归档清理失败：${message}`)
  } finally {
    purgeExecuting.value = false
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

async function changeStatus(space: WikiSpace, nextStatus: WikiSpaceStatus) {
  const isArchiving = nextStatus === 'archived'
  try {
    await ElMessageBox.confirm(
      isArchiving
        ? `归档「${space.name}」后将不能上传、摄入或创建新任务，但历史内容仍可读取。继续吗？`
        : `恢复「${space.name}」后将重新允许上传、摄入和创建任务。继续吗？`,
      isArchiving ? '确认归档' : '确认恢复',
      {
        type: isArchiving ? 'warning' : 'info',
        confirmButtonText: isArchiving ? '确认归档' : '恢复使用',
        cancelButtonText: '取消',
      },
    )
    statusChangingId.value = space.id
    await updateWikiSpaceStatus(space.id, nextStatus)
    ElMessage.success(isArchiving ? '空间已归档' : '空间已恢复使用')
    await load()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(`${isArchiving ? '归档' : '恢复'}失败：${(error as Error).message}`)
    }
  } finally {
    statusChangingId.value = null
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-header space-header">
      <div>
        <h1 class="page-title">知识空间</h1>
        <p class="page-subtitle">每个项目拥有独立的文档、Wiki 页面、审核和检索边界。</p>
      </div>
      <div class="header-actions">
        <el-tag type="info">活动空间 {{ activeCount }}</el-tag>
        <el-tag type="info">归档空间 {{ archivedCount }}</el-tag>
        <el-select v-model="statusFilter" style="width: 130px" aria-label="空间状态筛选">
          <el-option label="全部状态" value="all" />
          <el-option label="活动" value="active" />
          <el-option label="已归档" value="archived" />
        </el-select>
        <el-button type="primary" @click="openCreate">新建空间</el-button>
        <el-button v-if="auth.user?.role === 'admin'" type="danger" :loading="purgeLoading" @click="openPurge">清理归档 Wiki</el-button>
      </div>
    </div>

    <el-card shadow="never" class="space-card">
      <el-table v-loading="loading" :data="visibleSpaces" row-key="id">
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
            <el-tag v-if="row.slug === 'default'" type="info" effect="plain" class="default-tag">
              系统默认
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.status === 'active'" link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button
              v-if="row.status === 'active' && row.slug !== 'default'"
              link
              type="warning"
              :loading="statusChangingId === row.id"
              @click="changeStatus(row, 'archived')"
            >
              归档
            </el-button>
            <el-button
              v-if="row.status === 'archived'"
              link
              type="success"
              :loading="statusChangingId === row.id"
              @click="changeStatus(row, 'active')"
            >
              恢复使用
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

    <el-dialog v-model="purgeDialogVisible" title="清理所有归档 Wiki 内容" width="680px" :close-on-click-modal="!purgeExecuting" :close-on-press-escape="!purgeExecuting" :show-close="!purgeExecuting">
      <template v-if="purgePreview">
        <el-alert v-if="purgePreview.warnings.length" type="warning" :closable="false" title="执行前检查">
          <div v-for="warning in purgePreview.warnings" :key="warning">{{ warning }}</div>
        </el-alert>
        <el-alert v-if="purgePreview.unsafe.length || purgePreview.active_jobs.length" type="error" :closable="false" title="当前计划不可执行">
          <div v-if="purgePreview.unsafe.length">存在 {{ purgePreview.unsafe.length }} 个不安全路径，已阻止删除。</div>
          <div v-if="purgePreview.active_jobs.length">存在活动摄入任务（{{ purgePreview.active_jobs.length }} 个），请等待任务结束后刷新预览。</div>
        </el-alert>
        <el-table :data="purgePreview.spaces" size="small" class="purge-table">
          <el-table-column prop="space_name" label="空间" />
          <el-table-column prop="pages" label="页面" />
          <el-table-column prop="revisions" label="修订" />
          <el-table-column prop="page_sources" label="来源" />
          <el-table-column prop="reviews" label="审核" />
          <el-table-column prop="files" label="文件" />
        </el-table>
        <p>总计：{{ purgePreview.totals.pages }} 页面，{{ purgePreview.totals.files }} 个 Markdown 文件</p>
        <el-input v-model="purgeConfirmation" :disabled="purgeExecuting" placeholder="请输入服务端 confirmation_text" />
      </template>
      <template #footer>
        <el-button :disabled="purgeExecuting" @click="purgeDialogVisible = false">取消</el-button>
        <el-button type="danger" :loading="purgeExecuting" :disabled="!purgePreview || purgePreview.totals.pages === 0 || purgePreview.unsafe.length > 0 || purgePreview.active_jobs.length > 0 || purgeConfirmation !== purgePreview.confirmation_text" @click="executePurge">永久清理</el-button>
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
.default-tag { margin-left: 6px; }
</style>
