<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createUser,
  listUsers,
  resetUserPassword,
  updateUser,
  type UserItem,
} from '../api/users'

const loading = ref(false)
const saving = ref(false)
const users = ref<UserItem[]>([])

const createDialog = ref(false)
const createForm = reactive({
  username: '',
  display_name: '',
  password: '',
  role: 'user' as 'admin' | 'user',
})

const roleDialog = ref(false)
const roleTarget = ref<UserItem | null>(null)
const roleForm = reactive({ role: 'user' as 'admin' | 'user', display_name: '' })

const resetDialog = ref(false)
const resetTarget = ref<UserItem | null>(null)
const resetForm = reactive({ password: '' })

async function load() {
  loading.value = true
  try {
    users.value = await listUsers()
  } catch (e) {
    ElMessage.error(`加载用户失败：${(e as Error).message}`)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  createForm.username = ''
  createForm.display_name = ''
  createForm.password = ''
  createForm.role = 'user'
  createDialog.value = true
}

async function submitCreate() {
  if (!createForm.username.trim() || !createForm.password) {
    ElMessage.warning('请填写用户名与初始口令')
    return
  }
  saving.value = true
  try {
    await createUser({
      username: createForm.username.trim(),
      display_name: createForm.display_name.trim(),
      password: createForm.password,
      role: createForm.role,
    })
    ElMessage.success('用户已创建')
    createDialog.value = false
    await load()
  } catch (e) {
    ElMessage.error(`创建失败：${(e as Error).message}`)
  } finally {
    saving.value = false
  }
}

function openRole(row: UserItem) {
  roleTarget.value = row
  roleForm.role = row.role
  roleForm.display_name = row.display_name
  roleDialog.value = true
}

async function submitRole() {
  if (!roleTarget.value) return
  saving.value = true
  try {
    await updateUser(roleTarget.value.id, {
      role: roleForm.role,
      display_name: roleForm.display_name.trim(),
    })
    ElMessage.success('用户资料已更新')
    roleDialog.value = false
    await load()
  } catch (e) {
    ElMessage.error(`更新失败：${(e as Error).message}`)
  } finally {
    saving.value = false
  }
}

async function toggleActive(row: UserItem) {
  const next = !row.is_active
  try {
    await ElMessageBox.confirm(
      next ? `启用用户「${row.username}」？` : `停用用户「${row.username}」？其现有会话会立即失效。`,
      next ? '启用用户' : '停用用户',
      { type: 'warning', confirmButtonText: '确认' },
    )
  } catch {
    return
  }
  try {
    await updateUser(row.id, { is_active: next })
    ElMessage.success(next ? '用户已启用' : '用户已停用')
    await load()
  } catch (e) {
    ElMessage.error(`操作失败：${(e as Error).message}`)
  }
}

function openReset(row: UserItem) {
  resetTarget.value = row
  resetForm.password = ''
  resetDialog.value = true
}

async function submitReset() {
  if (!resetTarget.value || !resetForm.password) {
    ElMessage.warning('请填写新口令')
    return
  }
  saving.value = true
  try {
    await resetUserPassword(resetTarget.value.id, resetForm.password)
    ElMessage.success('口令已重置，该用户的旧会话已失效')
    resetDialog.value = false
    await load()
  } catch (e) {
    ElMessage.error(`重置失败：${(e as Error).message}`)
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1 class="page-title">用户管理</h1>
        <p class="page-subtitle">创建本地账号、调整角色与状态，或重置登录口令。</p>
      </div>
      <div class="actions">
        <el-button @click="load">刷新</el-button>
        <el-button type="primary" @click="openCreate">新建用户</el-button>
      </div>
    </div>

    <el-card shadow="never">
      <el-table v-loading="loading" :data="users" row-key="id">
        <el-table-column prop="username" label="用户名" min-width="140" />
        <el-table-column prop="display_name" label="显示名" min-width="140" />
        <el-table-column label="角色" width="110">
          <template #default="{ row }">
            <el-tag :type="row.role === 'admin' ? 'danger' : 'info'">
              {{ row.role === 'admin' ? '管理员' : '普通用户' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'">
              {{ row.is_active ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openRole(row)">编辑 / 改角色</el-button>
            <el-button link @click="openReset(row)">重置口令</el-button>
            <el-button link :type="row.is_active ? 'danger' : 'success'" @click="toggleActive(row)">
              {{ row.is_active ? '停用' : '启用' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="createDialog" title="新建用户" width="480px">
      <el-form label-position="top">
        <el-form-item label="用户名" required>
          <el-input v-model="createForm.username" placeholder="小写字母开头，3-32 位" />
        </el-form-item>
        <el-form-item label="显示名">
          <el-input v-model="createForm.display_name" />
        </el-form-item>
        <el-form-item label="初始口令" required>
          <el-input v-model="createForm.password" type="password" show-password placeholder="至少 10 位" />
        </el-form-item>
        <el-form-item label="角色">
          <el-radio-group v-model="createForm.role">
            <el-radio value="user">普通用户</el-radio>
            <el-radio value="admin">管理员</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="roleDialog" title="编辑用户" width="480px">
      <el-form label-position="top">
        <el-form-item label="显示名">
          <el-input v-model="roleForm.display_name" />
        </el-form-item>
        <el-form-item label="角色">
          <el-radio-group v-model="roleForm.role">
            <el-radio value="user">普通用户</el-radio>
            <el-radio value="admin">管理员</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="roleDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitRole">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="resetDialog" :title="`重置口令 · ${resetTarget?.username || ''}`" width="440px">
      <el-form label-position="top">
        <el-form-item label="新口令" required>
          <el-input v-model="resetForm.password" type="password" show-password placeholder="至少 10 位" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resetDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitReset">重置</el-button>
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
