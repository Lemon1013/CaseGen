import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { useRouter } from 'vue-router'
import {
  changePassword as changePasswordRequest,
  getAuthBootstrap,
  login,
  logout,
  register as registerAccount,
  setupAccount,
  type AuthUser,
  type RegisterPayload,
} from './api/auth'
import { ApiError, bumpAuthEpoch, getAuthEpoch } from './api/client'
import { resetProjectStoreState } from './projectStore'
import { useAgentChatStore } from './stores/agentChatStore'

function safeRedirect(value: unknown): string {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return '/'
  if (value.startsWith('/login') || value.startsWith('/setup') || value.startsWith('/register')) return '/'
  return value
}

export const useAuthStore = defineStore('auth', () => {
  const router = useRouter()
  const user = ref<AuthUser | null>(null)
  const setupRequired = ref(false)
  const registrationEnabled = ref(false)
  const restored = ref(false)
  const restorePending = ref(false)
  const restoreError = ref<string | null>(null)
  const restoreUnavailable = ref(false)
  let restoreFlight: Promise<void> | null = null
  let restoreGeneration = 0
  let unauthorizedFlight: Promise<void> | null = null

  const isAuthenticated = computed(() => user.value !== null)
  const isAdmin = computed(() => user.value?.role === 'admin')

  // Clear every bit of the previous account's client-side scope so a later
  // login cannot inherit its project_id (client.ts injects it into every
  // /api request) or its UI state.
  function clearAccountScope() {
    for (const key of [
      'casegen:last-project-id',
      'casegen:last-wiki-space-id',
      'casegen:nav-collapsed',
      'casegen:agent-list-collapsed',
    ]) localStorage.removeItem(key)
    resetProjectStoreState()
    useAgentChatStore().dispose()
  }

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
        registrationEnabled.value = payload.setup_required ? false : payload.registration_enabled === true
        user.value = payload.user
      })
      .catch(() => {
        if (!isCurrent()) return
        user.value = null
        registrationEnabled.value = false
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

  async function register(payload: RegisterPayload) {
    invalidateRestore()
    bumpAuthEpoch()
    const result = await registerAccount(payload)
    user.value = result.user
    setupRequired.value = false
    restored.value = true
    await router.replace({ name: 'workbench' })
  }

  async function changePassword(currentPassword: string, newPassword: string) {
    try {
      await changePasswordRequest({
        current_password: currentPassword,
        new_password: newPassword,
      })
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        await handleUnauthorized()
        return
      }
      throw err
    }
    invalidateRestore()
    bumpAuthEpoch()
    clearAccountScope()
    user.value = null
    restored.value = false
    await router.replace({ name: 'login', query: { changed: '1' } })
  }

  async function signOut() {
    invalidateRestore()
    bumpAuthEpoch()
    try {
      await logout()
    } finally {
      clearAccountScope()
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
      clearAccountScope()
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
    registrationEnabled,
    restored,
    restorePending,
    restoreError,
    restoreUnavailable,
    isAuthenticated,
    isAdmin,
    restoreSession,
    retryRestore,
    signIn,
    setup,
    register,
    changePassword,
    signOut,
    handleUnauthorized,
  }
})
