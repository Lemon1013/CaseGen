<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../authStore'

const auth = useAuthStore()
const router = useRouter()
const username = ref('admin')
const displayName = ref('管理员')
const password = ref('')
const confirmPassword = ref('')
const loading = ref(false)

async function retryService() {
  await auth.retryRestore()
  if (!auth.setupRequired && !auth.restoreUnavailable) await router.replace({ name: 'login' })
}

async function submit() {
  if (!username.value.trim() || password.value.length < 10) {
    ElMessage.warning('用户名不能为空，密码至少 10 位')
    return
  }
  if (password.value !== confirmPassword.value) {
    ElMessage.warning('两次输入的密码不一致')
    return
  }
  loading.value = true
  try {
    await auth.setup(username.value, displayName.value, password.value)
  } catch (err) {
    ElMessage.error((err as Error).message || '初始化失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="auth-page">
    <el-card class="auth-card" shadow="never">
      <div class="auth-brand">CaseGen</div>
      <h1>初始化管理员</h1>
      <p class="auth-hint">首次使用请创建本地管理员账号。密码只以不可逆哈希保存。</p>
      <el-alert
        v-if="auth.restoreUnavailable"
        type="error"
        :closable="false"
        title="服务暂不可用"
        :description="auth.restoreError || '请检查后端服务后重试'"
        style="margin-bottom: 18px"
      >
        <template #default>
          <el-button link type="primary" @click="retryService">重试连接</el-button>
        </template>
      </el-alert>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="用户名"><el-input v-model="username" autocomplete="username" /></el-form-item>
        <el-form-item label="显示名称"><el-input v-model="displayName" autocomplete="name" /></el-form-item>
        <el-form-item label="密码"><el-input v-model="password" type="password" show-password autocomplete="new-password" /></el-form-item>
        <el-form-item label="确认密码"><el-input v-model="confirmPassword" type="password" show-password autocomplete="new-password" @keyup.enter="submit" /></el-form-item>
        <el-button class="auth-submit" type="primary" :loading="loading" @click="submit">完成初始化</el-button>
      </el-form>
    </el-card>
  </main>
</template>

<style scoped>
.auth-page { min-height: 100vh; display: grid; place-items: center; padding: 24px; background: var(--cg-bg-glow); }
.auth-card { width: min(460px, 100%); border-radius: 16px; }
.auth-brand { color: var(--cg-primary); font-weight: 800; letter-spacing: .04em; }
h1 { margin: 18px 0 6px; font-size: 28px; }
.auth-hint { margin: 0 0 22px; color: var(--cg-text-muted); font-size: 13px; }
.auth-submit { width: 100%; margin-top: 8px; }
</style>
