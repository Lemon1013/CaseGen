<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Collection,
  Cpu,
  ChatDotRound,
  EditPen,
  DataAnalysis,
  Connection,
  List,
  ArrowDown,
  ArrowRight,
  UserFilled,
  SwitchButton,
  Tickets,
  SetUp,
  Upload,
  Grid,
  Expand,
  Fold,
} from '@element-plus/icons-vue'
import { listModels, MODELS_UPDATED_EVENT, type ModelConfig } from '../api/models'
import { setProjectInvalidHandler } from '../api/client'
import { useAuthStore } from '../authStore'
import BrandMark from '../components/BrandMark.vue'
import { useProjectStore } from '../projectStore'

const route = useRoute()
const router = useRouter()
const projects = useProjectStore()
const auth = useAuthStore()
const defaultModel = ref<ModelConfig | null>(null)
const projectQuery = computed(() => {
  const project = projects.current.value
  if (!project) return {}
  const requested = Number(Array.isArray(route.query.space_id) ? route.query.space_id[0] : route.query.space_id)
  const allowed = [...project.private_spaces, ...project.shared_spaces].some((space) => space.id === requested)
  const spaceId = allowed ? requested : project.default_wiki_space_id
  return { project_id: String(project.id), ...(spaceId ? { space_id: String(spaceId) } : {}) }
})

const useCaseItems = [
  { path: '/dashboard', label: '数据看板', icon: DataAnalysis, adminOnly: true },
  { path: '/', label: '用例生成', icon: Tickets },
  { path: '/tasks', label: '生成任务', icon: List },
  { path: '/agent', label: 'Agent 对话', icon: ChatDotRound },
  { path: '/cases', label: '用例管理', icon: Collection },
]

const platformItems = [
  { path: '/data-pools', label: '数据池', icon: DataAnalysis, adminOnly: true },
  { path: '/platform-management', label: '平台管理', icon: Connection, adminOnly: true },
  { path: '/platform-case-generate', label: '生成平台用例', icon: SetUp, adminOnly: true },
  { path: '/platform-cases', label: '平台用例管理', icon: Collection, adminOnly: true },
  { path: '/platform-upload', label: '上传至测试平台', icon: Upload, adminOnly: true },
]

const systemItems = [
  { path: '/spaces', label: '项目管理', icon: Grid, adminOnly: true },
  { path: '/prompts', label: '提示词管理', icon: EditPen },
  { path: '/models', label: '模型配置', icon: Cpu, adminOnly: true },
  { path: '/users', label: '用户管理', icon: UserFilled, adminOnly: true },
]

const platformPaths = [...platformItems.map((item) => item.path), '/platform-adapters']
const activePlatformPaths = computed(() => (showPlatformGroup.value ? platformPaths : []))

const platformExpanded = ref(false)

const navCollapsed = ref(localStorage.getItem('casegen:nav-collapsed') === '1')

function toggleNav() {
  navCollapsed.value = !navCollapsed.value
  localStorage.setItem('casegen:nav-collapsed', navCollapsed.value ? '1' : '0')
}

function onUserCommand(command: unknown) {
  if (command === 'security') {
    void router.push({ name: 'account-security' })
    return
  }
  if (command === 'logout') {
    void auth.signOut()
  }
}

function visible<T extends { adminOnly?: boolean }>(items: T[]): T[] {
  return auth.isAdmin ? items : items.filter((item) => !item.adminOnly)
}

const visibleUseCaseItems = computed(() => visible(useCaseItems))
const visiblePlatformItems = computed(() => visible(platformItems))
const visibleSystemItems = computed(() => visible(systemItems))
const showPlatformGroup = computed(() => visiblePlatformItems.value.length > 0)

const allMenuItems = computed(() => [
  ...visibleUseCaseItems.value,
  ...visiblePlatformItems.value,
  ...visibleSystemItems.value,
])

const pageTitle = computed(() => {
  const meta = route.meta || {}
  if (typeof meta.title === 'string' && meta.title) return meta.title
  if (route.name === 'task-detail') return '任务详情'
  const hit = allMenuItems.value.find((m) => m.path === route.path)
  return hit?.label || 'CaseGen'
})

