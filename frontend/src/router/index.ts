import { createRouter, createWebHistory } from 'vue-router'
import MainLayout from '../layouts/MainLayout.vue'
import { useAuthStore } from '../authStore'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/LoginView.vue'),
      meta: { title: '登录', public: true },
    },
    {
      path: '/setup',
      name: 'setup',
      component: () => import('../views/SetupView.vue'),
      meta: { title: '初始化管理员', public: true },
    },
    {
      path: '/',
      component: MainLayout,
      meta: { requiresAuth: true },
      children: [
        {
          path: '',
          name: 'workbench',
          component: () => import('../views/WorkbenchView.vue'),
          meta: { title: '用例生成', description: '填写需求，创建任务并生成测试用例草稿' },
        },
        {
          path: 'tasks',
          name: 'tasks',
          component: () => import('../views/TaskListView.vue'),
          meta: { title: '生成任务', description: '浏览生成任务、执行状态与最新评审分数' },
        },
        {
          path: 'tasks/:id',
          name: 'task-detail',
          component: () => import('../views/TaskDetailView.vue'),
          meta: { title: '生成任务详情', description: '查看生成草稿、评审结果与执行时间线' },
        },
        {
          path: 'cases',
          name: 'cases',
          component: () => import('../views/CasesView.vue'),
          meta: { title: '用例管理', description: '按需求管理已入库用例，编辑当前内容并导出 Markdown' },
        },
        {
          path: 'data-pools',
          name: 'data-pools',
          component: () => import('../views/DataPoolsView.vue'),
          meta: { title: '数据池', description: '导入并预览项目级实体数据与抽象测试数据' },
        },
        {
          path: 'platform-adapters',
          name: 'platform-adapters',
          redirect: '/platform-management',
        },
        {
          path: 'platform-management',
          name: 'platform-management',
          component: () => import('../views/PlatformManagementView.vue'),
          meta: { title: '平台管理', description: '维护平台、示例类型及其格式样例' },
        },
        {
          path: 'platform-case-generate',
          name: 'platform-case-generate',
          component: () => import('../views/PlatformCaseGeneratorView.vue'),
          meta: { title: '生成平台用例', description: '根据语义用例、示例类型和数据生成平台产物' },
        },
        {
          path: 'platform-cases',
          name: 'platform-cases',
          component: () => import('../views/PlatformCasesView.vue'),
          meta: { title: '平台用例管理', description: '查看、预览和维护生成产物的管理副本' },
        },
        {
          path: 'platform-upload',
          name: 'platform-upload',
          component: () => import('../views/PlatformUploadView.vue'),
          meta: { title: '上传至测试平台', description: '查看并下载生成产物，等待后续连接器推送' },
        },
        {
          path: 'documents',
          name: 'documents',
          component: () => import('../views/DocumentsView.vue'),
          meta: { title: '文档摄入', description: '上传业务文档、检查解析质量并摄入当前 Wiki 空间' },
        },
        {
          path: 'wiki',
          name: 'wiki',
          component: () => import('../views/WikiView.vue'),
          meta: { title: '知识浏览', description: '检索与预览当前 Wiki 空间的知识页面和原文依据' },
        },
        {
          path: 'wiki/reviews',
          name: 'wiki-reviews',
          component: () => import('../views/WikiReviewView.vue'),
          meta: { title: '变更审核', description: '审核 Wiki 候选变更、来源证据、历史版本与回滚记录' },
        },
        {
          path: 'wiki-spaces',
          name: 'wiki-spaces',
          component: () => import('../views/WikiSpacesView.vue'),
          meta: { title: '知识空间', description: '按项目管理隔离的 Wiki 文档、页面、审核与检索边界' },
        },
        {
          path: 'spaces',
          name: 'space-management',
          component: () => import('../views/SpaceManagementView.vue'),
          meta: { title: '空间管理', description: '管理全站项目空间及其生命周期' },
        },
        {
          path: 'prompts',
          name: 'prompts',
          component: () => import('../views/PromptsView.vue'),
          meta: { title: '提示词管理', description: '维护生成 / 评审 / Wiki 等提示词模板' },
        },
        {
          path: 'models',
          name: 'models',
          component: () => import('../views/ModelsView.vue'),
          meta: { title: '模型配置', description: '配置 OpenAI 兼容模型网关与默认模型' },
        },
      ],
    },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  await auth.restoreSession()

  if (auth.restoreUnavailable) {
    if (to.name === 'login' || to.name === 'setup') return true
    return { name: 'login', query: to.fullPath === '/' ? undefined : { redirect: to.fullPath } }
  }

  if (auth.setupRequired) {
    if (to.name === 'setup') return true
    return { name: 'setup', query: to.fullPath === '/' ? undefined : { redirect: to.fullPath } }
  }
  if (to.name === 'setup') return { name: 'login' }
  if (to.meta.requiresAuth && !auth.isAuthenticated) {
    return { name: 'login', query: to.fullPath === '/' ? undefined : { redirect: to.fullPath } }
  }
  if ((to.name === 'login' || to.name === 'setup') && auth.isAuthenticated) {
    return { name: 'workbench' }
  }
  return true
})

export default router
