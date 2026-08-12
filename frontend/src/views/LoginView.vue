<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../authStore'

const auth = useAuthStore()
const router = useRouter()
const username = ref('')
const password = ref('')
const loading = ref(false)

async function retryService() {
  await auth.retryRestore()
  if (auth.setupRequired) await router.replace({ name: 'setup' })
}

async function submit() {
  if (!username.value.trim() || !password.value) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    await auth.signIn(username.value, password.value)
  } catch (err) {
    ElMessage.error((err as Error).message || '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="auth-page">
    <el-card class="auth-card" shadow="never">
      <div class="auth-brand">CaseGen</div>
      <h1>登录</h1>
      <p class="auth-hint">登录后管理测试用例、任务和 Wiki 知识库</p>
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
        <el-form-item label="用户名">
          <el-input v-model="username" autocomplete="username" placeholder="例如 admin" @keyup.enter="submit" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="password" type="password" show-password autocomplete="current-password" @keyup.enter="submit" />
        </el-form-item>
        <el-button class="auth-submit" type="primary" :loading="loading" @click="submit">登录</el-button>
      </el-form>
    </el-card>
  </main>
</template>

<style scoped>
.auth-page { min-height: 100vh; display: grid; place-items: center; padding: 24px; background: var(--cg-bg-glow); }
.auth-card { width: min(420px, 100%); border-radius: 16px; }
.auth-brand { color: var(--cg-primary); font-weight: 800; letter-spacing: .04em; }
h1 { margin: 18px 0 6px; font-size: 28px; }
.auth-hint { margin: 0 0 22px; color: var(--cg-text-muted); font-size: 13px; }
.auth-submit { width: 100%; margin-top: 8px; }
</style>
