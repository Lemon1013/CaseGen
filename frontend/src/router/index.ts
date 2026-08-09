import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'workbench',
      component: () => import('../views/WorkbenchView.vue'),
      meta: {
        title: '工作台',
        description: '填写需求，创建任务并生成测试用例草稿',
      },
    },
    {
      path: '/tasks',
      name: 'tasks',
      component: () => import('../views/TaskListView.vue'),
      meta: {
        title: '任务列表',
        description: '浏览生成任务、状态与最新评审分数',
      },
    },
    {
      path: '/tasks/:id',
      name: 'task-detail',
      component: () => import('../views/TaskDetailView.vue'),
      meta: {
        title: '任务详情',
        description: '查看生成草稿、评审结果与执行时间线',
      },
    },
    {
      path: '/documents',
      name: 'documents',
      component: () => import('../views/DocumentsView.vue'),
      meta: {
        title: '文档管理',
        description: '上传业务文档并编译到 LLM-Wiki',
      },
    },
    {
      path: '/wiki',
      name: 'wiki',
      component: () => import('../views/WikiView.vue'),
      meta: {
        title: 'Wiki 浏览',
        description: '检索与预览知识库页面，作为生成依据',
      },
    },
    {
      path: '/wiki/reviews',
      name: 'wiki-reviews',
      component: () => import('../views/WikiReviewView.vue'),
      meta: {
        title: 'Wiki 审核',
        description: '审查候选变更、来源证据、历史版本与回滚记录',
      },
    },
    {
      path: '/wiki-spaces',
      name: 'wiki-spaces',
      component: () => import('../views/WikiSpacesView.vue'),
      meta: {
        title: 'Wiki 空间',
        description: '按项目管理隔离的 Wiki 文档、页面与审核空间',
      },
    },
    {
      path: '/prompts',
      name: 'prompts',
      component: () => import('../views/PromptsView.vue'),
      meta: {
        title: '提示词管理',
        description: '维护生成 / 评审 / Wiki 等提示词模板',
      },
    },
    {
      path: '/models',
      name: 'models',
      component: () => import('../views/ModelsView.vue'),
      meta: {
        title: '模型配置',
        description: '配置 OpenAI 兼容模型网关与默认模型',
      },
    },
  ],
})

export default router