const pageDescription = computed(() => {
  const meta = route.meta || {}
  if (typeof meta.description === 'string') return meta.description
  if (route.name === 'task-detail') return '查看生成草稿、评审结果与执行时间线'
  return ''
})

function isActive(path: string): boolean {
  if (path === '/') return route.path === '/'
  return route.path === path || route.path.startsWith(path + '/')
}

const isPlatformActive = computed(() => activePlatformPaths.value.some((path) => isActive(path)))

function togglePlatform() {
  platformExpanded.value = !platformExpanded.value
}

function onPlatformGroupClick() {
  if (navCollapsed.value) {
    navCollapsed.value = false
    localStorage.setItem('casegen:nav-collapsed', '0')
    platformExpanded.value = true
    return
  }
  togglePlatform()
}

watch(
  () => route.path,
  (path) => {
    if (platformPaths.some((platformPath) => path === platformPath || path.startsWith(platformPath + '/'))) platformExpanded.value = true
  },
  { immediate: true },
)

let modelLoadSeq = 0

async function loadDefaultModel() {
  const seq = ++modelLoadSeq
  try {
    const models = await listModels()
    if (seq !== modelLoadSeq) return
    defaultModel.value = models.find((m) => m.is_default) || models[0] || null
  } catch {
    if (seq !== modelLoadSeq) return
    defaultModel.value = null
  }
}

const MODEL_REFRESH_INTERVAL_MS = 5 * 60_000
let modelTimer: number | null = null

function onModelsUpdated() {
  void loadDefaultModel()
}

let projectRefreshFlight: Promise<void> | null = null
let lastProjectRefreshAt = 0

// Long-lived tabs keep the module-level project list; refresh it when the
// window regains focus or the selector opens.
function refreshProjects(force = false): Promise<void> {
  if (!projects.state.loaded) return Promise.resolve()
  if (projectRefreshFlight) return projectRefreshFlight
  if (!force && Date.now() - lastProjectRefreshAt < 15_000) return Promise.resolve()
  lastProjectRefreshAt = Date.now()
  projectRefreshFlight = projects.refresh(router)
    .catch(() => {})
    .finally(() => {
      projectRefreshFlight = null
    })
  return projectRefreshFlight
}

function onProjectSelectVisible(visible: boolean) {
  if (visible) void refreshProjects()
}

function onWindowFocus() {
  if (document.visibilityState === 'visible') void refreshProjects()
}

function onVisibilityChange() {
  if (document.visibilityState === 'visible') void refreshProjects()
}

onMounted(async () => {
  window.addEventListener('focus', onWindowFocus)
  window.addEventListener(MODELS_UPDATED_EVENT, onModelsUpdated)
  document.addEventListener('visibilitychange', onVisibilityChange)
  setProjectInvalidHandler(() => refreshProjects(true))
  modelTimer = window.setInterval(() => {
    if (document.visibilityState === 'visible') {
      void loadDefaultModel()
    }
  }, MODEL_REFRESH_INTERVAL_MS)
  await Promise.all([loadDefaultModel(), projects.load(route.query)])
  await projects.ensureRouteProject(router)
})

onUnmounted(() => {
  modelLoadSeq += 1
  if (modelTimer != null) {
    window.clearInterval(modelTimer)
    modelTimer = null
  }
  window.removeEventListener('focus', onWindowFocus)
  window.removeEventListener(MODELS_UPDATED_EVENT, onModelsUpdated)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  setProjectInvalidHandler(null)
})

watch(
  () => route.query.project_id,
  async () => {
    if (projects.state.loaded) await projects.ensureRouteProject(router)
  },
)
</script>

