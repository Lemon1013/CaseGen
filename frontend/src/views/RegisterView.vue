<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import { ApiError } from '../api/client'
import { useAuthStore } from '../authStore'

const auth = useAuthStore()
const router = useRouter()
const username = ref('')
const displayName = ref('')
const password = ref('')
const confirmPassword = ref('')
const loading = ref(false)

const usernamePattern = /^[a-z][a-z0-9._-]{2,31}$/

function goLogin() {
  void router.push({ name: 'login' })
}

async function submit() {
  if (!auth.registrationEnabled) return
  const name = username.value.trim().toLowerCase()
  if (!usernamePattern.test(name)) {
    ElMessage.warning('用户名需为 3-32 位，以字母开头，仅可包含小写字母、数字、.、_ 或 -')
    return
  }
  if (password.value.length < 10) {
    ElMessage.warning('密码至少 10 位')
    return
  }
  if (password.value !== confirmPassword.value) {
    ElMessage.warning('两次输入的密码不一致')
    return
  }
  loading.value = true
  try {
    await auth.register({
      username: name,
      display_name: displayName.value.trim(),
      password: password.value,
    })
    ElMessage.success('注册成功')
  } catch (err) {
    if (err instanceof ApiError && err.status === 403 && err.detail === '公开注册已关闭') {
      auth.registrationEnabled = false
      ElMessage.warning('公开注册已关闭，请联系管理员创建账号')
      return
    }
    ElMessage.error((err as Error).message || '注册失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="auth-page">
    <el-card class="auth-card" shadow="never">
      <div class="auth-brand">CaseGen</div>
      <h1>注册账号</h1>
      <p class="auth-hint">注册后为普通用户，可参与项目中的用例与任务。</p>

      <el-alert
        v-if="!auth.registrationEnabled"
        type="warning"
        :closable="false"
        title="公开注册已关闭"
        style="margin-bottom: 18px"
      >
        <template #default>
          <p class="closed-hint">请联系管理员创建账号。</p>
          <el-button link type="primary" @click="goLogin">返回登录</el-button>
        </template>
      </el-alert>

      <el-form v-else label-position="top" @submit.prevent="submit">
        <el-form-item label="用户名">
          <el-input v-model="username" maxlength="32" autocomplete="username" placeholder="小写字母开头，3-32 位" @keyup.enter="submit" />
        </el-form-item>
        <el-form-item label="显示名（可选）">
          <el-input v-model="displayName" maxlength="80" autocomplete="name" placeholder="用于界面展示" @keyup.enter="submit" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="password" type="password" maxlength="1024" show-password autocomplete="new-password" placeholder="至少 10 位" @keyup.enter="submit" />
        </el-form-item>
        <el-form-item label="确认密码">
          <el-input v-model="confirmPassword" type="password" maxlength="1024" show-password autocomplete="new-password" @keyup.enter="submit" />
        </el-form-item>
        <el-button class="auth-submit" type="primary" :loading="loading" @click="submit">注册</el-button>
      </el-form>

      <p class="auth-footer">
        已有账号？<router-link :to="{ name: 'login' }">去登录</router-link>
      </p>
    </el-card>
  </main>
</template>

<style scoped>
.auth-page { min-height: 100vh; display: grid; place-items: center; padding: 24px; background: var(--cg-bg); }
.auth-card { width: min(460px, 100%); border-radius: var(--cg-radius-lg); }
.auth-brand { color: var(--cg-primary); font-weight: 800; letter-spacing: .04em; }
h1 { margin: 18px 0 6px; font-size: 28px; }
.auth-hint { margin: 0 0 22px; color: var(--cg-text-muted); font-size: 13px; }
.auth-submit { width: 100%; margin-top: 8px; }
.auth-footer { margin: 16px 0 0; text-align: center; color: var(--cg-text-muted); font-size: 13px; }
.auth-footer a { color: var(--cg-primary); font-weight: 600; text-decoration: none; }
.auth-footer a:hover { text-decoration: underline; }
.closed-hint { margin: 0 0 6px; font-size: 13px; }
</style>
