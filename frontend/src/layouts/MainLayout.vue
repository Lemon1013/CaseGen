<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  Collection,
  CircleCheck,
  Cpu,
  Document,
  EditPen,
  List,
  Monitor,
  Reading,
} from '@element-plus/icons-vue'
import { listModels, type ModelConfig } from '../api/models'

const route = useRoute()
const defaultModel = ref<ModelConfig | null>(null)

const menuItems = [
  { path: '/', label: '工作台', icon: Monitor },
  { path: '/tasks', label: '任务列表', icon: List },
  { path: '/documents', label: '文档管理', icon: Document },
  { path: '/wiki', label: 'Wiki', icon: Reading },
  { path: '/wiki/reviews', label: 'Wiki 审核', icon: CircleCheck },
  { path: '/prompts', label: '提示词', icon: EditPen },
  { path: '/models', label: '模型配置', icon: Cpu },
]

const pageTitle = computed(() => {
  const meta = route.meta || {}
  if (typeof meta.title === 'string' && meta.title) return meta.title
  if (route.name === 'task-detail') return '任务详情'
  const hit = menuItems.find((m) => m.path === route.path)
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
  if (path === '/wiki') return route.path === '/wiki'
  return route.path === path || route.path.startsWith(path + '/')
}

async function loadDefaultModel() {
  try {
    const models = await listModels()
    defaultModel.value = models.find((m) => m.is_default) || models[0] || null
  } catch {
    defaultModel.value = null
  }
}

onMounted(loadDefaultModel)
</script>

<template>
  <el-container class="main-layout">
    <el-aside width="228px" class="sidebar">
      <div class="sidebar-accent" />
      <div class="logo">
        <div class="logo-mark">
          <el-icon :size="16"><Collection /></el-icon>
        </div>
        <div class="logo-text">
          <div class="logo-title">CaseGen</div>
          <div class="logo-sub">AI 测试用例平台</div>
        </div>
      </div>

      <nav class="nav">
        <router-link
          v-for="item in menuItems"
          :key="item.path"
          :to="item.path"
          class="nav-item"
          :class="{ active: isActive(item.path) }"
        >
          <el-icon class="nav-icon" :size="18">
            <component :is="item.icon" />
          </el-icon>
          <span>{{ item.label }}</span>
        </router-link>
      </nav>

      <div class="sidebar-footer">
        <div class="footer-label">Quality First</div>
        <div class="footer-hint">文档 → Wiki → 生成 → 评审</div>
      </div>
    </el-aside>

    <!-- direction=vertical: topbar above content (default horizontal left the title column empty) -->
    <el-container class="body" direction="vertical">
      <header class="topbar">
        <div class="topbar-left">
          <h1 class="topbar-title">{{ pageTitle }}</h1>
          <p v-if="pageDescription" class="topbar-desc">{{ pageDescription }}</p>
        </div>
        <div class="topbar-right">
          <div v-if="defaultModel" class="model-chip" title="当前默认模型">
            <span class="model-dot" />
            <span class="model-label">默认模型</span>
            <span class="model-name">{{ defaultModel.name }} · {{ defaultModel.model_name }}</span>
          </div>
        </div>
      </header>

      <el-main class="main-content">
        <router-view />
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
  position: relative;
  background: linear-gradient(180deg, #0b1220 0%, var(--cg-sidebar) 40%, #05080f 100%);
  color: var(--cg-text-on-dark);
  display: flex;
  flex-direction: column;
  border-right: 1px solid rgba(255, 255, 255, 0.06);
  overflow: hidden;
}

.sidebar-accent {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: var(--cg-gradient-brand);
  box-shadow: 0 0 18px rgba(79, 124, 255, 0.65);
}

.logo {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 22px 18px 18px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.logo-mark {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  background: var(--cg-gradient-brand);
  color: #fff;
  box-shadow: 0 0 0 4px rgba(79, 124, 255, 0.15), 0 8px 20px rgba(79, 124, 255, 0.35);
}

.logo-title {
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: #fff;
}

.logo-sub {
  margin-top: 2px;
  font-size: 11px;
  color: var(--cg-text-on-dark-muted);
}

.nav {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px 10px;
  flex: 1;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 11px 12px;
  border-radius: 10px;
  color: var(--cg-text-on-dark-muted);
  text-decoration: none;
  font-size: 14px;
  font-weight: 500;
  transition: background 0.15s ease, color 0.15s ease, box-shadow 0.15s ease;
  position: relative;
}

.nav-item:hover {
  background: rgba(255, 255, 255, 0.05);
  color: var(--cg-text-on-dark);
}

.nav-item.active {
  color: #fff;
  background: linear-gradient(
    90deg,
    rgba(79, 124, 255, 0.22),
    rgba(139, 92, 246, 0.12)
  );
  box-shadow: inset 0 0 0 1px rgba(79, 124, 255, 0.18);
}

.nav-item.active::before {
  content: "";
  position: absolute;
  left: 0;
  top: 10px;
  bottom: 10px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--cg-gradient-brand);
}

.nav-icon {
  opacity: 0.9;
}

.sidebar-footer {
  padding: 16px 18px 20px;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
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
  min-width: 0;
  min-height: 100vh;
  background: var(--cg-bg-glow);
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
  padding: 7px 12px;
  border-radius: 999px;
  background: #fff;
  border: 1px solid var(--cg-border);
  box-shadow: var(--cg-shadow);
  font-size: 12px;
  max-width: 360px;
}

.model-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--cg-accent);
  box-shadow: 0 0 0 3px rgba(18, 184, 134, 0.18);
  flex-shrink: 0;
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

.main-content {
  flex: 1;
  width: 100%;
  padding: 24px 28px 32px;
  background: transparent;
}

@media (max-width: 900px) {
  .topbar {
    padding: 12px 16px;
  }

  .main-content {
    padding: 16px;
  }

  .model-chip {
    max-width: 200px;
  }
}
</style>
