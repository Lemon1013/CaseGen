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
      meta: { title: '登录' },
    },
    {
      path: '/setup',
      name: 'setup',
      component: () => import('../views/SetupView.vue'),
      meta: { title: '初始化管理员' },
    },
    {
      path: '/register',
      name: 'register',
      component: () => import('../views/RegisterView.vue'),
      meta: { title: '注册' },
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
          path: 'dashboard',
          name: 'dashboard',
          component: () => import('../views/DashboardView.vue'),
          meta: { title: '数据看板', description: '全站运营与用例生成质量概览', adminOnly: true, skipProjectGate: true },
        },
        {
          path: 'account/security',
          name: 'account-security',
          component: () => import('../views/AccountSecurityView.vue'),
          meta: { title: '账号安全', description: '修改登录密码，保护账号与项目数据安全', skipProjectGate: true },
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
          path: 'agent',
          name: 'agent-chat',
          component: () => import('../views/AgentChatView.vue'),
          meta: { title: 'Agent 对话', description: '选择专家技能，与 Agent 多轮对话生成测试用例' },
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
          meta: { title: '数据池', description: '导入并预览项目级实体数据与抽象测试数据', adminOnly: true },
        },
        {
          path: 'platform-adapters',
          name: 'platform-adapters',
          redirect: '/platform-management',
          meta: { adminOnly: true },
        },
        {
          path: 'platform-management',
          name: 'platform-management',
          component: () => import('../views/PlatformManagementView.vue'),
          meta: { title: '平台管理', description: '维护平台、示例类型及其格式样例', adminOnly: true },
        },
        {
          path: 'platform-case-generate',
          name: 'platform-case-generate',
          component: () => import('../views/PlatformCaseGeneratorView.vue'),
          meta: { title: '生成平台用例', description: '根据语义用例、示例类型和数据生成平台产物', adminOnly: true },
        },
        {
          path: 'platform-cases',
          name: 'platform-cases',
          component: () => import('../views/PlatformCasesView.vue'),
          meta: { title: '平台用例管理', description: '查看、预览和维护生成产物的管理副本', adminOnly: true },
        },
        {
          path: 'platform-upload',
          name: 'platform-upload',
          component: () => import('../views/PlatformUploadView.vue'),
          meta: { title: '上传至测试平台', description: '查看并下载生成产物，等待后续连接器推送', adminOnly: true },
        },
        {
          path: 'spaces',
          name: 'space-management',
          component: () => import('../views/SpaceManagementView.vue'),
          meta: { title: '项目管理', description: '管理项目与多外部知识库连接配置', adminOnly: true },
        },
        {
          path: 'prompts',
          name: 'prompts',
          component: () => import('../views/PromptsView.vue'),
          meta: { title: '提示词管理', description: '维护生成 / 测试点 / 评审等提示词模板' },
        },
        {
          path: 'models',
          name: 'models',
          component: () => import('../views/ModelsView.vue'),
          meta: { title: '模型配置', description: '配置 OpenAI 兼容模型网关与默认模型', adminOnly: true },
        },
        {
          path: 'users',
          name: 'users',
          component: () => import('../views/UsersView.vue'),
          meta: { title: '用户管理', description: '管理本地账号、角色与登录口令', adminOnly: true, skipProjectGate: true },
        },
      ],
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      redirect: '/',
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
  if (to.meta.adminOnly && !auth.isAdmin) {
    return { name: 'workbench' }
  }
  if ((to.name === 'login' || to.name === 'setup' || to.name === 'register') && auth.isAuthenticated) {
    return { name: 'workbench' }
  }
  return true
})

export default router