<template>
  <el-container class="main-layout">
    <el-aside width="228px" class="sidebar" :class="{ 'sidebar--collapsed': navCollapsed }">
      <div class="logo">
        <BrandMark class="logo-mark" :size="36" title="CaseGen 用例叠层" />
        <div class="logo-text">
          <div class="logo-title">CaseGen</div>
          <div class="logo-sub">SPEC ENGINE // 规范工程平台</div>
        </div>
      </div>

      <nav class="nav">
        <section class="nav-group">
          <div class="nav-group-title">用例中心</div>
          <router-link
            v-for="item in visibleUseCaseItems"
            :key="item.path"
            :to="{ path: item.path, query: projectQuery }"
            class="nav-item"
            :class="{ active: isActive(item.path) }"
            :title="item.label"
          >
            <el-icon class="nav-icon" :size="18">
              <component :is="item.icon" />
            </el-icon>
            <span>{{ item.label }}</span>
          </router-link>
        </section>

        <section v-if="showPlatformGroup" class="nav-group wiki-group">
          <button type="button" class="nav-group-toggle" :class="{ active: isPlatformActive }" :aria-expanded="platformExpanded" aria-controls="platform-submenu" title="平台适配" @click="onPlatformGroupClick">
            <el-icon class="nav-icon" :size="18"><Connection /></el-icon><span>平台适配</span><el-icon class="nav-chevron" :size="14"><ArrowDown v-if="platformExpanded"/><ArrowRight v-else/></el-icon>
          </button>
          <div v-show="platformExpanded" id="platform-submenu" class="nav-submenu">
            <router-link v-for="item in visiblePlatformItems" :key="item.path" :to="{ path: item.path, query: projectQuery }" class="nav-item nav-subitem" :class="{active:isActive(item.path)}"><el-icon class="nav-icon" :size="17"><component :is="item.icon"/></el-icon><span>{{item.label}}</span></router-link>
          </div>
        </section>

        <section class="nav-group">
          <div class="nav-group-title">系统配置</div>
          <router-link
            v-for="item in visibleSystemItems"
            :key="item.path"
            :to="{ path: item.path, query: projectQuery }"
            class="nav-item"
            :class="{ active: isActive(item.path) }"
            :title="item.label"
          >
            <el-icon class="nav-icon" :size="18">
              <component :is="item.icon" />
            </el-icon>
            <span>{{ item.label }}</span>
          </router-link>
        </section>
      </nav>

      <button
        type="button"
        class="sidebar-collapse"
        :title="navCollapsed ? '展开导航' : '收起导航'"
        :aria-label="navCollapsed ? '展开导航' : '收起导航'"
        @click="toggleNav"
      >
        <el-icon :size="18"><Expand v-if="navCollapsed" /><Fold v-else /></el-icon>
        <span v-if="!navCollapsed">收起导航</span>
      </button>

      <div class="sidebar-footer">
        <div class="footer-label">Quality First</div>
        <div class="footer-hint">需求 → 检索 → 生成 → 评审</div>
      </div>
    </el-aside>

    <!-- direction=vertical: topbar above content (default horizontal left the title column empty) -->
    <el-container
      class="body"
      :class="{ 'body--fill': route.name === 'agent-chat', 'body--nav-collapsed': navCollapsed }"
      direction="vertical"
    >
      <header class="topbar">
        <div class="topbar-left">
          <h1 class="topbar-title">{{ pageTitle }}</h1>
          <p v-if="pageDescription" class="topbar-desc">{{ pageDescription }}</p>
        </div>
        <div class="topbar-right">
          <el-select v-if="projects.state.projects.length" :model-value="projects.state.currentId" class="project-select" aria-label="当前项目" @change="(id: number) => projects.select(id, router)" @visible-change="onProjectSelectVisible">
            <el-option v-for="project in projects.state.projects.filter((item) => item.status === 'active')" :key="project.id" :label="project.name" :value="project.id" />
          </el-select>
          <div v-if="defaultModel && route.name !== 'agent-chat'" class="model-chip" title="当前默认模型">
            <span class="model-dot" />
            <span class="model-label">默认模型</span>
            <span class="model-name">{{ defaultModel.name }} · {{ defaultModel.model_name }}</span>
          </div>
          <el-dropdown v-if="auth.user" trigger="click" @command="onUserCommand">
            <button type="button" class="user-chip" aria-label="用户菜单">
              <el-icon><UserFilled /></el-icon>
              <span class="user-name">{{ auth.user.display_name || auth.user.username }}</span>
              <el-icon class="user-caret"><ArrowDown /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="security" :icon="UserFilled">账号安全</el-dropdown-item>
                <el-dropdown-item command="logout" :icon="SwitchButton" divided>退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <el-main class="main-content">
        <router-view
          v-if="(route.meta.skipProjectGate || projects.state.loaded) && (route.meta.skipProjectGate || !projects.state.currentId || route.query.project_id)"
          :key="route.meta.skipProjectGate ? 'global' : (projects.state.currentId || 'no-project')"
        />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.main-layout {
  min-height: 100vh;
  background: var(--cg-bg);
}

