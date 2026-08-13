import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { useRouter } from 'vue-router'
import { getAuthBootstrap, login, logout, setupAccount, type AuthUser } from './api/auth'
import { bumpAuthEpoch, getAuthEpoch } from './api/client'

function safeRedirect(value: unknown): string {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return '/'
  if (value.startsWith('/login') || value.startsWith('/setup')) return '/'
  return value
}

export const useAuthStore = defineStore('auth', () => {
  const router = useRouter()
  const user = ref<AuthUser | null>(null)
  const setupRequired = ref(false)
  const restored = ref(false)
  const restorePending = ref(false)
  const restoreError = ref<string | null>(null)
  const restoreUnavailable = ref(false)
  let restoreFlight: Promise<void> | null = null
  let restoreGeneration = 0
  let unauthorizedFlight: Promise<void> | null = null

  const isAuthenticated = computed(() => user.value !== null)

  function invalidateRestore() {
    restoreGeneration += 1
    restoreFlight = null
    restorePending.value = false
  }

  async function restoreSession() {
    if (restored.value) return
    if (restoreFlight) return restoreFlight
    const requestEpoch = getAuthEpoch()
    const requestGeneration = ++restoreGeneration
    const isCurrent = () =>
      requestGeneration === restoreGeneration && requestEpoch === getAuthEpoch()
    restorePending.value = true
    restoreError.value = null
    restoreUnavailable.value = false
    const flight = getAuthBootstrap()
      .then((payload) => {
        if (!isCurrent()) return
        setupRequired.value = payload.setup_required
        user.value = payload.user
      })
      .catch(() => {
        if (!isCurrent()) return
        user.value = null
        restoreError.value = '无法连接到 CaseGen 服务，请检查后端是否已启动。'
        restoreUnavailable.value = true
      })
      .finally(() => {
        if (!isCurrent()) return
        restored.value = true
        restorePending.value = false
        restoreFlight = null
      })
    restoreFlight = flight
    return flight
  }

  async function retryRestore() {
    invalidateRestore()
    restored.value = false
    restoreError.value = null
    restoreUnavailable.value = false
    return restoreSession()
  }

  async function signIn(username: string, password: string) {
    invalidateRestore()
    bumpAuthEpoch()
    const payload = await login({ username, password })
    user.value = payload.user
    setupRequired.value = false
    restored.value = true
    await router.replace(safeRedirect(router.currentRoute.value.query.redirect))
  }

  async function setup(username: string, displayName: string, password: string) {
    invalidateRestore()
    bumpAuthEpoch()
    const payload = await setupAccount({ username, display_name: displayName, password })
    user.value = payload.user
    setupRequired.value = false
    restored.value = true
    await router.replace(safeRedirect(router.currentRoute.value.query.redirect))
  }

  async function signOut() {
    invalidateRestore()
    bumpAuthEpoch()
    try {
      await logout()
    } finally {
      user.value = null
      restored.value = true
      await router.replace({ name: 'login' })
    }
  }

  async function handleUnauthorized(requestEpoch?: number) {
    if (requestEpoch !== undefined && requestEpoch !== getAuthEpoch()) return
    if (unauthorizedFlight) return unauthorizedFlight
    unauthorizedFlight = (async () => {
      invalidateRestore()
      bumpAuthEpoch()
      user.value = null
      restored.value = true
      const routeName = router.currentRoute.value.name
      if (routeName !== 'login' && routeName !== 'setup') {
        const redirect = safeRedirect(router.currentRoute.value.fullPath)
        await router.replace({ name: 'login', query: redirect === '/' ? undefined : { redirect } })
      }
    })().finally(() => {
      unauthorizedFlight = null
    })
    return unauthorizedFlight
  }

  return {
    user,
    setupRequired,
    restored,
    restorePending,
    restoreError,
    restoreUnavailable,
    isAuthenticated,
    restoreSession,
    retryRestore,
    signIn,
    setup,
    signOut,
    handleUnauthorized,
  }
})
