<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../authStore'
import { PASSWORD_POLICY_HINT, passwordPolicyError } from '../utils/password'

const auth = useAuthStore()
const router = useRouter()
const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const loading = ref(false)

function goWorkbench() {
  void router.push({ name: 'workbench' })
}

async function submit() {
  if (!currentPassword.value) {
    ElMessage.warning('请输入当前密码')
    return
  }
  if (!newPassword.value) {
    ElMessage.warning('请输入新密码')
    return
  }
  const passwordError = passwordPolicyError(newPassword.value)
  if (passwordError) {
    ElMessage.warning(passwordError)
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }
  if (newPassword.value === currentPassword.value) {
    ElMessage.warning('新密码不能与当前密码相同')
    return
  }
  loading.value = true
  try {
    await auth.changePassword(currentPassword.value, newPassword.value)
    ElMessage.success('密码已修改，请重新登录')
  } catch (err) {
    ElMessage.error((err as Error).message || '修改密码失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="security-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">账号安全</h1>
        <p class="page-subtitle">当前用户名：{{ auth.user?.username || '-' }}</p>
      </div>
      <el-button @click="goWorkbench">返回工作台</el-button>
    </div>

    <el-card shadow="never" class="security-card">
      <h2 class="card-title">修改密码</h2>
      <p class="card-hint">修改后包括当前设备在内的所有会话都会退出登录，需要使用新密码重新登录。</p>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="当前密码">
          <el-input
            v-model="currentPassword"
            type="password"
            maxlength="1024"
            show-password
            autocomplete="current-password"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input
            v-model="newPassword"
            type="password"
            maxlength="1024"
            show-password
            autocomplete="new-password"
            :placeholder="PASSWORD_POLICY_HINT"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input
            v-model="confirmPassword"
            type="password"
            maxlength="1024"
            show-password
            autocomplete="new-password"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-button type="primary" :loading="loading" @click="submit">修改密码</el-button>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.security-page { max-width: 720px; }
.security-card { border-radius: var(--cg-radius-lg); }
.card-title { margin: 0 0 6px; font-size: 17px; }
.card-hint { margin: 0 0 20px; color: var(--cg-text-muted); font-size: 13px; line-height: 1.55; }
</style>