.sidebar {
  position: fixed;
  inset: 0 auto 0 0;
  width: 228px !important;
  height: 100vh;
  z-index: 30;
  background: #09090b;
  color: var(--cg-text-on-dark);
  display: flex;
  flex-direction: column;
  border-right: 1px solid #18181b;
  overflow: hidden;
  transition: width 0.18s ease;
}

.logo {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 22px 18px 18px;
  border-bottom: 1px solid #18181b;
}

.logo-mark {
  flex: 0 0 auto;
}

.logo-title {
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: #fff;
}

.logo-sub {
  margin-top: 2px;
  font-size: 10.5px;
  font-family: var(--cg-font-mono);
  letter-spacing: 0.03em;
  color: #71717a;
}

.nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 14px 10px;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
}

.nav-item span,
.nav-group-toggle span,
.nav-group-title {
  white-space: nowrap;
}

.nav-group {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.nav-group + .nav-group {
  margin-top: 10px;
}

.nav-group-title {
  padding: 0 12px 3px;
  color: #71717a;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 6px;
  color: #a1a1aa;
  text-decoration: none;
  font-size: 14px;
  font-weight: 500;
  transition: background 0.15s ease, color 0.15s ease;
  position: relative;
}

.nav-group-toggle {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 12px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: #a1a1aa;
  font: inherit;
  font-size: 14px;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
  position: relative;
}

.nav-group-toggle:hover {
  background: rgba(255, 255, 255, 0.06);
  color: #ffffff;
}

.nav-group-toggle.active {
  color: #ffffff;
  background: rgba(255, 255, 255, 0.08);
}

.nav-group-toggle.active::before {
  content: "";
  position: absolute;
  left: 0;
  top: 8px;
  bottom: 8px;
  width: 2px;
  border-radius: 0 2px 2px 0;
  background: #10b981;
}

.nav-chevron {
  margin-left: auto;
  opacity: 0.78;
}

.nav-submenu {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 1px 0 0 18px;
  padding-left: 8px;
  border-left: 1px solid #18181b;
}

.nav-subitem {
  padding: 9px 10px;
  font-size: 13px;
}

.nav-item:hover {
  background: rgba(255, 255, 255, 0.06);
  color: #ffffff;
}

.nav-item.active {
  color: #ffffff;
  background: rgba(255, 255, 255, 0.08);
}

.nav-item.active::before {
  content: "";
  position: absolute;
  left: 0;
  top: 8px;
  bottom: 8px;
  width: 2px;
  border-radius: 0 2px 2px 0;
  background: #10b981;
}

.nav-icon {
  opacity: 0.9;
}

.nav-item:focus-visible,
.nav-group-toggle:focus-visible {
  outline: 2px solid #10b981;
  outline-offset: 2px;
}

.sidebar-footer {
  flex: 0 0 auto;
  height: 68px;
  box-sizing: border-box;
  overflow: hidden;
  padding: 16px 18px 20px;
  border-top: 1px solid #18181b;
}

.footer-label {
  font-size: 12px;
  font-weight: 700;
  color: rgba(255, 255, 255, 0.78);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.footer-hint {
  margin-top: 4px;
  font-size: 11px;
  color: var(--cg-text-on-dark-muted);
}

.body {
  flex: 1;
  margin-left: 228px;
  min-width: 0;
  min-height: 100vh;
  background: var(--cg-bg-glow);
  transition: margin-left 0.18s ease;
}

.sidebar--collapsed {
  width: 68px !important;
}

.body--nav-collapsed {
  margin-left: 68px;
}

.sidebar--collapsed .logo {
  padding: 18px 0;
  justify-content: center;
}

.sidebar--collapsed .logo-text,
.sidebar--collapsed .nav-group-title,
.sidebar--collapsed .nav-item span,
.sidebar--collapsed .nav-group-toggle span,
.sidebar--collapsed .nav-chevron,
.sidebar--collapsed .nav-submenu,
.sidebar--collapsed .sidebar-collapse span {
  display: none;
}

.sidebar--collapsed .sidebar-footer {
  visibility: hidden;
  padding-left: 0;
  padding-right: 0;
}

.sidebar--collapsed .nav {
  padding: 14px 8px;
}

.sidebar--collapsed .nav-item,
.sidebar--collapsed .nav-group-toggle {
  justify-content: center;
  padding-left: 0;
  padding-right: 0;
  gap: 0;
}

.sidebar-collapse {
  align-self: flex-start;
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  width: auto;
  height: 36px;
  margin: 0 16px 12px;
  padding: 0 12px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: #a1a1aa;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}

.sidebar-collapse:hover {
  background: rgba(255, 255, 255, 0.06);
  color: #fff;
}

.sidebar--collapsed .sidebar-collapse {
  justify-content: flex-start;
  margin-left: 16px;
  margin-right: 0;
  padding: 0 12px;
}

.body--fill {
  height: 100vh;
  min-height: 0;
  overflow: hidden;
}

.body--fill .main-content {
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex: 0 0 auto;
  width: 100%;
  min-height: 64px;
  padding: 12px 28px;
  background: rgba(255, 255, 255, 0.82);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--cg-border);
  position: sticky;
  top: 0;
  z-index: 20;
}

.topbar-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  letter-spacing: -0.02em;
}

.topbar-desc {
  margin: 3px 0 0;
  font-size: 12.5px;
  color: var(--cg-text-muted);
}

.model-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-radius: 999px;
  background: #ffffff;
  border: 1px solid #e4e4e7;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
  font-size: 12px;
  font-family: var(--cg-font-mono);
  max-width: 360px;
}

.topbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.project-select { width: 180px; }

.model-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #10b981;
  box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.2);
  animation: pulse-emerald 2s infinite ease-in-out;
  flex-shrink: 0;
}

@keyframes pulse-emerald {
  0%, 100% {
    transform: scale(1);
    opacity: 1;
  }
  50% {
    transform: scale(1.15);
    opacity: 0.8;
    box-shadow: 0 0 0 4px rgba(16, 185, 129, 0.15);
  }
}

.model-label {
  color: var(--cg-text-muted);
  flex-shrink: 0;
}

.model-name {
  color: var(--cg-text);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.user-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--cg-text-muted);
  font: inherit;
  font-size: 12px;
  white-space: nowrap;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}

.user-chip:hover,
.user-chip:focus-visible {
  background: var(--cg-surface-muted);
  color: var(--cg-text);
}

.user-name {
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.user-caret {
  opacity: 0.7;
}

.main-content {
  flex: 1;
  width: 100%;
  padding: 24px 28px 32px;
  background: transparent;
}

@media (max-width: 900px) {
  .sidebar {
    position: relative;
    inset: auto;
    width: 228px !important;
    height: auto;
  }

  .body {
    margin-left: 0;
  }

  .body--nav-collapsed {
    margin-left: 0;
  }

  .body--fill {
    height: auto;
    min-height: 100vh;
    overflow: visible;
  }

  .body--fill .main-content {
    overflow: visible;
    display: block;
  }

  .topbar {
    padding: 12px 16px;
  }

  .main-content {
    padding: 16px;
  }

  .model-chip {
    max-width: 200px;
  }

  .user-name {
    max-width: 90px;
  }
}
</style>
